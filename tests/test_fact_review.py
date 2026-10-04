"""Focused tests for the pre-verify factual-audit gate.

These use the real ``Evolution._fact_review_gate`` / ``_call`` path against a
synthetic ledger (real Engine, real ``knowledge_calls`` reservation and
settlement, real captured source ledger, real frozen card bodies/read-set).
Model replies are provided by a worker transport stub so no live model,
credentials, network or vault is touched. A stub only proves machine behaviour,
not semantic quality: the gate is a bounded mitigation whose quote checks do NOT
prove semantic fidelity.

The fixtures deliberately use a REAL captured source and a REAL seeded card so
the full ``_verify_repair_context_current`` freshness guard (source snapshot +
per-part text hash + availability + read-set card version/body + scope) runs
exactly as it does in production, and drift tests can mutate live state.
"""
import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tongle.engine import Engine
from tongle.evidence_selection import body_sha256
from tongle.evolution import (BudgetWait, Evolution, FACT_REVIEW_RECEIPT_STATE,
                              FACT_REVIEW_REJECTED_STATE, FEEDBACK_ORIGIN_HOST,
                              FEEDBACK_ORIGIN_MODEL, STALE_FEEDBACK_NOTICE,
                              SelectionChanged)
from tongle.fact_review import (build_binding, build_content, build_receipt,
                                receipt_ok, required_op_indices,
                                validate_response)


SOURCE_TEXT = ('冷冻柜门保持打开超过90秒时,值班技术员必须关闭柜门,'
               '并在恢复制冷之前记录该事件,以避免未被记录的温度偏离。')
FAITHFUL_BODY = '冷冻柜门保持打开超过90秒时,值班技术员必须关闭柜门。'
EXTRA_BODY = '冷冻柜门保持打开超过90秒时,值班技术员必须关闭柜门,以避免未被记录的温湿度偏离。'
OLD_BODY = '冷冻柜门必须关闭。'


def _supported(op=0, substring=FAITHFUL_BODY, quote=SOURCE_TEXT):
    return {'op': op, 'claim': '门开超时关闭', 'verdict': 'supported',
            'proposal_substring': substring, 'source_quote': quote}


def _unsupported(op=0, substring='温湿度偏离'):
    return {'op': op, 'claim': '温湿度偏离', 'verdict': 'unsupported',
            'proposal_substring': substring,
            'source_quote': 'SOURCE 仅称 temperature excursions,无 humidity 对应内容'}


def _pass_response():
    return {'checks': [_supported()], 'unsupported_count': 0, 'decision': 'pass'}


def _fail_response():
    return {'checks': [_unsupported()], 'unsupported_count': 1, 'decision': 'fail'}


