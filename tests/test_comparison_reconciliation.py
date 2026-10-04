"""Bounded explicit reconciliation obligations after a blocking comparison.

These exercise the real ``Evolution.process`` / ``Evolution._compare_stage`` /
``Evolution._reconciliation_targets`` / ``Evolution._stage_loop`` paths with
synthetic fixtures only: no live vault, model, credentials or network is
touched. Where a model fixture is used it proves machine behaviour, not
semantic quality.

The failure being repaired (synthetic reproduction of a fixed real run): the
frozen paged comparison correctly rejected a create as a genuine duplicate of a
long, retired, *late* candidate (its body exceeded the initial 26k body budget,
so the refreshed initial selection omitted it). The next compile only received
a bounded STRING finding with the raw 32-hex id embedded in text -- which cannot
be transported as a typed alias -- returned a reason-only empty operations list
("insufficient available evidence"), and the verifier blindly approved. The
batch finished with the retired identity still retired and no coverage of the
new decision: a false completion, not a successful recovery.

The fix preserves a bounded explicit obligation derived from the *settled*
comparison receipts (never from the failure strings and never from a mutable
summary flag), bound to the source snapshot/scope/proposal/call/card
version+bodyhash. Every target's whole body is prepared before the next compile
and handed to both the compiler and the independent verifier as a typed,
addressable identity; a drifted source/scope/target invalidates it instead of
replaying a stale judgement; and a required union that genuinely cannot fit
fails explicitly (bounded) instead of becoming a fake no-op. No model budget is
added, no SQL is reset, and the finite attempt/dispatch/daily budgets remain.
"""
import json
import re
import copy
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tongle.engine import Engine
from tongle.evolution import Evolution, canonical
from tongle.comparison_review import (COMPARE_REPAIR_WORST_CASE,
                                      MAX_COMPARE_FEEDBACK_CHARS,
                                      MAX_COMPARE_FEEDBACK_ITEMS,
                                      MAX_COMPARE_PAGE_ATTEMPTS,
                                      compare_repair_feedback,
                                      compare_repair_field,
                                      reconciliation_obligations)

try:  # sibling import under ``unittest discover -s tests``
    from fact_review_stub import with_fact_audit
except ImportError:  # pragma: no cover - package-import invocation
    from tests.fact_review_stub import with_fact_audit


