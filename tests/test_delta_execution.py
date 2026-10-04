"""Synthetic delta-execution regressions; no live assets or models.

These tests drive the isolated delta pipeline against the REAL sibling
contracts (tongle.delta_contract, tongle.source_windows, KnowledgeStore.apply,
KnowledgeFiles.publish) using the working fixture pattern from
tests/test_evolution_regressions.py: Engine/Evolution/KnowledgeFiles with the
config written to ``engine.home/config.json``.

The stub branches on the *stage carried in the prompt* (delta_plan vs
delta_review) and on whether the plan payload carries host-derived
``repair_feedback`` -- never on invocation ordinal -- so a wrong-shaped reply
cannot masquerade as the right stage. All replies are synthetic; passing these
tests proves machine behaviour only, never real semantic model quality.
"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tongle import delta_contract, source_windows
from tongle.engine import Engine
from tongle.evolution import Evolution, BudgetWait, config as evolution_config
from tongle.knowledge_files import KnowledgeFiles

from tongle import delta_execution
from tongle.delta_execution import (
    PROTOCOL,
    ensure_schema,
    run_job,
    resume_publication,
)


class DeltaExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        (self.root / 'state').mkdir()
        (self.root / 'vault').mkdir()
        self.engine = Engine(self.root / 'state')
        self.engine.init(self.root / 'vault')
        self.addCleanup(self.engine.store.close)
        # Config lives at engine.home/config.json, not the parent root.
        self._write_config()
        self.evo = Evolution(self.engine)
        ensure_schema(self.evo)
        # Store is evo.engine.store; capture returns a receipt dict.
        receipt = self.engine.store.capture('project', 'session', 'user', 'event1', '新决定。')
        self.sid = receipt['source_id']
        self.evo.queue(force=True)
        self.work_id = self._row()['id']
        self._set_running()
        self.files = KnowledgeFiles(self.engine, self.evo.cards)
        self.calls = []
        self.plan_calls = 0
        self.review_calls = 0
        self.plan_replies = []
        self.review_replies = []
        self.plan_response = self._plan_response()
        self.repair_plan_response = None
        self.review_response = self._review_response()
        self.mode = 'ok'
        self.on_review = None
        self._patcher = mock.patch('tongle.worker.invoke_json', side_effect=self._invoke)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

    # ------------------------------------------------------------ fixtures

    def _write_config(self, extra=None):
        cfg = {
            'knowledge_mode': 'evolving',
            'model_command': ['stub'],
            'knowledge_quiet_seconds': 0,
            'knowledge_daily_call_budget': 30,
            'knowledge_daily_token_budget': 1000000,
        }
        cfg.update(extra or {})
        (self.engine.home / 'config.json').write_text(json.dumps(cfg))

    def _row(self):
        return self.engine.db.execute("SELECT * FROM knowledge_work WHERE project='project'").fetchone()

    def _set_running(self):
        self.engine.db.execute(
            "UPDATE knowledge_work SET pipeline_protocol=?, state='running', lease_token='t', lease_until=?, error=NULL WHERE id=?",
            (PROTOCOL, time.time() + 600, self.work_id),
        )
        self.engine.db.commit()

    def _source(self):
        # Real Store.source fields: source_id/current_version/text/role/kind.
        return self.engine.store.source(self.sid)

    def _context(self):
        src = self._source()
        parts = [{
            'source_id': src['source_id'],
            'version': src['current_version'],
            'part': 0,
            'role': src['role'],
            'kind': src['kind'],
            'text': src['text'],
        }]
        window = source_windows.freeze_window('project', parts)
        return delta_contract.build_context(window, self.evo.cards.list('project'))

    def _plan_response(self):
        # Real delta_contract plan schema: changes use ref/action/source_refs/
        # reason/title/topic/body; dispositions use source_refs/decision/
        # change_refs/card_refs/reason and must cover every owned source span.
        return {
            'changes': [{
                'ref': 'n1',
                'action': 'create',
                'source_refs': ['s1'],
                'reason': '明确的新决定需要持久保存',
                'title': '新决定',
                'topic': '方案',
                'body': '新决定。',
            }],
            'dispositions': [{
                'source_refs': ['s1'],
                'decision': 'material',
                'change_refs': ['n1'],
                'card_refs': [],
                'reason': '该来源片段形成可持久保存的新决定',
            }],
        }

    def _changed_plan_response(self):
        response = self._plan_response()
        response['changes'][0]['title'] = '修订后的新决定'
        response['changes'][0]['body'] = '修订后的新决定正文。'
        return response

    def _noop_plan_response(self):
        return {
            'changes': [],
            'dispositions': [{
                'source_refs': ['s1'],
                'decision': 'nonmaterial',
                'change_refs': [],
                'card_refs': [],
                'reason': '该来源片段无持久价值',
            }],
        }

    def _review_response(self):
        return {
            'checks': [{
                'index': 0, 'E': 'pass', 'Q': 'pass', 'T': 'pass', 'N': 'pass',
                'reason': '证据完整且语义一致',
            }],
            'whole_window': {'status': 'pass', 'reason': '窗口完整复核通过'},
            'human_preservation': [],
        }

    def _reject_review(self, payload):
        draft = payload.get('draft') or {}
        disps = draft.get('dispositions') or []
        checks = [{
            'index': i, 'E': 'fail', 'Q': 'pass', 'T': 'pass', 'N': 'pass',
            'reason': '证据不支持所声称的语义增量',
        } for i in range(len(disps))]
        return {
            'checks': checks,
            'whole_window': {'status': 'fail', 'reason': '整窗复核未通过'},
            'human_preservation': [],
        }

    def _plan_reply(self, payload):
        if self.plan_replies:
            item = self.plan_replies.pop(0)
        elif payload.get('repair_feedback') is not None and self.repair_plan_response is not None:
            item = self.repair_plan_response
        else:
            item = self.plan_response
        if item == 'budget':
            raise BudgetWait('daily compilation budget exhausted')
        return item, {'input': 10, 'output': 10}

    def _review_reply(self, payload):
        if self.review_replies:
            item = self.review_replies.pop(0)
        elif self.mode == 'budget_review':
            item = 'budget'
        elif self.mode == 'review_reject':
            item = 'reject'
        else:
            item = self.review_response
        if item == 'budget':
            raise BudgetWait('daily compilation budget exhausted')
        if item == 'reject':
            return self._reject_review(payload), {'input': 10, 'output': 10}
        return item, {'input': 10, 'output': 10}

    def _invoke(self, cmd, prompt, timeout=None, env=None):
        self.calls.append((cmd, prompt))
        payload = json.loads(prompt)
        stage = payload.get('stage')
        if stage == 'delta_review':
            self.review_calls += 1
            if self.on_review is not None:
                self.on_review()
            return self._review_reply(payload)
        self.plan_calls += 1
        return self._plan_reply(payload)

    def _job(self):
        return dict(self._row())

    def _cfg(self):
        return evolution_config(self.engine)

    def _changed_ids(self):
        row = self.engine.db.execute(
            "SELECT changed_ids FROM knowledge_batches WHERE batch_id=?",
            (self.work_id,)).fetchone()
        return json.loads(row['changed_ids']) if row else []

    def _bundle(self):
        return json.loads(self._row()['bundle'])

    def _rewrite_bundle(self, bundle):
        self.engine.db.execute(
            "UPDATE knowledge_work SET bundle=? WHERE id=?",
            (json.dumps(bundle), self.work_id))
        self.engine.db.commit()

    def _receipt(self):
        row = self.engine.db.execute(
            "SELECT error FROM knowledge_failures WHERE work_id=? AND state=?",
            (self.work_id, delta_execution.COMPLETE_AUDIT)).fetchone()
        self.assertIsNotNone(row)
        return json.loads(row['error'])

    def _stage_rows(self):
        return self.engine.db.execute(
            "SELECT stage,error,elapsed FROM knowledge_calls WHERE work_id=? ORDER BY created_at",
            (self.work_id,)).fetchall()

    # -------------------------------------------------------------- tests

    def test_create_completes_with_two_calls_and_publication(self):
        result = run_job(self.evo, self._job(), 't', self._cfg(), self.files, self._context)
        self.assertTrue(result.get('processed'))
        self.assertEqual(len(self.calls), 2)
        changed = self._changed_ids()
        self.assertEqual(len(changed), 1)
        card = self.evo.cards.get(changed[0])
        self.assertIsNotNone(card)
        self.assertGreaterEqual(card.get('version', 0), 1)
        tracking = self.files._tracking(changed[0])
        self.assertEqual(tracking.get('state'), 'ok')

    def test_reviewed_noop_and_counter(self):
        self.plan_response = self._noop_plan_response()
        result = run_job(self.evo, self._job(), 't', self._cfg(), self.files, self._context)
        self.assertEqual(result.get('no_op_done'), 1)
        self.assertEqual(result.get('records'), 0)
        self.assertEqual(self._changed_ids(), [])

    def test_semantic_rejection_identical_plan_not_reviewed_again(self):
        # A rejection re-opens ONE repair round, but the identical repaired
        # plan is refused before a second review is spent, and the ledger is
        # never reset.
        self.mode = 'review_reject'
        with self.assertRaises(Exception) as ctx:
            run_job(self.evo, self._job(), 't', self._cfg(), self.files, self._context)
        self.assertIn('unchanged plan', str(ctx.exception))
        self.assertEqual(self.plan_calls, 2)
        self.assertEqual(self.review_calls, 1)
        calls_after_first = len(self.calls)
        self.assertEqual(calls_after_first, 3)
        with self.assertRaises(Exception):
            run_job(self.evo, self._job(), 't', self._cfg(), self.files, self._context)
        self.assertEqual(len(self.calls), calls_after_first)

    def test_forged_bundle_response_hash_rejected(self):
        # Force a publish failure so the bulky bundle is retained in
        # publication_pending, mutate it, then require the resume to reject.
        with mock.patch.object(KnowledgeFiles, '_write_atomic',
                               side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                run_job(self.evo, self._job(), 't', self._cfg(), self.files, self._context)
        self.assertEqual(self._row()['state'], 'publication_pending')
        bundle = self._bundle()
        bundle['steps']['delta_plan']['response_hash'] = 'bad'
        self._rewrite_bundle(bundle)
        with self.assertRaises(Exception):
            resume_publication(self.evo, self.work_id, self.files)

    def test_malformed_nonempty_bundle_rejected(self):
        with mock.patch.object(KnowledgeFiles, '_write_atomic',
                               side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                run_job(self.evo, self._job(), 't', self._cfg(), self.files, self._context)
        self.engine.db.execute(
            "UPDATE knowledge_work SET bundle=? WHERE id=?",
            ('{not json', self.work_id))
        self.engine.db.commit()
        with self.assertRaises(Exception):
            resume_publication(self.evo, self.work_id, self.files)

    def test_lost_lease_prevents_dispatch(self):
        self.engine.db.execute(
            "UPDATE knowledge_work SET lease_until=0 WHERE id=?", (self.work_id,))
        self.engine.db.commit()
        with self.assertRaises(Exception):
            run_job(self.evo, self._job(), 't', self._cfg(), self.files, self._context)
        self.assertEqual(len(self.calls), 0)

    def test_source_change_between_review_and_commit_blocks_apply(self):
        original = self._context
        calls = {'n': 0}

        def factory():
            calls['n'] += 1
            ctx = original()
            # Trigger only at the pre-commit re-read (after both successful
            # dispatches and both persisted stages), so the change is caught
            # between review and commit and apply never runs.
            if calls['n'] >= 7:
                ctx = dict(ctx)
                ctx['binding'] = 'changed'
            return ctx

        with self.assertRaises(Exception):
            run_job(self.evo, self._job(), 't', self._cfg(), self.files, factory)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self._changed_ids(), [])

    def test_vault_manual_edit_before_commit_blocks_and_preserves_human_file(self):
        # Seed and publish a real card so it participates in the frozen
        # context; then hand-edit its Vault file between review and commit.
        seeded = self.evo.cards.seed(
            'project', 'seed-key', 'knowledge', '主题', '种子卡', '原始正文。')
        cid = seeded['id']
        self.files.publish([cid])
        path = self.evo.cards.get(cid)['path']
        self.assertIsInstance(path, str)
        target = self.files.root / path
        self.assertTrue(target.is_file())
        original = self._context
        calls = {'n': 0, 'wrote': False}
        marker = "\n\n%%\ntongle-card:"
        manual_text = '人类手动修改的内容。'

        def factory():
            calls['n'] += 1
            if calls['n'] >= 6 and not calls['wrote']:
                # A real manual Vault edit (keeps the wrapper identity), made
                # before the pre-commit files.sync().
                raw = target.read_text(encoding='utf-8')
                self.assertIn(marker, raw)
                head, sep, tail = raw.rpartition(marker)
                target.write_text(head + '\n\n' + manual_text + sep + tail,
                                  encoding='utf-8')
                calls['wrote'] = True
            return original()

        with self.assertRaises(Exception):
            run_job(self.evo, self._job(), 't', self._cfg(), self.files, factory)
        self.assertTrue(calls['wrote'])
        # Both model stages succeeded but the manual edit drifted the binding,
        # so apply/publish never ran and no batch was created.
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self._changed_ids(), [])
        self.assertEqual(self._row()['state'], 'running')
        # The human edit is preserved on disk and reflected in the card, and
        # the file is never rewritten over the human content.
        self.assertIn(manual_text, target.read_text(encoding='utf-8'))
        card = self.evo.cards.get(cid)
        self.assertIn(manual_text, card['body'])
        self.assertTrue(card['manually_edited'])
        self.assertGreaterEqual(card['version'], 2)

    def test_crash_before_apply_retains_two_successful_stages(self):
        with mock.patch.object(self.evo.cards, 'apply',
                               side_effect=RuntimeError('crash before apply')):
            with self.assertRaises(RuntimeError):
                run_job(self.evo, self._job(), 't', self._cfg(), self.files, self._context)
        self.assertEqual(len(self.calls), 2)
        bundle = self._bundle()
        self.assertIn('delta_plan', bundle['steps'])
        self.assertIn('delta_review', bundle['steps'])
        # The crash left the work running and uncommitted.
        self.assertEqual(self._row()['state'], 'running')
        self.assertEqual(self._changed_ids(), [])
        # A resumed attempt reuses both persisted stages: no extra calls.
        result = run_job(self.evo, self._job(), 't', self._cfg(), self.files, self._context)
        self.assertTrue(result.get('processed'))
        self.assertEqual(len(self.calls), 2)

    def test_publication_resume_is_replay_with_zero_extra_calls(self):
        run_job(self.evo, self._job(), 't', self._cfg(), self.files, self._context)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self._row()['state'], 'done')
        self.assertIsNone(self._row()['bundle'])
        again = resume_publication(self.evo, self.work_id, self.files)
        self.assertTrue(again.get('replayed'))
        self.assertEqual(len(self.calls), 2)

    def test_compact_done_clears_bulk_and_keeps_one_receipt(self):
        result = run_job(self.evo, self._job(), 't', self._cfg(), self.files, self._context)
        self.assertTrue(result.get('processed'))
        row = self._row()
        self.assertEqual(row['state'], 'done')
        self.assertEqual(row['inputs'], '[]')
        self.assertIsNone(row['proposal'])
        self.assertIsNone(row['review'])
        self.assertIsNone(row['bundle'])
        self.assertIsNone(row['lease_token'])
        self.assertEqual(row['lease_until'], 0)
        # Exactly one schema2 completion audit, and it verifies.
        rows = self.engine.db.execute(
            "SELECT error FROM knowledge_failures WHERE work_id=? AND state=?",
            (self.work_id, delta_execution.COMPLETE_AUDIT)).fetchall()
        self.assertEqual(len(rows), 1)
        receipt = json.loads(rows[0]['error'])
        self.assertEqual(receipt.get('schema'), 2)
        self.assertEqual(receipt.get('revision'), 0)
        self.assertIsNone(receipt.get('repair'))
        self.assertEqual(receipt.get('history_stages'), [])
        from tongle import delta_receipts
        verified = delta_receipts.verify_completed(self.evo, self.work_id)
        self.assertTrue(verified['verified'])

    def test_publication_pending_retains_bulk_until_receipt(self):
        with mock.patch.object(KnowledgeFiles, '_write_atomic',
                               side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                run_job(self.evo, self._job(), 't', self._cfg(), self.files, self._context)
        row = self._row()
        self.assertEqual(row['state'], 'publication_pending')
        # The bulky bundle is retained until the real receipt is durable.
        self.assertIsNotNone(row['bundle'])
        self.assertEqual(self.engine.db.execute(
            "SELECT COUNT(*) FROM knowledge_failures WHERE work_id=? AND state=?",
            (self.work_id, delta_execution.COMPLETE_AUDIT)).fetchone()[0], 0)
        result = resume_publication(self.evo, self.work_id, self.files)
        self.assertTrue(result.get('processed'))
        self.assertEqual(self._row()['state'], 'done')
        self.assertIsNone(self._row()['bundle'])

    def test_zero_write_replay_on_tampered_receipt(self):
        run_job(self.evo, self._job(), 't', self._cfg(), self.files, self._context)
        # Tamper the completion audit in place, then require the done replay
        # to reject with zero writes (no new calls, no publish, no extra audit).
        calls = len(self.calls)
        self.engine.db.execute(
            "UPDATE knowledge_failures SET error='{}' WHERE work_id=? AND state=?",
            (self.work_id, delta_execution.COMPLETE_AUDIT))
        self.engine.db.commit()
        with self.assertRaises(delta_execution.StageLimit):
            resume_publication(self.evo, self.work_id, self.files)
        self.assertEqual(len(self.calls), calls)
        self.assertEqual(self.engine.db.execute(
            "SELECT COUNT(*) FROM knowledge_failures WHERE work_id=? AND state=?",
            (self.work_id, delta_execution.COMPLETE_AUDIT)).fetchone()[0], 1)

    def test_budget_wait_preserves_plan_and_no_false_done(self):
        self.mode = 'budget_review'
        with self.assertRaises(Exception):
            run_job(self.evo, self._job(), 't', self._cfg(), self.files, self._context)
        row = self._row()
        self.assertEqual(row['state'], 'running')
        bundle = json.loads(row['bundle'])
        self.assertIn('delta_plan', bundle['steps'])
        self.assertNotIn('delta_review', bundle['steps'])

    def test_legacy_protocol_rejected(self):
        self.engine.db.execute(
            "UPDATE knowledge_work SET pipeline_protocol='old' WHERE id=?", (self.work_id,))
        self.engine.db.commit()
        with self.assertRaises(Exception):
            run_job(self.evo, self._job(), 't', self._cfg(), self.files, self._context)
        self.assertEqual(len(self.calls), 0)

    def test_cumulative_call_cap_includes_failed_calls(self):
        for i in range(2):
            self.engine.db.execute(
                "INSERT INTO knowledge_calls(id,work_id,stage,day,reserved,actual_input,actual_output,elapsed,error,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (100 + i, self.work_id, 'delta_plan', '2026-01-01', 0, 1, 1, None, 'x', time.time()),
            )
        self.engine.db.commit()
        with self.assertRaises(Exception):
            run_job(self.evo, self._job(), 't', self._cfg(), self.files, self._context)
        self.assertEqual(len(self.calls), 0)


class RepairRoundTests(DeltaExecutionTests):
    """ONE feedback-directed repair round, capped by the same 2-call totals."""

    def test_structural_error_triggers_single_repair_and_completes(self):
        self.plan_replies = [{'unexpected': True}, self._plan_response()]
        result = run_job(self.evo, self._job(), 't', self._cfg(), self.files, self._context)
        self.assertTrue(result.get('processed'))
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.plan_calls, 2)
        self.assertEqual(self.review_calls, 1)
        # After cleanup the compact receipt carries the body-free repair
        # provenance instead of the bulky bundle.
        receipt = self._receipt()
        self.assertEqual(receipt.get('revision'), 1)
        self.assertEqual(receipt['repair'].get('kind'), 'plan')
        self.assertEqual(len(receipt.get('history_stages') or []), 1)
        self.assertEqual(len(receipt.get('stages') or []), 2)
        self.assertEqual(len(self._changed_ids()), 1)
        # Provenance: both plan rounds and the review are settled successes.
        rows = self._stage_rows()
        self.assertEqual([r['stage'] for r in rows],
                         ['delta_plan', 'delta_plan', 'delta_review'])
        self.assertTrue(all(r['error'] is None and r['elapsed'] is not None for r in rows))

    def test_semantic_review_failure_triggers_repair_then_passes(self):
        self.plan_replies = [self._plan_response(), self._changed_plan_response()]
        self.review_replies = ['reject', self._review_response()]
        # Force a publish failure so the precleanup bundle is observable for
        # the feedback assertion, then resume to completion.
        with mock.patch.object(KnowledgeFiles, '_write_atomic',
                               side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                run_job(self.evo, self._job(), 't', self._cfg(), self.files, self._context)
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(self.plan_calls, 2)
        self.assertEqual(self.review_calls, 2)
        bundle = self._bundle()
        self.assertEqual(bundle.get('revision'), 1)
        feedback = bundle['repair_feedback']
        self.assertEqual(feedback.get('kind'), 'review')
        findings = feedback['prior_draft'].get('review_findings') or []
        self.assertTrue(findings)
        self.assertTrue(findings[0].get('reason'))
        result = resume_publication(self.evo, self.work_id, self.files)
        self.assertTrue(result.get('processed'))
        # The old rejected review audit stays bound to the old payload.
        self.assertIsNotNone(self._changed_ids())

    def test_unresolved_plan_triggers_repair_then_resolves(self):
        unresolved = {
            'changes': [],
            'dispositions': [{
                'source_refs': ['s1'],
                'decision': 'unresolved',
                'change_refs': [],
                'card_refs': [],
                'reason': '条件尚不明确，无法确定结论',
            }],
        }
        self.plan_replies = [unresolved, self._plan_response()]
        result = run_job(self.evo, self._job(), 't', self._cfg(), self.files, self._context)
        self.assertTrue(result.get('processed'))
        self.assertEqual(self.plan_calls, 2)
        self.assertEqual(self.review_calls, 1)
        receipt = self._receipt()
        self.assertEqual(receipt.get('revision'), 1)
        self.assertEqual(receipt['repair'].get('kind'), 'unresolved')
        self.assertEqual(len(self._changed_ids()), 1)

    def test_second_structural_failure_is_terminal_within_cap(self):
        self.plan_replies = [{'bad': 1}, {'bad': 2}]
        with self.assertRaises(Exception):
            run_job(self.evo, self._job(), 't', self._cfg(), self.files, self._context)
        self.assertEqual(self.plan_calls, 2)
        self.assertEqual(self.review_calls, 0)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self._changed_ids(), [])
        self.assertEqual(self._row()['state'], 'running')

    def test_second_review_failure_is_terminal_within_cap(self):
        self.plan_replies = [self._plan_response(), self._changed_plan_response()]
        self.mode = 'review_reject'
        with self.assertRaises(Exception):
            run_job(self.evo, self._job(), 't', self._cfg(), self.files, self._context)
        self.assertEqual(self.plan_calls, 2)
        self.assertEqual(self.review_calls, 2)
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(self._changed_ids(), [])

    def test_forged_repair_feedback_is_rejected_without_extra_calls(self):
        self.plan_replies = [{'bad': 1}, self._plan_response()]
        with mock.patch.object(KnowledgeFiles, '_write_atomic',
                               side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                run_job(self.evo, self._job(), 't', self._cfg(), self.files, self._context)
        calls = len(self.calls)
        bundle = self._bundle()
        self.assertEqual(bundle.get('revision'), 1)
        bundle['repair_feedback']['error'] = 'forged label'
        self._rewrite_bundle(bundle)
        with self.assertRaises(Exception):
            resume_publication(self.evo, self.work_id, self.files)
        self.assertEqual(len(self.calls), calls)

    def test_repair_original_call_tamper_fails_receipt(self):
        # A repaired completion must carry real ledger/audit proofs for the
        # ORIGINAL revision-0 step. Deleting that historical call makes the
        # compact receipt unprovable on a done replay (zero extra calls).
        self.plan_replies = [{'bad': 1}, self._plan_response()]
        run_job(self.evo, self._job(), 't', self._cfg(), self.files, self._context)
        receipt = self._receipt()
        self.assertEqual(receipt.get('revision'), 1)
        self.assertEqual(len(receipt.get('history_stages') or []), 1)
        original_call = receipt['history_stages'][0]['call_id']
        calls = len(self.calls)
        self.engine.db.execute("DELETE FROM knowledge_calls WHERE id=?", (original_call,))
        self.engine.db.commit()
        with self.assertRaises(Exception):
            resume_publication(self.evo, self.work_id, self.files)
        self.assertEqual(len(self.calls), calls)

    def test_fabricated_history_step_is_rejected_without_extra_calls(self):
        self.plan_replies = [{'bad': 1}, self._plan_response()]
        with mock.patch.object(KnowledgeFiles, '_write_atomic',
                               side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                run_job(self.evo, self._job(), 't', self._cfg(), self.files, self._context)
        calls = len(self.calls)
        bundle = self._bundle()
        bundle['history'][0]['steps']['delta_plan']['response_hash'] = 'forged'
        self._rewrite_bundle(bundle)
        with self.assertRaises(Exception):
            resume_publication(self.evo, self.work_id, self.files)
        self.assertEqual(len(self.calls), calls)

    def test_budget_wait_between_rounds_resumes_without_replaying_first(self):
        self._write_config({'knowledge_daily_call_budget': 2})
        self.plan_replies = [self._plan_response()]
        self.review_replies = ['reject']
        with self.assertRaises(BudgetWait):
            run_job(self.evo, self._job(), 't', self._cfg(), self.files, self._context)
        self.assertEqual(self.plan_calls, 1)
        self.assertEqual(self.review_calls, 1)
        bundle = self._bundle()
        self.assertEqual(bundle.get('revision'), 1)
        self.assertNotIn('delta_plan', bundle['steps'])
        # A restored budget resumes the repaired round only.
        self._write_config({'knowledge_daily_call_budget': 30})
        self.plan_replies = [self._changed_plan_response()]
        self.review_replies = [self._review_response()]
        result = run_job(self.evo, self._job(), 't', self._cfg(), self.files, self._context)
        self.assertTrue(result.get('processed'))
        self.assertEqual(self.plan_calls, 2)
        self.assertEqual(self.review_calls, 2)
        self.assertEqual(len(self.calls), 4)

    def test_repair_publication_failure_resumes_without_extra_calls(self):
        self.plan_replies = [{'bad': 1}, self._plan_response()]
        with mock.patch.object(KnowledgeFiles, '_write_atomic',
                               side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                run_job(self.evo, self._job(), 't', self._cfg(), self.files, self._context)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self._row()['state'], 'publication_pending')
        again = resume_publication(self.evo, self.work_id, self.files)
        self.assertTrue(again.get('processed'))
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self._row()['state'], 'done')

    def test_source_change_before_repaired_dispatch_blocks_without_extra_calls(self):
        self.mode = 'review_reject'
        mutated = {'n': 0}

        def edit():
            if not mutated['n']:
                with self.engine.transaction():
                    self.engine.db.execute(
                        'UPDATE versions SET text=text||? WHERE source_id=? AND version=1',
                        (' 追加修改。', self.sid))
                mutated['n'] = 1

        self.on_review = edit
        with self.assertRaises(Exception):
            run_job(self.evo, self._job(), 't', self._cfg(), self.files, self._context)
        self.assertTrue(mutated['n'])
        self.assertEqual(self.plan_calls, 1)
        self.assertEqual(self.review_calls, 1)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self._changed_ids(), [])

    def test_repair_manual_preservation_kept(self):
        seeded = self.evo.cards.seed(
            'project', 'seed-key', 'knowledge', '主题', '种子卡', '原始正文。')
        cid = seeded['id']
        self.files.publish([cid])
        path = self.evo.cards.get(cid)['path']
        target = self.files.root / path
        self.assertTrue(target.is_file())
        original = self._context
        calls = {'n': 0, 'wrote': False}
        marker = "\n\n%%\ntongle-card:"
        manual_text = '人类在修复轮之前手动修改的内容。'

        def factory():
            calls['n'] += 1
            if calls['n'] >= 6 and not calls['wrote']:
                raw = target.read_text(encoding='utf-8')
                self.assertIn(marker, raw)
                head, sep, tail = raw.rpartition(marker)
                target.write_text(head + '\n\n' + manual_text + sep + tail,
                                  encoding='utf-8')
                calls['wrote'] = True
            return original()

        self.plan_replies = [self._plan_response(), self._changed_plan_response()]
        with self.assertRaises(Exception):
            run_job(self.evo, self._job(), 't', self._cfg(), self.files, factory)
        self.assertTrue(calls['wrote'])
        self.assertEqual(self._changed_ids(), [])
        self.assertEqual(self._row()['state'], 'running')
        self.assertIn(manual_text, target.read_text(encoding='utf-8'))
        card = self.evo.cards.get(cid)
        self.assertIn(manual_text, card['body'])
        self.assertTrue(card['manually_edited'])


if __name__ == '__main__':
    unittest.main()
