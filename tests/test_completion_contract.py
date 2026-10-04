"""Completion-contract repair: materiality + bounded verify STRUCTURE repair.

These exercise the real ``Evolution.process`` / ``Evolution._compare_stage`` /
``Evolution._stage_loop`` / ``Evolution._confirmation_problems`` /
``Evolution._verify_repair_*`` paths with synthetic fixtures only: no live
vault, model, credentials or network is touched. Every model reply is a
synthetic stub, so these prove machine behaviour, not semantic quality.

Repairs pinned down here:

1. The reconciliation dispositions are explicitly scoped to the *listed*
   existing identities only, so an independent supported source increment is
   still carried by a normal create/update + the mandatory comparison and a
   no-op cannot be misread as a global "drop everything" permission.
2. One shared MATERIALITY_POLICY clause, applied identically to compile and
   verify, judges the actual final card state after the frozen operations (not
   the explanation prose): a supported increment missing from both the current
   cards and the operations must be rejected by the independent verifier.
3. A final verify reply that is ``approved:true`` with an empty ``findings``
   list but an invalid/missing ``reconciliation_confirmations`` STRUCTURE is
   asked again within the SAME ``MAX_STAGE_CALLS=3`` slots and the SAME frozen
   proposal, with host-only structural diagnostics plus typed expected target
   ids. It is never a semantic retry, never fabricates a confirmation and never
   auto-approves; the durable first-failure audit survives a later success.
"""
import json
import re
import unittest
from unittest.mock import patch

from tongle.evolution import (MATERIALITY_POLICY, MAX_STAGE_CALLS,
                              VERIFY_FORMAT_AUDIT_STATE)
from tongle.comparison_review import (MAX_RECONCILIATION_REASON_CHARS,
                                      RECONCILIATION_TASK)

try:  # sibling import under ``unittest discover -s tests``
    from test_comparison_reconciliation import _ReconcileHarness
except ImportError:  # pragma: no cover - package-import invocation
    from tests.test_comparison_reconciliation import _ReconcileHarness


class _CompletionHarness(_ReconcileHarness):
    def _verify_format_rows(self, work_id=None):
        sql = "SELECT error FROM knowledge_failures WHERE state=?"
        args = [VERIFY_FORMAT_AUDIT_STATE]
        if work_id:
            sql += ' AND work_id=?'; args.append(work_id)
        return [r['error'] for r in self.e.db.execute(sql, args).fetchall()]

    def _completion(self, work_id):
        row = self.e.db.execute(
            "SELECT error FROM knowledge_failures WHERE work_id=? AND state='completion'",
            (work_id,)).fetchone()
        return json.loads(row['error']) if row else None

    def _compile_noop_resolving(self, data):
        recon = data.get('reconciliation') or []
        return {'operations': [], 'reconciliation_resolutions': [
            {'card_id': r['card_id'], 'decision': 'noop',
             'reason': '当前完整正文下该目标已无增量价值，本轮无需新建重复身份'}
            for r in recon]}, {}


