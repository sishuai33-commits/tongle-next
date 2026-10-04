"""Independent association acceptance; no provider calls or production paths."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from tongle.engine import Engine
from tongle.evolution import Evolution


class ConsumptionBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.engine = Engine(self.root / 'home')
        self.engine.init(self.root / 'vault', evolving=True)
        (self.engine.home / 'config.json').write_text('{"knowledge_mode":"evolving"}')
        self.ev = Evolution(self.engine)
        self.addCleanup(self.engine.store.close)

    def offer(self, session='s', project='p'):
        return self.ev.context(project, '继续项目', session=session)['receipt_id']

    def answer(self, event, receipt=None, project='p', session='s', role='assistant'):
        saved = self.engine.store.capture(project, session, role, event, '实际输出：只做方案。')
        return saved, self.ev.captured(saved['source_id'], role, session, receipt_id=receipt)

    def row(self, rid):
        return dict(self.engine.db.execute('SELECT * FROM knowledge_consumption WHERE id=?', (rid,)).fetchone())

    def test_out_of_order_answers_bind_exact_receipts(self):
        first, second = self.offer(), self.offer()
        a, _ = self.answer('a', first)
        b, _ = self.answer('b', second)
        self.assertEqual(self.row(first)['output_source'], a['source_id'])
        self.assertEqual(self.row(second)['output_source'], b['source_id'])
        self.assertEqual(self.row(first)['binding'], 'receipt')

    def test_explicit_missing_does_not_steal_cancelled_or_latest_receipt(self):
        pending = self.offer()
        self.answer('a', '')
        self.answer('b', 'not-an-id')
        self.assertIsNone(self.row(pending)['output_source'])
        self.assertEqual(self.engine.db.execute('SELECT count(*) FROM events').fetchone()[0], 2)

    def test_project_session_and_role_boundaries(self):
        rid = self.offer()
        self.answer('foreign-project', rid, project='other')
        self.answer('foreign-session', rid, session='other')
        self.answer('user-not-output', rid, role='user')
        self.assertIsNone(self.row(rid)['output_source'])
        saved = self.engine.store.capture('p', 's', 'user', 'spoofed', '只做方案')
        self.ev.captured(saved['source_id'], 'assistant', 's', receipt_id=rid)
        self.assertIsNone(self.row(rid)['output_source'])

    def test_duplicate_preserves_checked_evidence_and_conflict_cannot_rebind(self):
        rid = self.offer()
        a, _ = self.answer('a', rid)
        self.engine.db.execute("UPDATE knowledge_consumption SET state='checked',checks=? WHERE id=?", ('{"evidence":[]}', rid))
        self.engine.db.commit()
        again = self.ev.captured(a['source_id'], 'assistant', 's', receipt_id=rid)
        self.assertTrue(again['replayed'])
        self.answer('different', rid)
        self.assertEqual(self.row(rid)['output_source'], a['source_id'])
        self.assertEqual(self.row(rid)['state'], 'checked')
        self.assertEqual(self.row(rid)['checks'], '{"evidence":[]}')

    def test_same_output_cannot_count_for_two_receipts(self):
        one, two = self.offer(), self.offer()
        saved, _ = self.answer('a', one)
        result = self.ev.captured(saved['source_id'], 'assistant', 's', receipt_id=two)
        self.assertEqual(result['binding'], 'unknown')
        self.assertIsNone(self.row(two)['output_source'])

    def test_legacy_binding_remains_weak_and_cannot_queue_correction(self):
        rid = self.offer()
        _, association = self.answer('legacy')
        self.assertEqual(association['binding'], 'legacy_recent')
        self.engine.db.execute("INSERT INTO knowledge_feedback VALUES(?,?,?,'pending')", (rid, 'p', '{}'))
        self.engine.db.commit()
        self.ev.queue_feedback()
        self.assertEqual(self.engine.db.execute('SELECT state FROM knowledge_feedback').fetchone()[0], 'unverified')
        self.assertEqual(self.engine.db.execute("SELECT count(*) FROM sources WHERE session='knowledge-feedback'").fetchone()[0], 0)

    def test_association_survives_core_restart(self):
        rid = self.offer()
        self.engine.store.close()
        self.engine = Engine(self.root / 'home')
        self.addCleanup(self.engine.store.close)
        self.ev = Evolution(self.engine)
        saved, _ = self.answer('restart', rid)
        self.assertEqual(self.row(rid)['output_source'], saved['source_id'])

    def test_additive_migration_preserves_historical_row_and_repeats(self):
        rid = self.offer()
        self.engine.db.execute('ALTER TABLE knowledge_consumption DROP COLUMN binding')
        self.engine.db.commit()
        Evolution(self.engine)
        Evolution(self.engine)
        self.assertEqual(self.row(rid)['binding'], 'legacy_recent')
        self.assertEqual(self.row(rid)['state'], 'offered')


if __name__ == '__main__':
    unittest.main()
