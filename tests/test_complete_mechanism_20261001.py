"""Focused regressions for the 2026-10-01 complete consumer/accounting repair.

Synthetic fixtures only: no live vault, model, credentials or network. Where a
model stub is used it proves machine behavior, not semantic quality.
"""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tongle.engine import Engine
from tongle.evolution import Evolution, canonical
from tongle.knowledge_files import KnowledgeFiles
from tongle.context_delivery import utf16_units
from tongle.context_retrieval import (is_acknowledgement, specific_terms)


class _Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.e = Engine(self.root / 'state')
        self.e.init(self.root / 'vault'); self.addCleanup(self.e.store.close)
        (self.e.home / 'config.json').write_text(json.dumps(
            {'knowledge_mode': 'evolving', 'model_command': ['stub'],
             'knowledge_quiet_seconds': 0}))
        self.ev = Evolution(self.e)

    def cfg(self, **overrides):
        base = {'knowledge_mode': 'evolving', 'model_command': ['stub'],
                'knowledge_quiet_seconds': 0}
        base.update(overrides)
        (self.e.home / 'config.json').write_text(json.dumps(base))

    def seed(self, project, key, kind, topic, title, body, **kw):
        return self.ev.cards.seed(project, key, kind, topic, title, body, **kw)

    def publish(self):
        return KnowledgeFiles(self.e, self.ev.cards).publish()

    def capture(self, text, event='e', role='user', session='session', receipt_id=None):
        rec = self.e.store.capture('project', session, role, event, text)
        self.ev.captured(rec['source_id'], role, session, receipt_id=receipt_id)
        return rec

    def run_model(self, model, force=True):
        with patch('tongle.worker.invoke_json', side_effect=model):
            return self.ev.process(force=force)

    def work(self, state=None):
        sql = 'SELECT * FROM knowledge_work'
        args = ()
        if state:
            sql += ' WHERE state=?'; args = (state,)
        return [dict(r) for r in self.e.db.execute(sql, args).fetchall()]

    def offered(self, receipt_id):
        row = self.e.db.execute('SELECT offered FROM knowledge_consumption WHERE id=?',
                                (receipt_id,)).fetchone()
        return json.loads(row['offered'])


class ConsumerGatingTests(_Harness):
    def test_zero_overlap_memory_cannot_crowd_out_specific_knowledge(self):
        rel = self.seed('project', '冷链密封', 'knowledge', '物流',
                        '冷链密封许可', '冷链密封许可的有效条件。')
        noise = [self.seed('project', '噪声%d' % i, 'memory', '无关主题',
                           '噪声%d' % i, '空气温度改变。') for i in range(8)]
        self.publish()
        got = self.ev.context('project', '冷链密封许可')
        ids = [r['id'] for r in got['results']]
        self.assertIn(rel['id'], ids)
        self.assertFalse(any(n['id'] in ids for n in noise))
        # The relevant card is delivered whole and bound to the receipt.
        result = next(r for r in got['results'] if r['id'] == rel['id'])
        self.assertTrue(result['complete'])
        self.assertIn(rel['body'], got['rendered_context'])

    def test_generic_word_does_not_summon_unrelated_shared_cards(self):
        own = self.seed('project', '本项目方案', 'memory', '方案',
                        '本项目方案', '本项目方案内容。')
        shared = [self.seed('shared', '%s安排' % n, 'knowledge', n,
                            '%s安排' % n, '出差旅行需要方案。')
                  for n in ('差旅', '招聘', '部署')]
        self.publish()
        got = self.ev.context('project', '方案')
        ids = [r['id'] for r in got['results']]
        self.assertIn(own['id'], ids)
        for card in shared:
            self.assertNotIn(card['id'], ids)

    def test_acknowledgements_yield_no_cards_and_no_receipt(self):
        self.seed('project', '状态卡', 'memory', '方案', '当前状态', '旧状态内容。')
        self.publish()
        for query in ('好', '好的', '谢谢', '收到', '好的，谢谢'):
            self.assertTrue(is_acknowledgement(query), query)
            got = self.ev.context('project', query)
            self.assertEqual(got['results'], [], query)
            self.assertIsNone(got['receipt_id'], query)
            self.assertEqual(got['rendered_context'], '', query)
        self.assertIsNone(self.e.db.execute(
            'SELECT 1 FROM knowledge_consumption').fetchone())

    def test_exact_title_long_card_is_delivered_as_honest_locator(self):
        card = self.seed('project', '长卡', 'memory', '大主题', '唯一的精确标题',
                         '正' * 12000)
        self.publish()
        got = self.ev.context('project', '唯一的精确标题')
        result = next(r for r in got['results'] if r['id'] == card['id'])
        self.assertFalse(result['complete'], 'an oversize exact match must not vanish')
        self.assertTrue(result['path'])
        self.assertLessEqual(utf16_units(got['rendered_context']), 6000)
        # The full body was not smuggled in truncated.
        self.assertNotIn('正' * 12000, got['rendered_context'])
        offered = {o['id']: o for o in self.offered(got['receipt_id'])}
        self.assertEqual(offered[card['id']]['delivery'], 'locator')
        self.assertEqual(offered[card['id']]['body'], '')


