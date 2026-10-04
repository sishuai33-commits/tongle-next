"""Durability of an accepted reconciliation responsibility.

These exercise the real ``Evolution.process`` / ``Evolution._compare_stage`` /
``Evolution._reconciliation_targets`` / ``Evolution._resolution_problems`` /
``Evolution._confirmation_problems`` paths with synthetic fixtures only: no live
vault, model, credentials or network is touched. Where a model fixture is used
it proves machine behaviour, not semantic quality.

The repair these pin down: a settled *blocking* comparison creates a stable
host-side responsibility (the target identity) that must NOT silently vanish
when the target body later moves, when the proposal is revised, when a
semantic validation retry happens, or when the whole comparison page cache is
replaced. The responsibility is persisted in the bundle apart from the current
proposal's page cache, and its old *semantic verdict* (relation/quote/reason)
is reused only while it is still currency-valid for the exact target body,
source snapshot, authorization scope and compile/verify contract. A moved target
keeps its identity, has its stale verdict withheld, and must be re-read whole
and freshly judged; a genuinely oversize target fails bounded; a narrowed scope
or a drifted source never leaks a revoked target or a stale quote. The compiler
must return one explicit structured resolution per open target and the
independent verifier must return one explicit ``confirmed`` confirmation per
target; a freeform reason plus ``approved:true`` cannot clear the known
responsibility.
"""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tongle.engine import Engine
from tongle.evolution import Evolution, SelectionChanged, StageLimit
from tongle.comparison_review import (MAX_RECONCILIATION_REASON_CHARS,
                                      MAX_RECONCILIATION_TARGETS,
                                      RECONCILIATION_PROTOCOL)

try:  # sibling import under ``unittest discover -s tests``
    from fact_review_stub import with_fact_audit
except ImportError:  # pragma: no cover - package-import invocation
    from tests.fact_review_stub import with_fact_audit


class _DurabilityHarness(unittest.TestCase):
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
               # flow is limited by the attempt/dispatch ceilings, never by an
               # accidental token wall. Production caps are untouched.
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

    def _blocking_model(self, source):
        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'frozen_proposal' in data:
                return self.duplicate_compare(data), {}
            if 'proposal' in data:
                return {'approved': True, 'findings': []}, {}
            return {'operations': [self.create_op(source)]}, {}
        return model

    def _noop_resolving_model(self, source, seen=None, confirm=True, resolutions=True):
        """A model that explicitly resolves each open target (unless disabled)
        and has the verifier explicitly confirm each one (unless disabled)."""
        state = seen if seen is not None else {}

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'frozen_proposal' in data:
                return self.duplicate_compare(data), {}
            if 'proposal' in data:
                recon = data.get('reconciliation') or []
                state.setdefault('verify_recon', []).append(recon)
                if not confirm:
                    return {'approved': True, 'findings': []}, {}
                confirmations = [
                    {'card_id': r['card_id'], 'decision': 'confirmed',
                     'reason': '独立确认该目标在其当前完整正文下确无增量价值'}
                    for r in recon]
                return {'approved': True, 'findings': [],
                        'reconciliation_confirmations': confirmations}, {}
            recon = data.get('reconciliation') or []
            if recon:
                state.setdefault('recon', []).append(recon)
                cards = {c['id']: c for c in data['existing']['cards']}
                state.setdefault('bodies', []).append(
                    [cards.get(r['card_id'], {}).get('body') for r in recon])
                state.setdefault('current', []).append(
                    [r.get('verdict_current') for r in recon])
            if recon and resolutions:
                resolutions_out = [{'card_id': r['card_id'], 'decision': 'noop',
                                    'reason': '当前完整正文下该目标已无增量价值，本轮无需新建重复身份'}
                                   for r in recon]
                return {'operations': [], 'reconciliation_resolutions': resolutions_out}, {}
            if recon:
                return {'operations': []}, {}
            return {'operations': [self.create_op(source)]}, {}

        return model

    def _to_blocked(self, body_len=27000):
        """Drive one real attempt until the comparison blocks it.

        Returns ``(dup_card, source_record, retry_row, stored_bundle)``.
        """
        dup = self.seed('退役旧卡', self.TOKEN + '密' * (body_len - len(self.TOKEN)),
                        state='retired')
        rec = self.capture(); source = self.source(rec)
        result = self.run_model(self._blocking_model(source))
        self.assertEqual(result['processed'], 0)
        row = self.work('retry')[0]
        self.assertIn('comparison', row['error'])
        stored = json.loads(row['bundle'])
        return dup, source, row, stored


