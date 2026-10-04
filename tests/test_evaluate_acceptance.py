"""Independent failure-oriented acceptance; never invokes a provider."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tongle import evaluate
from tongle.engine import Engine
from tongle.evolution import Evolution
from tongle.knowledge_files import KnowledgeFiles

CASE = Path(__file__).resolve().parents[1] / 'examples/evaluation/status-boundary.json'


class EvaluationAcceptance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.case = json.loads(CASE.read_text())

    def test_failed_measurement_cannot_become_effective_pair(self):
        def consumer(root, case, task, *args):
            self.assertEqual(set(case), {'project'})
            self.assertEqual(set(task), {'id', 'prompt'})
            expected = next(t['expect'] for t in self.case['tasks'] if t['id'] == task['id'])
            return {'answer': json.dumps(expected), 'provider': 'mock', 'model': 'mock'}
        with patch.object(evaluate, 'summary', side_effect=OSError('measurement unavailable')):
            report = evaluate.run_case(self.case, self.root/'run', 'unused', 'mock', 'mock', consumer=consumer)
        self.assertEqual(report['effective_pairs'], 0)
        self.assertEqual(report['infra_rows'], 9)
        for p in (self.root/'run').glob('[0-9][0-9]-*.json'):
            self.assertEqual(json.loads(p.read_text())['status'], 'infra_error')

    def test_unknown_or_mixed_identity_does_not_report_improvement(self):
        rows = [{'task': 't', 'arm': a, 'status': 'completed',
                 'identity': None, 'assessment': {'pass': a == 'current'}} for a in ['off', 'current']]
        report = evaluate.compare(rows, ['off', 'current'])
        self.assertFalse(report['comparison_valid'])
        self.assertEqual(report['effective_pairs'], 0)
        self.assertEqual(report['off_to_current']['improved'], 0)

    def test_case_failure_happens_before_directory_or_consumer(self):
        case = copy.deepcopy(self.case)
        case['cards'][0]['evidence'][0]['quote'] = 'fabricated quote'
        with patch.object(evaluate, 'consume') as consumer, self.assertRaises(ValueError):
            evaluate.run_case(case, self.root/'bad', 'unused', 'p', 'm', consumer=consumer)
        consumer.assert_not_called()
        self.assertFalse((self.root/'bad').exists())

    def test_compensating_version_does_not_rewind_database_or_ignore_new_edit(self):
        evaluate.prepare(self.root/'candidate', self.case, candidate=True)
        engine = Engine(self.root/'candidate/home')
        self.addCleanup(engine.store.close)
        ev = Evolution(engine)
        c = next(c for c in ev.cards.list(self.case['project']) if c['key'] == 'qinglan-status')
        self.assertEqual(c['version'], 2)
        old_body = self.case['cards'][0]['body']
        ops = [{'op': 'update', 'id': c['id'], 'expected_version': 2, 'body': old_body,
                'evidence': c['evidence'], 'reason': 'isolated compensation demonstration'}]
        review = {'approved': True, 'findings': [], 'proposal_hash': evaluate.digest(ops)}
        ev.cards.apply('compensate', self.case['project'], ops, review)
        KnowledgeFiles(engine).publish()
        latest = ev.cards.get(c['id'])
        self.assertEqual(latest['version'], 3)
        self.assertEqual(latest['body'], old_body)
        self.assertTrue(ev._published(c['id']))
        delivered = engine.context(self.case['project'], '青岚巡检最新状态', session='fresh-consumer')
        self.assertTrue(any(r['text'] == old_body for r in delivered['results']))
        ev.cards.manual_edit(c['id'], 3, old_body + ' 人工新增：只在隔离副本验证。')
        with self.assertRaises(ValueError):
            ev.cards.apply('stale-compensation', self.case['project'], ops, review)
        self.assertIn('人工新增', ev.cards.get(c['id'])['body'])

    def test_revoked_source_prevents_candidate_or_compensation_publication(self):
        evaluate.prepare(self.root/'withdrawal', self.case)
        engine = Engine(self.root/'withdrawal/home')
        self.addCleanup(engine.store.close)
        ev = Evolution(engine)
        c = next(c for c in ev.cards.list(self.case['project']) if c['key'] == 'qinglan-status')
        engine.source_state(self.case['project'], c['evidence'][0]['source_id'], 'revoked')
        ops = [{'op': 'update', 'id': c['id'], 'expected_version': c['version'],
                'body': 'outdated candidate', 'evidence': c['evidence'], 'reason': 'must reject'}]
        with self.assertRaises(ValueError):
            ev.cards.apply('revoked', self.case['project'], ops,
                           {'approved': True, 'findings': [], 'proposal_hash': evaluate.digest(ops)})
        self.assertFalse(any(r['id'] == c['id'] for r in engine.context(self.case['project'], '巡检')['results']))


if __name__ == '__main__':
    unittest.main()
