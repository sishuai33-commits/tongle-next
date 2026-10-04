"""False-overlap reconciliation: shared general context is not ``extends``.

These exercise the real ``Evolution.process`` / ``_compare_stage`` /
``_reconciliation_targets`` / ``_stage_loop`` / ``_resolution_problems`` /
``_confirmation_problems`` paths with synthetic fixtures only: no live vault,
model, credentials or network is touched. Every model reply is a synthetic
stub, so these prove machine behaviour, not semantic quality.

The corrected comparator/reconciliation contract:

* A shared topic, vendor, background or general principle is NOT enough for
  ``extends``; the SAME concrete assertion subject/object and its overlapping
  applicability must be identified. A genuinely different tool/object or an
  independent project event is ``distinct``.
* A target-level no-op may legitimately discharge an earlier *false* suspected
  overlap by naming the concrete assertion/object/applicability distinction;
  it never permits dropping a real new increment, a new create must still pass
  the normal duplicate guards and the mandatory comparison for its final
  frozen proposal, and a genuine same-claim increment still requires a
  supported same-ID disposition or ``unresolved``.

Covered here (synthetic names only):

(A) shared Atlas config-priority general rule vs an independent Boreal adapter
    failure: a first false ``extends`` leaves a genuine persisted duty; the
    next attempt states the actual different object, the shared target is an
    explicit no-op, and the new create earns a NEW comparison ``distinct`` plus
    an independent verify ``confirmed`` -- the source event is represented and
    the shared card identity/version/body are unchanged.
(B) shared generic supplier-role vs independent project metrics: a bare whole
    work no-op cannot drop the real increment (the verifier sends it back);
    the increment/new card is retained once the create passes comparison.
(C) the SAME Orion project/phase advancing from implementation complete +
    acceptance pending to acceptance passed is a genuine ``extends`` and must
    be a same-ID update + confirmed; a separate renaming/date-based create
    keeps the blocking comparison and is never published.
"""
import json
import unittest

# Sibling test modules are imported by bare name under ``unittest discover -s
# tests`` (the tests dir is the top level) and as a package under pytest / an
# explicit ``tests.test_...`` import. Try both so either invocation works.
try:
    from test_active_noop_contract import _ActiveNoopHarness
except ImportError:  # pragma: no cover - package-import invocation
    from tests.test_active_noop_contract import _ActiveNoopHarness


class _FalseOverlapHarness(_ActiveNoopHarness):
    """Active-noop scaffolding plus read-only shared-card seeding."""

    def seed_shared(self, key, body, **kw):
        return self.ev.cards.seed('shared', key, 'memory', kw.pop('topic', '主题'),
                                  key, body, **kw)

    def make_create(self, source, key, title, body, reason='新增独立事件材料'):
        return {'op': 'create', 'key': key, 'kind': 'memory', 'topic': '主题',
                'title': title, 'body': body,
                'evidence': [{'source_id': source['source_id'],
                              'version': source['version'], 'quote': source['text']}],
                'reason': reason}

    def refit(self, row):
        self.unblock()
        return row