class PersistentResponsibilityTests(_DurabilityHarness):
    def test_body_grown_beyond_budget_keeps_the_obligation_and_blocks(self):
        dup, source, _row, _stored = self._to_blocked()
        # A previously blocking target whose body now exceeds the whole body
        # budget: the identity must not vanish, and the required union cannot
        # fit, so the batch is an explicit bounded failure, never a done no-op.
        self.ev.cards.manual_edit(dup['id'], 1, self.TOKEN + '密' * 55000)
        self.unblock()

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'frozen_proposal' in data:
                return self.duplicate_compare(data), {}
            if 'proposal' in data:
                return {'approved': True, 'findings': []}, {}
            return {'operations': []}, {}

        result = self.run_model(model)
        self.assertEqual(result['processed'], 0)
        self.assertIsNone(self.e.db.execute('SELECT 1 FROM knowledge_batches').fetchone())
        failed = self.work('failed')[0]
        self.assertIn('exceeds body budget', failed['error'])

    def test_changed_current_target_is_reread_and_freshly_judged(self):
        dup, source, row, _stored = self._to_blocked()
        new_body = self.TOKEN + '修订后的完整正文。' * 50
        self.ev.cards.manual_edit(dup['id'], 1, new_body)
        self.unblock()
        seen = {}
        result = self.run_model(self._noop_resolving_model(source, seen))
        self.assertEqual(result['processed'], 1)
        self.assertEqual(result['failed'], 0)
        # The whole *current* body was re-read and the old verdict was withheld.
        self.assertEqual(seen['bodies'][0], [new_body])
        self.assertEqual(seen['current'][0], [False])
        # No new duplicate identity was created and the target was not lost.
        self.assertEqual([c['id'] for c in self.ev.cards.list('project', include_inactive=True)],
                         [dup['id']])
        record = json.loads(self.e.db.execute(
            "SELECT error FROM knowledge_failures WHERE work_id=? AND state='completion'",
            (row['id'],)).fetchone()['error'])
        self.assertEqual(record['reconciliation']['count'], 1)
        self.assertEqual(record['reconciliation']['targets'][0]['card_id'], dup['id'])
        self.assertEqual(record['reconciliation']['targets'][0]['decision'], 'noop')
        self.assertEqual(record['reconciliation']['targets'][0]['confirmed'], 'confirmed')

    def test_source_snapshot_drift_does_not_replay_stale_payload(self):
        _dup, _source, _row, stored = self._to_blocked()
        # A drifted source snapshot must not reuse the old verdict: the duty
        # belongs to the previous source snapshot and is not carried forward.
        drifted = dict(stored)
        drifted['source_snapshot'] = {sid: v + 1
                                      for sid, v in stored['source_snapshot'].items()}
        self.assertEqual(self.ev._reconciliation_targets(drifted, _row['id']), [])

    def test_scope_change_keeps_own_identity_but_withholds_stale_verdict(self):
        dup, _source, _row, stored = self._to_blocked()
        # A changed authorization scope (here: sharing disabled) no longer sends
        # a stale verdict, but an OWN target is still authorized, so its identity
        # survives as an outstanding duty rather than silently vanishing.
        drifted = dict(stored)
        drifted['scope_fingerprint'] = 'deadbeef'
        targets = self.ev._reconciliation_targets(drifted, _row['id'])
        self.assertEqual([t['card_id'] for t in targets], [dup['id']])
        self.assertFalse(targets[0]['verdict_current'])
        self.assertIsNone(targets[0]['relation'])
        self.assertEqual(targets[0]['quote'], '')

    def test_contract_drift_keeps_identity_but_withholds_the_old_verdict(self):
        dup, _source, _row, stored = self._to_blocked()
        # A compile/verify contract change drops the frozen page cache (only the
        # responsibility store is carried), so the stored verdict is bound to
        # the old contract while the live base carries the new one.
        drifted = copy.deepcopy(stored)
        drifted['compare'] = None
        drifted['contract'] = 'a-new-contract'
        targets = self.ev._reconciliation_targets(drifted, _row['id'])
        # The responsibility identity survives a contract change, but the stale
        # semantic verdict (relation/quote/reason) is never reused.
        self.assertEqual([t['card_id'] for t in targets], [dup['id']])
        self.assertFalse(targets[0]['verdict_current'])
        self.assertIsNone(targets[0]['relation'])
        self.assertEqual(targets[0]['quote'], '')

    def test_revised_proposal_cannot_drop_the_responsibility(self):
        _dup, _source, _row, _stored = self._to_blocked()
        self.unblock()
        presented = {}

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'frozen_proposal' in data:
                return self.duplicate_compare(data), {}
            if 'proposal' in data:
                return {'approved': True, 'findings': []}, {}
            recon = data.get('reconciliation') or []
            if recon:
                presented['recon'] = recon
                presented['target_in_cards'] = any(
                    c.get('key') == '退役旧卡'
                    for c in (data['existing'].get('cards') or []))
            s = data['sources'][-1]
            # A *different* create that never mentions the outstanding target:
            # the responsibility must still be required and cannot be dropped.
            return {'operations': [{'op': 'create', 'key': '完全不同的新键',
                    'kind': 'memory', 'topic': '主题', 'title': '完全不同的新键',
                    'body': '完全不同的新内容。',
                    'evidence': [{'source_id': s['source_id'], 'version': s['version'],
                                  'quote': s['text']}], 'reason': '新增其它内容'}]}, {}

        result = self.run_model(model)
        self.assertEqual(result['processed'], 0)
        self.assertIsNone(self.e.db.execute('SELECT 1 FROM knowledge_batches').fetchone())
        self.assertTrue(presented.get('recon'))
        self.assertTrue(presented.get('target_in_cards'))

    def test_bare_noop_cannot_clear_a_known_obligation(self):
        _dup, _source, _row, _stored = self._to_blocked()
        self.unblock()

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'frozen_proposal' in data:
                return self.duplicate_compare(data), {}
            if 'proposal' in data:
                return {'approved': True, 'findings': []}, {}
            return {'operations': []}, {}

        result = self.run_model(model)
        self.assertEqual(result['processed'], 0)
        self.assertIsNone(self.e.db.execute('SELECT 1 FROM knowledge_batches').fetchone())

    def test_bare_approval_without_per_target_confirmation_is_rejected(self):
        _dup, source, _row, _stored = self._to_blocked()
        self.unblock()
        seen = {}
        result = self.run_model(self._noop_resolving_model(source, seen, confirm=False))
        self.assertEqual(result['processed'], 0)
        self.assertIsNone(self.e.db.execute('SELECT 1 FROM knowledge_batches').fetchone())
        self.assertTrue(seen.get('verify_recon'))

    def test_legitimate_explicit_noop_after_both_stages_completes(self):
        dup, source, _row, _stored = self._to_blocked()
        self.unblock()
        seen = {}
        result = self.run_model(self._noop_resolving_model(source, seen))
        self.assertEqual(result['processed'], 1)
        self.assertEqual(result['failed'], 0)
        self.assertTrue(seen['recon'])
        self.assertTrue(seen['verify_recon'][0])
        self.assertEqual([c['id'] for c in self.ev.cards.list('project', include_inactive=True)],
                         [dup['id']])

    def test_old_identity_update_resolves_the_obligation(self):
        target = self.seed('旧身份', self.TOKEN + '密' * 27000)   # active
        rec = self.capture(); source = self.source(rec)

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'frozen_proposal' in data:
                return self.duplicate_compare(data), {}
            if 'proposal' in data:
                recon = data.get('reconciliation') or []
                confirmations = [{'card_id': r['card_id'], 'decision': 'confirmed',
                                  'reason': '确认由同一既有身份更新以调和该重叠'} for r in recon]
                return {'approved': True, 'findings': [],
                        'reconciliation_confirmations': confirmations}, {}
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

    def test_card_change_after_the_receipt_settles_rejects_publication(self):
        dup, source, _row, stored = self._to_blocked()
        op = self.create_op(source)
        # The compared card moved after the receipt was bound: the frozen
        # comparison can no longer publish against the refreshed body. The real
        # publication path replays the deterministic related results BEFORE the
        # receipt replay; editing the body removes the lexical pair, so both
        # guards are exercised in the real order.
        self.ev.cards.manual_edit(dup['id'], 1, '人工修改后的正文。')
        with self.assertRaises(SelectionChanged):
            self.ev._replay_related(stored, [op])
            self.ev._verify_compare(stored, [op])

    def test_scope_change_after_the_receipt_settles_rejects_publication(self):
        _dup, source, _row, stored = self._to_blocked()
        op = self.create_op(source)
        drifted = dict(stored, scope_fingerprint='deadbeef')
        with self.assertRaises(SelectionChanged):
            self.ev._verify_compare(drifted, [op])

    def test_retry_does_not_reset_the_persisted_dispatch_budget(self):
        _dup, _source, row, _stored = self._to_blocked()
        before = row['compare_dispatches']
        self.unblock()

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'frozen_proposal' in data:
                return self.duplicate_compare(data), {}
            if 'proposal' in data:
                return {'approved': True, 'findings': []}, {}
            return {'operations': []}, {}

        self.run_model(model)
        after = self.e.db.execute(
            'SELECT compare_dispatches FROM knowledge_work WHERE id=?',
            (row['id'],)).fetchone()['compare_dispatches']
        self.assertGreaterEqual(after, before)


