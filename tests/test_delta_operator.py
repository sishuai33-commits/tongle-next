"""Synthetic operator exception-completion regressions; no live assets/models.

These tests drive ``tongle.delta_operator`` against a REAL temporary
``Engine``/``Evolution``/``KnowledgeStore``/``KnowledgeFiles`` and real captured
sources. No model is ever called (``model_command`` is a stub that is never
invoked, and we assert the call ledger count is unchanged). Every fixture is
built from real store rows and real ``_source_parts`` boundaries.

Passing proves machine behaviour only, never real semantic model quality: the
operator completion is an EXPLICIT local exception finish, not an automatic LLM
fallback and not a claim of model-reviewed success.
"""
import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path

from tongle.engine import Engine
from tongle.evolution import Evolution
from tongle.knowledge_files import KnowledgeFiles

from tongle import delta_operator as dop
from tongle import delta_receipts
from tongle import delta_migration as dm

V2 = dop.PROTOCOL


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _canon(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.engine = Engine(self.root / 'state')
        self.engine.init(self.root / 'vault')
        self.addCleanup(self.engine.store.close)
        (self.engine.home / 'config.json').write_text(json.dumps({
            'knowledge_mode': 'evolving',
            'model_command': ['stub-never-called'],
            'knowledge_quiet_seconds': 0,
        }))
        self.evo = Evolution(self.engine)
        self.files = KnowledgeFiles(self.engine, self.evo.cards)
        self.calls = []
        self.material_ops = None

    # --------------------------------------------------------- fixtures

    def _capture(self, project, session, role, event_id, text):
        return self.engine.store.capture(project, session, role, event_id, text)

    def _failed_row(self, work_id='w1', project='proj', session='sess',
                    source_id=None, state='failed', lease_token=None,
                    lease_until=0.0, extra_inputs=None):
        source = self.engine.store.source(source_id)
        parts = list(self.evo._source_parts(source))
        inputs = parts if extra_inputs is None else extra_inputs
        self.engine.db.execute(
            "INSERT INTO knowledge_work(id,project,session,inputs,state,attempts,"
            "next_at,lease_token,lease_until,error,created_at,pipeline_protocol)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (work_id, project, session, _canon(inputs), state, 1, 0.0,
             lease_token, lease_until, 'prior failure', 0.0, V2))
        for part in parts:
            self.engine.db.execute(
                'INSERT OR IGNORE INTO knowledge_seen VALUES(?,?,?,?)',
                (part['source_id'], part['version'], part['part'], work_id))
        # A real failed model call already exists in the ledger for this work.
        self.engine.db.execute(
            "INSERT INTO knowledge_calls(id,work_id,stage,day,reserved,actual_input,"
            "actual_output,elapsed,error,created_at)"
            " VALUES('call-fail',?, 'delta_plan','2026-01-01',0,10,0,0.5,'timeout',0.0)",
            (work_id,))
        self.engine.db.execute(
            "INSERT INTO knowledge_failures(work_id,state,error,created_at)"
            " VALUES(?, 'failed','prior semantic failure',0.0)", (work_id,))
        self.engine.db.commit()
        return parts

    def _material_request(self, work_id='w1', source_id=None, grant='grant-1',
                          decision='material', code_sha=None, acceptance_sha=None,
                          review_sha=None, covered_ids=None, operations=None,
                          human_checks=None, rationale=None):
        source = self.engine.store.source(source_id)
        if operations is None and decision == 'material':
            operations = [{
                'op': 'create', 'key': 'op-key-1', 'kind': 'memory',
                'topic': '方案', 'title': '运维结论', 'body': '运维例外完成的结论正文。',
                'state': 'active',
                'evidence': [{'source_id': source['source_id'],
                              'version': source['version'],
                              'quote': source['text']}],
                'reason': '运维显式确认该来源的持久结论',
                'basis_cards': [],
            }]
        return {
            'grant_id': grant,
            'work_id': work_id,
            'code_sha': code_sha or 'a' * 64,
            'acceptance_sha': acceptance_sha or 'b' * 64,
            'review_sha': review_sha or 'c' * 64,
            'rationale': rationale or '运维确认为一次真实失败后的显式例外完成',
            'decision': decision,
            'covered_ids': covered_ids if covered_ids is not None else [],
            'operations': operations if operations is not None else [],
            'human_checks': human_checks if human_checks is not None else [],
        }

    def _call_count(self):
        return self.engine.db.execute(
            'SELECT COUNT(*) FROM knowledge_calls').fetchone()[0]