class FalseOverlapDistinctTests(_FalseOverlapHarness):
    def test_false_extends_then_truthful_noop_and_new_create(self):
        shared = self.seed_shared(
            'atlas-config-priority-rule',
            'Atlas config priority general rule: when config sources conflict, '
            'an explicit override wins.')
        rec = self.capture(
            'Boreal adapter failed while loading config; an independent tool event.')
        source = self.source(rec)
        create_body = 'Boreal adapter failed while loading config; independent tool event.'
        reason_first = 'shared config tool maintenance context'
        reason_second = ('actual different object: Boreal adapter failure is '
                         'independent of the Atlas rule')

        compare_relations = []
        verify_targets = []

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'frozen_proposal' in data:
                ops = data['frozen_proposal']['operations']
                cand = {c['card_id']: c for c in data['page']['candidates']}
                out = []
                for p in data['page']['pairs']:
                    op = ops[p['op']]
                    relation = ('distinct'
                                if 'actual different object' in (op.get('reason') or '')
                                else 'extends')
                    compare_relations.append(relation)
                    out.append({'op': p['op'], 'card_id': p['card_id'],
                                'relation': relation,
                                'quote': cand[p['card_id']]['body'][:60],
                                'reason': '共享主题不足以构成 extends，须比较具体对象与适用范围'})
                return {'pairs': out}, {}
            if 'proposal' in data:
                recon = data.get('reconciliation') or []
                verify_targets.append(recon)
                return {'approved': True, 'findings': [],
                        'reconciliation_confirmations': [
                            {'card_id': r['card_id'], 'decision': 'confirmed',
                             'reason': '确认该目标与本次独立事件对象不同，无需改动内容或状态'}
                            for r in recon]}, {}
            recon = data.get('reconciliation')
            if recon:
                alias = recon[0]['card_id']
                return {'operations': [self.make_create(
                            source, 'boreal-adapter-failure', 'Boreal adapter failure',
                            create_body, reason=reason_second)],
                        'reconciliation_resolutions': [
                            {'card_id': alias, 'decision': 'noop',
                             'reason': '早前疑似重叠为假：Atlas 通用规则与 Boreal 适配器故障对象不同'}],
                        'reason': '本来源为独立事件，共享目标无需改动'}, {}
            return {'operations': [self.make_create(
                        source, 'boreal-adapter-failure', 'Boreal adapter failure',
                        create_body, reason=reason_first)]}, {}

        first = self.run_model(model)
        self.assertEqual(first['processed'], 0)
        self.assertEqual(compare_relations, ['extends'])
        rows = self.work('retry')
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertIsNone(self.e.db.execute('SELECT 1 FROM knowledge_batches').fetchone())
        # The false extends left a genuine, persisted duty for the shared card.
        stored = json.loads(row['bundle'])
        self.assertEqual([t['card_id'] for t in self.ev._reconciliation_targets(stored)],
                         [shared['id']])
        self.assertEqual(self.ev.cards.get(shared['id'])['version'], shared['version'])

        self.refit(row)
        second = self.run_model(model)
        self.assertEqual(second['processed'], 1)
        self.assertEqual(second['failed'], 0)
        self.assertIn('distinct', compare_relations)

        # The independent verifier saw the structured shared target.
        self.assertTrue(any(v for v in verify_targets if v))
        # The final source event is represented by exactly one new own card.
        own = self.ev.cards.list('project', include_inactive=True)
        self.assertEqual([c['key'] for c in own], ['boreal-adapter-failure'])
        self.assertEqual(own[0]['body'], create_body)
        # The shared card identity, version and body are unchanged.
        after_shared = self.ev.cards.get(shared['id'])
        self.assertEqual(after_shared['version'], shared['version'])
        self.assertEqual(after_shared['body'], shared['body'])
        self.assertEqual(after_shared['key'], 'atlas-config-priority-rule')

        record = self.completion_record(row['id'])
        self.assertEqual(record['reconciliation']['count'], 1)
        self.assertEqual(record['reconciliation']['knowledge_changes'], 1)
        self.assertEqual(record['reconciliation']['targets'][0]['card_id'], shared['id'])
        self.assertEqual(record['reconciliation']['targets'][0]['decision'], 'noop')
        self.assertEqual(record['reconciliation']['targets'][0]['confirmed'], 'confirmed')