class _ReconcileHarness(unittest.TestCase):
    TOKEN = '共同令牌'
    MAIN_TEXT = '新增共同令牌材料。'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.e = Engine(self.root / 'state')
        self.e.init(self.root / 'vault'); self.addCleanup(self.e.store.close)
        self.write_config()
        self.ev = Evolution(self.e)

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

    def source(self, rec):
        return self.e.store.source(rec['source_id'])

    def run_model(self, model, force=True):
        # Shared by this module and by test_completion_contract's subclasses via
        # inheritance; the focused fact-audit dispatch is answered structurally.
        with patch('tongle.worker.invoke_json', side_effect=with_fact_audit(model)):
            return self.ev.process(force=force)

    def work(self, state=None):
        sql = 'SELECT * FROM knowledge_work'; args = ()
        if state:
            sql += ' WHERE state=?'; args = (state,)
        return [dict(r) for r in self.e.db.execute(sql, args).fetchall()]

    def unblock(self):
        with self.e.transaction():
            self.e.db.execute('UPDATE knowledge_work SET next_at=0')

    def create_op(self, source):
        return {'op': 'create', 'key': self.TOKEN, 'kind': 'memory', 'topic': '主题',
                'title': self.TOKEN, 'body': self.TOKEN + '的新说明。',
                'evidence': [{'source_id': source['source_id'],
                              'version': source['version'], 'quote': source['text']}],
                'reason': '新增材料'}

    def duplicate_compare(self, data):
        cand = {c['card_id']: c for c in data['page']['candidates']}
        out = []
        for p in data['page']['pairs']:
            body = cand[p['card_id']]['body']
            out.append({'op': p['op'], 'card_id': p['card_id'], 'relation': 'duplicate',
                        'quote': body[:60],
                        'reason': 'the proposed create recreates this existing card claim'})
        return {'pairs': out}

    def _run_to_blocked(self, body_len=27000):
        """Drive one real attempt until the independent comparison blocks it.

        Returns ``(card, row, stored_bundle)`` for the parked retry row.
        """
        card = self.seed('退役旧卡', self.TOKEN + '密' * (body_len - len(self.TOKEN)),
                         state='retired')
        rec = self.capture(); source = self.source(rec)

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'frozen_proposal' in data:
                return self.duplicate_compare(data), {}
            if 'proposal' in data:
                return {'approved': True, 'findings': []}, {}
            return {'operations': [self.create_op(source)]}, {}

        result = self.run_model(model)
        self.assertEqual(result['processed'], 0)
        row = self.work('retry')[0]
        stored = json.loads(row['bundle'])
        return card, row, stored

    def _unit_base(self, cfg=None):
        """A real compile base over the current store, with a live work row."""
        rec = self.capture()
        part = list(self.ev._source_parts(self.e.store.source(rec['source_id'])))[0]
        selected = self.ev._related('project', [part], cfg)
        guard = selected.pop('guard', {})
        selected.pop('related_hits', None)
        authorized = self.ev._authorized_catalog('project')
        catalog_cards = [{'id': c['id'], 'version': c['version'], 'project': c['project'],
                          'state': c['state'], 'basis_cards': c.get('basis_cards') or []}
                         for c in authorized]
        base = {'project': 'project', 'sources': [part], 'existing': selected,
                'catalog_cards': catalog_cards, 'guard': guard,
                'source_snapshot': {part['source_id']: part['version']},
                'scope_fingerprint': self.ev._scope_fingerprint('project'),
                'stage_calls': {'compile': 0, 'verify': 0},
                'read_sets': {'compile': {}, 'verify': {}},
                'read_feedback': {'compile': [], 'verify': []}}
        with self.e.transaction():
            self.e.db.execute(
                'INSERT OR REPLACE INTO knowledge_work(id,project,session,inputs,state,'
                'lease_token,lease_until,created_at) VALUES(?,?,?,?,?,?,?,?)',
                ('w', 'project', 'session', json.dumps([part]), 'running', 'tok',
                 time.time() + 600, time.time()))
        job = dict(self.e.db.execute(
            'SELECT * FROM knowledge_work WHERE id=?', ('w',)).fetchone())
        return base, job, part