class BoundedVerifyStructureRepairTests(_CompletionHarness):
    def test_missing_confirmation_then_corrected_repair_succeeds(self):
        card, row, _stored = self._run_to_blocked()
        self.unblock()
        verify_calls = []

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'frozen_proposal' in data:
                return self.duplicate_compare(data), {}
            if 'proposal' in data:
                recon = data.get('reconciliation') or []
                verify_calls.append(data)
                if len(verify_calls) == 1:
                    # The described real defect: approved + empty findings but
                    # no reconciliation_confirmations at all.
                    return {'approved': True, 'findings': []}, {}
                return {'approved': True, 'findings': [],
                        'reconciliation_confirmations': [
                            {'card_id': r['card_id'], 'decision': 'confirmed',
                             'reason': '独立确认该目标在当前整卡正文下确无增量价值'}
                            for r in recon]}, {}
            return self._compile_noop_resolving(data)

        result = self.run_model(model)
        self.assertEqual(result['processed'], 1)
        self.assertEqual(result['failed'], 0)
        # Exactly one repair dispatch, inside the existing slots.
        self.assertEqual(len(verify_calls), 2)
        self.assertNotIn('verify_repair', verify_calls[0])
        repair = verify_calls[1].get('verify_repair')
        self.assertIsInstance(repair, dict)
        # Host-only structural diagnostics plus typed expected targets; never a
        # model reason, review note, quote or body.
        blob = json.dumps(repair, ensure_ascii=False)
        self.assertNotIn('密', blob)
        self.assertEqual(len(repair['expected_targets']), 1)
        alias = repair['expected_targets'][0]['card_id']
        self.assertTrue(re.fullmatch(r'(?:card|ref)[0-9]{3,}', alias), alias)
        self.assertEqual(repair['problems'][0]['field'], 'reconciliation_confirmations')
        self.assertIn('reconciliation_confirmations', blob)
        # The durable first-failure audit survives the successful repair.
        rows = self._verify_format_rows(row['id'])
        self.assertEqual(len(rows), 1)
        record = json.loads(rows[0])
        self.assertEqual(record['stage'], 'verify')
        self.assertEqual(record['expected'], [card['id']])
        self.assertNotIn('密', rows[0])
        self.assertIsNotNone(self._completion(row['id']))
        self.assertEqual(self._completion(row['id'])['verify_format_repairs'], 1)

    def test_wrong_target_and_overlong_reason_cannot_pass_until_corrected(self):
        _card, row, _stored = self._run_to_blocked()
        self.unblock()
        verify_calls = []

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'frozen_proposal' in data:
                return self.duplicate_compare(data), {}
            if 'proposal' in data:
                recon = data.get('reconciliation') or []
                verify_calls.append(data)
                index = len(verify_calls)
                if index == 1:
                    return {'approved': True, 'findings': [],
                            'reconciliation_confirmations': [
                                {'card_id': 'f' * 32, 'decision': 'confirmed',
                                 'reason': '确认了错误的目标身份'}]}, {}
                if index == 2:
                    return {'approved': True, 'findings': [],
                            'reconciliation_confirmations': [
                                {'card_id': r['card_id'], 'decision': 'confirmed',
                                 'reason': '确' * (MAX_RECONCILIATION_REASON_CHARS + 1)}
                                for r in recon]}, {}
                return {'approved': True, 'findings': [],
                        'reconciliation_confirmations': [
                            {'card_id': r['card_id'], 'decision': 'confirmed',
                             'reason': '独立确认该目标在当前整卡正文下确无增量价值'}
                            for r in recon]}, {}
            return self._compile_noop_resolving(data)

        result = self.run_model(model)
        self.assertEqual(result['processed'], 1)
        self.assertEqual(len(verify_calls), 3)
        # Two rejected structure replies each left durable evidence.
        self.assertEqual(len(self._verify_format_rows(row['id'])), 2)

    def test_three_missing_leave_outer_reject_with_no_fourth_call(self):
        _card, row, _stored = self._run_to_blocked()
        self.unblock()
        verify_calls = []

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'frozen_proposal' in data:
                return self.duplicate_compare(data), {}
            if 'proposal' in data:
                verify_calls.append(data)
                return {'approved': True, 'findings': []}, {}
            return self._compile_noop_resolving(data)

        result = self.run_model(model)
        self.assertEqual(result['processed'], 0)
        # All three existing slots were consumed; no fourth call exists.
        self.assertEqual(len(verify_calls), MAX_STAGE_CALLS)
        self.assertIsNone(self.e.db.execute('SELECT 1 FROM knowledge_batches').fetchone())
        # Every one of the three malformed replies left durable structured
        # evidence -- including slot 3, which is returned unchanged to the outer
        # gate without a fourth dispatch.
        rows = self._verify_format_rows(row['id'])
        self.assertEqual(len(rows), 3)
        self.assertEqual(sorted(json.loads(r)['slot'] for r in rows), [1, 2, 3])

    def test_needs_revision_confirmation_never_enters_repair(self):
        self._assert_no_repair(self._needs_revision_verify)

    def test_approved_false_never_enters_repair(self):
        self._assert_no_repair(lambda recon: {'approved': False, 'findings': []})

    def test_nonempty_findings_never_enter_repair(self):
        self._assert_no_repair(lambda recon: {
            'approved': True, 'findings': [{'finding': 'still a blocking defect'}]})

    def test_top_level_unresolved_never_enters_repair(self):
        self._assert_no_repair(lambda recon: {
            'unresolved': True, 'reason': 'cannot decide from the available evidence'})

    def _needs_revision_verify(self, recon):
        return {'approved': True, 'findings': [],
                'reconciliation_confirmations': [
                    {'card_id': r['card_id'], 'decision': 'needs_revision',
                     'reason': '本来源确含新增条件，纯 no-op 未处置缺失增量'}
                    for r in recon]}

    def _assert_no_repair(self, verify_reply):
        _card, _row, _stored = self._run_to_blocked()
        self.unblock()
        verify_calls = []

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'frozen_proposal' in data:
                return self.duplicate_compare(data), {}
            if 'proposal' in data:
                recon = data.get('reconciliation') or []
                verify_calls.append(data)
                return verify_reply(recon), {}
            return self._compile_noop_resolving(data)

        result = self.run_model(model)
        self.assertEqual(result['processed'], 0)
        # Exactly one verify dispatch: an explicit rejection is never a format
        # repair, even though slots remain.
        self.assertEqual(len(verify_calls), 1)
        self.assertIsNone(self.e.db.execute('SELECT 1 FROM knowledge_batches').fetchone())

    def test_earlier_needs_cards_slot_counts_toward_the_same_three(self):
        _card, _row, _stored = self._run_to_blocked()
        self.unblock()
        verify_calls = []

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'frozen_proposal' in data:
                return self.duplicate_compare(data), {}
            if 'proposal' in data:
                verify_calls.append(data)
                if len(verify_calls) == 1:
                    # A redundant request still consumes one of the same three
                    # verify slots before any final answer.
                    return {'needs_cards': []}, {}
                return {'approved': True, 'findings': []}, {}
            return self._compile_noop_resolving(data)

        result = self.run_model(model)
        self.assertEqual(result['processed'], 0)
        # One request slot + two final slots = the original three; no fourth.
        self.assertEqual(len(verify_calls), MAX_STAGE_CALLS)

    def test_target_drift_between_invalid_and_repair_aborts(self):
        card, _row, _stored = self._run_to_blocked()
        self.unblock()
        verify_calls = []

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'frozen_proposal' in data:
                return self.duplicate_compare(data), {}
            if 'proposal' in data:
                verify_calls.append(data)
                if len(verify_calls) == 1:
                    # The compared identity moves during the provider call.
                    current = self.ev.cards.get(card['id'])
                    self.ev.cards.manual_edit(card['id'], current['version'],
                                              self.TOKEN + '人工修改后的正文。')
                    return {'approved': True, 'findings': []}, {}
                raise AssertionError('repair dispatched after a target drift')
            return self._compile_noop_resolving(data)

        result = self.run_model(model)
        self.assertEqual(len(verify_calls), 1)
        self.assertEqual(result['processed'], 0)
        self.assertIsNone(self.e.db.execute('SELECT 1 FROM knowledge_batches').fetchone())

    def test_scope_drift_between_invalid_and_repair_aborts(self):
        _card, _row, _stored = self._run_to_blocked()
        self.unblock()
        verify_calls = []

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'frozen_proposal' in data:
                return self.duplicate_compare(data), {}
            if 'proposal' in data:
                verify_calls.append(data)
                if len(verify_calls) == 1:
                    # Scope narrows during the provider call; the pre-dispatch
                    # check must refuse the stale repair.
                    self.write_config(library_excluded_projects=['project'])
                    return {'approved': True, 'findings': []}, {}
                raise AssertionError('repair dispatched after a scope change')
            return self._compile_noop_resolving(data)

        result = self.run_model(model)
        self.assertEqual(len(verify_calls), 1)
        self.assertEqual(result['processed'], 0)
        self.assertIsNone(self.e.db.execute('SELECT 1 FROM knowledge_batches').fetchone())

    # ---- safe confirmation-structure normalisation ---------------------------

    def _assert_malformed_structure_repairs(self, value):
        """A non-list confirmation value is malformed structure, not a crash,
        and is repaired inside the existing three slots."""
        card, row, _stored = self._run_to_blocked()
        self.unblock()
        verify_calls = []

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'frozen_proposal' in data:
                return self.duplicate_compare(data), {}
            if 'proposal' in data:
                recon = data.get('reconciliation') or []
                verify_calls.append(data)
                if len(verify_calls) == 1:
                    # A structurally present but malformed non-list value must
                    # not raise TypeError; it is routed to the format repair.
                    return {'approved': True, 'findings': [],
                            'reconciliation_confirmations': value}, {}
                return {'approved': True, 'findings': [],
                        'reconciliation_confirmations': [
                            {'card_id': r['card_id'], 'decision': 'confirmed',
                             'reason': '独立确认该目标在当前整卡正文下确无增量价值'}
                            for r in recon]}, {}
            return self._compile_noop_resolving(data)

        result = self.run_model(model)
        self.assertEqual(result['processed'], 1)
        self.assertEqual(result['failed'], 0)
        self.assertEqual(len(verify_calls), 2)
        self.assertTrue(verify_calls[1].get('verify_repair'))
        rows = self._verify_format_rows(row['id'])
        self.assertEqual(len(rows), 1)
        record = json.loads(rows[0])
        self.assertEqual(record['stage'], 'verify')
        self.assertEqual(record['slot'], 1)
        self.assertEqual(record['expected'], [card['id']])
        self.assertEqual(record['fields'], ['reconciliation_confirmations'])

    def test_true_confirmation_structure_is_malformed_not_fatal(self):
        self._assert_malformed_structure_repairs(True)

    def test_int_confirmation_structure_is_malformed_not_fatal(self):
        self._assert_malformed_structure_repairs(1)

    def test_malformed_decision_object_blocks_repair(self):
        """A structurally present decision object with an explicit semantic
        rejection stays a rejection: it must never enter format repair, and it
        is never guessed into a confirmation."""
        _card, row, _stored = self._run_to_blocked()
        self.unblock()
        verify_calls = []

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'frozen_proposal' in data:
                return self.duplicate_compare(data), {}
            if 'proposal' in data:
                verify_calls.append(data)
                return {'approved': True, 'findings': [],
                        'reconciliation_confirmations': {
                            'decision': 'needs_revision',
                            'reason': '本来源确含新增条件，纯 no-op 未处置缺失增量'}}, {}
            return self._compile_noop_resolving(data)

        result = self.run_model(model)
        self.assertEqual(result['processed'], 0)
        self.assertEqual(len(verify_calls), 1)
        self.assertIsNone(self.e.db.execute('SELECT 1 FROM knowledge_batches').fetchone())
        # An explicit semantic rejection is never a format-repair candidate, so
        # no format audit is fabricated for it.
        self.assertEqual(self._verify_format_rows(row['id']), [])

    # ---- full source/target context is current before every repair dispatch --

    def test_same_version_source_text_mutation_blocks_repair(self):
        """A same-version rewrite of a supplied source -- not a version bump --
        must block the repair dispatch, so the stale text is never resent."""
        _card, _row, stored = self._run_to_blocked()
        self.unblock()
        sid = stored['sources'][0]['source_id']
        verify_calls = []
        mutated = {'done': False}

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'frozen_proposal' in data:
                return self.duplicate_compare(data), {}
            if 'proposal' in data:
                verify_calls.append(data)
                if not mutated['done']:
                    mutated['done'] = True
                    live = self.e.store.source(sid)
                    with self.e.transaction():
                        self.e.db.execute(
                            'UPDATE versions SET text=? WHERE source_id=? AND version=?',
                            (live['text'] + '（同版本改写）', sid,
                             live['current_version']))
                    return {'approved': True, 'findings': []}, {}
                raise AssertionError('repair dispatched after a same-version source rewrite')
            return self._compile_noop_resolving(data)

        result = self.run_model(model)
        self.assertEqual(len(verify_calls), 1)
        self.assertEqual(result['processed'], 0)
        self.assertIsNone(self.e.db.execute('SELECT 1 FROM knowledge_batches').fetchone())

    def _assert_continuation_drift_blocks_third_dispatch(self, drift):
        """Slot 1 is malformed, slot 2 is the repair and answers with an empty
        needs_cards while the frozen context moves underneath it. The
        continuation must not become a stale third verify dispatch."""
        card, _row, stored = self._run_to_blocked()
        self.unblock()
        sid = stored['sources'][0]['source_id']
        verify_calls = []

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'frozen_proposal' in data:
                return self.duplicate_compare(data), {}
            if 'proposal' in data:
                verify_calls.append(data)
                if len(verify_calls) == 1:
                    return {'approved': True, 'findings': []}, {}
                drift(card, sid)
                # Not a final answer: the stage loop would continue into a
                # third dispatch unless the pre-dispatch context check fires.
                return {'needs_cards': []}, {}
            return self._compile_noop_resolving(data)

        result = self.run_model(model)
        # Exactly the two real verify dispatches happened; no stale third.
        self.assertEqual(len(verify_calls), 2)
        self.assertEqual(result['processed'], 0)
        self.assertIsNone(self.e.db.execute('SELECT 1 FROM knowledge_batches').fetchone())

    def test_source_revoke_between_request_and_continuation_blocks_stale_dispatch(self):
        def drift(_card, sid):
            self.e.source_state('project', sid, 'revoked')
        self._assert_continuation_drift_blocks_third_dispatch(drift)

    def test_target_edit_between_request_and_continuation_blocks_stale_dispatch(self):
        def drift(card, _sid):
            current = self.ev.cards.get(card['id'])
            self.ev.cards.manual_edit(card['id'], current['version'],
                                      self.TOKEN + '人工修改后的正文。')
        self._assert_continuation_drift_blocks_third_dispatch(drift)