class OperatorMaterialTests(_Base):
    def setUp(self):
        super().setUp()
        cap = self._capture('proj', 'sess', 'user', 'e-1', '决定采用方案甲。')
        self.sid = cap['source_id']
        self._failed_row(source_id=self.sid)

    def test_material_success_zero_model_calls_and_readonly_replay(self):
        calls_before = self._call_count()
        plan = dop.prepare(self.evo, self._material_request(source_id=self.sid))
        # The plan carries the operation (caller plan may include full data) but
        # the durable audit/meta must not copy the body/rationale/sourcequote.
        self.assertEqual(plan['decision'], 'material')
        result = dop.apply(self.evo, plan, self.files)
        self.assertFalse(result['replayed'])
        self.assertIn('receipt', result)

        row = self.engine.db.execute(
            'SELECT state,inputs,bundle FROM knowledge_work WHERE id=?',
            ('w1',)).fetchone()
        self.assertEqual(row['state'], 'done')
        self.assertEqual(row['inputs'], '[]')
        self.assertIsNone(row['bundle'])
        # No model call was made for the operator completion.
        self.assertEqual(self._call_count(), calls_before)

        card = self.evo.cards.get(result['receipt']['changed_ids'][0])
        self.assertEqual(card['body'], '运维例外完成的结论正文。')

        # The durable completion audit + intent meta are BODY-FREE.
        for state in (dop.OPERATOR_COMPLETE_AUDIT, dop.OPERATOR_INTENT_AUDIT):
            raw = self.engine.db.execute(
                'SELECT error FROM knowledge_failures WHERE work_id=? AND state=?',
                ('w1', state)).fetchone()[0]
            for secret in ('运维例外完成的结论正文', '决定采用方案甲',
                           '运维确认为一次真实失败后的显式例外完成'):
                self.assertNotIn(secret, raw)

        # Read-only done replay through delta_receipts delegation.
        calls = self._call_count()
        verify = delta_receipts.verify_completed(self.evo, 'w1')
        self.assertTrue(verify['verified'])
        self.assertEqual(verify['origin'], dop.OPERATOR_ORIGIN)
        self.assertEqual(verify['processed'], 0)
        self.assertEqual(self._call_count(), calls)

    def test_same_grant_identical_plan_replay_zero_writes(self):
        plan = dop.prepare(self.evo, self._material_request(source_id=self.sid))
        dop.apply(self.evo, plan, self.files)
        audits = self.engine.db.execute(
            'SELECT COUNT(*) FROM knowledge_failures WHERE work_id=?',
            ('w1',)).fetchone()[0]
        cards = self.engine.db.execute(
            'SELECT COUNT(*) FROM knowledge_cards').fetchone()[0]
        replay = dop.apply(self.evo, plan, self.files)
        self.assertTrue(replay['replayed'])
        self.assertEqual(self.engine.db.execute(
            'SELECT COUNT(*) FROM knowledge_failures WHERE work_id=?',
            ('w1',)).fetchone()[0], audits)
        self.assertEqual(self.engine.db.execute(
            'SELECT COUNT(*) FROM knowledge_cards').fetchone()[0], cards)

    def test_same_grant_different_plan_rejected(self):
        plan = dop.prepare(self.evo, self._material_request(source_id=self.sid))
        # Build a DIFFERENT canonical plan for the same grant while the row is
        # still failed (prepare reads only), then commit the first and confirm
        # the second is refused.
        other = dop.prepare(self.evo, self._material_request(
            source_id=self.sid, grant='grant-1',
            rationale='一条完全不同的运维理由文本'))
        self.assertNotEqual(plan['plan_sha'], other['plan_sha'])
        dop.apply(self.evo, plan, self.files)
        with self.assertRaises(ValueError):
            dop.apply(self.evo, other, self.files)

    def test_unknown_request_keys_rejected(self):
        req = self._material_request(source_id=self.sid)
        req['bogus'] = 1
        with self.assertRaises(ValueError):
            dop.prepare(self.evo, req)

    def test_material_requires_operations_and_own_source(self):
        req = self._material_request(source_id=self.sid, operations=[])
        with self.assertRaises(ValueError):
            dop.prepare(self.evo, req)
        # A foreign evidence source (a reference event NOT in the window) is
        # rejected: a validated reference must share project+session.
        other = self._capture('otherproj', 'othersess', 'user', 'e-x', '别的事。')
        bad = self._material_request(source_id=self.sid, operations=[{
            'op': 'create', 'key': 'k', 'kind': 'memory', 'topic': 't',
            'title': 'T', 'body': 'B。', 'state': 'active',
            'evidence': [{'source_id': other['source_id'], 'version': 1,
                          'quote': '别的事。'}],
            'reason': 'r', 'basis_cards': [],
        }])
        with self.assertRaises(ValueError):
            dop.prepare(self.evo, bad)

    def test_source_text_drift_rejected(self):
        plan_request = self._material_request(source_id=self.sid)
        # Advance the source to a new version in place: the stored input no
        # longer matches the current version.
        with self.engine.transaction():
            self.engine.db.execute(
                "INSERT INTO versions(source_id,version,text,original,hash,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (self.sid, 2, '改后的文本。', b'x', 'deadbeef',
                 '2026-02-02T00:00:00+00:00'))
            self.engine.db.execute('UPDATE sources SET current_version=2 WHERE id=?',
                                   (self.sid,))
        with self.assertRaises(ValueError):
            dop.prepare(self.evo, plan_request)

    def test_owner_drift_rejected(self):
        req = self._material_request(source_id=self.sid)
        with self.engine.transaction():
            self.engine.db.execute(
                "UPDATE knowledge_seen SET work_id='other' WHERE source_id=?",
                (self.sid,))
        with self.assertRaises(ValueError):
            dop.prepare(self.evo, req)

    def test_scope_guards_reject_wrong_state_live_lease_and_batch(self):
        req = self._material_request(source_id=self.sid)
        # Wrong protocol / running state.
        with self.engine.transaction():
            self.engine.db.execute("UPDATE knowledge_work SET state='running' WHERE id='w1'")
        with self.assertRaises(ValueError):
            dop.prepare(self.evo, req)
        with self.engine.transaction():
            self.engine.db.execute("UPDATE knowledge_work SET state='failed' WHERE id='w1'")
        # Live lease.
        with self.engine.transaction():
            self.engine.db.execute(
                "UPDATE knowledge_work SET lease_token='t',lease_until=? WHERE id='w1'",
                (time.time() + 600,))
        with self.assertRaises(ValueError):
            dop.prepare(self.evo, req)
        with self.engine.transaction():
            self.engine.db.execute(
                "UPDATE knowledge_work SET lease_token=NULL,lease_until=0 WHERE id='w1'")
        # Existing committed batch.
        with self.engine.transaction():
            self.engine.db.execute(
                "INSERT INTO knowledge_batches(batch_id,project,op_hash,review_hash,"
                "changed_ids,created_at) VALUES('w1','proj','o','r','[]',0.0)")
        with self.assertRaises(ValueError):
            dop.prepare(self.evo, req)

    def test_wrong_protocol_row_rejected(self):
        with self.engine.transaction():
            self.engine.db.execute(
                "UPDATE knowledge_work SET pipeline_protocol='tongle-knowledge-contract-v5'"
                " WHERE id='w1'")
        with self.assertRaises(ValueError):
            dop.prepare(self.evo, self._material_request(source_id=self.sid))


