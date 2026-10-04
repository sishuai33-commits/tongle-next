"""Bounded learning V1 — independent contract tests (DS/tests owned).

These tests exercise the *public* Learning surface against a real Engine on a
throwaway temporary home/vault.  They intentionally avoid guessing internal
payload shapes or the full state machine: they assert only documented behaviour
(record identity/dedupe, evidence refusal, scope isolation, zero-model paths,
case immutability, read-only list/status).

ROOT owns the native end-to-end matrix tests; we keep to 8-12 focused cases.
"""

import json
import tempfile
import unittest
from pathlib import Path

from tongle.engine import Engine
from tongle.evolution import Evolution
from tongle.knowledge_files import KnowledgeFiles
from tongle.learning import Learning


FACT = '接收请求不能当作业务完成，未知结果必须保留未知。'
QUERY = '接收请求是否代表业务完成？'


class LearningContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)

        self.e = Engine(root / 'home')
        self.e.init(root / 'vault', evolving=True)
        # Register close *after* tmp cleanup is registered, so LIFO order closes
        # the store before the directory is removed.
        self.addCleanup(self.e.store.close)

        cfg_path = root / 'home' / 'config.json'
        cfg_path.write_text(json.dumps({
            'knowledge_mode': 'evolving',
            'model_command': ['unused'],
            'knowledge_learning_projects': ['project'],
        }), encoding='utf-8')

        self.ev = Evolution(self.e)

        self.src = self.e.store.capture(
            'project', 'fixture', 'user', 'fact', FACT)
        self.ev.captured(self.src['source_id'], 'user', 'fixture')
        self.card = self.ev.cards.seed(
            'project', 'base', 'knowledge', '完成边界', '完成边界', FACT,
            evidence=[{'source_id': self.src['source_id'],
                       'version': 1, 'quote': FACT}])
        self.ev.baseline()
        KnowledgeFiles(self.e).publish()

        query = self.e.store.capture('project','task','user','query',QUERY)
        self.ev.captured(query['source_id'],'user','task')
        self.ctx = self.ev.context('project', QUERY, session='task')
        self.output = self.e.store.capture('project','task','assistant','answer','业务已完成')
        self.ev.captured(self.output['source_id'],'assistant','task',receipt_id=self.ctx['receipt_id'])

        self.outcome = self.e.store.capture(
            'project', 'task', 'user', 'correction',
            '刚才只能证明接收，尚无业务完成证据。')
        self.ev.captured(self.outcome['source_id'], 'user', 'task')

        self.task_source = self.e.store.capture('project','future-task','assistant','holdout','新任务：接收已确认但业务状态未知，能报告业务完成吗？')
        self.learning = Learning(self.ev)

    # -- helpers ---------------------------------------------------------
    def _records(self, project='project'):
        return self.learning.list(project)

    def _by_receipt(self, receipt_id, project='project'):
        for rec in self.learning.list(project):
            if rec['receipt_id'] == receipt_id:
                return rec
        return None

    def _ingest_feedback(self, project='project'):
        return self.learning.ingest([project])

    # 1. list/status are read-only surfaces and must not call the model.
    def test_list_and_status_do_not_call_model(self):
        def boom(*a, **k):
            raise AssertionError('model must not be called by list/status')
        self.ev._call = boom
        self.assertIsInstance(self.learning.list(), list)
        self.assertIsInstance(self.learning.list('project'), list)
        self.assertIsInstance(self.learning.status(), dict)

    # 2. observe is idempotent per receipt+source/version.
    def test_observe_is_idempotent_per_receipt_source(self):
        r = self.ctx['receipt_id']
        first = self.learning.observe('project', r, self.outcome['source_id'])
        second = self.learning.observe('project', r, self.outcome['source_id'])
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(first['state'], 'observed')
        got = self.learning.get(first['id'])
        self.assertEqual(got['id'], first['id'])
        self.assertEqual(got['project'], 'project')
        self.assertEqual(got['receipt_id'], r)

    # 3. observe refuses a source that is not available (revoked).
    def test_observe_refuses_unavailable_source(self):
        r = self.ctx['receipt_id']
        self.e.source_state(
            'project', self.outcome['source_id'], 'revoked')
        with self.assertRaises(ValueError):
            self.learning.observe('project', r, self.outcome['source_id'])

    # 4. observe refuses a source owned by another project (foreign scope).
    def test_observe_refuses_foreign_project_source(self):
        r = self.ctx['receipt_id']
        with self.assertRaises(ValueError):
            self.learning.observe('other', r, self.outcome['source_id'])

    def _feedback(self):
        payload={'receipt':self.ctx['receipt_id'],'output_source':self.output['source_id'],
                 'findings':[{'card_id':self.card['id'],'verdict':'misapplied','quote':'业务已完成'}]}
        with self.e.transaction():
            self.e.db.execute("INSERT INTO knowledge_feedback VALUES(?,?,?,'candidate')",
                              (self.ctx['receipt_id'],'project',json.dumps(payload)))

    def test_ingest_is_idempotent(self):
        self._feedback()
        self.learning.ingest(['project'])
        first=self.learning.list('project')
        self.learning.ingest(['project'])
        self.assertEqual([r['id'] for r in first],[r['id'] for r in self.learning.list('project')])
        self.assertEqual(len(first),1)

    def test_ingest_weak_binding_never_accepts(self):
        self._feedback()
        with self.e.transaction():
            self.e.db.execute("UPDATE knowledge_consumption SET binding='legacy_recent' WHERE id=?",(self.ctx['receipt_id'],))
        self.learning.ingest(['project'])
        records=self.learning.list('project')
        self.assertEqual(len(records),1)
        self.assertEqual(records[0]['state'],'needs_evidence')

    def test_ingest_other_scope_leaves_feedback_untouched(self):
        self._feedback()
        self.learning.ingest(['other'])
        self.assertEqual(self.learning.list(),[])
        self.assertEqual(self.e.db.execute('SELECT state FROM knowledge_feedback').fetchone()[0],'candidate')

    # 7. disabled/foreign scope performs zero model calls and zero mutations.
    def test_disabled_scope_zero_calls_zero_mutation(self):
        calls = []

        def spy(*a, **k):
            calls.append((a, k))
            raise AssertionError('model must not be called')
        self.ev._call = spy

        before = self.learning.list('project')
        # project outside configured scope => no learning work
        self.learning.cycle({'model_command': ['unused'],
                             'knowledge_learning_projects': ['other']})
        self.assertEqual(calls, [])
        self.assertEqual(
            [r['id'] for r in self.learning.list('project')],
            [r['id'] for r in before])

    # 8. status never claims natural user benefit.
    def test_status_counts_and_controlled_label(self):
        self.learning.observe('project', self.ctx['receipt_id'],
                              self.outcome['source_id'])
        st = self.learning.status()
        self.assertIn('candidates_pending', st)
        blob = json.dumps(st).lower()
        self.assertIn('controlled', blob)

    # 9. set_cases rejects unknown records without creating table rows.
    def test_set_cases_rejects_unknown_record(self):
        with self.assertRaises((ValueError, KeyError)):
            self.learning.set_cases('missing-record', {'id': 'c1',
                                                       'version': 1,
                                                       'tasks': []})

    # 10. set_cases validates finite scalar expectations and bool versions.
    def test_set_cases_rejects_bad_expect_and_version(self):
        rec = self.learning.observe('project', self.ctx['receipt_id'],
                                    self.outcome['source_id'])
        bad_expect = {
            'id': 'c1', 'version': 1,
            'tasks': [{'id': 't1', 'source_id': self.task_source['source_id'],
                       'source_version': 1, 'expect': {'ok': float('nan')}}],
        }
        with self.assertRaises(ValueError):
            self.learning.set_cases(rec['id'], bad_expect)
        bad_version = {
            'id': 'c1', 'version': True,
            'tasks': [{'id': 't1', 'source_id': self.task_source['source_id'],
                       'source_version': 1, 'expect': {'ok': True}}],
        }
        with self.assertRaises(ValueError):
            self.learning.set_cases(rec['id'], bad_version)

    # 11. set_cases is immutable: no overwrite of an already stored case.
    def test_set_cases_is_immutable(self):
        rec = self.learning.observe('project', self.ctx['receipt_id'],
                                    self.outcome['source_id'])
        case = {
            'id': 'c1', 'version': 1,
            'tasks': [{'id': 't1', 'source_id': self.task_source['source_id'],
                       'source_version': 1, 'expect': {'answer': True}}],
        }
        self.learning.set_cases(rec['id'], case)
        with self.assertRaises((ValueError, KeyError)):
            self.learning.set_cases(rec['id'], case)

    # 12. get() on an unknown id is a clean error, not a fabricated record.
    def test_get_unknown_id_errors(self):
        with self.assertRaises((ValueError, KeyError)):
            self.learning.get('no-such-record-id')


if __name__ == '__main__':
    unittest.main()
