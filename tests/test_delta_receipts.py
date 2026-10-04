"""Synthetic completion-receipt regressions; no live assets or models.

These tests hand-author a *labelled* completed v2 fixture: a real temp
Engine/Store, real KnowledgeStore cards/batches/revisions, a real
KnowledgeFiles publication, two real ``knowledge_calls`` ledger rows and their
real ``delta_stage_v1`` audits. The delta-execution pipeline is deliberately
NOT driven here -- the work row is completed manually -- so passing proves
receipt build/verify behaviour only, never real semantic model quality and not
that any particular executor wires the finalization.
"""
import json
import tempfile
import time
import unittest
from pathlib import Path

from tongle import delta_contract, source_windows
from tongle.engine import Engine
from tongle.knowledge_files import KnowledgeFiles
from tongle.knowledge_store import KnowledgeStore

from tongle.delta_receipts import (
    COMPLETE_AUDIT,
    PROTOCOL,
    RECEIPT_SCHEMA,
    _hash,
    build_receipt,
    verify_completed,
)

_WORK_SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge_work (
 id TEXT PRIMARY KEY, project TEXT NOT NULL, session TEXT NOT NULL, inputs TEXT NOT NULL,
 state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0,
 lease_token TEXT, lease_until REAL NOT NULL DEFAULT 0, proposal TEXT, review TEXT, bundle TEXT,
 error TEXT, created_at REAL NOT NULL, finished_at REAL,
 pipeline_protocol TEXT NOT NULL DEFAULT 'tongle-knowledge-contract-v5'
);
CREATE TABLE IF NOT EXISTS knowledge_seen (
 source_id TEXT NOT NULL, version INTEGER NOT NULL, part INTEGER NOT NULL,
 work_id TEXT, PRIMARY KEY(source_id,version,part)
);
CREATE TABLE IF NOT EXISTS knowledge_calls (
 id TEXT PRIMARY KEY, work_id TEXT NOT NULL, stage TEXT NOT NULL, day TEXT NOT NULL,
 reserved INTEGER NOT NULL, actual_input INTEGER, actual_output INTEGER,
 elapsed REAL, error TEXT, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge_failures (
 work_id TEXT NOT NULL, state TEXT NOT NULL, error TEXT NOT NULL, created_at REAL NOT NULL
);
"""

SOURCE_TEXT = '决定：采用方案甲。'
SEED_BODY = '背景信息。'
SEED_TITLE = '背景卡'
CREATE_BODY = '采用方案甲并记录理由。'
CREATE_TITLE = '方案甲'
REVIEW_PROSE = '证据完整且语义一致'


class _Evo:
    """Minimal holder exposing exactly the attributes the receipt module uses."""

    def __init__(self, engine, cards):
        self.engine = engine
        self.db = engine.db
        self.cards = cards


class DeltaReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        (self.root / 'state').mkdir()
        (self.root / 'vault').mkdir()
        self.engine = Engine(self.root / 'state')
        self.engine.init(self.root / 'vault')
        self.addCleanup(self.engine.store.close)
        for stmt in _WORK_SCHEMA.split(';'):
            if stmt.strip():
                self.engine.db.execute(stmt)
        self.engine.db.commit()
        self.cards = KnowledgeStore(self.engine)
        self.evo = _Evo(self.engine, self.cards)
        self.files = KnowledgeFiles(self.engine, self.cards)

        receipt = self.engine.store.capture('project', 'session', 'user', 'evt', SOURCE_TEXT)
        self.sid = receipt['source_id']
        self.work_id = 'w1'
        source = self.engine.store.source(self.sid)
        self.inputs = [{
            'source_id': self.sid, 'version': source['version'], 'part': 0,
            'role': source['role'], 'kind': source['kind'], 'start': 0,
            'end': len(source['text']), 'source_length': len(source['text']),
            'text': source['text'], 'partial': False,
        }]
        self.engine.db.execute(
            "INSERT INTO knowledge_work(id,project,session,inputs,state,pipeline_protocol,created_at)"
            " VALUES(?,?,?,?,'publication_pending',?,?)",
            (self.work_id, 'project', 'session',
             json.dumps(self.inputs, ensure_ascii=False), PROTOCOL, time.time()))
        self.engine.db.execute(
            "INSERT INTO knowledge_seen VALUES(?,?,?,?)", (self.sid, 1, 0, self.work_id))
        self.engine.db.commit()
        self.read_cid = None
        self.applied_cid = None

    # ------------------------------------------------------------ fixtures

    def _context(self, cards):
        source = self.engine.store.source(self.sid)
        parts = [{
            'source_id': source['source_id'], 'version': source['version'], 'part': 0,
            'role': source['role'], 'kind': source['kind'], 'text': source['text'],
            'session': source['session'],
            'ledger_captured_at': source['created_at'],
            'ledger_version_at': source['version_created_at'],
            'responsibility': 'owned',
        }]
        window = source_windows.freeze_window('project', parts)
        return delta_contract.build_context(window, cards)

    def _plan_response(self):
        return {
            'changes': [{
                'ref': 'n1', 'action': 'create', 'source_refs': ['s1'],
                'reason': '明确的新决定需要持久保存', 'title': CREATE_TITLE,
                'topic': '方案', 'body': CREATE_BODY,
            }],
            'dispositions': [{
                'source_refs': ['s1'], 'decision': 'material',
                'change_refs': ['n1'], 'card_refs': [],
                'reason': '该来源片段形成可持久保存的新决定',
            }],
        }

    def _noop_response(self):
        return {
            'changes': [],
            'dispositions': [{
                'source_refs': ['s1'], 'decision': 'nonmaterial',
                'change_refs': [], 'card_refs': [],
                'reason': '该来源片段无持久价值',
            }],
        }

    def _review_response(self, count):
        return {
            'checks': [{'index': i, 'E': 'pass', 'Q': 'pass', 'T': 'pass',
                        'N': 'pass', 'reason': REVIEW_PROSE} for i in range(count)],
            'whole_window': {'status': 'pass', 'reason': '窗口完整复核通过'},
            'human_preservation': [],
        }

    def _insert_call(self, call_id, stage):
        self.engine.db.execute(
            "INSERT INTO knowledge_calls(id,work_id,stage,day,reserved,actual_input,"
            " actual_output,elapsed,error,created_at) VALUES(?,?,?,?,?,?,?,?,NULL,?)",
            (call_id, self.work_id, stage, '2026-01-01', 0, 10, 10, 0.5, time.time()))

    def _insert_stage_audit(self, call_id, stage, binding, step):
        obj = {'protocol': PROTOCOL, 'work_id': self.work_id, 'stage': stage,
               'binding': binding, 'payload_hash': step['payload_hash'],
               'response_hash': step['response_hash'], 'call_id': call_id}
        self.engine.db.execute(
            "INSERT INTO knowledge_failures(work_id,state,error,created_at) VALUES(?,?,?,?)",
            (self.work_id, 'delta_stage_v1',
             json.dumps(obj, ensure_ascii=False, separators=(',', ':')), time.time()))

    def _complete(self, plan_response=None, seed_cards=True):
        cards = []
        if seed_cards:
            seeded = self.cards.seed('project', 'seed-key', 'knowledge',
                                     '背景', SEED_TITLE, SEED_BODY)
            self.read_cid = seeded['id']
            self.files.publish([self.read_cid])
            cards = self.cards.list('project')
        context = self._context(cards)
        plan_response = plan_response or self._plan_response()
        plan = delta_contract.compile_plan(context, plan_response)
        review_response = self._review_response(len(plan['dispositions']))
        approval = delta_contract.approve_review(context, plan, review_response)
        applied = self.cards.apply(self.work_id, 'project',
                                   plan['operations'] or [], approval)
        changed = list(applied['changed_ids'])
        if changed:
            self.files.publish(changed)
            self.applied_cid = changed[0]
        versions = {cid: self.cards.get(cid)['version'] for cid in changed}
        scope = 'f' * 64
        binding = _hash({'scope': scope, 'binding': context['binding']})
        steps = {
            'delta_plan': {'payload_hash': _hash({'stage': 'delta_plan'}),
                           'response_hash': _hash(plan_response),
                           'response': plan_response, 'call_id': 'call-plan'},
            'delta_review': {'payload_hash': _hash({'stage': 'delta_review'}),
                             'response_hash': _hash(review_response),
                             'response': review_response, 'call_id': 'call-review'},
        }
        bundle = {'protocol': PROTOCOL, 'binding': binding, 'scope': scope,
                  'context': context, 'steps': steps}
        self.engine.db.execute(
            "UPDATE knowledge_work SET bundle=? WHERE id=?",
            (json.dumps(bundle, ensure_ascii=False, separators=(',', ':')), self.work_id))
        self._insert_call('call-plan', 'delta_plan')
        self._insert_call('call-review', 'delta_review')
        self._insert_stage_audit('call-plan', 'delta_plan', binding, steps['delta_plan'])
        self._insert_stage_audit('call-review', 'delta_review', binding, steps['delta_review'])
        self.engine.db.commit()
        self.bundle = bundle
        self.plan = plan
        self.approval = approval
        self.changed_ids = changed
        self.applied_versions = versions
        return bundle, plan, approval, changed, versions

    def _finalize(self):
        receipt = build_receipt(self.evo, self.work_id, self.bundle, self.plan,
                                self.approval, self.changed_ids, self.applied_versions)
        self.receipt = receipt
        with self.engine.transaction():
            self.engine.db.execute(
                "INSERT INTO knowledge_failures(work_id,state,error,created_at) VALUES(?,?,?,?)",
                (self.work_id, COMPLETE_AUDIT,
                 json.dumps(receipt, ensure_ascii=False, separators=(',', ':')),
                 time.time()))
            self.engine.db.execute(
                "UPDATE knowledge_work SET state='done', inputs='[]', proposal=NULL,"
                " review=NULL, bundle=NULL, finished_at=? WHERE id=?",
                (time.time(), self.work_id))
        return receipt

    def _stored_receipt(self):
        row = self.engine.db.execute(
            "SELECT error FROM knowledge_failures WHERE work_id=? AND state=?",
            (self.work_id, COMPLETE_AUDIT)).fetchone()
        return json.loads(row['error'])

    def _rewrite_receipt(self, receipt):
        self.engine.db.execute(
            "UPDATE knowledge_failures SET error=? WHERE work_id=? AND state=?",
            (json.dumps(receipt, ensure_ascii=False, separators=(',', ':')),
             self.work_id, COMPLETE_AUDIT))
        self.engine.db.commit()

    # -------------------------------------------------------------- tests

    def test_build_receipt_is_body_free_and_verifies(self):
        # build_receipt itself must not mark done or erase anything.
        self._complete()
        receipt = build_receipt(self.evo, self.work_id, self.bundle, self.plan,
                                self.approval, self.changed_ids, self.applied_versions)
        row = self.engine.db.execute(
            "SELECT state,bundle FROM knowledge_work WHERE id=?", (self.work_id,)).fetchone()
        self.assertEqual(row['state'], 'publication_pending')
        self.assertIsNotNone(row['bundle'])
        self._finalize()

        self.assertEqual(receipt['schema'], RECEIPT_SCHEMA)
        self.assertEqual(receipt['protocol'], PROTOCOL)
        self.assertEqual(receipt['kind'], 'applied')
        self.assertEqual(receipt['changed_ids'], self.changed_ids)
        self.assertEqual(set(receipt['applied_versions']), set(self.changed_ids))
        self.assertEqual(len(receipt['stages']), 2)
        # The body-free receipt records the real part number for each source.
        self.assertEqual(receipt['window_sources'][0]['part'], 0)
        self.assertEqual(receipt['revision'], 0)
        self.assertIsNone(receipt['repair'])
        self.assertEqual(receipt['history_stages'], [])

        # No source bytes, card body, quote or model prose may appear anywhere.
        blob = json.dumps(receipt, ensure_ascii=False)
        for secret in (SOURCE_TEXT, CREATE_BODY, CREATE_TITLE, SEED_BODY,
                       SEED_TITLE, REVIEW_PROSE, '方案甲'):
            self.assertNotIn(secret, blob)

        verified = verify_completed(self.evo, self.work_id)
        self.assertTrue(verified['verified'])
        self.assertEqual(verified['processed'], 0)
        self.assertTrue(verified['replayed'])
        self.assertEqual(verified['receipt'], self.receipt)
        self.assertEqual(self.receipt['work_id'], self.work_id)

    def test_verify_is_readonly_replay(self):
        self._complete()
        self._finalize()
        calls = self.engine.db.execute("SELECT COUNT(*) FROM knowledge_calls").fetchone()[0]
        files = self.engine.db.execute("SELECT COUNT(*) FROM knowledge_files").fetchone()[0]
        first = verify_completed(self.evo, self.work_id)
        second = verify_completed(self.evo, self.work_id)
        self.assertEqual(first['receipt'], second['receipt'])
        self.assertEqual(
            self.engine.db.execute("SELECT COUNT(*) FROM knowledge_calls").fetchone()[0], calls)
        self.assertEqual(
            self.engine.db.execute("SELECT COUNT(*) FROM knowledge_files").fetchone()[0], files)
        row = self.engine.db.execute(
            "SELECT state,bundle FROM knowledge_work WHERE id=?", (self.work_id,)).fetchone()
        self.assertEqual(row['state'], 'done')
        self.assertIsNone(row['bundle'])

    def test_later_source_edit_or_revocation_preserves_proof(self):
        self._complete()
        self._finalize()
        # A later version and an explicit revocation must not invalidate the
        # proof that the PAST commit happened against version 1.
        with self.engine.transaction():
            self.engine.db.execute(
                "INSERT INTO versions(source_id,version,text,original,hash,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (self.sid, 2, '后续修改。', b'later', 'deadbeef',
                 '2026-02-02T00:00:00+00:00'))
            self.engine.db.execute(
                "UPDATE sources SET current_version=2 WHERE id=?", (self.sid,))
            self.engine.db.execute(
                "INSERT OR REPLACE INTO source_status(source_id,state,updated_at)"
                " VALUES(?,?,?)", (self.sid, 'revoked', time.time()))
        verify_completed(self.evo, self.work_id)

    def test_manual_card_revision_preserves_old_proof(self):
        self._complete()
        self._finalize()
        # Both the read card and the applied card advance by a real manual edit;
        # the historical revisions keep the committed versions provable.
        self.assertIsNotNone(self.read_cid)
        self.assertIsNotNone(self.applied_cid)
        self.cards.manual_edit(self.read_cid, 1, '人类修改后的背景正文。')
        self.cards.manual_edit(self.applied_cid, 1, '人类修改后的方案正文。')
        verify_completed(self.evo, self.work_id)

    def test_missing_historical_card_version_rejected(self):
        self._complete(seed_cards=False)
        self._finalize()
        # Advance the live card without any historical revision: the applied
        # version can no longer be proven.
        self.engine.db.execute(
            "UPDATE knowledge_cards SET version=version+1 WHERE id=?", (self.applied_cid,))
        self.engine.db.commit()
        with self.assertRaises(Exception):
            verify_completed(self.evo, self.work_id)

    def test_mutated_historical_card_body_rejected(self):
        self._complete(seed_cards=False)
        self._finalize()
        self.engine.db.execute(
            "UPDATE knowledge_cards SET body=? WHERE id=?",
            ('被篡改的正文。', self.applied_cid))
        self.engine.db.commit()
        with self.assertRaises(Exception):
            verify_completed(self.evo, self.work_id)

    def test_forged_receipt_call_id_rejected(self):
        self._complete()
        self._finalize()
        receipt = self._stored_receipt()
        receipt['stages'][0]['call_id'] = 'forged-call'
        self._rewrite_receipt(receipt)
        with self.assertRaises(Exception):
            verify_completed(self.evo, self.work_id)

    def test_wrong_changed_ids_rejected(self):
        self._complete()
        self._finalize()
        receipt = self._stored_receipt()
        receipt['changed_ids'] = list(receipt['changed_ids']) + ['extra']
        self._rewrite_receipt(receipt)
        with self.assertRaises(Exception):
            verify_completed(self.evo, self.work_id)

    def test_missing_stage_call_rejected(self):
        self._complete()
        self._finalize()
        self.engine.db.execute("DELETE FROM knowledge_calls WHERE id='call-review'")
        self.engine.db.commit()
        with self.assertRaises(Exception):
            verify_completed(self.evo, self.work_id)

    def test_zero_op_requires_real_batch_and_calls(self):
        self._complete(plan_response=self._noop_response())
        self.assertEqual(self.changed_ids, [])
        self._finalize()
        receipt = verify_completed(self.evo, self.work_id)['receipt']
        self.assertEqual(receipt['kind'], 'no_op')
        self.assertEqual(receipt['changed_ids'], [])
        batch = self.engine.db.execute(
            "SELECT changed_ids FROM knowledge_batches WHERE batch_id=?", (self.work_id,)).fetchone()
        self.assertEqual(json.loads(batch['changed_ids']), [])
        # Without the real batch the empty completion is not provable.
        self.engine.db.execute("DELETE FROM knowledge_batches WHERE batch_id=?", (self.work_id,))
        self.engine.db.commit()
        with self.assertRaises(Exception):
            verify_completed(self.evo, self.work_id)

    def test_zero_op_without_stage_calls_rejected(self):
        self._complete(plan_response=self._noop_response())
        self._finalize()
        self.engine.db.execute("DELETE FROM knowledge_calls WHERE id='call-plan'")
        self.engine.db.commit()
        with self.assertRaises(Exception):
            verify_completed(self.evo, self.work_id)

    def test_build_receipt_rejects_malformed_applied_versions(self):
        self._complete()
        with self.assertRaises(Exception):
            build_receipt(self.evo, self.work_id, self.bundle, self.plan,
                          self.approval, self.changed_ids, {})
        bad = dict(self.applied_versions)
        bad[self.changed_ids[0]] = 0
        with self.assertRaises(Exception):
            build_receipt(self.evo, self.work_id, self.bundle, self.plan,
                          self.approval, self.changed_ids, bad)

    def test_current_protocol_v5_refuses_verification(self):
        self._complete()
        self._finalize()
        self.engine.db.execute(
            "UPDATE knowledge_work SET pipeline_protocol=? WHERE id=?",
            ('tongle-knowledge-contract-v5', self.work_id))
        self.engine.db.commit()
        with self.assertRaises(Exception):
            verify_completed(self.evo, self.work_id)

    def test_applied_proof_version_must_equal_applied_version(self):
        self._complete(seed_cards=False)
        self._finalize()
        cid = self.applied_cid
        # A real manual edit advances the live card to version 2, keeping the
        # committed version 1 provable in knowledge_revisions.
        self.cards.manual_edit(cid, 1, '人类修改后的正文。')
        receipt = self._stored_receipt()
        self.assertEqual(receipt['applied_cards'][0]['version'], 1)
        receipt['applied_versions'][cid] = 2
        self._rewrite_receipt(receipt)
        with self.assertRaises(Exception):
            verify_completed(self.evo, self.work_id)

    def test_tampered_span_position_rejected(self):
        self._complete()
        self._finalize()
        receipt = self._stored_receipt()
        receipt['window_sources'][0]['spans'][0]['position'] = 99
        self._rewrite_receipt(receipt)
        with self.assertRaises(Exception):
            verify_completed(self.evo, self.work_id)

    def test_exact_owned_coverage_required(self):
        self._complete()
        self._finalize()
        receipt = self._stored_receipt()
        self.assertTrue(receipt['owned_refs'])
        receipt['owned_refs'] = []
        self._rewrite_receipt(receipt)
        with self.assertRaises(Exception):
            verify_completed(self.evo, self.work_id)

    def test_extra_owned_ref_rejected(self):
        self._complete()
        self._finalize()
        receipt = self._stored_receipt()
        receipt['owned_refs'] = list(receipt['owned_refs']) + ['s999']
        self._rewrite_receipt(receipt)
        with self.assertRaises(Exception):
            verify_completed(self.evo, self.work_id)

    def test_unresolved_disposition_rejected(self):
        self._complete()
        self._finalize()
        receipt = self._stored_receipt()
        self.assertTrue(receipt['dispositions'])
        receipt['dispositions'][0]['decision'] = 'unresolved'
        receipt['dispositions'][0]['change_refs'] = []
        self._rewrite_receipt(receipt)
        with self.assertRaises(Exception):
            verify_completed(self.evo, self.work_id)

    def test_tampered_stage_binding_rejected(self):
        self._complete()
        self._finalize()
        receipt = self._stored_receipt()
        receipt['stages'][0]['binding'] = 'f' * 64
        self._rewrite_receipt(receipt)
        with self.assertRaises(Exception):
            verify_completed(self.evo, self.work_id)


if __name__ == '__main__':
    unittest.main()