class OperatorCoveredNonmaterialTests(_Base):
    def setUp(self):
        super().setUp()
        cap = self._capture('proj', 'sess', 'user', 'e-2', '事实已经存在于卡片中。')
        self.sid = cap['source_id']
        self._failed_row(source_id=self.sid)
        self.card = self.evo.cards.seed('proj', 'covered-key', 'knowledge',
                                        '主题', '已覆盖卡', '已覆盖的正文。')

    def test_covered_success_no_operations_and_readonly_replay(self):
        plan = dop.prepare(self.evo, self._material_request(
            source_id=self.sid, decision='covered',
            covered_ids=[self.card['id']],
            rationale='该来源已被现有卡片完整覆盖'))
        self.assertEqual(plan['decision'], 'covered')
        self.assertEqual(plan['normalized_operations'], [])
        result = dop.apply(self.evo, plan, self.files)
        batch = self.engine.db.execute(
            'SELECT changed_ids FROM knowledge_batches WHERE batch_id=?',
            ('w1',)).fetchone()
        self.assertEqual(json.loads(batch['changed_ids']), [])
        self.assertEqual(self.engine.db.execute(
            "SELECT state FROM knowledge_work WHERE id='w1'").fetchone()[0], 'done')
        verify = delta_receipts.verify_completed(self.evo, 'w1')
        self.assertEqual(verify['origin'], dop.OPERATOR_ORIGIN)

    def test_covered_requires_proofs_and_rejects_operations(self):
        req = self._material_request(source_id=self.sid, decision='covered',
                                     covered_ids=[])
        with self.assertRaises(ValueError):
            dop.prepare(self.evo, req)
        req2 = self._material_request(source_id=self.sid, decision='covered',
                                      covered_ids=[self.card['id']],
                                      operations=[{'op': 'retire'}])
        with self.assertRaises(ValueError):
            dop.prepare(self.evo, req2)

    def test_nonmaterial_success_no_operations(self):
        plan = dop.prepare(self.evo, self._material_request(
            source_id=self.sid, decision='nonmaterial',
            rationale='该来源无持久价值，运维确认无操作'))
        self.assertEqual(plan['normalized_operations'], [])
        dop.apply(self.evo, plan, self.files)
        verify = delta_receipts.verify_completed(self.evo, 'w1')
        self.assertEqual(verify['origin'], dop.OPERATOR_ORIGIN)

    def test_nonmaterial_rejects_operations(self):
        req = self._material_request(source_id=self.sid, decision='nonmaterial',
                                     operations=[{'op': 'retire'}])
        with self.assertRaises(ValueError):
            dop.prepare(self.evo, req)


