"""Integration tests for the bounded v2 operator migration / rollback library.

Drives ``tongle.delta_migration`` against a REAL temporary
``Engine``/``Evolution``/``KnowledgeStore`` and real captured sources. No model
is ever called: the module and these tests perform no model dispatch, and every
fixture is built from real store rows and real ``_source_parts`` boundaries.

Covers: multi-source parent -> unique children ownership, multi-part
completeness, unchanged old audits/calls, exact replay with zero writes after a
child evolves, input/source/owner/required-card drift rejection, partial
outside-owner rejection, ordinary ``done`` refusal, corrective no-op audit exact
reconstruction (success) versus changed batch / missing completion (failure),
atomic rollback on an injected SQLite trigger failure, and park/unpark with
lease, publication-pending and paused-cache-drift handling.
"""
import hashlib
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from tongle.engine import Engine
from tongle.evolution import Evolution

from tongle import delta_migration as dm

LEGACY = dm.LEGACY_PROTOCOL
V2 = dm.V2_PROTOCOL


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

    # --------------------------------------------------------- fixtures

    def _capture(self, project, session, role, event_id, text):
        return self.engine.store.capture(project, session, role, event_id, text)

    def _legacy_row(self, work_id, project, session, inputs, state='pending',
                    attempts=0, next_at=0.0, lease_token=None, lease_until=0.0,
                    error=None):
        self.engine.db.execute(
            "INSERT INTO knowledge_work(id,project,session,inputs,state,attempts,"
            "next_at,lease_token,lease_until,error,created_at,pipeline_protocol)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (work_id, project, session, _canon(inputs), state, attempts, next_at,
             lease_token, lease_until, error, 0.0, LEGACY))
        for part in inputs:
            self.engine.db.execute(
                'INSERT OR IGNORE INTO knowledge_seen VALUES(?,?,?,?)',
                (part['source_id'], part['version'], part['part'], work_id))
        self.engine.db.commit()

    def _authoritative(self, source_id):
        source = self.engine.store.source(source_id)
        return source, list(self.evo._source_parts(source))

    def _request(self, grant='grant-a', parents=None, cards=None,
                 code_sha=None, acceptance_sha=None):
        return {
            'grant_id': grant,
            'code_sha': code_sha or 'a' * 64,
            'acceptance_sha': acceptance_sha or 'b' * 64,
            'parents': parents if parents is not None else [],
            'required_cards': cards if cards is not None else {},
        }

    def _count(self, sql, params=()):
        return self.engine.db.execute(sql, params).fetchone()[0]


