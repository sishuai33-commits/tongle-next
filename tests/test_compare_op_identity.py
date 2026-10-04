"""Global operation-index identity for the frozen paged comparison.

A real DS run failed a synthetic multi-operation page: the declared pair was the
ZERO-BASED GLOBAL ``op=1`` / ``card001``, but the comparer repeatedly returned
``op=0`` / ``card001`` (a page-local or default-0 index), and the bounded repair
feedback only echoed the wrong operation the model itself sent. These synthetic
regressions pin the two narrow fixes:

1. ``COMPARE_TASK`` defines ``op`` as the zero-based GLOBAL index into
   ``frozen_proposal.operations``, preserved identically on every page.
2. The unexpected-pair rejection adds a deterministic HOST-derived expected
   operation index for the card that is actually on the page, without echoing
   any model text; strict rejection is unchanged (no verdict, no remap).

They exercise the real ``Evolution._compare_stage`` / ``_compare_payload_core``
paths with synthetic fixtures only: no live vault, model, credentials or
network is touched.
"""
import json
import unittest
from unittest.mock import patch

from tongle.comparison_review import (COMPARE_TASK, COMPARE_REPAIR_WORST_CASE,
                                      MAX_COMPARE_FEEDBACK_CHARS,
                                      MAX_COMPARE_PAGE_ATTEMPTS,
                                      compare_repair_feedback,
                                      compare_repair_field,
                                      validate_compare_response)
from tongle.evolution import StageLimit, canonical, fingerprint

try:  # sibling import under ``unittest discover -s tests``
    from test_compare_quote_assistance import _AssistHarness
except ImportError:  # pragma: no cover - package-import invocation
    from tests.test_compare_quote_assistance import _AssistHarness


class CompareGlobalOpIdentityTests(unittest.TestCase):
    CID = 'a' * 32
    BODY = '旧卡甲的完整正文内容' + '细节' * 20
    REASON = 'scope and claim clearly differ from the proposed change here'

    def _page(self, pairs):
        return {'pairs': pairs, 'cards': [self.CID]}

    def _pair(self, op, cid=None):
        return {'op': op, 'card_id': cid or self.CID, 'relation': 'distinct',
                'quote': self.BODY[:12], 'reason': self.REASON}

    def test_compare_task_defines_zero_based_global_index(self):
        self.assertIn('ZERO-BASED GLOBAL index into frozen_proposal.operations',
                      COMPARE_TASK)
        self.assertIn('NEVER page-local', COMPARE_TASK)
        self.assertIn('NEVER a default of 0', COMPARE_TASK)

    def test_declared_global_op1_rejects_wrong_op0_with_expected_index(self):
        page = self._page([[1, self.CID]])
        result = validate_compare_response(
            {'pairs': [self._pair(0)]}, page, {self.CID: self.BODY})
        self.assertFalse(result['ok'])
        self.assertEqual(result['error'],
                         'unexpected pair for this page; expected op=1')
        item = result['feedback'][0]
        self.assertEqual(item['op'], 0)                 # the model's echoed wrong op
        self.assertEqual(item['card_id'], self.CID)
        self.assertEqual(item['error'],
                         'unexpected pair for this page; expected op=1')

    def test_declared_global_op1_valid_pair_passes(self):
        page = self._page([[1, self.CID]])
        result = validate_compare_response(
            {'pairs': [self._pair(1)]}, page, {self.CID: self.BODY})
        self.assertTrue(result['ok'])
        self.assertEqual(result['dispositions'][0]['op'], 1)

    def test_multiple_nonconsecutive_declared_ops_stay_exact(self):
        page = self._page([[1, self.CID], [3, self.CID]])
        bodies = {self.CID: self.BODY}
        wrong = validate_compare_response({'pairs': [self._pair(0)]}, page, bodies)
        self.assertEqual(wrong['error'],
                         'unexpected pair for this page; expected op=1,3')
        missing = validate_compare_response({'pairs': [self._pair(1)]}, page, bodies)
        self.assertFalse(missing['ok'])
        self.assertEqual(missing['error'], 'incomplete pair coverage')
        self.assertEqual([tuple(x) for x in missing['missing']], [(3, self.CID)])
        ok = validate_compare_response(
            {'pairs': [self._pair(1), self._pair(3)]}, page, bodies)
        self.assertTrue(ok['ok'])
        self.assertEqual(sorted(d['op'] for d in ok['dispositions']), [1, 3])

    def test_expected_detail_is_host_only_and_bounded(self):
        page = self._page([[2, self.CID]])
        bodies = {self.CID: self.BODY}
        result = validate_compare_response({'pairs': [self._pair(0)]}, page, bodies)
        self.assertEqual(result['feedback'][0]['error'],
                         'unexpected pair for this page; expected op=2')
        self.assertLessEqual(len(result['feedback'][0]['error']),
                             MAX_COMPARE_FEEDBACK_CHARS)
        feedback = compare_repair_feedback(result)
        self.assertTrue(any('expected op=2' in text for text in feedback), feedback)
        field = compare_repair_field(MAX_COMPARE_PAGE_ATTEMPTS, feedback)
        worst = len(canonical(COMPARE_REPAIR_WORST_CASE).encode('utf-8'))
        self.assertLessEqual(len(canonical(field).encode('utf-8')), worst)

    def test_unknown_card_keeps_historical_message(self):
        # A card that is not declared on this page has no host expected index,
        # so the historical bounded message is unchanged (no fabricated op).
        page = self._page([[1, self.CID]])
        result = validate_compare_response(
            {'pairs': [self._pair(0, cid='b' * 32)]}, page, {self.CID: self.BODY})
        self.assertFalse(result['ok'])
        self.assertEqual(result['error'], 'unexpected pair for this page')