class ScopeChangeRetentionTests(_DurabilityHarness):
    """Defect 1: an own target stays authorized when only sharing is disabled."""

    def test_own_scope_change_retains_identity_and_withholds_stale_verdict(self):
        dup, source, row, _stored = self._to_blocked()
        # Sharing is disabled: the scope fingerprint moves, but the OWN target
        # is still authorized. The obligation identity must survive; the stale
        # verdict/quote/reason are withheld and the whole current body re-read.
        self.write_config(library_excluded_projects=['project'])
        self.unblock()
        seen = {}
        result = self.run_model(self._noop_resolving_model(source, seen))
        self.assertEqual(result['processed'], 1)
        self.assertEqual(result['failed'], 0)
        self.assertTrue(seen['recon'])
        self.assertEqual(seen['current'][0], [False])
        self.assertEqual(seen['bodies'][0], [self.ev.cards.get(dup['id'])['body']])
        self.assertEqual([c['id'] for c in self.ev.cards.list('project', include_inactive=True)],
                         [dup['id']])
        record = json.loads(self.e.db.execute(
            "SELECT error FROM knowledge_failures WHERE work_id=? AND state='completion'",
            (row['id'],)).fetchone()['error'])
        self.assertEqual(record['reconciliation']['count'], 1)
        self.assertEqual(record['reconciliation']['targets'][0]['card_id'], dup['id'])
        self.assertEqual(record['reconciliation']['targets'][0]['decision'], 'noop')
        self.assertEqual(record['reconciliation']['targets'][0]['confirmed'], 'confirmed')

    def test_confirmed_noop_is_not_new_knowledge(self):
        dup, source, _row, _stored = self._to_blocked()
        self.unblock()
        seen = {}
        result = self.run_model(self._noop_resolving_model(source, seen))
        self.assertEqual(result['processed'], 1)
        record = json.loads(self.e.db.execute(
            "SELECT error FROM knowledge_failures WHERE state='completion'").fetchone()['error'])
        # Disposition confirmed, but no new/updated usable knowledge was created.
        self.assertEqual(record['reconciliation']['targets'][0]['confirmed'], 'confirmed')
        self.assertEqual(record['changed_ids'], [])
        self.assertEqual(record['reconciliation']['knowledge_changes'], 0)
        card = self.ev.cards.get(dup['id'])
        self.assertEqual(card['state'], 'retired')
        self.assertEqual(card['version'], 1)