class DeliveryTruthTests(_Harness):
    def test_rendered_context_is_whole_block_bounded_and_receipt_matches(self):
        for i in range(6):
            self.seed('project', '卡%d' % i, 'knowledge', '主题', '卡%d' % i,
                      '内容' * 900)   # 1800 chars each
        self.publish()
        got = self.ev.context('project', '主题')
        rendered = got['rendered_context']
        self.assertLessEqual(utf16_units(rendered), 6000)
        self.assertNotIn('truncated', rendered)
        # Every delivered full body appears whole; nothing is sliced.
        for r in got['results']:
            if r['complete']:
                self.assertIn(r['text'], rendered)
        offered = self.offered(got['receipt_id'])
        self.assertEqual({o['id']: o['delivery'] for o in offered},
                         {r['id']: ('full' if r['complete'] else 'locator')
                          for r in got['results']})
        for o in offered:
            if o['delivery'] == 'full':
                self.assertTrue(o['body'])
            else:
                self.assertEqual(o['body'], '')
        # The core payload, not a re-render, is what a consumer uses.
        self.assertEqual({r['id']: r['source_version'] for r in got['results']},
                         {o['id']: o['version'] for o in offered})

    def test_notice_is_accounted_before_cards_within_budget(self):
        for i in range(6):
            self.seed('project', '卡%d' % i, 'knowledge', '主题', '卡%d' % i,
                      '内容' * 900)
        self.publish()
        self.capture('更正：' + '把交付物改为三张方案图，' * 40, session='s1')
        got = self.ev.context('project', '本次交付')
        rendered = got['rendered_context']
        self.assertTrue(got['notices'])
        self.assertIn('尚未', rendered)
        self.assertLessEqual(utf16_units(rendered), 6000)
        self.assertNotIn('truncated', rendered)

    def test_oversize_delivery_does_not_raise_or_truncate(self):
        # A very large final block must degrade to locator/omission, never an
        # exception mid-render.
        self.seed('project', '大卡', 'knowledge', '主题', '大卡', '甲' * 20000)
        self.publish()
        got = self.ev.context('project', '主题')
        self.assertLessEqual(utf16_units(got['rendered_context']), 6000)
        self.assertNotIn('truncated', got['rendered_context'])


