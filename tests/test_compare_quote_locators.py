"""Bounded host-generated explicit quote locators for the frozen compare page.

These exercise the real ``Evolution._compare_stage`` / ``_verify_compare`` /
``_compare_payload`` / byte-plan paths and the pure ``comparison_review``
validators with synthetic fixtures only: no live vault, model, credentials or
network is touched. A model stub proves machine behaviour, not semantic
quality.

The production-evidenced failure class: the comparer returns multiple
non-exact quotes of length 14..37 (not overlong, so the existing
overlong-prefix suggestion never fires) and all three page-format attempts
fail; a normalized quote such as '权限检查必须先完成' (body:
'权限检查**必须先完成**；校验失败时不得发布。') is correctly rejected but the
generic feedback cannot ensure a correct recopy.

The fix: every full candidate card in a frozen compare payload carries a very
small host-generated set of {id, quote} locators (max 3, <= 80 chars each,
nonempty exact substrings, whitespace/Markdown preserved, identical spans
deduplicated, beginning/middle/end). Each id binds the frozen proposal hash,
the page's exact pair set and the card identity/version/body hash. The
comparer may return quote_locator_id instead of copying quote verbatim; the
host validates the selection against the exact current registry, translates
it to the canonical exact quote, and applies every unchanged strict check.
The strict exact-quote alternative, the overlong-prefix suggestion, every
bound, and the canonical disposition/cache structure are unchanged.
"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tongle.engine import Engine
from tongle.evolution import (Evolution, StageLimit, canonical, fingerprint)
from tongle.model_refs import encode_references
from tongle.comparison_review import (COMPARE_LOCATOR_NOTE,
                                      MAX_COMPARE_LOCATORS_PER_CARD,
                                      MAX_COMPARE_LOCATOR_CHARS,
                                      MAX_COMPARE_QUOTE_CHARS,
                                      _locator_quotes,
                                      build_quote_locators,
                                      validate_compare_response)


class _LocatorHarness(unittest.TestCase):
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
        # Shares '共同令牌' with the seeded cards so related pairs are planned.
        return {'op': 'create', 'key': '共同令牌', 'kind': 'memory', 'topic': '主题',
                'title': '共同令牌', 'body': '共同令牌的新说明。',
                'evidence': [{'source_id': 'f' * 32, 'version': 1, 'quote': 'x'}],
                'reason': '新增'}

    @staticmethod
    def _valid_reason():
        return 'scope and claim clearly differ from the proposed change here'


class LocatorHelperTests(unittest.TestCase):
    BODY = '权限检查**必须先完成**；校验失败时不得发布。' + '细节说明。' * 100

    def test_spans_are_bounded_exact_substrings_deduped(self):
        quotes = _locator_quotes(self.BODY)
        self.assertTrue(quotes)
        self.assertLessEqual(len(quotes), MAX_COMPARE_LOCATORS_PER_CARD)
        for quote in quotes:
            self.assertTrue(quote.strip())
            self.assertLessEqual(len(quote), MAX_COMPARE_LOCATOR_CHARS)
            self.assertIn(quote, self.BODY)
        self.assertEqual(len(quotes), len(set(quotes)))          # dedup
        # Short body: the whole body is the single locator, whitespace kept.
        short = '  含 **Markdown** 与空白  '
        self.assertEqual(_locator_quotes(short), [short])
        self.assertEqual(_locator_quotes('   '), [])
        self.assertEqual(_locator_quotes(''), [])
        self.assertEqual(_locator_quotes(None), [])

    def test_ids_bind_proposal_page_and_card_binding(self):
        cid = 'a' * 32
        body = self.BODY
        binding = {cid: {'version': 1, 'body_sha256': 'h1'}}
        bodies = {cid: body}
        page = {'pairs': [[0, cid]], 'cards': [cid]}
        reg1 = build_quote_locators(page, binding, 'ph1', bodies)
        self.assertEqual(reg1, build_quote_locators(page, binding, 'ph1', bodies))
        self.assertTrue(reg1[cid])
        for entry in reg1[cid]:
            self.assertIn(entry['quote'], body)
            self.assertTrue(entry['id'])
        # A different proposal hash, page pair set, version or body hash all
        # change every id: a stale/cross-context id can never resolve.
        self.assertNotEqual([e['id'] for e in reg1[cid]],
                            [e['id'] for e in
                             build_quote_locators(page, binding, 'ph2', bodies)[cid]])
        self.assertNotEqual([e['id'] for e in reg1[cid]],
                            [e['id'] for e in build_quote_locators(
                                {'pairs': [[1, cid]], 'cards': [cid]},
                                binding, 'ph1', bodies)[cid]])
        self.assertNotEqual([e['id'] for e in reg1[cid]],
                            [e['id'] for e in build_quote_locators(
                                page, {cid: {'version': 2, 'body_sha256': 'h1'}},
                                'ph1', bodies)[cid]])
        self.assertNotEqual([e['id'] for e in reg1[cid]],
                            [e['id'] for e in build_quote_locators(
                                page, {cid: {'version': 1, 'body_sha256': 'h2'}},
                                'ph1', bodies)[cid]])
        self.assertTrue(COMPARE_LOCATOR_NOTE)


class LocatorValidationTests(unittest.TestCase):
    BODY = '权限检查**必须先完成**；校验失败时不得发布。' + '细节说明。' * 100

    def setUp(self):
        self.cid = 'a' * 32
        self.other = 'b' * 32
        self.page = {'pairs': [[0, self.cid]], 'cards': [self.cid]}
        self.bodies = {self.cid: self.BODY, self.other: self.BODY + '异'}
        binding = {self.cid: {'version': 1, 'body_sha256': 'h1'}}
        self.registry = build_quote_locators(self.page, binding, 'ph', self.bodies)
        self.locator = self.registry[self.cid][0]

    def _pair(self, **kw):
        entry = {'op': 0, 'card_id': self.cid, 'relation': 'distinct',
                 'reason': 'scope and claim clearly differ from the proposed change here'}
        entry.update(kw)
        return entry

    def test_normalized_quote_still_fails_both_arities(self):
        normalized = '权限检查必须先完成'
        self.assertNotIn(normalized, self.BODY)
        old = validate_compare_response({'pairs': [self._pair(quote=normalized)]},
                                        self.page, self.bodies)
        self.assertFalse(old['ok'])
        self.assertNotIn('suggestion', old)      # not overlong: no suggestion
        new = validate_compare_response({'pairs': [self._pair(quote=normalized)]},
                                        self.page, self.bodies, self.registry)
        self.assertFalse(new['ok'])

    def test_explicit_locator_translates_to_canonical_exact_quote(self):
        result = validate_compare_response(
            {'pairs': [self._pair(quote_locator_id=self.locator['id'])]},
            self.page, self.bodies, self.registry)
        self.assertTrue(result['ok'])
        # Canonical structure: the resolved exact quote, never the id.
        self.assertEqual(result['dispositions'][0]['quote'], self.locator['quote'])
        self.assertNotIn('quote_locator_id', result['dispositions'][0])

    def test_exact_quote_path_still_passes_with_and_without_registry(self):
        quote = self.BODY[:40]
        for registry in (None, self.registry):
            result = validate_compare_response({'pairs': [self._pair(quote=quote)]},
                                               self.page, self.bodies, registry)
            self.assertTrue(result['ok'], registry)
            self.assertEqual(result['dispositions'][0]['quote'], quote)

    def test_both_paths_allowed_only_when_they_agree(self):
        agree = validate_compare_response(
            {'pairs': [self._pair(quote=self.locator['quote'],
                                  quote_locator_id=self.locator['id'])]},
            self.page, self.bodies, self.registry)
        self.assertTrue(agree['ok'])
        conflict = validate_compare_response(
            {'pairs': [self._pair(quote=self.locator['quote'] + 'x',
                                  quote_locator_id=self.locator['id'])]},
            self.page, self.bodies, self.registry)
        self.assertFalse(conflict['ok'])

    def test_unknown_cross_card_stale_and_mistyped_ids_fail_closed(self):
        # Unknown id (never in this registry).
        self.assertFalse(validate_compare_response(
            {'pairs': [self._pair(quote_locator_id='f' * 24)]},
            self.page, self.bodies, self.registry)['ok'])
        # Cross-card: a locator id belonging to a different card of the registry.
        two_page = {'pairs': [[0, self.cid], [1, self.other]],
                    'cards': [self.cid, self.other]}
        binding = {c: {'version': 1, 'body_sha256': 'h'} for c in two_page['cards']}
        two_registry = build_quote_locators(two_page, binding, 'ph', self.bodies)
        foreign = two_registry[self.other][0]['id']
        cross = validate_compare_response(
            {'pairs': [self._pair(quote_locator_id=foreign),
                       {'op': 1, 'card_id': self.other, 'relation': 'distinct',
                        'quote': (self.BODY + '异')[:40],
                        'reason': 'scope and claim clearly differ from the proposed change here'}]},
            two_page, self.bodies, two_registry)
        self.assertFalse(cross['ok'])
        # Stale: the id from an older proposal/page binding no longer resolves.
        stale = build_quote_locators(self.page, {self.cid: {'version': 9,
                                                            'body_sha256': 'old'}},
                                     'ph', self.bodies)[self.cid][0]['id']
        self.assertFalse(validate_compare_response(
            {'pairs': [self._pair(quote_locator_id=stale)]},
            self.page, self.bodies, self.registry)['ok'])
        # Wrong field types (bool included) fail closed.
        for bad in (True, 7, 3.5, ['x'], {'id': 'x'}):
            self.assertFalse(validate_compare_response(
                {'pairs': [self._pair(quote_locator_id=bad)]},
                self.page, self.bodies, self.registry)['ok'], bad)
        # A model-supplied registry is never trusted: old 3-arg callers fail
        # closed on any locator id.
        self.assertFalse(validate_compare_response(
            {'pairs': [self._pair(quote_locator_id=self.locator['id'])]},
            self.page, self.bodies)['ok'])
        # Neither path: still the unchanged hard rejection.
        missing = validate_compare_response({'pairs': [self._pair()]},
                                            self.page, self.bodies, self.registry)
        self.assertFalse(missing['ok'])

    def test_blocking_relation_via_locator_stays_blocking(self):
        result = validate_compare_response(
            {'pairs': [self._pair(relation='duplicate',
                                  quote_locator_id=self.locator['id'],
                                  reason='the proposal recreates this exact claim and scope')]},
            self.page, self.bodies, self.registry)
        self.assertTrue(result['ok'])
        self.assertEqual(result['findings'][0]['relation'], 'duplicate')
        self.assertEqual(result['findings'][0]['quote'], self.locator['quote'])


class LocatorEngineFlowTests(_LocatorHarness):
    def _seed_two(self):
        first = self.seed('共同令牌卡一', '共同令牌' + '甲' * 300)
        second = self.seed('共同令牌卡二', '共同令牌' + '乙' * 300)
        return first, second

    def _locator_model(self, payloads, relation='distinct', reason=None, bad_id=None):
        def side_effect(_job, stage, payload, _cfg):
            self._record_call(_job, stage)
            payloads.append(payload)
            entries = []
            locators = {c['card_id']: c.get('locators') or []
                        for c in payload['page']['candidates']}
            for pair in payload['page']['pairs']:
                locator = locators[pair['card_id']][0]
                entries.append({'op': pair['op'], 'card_id': pair['card_id'],
                                'relation': relation,
                                'reason': reason or self._valid_reason(),
                                'quote_locator_id': bad_id or locator['id']})
            return {'pairs': entries}
        return side_effect

    def test_multi_card_multi_pair_explicit_ids_complete_within_limits(self):
        self._seed_two()
        base, job, _part = self._unit_context()
        op = self._unit_op()
        payloads = []
        with patch.object(self.ev, '_call',
                          side_effect=self._locator_model(payloads)):
            reports, findings = self.ev._compare_stage(job, base, [op], 'tok', {})
        self.assertEqual(findings, [])
        # One page, ONE dispatch: no repair attempts were needed.
        self.assertEqual(len(payloads), 1)
        pairs = payloads[0]['page']['pairs']
        self.assertEqual(len(pairs), 2)
        self.assertEqual(len({p['card_id'] for p in pairs}), 2)   # multi-card
        for candidate in payloads[0]['page']['candidates']:
            locators = candidate['locators']
            self.assertTrue(locators)
            self.assertLessEqual(len(locators), MAX_COMPARE_LOCATORS_PER_CARD)
            for locator in locators:
                self.assertLessEqual(len(locator['quote']), MAX_COMPARE_LOCATOR_CHARS)
                self.assertIn(locator['quote'], candidate['body'])
        # Canonical receipts/cache carry the exact resolved quote only.
        pages = base['compare']['pages']
        self.assertEqual(len(pages), 1)
        receipt = next(iter(pages.values()))
        self.assertEqual(len(receipt['dispositions']), 2)
        for disposition in receipt['dispositions']:
            self.assertTrue(disposition['quote'])
            self.assertNotIn('quote_locator_id', disposition)
        pair_total = sum(r['pair_count'] for r in reports)
        self.assertEqual(pair_total, 2)
        # Publication replay passes on the canonical exact-quote receipt.
        self.ev._verify_compare(base, [op])

    def test_old_exact_quote_path_passes_unchanged(self):
        self._seed_two()
        base, job, _part = self._unit_context()
        op = self._unit_op()
        payloads = []

        def side_effect(_job, stage, payload, _cfg):
            self._record_call(_job, stage)
            payloads.append(payload)
            bodies = {c['card_id']: c['body'] for c in payload['page']['candidates']}
            return {'pairs': [{'op': p['op'], 'card_id': p['card_id'],
                               'relation': 'distinct',
                               'quote': bodies[p['card_id']][:40],
                               'reason': self._valid_reason()}
                              for p in payload['page']['pairs']]}

        with patch.object(self.ev, '_call', side_effect=side_effect):
            reports, findings = self.ev._compare_stage(job, base, [op], 'tok', {})
        self.assertEqual(findings, [])
        self.assertEqual(len(payloads), 1)
        self.ev._verify_compare(base, [op])

    def test_forged_id_fails_closed_and_never_auto_approves(self):
        self._seed_two()
        base, job, _part = self._unit_context()
        op = self._unit_op()
        payloads = []
        with patch.object(self.ev, '_call',
                          side_effect=self._locator_model(payloads, bad_id='0' * 24)):
            with self.assertRaises(StageLimit):
                self.ev._compare_stage(job, base, [op], 'tok', {})
        # Exactly MAX_COMPARE_PAGE_ATTEMPTS dispatches, all rejected; no receipt
        # was settled and no verdict was auto-approved from the invalid id.
        self.assertEqual(len(payloads), 3)
        self.assertEqual(base['compare']['pages'], {})

    def test_blocking_relation_via_locator_blocks_publish(self):
        self._seed_two()
        base, job, _part = self._unit_context()
        op = self._unit_op()
        payloads = []
        with patch.object(self.ev, '_call',
                          side_effect=self._locator_model(
                              payloads, relation='duplicate',
                              reason='the proposal recreates this exact claim and scope')):
            reports, findings = self.ev._compare_stage(job, base, [op], 'tok', {})
        self.assertEqual(len(findings), 2)
        for finding in findings:
            self.assertEqual(finding['relation'], 'duplicate')
            self.assertTrue(finding['quote'])
            self.assertNotIn('quote_locator_id', finding)
        # The blocking receipt can never be turned distinct at publish replay.
        with self.assertRaises(StageLimit):
            self.ev._verify_compare(base, [op])


class LocatorBytePlanTests(_LocatorHarness):
    def test_byte_planner_includes_locators_at_unicode_boundary(self):
        # 4-byte (non-BMP) Unicode at the locator span boundary.
        card = self.seed('共同令牌卡', '共同令牌' + '\U0001F600密' * 200)
        base, _job, _part = self._unit_context()
        op = self._unit_op()
        cards = self.ev._authorized_catalog('project')
        by_id = {c['id']: c for c in cards}
        proposal_hash = fingerprint([op])
        page = {'cards': [card['id']], 'pairs': [[0, card['id']]]}
        payload = self.ev._compare_payload(base, page, by_id, [op], proposal_hash, 0, 1)
        locators = payload['page']['candidates'][0]['locators']
        self.assertTrue(locators)
        self.assertLessEqual(len(locators), MAX_COMPARE_LOCATORS_PER_CARD)
        for locator in locators:
            self.assertLessEqual(len(locator['quote']), MAX_COMPARE_LOCATOR_CHARS)
            self.assertIn(locator['quote'], card['body'])
        encoded, _refs = encode_references(payload)
        actual = len(canonical(encoded).encode('utf-8'))
        measure = self.ev._compare_page_measure(base, by_id, [op], proposal_hash)
        measured = measure([{'card_id': card['id'], 'version': card['version'],
                             'ops': {0}}])
        # The shared helper feeds both the real payload and the byte plan: the
        # measured worst case covers the real locator-carrying 4-byte payload.
        self.assertGreaterEqual(measured, actual)


if __name__ == '__main__':
    unittest.main()