class UncommittedMigrationTests(_Base):
    def test_multi_source_parent_to_unique_children_and_ownership(self):
        src_a = self._capture('proj', 'sess', 'user', 'e-a', 'alpha statement')
        src_b = self._capture('proj', 'sess', 'user', 'e-b', 'beta statement')
        a_source, a_parts = self._authoritative(src_a['source_id'])
        b_source, b_parts = self._authoritative(src_b['source_id'])
        self._legacy_row('p1', 'proj', 'sess', a_parts + b_parts, state='failed')

        plan = dm.prepare(self.evo, self._request(
            parents=[{'id': 'p1', 'mode': 'uncommitted'}]))
        self.assertEqual(len(plan['children']), 2)
        child_ids = {c['child_id'] for c in plan['children']}
        self.assertEqual(len(child_ids), 2)
        # No body/prose leaks into the plan output.
        blob = _canon(plan)
        for forbidden in ('alpha statement', 'beta statement'):
            self.assertNotIn(forbidden, blob)

        receipt = dm.apply(self.evo, plan)
        self.assertTrue(receipt['responsibility_transferred'])
        self.assertFalse(receipt['semantic_completion'])
        self.assertFalse(receipt['replayed'])

        for child in plan['children']:
            row = self.engine.db.execute(
                'SELECT * FROM knowledge_work WHERE id=?', (child['child_id'],)).fetchone()
            self.assertEqual(row['state'], 'pending')
            self.assertEqual(row['pipeline_protocol'], V2)
            # Reconstructed inputs equal the real current source parts.
            source, parts = self._authoritative(child['source_id'])
            parsed = json.loads(row['inputs'])
            self.assertEqual([p['text'] for p in parsed], [p['text'] for p in parts])

        # Ownership fully transferred, no claim left on the parent.
        parent_claims = self._count(
            'SELECT COUNT(*) FROM knowledge_seen WHERE work_id=?', ('p1',))
        self.assertEqual(parent_claims, 0)
        for child in plan['children']:
            owned = self._count(
                'SELECT COUNT(*) FROM knowledge_seen WHERE work_id=?',
                (child['child_id'],))
            self.assertGreater(owned, 0)

        parent = self.engine.db.execute(
            'SELECT * FROM knowledge_work WHERE id=?', ('p1',)).fetchone()
        self.assertEqual(parent['state'], 'obsolete')
        self.assertIn('responsibility transferred: grant-a', parent['error'])
        self.assertIsNone(parent['lease_token'])

    def test_multi_part_completeness_and_partial_digest_binding(self):
        # >10000 chars with a paragraph boundary forces several real parts.
        text = ('para one\n\n' * 1200) + ('para two\n\n' * 1200)
        cap = self._capture('proj', 'sess', 'user', 'e-multi', text)
        source, parts = self._authoritative(cap['source_id'])
        self.assertGreater(len(parts), 1)
        self._legacy_row('p1', 'proj', 'sess', parts, state='pending')

        plan = dm.prepare(self.evo, self._request(
            parents=[{'id': 'p1', 'mode': 'uncommitted'}]))
        child = plan['children'][0]
        self.assertEqual(len(child['parts']), len(parts))
        self.assertEqual(set(child['owner_map'].keys()), {str(p['part']) for p in parts})

        # Part digest is bound: flipping one part's text sha makes the plan stale.
        drift = json.loads(_canon(plan))
        drift['children'][0]['parts'][1]['sha256'] = 'c' * 64
        drift['plan_sha'] = dm._hash({k: v for k, v in drift.items() if k != 'plan_sha'})
        with self.assertRaises(ValueError):
            dm.apply(self.evo, drift)

    def test_unchanged_old_audits_and_calls_after_apply(self):
        cap = self._capture('proj', 'sess', 'user', 'e-x', 'material')
        source, parts = self._authoritative(cap['source_id'])
        self._legacy_row('p1', 'proj', 'sess', parts, state='retry', attempts=2)
        self.engine.db.execute(
            "INSERT INTO knowledge_calls(id,work_id,stage,day,reserved,actual_input,"
            "actual_output,elapsed,error,created_at) VALUES('call-1','p1','select',"
            "'2026-01-01',0,10,10,0.5,NULL,0.0)")
        self.engine.db.execute(
            "INSERT INTO knowledge_failures(work_id,state,error,created_at)"
            " VALUES('p1','retry','prior transient',0.0)")
        self.engine.db.commit()
        calls_before = self.engine.db.execute(
            'SELECT id,work_id,stage,actual_input,actual_output FROM knowledge_calls'
            ' ORDER BY id').fetchall()
        audits_before = self.engine.db.execute(
            "SELECT state,error FROM knowledge_failures WHERE work_id='p1'"
            " AND state='retry' ORDER BY created_at").fetchall()

        plan = dm.prepare(self.evo, self._request(
            parents=[{'id': 'p1', 'mode': 'uncommitted'}]))
        dm.apply(self.evo, plan)

        calls_after = self.engine.db.execute(
            'SELECT id,work_id,stage,actual_input,actual_output FROM knowledge_calls'
            ' ORDER BY id').fetchall()
        self.assertEqual([tuple(r) for r in calls_before], [tuple(r) for r in calls_after])
        audits_after = self.engine.db.execute(
            "SELECT state,error FROM knowledge_failures WHERE work_id='p1'"
            " AND state='retry' ORDER BY created_at").fetchall()
        self.assertEqual([tuple(r) for r in audits_before], [tuple(r) for r in audits_after])
        # Parent attempts preserved, not reset.
        self.assertEqual(self._count(
            'SELECT attempts FROM knowledge_work WHERE id=?', ('p1',)), 2)

    def test_exact_replay_zero_writes_even_after_child_evolves(self):
        cap = self._capture('proj', 'sess', 'user', 'e-y', 'content')
        source, parts = self._authoritative(cap['source_id'])
        self._legacy_row('p1', 'proj', 'sess', parts, state='pending')
        plan = dm.prepare(self.evo, self._request(
            parents=[{'id': 'p1', 'mode': 'uncommitted'}]))
        first = dm.apply(self.evo, plan)
        self.assertFalse(first['replayed'])

        child_id = plan['children'][0]['child_id']
        # The child evolves after the transfer (legacy v5 done semantics).
        self.engine.db.execute(
            "UPDATE knowledge_work SET state='done',inputs='[]',error=NULL"
            " WHERE id=?", (child_id,))
        self.engine.db.commit()
        audits_before = self._count(
            'SELECT COUNT(*) FROM knowledge_failures WHERE work_id=?', (child_id,))

        replay = dm.apply(self.evo, plan)
        self.assertTrue(replay['replayed'])
        self.assertEqual(replay['plan_sha'], plan['plan_sha'])
        self.assertEqual(replay['parents'], first['parents'])
        row = self.engine.db.execute(
            'SELECT state FROM knowledge_work WHERE id=?', (child_id,)).fetchone()
        self.assertEqual(row['state'], 'done')  # untouched
        self.assertEqual(self._count(
            'SELECT COUNT(*) FROM knowledge_failures WHERE work_id=?', (child_id,)),
            audits_before)

    def test_different_plan_for_same_grant_rejected(self):
        cap = self._capture('proj', 'sess', 'user', 'e-z', 'content')
        source, parts = self._authoritative(cap['source_id'])
        self._legacy_row('p1', 'proj', 'sess', parts, state='pending')
        plan = dm.prepare(self.evo, self._request(
            parents=[{'id': 'p1', 'mode': 'uncommitted'}]))
        # A different acceptance_sha yields a different plan for the same grant;
        # both are prepared while the parent is still migratable.
        other = dm.prepare(self.evo, self._request(
            parents=[{'id': 'p1', 'mode': 'uncommitted'}],
            acceptance_sha='d' * 64))
        self.assertNotEqual(plan['plan_sha'], other['plan_sha'])
        dm.apply(self.evo, plan)
        with self.assertRaises(ValueError):
            dm.apply(self.evo, other)

    def test_ordinary_done_parent_refused(self):
        cap = self._capture('proj', 'sess', 'user', 'e-d', 'done content')
        source, parts = self._authoritative(cap['source_id'])
        self._legacy_row('p1', 'proj', 'sess', parts, state='done')
        with self.assertRaises(ValueError):
            dm.prepare(self.evo, self._request(
                parents=[{'id': 'p1', 'mode': 'uncommitted'}]))