class ReferentialFollowupTests(_Harness):
    def test_same_session_anchor_avoids_office_workbook_noise(self):
        self.seed('project', '助手设计', 'memory', '助手', '知识助手设计',
                  '关于知识助手的推送设计：每轮推送卡片还是开头才有。')
        office = self.seed('project', '办公', 'knowledge', '办公', '办公工作簿',
                           '办公工作簿的效果和交互说明。')
        self.publish()
        self.capture('我们在讨论知识助手的推送策略。', session='s1', event='p1')
        got = self.ev.context('project',
                              '你是每轮推送卡片还是开头才有？效果变好了么',
                              session='s1')
        ids = [r['id'] for r in got['results']]
        self.assertIn('助手设计', [self.ev.cards.get(i)['key'] for i in ids])
        self.assertNotIn(office['id'], ids)

    def test_explicit_new_topic_wins_over_session_anchor(self):
        self.capture('我们在讨论知识助手。', session='s1', event='p1')
        assistant = self.seed('project', '助手卡', 'memory', '助手', '知识助手',
                              '知识助手内容。')
        cold = self.seed('project', '冷链卡', 'knowledge', '物流', '冷链密封',
                         '冷链密封许可。')
        self.publish()
        got = self.ev.context('project', '冷链密封许可', session='s1')
        ids = [r['id'] for r in got['results']]
        self.assertIn(cold['id'], ids)
        self.assertNotIn(assistant['id'], ids)

    def test_explicit_continuation_recovers_small_project_status_set(self):
        a = self.seed('project', '状态A', 'memory', '方案', '当前进展', '先完成方案。')
        for i in range(8):
            self.seed('project', '旧卡%d' % i, 'knowledge', '主题', '旧卡%d' % i,
                      '很久以前的无关内容。')
        self.publish()
        got = self.ev.context('project', '继续', session='s2')
        ids = [r['id'] for r in got['results']]
        self.assertIn(a['id'], ids)
        self.assertLessEqual(len(ids), 4, 'continuation recovers a small status set')
        self.assertIsNotNone(got['receipt_id'])

    def test_no_cross_session_carry_and_no_self_anchor(self):
        rec = self.capture('项目甲的关键决定。', session='s1', event='p1')
        self.assertEqual(self.ev._session_anchor_turns('project', 's2', '继续'), [])
        turns = self.ev._session_anchor_turns('project', 's1', '继续')
        self.assertIn(rec['source_id'], [t[2] for t in turns])
        # The current turn (already captured) must never anchor on itself.
        cur = self.capture('继续冷链许可核对。', session='s1', event='p2')
        turns = self.ev._session_anchor_turns('project', 's1', '继续冷链许可核对。')
        self.assertNotIn(cur['source_id'], [t[2] for t in turns])


class RawStatementGateTests(_Harness):
    def test_finance_query_does_not_pull_airport_statement_for_ready(self):
        self.capture('明天去机场接人，准备车辆。', session='s0')
        got = self.ev.context('project', '准备财务数据', session='new')
        self.assertEqual(got['recent_statements'], [])

    def test_correction_does_not_suppress_unrelated_memory(self):
        payroll = self.seed('project', '薪资状态', 'memory', '人事', '薪资发放',
                            '本轮薪资已发放至三月。')
        dinner = self.seed('project', '聚餐安排', 'memory', '活动', '今晚聚餐',
                           '今晚团队聚餐安排在七点。')
        self.publish()
        self.capture('取消今晚聚餐。', session='s1')
        got = self.ev.context('project', '薪资状态')
        ids = [r['id'] for r in got['results']]
        self.assertIn(payroll['id'], ids)
        self.assertNotIn(dinner['id'], ids)
        self.assertTrue(got['notices'])
        self.assertIn('聚餐', got['notices'][0])

    def test_unknown_impact_correction_does_not_blank_all_memory(self):
        a = self.seed('project', '薪资', 'memory', '人事', '薪资', '薪资正常。')
        b = self.seed('project', '部署', 'memory', '运维', '部署', '部署正常。')
        self.publish()
        self.capture('更正。', session='s1')
        got = self.ev.context('project', '薪资', session='s2')
        ids = [r['id'] for r in got['results']]
        self.assertIn(a['id'], ids)
        self.assertTrue(got['notices'])
        # The unrelated same-project memory is never withheld.
        self.assertIn(b['id'], [c['id'] for c in self.ev.cards.list('project')])

    def test_weak_followup_keeps_correction_notice(self):
        self.capture('取消今晚聚餐。', session='s1')
        got = self.ev.context('project', '怎么样了', session='s2')
        self.assertTrue(got['notices'])
        self.assertIn('聚餐', got['notices'][0])