class OperatorVerificationTests(_Base):
    def setUp(self):
        super().setUp()
        cap = self._capture('proj', 'sess', 'user', 'e-3', '运维绑定来源。')
        self.sid = cap['source_id']
        self._failed_row(source_id=self.sid)

    def _finish(self):
        plan = dop.prepare(self.evo, self._material_request(source_id=self.sid))
        dop.apply(self.evo, plan, self.files)
        return plan

    def test_forged_completion_audit_without_meta_rejected(self):
        self._finish()
        # Drop the durable meta intent: the audit alone must not verify.
        with self.engine.transaction():
            self.engine.db.execute(
                "DELETE FROM meta WHERE key=?", (dop.OPERATOR_META_PREFIX + 'grant-1',))
        with self.assertRaises(ValueError):
            delta_receipts.verify_completed(self.evo, 'w1')

    def test_mixed_auto_and_operator_completion_rejected(self):
        self._finish()
        with self.engine.transaction():
            self.engine.db.execute(
                "INSERT INTO knowledge_failures(work_id,state,error,created_at)"
                " VALUES('w1',?,'{}',0.0)", (delta_receipts.COMPLETE_AUDIT,))
        with self.assertRaises(ValueError):
            delta_receipts.verify_completed(self.evo, 'w1')

    def test_later_card_and_source_revision_preserves_completion(self):
        self._finish()
        cid = self.engine.db.execute(
            "SELECT changed_ids FROM knowledge_batches WHERE batch_id='w1'").fetchone()[0]
        cid = json.loads(cid)[0]
        self.evo.cards.manual_edit(cid, 1, '人类后续修改的正文。')
        with self.engine.transaction():
            self.engine.db.execute(
                "INSERT INTO versions(source_id,version,text,original,hash,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (self.sid, 2, '后续来源修改。', b'y', 'feed',
                 '2026-03-03T00:00:00+00:00'))
            self.engine.db.execute('UPDATE sources SET current_version=2 WHERE id=?',
                                   (self.sid,))
        verify = delta_receipts.verify_completed(self.evo, 'w1')
        self.assertTrue(verify['verified'])

    def test_call_ledger_digest_recorded_and_checked(self):
        self._finish()
        receipt = dop._unique_operator_audit(self.evo, 'w1',
                                             dop.OPERATOR_COMPLETE_AUDIT)
        self.assertEqual(receipt['calls_before']['count'], 1)
        # A fictitious model provenance (an extra call) must fail verification.
        with self.engine.transaction():
            self.engine.db.execute(
                "INSERT INTO knowledge_calls(id,work_id,stage,day,reserved,elapsed,"
                "error,created_at) VALUES('fake','w1','delta_review','2026-01-01',0,"
                "0.1,'',0.0)")
        with self.assertRaises(ValueError):
            delta_receipts.verify_completed(self.evo, 'w1')


class OperatorResumeTests(_Base):
    def setUp(self):
        super().setUp()
        cap = self._capture('proj', 'sess', 'user', 'e-4', '崩溃恢复来源。')
        self.sid = cap['source_id']

    def test_publication_failure_then_resume_no_duplicate_apply(self):
        self._failed_row(source_id=self.sid)
        plan = dop.prepare(self.evo, self._material_request(source_id=self.sid))
        # Force publication to fail once by pointing the vault at an invalid
        # target: publish raises OSError -> apply leaves operator_pending.
        real_publish = self.files.publish

        def boom(card_ids=None):
            raise OSError('disk full')

        self.files.publish = boom
        try:
            with self.assertRaises(OSError):
                dop.apply(self.evo, plan, self.files)
        finally:
            self.files.publish = real_publish

        row = self.engine.db.execute(
            'SELECT state,inputs,bundle FROM knowledge_work WHERE id=?',
            ('w1',)).fetchone()
        self.assertEqual(row['state'], dop.OPERATOR_STATE)
        # The cache is retained until publication is confirmed.
        self.assertNotEqual(row['bundle'], '[]')
        batch = self.engine.db.execute(
            'SELECT changed_ids FROM knowledge_batches WHERE batch_id=?',
            ('w1',)).fetchone()
        changed = json.loads(batch['changed_ids'])
        self.assertEqual(len(changed), 1)

        # Resume: no second store.apply; the batch changed_ids set is identical.
        calls = self._call_count()
        outcome = dop.resume_pending(self.evo, self.files, limit=3)
        self.assertEqual(outcome['operator_published'], 1)
        self.assertEqual(self._call_count(), calls)
        row = self.engine.db.execute(
            'SELECT state FROM knowledge_work WHERE id=?', ('w1',)).fetchone()
        self.assertEqual(row['state'], 'done')
        batch2 = self.engine.db.execute(
            'SELECT changed_ids FROM knowledge_batches WHERE batch_id=?',
            ('w1',)).fetchone()
        self.assertEqual(json.loads(batch2['changed_ids']), changed)

    def test_malformed_intent_fails_closed(self):
        self._failed_row(source_id=self.sid)
        plan = dop.prepare(self.evo, self._material_request(source_id=self.sid))
        real_publish = self.files.publish
        self.files.publish = lambda card_ids=None: (_ for _ in ()).throw(OSError('x'))
        try:
            with self.assertRaises(OSError):
                dop.apply(self.evo, plan, self.files)
        finally:
            self.files.publish = real_publish
        # Corrupt the meta intent: resume must not guess.
        with self.engine.transaction():
            self.engine.db.execute(
                "UPDATE meta SET value='not json' WHERE key=?",
                (dop.OPERATOR_META_PREFIX + 'grant-1',))
        outcome = dop.resume_pending(self.evo, self.files, limit=3)
        self.assertEqual(outcome['operator_published'], 0)
        self.assertEqual(outcome['operator_pending'], 1)


