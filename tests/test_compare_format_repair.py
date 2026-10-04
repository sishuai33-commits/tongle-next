"""Bounded stage-owned FORMAT repair of one frozen compare page.

These exercise the real ``Evolution._compare_stage`` / ``_verify_compare`` /
``process`` paths with synthetic fixtures only: no live vault, model,
credentials or network is touched. The process-level cases drive the real core
through a worker transport stub; the unit-level cases patch only ``_call``.
A model stub proves machine behaviour, not semantic quality.

The core failure: a comparer that correctly recognises a duplicate but quotes
one overlong (170-char) English sentence. Strict validation (the existing
120-char quote bound) rejects it, and a whole-proposal rewrite wasted every
earlier accepted page receipt. The fix retries the SAME frozen page in place,
with bounded host-generated field feedback, at most
``MAX_COMPARE_PAGE_ATTEMPTS`` dispatched attempts, preserving valid earlier
page receipts, without relaxing the quote bound, silently truncating, rewriting
the proposal, or resetting the global dispatch counter.
"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tongle.engine import Engine
from tongle.evolution import (BudgetWait, Evolution, SelectionChanged, StageLimit,
                              canonical, fingerprint)
from tongle.comparison_review import (COMPARE_REPAIR_NOTE,
                                      COMPARE_REPAIR_WORST_CASE,
                                      MAX_COMPARE_FEEDBACK_CHARS,
                                      MAX_COMPARE_FEEDBACK_ITEMS,
                                      MAX_COMPARE_PAGE_ATTEMPTS,
                                      MAX_COMPARE_QUOTE_CHARS,
                                      compare_repair_feedback,
                                      compare_repair_field,
                                      page_identity,
                                      validate_compare_response)
from tongle.model_refs import encode_references

try:  # sibling import under ``unittest discover -s tests``
    from fact_review_stub import with_fact_audit
except ImportError:  # pragma: no cover - package-import invocation
    from tests.fact_review_stub import with_fact_audit


class _FormatHarness(unittest.TestCase):
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
        rec = self.e.store.capture('project', session, role, event,
                                   text or self.MAIN_TEXT)
        self.ev.captured(rec['source_id'], role, session)
        return rec

    def _source(self, rec):
        return self.e.store.source(rec['source_id'])

    def run_model(self, model, force=True):
        # Answer only the focused fact-audit dispatch structurally; every other
        # payload still reaches ``model`` unchanged.
        with patch('tongle.worker.invoke_json', side_effect=with_fact_audit(model)):
            return self.ev.process(force=force)

    def run_attempts(self, model, times=2):
        result = None
        for _ in range(times):
            result = self.run_model(model)
            with self.e.transaction():
                self.e.db.execute('UPDATE knowledge_work SET next_at=0')
        return result

    def work(self, state=None):
        sql = 'SELECT * FROM knowledge_work'; args = ()
        if state:
            sql += ' WHERE state=?'; args = (state,)
        return [dict(r) for r in self.e.db.execute(sql, args).fetchall()]

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

    def _create_op(self, key, title, body, source):
        return {'op': 'create', 'key': key, 'kind': 'memory', 'topic': '主题',
                'title': title, 'body': body,
                'evidence': [{'source_id': source['source_id'],
                              'version': source['version'], 'quote': source['text']}],
                'reason': '新增材料'}

    def _pair_response(self, payload, relation, quote_len):
        cand = {c['card_id']: c for c in payload['page']['candidates']}
        out = []
        for p in payload['page']['pairs']:
            body = cand[p['card_id']]['body']
            out.append({'op': p['op'], 'card_id': p['card_id'], 'relation': relation,
                        'quote': body[:quote_len],
                        'reason': 'the proposed change recreates this candidate claim'
                        if relation != 'distinct'
                        else 'scope and claim differ from the proposed change here'})
        return {'pairs': out}

    def _invalid_long_quote(self, _job, _stage, payload, _cfg):
        return self._pair_response(payload, 'distinct', 170)

    def _distinct(self, payload):
        return self._pair_response(payload, 'distinct', 40)

    def _compare_format_rows(self, work_id=None):
        sql = "SELECT error FROM knowledge_failures WHERE state='compare_format'"
        args = ()
        if work_id:
            sql += ' AND work_id=?'; args = (work_id,)
        return [r['error'] for r in self.e.db.execute(sql, args).fetchall()]


class CachedFirstPageRepairTests(_FormatHarness):
    def test_later_page_format_repair_succeeds_without_recompiling(self):
        for i in range(8):
            self.seed('卡%d' % i, '共同令牌%d' % i + '据' * 7995)
        rec = self.capture()
        source = self._source(rec)
        state = {'compile': 0, 'page_calls': {}, 'bodies': set(),
                 'phase': 'crash', 'resume_page2': 0}

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'frozen_proposal' in data:
                page = data['stage_progress']['page']
                state['page_calls'][page] = state['page_calls'].get(page, 0) + 1
                state['bodies'].update(c['body'] for c in data['page']['candidates'])
                long_invalid = page == 2
                if page == 2 and state['phase'] == 'resume':
                    long_invalid = state['resume_page2'] == 0
                    state['resume_page2'] += 1
                if long_invalid:
                    # Recognises the duplicate but quotes a 170-char sentence,
                    # violating the strict 120-char quote bound. The host must
                    # reject the field without accepting the (invalid) verdict.
                    out = self._pair_response(data, 'duplicate', 170)
                else:
                    out = self._pair_response(data, 'distinct', 80)
                if page == 2 and state['phase'] == 'crash':
                    raise OSError('transient upstream')
                return out, {}
            if 'proposal' in data:
                return {'approved': True, 'findings': []}, {}
            state['compile'] += 1
            return {'operations': [self._create_op(
                '共同令牌', '共同令牌', '共同令牌的新说明。', source)]}, {}

        first = self.run_model(model)
        self.assertEqual(first['processed'], 0)
        row = self.work('retry')[0]
        self.assertIn('OSError', row['error'])
        stored = json.loads(row['bundle'])
        self.assertEqual(len(stored['compare']['pages']), 1)   # first page cached
        self.assertEqual(state['page_calls'][1], 1)

        # Resume: the cached first page must NOT be re-dispatched; the later
        # page is repaired in place (one invalid field, then a valid one).
        state['phase'] = 'resume'
        with self.e.transaction():
            self.e.db.execute('UPDATE knowledge_work SET next_at=0')
        second = self.run_model(model)
        self.assertEqual(second['processed'], 1)
        self.assertEqual(second['failed'], 0)
        self.assertEqual(state['compile'], 1)                  # no recompile
        self.assertEqual(state['page_calls'][1], 1)            # cache reused
        self.assertEqual(state['page_calls'][2], 3)            # crash + invalid + valid
        self.assertEqual(len(state['bodies']), 8)              # full body coverage
        # The repair never accepted the invalid quote: the accepted coverage is
        # all-distinct with the correct pair count.
        rows = self.e.db.execute(
            "SELECT error FROM knowledge_failures WHERE state='completion'").fetchall()
        record = json.loads(rows[0]['error'])
        self.assertEqual(record['comparison']['pair_count'], 8)
        self.assertEqual(record['comparison']['page_count'], 2)
        self.assertEqual(record['comparison']['format_repairs'], 1)
        self.assertTrue(record['comparison']['all_distinct'])
        # The consumed format failure stays in the durable history even though
        # the repair succeeded -- and no body text leaks into that history.
        formats = self._compare_format_rows(row['id'])
        self.assertEqual(len(formats), 1)
        self.assertIn('quote', formats[0])
        self.assertNotIn('据', formats[0])
        self.assertNotIn('据', rows[0]['error'])


class AttemptCeilingTests(_FormatHarness):
    def test_three_repeated_invalid_quotes_stop_truthfully(self):
        self._seed_single()
        rec = self.capture()
        source = self._source(rec)
        calls = {'compile': 0, 'compare': 0}

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'frozen_proposal' in data:
                calls['compare'] += 1
                return self._pair_response(data, 'distinct', 170), {}
            if 'proposal' in data:
                return {'approved': True, 'findings': []}, {}
            calls['compile'] += 1
            return {'operations': [self._create_op(
                '共同令牌', '共同令牌', '共同令牌的新说明。', source)]}, {}

        result = self.run_model(model)
        self.assertEqual(result['failed'], 1)
        row = self.work('failed')[0]
        self.assertIn('comparison page format repair exhausted', row['error'])
        self.assertEqual(row['compare_dispatches'], MAX_COMPARE_PAGE_ATTEMPTS)
        self.assertEqual(calls['compare'], MAX_COMPARE_PAGE_ATTEMPTS)
        self.assertEqual(calls['compile'], 1)
        self.assertEqual(len(self._compare_format_rows(row['id'])), 3)
        self.assertIsNone(self.e.db.execute('SELECT 1 FROM knowledge_batches').fetchone())

    def test_predispatch_budgetwait_refunds_only_undispatched(self):
        self._seed_single()
        base, job, _part = self._unit_context()
        op = self._unit_op()
        with patch.object(self.ev, '_call', side_effect=BudgetWait('budget exhausted')):
            with self.assertRaises(BudgetWait):
                self.ev._compare_stage(job, base, [op], 'tok', {})
        row = self.e.db.execute('SELECT compare_dispatches FROM knowledge_work WHERE id=?',
                                (job['id'],)).fetchone()
        self.assertEqual(row['compare_dispatches'], 0)
        stored = json.loads(self.e.db.execute(
            'SELECT bundle FROM knowledge_work WHERE id=?', (job['id'],)).fetchone()['bundle'])
        attempts = stored['compare']['page_attempts']
        self.assertEqual(sum(int(p['dispatched']) for p in attempts.values()), 0)
        # No provider call was ever recorded (nothing was dispatched).
        self.assertEqual(self.e.db.execute(
            'SELECT COUNT(*) FROM knowledge_calls').fetchone()[0], 0)

    def test_dispatched_format_failures_are_counted_not_refunded(self):
        self._seed_single()
        base, job, _part = self._unit_context()
        op = self._unit_op()
        calls = {'n': 0}

        def side_effect(_job, stage, payload, _cfg):
            calls['n'] += 1
            return self._invalid_long_quote(_job, stage, payload, _cfg)

        with patch.object(self.ev, '_call', side_effect=side_effect):
            with self.assertRaises(StageLimit):
                self.ev._compare_stage(job, base, [op], 'tok', {})
        self.assertEqual(calls['n'], 3)
        row = self.e.db.execute('SELECT compare_dispatches FROM knowledge_work WHERE id=?',
                                (job['id'],)).fetchone()
        self.assertEqual(row['compare_dispatches'], 3)
        self.assertEqual(len(self._compare_format_rows(job['id'])), 3)

    def test_cache_resume_does_not_reset_page_or_global_attempts(self):
        self._seed_single()
        base, job, _part = self._unit_context()
        op = self._unit_op()
        calls = {'n': 0}

        def side_effect(_job, stage, payload, _cfg):
            calls['n'] += 1
            if calls['n'] == 2:
                raise OSError('crash mid-repair')
            return self._invalid_long_quote(_job, stage, payload, _cfg)

        with patch.object(self.ev, '_call', side_effect=side_effect):
            with self.assertRaises(OSError):
                self.ev._compare_stage(job, base, [op], 'tok', {})
        row = self.e.db.execute('SELECT compare_dispatches FROM knowledge_work WHERE id=?',
                                (job['id'],)).fetchone()
        self.assertEqual(row['compare_dispatches'], 2)
        stored = json.loads(self.e.db.execute(
            'SELECT bundle FROM knowledge_work WHERE id=?', (job['id'],)).fetchone()['bundle'])
        pid = next(iter(stored['compare']['page_attempts']))
        self.assertEqual(stored['compare']['page_attempts'][pid]['dispatched'], 2)

        # Resume from the persisted bundle grants only the one remaining page
        # attempt -- neither the page nor the global counter is reset.
        resume = {'n': 0}

        def resume_effect(_job, stage, payload, _cfg):
            resume['n'] += 1
            return self._invalid_long_quote(_job, stage, payload, _cfg)

        with patch.object(self.ev, '_call', side_effect=resume_effect):
            with self.assertRaises(StageLimit):
                self.ev._compare_stage(job, stored, [op], 'tok', {})
        self.assertEqual(resume['n'], 1)
        row2 = self.e.db.execute('SELECT compare_dispatches FROM knowledge_work WHERE id=?',
                                 (job['id'],)).fetchone()
        self.assertEqual(row2['compare_dispatches'], 3)


class CurrencyDuringRepairTests(_FormatHarness):
    def _run_first_invalid_then(self, mutate):
        self._seed_single()
        base, job, _part = self._unit_context()
        op = self._unit_op()
        state = {'n': 0}

        def side_effect(_job, stage, payload, _cfg):
            state['n'] += 1
            if state['n'] == 1:
                mutate()
                return self._invalid_long_quote(_job, stage, payload, _cfg)
            raise AssertionError('a repair dispatch happened after a currency change')
        return base, job, op, state, side_effect

    def test_card_change_during_repair_prevents_next_dispatch(self):
        base, job, op, state, side_effect = self._run_first_invalid_then(
            lambda: self.ev.cards.manual_edit(
                self.ev.cards.list('project')[0]['id'], 1, '人工修改的正文。'))
        with patch.object(self.ev, '_call', side_effect=side_effect):
            with self.assertRaises(SelectionChanged):
                self.ev._compare_stage(job, base, [op], 'tok', {})
        self.assertEqual(state['n'], 1)

    def test_source_change_during_repair_prevents_next_dispatch(self):
        base, job, op, state, side_effect = self._run_first_invalid_then(
            lambda: (self._unit_path.write_text('来源的新版本内容。'),
                     self.e.store.ingest('project', str(self._unit_path))))
        with patch.object(self.ev, '_call', side_effect=side_effect):
            with self.assertRaises(ValueError):
                self.ev._compare_stage(job, base, [op], 'tok', {})
        self.assertEqual(state['n'], 1)

    def test_lease_loss_during_repair_prevents_next_dispatch(self):
        def mutate():
            with self.e.transaction():
                self.e.db.execute(
                    "UPDATE knowledge_work SET lease_token='other-owner' WHERE id='w'")
        base, job, op, state, side_effect = self._run_first_invalid_then(mutate)
        with patch.object(self.ev, '_call', side_effect=side_effect):
            with self.assertRaises(ValueError):
                self.ev._compare_stage(job, base, [op], 'tok', {})
        self.assertEqual(state['n'], 1)


class SemanticFindingTests(_FormatHarness):
    def test_valid_duplicate_still_forces_compiler_revision(self):
        self._seed_single()
        rec = self.capture()
        source = self._source(rec)
        calls = {'compile': 0}

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'frozen_proposal' in data:
                # A VALID duplicate verdict (bounded quote and reason). This is
                # a semantic finding, never a format fault.
                return self._pair_response(data, 'duplicate', 60), {}
            if 'proposal' in data:
                return {'approved': True, 'findings': []}, {}
            calls['compile'] += 1
            operations = [self._create_op(
                '共同令牌', '共同令牌', '共同令牌的新说明。', source)]
            # On a retry the host supplies the settled blocking obligation, so
            # the compiler must return an explicit per-target resolution. The
            # SAME deliberately duplicate create is kept, so the independent
            # comparison still rejects it semantically (compile == 2).
            recon = data.get('reconciliation') or []
            resolutions = [{'card_id': r['card_id'], 'decision': 'noop',
                            'reason': '同一既有卡已承载该主张，本轮不新建重复身份待复核'}
                           for r in recon]
            return {'operations': operations,
                    'reconciliation_resolutions': resolutions}, {}

        first = self.run_model(model)
        self.assertEqual(first['processed'], 0)
        row = self.work('retry')[0]
        self.assertIn('comparison', row['error'])
        self.assertNotIn('format repair', row['error'])
        self.assertEqual(self._compare_format_rows(row['id']), [])
        with self.e.transaction():
            self.e.db.execute('UPDATE knowledge_work SET next_at=0')
        self.run_model(model)
        # The rejection forced the compiler to be asked again (a revision), not
        # an in-place format repair of the same page.
        self.assertEqual(calls['compile'], 2)
        self.assertIsNone(self.e.db.execute('SELECT 1 FROM knowledge_batches').fetchone())


class FeedbackBoundTests(_FormatHarness):
    def test_validation_feedback_names_field_and_lengths(self):
        page = {'pairs': [[0, 'a' * 32]], 'cards': ['a' * 32]}
        bodies = {'a' * 32: '旧' * 300}
        result = validate_compare_response(
            {'pairs': [{'op': 0, 'card_id': 'a' * 32, 'relation': 'distinct',
                        'quote': '旧' * 170,
                        'reason': 'scope and claim clearly differ here'}]}, page, bodies)
        self.assertFalse(result['ok'])
        problem = result['feedback'][0]
        self.assertEqual(problem['field'], 'quote')
        self.assertEqual(problem['actual'], 170)
        self.assertEqual(problem['max'], MAX_COMPARE_QUOTE_CHARS)
        strings = compare_repair_feedback(result)
        joined = ' '.join(strings)
        self.assertIn('quote', joined)
        self.assertIn('170', joined)
        self.assertIn(str(MAX_COMPARE_QUOTE_CHARS), joined)

    def test_repair_feedback_stays_within_reserved_utf8_byte_cap(self):
        # A pathological multibyte feedback set is truncated to the fixed
        # item/character bound, and the serialized slot never exceeds the
        # reserved worst case that both the byte plan and the preflight count.
        field = compare_repair_field(2, ['引' * 5000 for _ in range(50)])
        self.assertLessEqual(len(field['feedback']), MAX_COMPARE_FEEDBACK_ITEMS)
        self.assertTrue(all(len(x) <= MAX_COMPARE_FEEDBACK_CHARS
                            for x in field['feedback']))
        self.assertIn(COMPARE_REPAIR_NOTE, field['feedback'])
        worst = len(canonical(COMPARE_REPAIR_WORST_CASE).encode('utf-8'))
        real = len(canonical(field).encode('utf-8'))
        self.assertLessEqual(real, worst)

    def test_measure_reserves_bounded_repair_overhead(self):
        # The encoded-byte measure used to pack pages already includes the
        # bounded repair slot, so a repair dispatch cannot exceed the fixed
        # input-byte ceiling once real feedback is attached.
        card = self._seed_single()
        base, _job, _part = self._unit_context()
        op = self._unit_op()
        cards = self.ev._authorized_catalog('project')
        by_id = {c['id']: c for c in cards}
        proposal_hash = fingerprint([op])
        measure = self.ev._compare_page_measure(base, by_id, [op], proposal_hash)
        entries = [{'card_id': card['id'], 'version': card['version'], 'ops': {0}}]
        measured = measure(entries)
        page = {'cards': [card['id']], 'pairs': [[0, card['id']]]}
        repair = compare_repair_field(MAX_COMPARE_PAGE_ATTEMPTS,
                                      ['op=0 card=card001 field=quote: too long '
                                       '(actual=170 max=120)'])
        payload = self.ev._compare_payload(base, page, by_id, [op], proposal_hash,
                                           0, 2, repair=repair)
        encoded, _refs = encode_references(payload)
        actual = len(canonical(encoded).encode('utf-8'))
        self.assertGreaterEqual(measured, actual)

    def test_repair_does_not_change_page_partition_or_ids(self):
        self._seed_single()
        base, job, _part = self._unit_context()
        op = self._unit_op()
        cards = self.ev._authorized_catalog('project')
        _pairs, by_id, pages = self.ev._compare_plan(base, cards, [op])
        before = [page_identity(p, self.ev._compare_page_binding(p, by_id),
                                fingerprint([op])) for p in pages]
        calls = {'n': 0}

        def side_effect(_job, stage, payload, _cfg):
            self._record_call(_job, stage)
            calls['n'] += 1
            if calls['n'] == 1:
                return self._invalid_long_quote(_job, stage, payload, _cfg)
            return self._distinct(payload)

        with patch.object(self.ev, '_call', side_effect=side_effect):
            reports, findings = self.ev._compare_stage(job, base, [op], 'tok', {})
        self.assertEqual(findings, [])
        self.assertTrue(reports)
        _pairs2, by_id2, pages2 = self.ev._compare_plan(base, cards, [op])
        after = [page_identity(p, self.ev._compare_page_binding(p, by_id2),
                               fingerprint([op])) for p in pages2]
        self.assertEqual(before, after)                        # same page ids
        self.ev._verify_compare(base, [op])                    # deterministic replay


if __name__ == '__main__':
    unittest.main()