class OperationValidationTests(_Harness):
    def test_invalid_evidence_version_prevents_any_verify_call(self):
        rec = self.capture('需要写一条新记录。')
        calls = {'compile': 0, 'verify': 0}
        payloads = []

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'proposal' in data:
                calls['verify'] += 1
                return {'approved': True, 'findings': []}, {}
            calls['compile'] += 1
            payloads.append(data)
            s = data['sources'][-1]
            return {'operations': [{'op': 'create', 'key': '新记录', 'kind': 'memory',
                    'topic': '方案', 'title': '新记录', 'body': '新记录正文。',
                    'evidence': [{'source_id': s['source_id'], 'version': '1',
                                  'quote': s['text']}],
                    'reason': '用户明确要求'}]}, {}

        result = self.run_model(model)
        self.assertEqual(calls['verify'], 0, 'independent review must never be spent')
        self.assertEqual(calls['compile'], 3)
        self.assertEqual(result['failed'], 1)
        self.assertEqual(self.work('failed')[0]['state'], 'failed')
        self.assertIn('stage call limit', self.work('failed')[0]['error'])
        # The model was told the exact field and the allowed schema.
        retry = payloads[-1].get('retry_findings') or []
        blob = json.dumps(retry, ensure_ascii=False)
        self.assertIn('evidence.version', blob)
        self.assertIn('operation schema', blob)

    def test_validate_operations_reports_per_op_field_without_side_effects(self):
        card = self.seed('project', '目标卡', 'memory', '方案', '目标卡', '正文。')
        rec = self.capture('来源材料。')
        text = self.e.store.source(rec['source_id'])['text']
        before = self.ev.cards.get(card['id'])['version']
        report = self.ev.cards.validate_operations([{
            'op': 'update', 'id': card['id'], 'expected_version': True,
            'body': '改动正文。',
            'evidence': [{'source_id': rec['source_id'], 'version': 1, 'quote': text}],
            'reason': '更正'}], 'project')
        self.assertFalse(report['ok'])
        self.assertEqual(report['problems'][0]['field'], 'expected_version')
        # Read-only: the card version is untouched.
        self.assertEqual(self.ev.cards.get(card['id'])['version'], before)


class BlockedAccountingTests(_Harness):
    def _queued_documents(self):
        a = self.root / 'A.md'; a.write_text('甲来源内容。')
        b = self.root / 'B.md'; b.write_text('乙来源内容。')
        sa = self.e.store.ingest('project', str(a))
        sb = self.e.store.ingest('project', str(b))
        self.ev.queue(force=True)
        job = dict(self.e.db.execute(
            "SELECT * FROM knowledge_work WHERE state='pending'").fetchone())
        return a, b, sa, sb, job

    def test_blocked_before_dispatch_refunds_exactly_one_attempt(self):
        a, b, sa, sb, job = self._queued_documents()
        self.e.source_state('project', sb['source_id'], 'missing')
        with patch('tongle.worker.invoke_json') as model:
            self.ev.process(force=True)
        model.assert_not_called()
        row = dict(self.e.db.execute(
            'SELECT state,attempts,error FROM knowledge_work WHERE id=?', (job['id'],)).fetchone())
        self.assertEqual(row['state'], 'blocked')
        self.assertIn('source missing', row['error'])
        self.assertEqual(row['attempts'], 0, 'pre-dispatch block must refund the attempt')
        self.assertIsNotNone(self.e.db.execute(
            "SELECT 1 FROM knowledge_failures WHERE work_id=? AND state='attempt_refunded'",
            (job['id'],)).fetchone())
        self.assertEqual(self.e.db.execute(
            'SELECT COUNT(*) FROM knowledge_calls').fetchone()[0], 0)

    def test_provider_failure_that_dispatched_is_not_refunded(self):
        self.capture('先完成方案。')
        def boom(cmd, prompt, timeout, env=None):
            raise RuntimeError('temporary unavailable')
        with patch('tongle.worker.invoke_json', side_effect=boom):
            self.ev.process(force=True)
        row = self.work()[0]
        self.assertEqual(row['state'], 'retry')
        self.assertEqual(row['attempts'], 1)
        self.assertIsNone(self.e.db.execute(
            "SELECT 1 FROM knowledge_failures WHERE work_id=? AND state='attempt_refunded'",
            (row['id'],)).fetchone())

    def test_blocked_recovery_does_not_reset_exhausted_attempts(self):
        a, b, sa, sb, job = self._queued_documents()
        self.e.db.execute(
            "UPDATE knowledge_work SET state='blocked',attempts=5,error='source unavailable'"
            " WHERE id=?", (job['id'],))
        self.e.db.commit()
        self.ev.queue(force=True)
        row = dict(self.e.db.execute(
            'SELECT state,error FROM knowledge_work WHERE id=?', (job['id'],)).fetchone())
        self.assertEqual(row['state'], 'failed')
        self.assertIn('retry limit reached', row['error'])

    def test_revoked_source_blocks_with_distinct_status_and_stays(self):
        a, b, sa, sb, job = self._queued_documents()
        self.e.source_state('project', sb['source_id'], 'revoked')
        with patch('tongle.worker.invoke_json') as model:
            self.ev.process(force=True)
        model.assert_not_called()
        row = dict(self.e.db.execute(
            'SELECT state,error FROM knowledge_work WHERE id=?', (job['id'],)).fetchone())
        self.assertEqual(row['state'], 'blocked')
        self.assertIn('source revoked', row['error'])
        # A revoked source never auto-resurrects into pending.
        self.ev.queue(force=True)
        again = dict(self.e.db.execute(
            'SELECT state FROM knowledge_work WHERE id=?', (job['id'],)).fetchone())
        self.assertEqual(again['state'], 'blocked')


