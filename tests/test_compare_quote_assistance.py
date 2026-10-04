"""Bounded MECHANICAL quote-length assistance for the frozen compare page.

These exercise the real ``Evolution._compare_stage`` / ``process`` paths and the
pure ``comparison_review`` validators with synthetic fixtures only: no live
vault, model, credentials or network is touched. A model stub proves machine
behaviour, not semantic quality.

The core failure: a comparer that correctly recognises an overlap but quotes a
~170-character sentence. Strict validation (the existing 120-char quote bound)
rejects it. The fix hands the SAME comparer back at most ONE short exact locator
(a prefix of that *same* exact substring), under the op/card identity, on the
next repair payload -- as assistance, never as acceptance. A fabricated or
non-substring quote yields no suggestion, the quote bound is never relaxed, no
verdict is auto-accepted, and no body/quote text leaks into the audit or the
completion summary.
"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tongle.engine import Engine
from tongle.evolution import (Evolution, canonical, fingerprint)
from tongle.model_refs import encode_references
from tongle.comparison_review import (COMPARE_REPAIR_WORST_CASE,
                                      COMPARE_SUGGESTION_NOTE,
                                      MAX_COMPARE_QUOTE_CHARS,
                                      MAX_COMPARE_SUGGESTION_CHARS,
                                      compare_repair_field,
                                      validate_compare_response)

try:  # sibling import under ``unittest discover -s tests``
    from fact_review_stub import with_fact_audit
except ImportError:  # pragma: no cover - package-import invocation
    from tests.fact_review_stub import with_fact_audit


class _AssistHarness(unittest.TestCase):
    MAIN_TEXT = '触发一次局部维护。'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.e = Engine(self.root / 'state')
        self.e.init(self.root / 'vault'); self.addCleanup(self.e.store.close)
        self.write_config()
        self.ev = Evolution(self.e)
        self._call_seq = [0]

    def write_config(self, **overrides):
        cfg = {'knowledge_mode': 'evolving', 'model_command': ['stub'],
               'knowledge_quiet_seconds': 0,
               'knowledge_daily_token_budget': 100000000,
               'knowledge_daily_call_budget': 100000}
        cfg.update(overrides)
        (self.e.home / 'config.json').write_text(json.dumps(cfg))

    def seed(self, key, body, **kw):
        return self.ev.cards.seed('project', key, 'memory', kw.pop('topic', '主题'),
                                  key, body, **kw)

    def capture(self, text=None, event='e', role='user', session='session'):
        rec = self.e.store.capture('project', session, role, event, text or self.MAIN_TEXT)
        self.ev.captured(rec['source_id'], role, session)
        return rec

    def _source(self, rec):
        return self.e.store.source(rec['source_id'])

    def run_model(self, model, force=True):
        with patch('tongle.worker.invoke_json', side_effect=with_fact_audit(model)):
            return self.ev.process(force=force)

    def work(self, state=None):
        sql = 'SELECT * FROM knowledge_work'; args = ()
        if state:
            sql += ' WHERE state=?'; args = (state,)
        return [dict(r) for r in self.e.db.execute(sql, args).fetchall()]

    def _create_op(self, key, title, body, source):
        return {'op': 'create', 'key': key, 'kind': 'memory', 'topic': '主题',
                'title': title, 'body': body,
                'evidence': [{'source_id': source['source_id'],
                              'version': source['version'], 'quote': source['text']}],
                'reason': '新增材料'}

    def _record_call(self, job, stage='compare'):
        self._call_seq[0] += 1
        call_id = 'call%04d' % self._call_seq[0]
        with self.e.transaction():
            self.e.db.execute(
                'INSERT INTO knowledge_calls(id,work_id,stage,day,reserved,elapsed,created_at,error)'
                ' VALUES(?,?,?,?,?,0,?,NULL)',
                (call_id, job['id'], stage, '2026-01-01', 1, time.time()))
        self.ev._last_call_id = call_id
        return call_id

    def _unit_context(self, text=None, wid='w'):
        path = self.root / (wid + '.md')
        path.write_text(text or self.MAIN_TEXT)
        info = self.e.store.ingest('project', str(path))
        source = self.e.store.source(info['source_id'])
        part = list(self.ev._source_parts(source))[0]
        self._unit_path = path
        related = self.ev._related('project', [part], None)
        guard = related.pop('guard', {})
        related.pop('related_hits', None)
        authorized = self.ev._authorized_catalog('project')
        catalog_cards = [{'id': c['id'], 'version': c['version'], 'project': c['project'],
                          'state': c['state'], 'basis_cards': c.get('basis_cards') or []}
                         for c in authorized]
        base = {'project': 'project', 'sources': [part], 'existing': related,
                'source_snapshot': {part['source_id']: part['version']},
                'catalog_cards': catalog_cards, 'guard': guard,
                'scope_fingerprint': self.ev._scope_fingerprint('project'),
                'stage_calls': {'compile': 0, 'verify': 0},
                'read_sets': {'compile': {}, 'verify': {}},
                'read_feedback': {'compile': [], 'verify': []}}
        with self.e.transaction():
            self.e.db.execute(
                'INSERT OR REPLACE INTO knowledge_work(id,project,session,inputs,state,'
                'lease_token,lease_until,created_at) VALUES(?,?,?,?,?,?,?,?)',
                (wid, 'project', 'session', json.dumps([part]), 'running', 'tok',
                 time.time() + 600, time.time()))
        job = dict(self.e.db.execute(
            'SELECT * FROM knowledge_work WHERE id=?', (wid,)).fetchone())
        return base, job, part

    def _unit_op(self):
        # Shares '共同令牌' with the seeded card so exactly one related pair
        # (one page) is planned.
        return {'op': 'create', 'key': '共同令牌', 'kind': 'memory', 'topic': '主题',
                'title': '共同令牌', 'body': '共同令牌的新说明。',
                'evidence': [{'source_id': 'f' * 32, 'version': 1, 'quote': 'x'}],
                'reason': '新增'}

    def _seed_single(self):
        return self.seed('共同令牌卡', '共同令牌' + '密' * 300)

    def _compare_format_rows(self, work_id=None):
        sql = "SELECT error FROM knowledge_failures WHERE state='compare_format'"
        args = ()
        if work_id:
            sql += ' AND work_id=?'; args = (work_id,)
        return [r['error'] for r in self.e.db.execute(sql, args).fetchall()]

    @staticmethod
    def _valid_reason():
        return 'scope and claim clearly differ from the proposed change here'


class SuggestionDerivationTests(_AssistHarness):
    def _page(self):
        body = '旧卡甲的完整正文内容' + '细节' * 200
        cid = 'a' * 32
        return {'pairs': [[0, cid]], 'cards': [cid]}, {cid: body}, cid, body

    def test_overlong_exact_quote_yields_short_exact_suggestion(self):
        page, bodies, cid, body = self._page()
        overlong = body[:170]
        self.assertGreater(len(overlong), MAX_COMPARE_QUOTE_CHARS)
        result = validate_compare_response(
            {'pairs': [{'op': 0, 'card_id': cid, 'relation': 'distinct',
                        'quote': overlong, 'reason': self._valid_reason()}]}, page, bodies)
        self.assertFalse(result['ok'])
        self.assertFalse(result.get('dispositions'))
        suggestion = result['suggestion']
        self.assertEqual(suggestion['op'], 0)
        self.assertEqual(suggestion['card_id'], cid)
        self.assertTrue(suggestion['quote'])
        self.assertLessEqual(len(suggestion['quote']), MAX_COMPARE_SUGGESTION_CHARS)
        self.assertLessEqual(len(suggestion['quote']), 80)
        # The suggestion is a genuine exact substring of the supplied body, and
        # a prefix of the exact quote the comparer originally sent.
        self.assertIn(suggestion['quote'], body)
        self.assertTrue(overlong.startswith(suggestion['quote']))

    def test_fabricated_overlong_quote_yields_no_suggestion(self):
        page, bodies, cid, _body = self._page()
        fabricated = '不存在的引文' * 40          # 200 chars, not in the body
        result = validate_compare_response(
            {'pairs': [{'op': 0, 'card_id': cid, 'relation': 'distinct',
                        'quote': fabricated, 'reason': self._valid_reason()}]}, page, bodies)
        self.assertFalse(result['ok'])
        self.assertNotIn('suggestion', result)

    def test_suggestion_carried_in_repair_slot_with_identity_and_instruction(self):
        page, bodies, cid, body = self._page()
        result = validate_compare_response(
            {'pairs': [{'op': 0, 'card_id': cid, 'relation': 'distinct',
                        'quote': body[:170], 'reason': self._valid_reason()}]}, page, bodies)
        repair = compare_repair_field(2, ['op=0 card=card001 field=quote: too long'],
                                      result.get('suggestion'))
        suggestion = repair['suggestion']
        self.assertEqual(suggestion['op'], 0)
        self.assertEqual(suggestion['card_id'], cid)
        self.assertEqual(suggestion['quote'], body[:MAX_COMPARE_SUGGESTION_CHARS])
        self.assertEqual(suggestion['note'], COMPARE_SUGGESTION_NOTE)
        self.assertIn('copy', suggestion['note'])
        # No suggestion is added when none was derived.
        bare = compare_repair_field(2, ['op=0 field=quote: too long'])
        self.assertNotIn('suggestion', bare)

    def test_copied_suggestion_does_not_excuse_missing_relation_reason_or_coverage(self):
        body = '旧卡甲的完整正文内容' + '细节' * 200
        cid = 'a' * 32; other = 'b' * 32
        one = {'pairs': [[0, cid]], 'cards': [cid]}
        bodies = {cid: body, other: body}
        locator = validate_compare_response(
            {'pairs': [{'op': 0, 'card_id': cid, 'relation': 'distinct',
                        'quote': body[:170], 'reason': self._valid_reason()}]},
            one, bodies)['suggestion']['quote']
        # relation omitted
        self.assertFalse(validate_compare_response(
            {'pairs': [{'op': 0, 'card_id': cid, 'quote': locator,
                        'reason': self._valid_reason()}]}, one, bodies)['ok'])
        # reason omitted (too short)
        self.assertFalse(validate_compare_response(
            {'pairs': [{'op': 0, 'card_id': cid, 'relation': 'distinct',
                        'quote': locator, 'reason': 'short'}]}, one, bodies)['ok'])
        # missing pair coverage even though the copied locator is valid
        two = {'pairs': [[0, cid], [1, other]], 'cards': [cid, other]}
        covered = validate_compare_response(
            {'pairs': [{'op': 0, 'card_id': cid, 'relation': 'distinct',
                        'quote': locator, 'reason': self._valid_reason()}]}, two, bodies)
        self.assertFalse(covered['ok'])
        self.assertEqual(covered['error'], 'incomplete pair coverage')
        # ... and when the suggestion is copied properly the pair is accepted.
        accepted = validate_compare_response(
            {'pairs': [{'op': 0, 'card_id': cid, 'relation': 'distinct',
                        'quote': locator, 'reason': self._valid_reason()}]}, one, bodies)
        self.assertTrue(accepted['ok'])
        self.assertEqual(accepted['dispositions'][0]['quote'], locator)


class SuggestionRepairFlowTests(_AssistHarness):
    def test_invalid_overlong_quote_then_valid_suggestion_accepted_no_auto_accept(self):
        self._seed_single()
        base, job, _part = self._unit_context()
        op = self._unit_op()
        payloads = []

        def side_effect(_job, stage, payload, _cfg):
            self._record_call(_job, stage)
            payloads.append(payload)
            pair = payload['page']['pairs'][0]
            cand = {c['card_id']: c for c in payload['page']['candidates']}
            body = cand[pair['card_id']]['body']
            if len(payloads) == 1:
                # A strictly-invalid overlong quote that IS an exact substring.
                return {'pairs': [{'op': pair['op'], 'card_id': pair['card_id'],
                                   'relation': 'distinct', 'quote': body[:170],
                                   'reason': self._valid_reason()}]}
            suggestion = payload['page_repair']['suggestion']
            return {'pairs': [{'op': pair['op'], 'card_id': suggestion['card_id'],
                               'relation': 'distinct', 'quote': suggestion['quote'],
                               'reason': self._valid_reason()}]}

        with patch.object(self.ev, '_call', side_effect=side_effect):
            reports, findings = self.ev._compare_stage(job, base, [op], 'tok', {})
        self.assertEqual(findings, [])
        self.assertTrue(reports)
        # The invalid first reply was NOT accepted: it took a second dispatch
        # that carried the bounded suggestion, which the comparer then copied.
        self.assertEqual(len(payloads), 2)
        self.assertIsNone((payloads[0].get('page_repair') or {}).get('suggestion'))
        suggestion = payloads[1]['page_repair']['suggestion']
        self.assertLessEqual(len(suggestion['quote']), MAX_COMPARE_SUGGESTION_CHARS)
        self.assertIn(suggestion['quote'], payloads[0]['page']['candidates'][0]['body'])
        self.assertEqual(len(base['compare']['pages']), 1)      # exactly one receipt
        # Durable format evidence exists, but it is bounded and body-free.
        rows = self._compare_format_rows(job['id'])
        self.assertEqual(len(rows), 1)
        self.assertIn('quote', rows[0])
        self.assertNotIn('密', rows[0])

    def test_process_repairs_in_place_and_leaks_no_quote_text(self):
        self.seed('共同令牌卡', '共同令牌' + '密' * 300)
        rec = self.capture()
        source = self._source(rec)
        calls = {'compare': 0, 'compile': 0}

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'frozen_proposal' in data:
                calls['compare'] += 1
                pair = data['page']['pairs'][0]
                cand = {c['card_id']: c for c in data['page']['candidates']}
                body = cand[pair['card_id']]['body']
                if calls['compare'] == 1:
                    return {'pairs': [{'op': pair['op'], 'card_id': pair['card_id'],
                                       'relation': 'distinct', 'quote': body[:170],
                                       'reason': self._valid_reason()}]}, {}
                suggestion = data['page_repair']['suggestion']
                return {'pairs': [{'op': pair['op'], 'card_id': suggestion['card_id'],
                                   'relation': 'distinct', 'quote': suggestion['quote'],
                                   'reason': self._valid_reason()}]}, {}
            if 'proposal' in data:
                return {'approved': True, 'findings': []}, {}
            calls['compile'] += 1
            return {'operations': [self._create_op(
                '共同令牌', '共同令牌', '共同令牌的新说明。', source)]}, {}

        result = self.run_model(model)
        self.assertEqual(result['processed'], 1)
        self.assertEqual(result['failed'], 0)
        self.assertEqual(calls['compare'], 2)                   # no auto-accept
        self.assertEqual(calls['compile'], 1)                   # no recompile
        rows = [r['error'] for r in self.e.db.execute(
            "SELECT error FROM knowledge_failures WHERE state IN ('compare_format','completion')"
        ).fetchall()]
        self.assertTrue(rows)
        completion = [r for r in rows if '"comparison"' in r or 'format_repairs' in r]
        self.assertTrue(completion)
        record = json.loads(completion[0])
        self.assertEqual(record['comparison']['format_repairs'], 1)
        self.assertTrue(record['comparison']['all_distinct'])
        # No body or quote text ever reaches the audit or the completion summary.
        for text in rows:
            self.assertNotIn('密', text)
            self.assertNotIn('旧卡甲的完整正文内容', text)


class SuggestionBytePlanTests(_AssistHarness):
    def test_four_byte_unicode_suggestion_fits_reserved_worst_case_and_preflight(self):
        card = self._seed_single()
        base, _job, _part = self._unit_context()
        op = self._unit_op()
        cards = self.ev._authorized_catalog('project')
        by_id = {c['id']: c for c in cards}
        proposal_hash = fingerprint([op])
        emoji = '\U0001F600' * MAX_COMPARE_SUGGESTION_CHARS      # 80 * 4 UTF-8 bytes
        repair = compare_repair_field(
            2, ['op=0 field=quote: too long'],
            {'op': 0, 'card_id': card['id'], 'field': 'quote', 'quote': emoji})
        self.assertEqual(repair['suggestion']['quote'], emoji)
        # The real slot is never larger than the reserved worst case.
        worst = len(canonical(COMPARE_REPAIR_WORST_CASE).encode('utf-8'))
        real_slot = len(canonical(repair).encode('utf-8'))
        self.assertLessEqual(real_slot, worst)
        page = {'cards': [card['id']], 'pairs': [[0, card['id']]]}
        payload = self.ev._compare_payload(base, page, by_id, [op], proposal_hash,
                                           0, 1, repair=repair)
        encoded, _refs = encode_references(payload)
        actual = len(canonical(encoded).encode('utf-8'))
        measure = self.ev._compare_page_measure(base, by_id, [op], proposal_hash)
        measured = measure([{'card_id': card['id'], 'version': card['version'], 'ops': {0}}])
        # The byte-plan measurement (worst case, suggestion reserved even when
        # absent) is a conservative bound for the real oversized-Unicode payload.
        self.assertGreaterEqual(measured, actual)


if __name__ == '__main__':
    unittest.main()