class CompareGlobalOpIndexFlowTests(_AssistHarness):
    def _seed_target(self):
        return self.seed('共同令牌卡', '共同令牌' + '密' * 300)

    def _operations(self):
        unrelated = {'op': 'create', 'key': '无关键', 'kind': 'memory', 'topic': '主题',
                     'title': '无关题', 'body': '完全无关的其他内容。',
                     'evidence': [{'source_id': 'f' * 32, 'version': 1, 'quote': 'x'}],
                     'reason': '无关新增'}
        target = self._unit_op()        # create '共同令牌', shares the seeded card
        return [unrelated, target]

    def _pair_body(self, payload):
        pair = payload['page']['pairs'][0]
        cand = {c['card_id']: c for c in payload['page']['candidates']}
        return pair, cand[pair['card_id']]['body']

    def test_wrong_global_op_rejected_with_expected_index_and_frozen_ops(self):
        self._seed_target()
        base, job, _part = self._unit_context()
        operations = self._operations()
        operations_snapshot = json.loads(json.dumps(operations))
        payloads = []

        def side_effect(_job, stage, payload, _cfg):
            self._record_call(_job, stage)
            payloads.append(payload)
            pair, body = self._pair_body(payload)
            return {'pairs': [{'op': 0, 'card_id': pair['card_id'],
                               'relation': 'distinct', 'quote': body[:12],
                               'reason': 'scope and claim clearly differ here'}]}

        with patch.object(self.ev, '_call', side_effect=side_effect):
            with self.assertRaises(StageLimit):
                self.ev._compare_stage(job, base, operations, 'tok', {})

        self.assertEqual(len(payloads), MAX_COMPARE_PAGE_ATTEMPTS)
        for payload in payloads:
            # The approved wire projection retains the GLOBAL index and full
            # selected operation on EVERY repair; unrelated op 0 is null. The
            # authoritative full proposal/hash remains unchanged.
            self.assertEqual(payload['page']['pairs'][0]['op'], 1)
            self.assertEqual(payload['frozen_proposal']['operations'],
                             [None, operations_snapshot[1]])
            self.assertEqual(payload['frozen_proposal']['proposal_hash'],
                             fingerprint(operations_snapshot))
        self.assertEqual(operations, operations_snapshot)
        # The FIRST repair payload carries the host-derived expected index.
        feedback = payloads[1]['page_repair']['feedback']
        self.assertTrue(any('expected op=1' in text for text in feedback), feedback)
        self.assertIn('op=0', ' '.join(feedback))

    def test_correct_global_op_is_accepted(self):
        self._seed_target()
        base, job, _part = self._unit_context()
        operations = self._operations()

        def side_effect(_job, stage, payload, _cfg):
            self._record_call(_job, stage)
            pair, body = self._pair_body(payload)
            self.assertEqual(pair['op'], 1)
            return {'pairs': [{'op': pair['op'], 'card_id': pair['card_id'],
                               'relation': 'distinct', 'quote': body[:12],
                               'reason': 'scope and claim clearly differ here'}]}

        with patch.object(self.ev, '_call', side_effect=side_effect):
            reports, findings = self.ev._compare_stage(job, base, operations, 'tok', {})
        self.assertEqual(findings, [])
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]['pairs'][0]['op'], 1)


if __name__ == '__main__':
    unittest.main()