class DriftRejectionTests(_Base):
    def test_input_text_drift_rejected(self):
        cap = self._capture('proj', 'sess', 'user', 'e-1', 'original text')
        source, parts = self._authoritative(cap['source_id'])
        bad = [dict(parts[0])]
        bad[0]['text'] = 'tampered text'
        self._legacy_row('p1', 'proj', 'sess', bad, state='pending')
        with self.assertRaises(ValueError):
            dm.prepare(self.evo, self._request(
                parents=[{'id': 'p1', 'mode': 'uncommitted'}]))

    def test_source_version_substitution_rejected(self):
        cap = self._capture('proj', 'sess', 'user', 'e-1', 'v1 text')
        source, parts = self._authoritative(cap['source_id'])
        # Advance the source to version 2 with genuinely different bytes.
        self.engine.db.execute(
            "INSERT INTO versions(source_id,version,text,original,hash,created_at)"
            " VALUES(?,2,?,?,?,?)",
            (cap['source_id'], 'v2 text', b'v2 text', _sha('v2 text'), '2026-01-01T00:00:00'))
        self.engine.db.execute(
            'UPDATE sources SET current_version=2 WHERE id=?', (cap['source_id'],))
        self.engine.db.commit()
        self._legacy_row('p1', 'proj', 'sess', parts, state='pending')
        with self.assertRaises(ValueError):
            dm.prepare(self.evo, self._request(
                parents=[{'id': 'p1', 'mode': 'uncommitted'}]))

    def test_owner_drift_partial_outside_owner_rejected(self):
        cap = self._capture('proj', 'sess', 'user', 'e-1', 'owned text')
        source, parts = self._authoritative(cap['source_id'])
        self._legacy_row('p1', 'proj', 'sess', parts, state='pending')
        # A hidden second claim on the same part with a different owner.
        self.engine.db.execute(
            'UPDATE knowledge_seen SET work_id=? WHERE source_id=? AND version=? AND part=?',
            ('other', parts[0]['source_id'], parts[0]['version'], parts[0]['part']))
        self.engine.db.commit()
        with self.assertRaises(ValueError):
            dm.prepare(self.evo, self._request(
                parents=[{'id': 'p1', 'mode': 'uncommitted'}]))

    def test_hidden_extra_claim_rejected(self):
        cap = self._capture('proj', 'sess', 'user', 'e-1', 'owned text')
        source, parts = self._authoritative(cap['source_id'])
        self._legacy_row('p1', 'proj', 'sess', parts, state='pending')
        # A claim owned by p1 with no matching input (a fake source/part).
        self.engine.db.execute(
            'INSERT INTO knowledge_seen VALUES(?,?,?,?)',
            ('ghost-source', 1, 0, 'p1'))
        self.engine.db.commit()
        with self.assertRaises(ValueError):
            dm.prepare(self.evo, self._request(
                parents=[{'id': 'p1', 'mode': 'uncommitted'}]))

    def test_required_card_drift_rejected(self):
        cap = self._capture('proj', 'sess', 'user', 'e-1', 'text')
        source, parts = self._authoritative(cap['source_id'])
        self._legacy_row('p1', 'proj', 'sess', parts, state='pending')
        card = self.evo.cards.seed('proj', 'k', 'knowledge', 'topic', 'title', 'body v1')
        plan = dm.prepare(self.evo, self._request(
            parents=[{'id': 'p1', 'mode': 'uncommitted'}],
            cards={cap['source_id']: [card['id']]}))
        self.assertEqual(plan['children'][0]['required_cards'][0]['id'], card['id'])
        # The card body changes -> bound body_sha no longer matches.
        self.engine.db.execute(
            'UPDATE knowledge_cards SET body=?, version=version+1 WHERE id=?',
            ('body v2', card['id']))
        self.engine.db.commit()
        with self.assertRaises(ValueError):
            dm.apply(self.evo, plan)

    def test_unauthorized_required_card_rejected(self):
        cap = self._capture('proj', 'sess', 'user', 'e-1', 'text')
        source, parts = self._authoritative(cap['source_id'])
        self._legacy_row('p1', 'proj', 'sess', parts, state='pending')
        with self.assertRaises(ValueError):
            dm.prepare(self.evo, self._request(
                parents=[{'id': 'p1', 'mode': 'uncommitted'}],
                cards={cap['source_id']: ['no-such-card']}))

    def test_live_lease_parent_rejected(self):
        cap = self._capture('proj', 'sess', 'user', 'e-1', 'text')
        source, parts = self._authoritative(cap['source_id'])
        self._legacy_row('p1', 'proj', 'sess', parts, state='pending',
                         lease_token='tok', lease_until=time.time() + 600)
        with self.assertRaises(ValueError):
            dm.prepare(self.evo, self._request(
                parents=[{'id': 'p1', 'mode': 'uncommitted'}]))