class OperatorRollbackTests(_Base):
    def setUp(self):
        super().setUp()
        cap = self._capture('proj', 'sess', 'user', 'e-5', '回滚来源。')
        self.sid = cap['source_id']

    def test_unpark_restores_operator_pending(self):
        self._failed_row(source_id=self.sid)
        plan = dop.prepare(self.evo, self._material_request(source_id=self.sid))
        real_publish = self.files.publish
        self.files.publish = lambda card_ids=None: (_ for _ in ()).throw(OSError('x'))
        try:
            with self.assertRaises(OSError):
                dop.apply(self.evo, plan, self.files)
        finally:
            self.files.publish = real_publish
        self.assertEqual(self.engine.db.execute(
            "SELECT state FROM knowledge_work WHERE id='w1'").fetchone()[0],
            dop.OPERATOR_STATE)
        dm.park(self.evo, 'park-1')
        self.assertEqual(self.engine.db.execute(
            "SELECT state FROM knowledge_work WHERE id='w1'").fetchone()[0],
            'protocol_paused')
        dm.unpark(self.evo, 'park-1')
        row = self.engine.db.execute(
            "SELECT state FROM knowledge_work WHERE id='w1'").fetchone()
        self.assertEqual(row['state'], dop.OPERATOR_STATE)


class OperatorManualProtectionTests(_Base):
    def setUp(self):
        super().setUp()
        cap = self._capture('proj', 'sess', 'user', 'e-6', '人工卡保护来源。')
        self.sid = cap['source_id']
        self._failed_row(source_id=self.sid)
        self.card = self.evo.cards.seed('proj', 'manual-key', 'knowledge',
                                        '主题', '人工卡', '人工编辑前的正文。')
        self.evo.cards.manual_edit(self.card['id'], 1, '人工编辑后的正文。')

    def test_manual_target_requires_bound_human_checks(self):
        manual = self.evo.cards.get(self.card['id'])
        source = self.engine.store.source(self.sid)
        op = {
            'op': 'update', 'id': manual['id'],
            'expected_version': manual['version'],
            'body': manual['body'] + ' 追加。', 'state': None, 'title': None,
            'topic': None,
            'evidence': [{'source_id': source['source_id'],
                          'version': source['version'],
                          'quote': source['text']}],
            'reason': '运维更新', 'basis_cards': [],
        }
        # Without human_checks the store refuses the manual update.
        req = self._material_request(source_id=self.sid, operations=[op],
                                     human_checks=[])
        with self.assertRaises(ValueError):
            dop.prepare(self.evo, req)
        # With the correct bound check the store accepts it.
        check = {'id': manual['id'], 'version': manual['version'],
                 'body_sha256': manual['body_sha256'], 'preserved': True,
                 'reason': '运维核对保留人工正文并仅追加内容'}
        req2 = self._material_request(source_id=self.sid, operations=[op],
                                      human_checks=[check])
        plan = dop.prepare(self.evo, req2)
        dop.apply(self.evo, plan, self.files)
        self.assertIn('追加', self.evo.cards.get(manual['id'])['body'])


class OperatorFileProtectionTests(_Base):
    def setUp(self):
        super().setUp()
        cap = self._capture('proj', 'sess', 'user', 'e-7', '文件保护来源。')
        self.sid = cap['source_id']
        self._failed_row(source_id=self.sid)

    def test_newer_manual_file_edit_not_overwritten(self):
        # A card whose file was manually edited AFTER its last publish must not
        # be clobbered by an operator completion of an UNRELATED source.
        card = self.evo.cards.seed('proj', 'file-key', 'knowledge', '主题',
                                   '文件卡', '文件卡正文。')
        self.files.publish([card['id']])
        target = self.files.root / self.evo.cards.get(card['id'])['path']
        # A human changes ONLY the on-disk file (a real manual edit).
        target.write_text('人工改写的磁盘内容。', encoding='utf-8')
        plan = dop.prepare(self.evo, self._material_request(source_id=self.sid))
        dop.apply(self.evo, plan, self.files)
        # The unrelated manual edit survives; the new card is still published.
        self.assertEqual(target.read_text(encoding='utf-8'), '人工改写的磁盘内容。')
        self.assertEqual(self.engine.db.execute(
            "SELECT state FROM knowledge_work WHERE id='w1'").fetchone()[0], 'done')


