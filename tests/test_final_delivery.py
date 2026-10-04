import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from tongle.engine import Engine
from tongle.evolution import Evolution


class KnowledgeRetryFlowTest(unittest.TestCase):
    def setUp(self):
        self.temp_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.temp_root, ignore_errors=True)

        config = {
            'knowledge_mode': 'evolving',
            'model_command': ['synthetic-stub'],
            'knowledge_quiet_seconds': 0,
        }
        os.makedirs(os.path.join(self.temp_root, 'state'))
        with open(os.path.join(self.temp_root, 'state', 'config.json'), 'w') as handle:
            json.dump(config, handle)

        self.e = Engine(Path(self.temp_root) / 'state')
        self.addCleanup(self.e.store.close)
        self.e.init(Path(self.temp_root) / 'vault')
        self.ev = Evolution(self.e)

        rec = self.e.store.capture(
            'project',
            's1',
            'user',
            'e1',
            'Synthetic evidence with a required condition.',
        )
        self.ev.captured(rec['source_id'], 'user', 's1')

    def _queue_and_get_job(self):
        self.ev.queue(force=True)
        job = self.e.db.execute(
            'SELECT * FROM knowledge_work LIMIT 1'
        ).fetchone()
        self.assertIsNotNone(job, 'expected queued knowledge_work row')
        return job

    def _work_row(self, work_id):
        return self.e.db.execute(
            'SELECT * FROM knowledge_work WHERE id=?', (work_id,)
        ).fetchone()

    def _batch_count(self):
        row = self.e.db.execute(
            'SELECT COUNT(*) AS n FROM knowledge_batches'
        ).fetchone()
        return row['n']

    def _failure_rows(self, work_id, state):
        return self.e.db.execute(
            'SELECT * FROM knowledge_failures WHERE work_id=? AND state=?',
            (work_id, state),
        ).fetchall()

    def test_rejection_retains_feedback_then_retry_sees_defect_and_reaches_done(self):
        job = self._queue_and_get_job()
        work_id = job['id']

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'proposal' in data:
                return {
                    'approved': False,
                    'findings': [{'issue': 'required condition absent'}],
                }, {}
            return {
                'operations': [],
                'reason': 'synthetic fixture proposal',
            }, {}

        with patch('tongle.worker.invoke_json', side_effect=model):
            self.ev.process(limit=1, force=True)

        row = self._work_row(work_id)
        self.assertIsNotNone(row)
        self.assertEqual(row['state'], 'retry')
        self.assertEqual(self._batch_count(), 0)

        rejected = self._failure_rows(work_id, 'review_rejected')
        self.assertEqual(len(rejected), 1)
        self.assertIn('required condition absent', rejected[0]['error'])

        retry_rows = self._failure_rows(work_id, 'retry')
        self.assertEqual(len(retry_rows), 1)

        stored = json.loads(row['bundle'])
        feedback = stored['feedback']
        self.assertTrue(feedback)
        flattened = json.dumps(feedback)
        self.assertIn('required condition absent', flattened)

        # Test-only: clear backoff so the retry is immediately eligible.
        self.e.db.execute(
            'UPDATE knowledge_work SET next_at=0 WHERE id=?', (work_id,)
        )
        self.e.db.commit()

        seen_retry_findings = {}

        def retry_model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'proposal' in data:
                seen_retry_findings['proposal'] = data.get('retry_findings')
                seen_retry_findings['feedback'] = data.get('feedback')
                return {'approved': True, 'findings': []}, {}
            seen_retry_findings['compile'] = data
            return {
                'operations': [],
                'reason': 'synthetic fixture proposal',
            }, {}

        with patch('tongle.worker.invoke_json', side_effect=retry_model):
            self.ev.process(limit=1, force=True)

        self.assertIn('compile', seen_retry_findings)
        self.assertIn('proposal', seen_retry_findings)

        retry_findings = seen_retry_findings['compile'].get('retry_findings')
        self.assertIsNotNone(
            retry_findings, 'compiler retry must receive the prior rejection'
        )
        self.assertIn('required condition absent', json.dumps(retry_findings))

        row = self._work_row(work_id)
        self.assertEqual(row['state'], 'done')

    def test_reason_only_rejection_retains_reason_and_remains_retry(self):
        job = self._queue_and_get_job()
        work_id = job['id']

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'proposal' in data:
                return {
                    'approved': False,
                    'reason': 'scope not evidenced',
                }, {}
            return {
                'operations': [],
                'reason': 'synthetic fixture proposal',
            }, {}

        with patch('tongle.worker.invoke_json', side_effect=model):
            self.ev.process(limit=1, force=True)

        row = self._work_row(work_id)
        self.assertIsNotNone(row)
        self.assertEqual(row['state'], 'retry')
        self.assertEqual(self._batch_count(), 0)

        rejected = self._failure_rows(work_id, 'review_rejected')
        self.assertEqual(len(rejected), 1)
        self.assertIn('scope not evidenced', rejected[0]['error'])
        self.assertIsNone(row['review'])  # rejected result is not an approval cache

        stored = json.loads(row['bundle'])
        self.assertIn('scope not evidenced', json.dumps(stored['feedback']))

        retry_rows = self._failure_rows(work_id, 'retry')
        self.assertEqual(len(retry_rows), 1)

    def test_rejected_commit_validation_preserves_list_feedback(self):
        job = self._queue_and_get_job()
        work_id = job['id']

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'proposal' in data:
                return {'approved': True, 'findings': []}, {}
            return {'operations': [], 'reason': 'synthetic fixture'}, {}

        with patch('tongle.worker.invoke_json', side_effect=model), patch.object(
                self.ev.cards, 'apply', side_effect=ValueError('synthetic malformed output')):
            self.ev.process(limit=1, force=True)

        row = self._work_row(work_id)
        self.assertIsNotNone(row)
        self.assertEqual(row['state'], 'retry')
        self.assertEqual(self._batch_count(), 0)

        stored = json.loads(row['bundle'])
        feedback = stored['feedback']
        self.assertIsInstance(feedback, list)
        self.assertIn('synthetic malformed output', json.dumps(feedback))

        retry_rows = self._failure_rows(work_id, 'retry')
        self.assertGreaterEqual(len(retry_rows), 1)

    def test_bounded_findings_caps_many_and_oversized_issues(self):
        many = [
            {'issue': 'issue-%d' % i} for i in range(200)
        ]
        huge = [{'issue': 'x' * 100000}]

        bounded_many = self.ev._bounded_findings(many)
        bounded_huge = self.ev._bounded_findings(huge)

        self.assertEqual(len(bounded_many), 8)
        self.assertEqual(len(bounded_huge[0]['finding']), 1600)
        serialized = json.dumps(bounded_many)
        self.assertLess(len(serialized), len(json.dumps(many)) + 1)

        self.assertTrue(bounded_huge)
        self.assertLess(
            len(json.dumps(bounded_huge)), len(json.dumps(huge))
        )


if __name__ == '__main__':
    unittest.main()