class CorrectiveNoOpTests(_Base):
    def _make_noop_parent(self, work_id, source_id, text, review_sha=None):
        """Build a real done parent with an empty batch and completion audit."""
        source = self.engine.store.source(source_id)
        parts = list(self.evo._source_parts(source))
        audit = {
            'record_version': 1,
            'inputs': [{'source_id': source_id, 'version': source['current_version'],
                        'part': p['part'], 'sha256': _sha(p['text'])} for p in parts],
            'source_count': 1,
            'proposal_hash': _sha('[]'),
            'operation_count': 0,
            'changed_ids': [],
            'verify_format_repairs': 0,
        }
        self.engine.db.execute(
            "INSERT INTO knowledge_work(id,project,session,inputs,state,created_at,"
            "pipeline_protocol) VALUES(?,?,?,?,'done',0.0,?)",
            (work_id, 'proj', 'sess', '[]', LEGACY))
        self.engine.db.execute(
            "INSERT INTO knowledge_batches(batch_id,project,op_hash,review_hash,"
            "changed_ids,created_at) VALUES(?,?,?,?,?,0.0)",
            (work_id, 'proj', _sha('[]'), _sha('review'), '[]'))
        self.engine.db.execute(
            "INSERT INTO knowledge_failures(work_id,state,error,created_at)"
            " VALUES(?, 'completion', ?, 0.0)",
            (work_id, _canon(audit)))
        for p in parts:
            self.engine.db.execute(
                "INSERT OR IGNORE INTO knowledge_seen VALUES(?,?,?,?)",
                (source_id, p['version'], p['part'], work_id))
        self.engine.db.commit()
        return audit

    def test_noop_exact_reconstruct_succeeds(self):
        cap = self._capture('proj', 'sess', 'user', 'e-noop', 'nothing to store')
        self._make_noop_parent('p1', cap['source_id'], 'nothing to store')
        plan = dm.prepare(self.evo, self._request(
            parents=[{'id': 'p1', 'mode': 'correct_false_noop', 'review_sha': 'e' * 64}]))
        self.assertEqual(len(plan['children']), 1)
        self.assertIsNotNone(plan['parents'][0]['audit_raw_sha'])
        receipt = dm.apply(self.evo, plan)
        self.assertTrue(receipt['responsibility_transferred'])
        # Old completed data preserved.
        self.assertEqual(self._count(
            'SELECT COUNT(*) FROM knowledge_batches WHERE batch_id=?', ('p1',)), 1)
        self.assertEqual(self._count(
            "SELECT COUNT(*) FROM knowledge_failures WHERE work_id='p1' AND state='completion'"),
            1)

    def test_noop_changed_batch_fails(self):
        cap = self._capture('proj', 'sess', 'user', 'e-noop', 'content')
        self._make_noop_parent('p1', cap['source_id'], 'content')
        self.engine.db.execute(
            "UPDATE knowledge_batches SET changed_ids='[\"x\"]' WHERE batch_id='p1'")
        self.engine.db.commit()
        with self.assertRaises(ValueError):
            dm.prepare(self.evo, self._request(
                parents=[{'id': 'p1', 'mode': 'correct_false_noop', 'review_sha': 'e' * 64}]))

    def test_noop_missing_completion_fails(self):
        cap = self._capture('proj', 'sess', 'user', 'e-noop', 'content')
        self._make_noop_parent('p1', cap['source_id'], 'content')
        self.engine.db.execute(
            "DELETE FROM knowledge_failures WHERE work_id='p1' AND state='completion'")
        self.engine.db.commit()
        with self.assertRaises(ValueError):
            dm.prepare(self.evo, self._request(
                parents=[{'id': 'p1', 'mode': 'correct_false_noop', 'review_sha': 'e' * 64}]))


class AtomicRollbackTests(_Base):
    def test_injected_trigger_failure_rolls_back_everything(self):
        cap = self._capture('proj', 'sess', 'user', 'e-1', 'rollback content')
        source, parts = self._authoritative(cap['source_id'])
        self._legacy_row('p1', 'proj', 'sess', parts, state='pending')
        plan = dm.prepare(self.evo, self._request(
            parents=[{'id': 'p1', 'mode': 'uncommitted'}]))
        child_id = plan['children'][0]['child_id']

        # A trigger that fails the PARENT audit insert, after the child row and
        # the ownership CAS have already run inside the same transaction.
        self.engine.db.execute(
            "CREATE TRIGGER boom BEFORE INSERT ON knowledge_failures"
            " WHEN NEW.state='%s' BEGIN SELECT RAISE(ABORT, 'injected'); END"
            % dm.PARENT_AUDIT)
        self.engine.db.commit()
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                dm.apply(self.evo, plan)
        finally:
            self.engine.db.execute('DROP TRIGGER boom')
            self.engine.db.commit()

        # Nothing committed: no child, parent unchanged, ownership intact,
        # no grant receipt.
        self.assertEqual(self._count(
            'SELECT COUNT(*) FROM knowledge_work WHERE id=?', (child_id,)), 0)
        p = self.engine.db.execute(
            'SELECT state,error FROM knowledge_work WHERE id=?', ('p1',)).fetchone()
        self.assertEqual(p['state'], 'pending')
        self.assertIsNone(p['error'])
        self.assertEqual(self._count(
            'SELECT COUNT(*) FROM knowledge_seen WHERE work_id=?', ('p1',)), len(parts))
        self.assertEqual(self._count(
            "SELECT COUNT(*) FROM meta WHERE key=?",
            (dm.MIGRATION_META_PREFIX + 'grant-a',)), 0)

        # The same plan now applies cleanly once the injected fault is gone.
        receipt = dm.apply(self.evo, plan)
        self.assertFalse(receipt['replayed'])