class LateLongDuplicateReconciliationTests(_ReconcileHarness):
    def test_long_late_duplicate_target_is_reconciled_and_read_whole(self):
        dup = self.seed('退役旧卡', self.TOKEN + '密' * 27000, state='retired')
        rec = self.capture(); source = self.source(rec)
        calls = {'compile': 0, 'compare': 0}
        seen = {'recon': [], 'verify_recon': []}

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'frozen_proposal' in data:
                calls['compare'] += 1
                return self.duplicate_compare(data), {}
            if 'proposal' in data:
                recon = data.get('reconciliation') or []
                seen['verify_recon'].append(recon)
                confirmations = [
                    {'card_id': r['card_id'], 'decision': 'confirmed',
                     'reason': '确认该退役目标已在本轮被明确处置且无增量价值'}
                    for r in recon]
                return {'approved': True, 'findings': [],
                        'reconciliation_confirmations': confirmations}, {}
            calls['compile'] += 1
            recon = data.get('reconciliation')
            if recon:
                seen['recon'].append(data)
                cards = {c['id']: c for c in data['existing']['cards']}
                catalog = {c['id'] for c in data['existing']['catalog']}
                alias = recon[0]['card_id']
                # The whole long body the initial 26k selection omitted must be
                # supplied, and the target must be an addressable typed alias.
                self.assertIn(alias, cards)
                self.assertIn(alias, catalog)
                self.assertTrue(re.fullmatch(r'(?:card|ref)[0-9]{3,}', alias), alias)
                self.assertEqual(cards[alias]['body'],
                                 self.ev.cards.get(dup['id'])['body'])
                return {'operations': [],
                        'reconciliation_resolutions': [
                            {'card_id': alias, 'decision': 'noop',
                             'reason': '同一退役卡已承载该主张，本轮不新建重复身份'}],
                        'reason': '同一退役卡已承载该主张，本轮不新建重复身份'}, {}
            return {'operations': [self.create_op(source)]}, {}

        first = self.run_model(model)
        self.assertEqual(first['processed'], 0)
        self.assertEqual(calls['compare'], 1)
        row = self.work('retry')[0]
        self.assertIn('comparison', row['error'])
        stored = json.loads(row['bundle'])
        # The obligation is reconstructed from the settled receipts, not from
        # the failure string: it carries the structured target identity.
        targets = self.ev._reconciliation_targets(stored)
        self.assertEqual([t['card_id'] for t in targets], [dup['id']])
        self.assertEqual(targets[0]['relation'], 'duplicate')
        dispatches = row['compare_dispatches']

        self.unblock()
        second = self.run_model(model)
        self.assertEqual(second['processed'], 1)
        self.assertEqual(second['failed'], 0)
        self.assertTrue(seen['recon'], 'the compiler saw the reconciliation context')
        self.assertEqual(calls['compare'], 1)   # no extra comparison call
        # The independent verifier saw the same structured blocking context even
        # though the proposal became a no-op.
        self.assertTrue(any(v for v in seen['verify_recon'] if v))
        # No new duplicate identity was created; the source was not silently lost.
        self.assertEqual([c['id'] for c in self.ev.cards.list('project', include_inactive=True)],
                         [dup['id']])
        # The finite comparison budget was never reset by the retry.
        after = self.e.db.execute('SELECT compare_dispatches FROM knowledge_work WHERE id=?',
                                  (row['id'],)).fetchone()
        self.assertEqual(after['compare_dispatches'], dispatches)
        # The compact completion record ties the accepted no-op to the target.
        audit = self.e.db.execute(
            "SELECT error FROM knowledge_failures WHERE work_id=? AND state='completion'",
            (row['id'],)).fetchone()
        record = json.loads(audit['error'])
        self.assertEqual(record['reconciliation']['count'], 1)
        self.assertEqual(record['reconciliation']['targets'][0]['card_id'], dup['id'])
        # No body text leaks into the completion record.
        self.assertNotIn('密', audit['error'])

    def test_duplicate_target_identity_is_reused_by_update(self):
        target = self.seed('旧身份', self.TOKEN + '密' * 27000)   # active
        rec = self.capture(); source = self.source(rec)
        calls = {'compile': 0}

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'frozen_proposal' in data:
                return self.duplicate_compare(data), {}
            if 'proposal' in data:
                recon = data.get('reconciliation') or []
                confirmations = [
                    {'card_id': r['card_id'], 'decision': 'confirmed',
                     'reason': '确认同一既有身份已被更新调和重叠'}
                    for r in recon]
                return {'approved': True, 'findings': [],
                        'reconciliation_confirmations': confirmations}, {}
            calls['compile'] += 1
            recon = data.get('reconciliation')
            if recon:
                alias = recon[0]['card_id']
                return {'operations': [{'op': 'update', 'id': alias,
                        'expected_version': 1,
                        'body': self.TOKEN + '调和后的正文。',
                        'evidence': [{'source_id': source['source_id'],
                                      'version': source['version'],
                                      'quote': source['text']}],
                        'reason': '复用既有身份，通过更新调和该重叠'}],
                        'reconciliation_resolutions': [
                            {'card_id': alias, 'decision': 'update',
                             'reason': '复用既有身份，通过更新调和该重叠'}]}, {}
            return {'operations': [self.create_op(source)]}, {}

        first = self.run_model(model)
        self.assertEqual(first['processed'], 0)
        self.unblock()
        second = self.run_model(model)
        self.assertEqual(second['processed'], 1)
        card = self.ev.cards.get(target['id'])
        self.assertEqual(card['body'], self.TOKEN + '调和后的正文。')
        self.assertEqual(card['version'], 2)
        self.assertEqual(len(self.ev.cards.list('project', include_inactive=True)), 1)