class MissingIncrementRejectionTests(_FalseOverlapHarness):
    def test_bare_whole_work_noop_cannot_drop_the_increment(self):
        shared = self.seed_shared(
            'generic-supplier-role',
            'Generic supplier role: a read-only reference describing a supplier role.')
        rec = self.capture(
            'Aurora independent project supplier metrics: throughput and latency.')
        source = self.source(rec)
        create_body = 'Aurora independent project supplier metrics: throughput and latency.'
        reason_first = 'shared supplier role background'
        reason_second = ('actual increment retained: Aurora project metrics are an '
                         'independent project event')

        compile_calls = []
        verify_decisions = []

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'frozen_proposal' in data:
                ops = data['frozen_proposal']['operations']
                cand = {c['card_id']: c for c in data['page']['candidates']}
                out = []
                for p in data['page']['pairs']:
                    op = ops[p['op']]
                    relation = ('distinct'
                                if 'actual increment retained' in (op.get('reason') or '')
                                else 'extends')
                    out.append({'op': p['op'], 'card_id': p['card_id'],
                                'relation': relation,
                                'quote': cand[p['card_id']]['body'][:60],
                                'reason': '共享供应商角色不足以覆盖独立项目的具体指标主张'})
                return {'pairs': out}, {}
            if 'proposal' in data:
                recon = data.get('reconciliation') or []
                decision = 'needs_revision' if not verify_decisions else 'confirmed'
                verify_decisions.append(decision)
                return {'approved': True, 'findings': [],
                        'reconciliation_confirmations': [
                            {'card_id': r['card_id'], 'decision': decision,
                             'reason': ('本来源确含独立新增指标，纯 no-op 未处置缺失增量'
                                        if decision == 'needs_revision'
                                        else '确认独立新增指标已通过比较并保留')}
                            for r in recon]}, {}
            compile_calls.append(1)
            recon = data.get('reconciliation')
            if recon:
                alias = recon[0]['card_id']
                if len(compile_calls) == 2:
                    # A bare whole-work no-op that silently drops the increment.
                    return {'operations': [],
                            'reconciliation_resolutions': [
                                {'card_id': alias, 'decision': 'noop',
                                 'reason': '声称共享供应商卡已完整承载，无缺失增量'}],
                            'reason': '声称共享供应商卡已完整承载，无缺失增量'}, {}
                return {'operations': [self.make_create(
                            source, 'aurora-project-metrics', 'Aurora project metrics',
                            create_body, reason=reason_second)],
                        'reconciliation_resolutions': [
                            {'card_id': alias, 'decision': 'noop',
                             'reason': '共享供应商角色与独立项目指标对象不同，目标无需改动'}],
                        'reason': '独立项目指标为新增增量，比较已判 distinct'}, {}
            return {'operations': [self.make_create(
                        source, 'aurora-project-metrics', 'Aurora project metrics',
                        create_body, reason=reason_first)]}, {}

        first = self.run_model(model)
        self.assertEqual(first['processed'], 0)
        rows = self.work('retry')
        self.assertEqual(len(rows), 1)
        row = rows[0]

        # Attempt 2: the compile returns a bare no-op; the independent verifier
        # rejects the missing increment, so nothing is published.
        self.unblock()
        second = self.run_model(model)
        self.assertEqual(second['processed'], 0)
        self.assertEqual(second['failed'], 1)
        self.assertIn('needs_revision', verify_decisions)
        self.assertIsNone(self.e.db.execute('SELECT 1 FROM knowledge_batches').fetchone())
        self.assertEqual(self.ev.cards.list('project', include_inactive=True), [])

        # Attempt 3: the increment is retained as a create that passes the
        # mandatory comparison and the independent confirmation.
        self.unblock()
        third = self.run_model(model)
        self.assertEqual(third['processed'], 1)
        self.assertEqual(third['failed'], 0)
        own = self.ev.cards.list('project', include_inactive=True)
        self.assertEqual([c['key'] for c in own], ['aurora-project-metrics'])
        self.assertEqual(own[0]['body'], create_body)
        after_shared = self.ev.cards.get(shared['id'])
        self.assertEqual(after_shared['version'], shared['version'])
        self.assertEqual(after_shared['body'], shared['body'])
        record = self.completion_record(row['id'])
        self.assertEqual(record['reconciliation']['knowledge_changes'], 1)