class OperatorTargetDriftTests(_Base):
    """A same-version body/evidence/basis drift must not satisfy a stale plan."""

    def setUp(self):
        super().setUp()
        cap = self._capture('proj', 'sess', 'user', 'e-t1', '目标卡漂移来源。')
        self.sid = cap['source_id']
        self._failed_row(source_id=self.sid)
        self.card = self.evo.cards.seed('proj', 'target-key', 'knowledge',
                                        '主题', '目标卡', '目标卡原始正文。')

    def _update_request(self):
        source = self.engine.store.source(self.sid)
        op = {
            'op': 'update', 'id': self.card['id'],
            'expected_version': self.card['version'],
            'body': '目标卡更新后的正文。', 'state': None, 'title': None,
            'topic': None,
            'evidence': [{'source_id': source['source_id'],
                          'version': source['version'],
                          'quote': source['text']}],
            'reason': '运维更新目标卡', 'basis_cards': [],
        }
        return self._material_request(source_id=self.sid, operations=[op])

    def test_material_target_same_version_body_drift_rejected(self):
        plan = dop.prepare(self.evo, self._update_request())
        # The plan freezes the CURRENT full target digest (incl. evidence/basis).
        self.assertEqual(len(plan['targets']), 1)
        self.assertIn('card_full_digest', plan['targets'][0])
        # Mutate the body IN PLACE WITHOUT bumping the version: the store's own
        # version check cannot see this, so only the frozen full digest catches
        # it. apply recomputes the plan and must refuse.
        with self.engine.transaction():
            self.engine.db.execute(
                "UPDATE knowledge_cards SET body='同一版本但正文已改。' WHERE id=?",
                (self.card['id'],))
        with self.assertRaises(ValueError):
            dop.apply(self.evo, plan, self.files)
        # No batch was committed: the row is still failed.
        self.assertEqual(self.engine.db.execute(
            "SELECT state FROM knowledge_work WHERE id='w1'").fetchone()[0],
            'failed')

    def test_material_target_basis_drift_rejected(self):
        base = self.evo.cards.seed('proj', 'basis-key', 'knowledge', '主题',
                                   '依据卡', '依据正文。')
        plan = dop.prepare(self.evo, self._update_request())
        # Add a basis reference to the target card (same version) whose digest is
        # bound by the frozen full target digest.
        with self.engine.transaction():
            self.engine.db.execute(
                'INSERT INTO knowledge_dependencies(card_id,basis_id,basis_version)'
                ' VALUES(?,?,?)',
                (self.card['id'], base['id'], base['version']))
        with self.assertRaises(ValueError):
            dop.apply(self.evo, plan, self.files)


class OperatorInheritedEvidenceTests(_Base):
    """Inherited other-session target evidence is allowed; a NEW foreign ref is not."""

    def setUp(self):
        super().setUp()
        cap = self._capture('proj', 'sess', 'user', 'e-t2', '本会话来源。')
        self.sid = cap['source_id']
        # An other-session source in the SAME project: a legitimate historical
        # provenance of the target card, but NOT in this work's window.
        other = self._capture('proj', 'other-session', 'user', 'e-other',
                              '另一个会话中记录的依据。')
        self.other_sid = other['source_id']
        self._failed_row(source_id=self.sid)
        self.card = self.evo.cards.seed(
            'proj', 'inherit-key', 'knowledge', '主题', '继承卡', '继承卡正文。',
            evidence=[{'source_id': self.other_sid, 'version': 1,
                       'quote': '另一个会话中记录的依据。'}])

    def test_inherited_other_session_evidence_allowed_new_foreign_ref_rejected(self):
        source = self.engine.store.source(self.sid)
        op = {
            'op': 'update', 'id': self.card['id'],
            'expected_version': self.card['version'],
            'body': '继承卡更新正文。', 'state': None, 'title': None, 'topic': None,
            'evidence': [
                # Exact persisted target-evidence tuple (inherited, other session)
                {'source_id': self.other_sid, 'version': 1,
                 'quote': '另一个会话中记录的依据。'},
                # Plus the owned source (required to carry the owned ref).
                {'source_id': source['source_id'], 'version': source['version'],
                 'quote': source['text']},
            ],
            'reason': '运维更新并保留历史依据', 'basis_cards': [],
        }
        plan = dop.prepare(self.evo, self._material_request(source_id=self.sid,
                                                            operations=[op]))
        self.assertEqual(len(plan['targets']), 1)
        dop.apply(self.evo, plan, self.files)
        self.assertTrue(delta_receipts.verify_completed(self.evo, 'w1')['verified'])

    def test_new_foreign_session_ref_rejected(self):
        source = self.engine.store.source(self.sid)
        other = self._capture('proj', 'other-session', 'user', 'e-other2',
                              '另一个新的外来依据。')
        op = {
            'op': 'create', 'key': 'foreign-key', 'kind': 'memory',
            'topic': '主题', 'title': '外来卡', 'body': '外来正文。',
            'state': 'active',
            'evidence': [
                {'source_id': source['source_id'], 'version': source['version'],
                 'quote': source['text']},
                # A brand-new other-session reference: NOT inherited, NOT in the
                # frozen window and NOT a live prior-two event of this session.
                {'source_id': other['source_id'], 'version': 1,
                 'quote': '另一个新的外来依据。'},
            ],
            'reason': '尝试借用其他会话新引用', 'basis_cards': [],
        }
        with self.assertRaises(ValueError):
            dop.prepare(self.evo, self._material_request(source_id=self.sid,
                                                         operations=[op]))