class PrematureEmptyFinalTests(_ReconcileHarness):
    def test_empty_final_cannot_finish_without_reading_required_target(self):
        target = self.seed('必需目标', '必需目标的完整正文。')
        base, job, _part = self._unit_base()
        payloads = []

        def call(_job, stage, payload, _cfg):
            payloads.append(copy.deepcopy(payload))
            return {'operations': []}

        with patch.object(self.ev, '_call', side_effect=call):
            response = self.ev._stage_loop(
                job, 'compile', base, 'TASK', {}, 'tok',
                base['read_sets']['compile'],
                requirements=lambda _response: {target['id']})
        # The premature empty final was refused until the whole target body was
        # actually dispatched; only then (second bounded call) was it accepted.
        self.assertEqual(len(payloads), 2)
        self.assertNotIn(target['id'],
                         [c['id'] for c in payloads[0]['existing']['cards']])
        self.assertIn(target['id'],
                      [c['id'] for c in payloads[1]['existing']['cards']])
        self.assertEqual(response, {'operations': []})

    def test_unresolved_is_an_honest_failure_not_a_completion(self):
        self.seed('退役旧卡', self.TOKEN + '密' * 27000, state='retired')
        rec = self.capture(); source = self.source(rec)

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'frozen_proposal' in data:
                return self.duplicate_compare(data), {}
            if 'proposal' in data:
                return {'approved': True, 'findings': []}, {}
            if data.get('reconciliation'):
                return {'unresolved': True, 'reason': 'cannot decide from the available evidence'}, {}
            return {'operations': [self.create_op(source)]}, {}

        self.run_model(model)
        self.unblock()
        result = self.run_model(model)
        # An unresolved answer blocks honestly; it is never turned into a done
        # batch that drops the established responsibility.
        self.assertEqual(result['processed'], 0)
        self.assertEqual(result['failed'], 1)
        row = self.work('failed')[0]
        self.assertIn('unresolved evidence', row['error'])
        self.assertIsNone(self.e.db.execute('SELECT 1 FROM knowledge_batches').fetchone())


class ObligationCurrencyTests(_ReconcileHarness):
    def test_obligation_survives_restart_without_resetting_budget(self):
        dup, row, stored = self._run_to_blocked()
        before = row['compare_dispatches']
        restarted = Evolution(self.e)
        targets = restarted._reconciliation_targets(stored)
        self.assertEqual([t['card_id'] for t in targets], [dup['id']])
        after = self.e.db.execute('SELECT compare_dispatches FROM knowledge_work WHERE id=?',
                                  (row['id'],)).fetchone()
        self.assertEqual(after['compare_dispatches'], before)

    def test_card_drift_invalidates_the_old_verdict_but_keeps_the_obligation(self):
        dup, _row, stored = self._run_to_blocked()
        self.assertEqual([t['card_id'] for t in self.ev._reconciliation_targets(stored)],
                         [dup['id']])
        self.ev.cards.manual_edit(dup['id'], 1, self.TOKEN + '人工修改后的正文。')
        # A moved card invalidates the *old semantic verdict* (relation/quote/
        # reason) but NOT the responsibility: the target identity stays an
        # outstanding, host-side evidence obligation that must be re-read and
        # freshly judged. A stale verdict is never reused and never simply
        # disappears.
        targets = self.ev._reconciliation_targets(stored)
        self.assertEqual([t['card_id'] for t in targets], [dup['id']])
        self.assertFalse(targets[0]['verdict_current'])
        self.assertIsNone(targets[0]['relation'])
        self.assertEqual(targets[0]['quote'], '')

    def test_source_snapshot_drift_invalidates_the_obligation(self):
        dup, _row, stored = self._run_to_blocked()
        drifted = dict(stored)
        drifted['source_snapshot'] = {sid: ver + 1
                                      for sid, ver in stored['source_snapshot'].items()}
        self.assertEqual(self.ev._reconciliation_targets(drifted), [])

    def test_scope_change_retains_own_identity_but_withholds_stale_verdict(self):
        dup, _row, stored = self._run_to_blocked()
        # A scope change (sharing disabled) must not silently drop an OWN target
        # that is still authorized: the identity survives as an outstanding,
        # auditable duty while its stale verdict/quote/reason are withheld.
        drifted = dict(stored, scope_fingerprint='deadbeef')
        targets = self.ev._reconciliation_targets(drifted)
        self.assertEqual([t['card_id'] for t in targets], [dup['id']])
        self.assertFalse(targets[0]['verdict_current'])
        self.assertIsNone(targets[0]['relation'])
        self.assertEqual(targets[0]['quote'], '')

    def test_erased_summary_cannot_hide_a_blocking_obligation(self):
        dup, _row, stored = self._run_to_blocked()
        # Tamper with the mutable summary flag the host must never trust: the
        # cached findings are erased, but the obligation is recomputed from the
        # re-validated dispositions, so it cannot be hidden.
        for _pid, entry in stored['compare']['pages'].items():
            entry['findings'] = []
            entry.pop('report', None)
        targets = self.ev._reconciliation_targets(stored)
        self.assertEqual([t['card_id'] for t in targets], [dup['id']])
        self.assertEqual(targets[0]['relation'], 'duplicate')