class LegacyCoverageTests(_Harness):
    def _coverage_row(self, sid, version):
        source = self.e.store.source(sid)
        parts = list(self.ev._source_parts(source))
        for part in parts:
            part['version'] = version
        with self.e.transaction():
            self.e.db.execute(
                "INSERT INTO knowledge_work(id,project,session,inputs,state,created_at)"
                " VALUES('cov','project','documents',?,'coverage_wait',?)",
                (json.dumps(parts), time.time()))

    def test_superseded_coverage_routes_to_successor_not_silent_skip(self):
        p = self.root / 'A.md'; p.write_text('甲来源内容。')
        sa = self.e.store.ingest('project', str(p))
        self._coverage_row(sa['source_id'], 1)
        p.write_text('甲来源新版本。'); self.e.store.ingest('project', str(p))
        result = self.ev.recover_legacy_coverage()
        self.assertFalse(result['already_done'])
        row = dict(self.e.db.execute(
            "SELECT state FROM knowledge_work WHERE id='cov'").fetchone())
        self.assertEqual(row['state'], 'obsolete')
        self.assertIsNotNone(self.e.db.execute(
            "SELECT 1 FROM knowledge_failures WHERE work_id='cov' AND state='legacy_recovery'").fetchone())
        self.assertIsNotNone(self.e.db.execute(
            "SELECT 1 FROM meta WHERE key='knowledge_recovery_coverage_v2'").fetchone())

    def test_revoked_coverage_routes_to_blocked_not_reset(self):
        p = self.root / 'B.md'; p.write_text('乙来源内容。')
        sb = self.e.store.ingest('project', str(p))
        self._coverage_row(sb['source_id'], 1)
        self.e.source_state('project', sb['source_id'], 'revoked')
        self.ev.recover_legacy_coverage()
        row = dict(self.e.db.execute(
            "SELECT state,error FROM knowledge_work WHERE id='cov'").fetchone())
        self.assertEqual(row['state'], 'blocked')
        self.assertIn('source revoked', row['error'])
        self.assertEqual(self.e.db.execute(
            "SELECT COUNT(*) FROM knowledge_work WHERE succession_depth>0").fetchone()[0], 0)