class OperatorReferenceFreezeTests(_Base):
    """A frozen new-reference identity must be stable between prepare and apply."""

    def setUp(self):
        super().setUp()
        cap = self._capture('proj', 'sess', 'user', 'e-owned', '本会话当前来源。')
        self.sid = cap['source_id']
        prior = self._capture('proj', 'sess', 'user', 'e-prior',
                              '更早的一条会话记录，含 quote 片段。')
        self.ref_sid = prior['source_id']
        # Make the owned source strictly LATER so the reference is a real live
        # prior-two event of the same session.
        with self.engine.transaction():
            self.engine.db.execute(
                "UPDATE sources SET created_at='2026-01-01T00:00:00+00:00' WHERE id=?",
                (self.ref_sid,))
            self.engine.db.execute(
                "UPDATE sources SET created_at='2026-02-01T00:00:00+00:00' WHERE id=?",
                (self.sid,))
        self._failed_row(source_id=self.sid)

    def _request(self):
        owned = self.engine.store.source(self.sid)
        ref = self.engine.store.source(self.ref_sid)
        op = {
            'op': 'create', 'key': 'ref-key', 'kind': 'memory',
            'topic': '主题', 'title': '引用卡', 'body': '引用正文。',
            'state': 'active',
            'evidence': [
                {'source_id': owned['source_id'], 'version': owned['version'],
                 'quote': owned['text']},
                {'source_id': self.ref_sid, 'version': ref['version'],
                 'quote': '含 quote 片段'},
            ],
            'reason': '运维引用既往前因', 'basis_cards': [],
        }
        return self._material_request(source_id=self.sid, operations=[op])

    def test_reference_fulltext_changed_while_quote_survives_rejected(self):
        plan = dop.prepare(self.evo, self._request())
        # The new reference's full identity/version/textsha is frozen.
        self.assertEqual(len(plan['references']), 1)
        self.assertEqual(plan['references'][0]['source_id'], self.ref_sid)
        # Advance the reference source to a NEW version whose text still
        # contains the quote: the plan's frozen text-sha no longer matches.
        with self.engine.transaction():
            self.engine.db.execute(
                "INSERT INTO versions(source_id,version,text,original,hash,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (self.ref_sid, 2, '更早的一条会话记录，含 quote 片段，但全文已改。',
                 b'x', 'deadbeef', '2026-03-01T00:00:00+00:00'))
            self.engine.db.execute('UPDATE sources SET current_version=2 WHERE id=?',
                                   (self.ref_sid,))
        with self.assertRaises(ValueError):
            dop.apply(self.evo, plan, self.files)


class OperatorPublishGuardTests(_Base):
    """publish_one/resume refuse malformed, paused, drifted states before effect."""

    def setUp(self):
        super().setUp()
        cap = self._capture('proj', 'sess', 'user', 'e-p1', '发布守卫来源。')
        self.sid = cap['source_id']
        self._failed_row(source_id=self.sid)

    def _leave_pending(self):
        plan = dop.prepare(self.evo, self._material_request(source_id=self.sid))
        real_publish = self.files.publish
        self.files.publish = lambda card_ids=None: (_ for _ in ()).throw(OSError('x'))
        try:
            with self.assertRaises(OSError):
                dop.apply(self.evo, plan, self.files)
        finally:
            self.files.publish = real_publish
        self.assertEqual(self.engine.db.execute(
            "SELECT state FROM knowledge_work WHERE id='w1'").fetchone()[0],
            dop.OPERATOR_STATE)
        return plan

    def test_paused_publisher_blocked(self):
        self._leave_pending()
        dm.park(self.evo, 'park-pub')
        with self.assertRaises(ValueError):
            dop.publish_one(self.evo, 'w1', self.files)
        # Still paused: the refused publish performed no state change.
        self.assertEqual(self.engine.db.execute(
            "SELECT state FROM knowledge_work WHERE id='w1'").fetchone()[0],
            'protocol_paused')

    def test_changed_intent_before_resume_no_publish(self):
        self._leave_pending()
        # Change the durable meta intent to valid JSON that no longer matches the
        # intent audit: resume must refuse and NOT publish.
        with self.engine.transaction():
            row = self.engine.db.execute(
                'SELECT value FROM meta WHERE key=?',
                (dop.OPERATOR_META_PREFIX + 'grant-1',)).fetchone()
            obj = json.loads(row['value'])
            obj['changed_ids'] = ['tampered']
            self.engine.db.execute(
                'UPDATE meta SET value=? WHERE key=?',
                (_canon(obj), dop.OPERATOR_META_PREFIX + 'grant-1'))
        outcome = dop.resume_pending(self.evo, self.files, limit=3)
        self.assertEqual(outcome['operator_published'], 0)
        self.assertEqual(outcome['operator_failed'], 1)
        self.assertEqual(self.engine.db.execute(
            "SELECT state FROM knowledge_work WHERE id='w1'").fetchone()[0],
            dop.OPERATOR_STATE)

    def test_call_ledger_drift_after_commit_rejected(self):
        self._leave_pending()
        # A fabricated call after the commit-time snapshot must block finalize.
        with self.engine.transaction():
            self.engine.db.execute(
                "INSERT INTO knowledge_calls(id,work_id,stage,day,reserved,"
                "elapsed,error,created_at) VALUES('drift','w1','delta_plan',"
                "'2026-01-01',0,0.1,'',0.0)")
        with self.assertRaises(ValueError):
            dop.publish_one(self.evo, 'w1', self.files)
        self.assertEqual(self.engine.db.execute(
            "SELECT state FROM knowledge_work WHERE id='w1'").fetchone()[0],
            dop.OPERATOR_STATE)

    def test_missing_intent_unpark_stays_paused(self):
        self._leave_pending()
        dm.park(self.evo, 'park-intent')
        # Drop the durable meta intent: the recorded operator prior state can no
        # longer be validated, so unpark must RAISE and leave it paused.
        with self.engine.transaction():
            self.engine.db.execute(
                'DELETE FROM meta WHERE key=?',
                (dop.OPERATOR_META_PREFIX + 'grant-1',))
        with self.assertRaises(ValueError):
            dm.unpark(self.evo, 'park-intent')
        self.assertEqual(self.engine.db.execute(
            "SELECT state FROM knowledge_work WHERE id='w1'").fetchone()[0],
            'protocol_paused')