class PureModuleTests(unittest.TestCase):
    def test_required_ops_skip_retire_and_empty(self):
        ops = [{'op': 'create', 'body': '正文'},
               {'op': 'retire', 'id': 'x'},
               {'op': 'update', 'id': 'y', 'body': '   '},
               {'op': 'update', 'id': 'z', 'title': '标题'}]
        self.assertEqual(required_op_indices(ops), [0, 3])

    def test_build_content_separates_sources_from_old_cards(self):
        ops = [{'op': 'update', 'id': 'c1', 'title': 't', 'body': 'b'}]
        cards = [{'id': 'c1', 'version': 3, 'body': '旧正文'}]
        content = build_content([{'source_id': 's', 'version': 1, 'text': '源'}],
                                ops, cards, 'scope', 'contract')
        # A card id is a card, never a fake source entry with version None.
        self.assertEqual([s['source_id'] for s in content['sources']], ['s'])
        self.assertEqual(content['existing']['cards'],
                         [{'id': 'c1', 'version': 3, 'body': '旧正文'}])
        self.assertEqual(content['operations'][0]['card_id'], 'c1')
        # The old body is carried once (under existing.cards), never twice.
        self.assertNotIn('old_body', content['operations'][0])
        self.assertIn('温度', SOURCE_TEXT)

    def test_validate_supported_pass(self):
        content = {'task': 't', 'sources': [{'text': SOURCE_TEXT}],
                   'operations': [{'op': 0, 'proposal_title': '', 'proposal_body': FAITHFUL_BODY}]}
        response = {'checks': [_supported()], 'unsupported_count': 0, 'decision': 'pass'}
        self.assertTrue(validate_response(content, response)['ok'])

    def test_validate_supported_quote_may_come_from_old_card(self):
        content = {'task': 't', 'sources': [{'text': '无关来源'}],
                   'existing': {'cards': [{'id': 'c1', 'version': 1, 'body': SOURCE_TEXT}]},
                   'operations': [{'op': 0, 'proposal_title': '', 'proposal_body': FAITHFUL_BODY}]}
        response = {'checks': [_supported()], 'unsupported_count': 0, 'decision': 'pass'}
        self.assertTrue(validate_response(content, response)['ok'])

    def test_validate_extra_attribute_fails_with_finding(self):
        content = {'task': 't', 'sources': [{'text': SOURCE_TEXT}],
                   'operations': [{'op': 0, 'proposal_title': '', 'proposal_body': EXTRA_BODY}]}
        response = {'checks': [_unsupported()], 'unsupported_count': 1, 'decision': 'fail'}
        result = validate_response(content, response)
        self.assertFalse(result['ok'])
        self.assertEqual(result['origin'], 'model')
        self.assertTrue(result['findings'])
        self.assertTrue(any('op 0' in f['finding'] for f in result['findings']))

    def test_validate_structural_failure_is_host_origin(self):
        content = {'task': 't', 'sources': [{'text': SOURCE_TEXT}],
                   'operations': [{'op': 0, 'proposal_title': '', 'proposal_body': FAITHFUL_BODY}]}
        result = validate_response(content, {'checks': [], 'unsupported_count': 0,
                                             'decision': 'pass'})
        self.assertFalse(result['ok'])
        self.assertEqual(result['origin'], 'host')

    def test_validate_rejects_missing_coverage_and_contradiction(self):
        content = {'task': 't', 'sources': [{'text': SOURCE_TEXT}],
                   'operations': [{'op': 0, 'proposal_title': '', 'proposal_body': FAITHFUL_BODY}]}
        self.assertFalse(validate_response(content, {'checks': [], 'unsupported_count': 0,
                                                     'decision': 'pass'})['ok'])
        self.assertFalse(validate_response(content, {'checks': [_unsupported()],
                                                     'unsupported_count': 0,
                                                     'decision': 'pass'})['ok'])

    def test_validate_rejects_non_exact_substring_and_missing_quote(self):
        content = {'task': 't', 'sources': [{'text': SOURCE_TEXT}],
                   'operations': [{'op': 0, 'proposal_title': '', 'proposal_body': FAITHFUL_BODY}]}
        bad = {'op': 0, 'claim': 'c', 'verdict': 'supported',
               'proposal_substring': '不存在的子串', 'source_quote': SOURCE_TEXT}
        self.assertFalse(validate_response(content, {'checks': [bad], 'unsupported_count': 0,
                                                     'decision': 'pass'})['ok'])
        noquote = dict(_supported(), source_quote='不在来源里')
        self.assertFalse(validate_response(content, {'checks': [noquote],
                                                     'unsupported_count': 0,
                                                     'decision': 'pass'})['ok'])

    def test_receipt_ok_rejects_boolean_and_drift(self):
        content = {'task': 't', 'sources': [], 'operations': []}
        binding = build_binding(content, 'ph', {'s': 1}, 'scope', 'contract')
        good = dict(binding, decision='pass', call_id='c1')
        self.assertTrue(receipt_ok(good, binding))
        self.assertFalse(receipt_ok(True, binding))
        self.assertFalse(receipt_ok(dict(good, content_hash='drift'), binding))
        self.assertFalse(receipt_ok(dict(good, decision='fail'), binding))