class RevokedSharedTargetTests(_DurabilityHarness):
    """Defect 2: a genuinely revoked SHARED target must stop before dispatch."""

    def _to_blocked_shared(self):
        shared = self.ev.cards.seed('shared', '共享旧卡', 'memory', '主题', '共享旧卡',
                                    self.TOKEN + '密' * 27000, state='retired')
        rec = self.capture(); source = self.source(rec)
        result = self.run_model(self._blocking_model(source))
        self.assertEqual(result['processed'], 0)
        row = self.work('retry')[0]
        stored = json.loads(row['bundle'])
        return shared, source, row, stored

    def test_revoked_shared_target_stops_before_any_dispatch_or_feedback(self):
        shared, _source, row, stored = self._to_blocked_shared()
        self.assertEqual([t['card_id'] for t in self.ev._reconciliation_targets(stored, row['id'])],
                         [shared['id']])
        self.write_config(library_excluded_projects=['project'])
        self.unblock()
        dispatched = {'n': 0}

        def model(cmd, prompt, timeout, env=None):
            dispatched['n'] += 1
            raise AssertionError('provider dispatched for a revoked outstanding target')

        result = self.run_model(model)
        self.assertEqual(dispatched['n'], 0)
        self.assertEqual(result['processed'], 0)
        self.assertEqual(result['failed'], 1)
        self.assertIn('unresolved', self.work('failed')[0]['error'])
        self.assertIsNone(self.e.db.execute('SELECT 1 FROM knowledge_batches').fetchone())
        bundle = json.loads(self.e.db.execute(
            'SELECT bundle FROM knowledge_work WHERE id=?', (row['id'],)).fetchone()['bundle'])
        feedback_blob = json.dumps(bundle.get('feedback') or [], ensure_ascii=False)
        self.assertNotIn('密', feedback_blob)
        # The duty stays auditable (never silently cleared) and no revoked shared
        # card was adopted as a new owned source.
        self.assertEqual([t['card_id'] for t in bundle['reconciliation_store']['targets']],
                         [shared['id']])
        self.assertEqual([c['id'] for c in self.ev.cards.list('project', include_inactive=True)],
                         [])