class SameProjectPhaseIncrementTests(_FalseOverlapHarness):
    def test_same_phase_advance_updates_same_identity(self):
        orion = self.seed(
            'orion-phase-acceptance',
            'Orion project phase: implementation complete, acceptance pending.')
        rec = self.capture('Orion project phase: acceptance passed.')
        source = self.source(rec)
        renamed_body = 'Orion project phase 2026-10-01: acceptance passed (new card).'
        updated_body = 'Orion project phase: implementation complete, acceptance passed.'

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'frozen_proposal' in data:
                cand = {c['card_id']: c for c in data['page']['candidates']}
                out = []
                for p in data['page']['pairs']:
                    out.append({'op': p['op'], 'card_id': p['card_id'],
                                'relation': 'extends',
                                'quote': cand[p['card_id']]['body'][:60],
                                'reason': '同一 Orion 项目阶段的推进是既有持续主张的真实增量'})
                return {'pairs': out}, {}
            if 'proposal' in data:
                recon = data.get('reconciliation') or []
                return {'approved': True, 'findings': [],
                        'reconciliation_confirmations': [
                            {'card_id': r['card_id'], 'decision': 'confirmed',
                             'reason': '确认同一既有身份已被更新以纳入本来源的阶段推进'}
                            for r in recon]}, {}
            recon = data.get('reconciliation')
            if recon:
                alias = recon[0]['card_id']
                return {'operations': [
                            {'op': 'update', 'id': alias,
                             'expected_version': orion['version'], 'body': updated_body,
                             'evidence': [{'source_id': source['source_id'],
                                           'version': source['version'],
                                           'quote': source['text']}],
                             'reason': '同一 Orion 阶段推进复用既有身份，不新建身份'}],
                        'reconciliation_resolutions': [
                            {'card_id': alias, 'decision': 'update',
                             'reason': '同一项目阶段的增量由同一 card_id 更新承载'}]}, {}
            return {'operations': [self.make_create(
                        source, 'orion-2026-10-01-acceptance',
                        'Orion 2026-10-01 acceptance passed',
                        renamed_body, reason='仅按改名与日期另建一张新卡')]}, {}

        first = self.run_model(model)
        self.assertEqual(first['processed'], 0)
        self.assertIsNone(self.e.db.execute('SELECT 1 FROM knowledge_batches').fetchone())
        self.assertEqual(self.ev.cards.get(orion['id'])['version'], orion['version'])

        self.unblock()
        second = self.run_model(model)
        self.assertEqual(second['processed'], 1)
        self.assertEqual(second['failed'], 0)
        after = self.ev.cards.get(orion['id'])
        self.assertEqual(after['version'], orion['version'] + 1)
        self.assertEqual(after['body'], updated_body)
        own = self.ev.cards.list('project', include_inactive=True)
        self.assertEqual([c['id'] for c in own], [orion['id']])

    def test_renaming_or_date_create_stays_blocked(self):
        orion = self.seed(
            'orion-phase-acceptance',
            'Orion project phase: implementation complete, acceptance pending.')
        rec = self.capture('Orion project phase: acceptance passed.')
        source = self.source(rec)
        renamed_body = 'Orion project phase 2026-10-01: acceptance passed (new card).'

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'frozen_proposal' in data:
                cand = {c['card_id']: c for c in data['page']['candidates']}
                out = []
                for p in data['page']['pairs']:
                    out.append({'op': p['op'], 'card_id': p['card_id'],
                                'relation': 'extends',
                                'quote': cand[p['card_id']]['body'][:60],
                                'reason': '改名与日期不足以区分同一 Orion 阶段的持续主张'})
                return {'pairs': out}, {}
            if 'proposal' in data:                       # never reached on failure
                return {'approved': True, 'findings': []}, {}
            recon = data.get('reconciliation')
            if recon:
                alias = recon[0]['card_id']
                return {'operations': [self.make_create(
                            source, 'orion-2026-10-01-acceptance',
                            'Orion 2026-10-01 acceptance passed',
                            renamed_body, reason='仅按改名与日期另建一张新卡')],
                        'reconciliation_resolutions': [
                            {'card_id': alias, 'decision': 'noop',
                             'reason': '声称改名与日期已构成新的独立主张，无需改动旧卡'}],
                        'reason': '改名与日期即新主张'}, {}
            return {'operations': [self.make_create(
                        source, 'orion-2026-10-01-acceptance',
                        'Orion 2026-10-01 acceptance passed',
                        renamed_body, reason='仅按改名与日期另建一张新卡')]}, {}

        # Three bounded attempts, each blocked by the genuine extends.
        first = self.run_model(model)
        self.assertEqual(first['processed'], 0)
        self.unblock()
        second = self.run_model(model)
        self.assertEqual(second['processed'], 0)
        self.unblock()
        third = self.run_model(model)
        self.assertEqual(third['processed'], 0)
        self.assertEqual(third['failed'], 1)

        self.assertIsNone(self.e.db.execute('SELECT 1 FROM knowledge_batches').fetchone())
        self.assertTrue(self.work('failed'))
        own = self.ev.cards.list('project', include_inactive=True)
        self.assertEqual([c['id'] for c in own], [orion['id']])
        after = self.ev.cards.get(orion['id'])
        self.assertEqual(after['version'], orion['version'])
        self.assertEqual(after['body'], orion['body'])


if __name__ == '__main__':
    unittest.main()
