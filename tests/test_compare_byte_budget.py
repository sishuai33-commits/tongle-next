"""Byte-aware whole-card packing regressions for the frozen comparison.

These exercise the real ``Evolution._compare_stage`` / ``_verify_compare`` and
the deterministic ``plan_compare_pages`` packing path with synthetic fixtures
only: no live vault, model, credentials or network is touched. A model stub
proves machine behaviour, not semantic quality.

The core scenario is a batch whose candidate bodies fit the fixed 52k-character
body budget on ONE page but whose real *encoded* payload (task + frozen
proposal + sources + candidate bodies + page metadata, after identity-alias
transport) exceeds a configured 100000-byte input ceiling. Character-only
packing would dispatch that over-budget page and the host would permanently
StageLimit a feasible batch; byte-aware whole-card packing must split it into
smaller pages that each fit, with complete coverage and identical stage/final
page identities.
"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tongle.engine import Engine
from tongle.evolution import Evolution, SelectionChanged, StageLimit, canonical, fingerprint
from tongle.model_refs import encode_references
from tongle.comparison_review import (MAX_COMPARE_CANDIDATES_PER_PAGE,
                                      MAX_COMPARE_DISPATCHES, MAX_COMPARE_PAGES,
                                      ComparePlanError, plan_compare_pages)


class _ByteHarness(unittest.TestCase):
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
               # Synthetic-only, generous finite daily allowance so the bounded
               # comparison is limited by the attempt/dispatch ceilings, never
               # by an accidental token wall. Production caps are untouched.
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

    def _record_call(self, job, stage='compare'):
        """Synthetic stand-in for the real ``_call`` dispatch-identity row.

        Settled with an explicit ``elapsed=0`` so it matches production, where
        ``elapsed IS NOT NULL`` marks a finished call whose receipt is reusable.
        """
        self._call_seq[0] += 1
        call_id = 'call%04d' % self._call_seq[0]
        with self.e.transaction():
            self.e.db.execute(
                'INSERT INTO knowledge_calls(id,work_id,stage,day,reserved,elapsed,created_at,error)'
                ' VALUES(?,?,?,?,?,0,?,NULL)',
                (call_id, job['id'], stage, '2026-01-01', 1, time.time()))
        self.ev._last_call_id = call_id
        return call_id

    def _distinct(self, data):
        cand = {c['card_id']: c for c in data['page']['candidates']}
        out = []
        for p in data['page']['pairs']:
            body = (cand.get(p['card_id'], {}).get('body') or '') or '正文'
            out.append({'op': p['op'], 'card_id': p['card_id'], 'relation': 'distinct',
                        'quote': body[:20],
                        'reason': 'scope and claim differ from the proposed change here'})
        return {'pairs': out}

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

    def _unit_op(self, key='共同令牌', body='共同令牌的新说明。'):
        # The key/title/body share the cards' '共同令牌' bigrams, so every seeded
        # card is a related candidate for this single create operation.
        return {'op': 'create', 'key': key, 'kind': 'memory', 'topic': '主题',
                'title': key, 'body': body,
                'evidence': [{'source_id': 'f' * 32, 'version': 1, 'quote': 'x'}],
                'reason': '新增'}

    def _four_big_cards(self, chars=13000):
        # 13000 CJK characters: 4 * 13000 == 52000 == the character body budget,
        # so a character-only planner packs all four on ONE page -- but
        # 4 * 13000 * 3 == 156000 encoded bytes far exceeds a 100000-byte cap.
        body = '共同令牌' + '密' * (chars - 4)
        return [self.seed('大卡%d' % i, body) for i in range(4)]


class ByteAwarePackingTests(_ByteHarness):
    def test_char_fitting_multibyte_page_is_split_by_real_input_bytes(self):
        self.write_config(knowledge_max_input_bytes=100000)
        cards = self._four_big_cards()
        bodies = [self.ev.cards.get(c['id'])['body'] for c in cards]
        # The old character-only plan really would have packed one page.
        self.assertLessEqual(sum(len(b) for b in bodies), 52000)
        base, job, _part = self._unit_context()
        op = self._unit_op()
        max_bytes = self.ev._max_input_bytes({'knowledge_max_input_bytes': 100000})
        self.assertEqual(max_bytes, 100000)
        payload_bytes = []
        page_cards = []

        def call(_job, stage, payload, _cfg):
            self._record_call(_job, stage)
            encoded, _refs = encode_references(payload)
            payload_bytes.append(len(canonical(encoded).encode('utf-8')))
            page_cards.append(sorted(c['card_id'] for c in payload['page']['candidates']))
            return self._distinct(payload)

        with patch.object(self.ev, '_call', side_effect=call):
            reports, findings = self.ev._compare_stage(job, base, [op], 'tok', {})
        self.assertEqual(findings, [])
        self.assertTrue(reports)
        # Split into smaller whole-card pages, each within the input-byte
        # ceiling -- never the single 156000-byte page.
        self.assertGreaterEqual(len(page_cards), 2)
        self.assertTrue(all(b <= max_bytes for b in payload_bytes), payload_bytes)
        self.assertTrue(all(len(p) <= MAX_COMPARE_CANDIDATES_PER_PAGE for p in page_cards))
        # Complete coverage: every related candidate exactly once.
        flat = [cid for page in page_cards for cid in page]
        self.assertEqual(sorted(flat), sorted(c['id'] for c in cards))
        self.assertEqual(len(flat), len(set(flat)))
        # Stable page identities: a resume reuses the receipts without any new
        # dispatch, and the final publication-lock replay reconstructs the same
        # pages and accepts them.
        self.assertEqual(len(base['compare']['pages']), len(page_cards))
        before = self._call_seq[0]
        with patch.object(self.ev, '_call',
                          side_effect=lambda *a, **k: (_ for _ in ()).throw(
                              AssertionError('re-dispatched a reusable page'))):
            reports2, findings2 = self.ev._compare_stage(job, base, [op], 'tok', {})
        self.assertEqual(self._call_seq[0], before)
        self.assertEqual(findings2, [])
        self.ev._verify_compare(base, [op])

    def test_verify_compare_requires_complete_coverage(self):
        self.write_config(knowledge_max_input_bytes=100000)
        self._four_big_cards()
        base, job, _part = self._unit_context()
        op = self._unit_op()

        def call(_job, stage, payload, _cfg):
            self._record_call(_job, stage)
            return self._distinct(payload)

        with patch.object(self.ev, '_call', side_effect=call):
            self.ev._compare_stage(job, base, [op], 'tok', {})
        # Dropping one page's receipt must block publication rather than
        # silently approving uncovered candidates.
        state = base['compare']
        state['pages'].pop(sorted(state['pages'])[0])
        with self.assertRaises(SelectionChanged):
            self.ev._verify_compare(base, [op])

    def test_preflight_blocks_over_budget_page_without_dispatch(self):
        self.write_config(knowledge_max_input_bytes=100000)
        self._four_big_cards()
        base, job, _part = self._unit_context()
        op = self._unit_op()

        def call(*a, **k):
            raise AssertionError('preflight let an over-budget page dispatch')

        # Sabotage the packer's own measurement so it would pack the single
        # over-budget page the byte-aware plan must never produce: the retained
        # actual-byte preflight must still refuse it BEFORE reserving a slot.
        with patch.object(self.ev, '_compare_page_measure',
                          return_value=lambda entries: 1), \
                patch.object(self.ev, '_call', side_effect=call):
            with self.assertRaises(StageLimit) as ctx:
                self.ev._compare_stage(job, base, [op], 'tok', {})
        self.assertIn('input byte budget', str(ctx.exception))
        row = self.e.db.execute('SELECT compare_dispatches FROM knowledge_work WHERE id=?',
                                (job['id'],)).fetchone()
        self.assertEqual(row['compare_dispatches'], 0)
        self.assertEqual(self.e.db.execute(
            'SELECT COUNT(*) FROM knowledge_calls').fetchone()[0], 0)

    def test_unrepresentable_singleton_fails_before_dispatch(self):
        self.write_config(knowledge_max_input_bytes=100000)
        # 40000 CJK characters: under the 52000-character body budget, but
        # ~120000 encoded bytes exceed the 100000-byte ceiling even alone.
        self.seed('共同令牌大卡', '共同令牌' + '密' * 39996)
        base, job, _part = self._unit_context()
        op = self._unit_op()
        dispatched = []

        def call(_job, stage, payload, _cfg):
            dispatched.append(stage)
            self._record_call(_job, stage)
            return self._distinct(payload)

        with patch.object(self.ev, '_call', side_effect=call):
            with self.assertRaises(StageLimit) as ctx:
                self.ev._compare_stage(job, base, [op], 'tok', {})
        self.assertIn('unrepresentable_required', str(ctx.exception))
        # Fails clearly BEFORE any dispatch or counter reservation.
        self.assertEqual(dispatched, [])
        row = self.e.db.execute('SELECT compare_dispatches FROM knowledge_work WHERE id=?',
                                (job['id'],)).fetchone()
        self.assertEqual(row['compare_dispatches'], 0)
        self.assertEqual(self.e.db.execute(
            'SELECT COUNT(*) FROM knowledge_calls').fetchone()[0], 0)

    def test_measure_uses_encoded_aliased_multibyte_payload(self):
        self.write_config(knowledge_max_input_bytes=100000)
        card = self.seed('共同令牌卡', '共同令牌' + '密' * 999)
        base, _job, _part = self._unit_context()
        op = self._unit_op()
        cards = self.ev._authorized_catalog('project')
        by_id = {c['id']: c for c in cards}
        proposal_hash = fingerprint([op])
        measure = self.ev._compare_page_measure(base, by_id, [op], proposal_hash)
        entries = [{'card_id': card['id'], 'version': card['version'], 'ops': {0}}]
        measured = measure(entries)
        page = {'cards': [card['id']], 'pairs': [[0, card['id']]]}
        payload = self.ev._compare_payload(base, page, by_id, [op], proposal_hash, 0, 1)
        encoded, _refs = encode_references(payload)
        text = canonical(encoded)
        actual = len(text.encode('utf-8'))
        # The real encoded payload carries the short identity alias, never the
        # raw 32-hex card id.
        self.assertIn('card001', text)
        self.assertNotIn(card['id'], text)
        # The measurement is a bounded worst-case (maximal stage_progress
        # digits) overestimate of the actual page bytes, never a body-character
        # count: multibyte bodies are measured in encoded UTF-8 bytes.
        self.assertGreater(measured, len(self.ev.cards.get(card['id'])['body']))
        self.assertGreaterEqual(measured, actual)
        self.assertLessEqual(measured - actual, 8)


class PlanByteAwareUnitTests(unittest.TestCase):
    def test_byte_measure_splits_where_characters_would_not(self):
        bodies = {c: '一' * 100 for c in ('a', 'b', 'c')}
        # Each singleton is 310 bytes; two are 610 > 350, so the byte ceiling
        # forces one card per page even though the character budget (1000) would
        # pack all three together.
        pages = plan_compare_pages(
            [{'op': 0, 'card_id': c, 'version': 1} for c in bodies],
            lambda c: bodies[c], 1000, measure=lambda entries: 300 * len(entries) + 10,
            max_bytes=350)
        self.assertEqual([len(p['cards']) for p in pages], [1, 1, 1])

    def test_unrepresentable_singleton_raises(self):
        with self.assertRaises(ComparePlanError) as ctx:
            plan_compare_pages(
                [{'op': 0, 'card_id': 'a', 'version': 1}],
                lambda c: 'x' * 100, 1000, measure=lambda entries: 10 ** 6, max_bytes=100)
        self.assertEqual(ctx.exception.reason, 'unrepresentable_required')

    def test_without_measure_keeps_character_budget_behaviour(self):
        bodies = {c: 'x' * 100 for c in ('a', 'b', 'c')}
        pages = plan_compare_pages(
            [{'op': 0, 'card_id': c, 'version': 1} for c in bodies],
            lambda c: bodies[c], 250)
        self.assertEqual([len(p['cards']) for p in pages], [2, 1])
        # Every pair is preserved whole.
        self.assertEqual(sorted(p for page in pages for p in page['pairs']),
                         sorted([[0, c] for c in bodies]))


class DispatchCeilingTests(_ByteHarness):
    def test_finite_cap_covers_two_max_page_revisions(self):
        # The engine supports up to 5 compile attempts, each of which may carry
        # a fresh MAX_COMPARE_PAGES-page comparison plan; the persisted global
        # counter must cover at least two such legal revisions.
        self.assertEqual(MAX_COMPARE_DISPATCHES, MAX_COMPARE_PAGES * 5)
        self.assertGreaterEqual(MAX_COMPARE_DISPATCHES, MAX_COMPARE_PAGES * 2)

    def test_exhausting_the_finite_cap_blocks_and_is_not_reset(self):
        for i in range(6):
            self.seed('共同令牌卡%d' % i, '共同令牌的说明%d。' % i)
        base, job, _part = self._unit_context()
        op = self._unit_op()
        with self.e.transaction():
            self.e.db.execute('UPDATE knowledge_work SET compare_dispatches=? WHERE id=?',
                              (MAX_COMPARE_DISPATCHES - 1, job['id']))
        stages = []

        def call(_job, stage, payload, _cfg):
            stages.append(stage)
            self._record_call(_job, stage)
            return self._distinct(payload)

        with patch.object(self.ev, '_call', side_effect=call):
            with self.assertRaises(StageLimit) as ctx:
                self.ev._compare_stage(job, base, [op], 'tok', {})
        self.assertIn('dispatch limit', str(ctx.exception))
        row = self.e.db.execute('SELECT compare_dispatches FROM knowledge_work WHERE id=?',
                                (job['id'],)).fetchone()
        self.assertEqual(row['compare_dispatches'], MAX_COMPARE_DISPATCHES)
        self.assertEqual(len(stages), 1)   # exactly the last available slot
        # A fresh proposal revision must not reset the persisted finite cap.
        changed = self._unit_op(key='共同令牌二', body='共同令牌的另一版说明。')
        with patch.object(self.ev, '_call', side_effect=call):
            with self.assertRaises(StageLimit):
                self.ev._compare_stage(job, base, [changed], 'tok', {})
        row2 = self.e.db.execute('SELECT compare_dispatches FROM knowledge_work WHERE id=?',
                                 (job['id'],)).fetchone()
        self.assertGreaterEqual(row2['compare_dispatches'], MAX_COMPARE_DISPATCHES)


class CompileQualificationAndFingerprintTests(_ByteHarness):
    def test_compile_task_qualifies_speculative_duplicate_requests(self):
        from tongle.evolution import COMPILE_TASK
        # The speculative whole-set comparison is delegated; a full-body read is
        # still required for an actual update/merge/retire target.
        self.assertIn('delegated to the independent paged comparison', COMPILE_TASK)
        self.assertIn('wholesale set of speculative duplicates', COMPILE_TASK)
        self.assertIn('update, merge or retire target', COMPILE_TASK)
        self.assertIn('read_budget', COMPILE_TASK)

    def test_byte_plan_and_compile_task_are_in_the_contract_fingerprint(self):
        base = self.ev._contract_fingerprint()
        with patch('tongle.evolution.COMPARE_BYTE_PLAN', 'other-plan'):
            self.assertNotEqual(self.ev._contract_fingerprint(), base)
        with patch('tongle.evolution.COMPILE_TASK', 'X'):
            self.assertNotEqual(self.ev._contract_fingerprint(), base)


if __name__ == '__main__':
    unittest.main()