class ParkUnparkTests(_Base):
    def _v2_row(self, work_id, state='pending', attempts=1, next_at=0.0,
                lease_token=None, lease_until=0.0, batch=False, inputs=None):
        self.engine.db.execute(
            "INSERT INTO knowledge_work(id,project,session,inputs,state,attempts,"
            "next_at,lease_token,lease_until,created_at,pipeline_protocol)"
            " VALUES(?,?,?,?,?,?,?,?,?,0.0,?)",
            (work_id, 'proj', 'sess', inputs or '[]', state, attempts, next_at,
             lease_token, lease_until, V2))
        if batch:
            self.engine.db.execute(
                "INSERT INTO knowledge_batches(batch_id,project,op_hash,review_hash,"
                "changed_ids,created_at) VALUES(?,?,?,?,?,0.0)",
                (work_id, 'proj', 'o', 'r', '[]'))
        self.engine.db.commit()

    def test_park_clears_lease_and_unpark_restores(self):
        self._v2_row('w1', state='running', attempts=3, next_at=123.0,
                     lease_token='tok', lease_until=time.time() + 600,
                     inputs='[{"a":1}]')
        self._v2_row('w2', state='pending', next_at=50.0, batch=True)

        receipt = dm.park(self.evo, 'park-1')
        self.assertEqual(receipt['count'], 2)
        for rid in ('w1', 'w2'):
            row = self.engine.db.execute(
                'SELECT state,lease_token,lease_until,next_at FROM knowledge_work'
                ' WHERE id=?', (rid,)).fetchone()
            self.assertEqual(row['state'], 'protocol_paused')
            self.assertIsNone(row['lease_token'])
            self.assertEqual(row['lease_until'], 0.0)
        # Idempotent: duplicate returns saved result, zero writes.
        again = dm.park(self.evo, 'park-1')
        self.assertTrue(again['replayed'])

        # A new/unknown v2 row stays untouched by unpark.
        self._v2_row('w3', state='pending')

        restored = dm.unpark(self.evo, 'park-1')
        self.assertEqual(restored['count'], 2)
        w1 = self.engine.db.execute(
            'SELECT state,next_at,attempts FROM knowledge_work WHERE id=?',
            ('w1',)).fetchone()
        self.assertEqual(w1['state'], 'pending')  # running -> pending
        self.assertEqual(w1['next_at'], 123.0)
        self.assertEqual(w1['attempts'], 3)
        w2 = self.engine.db.execute(
            'SELECT state FROM knowledge_work WHERE id=?', ('w2',)).fetchone()
        self.assertEqual(w2['state'], 'publication_pending')  # committed work
        w3 = self.engine.db.execute(
            'SELECT state FROM knowledge_work WHERE id=?', ('w3',)).fetchone()
        self.assertEqual(w3['state'], 'pending')

        # Repetition is a zero-write replay.
        replay = dm.unpark(self.evo, 'park-1')
        self.assertTrue(replay['replayed'])

    def test_publication_pending_parked_and_restored(self):
        self._v2_row('w1', state='publication_pending', batch=True)
        dm.park(self.evo, 'park-2')
        row = self.engine.db.execute(
            'SELECT state,lease_token FROM knowledge_work WHERE id=?', ('w1',)).fetchone()
        self.assertEqual(row['state'], 'protocol_paused')
        self.assertIsNone(row['lease_token'])
        dm.unpark(self.evo, 'park-2')
        row = self.engine.db.execute(
            'SELECT state FROM knowledge_work WHERE id=?', ('w1',)).fetchone()
        self.assertEqual(row['state'], 'publication_pending')

    def test_paused_cache_drift_rejected(self):
        self._v2_row('w1', state='pending', inputs='[{"a":1}]')
        dm.park(self.evo, 'park-3')
        # A stale worker edits the row's cache while it is paused.
        self.engine.db.execute(
            "UPDATE knowledge_work SET bundle='{\"evil\":true}' WHERE id='w1'")
        self.engine.db.commit()
        with self.assertRaises(ValueError):
            dm.unpark(self.evo, 'park-3')

    def test_park_unrelated_rows_untouched(self):
        # A legacy v5 row is never parked.
        cap = self._capture('proj', 'sess', 'user', 'e-1', 'text')
        source, parts = self._authoritative(cap['source_id'])
        self._legacy_row('legacy-1', 'proj', 'sess', parts, state='pending')
        self._v2_row('w1', state='pending')
        dm.park(self.evo, 'park-4')
        lrow = self.engine.db.execute(
            'SELECT state FROM knowledge_work WHERE id=?', ('legacy-1',)).fetchone()
        self.assertEqual(lrow['state'], 'pending')