class CompletionEvidenceTests(_Harness):
    def _noop(self, cmd, prompt, timeout, env=None):
        data = json.loads(prompt)
        if 'proposal' in data:
            return {'approved': True, 'findings': []}, {}
        return {'operations': [], 'reason': '本批没有可持久化的增量'}, {}

    def test_compact_completion_record_survives_cleanup(self):
        secret = '本批只有一条例行记录，不应被存进完成证据。'
        self.capture(secret)
        real_publish = KnowledgeFiles.publish

        def fail(files, ids=None):
            if ids is not None:
                raise OSError('disk unavailable')
            return real_publish(files, ids)

        # First attempt: publication raises, leaving the committed batch pending.
        with patch('tongle.worker.invoke_json', side_effect=self._noop), \
                patch.object(KnowledgeFiles, 'publish', fail):
            self.ev.process(force=True)
        row = self.work()[0]
        self.assertEqual(row['state'], 'publication_pending')
        # Retry: the recovery path completes and keeps one compact receipt.
        with patch('tongle.worker.invoke_json', side_effect=self._noop):
            self.ev.process(force=True)
        row = dict(self.e.db.execute('SELECT * FROM knowledge_work').fetchone())
        self.assertEqual(row['state'], 'done')
        self.assertIsNone(row['bundle'])
        rows = self.e.db.execute(
            "SELECT error FROM knowledge_failures WHERE work_id=? AND state='completion'",
            (row['id'],)).fetchall()
        self.assertEqual(len(rows), 1)
        record = json.loads(rows[0]['error'])
        self.assertEqual(record['record_version'], 1)
        self.assertEqual(record['source_count'], 1)
        self.assertEqual(record['changed_ids'], [])
        self.assertIn('proposal_hash', record)
        self.assertTrue(record['inputs'])
        self.assertTrue(record['inputs'][0]['sha256'])
        self.assertIn('reason', record)
        # No source body / full prompt is copied into the completion proof.
        self.assertNotIn(secret, rows[0]['error'])


class AdapterPayloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'adapters'))
        import common
        self.common = common

    def _cfg(self, body):
        script = self.root / 'core'
        script.write_text('#!' + sys.executable + '\nimport sys\nsys.stdout.write(' + repr(body) + ')\n')
        script.chmod(0o755)
        return self.common.Config(home=str(self.root), project='p',
                                  python=sys.executable,
                                  root=str(self.root), cli_override=str(script))

    def test_canonical_rendered_context_used_verbatim_and_binds_receipt(self):
        rendered = self.common.CONTEXT_PREAMBLE + '\n- [完整卡片] 标题\n正文。'
        cfg = self._cfg(json.dumps({'results': [], 'rendered_context': rendered,
                                    'receipt_id': 'r1'}))
        receipt = {}
        out, warn = self.common.fetch_context(cfg, '问题', session_id='s', receipt=receipt)
        self.assertIsNone(warn)
        self.assertEqual(out, rendered)
        self.assertEqual(receipt.get('id'), 'r1')

    def test_oversize_canonical_payload_fails_closed_without_receipt(self):
        cfg = self._cfg(json.dumps({'results': [], 'rendered_context': 'x' * 7000,
                                    'receipt_id': 'r1'}))
        receipt = {}
        out, warn = self.common.fetch_context(cfg, '问题', session_id='s', receipt=receipt)
        self.assertIsNone(out)
        self.assertIn('fail closed', warn)
        self.assertEqual(receipt, {})

    def test_legacy_fallback_truncation_never_binds_receipt(self):
        big = {'ok': True, 'results': [{'text': 'x' * 5000, 'status': 'candidate'},
                                       {'text': 'y' * 5000, 'status': 'candidate'}],
               'receipt_id': 'legacy1'}
        cfg = self._cfg(json.dumps(big))
        receipt = {}
        out, warn = self.common.fetch_context(cfg, '问题', session_id='s', receipt=receipt)
        self.assertIsNone(warn)
        self.assertLessEqual(self.common._utf16_length(out),
                             self.common.QUERY_CONTEXT_BUDGET_CHARS)
        self.assertIn('truncated', out)
        self.assertEqual(receipt, {})

    def test_utf16_length_counts_astral_units(self):
        self.assertEqual(self.common._utf16_length('😀'), 2)
        self.assertEqual(self.common._utf16_length('a😀b'), 4)


if __name__ == '__main__':
    unittest.main()