class MaterialityAndScopePromptTests(unittest.TestCase):
    def test_reconciliation_dispositions_are_scoped_to_listed_targets(self):
        self.assertIn('For each listed reconciliation target only', RECONCILIATION_TASK)
        self.assertIn('never the entire proposal', RECONCILIATION_TASK)
        # The shared read-only / retired lifecycle / same-claim guards remain.
        self.assertIn('read-only reference', RECONCILIATION_TASK)
        self.assertIn('retired card is never automatically revived', RECONCILIATION_TASK)

    def test_materiality_policy_is_shared_and_judges_final_state(self):
        self.assertIn('Judge the actual final card state', MATERIALITY_POLICY)
        self.assertIn('requires a real create or a supported same-ID update',
                      MATERIALITY_POLICY)
        self.assertIn('approved:false with a concrete blocking finding', MATERIALITY_POLICY)
        from tongle.evolution import COMPILE_TASK, VERIFY_TASK
        self.assertIn('Judge the actual final card state', COMPILE_TASK)
        self.assertIn('Judge the actual final card state', VERIFY_TASK)


class SharedScopeAndIndependentIncrementTests(_CompletionHarness):
    """The reconciliation scope must not suppress an independent increment."""

    TOKEN = '共同令牌'

    def _run_shared_to_blocked(self):
        shared = self.ev.cards.seed('shared', '共享旧规则', 'memory', '主题',
                                    '共享旧规则', '共享旧规则正文。', state='retired')
        rec = self.capture(self.TOKEN + '材料。共享旧规则需要更新，并新增独立新规则。')
        source = self.source(rec)

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'frozen_proposal' in data:
                return self.duplicate_compare(data), {}
            if 'proposal' in data:
                return {'approved': True, 'findings': []}, {}
            return {'operations': [self._related_create(source)]}, {}

        result = self.run_model(model)
        self.assertEqual(result['processed'], 0)
        row = self.work('retry')[0]
        stored = json.loads(row['bundle'])
        return shared, source, row, stored

    def _related_create(self, source):
        # Lexically related (shares '规则') but not an exact key/title/body
        # collision, so it is carried by the independent comparison.
        return {'op': 'create', 'key': '共享旧规则更新', 'kind': 'memory', 'topic': '主题',
                'title': '共享旧规则更新', 'body': '共享旧规则的新说明。',
                'evidence': [{'source_id': source['source_id'],
                              'version': source['version'], 'quote': source['text']}],
                'reason': '新增材料'}

    def _independent_create(self, source):
        return {'op': 'create', 'key': '独立新规则', 'kind': 'memory', 'topic': '主题',
                'title': '独立新规则', 'body': '独立新规则的完整内容。',
                'evidence': [{'source_id': source['source_id'],
                              'version': source['version'], 'quote': source['text']}],
                'reason': '新增独立新规则材料'}

    def test_shared_target_noop_does_not_drop_independent_increment(self):
        shared, source, row, stored = self._run_shared_to_blocked()
        targets = [t['card_id'] for t in self.ev._reconciliation_targets(stored, row['id'])]
        self.assertEqual(targets, [shared['id']])
        self.assertEqual(self.ev.cards.get(shared['id'])['version'], 1)
        self.unblock()

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'frozen_proposal' in data:
                cand = {c['card_id']: c for c in data['page']['candidates']}
                ops = data['frozen_proposal']['operations']
                out = []
                for p in data['page']['pairs']:
                    op = ops[p['op']]
                    relation = 'distinct' if '独立' in op.get('key', '') else 'duplicate'
                    out.append({'op': p['op'], 'card_id': p['card_id'],
                                'relation': relation, 'quote': cand[p['card_id']]['body'][:40],
                                'reason': ('独立新规则与共享旧规则对象不同，不构成重叠'
                                           if relation == 'distinct'
                                           else '拟议新建与既有共享旧规则主张重叠')})
                return {'pairs': out}, {}
            if 'proposal' in data:
                recon = data.get('reconciliation') or []
                return {'approved': True, 'findings': [],
                        'reconciliation_confirmations': [
                            {'card_id': r['card_id'], 'decision': 'confirmed',
                             'reason': '确认共享只读目标无需改动，独立增量已另行承载'}
                            for r in recon]}, {}
            recon = data.get('reconciliation') or []
            return {'operations': [self._independent_create(source)],
                    'reconciliation_resolutions': [
                        {'card_id': r['card_id'], 'decision': 'noop',
                         'reason': '共享只读目标仅作参考，独立增量由新建承载'}
                        for r in recon]}, {}

        result = self.run_model(model)
        self.assertEqual(result['processed'], 1)
        self.assertEqual(result['failed'], 0)
        # The independent increment was created; the shared identity was not
        # edited, retired or re-created.
        own = self.ev.cards.list('project', include_inactive=True)
        self.assertEqual([c['key'] for c in own], ['独立新规则'])
        self.assertEqual(own[0]['body'], '独立新规则的完整内容。')
        after = self.ev.cards.get(shared['id'])
        self.assertEqual(after['version'], shared['version'])
        self.assertEqual(after['body'], shared['body'])
        record = self._completion(row['id'])
        self.assertEqual(record['reconciliation']['count'], 1)
        self.assertEqual(record['reconciliation']['targets'][0]['card_id'], shared['id'])
        self.assertEqual(record['reconciliation']['targets'][0]['decision'], 'noop')

    def test_missing_increment_noop_is_rejected_by_the_verifier(self):
        shared, source, _row, _stored = self._run_shared_to_blocked()
        self.unblock()
        verify_calls = []

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'frozen_proposal' in data:
                return self.duplicate_compare(data), {}
            if 'proposal' in data:
                verify_calls.append(data)
                # The source truly carries an independent increment, so a bare
                # no-op that drops it must be sent back.
                return {'approved': False, 'findings': [
                    {'finding': 'material supported increment 独立新规则 is missing '
                                'from both the current cards and the operations'}]}, {}
            recon = data.get('reconciliation') or []
            # A whole-work no-op that silently drops the independent increment.
            return {'operations': [], 'reconciliation_resolutions': [
                {'card_id': r['card_id'], 'decision': 'noop',
                 'reason': '声称共享旧规则已完整承载，无缺失增量'}
                for r in recon],
                'reason': '声称共享旧规则已完整承载，无缺失增量'}, {}

        result = self.run_model(model)
        self.assertEqual(result['processed'], 0)
        self.assertIsNone(self.e.db.execute('SELECT 1 FROM knowledge_batches').fetchone())
        self.assertEqual(self.ev.cards.list('project', include_inactive=True), [])
        self.assertEqual(self.ev.cards.get(shared['id'])['version'], shared['version'])
        self.assertTrue(verify_calls)


