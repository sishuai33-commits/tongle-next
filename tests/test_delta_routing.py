"""Integration tests for the isolated v2 queue/process routing seam.

These tests drive ``tongle.delta_routing`` against the REAL sibling contracts
(``tongle.delta_contract``, ``tongle.delta_context``, ``tongle.delta_execution``,
``KnowledgeStore.apply``, ``KnowledgeFiles.publish``) using a real temporary
``Engine``/``Evolution``/config/capture and a stub ``invoke_json`` that returns
genuine ``delta_contract`` plan/review responses. ``run_job`` is never patched
to simulate success.
"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tongle.engine import Engine
from tongle.evolution import Evolution, config as evolution_config
from tongle.knowledge_files import KnowledgeFiles
from tongle.model_errors import ModelError

from tongle import delta_execution
from tongle import delta_routing
from tongle.delta_context import build_live_context

V2 = 'tongle-delta-pipeline-v1'
LEGACY = 'tongle-knowledge-contract-v5'


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.engine = Engine(self.root / 'state')
        self.engine.init(self.root / 'vault')
        self.addCleanup(self.engine.store.close)
        self._write_config({})
        self.evo = Evolution(self.engine)
        self.calls = []
        self.plan_mode = 'create'
        self.review_mode = 'approve'
        self.on_review = None
        self.fail_mode = None
        self._patcher = mock.patch('tongle.worker.invoke_json',
                                   side_effect=self._invoke)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

    # ------------------------------------------------------------ fixtures

    def _write_config(self, extra):
        cfg = {
            'knowledge_mode': 'evolving',
            'model_command': ['stub'],
            'knowledge_quiet_seconds': 0,
            'knowledge_daily_call_budget': 30,
            'knowledge_daily_token_budget': 1000000,
        }
        cfg.update(extra)
        (self.engine.home / 'config.json').write_text(json.dumps(cfg))

    @staticmethod
    def _owned_refs(payload):
        refs = []
        for part in payload.get('sources', []):
            if part.get('responsibility') == 'owned':
                for span in part.get('spans', []):
                    refs.append(span['ref'])
        return refs

    def _invoke(self, command, prompt, timeout=None, env=None):
        self.calls.append(prompt)
        if self.fail_mode == 'timeout':
            raise ModelError('timeout', code='timeout')
        payload = json.loads(prompt)
        if payload.get('stage') == 'delta_review':
            if self.on_review is not None:
                self.on_review()
            disps = payload['draft']['dispositions']
            state = 'fail' if self.review_mode == 'reject' else 'pass'
            checks = [{'index': i, 'E': state, 'Q': 'pass', 'T': 'pass',
                       'N': 'pass', 'reason': '窗口内逐条复核结论一致'}
                      for i in range(len(disps))]
            return ({'checks': checks,
                     'whole_window': {'status': 'pass', 'reason': '整窗复核通过'},
                     'human_preservation': []}, {'input': 1, 'output': 1})
        owned = self._owned_refs(payload)
        if self.plan_mode in ('noop', 'unresolved'):
            return ({'changes': [], 'dispositions': [{
                'source_refs': owned, 'decision': ('unresolved' if self.plan_mode == 'unresolved' else 'nonmaterial'),
                'change_refs': [], 'card_refs': [],
                'reason': '问候没有形成可持久保存的新事实'}]},
                {'input': 1, 'output': 1})
        sample = ''
        for part in payload.get('sources', []):
            if part.get('responsibility') == 'owned' and part.get('spans'):
                sample = part['spans'][0].get('text') or ''
                break
        body = ('新决定正文：' + sample[:120]) if sample else '新决定正文。'
        return ({'changes': [{'ref': 'n1', 'action': 'create',
                              'source_refs': owned, 'reason': '形成持久决定',
                              'title': '新决定', 'topic': '方案',
                              'body': body}],
                 'dispositions': [{'source_refs': owned, 'decision': 'material',
                                   'change_refs': ['n1'], 'card_refs': [],
                                   'reason': '该来源形成可持久保存的决定'}]},
                {'input': 1, 'output': 1})

    # ------------------------------------------------------------- helpers

    def _capture(self, project, session, role, event_id, text):
        return self.engine.store.capture(project, session, role, event_id, text)

    def _rows(self):
        return [dict(r) for r in self.engine.db.execute(
            'SELECT * FROM knowledge_work').fetchall()]

    def _row_for(self, source_id):
        # After a completed v2 row clears its bulky ``inputs``, ownership is
        # proven by the durable ``knowledge_seen`` booking instead.
        seen = self.engine.db.execute(
            'SELECT work_id FROM knowledge_seen WHERE source_id=? AND work_id IS NOT NULL',
            (source_id,)).fetchone()
        if seen is not None and seen['work_id']:
            row = self.engine.db.execute(
                'SELECT * FROM knowledge_work WHERE id=?', (seen['work_id'],)).fetchone()
            if row is not None:
                return dict(row)
        for row in self._rows():
            try:
                inputs = json.loads(row['inputs'] or '[]')
            except ValueError:
                inputs = []
            if any(isinstance(p, dict) and p.get('source_id') == source_id
                   for p in inputs):
                return row
        return None

    def _count(self, sql, params=()):
        return self.engine.db.execute(sql, params).fetchone()[0]

    def _completion_count(self):
        return self._count(
            'SELECT COUNT(*) FROM knowledge_failures WHERE state=?',
            (delta_execution.COMPLETE_AUDIT,))

    def _batch_changed(self, work_id):
        row = self.engine.db.execute(
            'SELECT changed_ids FROM knowledge_batches WHERE batch_id=?',
            (work_id,)).fetchone()
        return json.loads(row['changed_ids']) if row else None

    def _receipt(self, work_id):
        row = self.engine.db.execute(
            'SELECT error FROM knowledge_failures WHERE work_id=? AND state=?',
            (work_id, delta_execution.COMPLETE_AUDIT)).fetchone()
        self.assertIsNotNone(row)
        return json.loads(row['error'])


class QueueRoutingTests(_Base):
    def test_v2_queue_coalesces_full_source_and_claims_all_parts(self):
        self._write_config({'knowledge_pipeline_protocol': V2})
        text = 'A' * 11000 + 'LATEMARKER' + 'B' * 2000
        rec = self._capture('project', 's1', 'user', 'e1', text)
        sid = rec['source_id']
        source = self.engine.store.source(sid)
        parts = list(self.evo._source_parts(source))
        self.assertGreater(len(parts), 1)  # a real >10000 split exists

        result = self.evo.process(force=True)
        self.assertEqual(result['processed'], 1)
        row = self._row_for(sid)
        self.assertIsNotNone(row)
        self.assertEqual(row['pipeline_protocol'], V2)
        # After the compact cleanup the row no longer carries ``inputs``; the
        # durable receipt proves every recomputed part was owned (no truncation).
        self.assertEqual(row['inputs'], '[]')
        receipt = self._receipt(row['id'])
        proven = {(p['source_id'], p['version'], p['part'])
                  for p in receipt['owned_inputs']}
        self.assertEqual(len(receipt['owned_inputs']), len(parts))
        for part in parts:
            self.assertIn((sid, source['current_version'], part['part']), proven)
        # Row + ALL seen claims were written in the same routing transaction.
        for part in parts:
            seen = self.engine.db.execute(
                'SELECT work_id FROM knowledge_seen WHERE source_id=?'
                ' AND version=? AND part=?',
                (sid, source['current_version'], part['part'])).fetchone()
            self.assertEqual(seen['work_id'], row['id'])
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(row['state'], 'done')

    def test_quiet_period_never_steals_into_legacy(self):
        self._write_config({'knowledge_pipeline_protocol': V2,
                            'knowledge_quiet_seconds': 3600})
        rec = self._capture('project', 's1', 'user', 'e1', '最近的新决定。')
        result = self.evo.queue(force=False)
        self.assertEqual(result, 0)
        self.assertEqual(self._count('SELECT COUNT(*) FROM knowledge_work'), 0)
        self.assertIsNone(self._row_for(rec['source_id']))
        self.assertEqual(len(self.calls), 0)

    def test_config_switch_keeps_existing_v5_row_pinned(self):
        rec = self._capture('project', 's1', 'user', 'e1', '旧决定需要保留。')
        self.evo.queue(force=True)  # default config => legacy v5 contract
        row = self._row_for(rec['source_id'])
        self.assertIsNotNone(row)
        self.assertEqual(row['pipeline_protocol'], LEGACY)
        self._write_config({'knowledge_pipeline_protocol': V2})
        self.evo.queue(force=True)
        again = self._row_for(rec['source_id'])
        self.assertEqual(again['id'], row['id'])
        self.assertEqual(again['pipeline_protocol'], LEGACY)

    def test_partial_ownership_not_repinned_to_v2(self):
        self._write_config({'knowledge_pipeline_protocol': V2})
        rec = self._capture('project', 's1', 'user', 'e1', '部分已经归属的事件。')
        source = self.engine.store.source(rec['source_id'])
        with self.engine.transaction():
            self.engine.db.execute(
                'INSERT OR REPLACE INTO knowledge_seen'
                '(source_id,version,part,work_id) VALUES(?,?,?,?)',
                (source['source_id'], source['current_version'], 0,
                 'legacy-owner'))
        self.evo.queue(force=True)
        self.assertEqual(self._count('SELECT COUNT(*) FROM knowledge_work'), 0)
        seen = self.engine.db.execute(
            'SELECT work_id FROM knowledge_seen WHERE source_id=? AND part=0',
            (source['source_id'],)).fetchone()
        self.assertEqual(seen['work_id'], 'legacy-owner')

    def test_unknown_protocol_never_routed_legacy(self):
        self._write_config({'knowledge_pipeline_protocol': V2})
        with self.engine.transaction():
            self.engine.db.execute(
                "INSERT INTO knowledge_work(id,project,session,inputs,state,"
                "created_at,pipeline_protocol)"
                " VALUES('bogus','project','s','[]','pending',0.0,'bogus-proto')")
        result = self.evo.process(force=True)
        self.assertEqual(len(self.calls), 0)
        self.assertGreaterEqual(result.get('unknown_protocol_pending', 0), 1)
        row = self.engine.db.execute(
            "SELECT state,pipeline_protocol FROM knowledge_work WHERE id='bogus'"
        ).fetchone()
        self.assertEqual(row['state'], 'pending')
        self.assertEqual(row['pipeline_protocol'], 'bogus-proto')


class HostRoutingBoundaryTests(_Base):
    def test_queue_reports_real_v2_count(self):
        self._write_config({'knowledge_pipeline_protocol': V2})
        self._capture('project', 's', 'user', 'e', '决定保留审计记录。')
        self.assertEqual(self.evo.queue(force=True), 1)
        self.assertEqual(self.evo.queue(force=True), 0)

    def test_unknown_expired_running_and_blocked_are_untouched(self):
        with self.engine.transaction():
            for state in ('running', 'blocked'):
                self.engine.db.execute(
                    "INSERT INTO knowledge_work(id,project,session,inputs,state,created_at,"
                    "pipeline_protocol,attempts,lease_until) VALUES(?,?,?,?,?,0,'future',5,0)",
                    (state, 'project', 's', '[]', state))
        before = self._rows()
        self.evo.process(force=True)
        self.assertEqual(self._rows(), before)
        self.assertEqual(self.calls, [])

    def test_paused_committed_worker_cannot_restore_publication_state(self):
        self._write_config({'knowledge_pipeline_protocol': V2})
        self._capture('project', 's', 'user', 'e', '决定保留审计记录。')
        self.evo.process(force=True)
        row = self._rows()[0]
        with self.engine.transaction():
            self.engine.db.execute("UPDATE knowledge_work SET state='paused',lease_token='old',lease_until=? WHERE id=?", (time.time()+600, row['id']))
        before = self._rows()
        audits = self._count('SELECT COUNT(*) FROM knowledge_failures')
        outcome = delta_routing._handle_error(self.evo, row, 'old', evolution_config(self.engine), OSError('publication interrupted'))
        self.assertEqual(outcome, {'stale': 1})
        self.assertEqual(self._rows(), before)
        self.assertEqual(self._count('SELECT COUNT(*) FROM knowledge_failures'), audits)


class ProcessRoutingTests(_Base):
    def test_create_completes_two_calls_publishes_and_audits(self):
        self._write_config({'knowledge_pipeline_protocol': V2})
        rec = self._capture('project', 's1', 'user', 'e1', '新决定：保留审核步骤。')
        result = self.evo.process(force=True)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(result['processed'], 1)
        row = self._row_for(rec['source_id'])
        self.assertEqual(row['state'], 'done')
        changed = self._batch_changed(row['id'])
        self.assertEqual(len(changed), 1)
        card = self.evo.cards.get(changed[0])
        self.assertGreaterEqual(card['version'], 1)
        files = KnowledgeFiles(self.engine, self.evo.cards)
        self.assertEqual(files._tracking(changed[0])['state'], 'ok')
        # The published Markdown really exists on disk.
        self.assertTrue((files.root / card['path']).is_file())
        self.assertIsInstance(self.evo.cards.history(changed[0]), list)
        self.assertEqual(self._completion_count(), 1)

    def test_greeting_noop_counted_separately(self):
        self._write_config({'knowledge_pipeline_protocol': V2})
        self.plan_mode = 'noop'
        rec = self._capture('project', 's1', 'user', 'e1', '你好，谢谢。')
        result = self.evo.process(force=True)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(result['processed'], 1)
        self.assertEqual(result['records'], 0)
        self.assertEqual(result.get('no_op_done'), 1)
        row = self._row_for(rec['source_id'])
        self.assertEqual(self._batch_changed(row['id']), [])
        self.assertEqual(self.evo.cards.list('project'), [])

    def test_second_process_adds_no_calls_or_audits(self):
        self._write_config({'knowledge_pipeline_protocol': V2})
        self._capture('project', 's1', 'user', 'e1', '新决定需要记录。')
        self.evo.process(force=True)
        calls = len(self.calls)
        audits = self._count('SELECT COUNT(*) FROM knowledge_failures')
        self.evo.process(force=True)
        self.assertEqual(len(self.calls), calls)
        self.assertEqual(self._count('SELECT COUNT(*) FROM knowledge_failures'),
                         audits)

    def test_unchanged_plan_after_feedback_is_terminal_no_autoretry(self):
        self._write_config({'knowledge_pipeline_protocol': V2})
        rec = self._capture('project', 's1', 'user', 'e1', '一个来源。')
        self.review_mode = 'reject'
        self.evo.process(force=True)
        self.assertEqual(len(self.calls), 3)
        row = self._row_for(rec['source_id'])
        self.assertEqual(row['state'], 'failed')
        self.assertEqual(self._count('SELECT COUNT(*) FROM knowledge_batches'), 0)
        # A failed row is never re-dispatched.
        self.evo.process(force=True)
        self.assertEqual(len(self.calls), 3)

    def test_source_edit_between_review_and_commit_blocks_apply(self):
        self._write_config({'knowledge_pipeline_protocol': V2})
        rec = self._capture('project', 's1', 'user', 'e1', '原始事件文本。')
        sid = rec['source_id']

        def edit():
            with self.engine.transaction():
                self.engine.db.execute(
                    'UPDATE versions SET text=text||? WHERE source_id=?'
                    ' AND version=1', (' 追加修改。', sid))

        self.on_review = edit
        self.evo.process(force=True)
        self.assertEqual(len(self.calls), 2)
        # No committed batch, no card: an old approval is never applied.
        self.assertEqual(self._count('SELECT COUNT(*) FROM knowledge_batches'), 0)
        self.assertEqual(self.evo.cards.list('project'), [])
        row = self._row_for(sid)
        self.assertEqual(row['state'], 'failed')

    def test_publish_failure_retains_receipt_and_resumes_without_calls(self):
        self._write_config({'knowledge_pipeline_protocol': V2})
        rec = self._capture('project', 's1', 'user', 'e1', '需要发布的决定。')
        with mock.patch.object(KnowledgeFiles, '_write_atomic',
                               side_effect=OSError('disk full')):
            self.evo.process(force=True)
        self.assertEqual(len(self.calls), 2)
        row = self._row_for(rec['source_id'])
        self.assertEqual(row['state'], 'publication_pending')
        self.assertIsNotNone(row['bundle'])
        self.assertIsNotNone(self._batch_changed(row['id']))
        # A later cycle resumes publication with zero extra model calls.
        self.evo.process(force=True)
        self.assertEqual(len(self.calls), 2)
        row = self._row_for(rec['source_id'])
        self.assertEqual(row['state'], 'done')

    def test_zero_budget_parks_without_false_done(self):
        self._write_config({'knowledge_pipeline_protocol': V2,
                            'knowledge_daily_call_budget': 0})
        rec = self._capture('project', 's1', 'user', 'e1', '预算为零的决定。')
        result = self.evo.process(force=True)
        self.assertEqual(len(self.calls), 0)
        self.assertTrue(result.get('budget_wait'))
        row = self._row_for(rec['source_id'])
        self.assertEqual(row['state'], 'budget_wait')
        self.assertNotEqual(row['state'], 'done')
        self.assertEqual(self._completion_count(), 0)
        self.assertEqual(self._count('SELECT COUNT(*) FROM knowledge_batches'), 0)

    def test_reference_role_is_independent_and_not_owned(self):
        self._write_config({'knowledge_pipeline_protocol': V2})
        prev = self._capture('project', 's1', 'user', 'e1', '先前主题：猫。')
        time.sleep(0.02)
        owned = self._capture('project', 's1', 'user', 'e2', '现在讨论狗。')
        # Both cycles run the real pipeline; the earliest event goes first.
        self.evo.process(force=True)
        self.evo.process(force=True)
        owned_job = self._row_for(owned['source_id'])
        self.assertIsNotNone(owned_job)
        # The frozen window is reconstructed from the compact receipt after
        # cleanup: the owned event stays owned, the prior stays a reference.
        receipt = self._receipt(owned_job['id'])
        roles = {p['source_id']: p['responsibility']
                 for p in receipt['window_sources']}
        self.assertEqual(roles.get(owned['source_id']), 'owned')
        self.assertEqual(roles.get(prev['source_id']), 'reference')
        # The reference remains a distinct source, never a second owned part.
        self.assertEqual(
            [p['source_id'] for p in receipt['window_sources']
             if p['responsibility'] == 'owned'],
            [owned['source_id']])

    def test_revoked_source_stops_with_explicit_attention_audit(self):
        self._write_config({'knowledge_pipeline_protocol': V2})
        rec = self._capture('project', 's1', 'user', 'e1', '一个会被撤回的来源。')
        self.evo.queue(force=True)  # create the v2 row without processing it
        self.engine.source_state('project', rec['source_id'], 'revoked')
        self.evo.process(force=True)
        row = self._row_for(rec['source_id'])
        self.assertEqual(row['state'], 'failed')
        self.assertIn('needs review:', row['error'])
        audits = self._count(
            "SELECT COUNT(*) FROM knowledge_failures WHERE work_id=?"
            " AND state='failed'", (row['id'],))
        self.assertEqual(audits, 1)
        self.assertEqual(self._count('SELECT COUNT(*) FROM knowledge_batches'), 0)

    def test_unresolved_is_visible_attention_not_an_auto_retry_loop(self):
        self._write_config({'knowledge_pipeline_protocol': V2})
        self._capture('project', 's', 'user', 'e', '条件尚不明确，不能确定结论。')
        self.plan_mode = 'unresolved'
        self.evo.process(force=True)
        before = self._rows()
        self.assertEqual(before[0]['state'], 'failed')
        self.assertIn('needs review:', before[0]['error'])
        self.assertEqual(len(self.calls), 2)
        self.evo.process(force=True)
        self.assertEqual(self._rows(), before)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self._completion_count(), 0)

    def test_legacy_recovery_never_resets_delta_receipts(self):
        self._write_config({'knowledge_pipeline_protocol': V2})
        self._capture('project', 's', 'user', 'e', '条件尚不明确，不能确定结论。')
        self.plan_mode = 'unresolved'
        self.evo.process(force=True)
        with self.engine.transaction():
            self.engine.db.execute("DELETE FROM meta WHERE key IN ('knowledge_recovery_coverage_v2','knowledge_recovery_catalog_overflow_v1')")
            self.engine.db.execute("UPDATE knowledge_work SET state='coverage_wait'")
        before = self._rows()
        self.assertEqual(self.evo.recover_legacy_coverage()['recovered'], 0)
        self.assertEqual(self.evo.recover_catalog_overflow()['recovered'], 0)
        self.assertEqual(self._rows(), before)

    def test_stale_worker_writes_nothing_and_reports_stale(self):
        self._write_config({'knowledge_pipeline_protocol': V2})
        rec = self._capture('project', 's1', 'user', 'e1', '一个正常的事件。')
        self.evo.queue(force=True)
        row = self._row_for(rec['source_id'])
        now = time.time()
        with self.engine.transaction():
            self.engine.db.execute(
                "UPDATE knowledge_work SET state='running',lease_token='other',"
                " lease_until=? WHERE id=?", (now + 600, row['id']))
        job = dict(self.engine.db.execute(
            'SELECT * FROM knowledge_work WHERE id=?', (row['id'],)).fetchone())
        files = KnowledgeFiles(self.engine, self.evo.cards)
        outcome = delta_routing.process_one(
            self.evo, job, 'not-the-token', evolution_config(self.engine), files)
        self.assertEqual(outcome, {'stale': 1})
        after = self.engine.db.execute(
            'SELECT state,lease_token,error FROM knowledge_work WHERE id=?',
            (row['id'],)).fetchone()
        self.assertEqual(after['state'], 'running')
        self.assertEqual(after['lease_token'], 'other')
        self.assertIsNone(after['error'])
        self.assertEqual(len(self.calls), 0)

    def test_transient_failure_retries_and_trips_breaker(self):
        self._write_config({'knowledge_pipeline_protocol': V2,
                            'knowledge_breaker_failures': 1})
        rec = self._capture('project', 's1', 'user', 'e1', '临时失败的事件。')
        self.fail_mode = 'timeout'
        result = self.evo.process(force=True)
        self.assertEqual(len(self.calls), 1)
        row = self._row_for(rec['source_id'])
        self.assertEqual(row['state'], 'retry')
        self.assertTrue(result.get('breaker_paused'))
        self.assertEqual(self._completion_count(), 0)

    def test_successful_completion_clears_existing_breaker(self):
        self._write_config({'knowledge_pipeline_protocol': V2})
        self.evo.breaker_trip('timeout')
        self.assertEqual(self.evo._breaker()['streak'], 1)
        self._capture('project', 's1', 'user', 'e1', '一个成功完成的事件。')
        self.evo.process(force=True)
        self.assertEqual(self.evo._breaker()['streak'], 0)
        self.assertEqual(self._completion_count(), 1)


if __name__ == '__main__':
    unittest.main()