class ResponsibilityIntegrityTests(_DurabilityHarness):
    """Defect 3: origin proof membership, malformed/forged, sticky overflow."""

    def _rewrite_bundle(self, row_id, bundle):
        with self.e.transaction():
            self.e.db.execute('UPDATE knowledge_work SET bundle=? WHERE id=?',
                              (json.dumps(bundle, ensure_ascii=False), row_id))

    def test_forged_pair_origin_stops_safely(self):
        dup, _source, row, stored = self._to_blocked()
        forged = copy.deepcopy(stored)
        # Simulate the proposal-cache overwrite: the persisted responsibility is
        # the only remaining record, so its forged pair membership must be
        # caught by the offline origin proof rather than masked by a receipt.
        forged['compare'] = None
        forged['reconciliation_store']['targets'][0]['origin']['pair']['relation'] = 'distinct'
        self._rewrite_bundle(row['id'], forged)
        self.unblock()
        dispatched = {'n': 0}

        def model(cmd, prompt, timeout, env=None):
            dispatched['n'] += 1
            return {'operations': []}, {}

        result = self.run_model(model)
        self.assertEqual(dispatched['n'], 0)
        self.assertEqual(result['processed'], 0)
        self.assertEqual(result['failed'], 1)
        self.assertIn('unresolved', self.work('failed')[0]['error'])
        self.assertIsNone(self.e.db.execute('SELECT 1 FROM knowledge_batches').fetchone())
        kept = json.loads(self.e.db.execute(
            'SELECT bundle FROM knowledge_work WHERE id=?', (row['id'],)).fetchone()['bundle'])
        self.assertEqual([t['card_id'] for t in kept['reconciliation_store']['targets']],
                         [dup['id']])

    def test_protocol_drift_stops_safely(self):
        _dup, _source, row, stored = self._to_blocked()
        drifted = copy.deepcopy(stored)
        drifted['compare'] = None
        drifted['reconciliation_store']['targets'][0]['origin']['protocol'] = 'tongle-reconcile-v1'
        self._rewrite_bundle(row['id'], drifted)
        self.unblock()
        dispatched = {'n': 0}

        def model(cmd, prompt, timeout, env=None):
            dispatched['n'] += 1
            return {'operations': []}, {}

        result = self.run_model(model)
        self.assertEqual(dispatched['n'], 0)
        self.assertEqual(result['processed'], 0)
        self.assertIn('unresolved', self.work('failed')[0]['error'])
        self.assertIsNone(self.e.db.execute('SELECT 1 FROM knowledge_batches').fetchone())

    def test_overflow_is_sticky_across_calls_and_restart(self):
        dup, _source, _row, stored = self._to_blocked()
        base = copy.deepcopy(stored)
        source_fp = base['reconciliation_store']['targets'][0]['origin']['source_fingerprint']
        targets = list(base['reconciliation_store']['targets'])
        for i in range(MAX_RECONCILIATION_TARGETS):
            cid = '%032x' % (i + 100000)
            targets.append({'card_id': cid,
                            'origin': {'protocol': RECONCILIATION_PROTOCOL,
                                       'work_id': 'w', 'call_id': 'c', 'page_id': cid,
                                       'proposal_hash': 'p',
                                       'source_fingerprint': source_fp,
                                       'scope_fingerprint': base.get('scope_fingerprint'),
                                       'pair': {'op': 0, 'card_id': cid, 'relation': 'duplicate'},
                                       'target': {'version': 1, 'body_sha256': 'h'},
                                       'proof': 'forged'},
                            'verdict': {}})
        base['reconciliation_store']['targets'] = targets
        self.ev._reconciliation_targets(base)
        self.assertTrue(base['reconciliation_store']['truncated'])
        self.assertEqual(base['reconciliation_store']['overflow']['count'],
                         MAX_RECONCILIATION_TARGETS + 1)
        # A second retrieval on the same persisted store must not clear it.
        self.ev._reconciliation_targets(base)
        self.assertTrue(base['reconciliation_store']['truncated'])
        self.assertEqual(base['reconciliation_store']['overflow']['count'],
                         MAX_RECONCILIATION_TARGETS + 1)
        # Nor does a restart (fresh Evolution over the same persisted store).
        restarted = Evolution(self.e)
        restarted._reconciliation_targets(base)
        self.assertTrue(base['reconciliation_store']['truncated'])
        self.assertEqual(base['reconciliation_store']['overflow']['count'],
                         MAX_RECONCILIATION_TARGETS + 1)
        self.assertEqual([t['card_id'] for t in base['reconciliation_store']['targets']],
                         sorted([t['card_id'] for t in targets])[:MAX_RECONCILIATION_TARGETS])