class OversizeReconciliationTests(_ReconcileHarness):
    def test_required_union_that_cannot_fit_fails_explicitly(self):
        # Two related duplicate targets, each larger than the initial budget and
        # together larger than the fixed 52k total: the whole required union
        # cannot be prepared, so it must fail as an explicit capacity result.
        self.seed('旧卡甲', self.TOKEN + '甲' * 26996, state='retired')
        self.seed('旧卡乙', self.TOKEN + '乙' * 26996, state='retired')
        rec = self.capture(); source = self.source(rec)
        calls = {'compare': 0}

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'frozen_proposal' in data:
                calls['compare'] += 1
                return self.duplicate_compare(data), {}
            if 'proposal' in data:
                return {'approved': True, 'findings': []}, {}
            return {'operations': [self.create_op(source)]}, {}

        first = self.run_model(model)
        self.assertEqual(first['processed'], 0)
        self.assertGreaterEqual(calls['compare'], 2)   # one page per long card
        self.unblock()
        second = self.run_model(model)
        self.assertEqual(second['processed'], 0)
        row = self.work('failed')[0]
        self.assertIn('exceeds body budget', row['error'])
        self.assertIsNone(self.e.db.execute('SELECT 1 FROM knowledge_batches').fetchone())


class ReconciliationUnitTests(unittest.TestCase):
    def test_distinct_dispositions_are_not_obligations(self):
        dispositions = [
            {'op': 0, 'card_id': 'a' * 32, 'relation': 'distinct',
             'quote': 'x', 'reason': 'scope and claim differ here'},
            {'op': 0, 'card_id': 'b' * 32, 'relation': 'duplicate',
             'quote': 'y', 'reason': 'the same claim under another title'},
            {'op': 1, 'card_id': 'b' * 32, 'relation': 'conflict',
             'quote': 'y', 'reason': 'contradicts the candidate condition'},
        ]
        out = reconciliation_obligations(dispositions)
        self.assertEqual([o['card_id'] for o in out], ['b' * 32])
        self.assertEqual(out[0]['relation'], 'duplicate')
        self.assertEqual(out[0]['quote'], 'y')
        self.assertEqual(out[0]['reason'], 'the same claim under another title')

    def test_four_byte_invalid_card_id_feedback_fits_reserved_worst_case(self):
        # An invalid/unknown card_id may contain 4-byte (non-BMP) code points
        # and is echoed into several bounded feedback strings. The whole
        # worst-case feedback reservation must use 4-byte code points too, not
        # only the locator suggestion.
        filler = COMPARE_REPAIR_WORST_CASE['feedback'][0]
        self.assertEqual(len(filler), MAX_COMPARE_FEEDBACK_CHARS)
        self.assertTrue(all(ord(ch) > 0xFFFF for ch in filler))
        self.assertEqual(len(filler.encode('utf-8')), 4 * MAX_COMPARE_FEEDBACK_CHARS)
        cid = '\U0001F600' * 400
        validation = {'feedback': [{'op': 0, 'card_id': cid, 'field': 'card_id',
                                    'error': 'unexpected pair for this page'}]
                      * MAX_COMPARE_FEEDBACK_ITEMS}
        strings = compare_repair_feedback(validation)
        self.assertEqual(len(strings), MAX_COMPARE_FEEDBACK_ITEMS)
        field = compare_repair_field(MAX_COMPARE_PAGE_ATTEMPTS, strings)
        worst = len(canonical(COMPARE_REPAIR_WORST_CASE).encode('utf-8'))
        real = len(canonical(field).encode('utf-8'))
        self.assertLessEqual(real, worst)

    def test_reconciliation_task_and_protocol_are_fingerprinted(self):
        from tongle.evolution import COMPILE_TASK, VERIFY_TASK
        self.assertIn('reconciliation', COMPILE_TASK)
        self.assertIn('reconciliation', VERIFY_TASK)
        with patch('tongle.evolution.RECONCILIATION_TASK', 'X'):
            cfg = self._fingerprint()
            with patch('tongle.evolution.RECONCILIATION_TASK', 'Y'):
                self.assertNotEqual(cfg, self._fingerprint())

    def _fingerprint(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        e = Engine(root / 'state'); e.init(root / 'vault'); self.addCleanup(e.store.close)
        (e.home / 'config.json').write_text(json.dumps({'knowledge_mode': 'evolving'}))
        return Evolution(e)._contract_fingerprint()


if __name__ == '__main__':
    unittest.main()