class ContractGapTests(_Base):
    """Focused regressions for the concrete contract gaps fixed locally.

    Covers exact outer schemas (unknown keys / strict types / real bool),
    false-noop empty changed_ids and int-zero operation_count, duplicate
    completion tuples, project and canonical-session identity for both modes,
    the actual source-count bound, prepare-time deterministic child collisions,
    raw input-column SHA binding (including empty legacy inputs), real epoch
    audit/work timestamps, the retained plan in the migration receipt, the
    reserved park error text, lease-drift refusal and restored-failure state.
    """

    # ------------------------------------------------------- schemas/types

    def test_request_unknown_keys_rejected(self):
        cap = self._capture('proj', 'sess', 'user', 'e-k', 'text')
        _, parts = self._authoritative(cap['source_id'])
        self._legacy_row('p1', 'proj', 'sess', parts, state='pending')
        for mutate in (
                lambda r: r.update(bogus=1),
                lambda r: r['parents'][0].update(extra='x')):
            req = self._request(parents=[{'id': 'p1', 'mode': 'uncommitted'}])
            mutate(req)
            with self.subTest(mutate=mutate):
                with self.assertRaises(ValueError):
                    dm.prepare(self.evo, req)

    def test_input_schema_strict_types(self):
        cap = self._capture('proj', 'sess', 'user', 'e-t', 'strict text')
        _, parts = self._authoritative(cap['source_id'])
        # Unknown input key on the parent's stored inputs.
        bad = [dict(parts[0])]
        bad[0]['surplus'] = 1
        self._legacy_row('p1', 'proj', 'sess', bad, state='pending')
        with self.assertRaises(ValueError):
            dm.prepare(self.evo, self._request(
                parents=[{'id': 'p1', 'mode': 'uncommitted'}]))

    def test_input_bool_is_not_an_int_and_partial_must_be_bool(self):
        cap = self._capture('proj', 'sess', 'user', 'e-b', 'one two')
        _, parts = self._authoritative(cap['source_id'])
        # version as a bool is a wrong type even though True == 1.
        bad = [dict(parts[0])]
        bad[0]['version'] = True
        self._legacy_row('p1', 'proj', 'sess', bad, state='pending')
        with self.assertRaises(ValueError):
            dm.prepare(self.evo, self._request(
                parents=[{'id': 'p1', 'mode': 'uncommitted'}]))
        # partial as 0/1 integer is rejected: no bool() coercion.
        for value in (0, 1):
            bad = [dict(parts[0])]
            bad[0]['partial'] = value
            self.engine.db.execute('DELETE FROM knowledge_work WHERE id=?', ('p1',))
            self.engine.db.execute('DELETE FROM knowledge_seen WHERE work_id=?', ('p1',))
            self.engine.db.commit()
            self._legacy_row('p1', 'proj', 'sess', bad, state='pending')
            with self.subTest(partial=value):
                with self.assertRaises(ValueError):
                    dm.prepare(self.evo, self._request(
                        parents=[{'id': 'p1', 'mode': 'uncommitted'}]))

    # ------------------------------------------- project/session identity

    def test_parent_project_and_session_must_match_source(self):
        cap = self._capture('proj', 'sess', 'user', 'e-id', 'identity text')
        _, parts = self._authoritative(cap['source_id'])
        for project, session in (('other', 'sess'), ('proj', 'documents')):
            self.engine.db.execute('DELETE FROM knowledge_work WHERE id=?', ('p1',))
            self.engine.db.execute('DELETE FROM knowledge_seen WHERE work_id=?', ('p1',))
            self.engine.db.commit()
            self._legacy_row('p1', project, session, parts, state='pending')
            with self.subTest(project=project, session=session):
                with self.assertRaises(ValueError):
                    dm.prepare(self.evo, self._request(
                        parents=[{'id': 'p1', 'mode': 'uncommitted'}]))

    def test_noop_project_mismatch_rejected(self):
        cap = self._capture('proj', 'sess', 'user', 'e-np', 'noop text')
        self._make_noop_row('p1', cap['source_id'], project='other')
        with self.assertRaises(ValueError):
            dm.prepare(self.evo, self._request(
                parents=[{'id': 'p1', 'mode': 'correct_false_noop',
                          'review_sha': 'e' * 64}]))

    def test_noop_batch_project_mismatch_rejected(self):
        cap = self._capture('proj', 'sess', 'user', 'e-nb', 'noop text')
        self._make_noop_row('p1', cap['source_id'])
        self.engine.db.execute(
            "UPDATE knowledge_batches SET project='other' WHERE batch_id='p1'")
        self.engine.db.commit()
        with self.assertRaises(ValueError):
            dm.prepare(self.evo, self._request(
                parents=[{'id': 'p1', 'mode': 'correct_false_noop',
                          'review_sha': 'e' * 64}]))

    # ------------------------------------------------------ actual bound

    def test_actual_source_count_bound(self):
        caps = []
        for i in range(3):
            caps.append(self._capture('proj', 'sess', 'user', 'e-%d' % i, 'text %d' % i))
        parts = []
        for cap in caps:
            _, cp = self._authoritative(cap['source_id'])
            parts.extend(cp)
        self._legacy_row('p1', 'proj', 'sess', parts, state='pending')
        original = dm.MAX_SOURCES
        dm.MAX_SOURCES = 2
        try:
            with self.assertRaises(ValueError):
                dm.prepare(self.evo, self._request(
                    parents=[{'id': 'p1', 'mode': 'uncommitted'}]))
        finally:
            dm.MAX_SOURCES = original

    # --------------------------------------------- child collision at prep

    def test_prepare_rejects_existing_child_row_or_batch(self):
        cap = self._capture('proj', 'sess', 'user', 'e-col', 'collision text')
        source, parts = self._authoritative(cap['source_id'])
        self._legacy_row('p1', 'proj', 'sess', parts, state='pending')
        cid = dm._child_id(source['project'], source['session'],
                           source['source_id'], source['current_version'])
        # An unrelated existing work row with the deterministic child id.
        self.engine.db.execute(
            "INSERT INTO knowledge_work(id,project,session,inputs,state,created_at,"
            "pipeline_protocol) VALUES(?,?,?,?, 'pending',0.0,?)",
            (cid, 'proj', 'sess', '[]', LEGACY))
        self.engine.db.commit()
        with self.assertRaises(ValueError):
            dm.prepare(self.evo, self._request(
                parents=[{'id': 'p1', 'mode': 'uncommitted'}]))
        self.engine.db.execute('DELETE FROM knowledge_work WHERE id=?', (cid,))
        self.engine.db.execute(
            "INSERT INTO knowledge_batches(batch_id,project,op_hash,review_hash,"
            "changed_ids,created_at) VALUES(?,?,?,?,?,0.0)",
            (cid, 'proj', 'o', 'r', '[]'))
        self.engine.db.commit()
        with self.assertRaises(ValueError):
            dm.prepare(self.evo, self._request(
                parents=[{'id': 'p1', 'mode': 'uncommitted'}]))

    # ------------------------------------------------- raw input column

    def test_raw_input_column_sha_bound(self):
        cap = self._capture('proj', 'sess', 'user', 'e-raw', 'raw text')
        _, parts = self._authoritative(cap['source_id'])
        raw = _canon(parts)
        self._legacy_row('p1', 'proj', 'sess', parts, state='pending')
        plan_a = dm.prepare(self.evo, self._request(
            parents=[{'id': 'p1', 'mode': 'uncommitted'}]))
        # Re-serialize the same semantic parts with different raw bytes.
        reordered = json.dumps(parts, ensure_ascii=False, indent=1, sort_keys=False)
        self.assertNotEqual(reordered, raw)
        self.engine.db.execute('UPDATE knowledge_work SET inputs=? WHERE id=?',
                               (reordered, 'p1'))
        self.engine.db.commit()
        plan_b = dm.prepare(self.evo, self._request(
            parents=[{'id': 'p1', 'mode': 'uncommitted'}]))
        self.assertNotEqual(plan_a['plan_sha'], plan_b['plan_sha'])
        self.assertEqual(plan_a['parents'][0]['inputs_sha'],
                         plan_b['parents'][0]['inputs_sha'])
        self.assertNotEqual(plan_a['parents'][0]['raw_inputs_sha'],
                            plan_b['parents'][0]['raw_inputs_sha'])

    def test_empty_legacy_inputs_sha_bound(self):
        cap = self._capture('proj', 'sess', 'user', 'e-no', 'noop text')
        self._make_noop_row('p1', cap['source_id'])
        plan_a = dm.prepare(self.evo, self._request(
            parents=[{'id': 'p1', 'mode': 'correct_false_noop', 'review_sha': 'e' * 64}]))
        self.assertEqual(plan_a['parents'][0]['raw_inputs_sha'], _sha('[]'))
        self.engine.db.execute("UPDATE knowledge_work SET inputs='[ ]' WHERE id='p1'")
        self.engine.db.commit()
        plan_b = dm.prepare(self.evo, self._request(
            parents=[{'id': 'p1', 'mode': 'correct_false_noop', 'review_sha': 'e' * 64}]))
        self.assertNotEqual(plan_a['plan_sha'], plan_b['plan_sha'])

    # ------------------------------------------------- real audit times

    def test_apply_timestamps_are_real_epoch(self):
        cap = self._capture('proj', 'sess', 'user', 'e-ts', 'timestamp text')
        _, parts = self._authoritative(cap['source_id'])
        self._legacy_row('p1', 'proj', 'sess', parts, state='pending')
        before = time.time() - 5
        plan = dm.prepare(self.evo, self._request(
            parents=[{'id': 'p1', 'mode': 'uncommitted'}]))
        dm.apply(self.evo, plan)
        cid = plan['children'][0]['child_id']
        row = self.engine.db.execute(
            'SELECT created_at FROM knowledge_work WHERE id=?', (cid,)).fetchone()
        self.assertGreater(row['created_at'], before)
        for state in (dm.PARENT_AUDIT, dm.CHILD_AUDIT):
            times = self.engine.db.execute(
                'SELECT created_at FROM knowledge_failures WHERE state=?',
                (state,)).fetchall()
            self.assertTrue(times)
            for t in times:
                self.assertGreater(t['created_at'], before)

    # -------------------------------------------------- receipt carries plan

    def test_migration_receipt_retains_full_plan(self):
        cap = self._capture('proj', 'sess', 'user', 'e-pl', 'plan text')
        _, parts = self._authoritative(cap['source_id'])
        self._legacy_row('p1', 'proj', 'sess', parts, state='pending')
        plan = dm.prepare(self.evo, self._request(
            parents=[{'id': 'p1', 'mode': 'uncommitted'}]))
        dm.apply(self.evo, plan)
        raw = self.engine.db.execute(
            'SELECT value FROM meta WHERE key=?',
            (dm.MIGRATION_META_PREFIX + 'grant-a',)).fetchone()[0]
        stored = json.loads(raw)
        self.assertEqual(stored['plan']['plan_sha'], plan['plan_sha'])
        self.assertEqual(_canon(stored['plan']), _canon(plan))

    def test_child_audit_carries_work_id_and_project(self):
        cap = self._capture('proj', 'sess', 'user', 'e-au', 'audit text')
        _, parts = self._authoritative(cap['source_id'])
        self._legacy_row('p1', 'proj', 'sess', parts, state='pending')
        plan = dm.prepare(self.evo, self._request(
            parents=[{'id': 'p1', 'mode': 'uncommitted'}]))
        dm.apply(self.evo, plan)
        cid = plan['children'][0]['child_id']
        raw = self.engine.db.execute(
            'SELECT error FROM knowledge_failures WHERE state=? AND work_id=?',
            (dm.CHILD_AUDIT, cid)).fetchone()[0]
        audit = json.loads(raw)
        self.assertEqual(audit['work_id'], cid)
        self.assertEqual(audit['project'], 'proj')
        self.assertEqual(audit['source_id'], cap['source_id'])

    # ------------------------------------------------------------- park

    def _v2_row(self, work_id, state='pending', **kw):
        self.engine.db.execute(
            "INSERT INTO knowledge_work(id,project,session,inputs,state,attempts,"
            "next_at,lease_token,lease_until,created_at,pipeline_protocol)"
            " VALUES(?,?,?,?,?,?,?,?,?,0.0,?)",
            (work_id, 'proj', 'sess', kw.get('inputs', '[]'), state,
             kw.get('attempts', 1), kw.get('next_at', 0.0),
             kw.get('lease_token'), kw.get('lease_until', 0.0), V2))
        self.engine.db.commit()

    def test_park_sets_short_safe_error(self):
        self._v2_row('w1', state='failed')
        self.engine.db.execute("UPDATE knowledge_work SET error='prior explosion'"
                               " WHERE id='w1'")
        self.engine.db.commit()
        dm.park(self.evo, 'park-g1')
        row = self.engine.db.execute(
            'SELECT state,error FROM knowledge_work WHERE id=?', ('w1',)).fetchone()
        self.assertEqual(row['state'], 'protocol_paused')
        self.assertEqual(row['error'], 'protocol paused')

    def test_unpark_rejects_changed_live_lease(self):
        self._v2_row('w1', state='pending')
        dm.park(self.evo, 'park-g2')
        self.engine.db.execute(
            "UPDATE knowledge_work SET lease_token='stray', lease_until=?"
            " WHERE id='w1'", (time.time() + 600,))
        self.engine.db.commit()
        with self.assertRaises(ValueError):
            dm.unpark(self.evo, 'park-g2')

    def test_unpark_restores_failed_state_with_short_reason(self):
        self._v2_row('w1', state='failed')
        dm.park(self.evo, 'park-g3')
        dm.unpark(self.evo, 'park-g3')
        row = self.engine.db.execute(
            'SELECT state,error FROM knowledge_work WHERE id=?', ('w1',)).fetchone()
        self.assertEqual(row['state'], 'failed')
        self.assertEqual(row['error'], 'restored failure; inspect prior audit')

    def test_unpark_timestamps_are_real_epoch(self):
        self._v2_row('w1', state='pending')
        dm.park(self.evo, 'park-g4')
        before = time.time() - 5
        dm.unpark(self.evo, 'park-g4')
        times = self.engine.db.execute(
            'SELECT created_at FROM knowledge_failures WHERE state=?',
            (dm.PARK_AUDIT + '_restored',)).fetchall()
        self.assertTrue(times)
        for t in times:
            self.assertGreater(t['created_at'], before)

    # ------------------------------------------------- false-noop contract

    def _make_noop_row(self, work_id, source_id, project='proj', input_raw='[]'):
        source = self.engine.store.source(source_id)
        parts = list(self.evo._source_parts(source))
        audit = {
            'inputs': [{'source_id': source_id, 'version': source['current_version'],
                        'part': p['part'], 'sha256': _sha(p['text'])} for p in parts],
            'operation_count': 0,
            'changed_ids': [],
        }
        self.engine.db.execute(
            "INSERT INTO knowledge_work(id,project,session,inputs,state,created_at,"
            "pipeline_protocol) VALUES(?,?,?,?,'done',0.0,?)",
            (work_id, project, 'sess', input_raw, LEGACY))
        self.engine.db.execute(
            "INSERT INTO knowledge_batches(batch_id,project,op_hash,review_hash,"
            "changed_ids,created_at) VALUES(?,?,?,?,?,0.0)",
            (work_id, project, _sha('[]'), _sha('review'), '[]'))
        self.engine.db.execute(
            "INSERT INTO knowledge_failures(work_id,state,error,created_at)"
            " VALUES(?, 'completion', ?, 0.0)",
            (work_id, _canon(audit)))
        for p in parts:
            self.engine.db.execute(
                'INSERT OR IGNORE INTO knowledge_seen VALUES(?,?,?,?)',
                (source_id, p['version'], p['part'], work_id))
        self.engine.db.commit()
        return audit

    def test_noop_empty_changed_ids_and_int_zero_operation_count(self):
        cap = self._capture('proj', 'sess', 'user', 'e-nz', 'zero text')
        self._make_noop_row('p1', cap['source_id'])
        # batch changed_ids is a non-empty-but-falsy value: {""} / 0 / {}.json
        for value in ('{}', '0', '""', 'null', ''):
            self.engine.db.execute(
                'UPDATE knowledge_batches SET changed_ids=? WHERE batch_id=?',
                (value, 'p1'))
            self.engine.db.commit()
            with self.subTest(batch_changed=value):
                with self.assertRaises(ValueError):
                    dm.prepare(self.evo, self._request(
                        parents=[{'id': 'p1', 'mode': 'correct_false_noop',
                                  'review_sha': 'e' * 64}]))
        self.engine.db.execute(
            "UPDATE knowledge_batches SET changed_ids='[]' WHERE batch_id='p1'")
        self.engine.db.commit()
        # operation_count as a bool or float is not a strict int zero.
        source = self.engine.store.source(cap['source_id'])
        part = list(self.evo._source_parts(source))[0]
        entries = [{'source_id': cap['source_id'],
                    'version': source['current_version'],
                    'part': part['part'], 'sha256': _sha(part['text'])}]
        for value in ('false', '0.0'):
            raw_audit = {
                'inputs': entries,
                'operation_count': json.loads(value),
                'changed_ids': [],
            }
            self.engine.db.execute(
                "UPDATE knowledge_failures SET error=? WHERE work_id='p1'"
                " AND state='completion'", (_canon(raw_audit),))
            self.engine.db.commit()
            with self.subTest(operation_count=value):
                with self.assertRaises(ValueError):
                    dm.prepare(self.evo, self._request(
                        parents=[{'id': 'p1', 'mode': 'correct_false_noop',
                                  'review_sha': 'e' * 64}]))

    def test_noop_duplicate_completion_tuple_rejected(self):
        cap = self._capture('proj', 'sess', 'user', 'e-dup', 'dup text')
        self._make_noop_row('p1', cap['source_id'])
        source = self.engine.store.source(cap['source_id'])
        part = list(self.evo._source_parts(source))[0]
        entry = {'source_id': cap['source_id'], 'version': source['current_version'],
                 'part': part['part'], 'sha256': _sha(part['text'])}
        audit = {'inputs': [entry, dict(entry)], 'operation_count': 0,
                 'changed_ids': []}
        self.engine.db.execute(
            "UPDATE knowledge_failures SET error=? WHERE work_id='p1'"
            " AND state='completion'", (_canon(audit),))
        self.engine.db.commit()
        with self.assertRaises(ValueError):
            dm.prepare(self.evo, self._request(
                parents=[{'id': 'p1', 'mode': 'correct_false_noop',
                          'review_sha': 'e' * 64}]))

    def test_plan_unknown_keys_rejected_on_apply(self):
        cap = self._capture('proj', 'sess', 'user', 'e-pk', 'plan keys')
        _, parts = self._authoritative(cap['source_id'])
        self._legacy_row('p1', 'proj', 'sess', parts, state='pending')
        plan = dm.prepare(self.evo, self._request(
            parents=[{'id': 'p1', 'mode': 'uncommitted'}]))
        bad = json.loads(_canon(plan))
        bad['injected'] = True
        bad['plan_sha'] = dm._hash({k: v for k, v in bad.items() if k != 'plan_sha'})
        with self.assertRaises(ValueError):
            dm.apply(self.evo, bad)


if __name__ == '__main__':
    unittest.main()