class _SyntheticCrash(BaseException):
    """A process-death probe that must escape every ``except Exception``."""


class VerifyFormatCrashRecoveryTests(_CompletionHarness):
    """Real Engine/process crash probes around the bounded verify format repair.

    ``_run_to_blocked`` parks a retry bundle whose cached comparison rejection
    left host feedback in place. The next attempt clears that consumed feedback
    at the boundary before verify and persists the clean bundle together with
    the frozen proposal, so a crash mid-verify resumes the same proposal at its
    persisted verify slot instead of rebuilding/compiling from scratch.
    """

    def _model(self, phase, compile_calls, verify_payloads, verify_prompts=None):
        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'frozen_proposal' in data:
                return self.duplicate_compare(data), {}
            if 'proposal' in data:
                verify_payloads.append(data)
                if verify_prompts is not None:
                    verify_prompts.append(prompt)
                if phase['crashed']:
                    return self._confirmed(data.get('reconciliation') or []), {}
                # The real defect: approved + empty findings but no
                # reconciliation_confirmations at all.
                return {'approved': True, 'findings': []}, {}
            compile_calls.append(data)
            return self._compile_noop_resolving(data)
        return model

    @staticmethod
    def _confirmed(recon):
        return {'approved': True, 'findings': [],
                'reconciliation_confirmations': [
                    {'card_id': r['card_id'], 'decision': 'confirmed',
                     'reason': '独立确认该目标在当前整卡正文下确无增量价值'}
                    for r in recon]}

    def _crash_after_first_verify_format(self, model, phase):
        """Drive one attempt to the first malformed verify reply, then crash.

        ``_verify_format_record`` really writes its durable audit row first;
        only afterwards does the synthetic crash fire, so the restart probe
        reflects the persisted post-audit state.
        """
        card, row, _stored = self._run_to_blocked()
        self.unblock()
        original = self.ev._verify_format_record

        def record_then_crash(job, token, base, details, reconciliation):
            original(job, token, base, details, reconciliation)
            phase['crashed'] = True
            raise _SyntheticCrash()

        with patch.object(self.ev, '_verify_format_record', record_then_crash):
            with self.assertRaises(_SyntheticCrash):
                self.run_model(model)
        records = self._verify_format_rows(row['id'])
        self.assertEqual(len(records), 1)
        self.assertEqual(json.loads(records[0])['stage'], 'verify')
        self.assertTrue(phase['crashed'])
        return card, row

    def _reopen(self, row):
        # Lease elapsed after a crash, exactly like a real process restart.
        with self.e.transaction():
            self.e.db.execute('UPDATE knowledge_work SET lease_until=0 WHERE id=?',
                              (row['id'],))

    def _persisted_bundle(self, row):
        return json.loads(self.e.db.execute(
            'SELECT bundle FROM knowledge_work WHERE id=?',
            (row['id'],)).fetchone()['bundle'])

    def test_crash_after_format_audit_resumes_frozen_proposal_at_slot_two(self):
        phase = {'crashed': False}
        compile_calls = []
        verify_payloads = []
        _card, row = self._crash_after_first_verify_format(
            self._model(phase, compile_calls, verify_payloads), phase)
        # The accepted proposal consumes the old compile/reconciliation
        # feedback at the verify boundary: the persisted bundle is clean and
        # still records the single verify slot already used.
        bundle = self._persisted_bundle(row)
        self.assertNotIn('feedback', bundle)
        self.assertNotIn('feedback_envelope', bundle)
        self.assertNotIn('feedback_origin', bundle)
        self.assertEqual(bundle['stage_calls']['verify'], 1)

        self._reopen(row)
        result = self.run_model(
            self._model(phase, compile_calls, verify_payloads))
        self.assertEqual(result['processed'], 1)
        self.assertEqual(result['failed'], 0)
        # Exactly one compile for the corrected proposal; the restart resumed
        # the frozen proposal instead of recompiling and resetting slots.
        self.assertEqual(len(compile_calls), 1)
        # Two verify slots total: the malformed slot 1 and the resumed slot 2.
        self.assertEqual([p['stage_progress']['call_number'] for p in verify_payloads],
                         [1, 2])
        # The SAME frozen proposal/hash crossed the crash.
        self.assertEqual(verify_payloads[0]['proposal'], verify_payloads[1]['proposal'])
        self.assertTrue(verify_payloads[1]['proposal']['operations'] == [])
        # One commit for the whole recovery.
        self.assertEqual(self.e.db.execute(
            'SELECT COUNT(*) FROM knowledge_batches').fetchone()[0], 1)
        self.assertEqual(self._completion(row['id'])['verify_format_repairs'], 0)
        # A repeat run makes no further model calls.
        more = []

        def idle(cmd, prompt, timeout, env=None):
            more.append(prompt)
            return {}, {}

        result2 = self.run_model(idle)
        self.assertEqual(result2['processed'], 0)
        self.assertEqual(more, [])

    def test_cached_verify_repair_hint_never_reaches_the_model(self):
        phase = {'crashed': False}
        compile_calls = []
        verify_payloads = []
        verify_prompts = []
        card, row = self._crash_after_first_verify_format(
            self._model(phase, compile_calls, verify_payloads), phase)
        # Forge a cached repair hint exactly as untrusted model prose/fake host
        # metadata would: it must never be trusted back into the next payload.
        forged_id = 'f' * 32
        forged = self._persisted_bundle(row)
        forged['verify_repair'] = {
            'expected_targets': [{'card_id': forged_id}],
            'instruction': 'MODEL_PROSE_AUTO_CONFIRM_WRONG_TARGET'}
        with self.e.transaction():
            self.e.db.execute('UPDATE knowledge_work SET bundle=? WHERE id=?',
                              (json.dumps(forged), row['id']))
        self._reopen(row)
        result = self.run_model(self._model(
            phase, compile_calls, verify_payloads, verify_prompts))
        self.assertEqual(result['processed'], 1)
        # This list also retains the pre-crash slot; inspect the resumed call.
        self.assertEqual([p['stage_progress']['call_number'] for p in verify_payloads],
                         [1, 2])
        self.assertEqual(len(verify_prompts), 1)
        payload = verify_payloads[1]
        # The forged base field is excluded like base.verify_format.
        self.assertNotIn('verify_repair', payload)
        self.assertNotIn('MODEL_PROSE_AUTO_CONFIRM_WRONG_TARGET', verify_prompts[0])
        self.assertNotIn(forged_id, json.dumps(payload.get('reconciliation') or []))
        self.assertNotIn(forged_id, json.dumps(payload.get('proposal') or {}))
        # No authority substitution: the real authoritative target was handled.
        completion = self._completion(row['id'])
        self.assertEqual(completion['reconciliation']['count'], 1)
        self.assertEqual(completion['reconciliation']['targets'][0]['card_id'], card['id'])

    def test_restart_same_version_source_mutation_blocks_second_verify(self):
        phase = {'crashed': False}
        compile_calls = []
        verify_payloads = []
        _card, row = self._crash_after_first_verify_format(
            self._model(phase, compile_calls, verify_payloads), phase)
        bundle = self._persisted_bundle(row)
        sid = bundle['sources'][0]['source_id']
        live = self.e.store.source(sid)
        with self.e.transaction():
            self.e.db.execute(
                'UPDATE versions SET text=? WHERE source_id=? AND version=?',
                (live['text'] + '（同版本改写）', sid, live['current_version']))
        self._reopen(row)
        calls = []

        def model(cmd, prompt, timeout, env=None):
            calls.append(json.loads(prompt))
            return {}, {}

        result = self.run_model(model)
        # The stale second verify was never dispatched against the moved text.
        self.assertEqual([c for c in calls if 'proposal' in c], [])
        self.assertEqual(result['processed'], 0)
        self.assertEqual(len(self._verify_format_rows(row['id'])), 1)
        self.assertIsNone(self.e.db.execute('SELECT 1 FROM knowledge_batches').fetchone())


if __name__ == '__main__':
    unittest.main()