class OperatorProofAndScopeTests(_Base):
    """Missing applied proof / owned parts are refused; historical scope holds."""

    def setUp(self):
        super().setUp()
        cap = self._capture('proj', 'sess', 'user', 'e-v1', '证明检查来源。')
        self.sid = cap['source_id']
        self._failed_row(source_id=self.sid)

    def _finish(self):
        plan = dop.prepare(self.evo, self._material_request(source_id=self.sid))
        dop.apply(self.evo, plan, self.files)
        return plan

    def _tamper_receipt(self, mutate):
        with self.engine.transaction():
            row = self.engine.db.execute(
                "SELECT error FROM knowledge_failures WHERE work_id='w1' AND"
                " state=?", (dop.OPERATOR_COMPLETE_AUDIT,)).fetchone()
            obj = json.loads(row['error'])
            mutate(obj)
            self.engine.db.execute(
                "UPDATE knowledge_failures SET error=? WHERE work_id='w1' AND"
                " state=?", (_canon(obj), dop.OPERATOR_COMPLETE_AUDIT))

    def test_missing_applied_proof_rejected(self):
        self._finish()

        def drop(obj):
            obj['applied_cards'] = []

        self._tamper_receipt(drop)
        with self.assertRaises(ValueError):
            delta_receipts.verify_completed(self.evo, 'w1')

    def test_missing_owned_part_rejected(self):
        self._finish()
        # Drop one owned seen part: the recomputed authoritative part set no
        # longer matches, so verification must refuse.
        with self.engine.transaction():
            self.engine.db.execute(
                'DELETE FROM knowledge_seen WHERE source_id=? AND version=? AND part=0',
                (self.sid, 1))
        with self.assertRaises(ValueError):
            delta_receipts.verify_completed(self.evo, 'w1')

    def test_tampered_owned_part_sha_rejected(self):
        self._finish()

        def tamper(obj):
            obj['source']['parts'][0]['sha256'] = 'f' * 64

        self._tamper_receipt(tamper)
        with self.assertRaises(ValueError):
            delta_receipts.verify_completed(self.evo, 'w1')

    def test_project_scope_changed_after_done_historical_valid(self):
        self._finish()
        # A legitimate later change to the project's CURRENT permissions must not
        # erase a past completion: the historical scope binding is compared
        # across intent/meta/receipt, not against the live fingerprint.
        (self.engine.home / 'config.json').write_text(json.dumps({
            'knowledge_mode': 'evolving',
            'model_command': ['stub-never-called'],
            'knowledge_quiet_seconds': 0,
            'knowledge_project_topics': {'proj': ['主题']},
        }))
        verify = delta_receipts.verify_completed(self.evo, 'w1')
        self.assertTrue(verify['verified'])

    def test_mixed_structural_binding_mismatch_rejected(self):
        self._finish()

        def tamper(obj):
            obj['targets'] = [{'id': 'forged', 'version': 1,
                               'state': 'active', 'card_digest': 'a' * 64,
                               'card_full_digest': 'b' * 64}]

        self._tamper_receipt(tamper)
        with self.assertRaises(ValueError):
            delta_receipts.verify_completed(self.evo, 'w1')


if __name__ == '__main__':
    unittest.main()