class ResolutionUnitTests(_DurabilityHarness):
    def _target(self):
        card = self.seed('目标卡', '目标卡的完整正文。')
        recon = [{'card_id': card['id'], 'relation': 'duplicate', 'quote': '',
                  'reason': '', 'op': 0, 'verdict_current': True}]
        return card, recon

    def test_bare_or_missing_resolution_is_rejected(self):
        _card, recon = self._target()
        self.assertTrue(self.ev._resolution_problems(recon, [], None))
        self.assertTrue(self.ev._resolution_problems(recon, [], []))

    def test_resolution_decision_must_match_a_real_operation_identity(self):
        card, recon = self._target()
        resolution = [{'card_id': card['id'], 'decision': 'update',
                       'reason': '更新该既有身份，以实际行动调和重叠'}]
        # update decision with no matching operation -> rejected
        self.assertTrue(self.ev._resolution_problems(recon, [], resolution))
        # a matching update operation on the SAME identity is accepted
        op = {'op': 'update', 'id': card['id'], 'expected_version': 1,
              'body': '新的正文。', 'evidence': [], 'reason': '更新'}
        self.assertEqual(self.ev._resolution_problems(recon, [op], resolution), [])

    def test_unresolved_resolution_is_an_honest_bounded_failure(self):
        card, recon = self._target()
        with self.assertRaises(StageLimit):
            self.ev._resolution_problems(
                recon, [], [{'card_id': card['id'], 'decision': 'unresolved',
                             'reason': '无法从现有完整证据判断'}])

    def test_inactive_target_noop_with_same_claim_create_is_rejected(self):
        card = self.seed('退役卡', '退役卡的完整正文。', state='retired')
        recon = [{'card_id': card['id'], 'relation': 'duplicate', 'quote': '',
                  'reason': '', 'op': 0, 'verdict_current': True}]
        resolution = [{'card_id': card['id'], 'decision': 'noop',
                       'reason': '该目标已无增量价值，本轮无需任何处理动作'}]
        # A no-op on an inactive target with no adopted same-claim create is a
        # legitimate resolution.
        self.assertEqual(self.ev._resolution_problems(recon, [], resolution), [])
        # Adopting a same-claim create under the same key while claiming the
        # retired target is a no-op is rejected.
        create = {'op': 'create', 'key': '退役卡', 'kind': 'memory', 'topic': '主题',
                  'title': '退役卡', 'body': '新的正文。', 'evidence': [], 'reason': '新增'}
        self.assertTrue(self.ev._resolution_problems(recon, [create], resolution))

    def test_confirmation_must_be_explicitly_confirmed(self):
        card, recon = self._target()
        self.assertTrue(self.ev._confirmation_problems(recon, {'approved': True}))
        self.assertTrue(self.ev._confirmation_problems(recon, {
            'approved': True,
            'reconciliation_confirmations': [
                {'card_id': card['id'], 'decision': 'needs_revision',
                 'reason': '证据不足，需要修订该目标'}]}))
        self.assertEqual(self.ev._confirmation_problems(recon, {
            'approved': True,
            'reconciliation_confirmations': [
                {'card_id': card['id'], 'decision': 'confirmed',
                 'reason': '独立确认该目标在当前正文下确无增量价值'}]}), [])

    def test_confirmation_reason_bound_is_enforced(self):
        card, recon = self._target()
        long_reason = '确' * (MAX_RECONCILIATION_REASON_CHARS + 1)
        self.assertTrue(self.ev._confirmation_problems(recon, {
            'approved': True,
            'reconciliation_confirmations': [
                {'card_id': card['id'], 'decision': 'confirmed',
                 'reason': long_reason}]}))


if __name__ == '__main__':
    unittest.main()
