import copy
import tempfile
import unittest
from pathlib import Path

from tongle.engine import Engine
from tongle.evolution import Evolution, fingerprint
from tongle.comparison_review import validate_compare_response


def _contains_value(obj, value):
    if isinstance(obj, dict):
        return any(_contains_value(v, value) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return any(_contains_value(v, value) for v in obj)
    return obj == value


def _failure_text(result):
    parts = []
    for key in ('error', 'errors', 'reason', 'message', 'detail', 'details', 'problems', 'missing', 'extra'):
        if key in result and result[key]:
            parts.append(str(result[key]))
    return ' '.join(parts).lower()


class TestComparePageProjection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self.tmp.name)
        state_path = self.tmpdir / 'state'
        vault_path = self.tmpdir / 'vault'
        self.engine = Engine(state_path)
        self.engine.init(vault_path)
        self.evolution = Evolution(self.engine)

        self.base = {'sources': []}
        self.operations = [
            {'op': 'create', 'key': 'op0', 'kind': 'memory', 'topic': 'test', 'title': 'op0', 'body': 'body0', 'evidence': [], 'reason': 'synthetic'},
            {'op': 'create', 'key': 'op1', 'kind': 'memory', 'topic': 'test', 'title': 'op1', 'body': 'body1', 'evidence': [], 'reason': 'synthetic'},
            {'op': 'create', 'key': 'op2', 'kind': 'memory', 'topic': 'test', 'title': 'op2', 'body': 'body2', 'evidence': [], 'reason': 'synthetic'},
            {'op': 'create', 'key': 'op3', 'kind': 'memory', 'topic': 'test', 'title': 'op3', 'body': 'body3', 'evidence': [], 'reason': 'synthetic'},
        ]
        self.fullhash = fingerprint(self.operations)
        self.by_id = {
            'c1': {'body': 'body c1', 'version': 1, 'key': 'k1', 'title': 't1', 'topic': 'top1', 'kind': 'k', 'state': 's'},
            'c2': {'body': 'body c2', 'version': 2, 'key': 'k2', 'title': 't2', 'topic': 'top2', 'kind': 'k', 'state': 's'},
            'c3': {'body': 'body c3', 'version': 3, 'key': 'k3', 'title': 't3', 'topic': 'top3', 'kind': 'k', 'state': 's'},
            'c4': {'body': 'body c4', 'version': 4, 'key': 'k4', 'title': 't4', 'topic': 'top4', 'kind': 'k', 'state': 's'},
        }
        self.bodies = {'c1': 'body c1', 'c2': 'body c2'}
        self.page1 = {'cards': ['c1', 'c2'], 'pairs': [[1, 'c1'], [2, 'c2']]}
        self.page2 = {'cards': ['c3', 'c4'], 'pairs': [[0, 'c3'], [3, 'c4']]}
        self.full_page = {'cards': ['c1', 'c2', 'c3', 'c4'], 'pairs': [[0, 'c1'], [1, 'c2'], [2, 'c3'], [3, 'c4']]}
        self.validation_page = {'pairs': [[1, 'c1'], [2, 'c2']]}
        self.valid_response = {
            'pairs': [
                {'op': 1, 'card_id': 'c1', 'relation': 'distinct', 'quote': 'body c1', 'reason': 'The concrete objects differ in this synthetic fixture.'},
                {'op': 2, 'card_id': 'c2', 'relation': 'distinct', 'quote': 'body c2', 'reason': 'The concrete objects differ in this synthetic fixture.'},
            ]
        }

    def tearDown(self):
        try:
            self.engine.store.close()
        finally:
            self.tmp.cleanup()

    def _payload(self, page):
        return self.evolution._compare_payload_core(
            self.base, page, self.by_id, self.operations, self.fullhash)

    def test_projection_page1(self):
        original_ops = copy.deepcopy(self.operations)
        payload = self._payload(self.page1)
        proj_ops = payload['frozen_proposal']['operations']
        self.assertEqual(len(proj_ops), 4)
        self.assertIsNone(proj_ops[0])
        self.assertIs(proj_ops[1], self.operations[1])
        self.assertIs(proj_ops[2], self.operations[2])
        self.assertIsNone(proj_ops[3])
        self.assertEqual(self.operations, original_ops)
        self.assertEqual(fingerprint(self.operations), fingerprint(original_ops))
        self.assertEqual(fingerprint(self.operations), self.fullhash)
        self.assertTrue(_contains_value(payload, self.fullhash))
        self.assertNotEqual(fingerprint(proj_ops), self.fullhash)
        candidates = payload['page']['candidates']
        self.assertEqual(len(candidates), 2)
        self.assertEqual(candidates[0]['body'], self.by_id['c1']['body'])
        self.assertEqual(candidates[1]['body'], self.by_id['c2']['body'])

    def test_projection_page2(self):
        payload = self._payload(self.page2)
        proj_ops = payload['frozen_proposal']['operations']
        self.assertEqual(len(proj_ops), 4)
        self.assertIsNone(proj_ops[1])
        self.assertIsNone(proj_ops[2])
        self.assertIs(proj_ops[0], self.operations[0])
        self.assertIs(proj_ops[3], self.operations[3])
        self.assertTrue(_contains_value(payload, self.fullhash))
        self.assertNotEqual(fingerprint(proj_ops), self.fullhash)

    def test_full_page_all_ops(self):
        payload = self._payload(self.full_page)
        proj_ops = payload['frozen_proposal']['operations']
        self.assertEqual(len(proj_ops), 4)
        for i, op in enumerate(proj_ops):
            self.assertIs(op, self.operations[i])
        self.assertTrue(_contains_value(payload, self.fullhash))
        self.assertEqual(fingerprint(proj_ops), self.fullhash)

    def test_fullhash_binding_changes_with_actual_hash(self):
        other_ops = copy.deepcopy(self.operations)
        other_ops[1]['body'] = 'different body'
        other_hash = fingerprint(other_ops)
        self.assertNotEqual(other_hash, self.fullhash)

        payload = self._payload(self.page1)
        self.assertTrue(_contains_value(payload, self.fullhash))
        self.assertFalse(_contains_value(payload, other_hash))

        other_payload = self.evolution._compare_payload_core(
            self.base, self.page1, self.by_id, other_ops, other_hash)
        self.assertTrue(_contains_value(other_payload, other_hash))
        self.assertFalse(_contains_value(other_payload, self.fullhash))

    def test_malformed_indices(self):
        bad_pages = [
            {'cards': ['c1'], 'pairs': [[True, 'c1']]},
            {'cards': ['c1'], 'pairs': [[-1, 'c1']]},
            {'cards': ['c1'], 'pairs': [[4, 'c1']]},
            {'cards': ['c1'], 'pairs': [['a', 'c1']]},
        ]
        for page in bad_pages:
            with self.assertRaises(ValueError):
                self._payload(page)

    def test_non_dict_operation(self):
        ops = [self.operations[0], 'not a dict', self.operations[2], self.operations[3]]
        page = {'cards': ['c1'], 'pairs': [[1, 'c1']]}
        with self.assertRaises(ValueError):
            self.evolution._compare_payload_core(
                self.base, page, self.by_id, ops, self.fullhash)

    def test_strict_validator_positive_control(self):
        result = validate_compare_response(self.valid_response, self.validation_page, self.bodies)
        self.assertTrue(result['ok'])
        self.assertFalse(_failure_text(result))

    def test_strict_validator_rejects_extra_op(self):
        extra_response = copy.deepcopy(self.valid_response)
        extra_response['pairs'].append({
            'op': 0,
            'card_id': 'c1',
            'relation': 'distinct',
            'quote': 'body c1',
            'reason': 'The concrete objects differ in this synthetic fixture.',
        })
        bodies = dict(self.bodies)
        # Reuse a valid declared card so this isolates the extra-op rejection.
        result = validate_compare_response(extra_response, self.validation_page, bodies)
        self.assertFalse(result['ok'])
        text = _failure_text(result)
        self.assertTrue(text)
        self.assertTrue('0' in text or 'op' in text)

    def test_strict_validator_rejects_missing_op(self):
        missing_response = {'pairs': [copy.deepcopy(self.valid_response['pairs'][1])]}
        result = validate_compare_response(missing_response, self.validation_page, self.bodies)
        self.assertFalse(result['ok'])
        text = _failure_text(result)
        self.assertTrue(text)
        self.assertTrue('1' in text or 'op' in text)

    def test_measure_uses_projection(self):
        short_ops = copy.deepcopy(self.operations)
        pinned_hash = fingerprint(short_ops)
        long_ops = [
            {'op': 'create', 'key': 'op0', 'kind': 'memory', 'topic': 'test', 'title': 'op0', 'body': 'x' * 10000, 'evidence': [], 'reason': 'synthetic'},
            self.operations[1],
            self.operations[2],
            {'op': 'create', 'key': 'op3', 'kind': 'memory', 'topic': 'test', 'title': 'op3', 'body': 'y' * 10000, 'evidence': [], 'reason': 'synthetic'},
        ]
        measure_long = self.evolution._compare_page_measure(
            self.base, self.by_id, long_ops, pinned_hash)
        measure_short = self.evolution._compare_page_measure(
            self.base, self.by_id, short_ops, pinned_hash)
        locators = [{'card_id': 'c1', 'ops': [1, 2]}]
        size_long = measure_long(locators)
        size_short = measure_short(locators)
        self.assertEqual(size_long, size_short)

        measure_full = self.evolution._compare_page_measure(
            self.base, self.by_id, short_ops, pinned_hash)
        size_full = measure_full([{'card_id': 'c1', 'ops': [0, 1, 2, 3]}])
        self.assertGreater(size_full, size_short)

    def test_contract_fingerprint_includes_task(self):
        fp1 = self.evolution._contract_fingerprint()
        import tongle.evolution as evo_mod
        old_task = evo_mod.COMPARE_TASK
        try:
            evo_mod.COMPARE_TASK = old_task + ' changed'
            fp2 = self.evolution._contract_fingerprint()
            self.assertNotEqual(fp1, fp2)
        finally:
            evo_mod.COMPARE_TASK = old_task

    def test_method_pure(self):
        ops_snapshot = copy.deepcopy(self.operations)
        by_id_snapshot = copy.deepcopy(self.by_id)
        page_snapshot = copy.deepcopy(self.page1)
        self._payload(self.page1)
        self.assertEqual(self.operations, ops_snapshot)
        self.assertEqual(self.by_id, by_id_snapshot)
        self.assertEqual(self.page1, page_snapshot)


if __name__ == '__main__':
    unittest.main()