class _GateHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.e = Engine(self.root / 'state')
        self.e.init(self.root / 'vault'); self.addCleanup(self.e.store.close)
        (self.e.home / 'config.json').write_text(json.dumps({
            'knowledge_mode': 'evolving', 'model_command': ['stub'],
            'knowledge_daily_token_budget': 100000000,
            'knowledge_daily_call_budget': 100000}))
        self.ev = Evolution(self.e)
        self.card = self.ev.cards.seed('project', '冷冻柜规则', 'memory', '主题',
                                       '冷冻柜规则', OLD_BODY)
        # A real captured source in the ledger (not a made-up id), so the
        # freshness guard re-derives the live part text/version for real.
        self.rec = self.e.store.capture('project', 'session', 'user', 'ev', SOURCE_TEXT)
        self.ev.captured(self.rec['source_id'], 'user', 'session')
        self.calls = [0]

    def _cfg(self, **overrides):
        cfg = {'model_command': ['stub'], 'knowledge_max_output_tokens': 4096,
               'knowledge_daily_token_budget': 100000000,
               'knowledge_daily_call_budget': 100000, 'model_timeout_seconds': 30}
        cfg.update(overrides)
        return cfg

    def _source_part(self):
        src = self.e.store.source(self.rec['source_id'])
        part = next(self.ev._source_parts(src))
        return {'source_id': part['source_id'], 'version': part['version'],
                'part': part['part'], 'text': part['text']}

    def _operations(self, body):
        return [{'op': 'update', 'id': self.card['id'],
                 'expected_version': self.card['version'], 'title': '冷冻柜规则',
                 'body': body, 'evidence': [{'source_id': self.rec['source_id'],
                                             'version': 1, 'quote': SOURCE_TEXT}],
                 'reason': '修订'}]

    def _driver(self, responses):
        def model(command, prompt, timeout, env=None):
            self.calls[0] += 1
            return responses[self.calls[0] - 1], {}
        return model

    def _no_call(self):
        def model(command, prompt, timeout, env=None):
            raise AssertionError('unexpected model dispatch')
        return model

    def _call_rows(self, wid):
        return [dict(r) for r in self.e.db.execute(
            'SELECT * FROM knowledge_calls WHERE work_id=?', (wid,)).fetchall()]

    def _fresh(self, wid='w1', token='tok'):
        part = self._source_part()
        card = self.ev.cards.get(self.card['id'])
        sources = [part]
        base = {
            'project': 'project',
            'sources': sources,
            'source_snapshot': {part['source_id']: part['version']},
            'scope_fingerprint': self.ev._scope_fingerprint('project'),
            'contract': self.ev._contract_fingerprint(),
            'stage_calls': {'compile': 0, 'verify': 0},
            'read_sets': {'compile': {}, 'verify': {
                card['id']: {'version': card['version'],
                             'body_sha256': body_sha256(card['body'])}}},
            'read_feedback': {'compile': [], 'verify': []},
            'existing': {'cards': [{'id': card['id'], 'version': card['version'],
                                    'body': card['body']}]},
            'catalog_cards': [{'id': card['id'], 'version': card['version'],
                               'project': 'project', 'state': card.get('state'),
                               'basis_cards': []}],
        }
        with self.e.transaction():
            self.e.db.execute(
                'INSERT OR REPLACE INTO knowledge_work(id,project,session,inputs,state,'
                'lease_token,lease_until,created_at,bundle) VALUES(?,?,?,?,?,?,?,?,?)',
                (wid, 'project', 'session', json.dumps(sources), 'running', token,
                 time.time() + 600, time.time(), json.dumps(base)))
        job = dict(self.e.db.execute('SELECT * FROM knowledge_work WHERE id=?',
                                     (wid,)).fetchone())
        return base, job

    def _settled_failed_call(self, wid, call_id):
        """A settled transport call that later failed SEMANTIC validation."""
        day = time.strftime('%Y-%m-%d', time.gmtime())
        with self.e.transaction():
            self.e.db.execute(
                'INSERT INTO knowledge_calls(id,work_id,stage,day,reserved,'
                'actual_input,actual_output,elapsed,error,created_at)'
                ' VALUES(?,?,?,?,?,?,?,?,?,?)',
                (call_id, wid, 'fact_review', day, 1, 0, 0, 0.01, None, time.time()))
            self.e.db.execute(
                'INSERT INTO knowledge_failures(work_id,state,error,created_at)'
                ' VALUES(?,?,?,?)',
                (wid, FACT_REVIEW_REJECTED_STATE,
                 json.dumps({'call_id': call_id, 'decision': 'fail'}), time.time()))

    def _expected_for(self, base, operations):
        content = self.ev._fact_review_content(base, operations)
        return self.ev._fact_review_binding(base, operations, content)

    def _receipt_records(self, wid):
        rows = self.e.db.execute(
            'SELECT error FROM knowledge_failures WHERE work_id=? AND state=?',
            (wid, FACT_REVIEW_RECEIPT_STATE)).fetchall()
        return [json.loads(r[0]) for r in rows if r[0]]

    def test_pass_settles_receipt_and_consumes_one_slot(self):
        base, job = self._fresh()
        operations = self._operations(FAITHFUL_BODY)
        with patch('tongle.worker.invoke_json', side_effect=self._driver([_pass_response()])):
            self.ev._fact_review_gate(job, base, operations, 'tok', self._cfg())
        self.assertEqual(base['stage_calls']['verify'], 1)
        self.assertIn('fact_review', base)
        rows = self._call_rows('w1')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['stage'], 'fact_review')
        self.assertIsNotNone(rows[0]['elapsed'])
        self.assertIsNone(rows[0]['error'])
        # The independent host audit row exists and binds the real call id.
        records = self._receipt_records('w1')
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['call_id'], base['fact_review']['call_id'])
        # Re-entry validates receipt + host audit and does not dispatch again.
        with patch('tongle.worker.invoke_json', side_effect=self._no_call()):
            self.ev._fact_review_gate(job, base, operations, 'tok', self._cfg())
        self.assertEqual(self.calls[0], 1)
        self.assertEqual(base['stage_calls']['verify'], 1)

    def test_extra_attribute_routes_model_origin_envelope(self):
        base, job = self._fresh(wid='w2')
        operations = self._operations(EXTRA_BODY)
        with patch('tongle.worker.invoke_json', side_effect=self._driver([_fail_response()])):
            with self.assertRaises(ValueError):
                self.ev._fact_review_gate(job, base, operations, 'tok', self._cfg())
        self.assertEqual(base['stage_calls']['verify'], 1)
        self.assertNotIn('fact_review', base)
        self.assertTrue(base['feedback'])
        # Model-supplied proposal text is NOT laundered into the host channel.
        self.assertEqual(base['feedback_origin'], FEEDBACK_ORIGIN_MODEL)
        envelope = base['feedback_envelope']
        self.assertEqual(envelope['stage'], 'fact_review')
        self.assertEqual(envelope['call_id'], self._call_rows('w2')[0]['id'])
        # The transport call itself succeeded: usage is intact, error NULL.
        self.assertIsNone(self._call_rows('w2')[0]['error'])
        self.assertIsNotNone(self._call_rows('w2')[0]['elapsed'])
        # Semantic rejection recorded separately, never as a transport error.
        rejected = self.e.db.execute(
            "SELECT error FROM knowledge_failures WHERE work_id='w2' AND state=?",
            (FACT_REVIEW_REJECTED_STATE,)).fetchall()
        self.assertTrue(rejected)
        # The replay path accepts a legitimate fact_review envelope.
        self.assertEqual(self.ev._replay_feedback(base, job), base['feedback'])
        # A missing call id is withheld by the existing envelope validation.
        broken = dict(base)
        broken['feedback_envelope'] = dict(envelope, call_id='')
        self.assertEqual(self.ev._replay_feedback(broken, job),
                         [{'finding': STALE_FEEDBACK_NOTICE}])

    def test_malformed_reply_is_host_origin_no_envelope(self):
        base, job = self._fresh(wid='w7')
        operations = self._operations(FAITHFUL_BODY)
        bad = {'checks': [], 'unsupported_count': 0, 'decision': 'pass'}
        with patch('tongle.worker.invoke_json', side_effect=self._driver([bad])):
            with self.assertRaises(ValueError):
                self.ev._fact_review_gate(job, base, operations, 'tok', self._cfg())
        self.assertEqual(base['feedback_origin'], FEEDBACK_ORIGIN_HOST)
        self.assertNotIn('feedback_envelope', base)
        self.assertNotIn('fact_review', base)
        self.assertEqual(base['stage_calls']['verify'], 1)
        self.assertEqual(self._receipt_records('w7'), [])

    def test_budgetwait_does_not_consume_slot(self):
        base, job = self._fresh(wid='w3')
        operations = self._operations(FAITHFUL_BODY)
        with patch('tongle.worker.invoke_json', side_effect=self._driver([{}])):
            with self.assertRaises(BudgetWait):
                self.ev._fact_review_gate(job, base, operations, 'tok',
                                          self._cfg(knowledge_daily_call_budget=0))
        self.assertEqual(base['stage_calls']['verify'], 0)
        self.assertEqual(self._call_rows('w3'), [])

    def test_forged_boolean_receipt_cannot_bypass(self):
        base, job = self._fresh(wid='w4')
        operations = self._operations(FAITHFUL_BODY)
        base['fact_review'] = True
        with patch('tongle.worker.invoke_json', side_effect=self._driver([_pass_response()])):
            self.ev._fact_review_gate(job, base, operations, 'tok', self._cfg())
        self.assertEqual(base['stage_calls']['verify'], 1)
        self.assertIsInstance(base['fact_review'], dict)

    def test_forged_settled_failed_call_cannot_bypass(self):
        base, job = self._fresh(wid='w6')
        operations = self._operations(FAITHFUL_BODY)
        # A prior attempt settled the transport call with error NULL but FAILED
        # the audit: its id is a plausible forgery anchor.
        self._settled_failed_call('w6', 'deadbeef' * 4)
        expected = self._expected_for(base, operations)
        base['fact_review'] = build_receipt(
            expected, 'deadbeef' * 4, {'decision': 'pass', 'unsupported_count': 0})
        with patch('tongle.worker.invoke_json', side_effect=self._driver([_pass_response()])):
            self.ev._fact_review_gate(job, base, operations, 'tok', self._cfg())
        # The forged anchor is rejected: a fresh call is dispatched and the
        # persisted receipt names the NEW call, never the failed one.
        self.assertEqual(self.calls[0], 1)
        self.assertEqual(base['stage_calls']['verify'], 1)
        self.assertNotEqual(base['fact_review']['call_id'], 'deadbeef' * 4)
        records = self._receipt_records('w6')
        self.assertTrue(any(r['call_id'] == base['fact_review']['call_id'] for r in records))

    def test_missing_callid_receipt_is_not_reused(self):
        base, job = self._fresh(wid='w8')
        operations = self._operations(FAITHFUL_BODY)
        expected = self._expected_for(base, operations)
        forged = build_receipt(expected, None, {'decision': 'pass', 'unsupported_count': 0})
        base['fact_review'] = forged
        with patch('tongle.worker.invoke_json', side_effect=self._driver([_pass_response()])):
            self.ev._fact_review_gate(job, base, operations, 'tok', self._cfg())
        self.assertEqual(self.calls[0], 1)
        self.assertIsInstance(base['fact_review']['call_id'], str)

    def test_changed_proposal_invalidates_receipt(self):
        base, job = self._fresh(wid='w5')
        operations = self._operations(FAITHFUL_BODY)
        with patch('tongle.worker.invoke_json', side_effect=self._driver([_pass_response()])):
            self.ev._fact_review_gate(job, base, operations, 'tok', self._cfg())
        # Same frozen proposal: no new call.
        with patch('tongle.worker.invoke_json', side_effect=self._no_call()):
            self.ev._fact_review_gate(job, base, operations, 'tok', self._cfg())
        self.assertEqual(self.calls[0], 1)
        # A different proposal must re-audit even though the old call settled.
        changed = self._operations(FAITHFUL_BODY + '新增一句。')
        changed[-1]['reason'] = '再次修订'
        response = {'checks': [_supported(substring=FAITHFUL_BODY + '新增一句。')],
                    'unsupported_count': 0, 'decision': 'pass'}
        self.calls[0] = 0
        with patch('tongle.worker.invoke_json', side_effect=self._driver([response])):
            self.ev._fact_review_gate(job, base, changed, 'tok', self._cfg())
        self.assertEqual(self.calls[0], 1)
        self.assertEqual(base['stage_calls']['verify'], 2)

    def test_changed_source_text_same_version_rejects_before_reuse(self):
        base, job = self._fresh(wid='w9')
        operations = self._operations(FAITHFUL_BODY)
        with patch('tongle.worker.invoke_json', side_effect=self._driver([_pass_response()])):
            self.ev._fact_review_gate(job, base, operations, 'tok', self._cfg())
        self.assertEqual(self.calls[0], 1)
        # Rewrite the SAME version's text in the live ledger: the receipt must
        # not be reused and no dispatch may happen on moved evidence.
        new_text = SOURCE_TEXT + '额外一句。'
        with self.e.transaction():
            self.e.db.execute(
                'UPDATE versions SET text=?,original=?,hash=? WHERE source_id=? AND version=?',
                (new_text, new_text.encode('utf-8'),
                 hashlib.sha256(new_text.encode('utf-8')).hexdigest(),
                 self.rec['source_id'], 1))
        with patch('tongle.worker.invoke_json', side_effect=self._no_call()):
            with self.assertRaises(SelectionChanged):
                self.ev._fact_review_gate(job, base, operations, 'tok', self._cfg())
        self.assertEqual(self.calls[0], 1)

    def test_changed_card_body_rejects_before_reuse(self):
        base, job = self._fresh(wid='w10')
        operations = self._operations(FAITHFUL_BODY)
        with patch('tongle.worker.invoke_json', side_effect=self._driver([_pass_response()])):
            self.ev._fact_review_gate(job, base, operations, 'tok', self._cfg())
        with self.e.transaction():
            self.e.db.execute('UPDATE knowledge_cards SET body=? WHERE id=?',
                              (OLD_BODY + '被并发改写。', self.card['id']))
        with patch('tongle.worker.invoke_json', side_effect=self._no_call()):
            with self.assertRaises(SelectionChanged):
                self.ev._fact_review_gate(job, base, operations, 'tok', self._cfg())
        self.assertEqual(self.calls[0], 1)

    def test_changed_card_version_rejects_before_reuse(self):
        base, job = self._fresh(wid='w12')
        operations = self._operations(FAITHFUL_BODY)
        with patch('tongle.worker.invoke_json', side_effect=self._driver([_pass_response()])):
            self.ev._fact_review_gate(job, base, operations, 'tok', self._cfg())
        with self.e.transaction():
            self.e.db.execute('UPDATE knowledge_cards SET version=version+1 WHERE id=?',
                              (self.card['id'],))
        with patch('tongle.worker.invoke_json', side_effect=self._no_call()):
            with self.assertRaises(SelectionChanged):
                self.ev._fact_review_gate(job, base, operations, 'tok', self._cfg())
        self.assertEqual(self.calls[0], 1)

    def test_scope_drift_rejects_before_reuse(self):
        base, job = self._fresh(wid='w11')
        operations = self._operations(FAITHFUL_BODY)
        with patch('tongle.worker.invoke_json', side_effect=self._driver([_pass_response()])):
            self.ev._fact_review_gate(job, base, operations, 'tok', self._cfg())
        base['scope_fingerprint'] = 'drifted'
        with patch('tongle.worker.invoke_json', side_effect=self._no_call()):
            with self.assertRaises(SelectionChanged):
                self.ev._fact_review_gate(job, base, operations, 'tok', self._cfg())
        self.assertEqual(self.calls[0], 1)


if __name__ == '__main__':
    unittest.main()
