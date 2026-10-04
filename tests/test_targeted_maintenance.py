"""Targeted regressions for the independently reviewed maintenance bypasses.

Each bypass is exercised through the real ``Evolution.process`` /
``_stage_loop`` path (or the actual ``_hydrate``/``_call`` transport), not only
through helpers. Model fixtures are synthetic: they prove machine behavior, not
semantic quality. No live vault, credentials or network is touched.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tongle.engine import Engine
from tongle.evolution import Evolution, CONSUMPTION_MAX_ATTEMPTS
from tongle.evidence_selection import basis_closure_report
from tongle.knowledge_files import KnowledgeFiles

try:  # sibling import under ``unittest discover -s tests``
    from fact_review_stub import with_fact_audit
except ImportError:  # pragma: no cover - package-import invocation
    from tests.fact_review_stub import with_fact_audit

ROOT = Path(__file__).resolve().parents[1]


def _payload_alias(data, key):
    """Return the alias the model was handed for a card key.

    Payloads are identity-encoded before transport, so a fixture must recover
    the short alias from the catalog/cards it actually received instead of
    assuming the real 32-hex id survived to the model.
    """
    existing = data.get('existing') or {}
    for row in list(existing.get('cards') or []) + list(existing.get('catalog') or []):
        if row.get('key') == key and isinstance(row.get('id'), str):
            return row['id']
    raise AssertionError('no payload alias for card key ' + repr(key))


class _Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.e = Engine(self.root / 'state')
        self.e.init(self.root / 'vault'); self.addCleanup(self.e.store.close)
        self.cfg = {'knowledge_mode': 'evolving', 'model_command': ['synthetic-stub'],
                    'knowledge_quiet_seconds': 0}
        self.write_cfg(); self.ev = Evolution(self.e)

    def write_cfg(self):
        (self.e.home / 'config.json').write_text(json.dumps(self.cfg))

    def capture(self, text, event='one', role='user', session='session', receipt_id=None):
        rec = self.e.store.capture('project', session, role, event, text)
        self.ev.captured(rec['source_id'], role, session, receipt_id=receipt_id)
        return rec

    def hidden(self, key, body, topic='无关'):
        return self.ev.cards.seed('project', key, 'memory', topic, key, body)

    def offer(self, session='s2'):
        card = self.ev.cards.seed('project', '使用卡', 'memory', '方案', '使用卡',
                                  '必须先完成方案。')
        KnowledgeFiles(self.e, self.ev.cards).publish()
        got = self.ev.context('project', '继续', session=session)
        return card, got

    def work(self):
        row = self.e.db.execute('SELECT * FROM knowledge_work LIMIT 1').fetchone()
        return dict(row) if row else None

    def run_model(self, model, force=True):
        with patch('tongle.worker.invoke_json', side_effect=model):
            return self.ev.process(force=force)


class ReadBypassTests(_Harness):
    def test_third_call_final_naming_unread_target_fails_stage_limit(self):
        a = self.hidden('隐藏甲', '正文甲')
        b = self.hidden('隐藏乙', '正文乙')
        c = self.hidden('隐藏丙', '正文丙')
        self.capture('新规则生效。')
        stages = []
        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'proposal' in data:
                return {'approved': True, 'findings': []}, {}
            stages.append(data)
            if len(stages) == 1:
                return {'needs_cards': [a['id']]}, {}
            if len(stages) == 2:
                return {'needs_cards': [b['id']]}, {}
            return {'operations': [{'op': 'update', 'id': c['id'], 'expected_version': 1,
                                    'body': '正文丙完整版本。', 'evidence': [], 'reason': '补充'}]}, {}
        with patch('tongle.worker.invoke_json', side_effect=model) as call:
            result = self.ev.process(force=True)
        self.assertEqual(result['failed'], 1)
        row = self.work()
        self.assertEqual(row['state'], 'failed')
        self.assertIn('stage call limit', row['error'])
        self.assertEqual(call.call_count, 3)
        # Loaded-but-unsent cards are never accepted as read: a and b were
        # dispatched on following calls, the third-call target c was not.
        read = json.loads(row['bundle'])['read_sets']['compile']
        self.assertIn(a['id'], read)
        self.assertIn(b['id'], read)
        self.assertNotIn(c['id'], read)

    def test_hydrate_rejects_stale_version_and_scope(self):
        card = self.hidden('快照卡', '旧正文')
        live = self.ev.cards.get(card['id'])
        base = {'existing': {'catalog': [{'id': live['id'], 'project': live['project'],
                 'version': live['version'] + 1}], 'cards': [], 'catalog_complete': True}}
        added, rejected, _ = self.ev._hydrate(base, [live['id']], set(), 50000)
        self.assertEqual(added, [])
        self.assertEqual(rejected[0]['reason'], 'stale_catalog_version')
        base['existing']['catalog'][0]['version'] = live['version']
        base['existing']['catalog'][0]['project'] = 'other'
        added, rejected, _ = self.ev._hydrate(base, [live['id']], set(), 50000)
        self.assertEqual(added, [])
        self.assertEqual(rejected[0]['reason'], 'scope_changed')


class ReadFeedbackTests(_Harness):
    """Stateless read feedback and the redundant-request bounded loop."""

    def supplied_mandatory(self):
        """A card tied to the batch source, so it is supplied up front."""
        src = self.capture('规则内容。')
        return self.ev.cards.seed(
            'project', '已有卡', 'memory', '方案', '已有卡', '正文必须遵守。',
            evidence=[{'source_id': src['source_id'], 'version': 1,
                       'quote': '规则内容。'}])

    def test_already_supplied_then_final_succeeds_two_compile_calls(self):
        self.supplied_mandatory()
        compile_calls = []
        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'proposal' in data:
                return {'approved': True, 'findings': []}, {}
            compile_calls.append(data)
            if len(compile_calls) == 1:
                return {'needs_cards': [_payload_alias(data, '已有卡')]}, {}
            return {'operations': []}, {}
        result = self.run_model(model)
        self.assertEqual(result['processed'], 1)
        self.assertEqual(result['failed'], 0)
        self.assertEqual(len(compile_calls), 2)
        # The card was already in the first payload, so it is in existing.cards
        # from the start; the redundant request neither re-reads nor fails.
        alias = _payload_alias(compile_calls[0], '已有卡')
        self.assertIn(alias, {c['id'] for c in compile_calls[0]['existing']['cards']})
        second = compile_calls[1]
        self.assertEqual(second['stage_progress'],
                         {'call_number': 2, 'max_calls': 3, 'remaining_calls': 1})
        self.assertIn({'id': alias, 'status': 'already_supplied'},
                      second['read_feedback'])

    def test_newly_loaded_feedback_and_body_in_next_payload(self):
        # The target card's topic overlaps the batch so it appears in the
        # bounded candidate hint, but its body exceeds the (deliberately small)
        # initial body budget, so it is not supplied until explicitly asked for.
        self.cfg['knowledge_initial_body_chars'] = 2000
        self.write_cfg()
        self.capture('新规则。')
        self.ev.cards.seed('project', '待读卡', 'memory', '新规则', '待读卡',
                           '完整正文' + '补充内容' * 700)
        compile_calls = []
        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'proposal' in data:
                return {'approved': True, 'findings': []}, {}
            compile_calls.append(data)
            if len(compile_calls) == 1:
                return {'needs_cards': [_payload_alias(data, '待读卡')]}, {}
            return {'operations': []}, {}
        result = self.run_model(model)
        self.assertEqual(result['processed'], 1)
        self.assertEqual(len(compile_calls), 2)
        alias = _payload_alias(compile_calls[0], '待读卡')
        second = compile_calls[1]
        self.assertIn({'id': alias, 'status': 'newly_loaded'},
                      second['read_feedback'])
        self.assertIn(alias, {c['id'] for c in second['existing']['cards']})

    def test_mixed_available_and_oversize_request_fails_without_final(self):
        # Both cards overlap the batch topic (so they are catalog candidates);
        # the small initial body budget keeps the readable one unsupplied until
        # requested, and the oversize one still trips the whole-batch refusal.
        self.cfg['knowledge_initial_body_chars'] = 2000
        self.write_cfg()
        self.capture('新规则。')
        self.ev.cards.seed('project', '可读卡', 'memory', '新规则', '可读卡',
                           '普通正文' + '补充内容' * 700)
        self.ev.cards.seed('project', '超大卡', 'memory', '新规则', '超大卡',
                           '完整条件。' * 11000)
        compile_calls = []
        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            self.assertNotIn('proposal', data, 'No verifier after unresolved reads')
            compile_calls.append(data)
            self.assertFalse(any(c.get('key') == '可读卡' for c in data['existing']['cards']))
            return {'needs_cards': [_payload_alias(data, '可读卡'),
                                    _payload_alias(data, '超大卡')]}, {}
        result = self.run_model(model)
        # Every failed batch delivers zero sibling bodies; repeated invalid
        # requests exhaust the same three slots without a final or commit.
        self.assertEqual(result['failed'], 1)
        self.assertEqual(len(compile_calls), 3)
        row = self.work()
        self.assertEqual(row['state'], 'failed')
        self.assertIn('stage call limit', row['error'])
        self.assertEqual(self.ev.db.execute('SELECT COUNT(*) FROM knowledge_batches').fetchone()[0], 0)

    def test_always_rerequest_already_supplied_stops_at_three(self):
        card = self.supplied_mandatory()
        compile_calls = []
        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'proposal' in data:
                return {'approved': True, 'findings': []}, {}
            compile_calls.append(data)
            return {'needs_cards': [card['id']]}, {}
        with patch('tongle.worker.invoke_json', side_effect=model) as call:
            result = self.ev.process(force=True)
        self.assertEqual(result['failed'], 1)
        row = self.work()
        self.assertEqual(row['state'], 'failed')
        self.assertIn('stage call limit', row['error'])
        self.assertEqual(len(compile_calls), 3)
        self.assertEqual(call.call_count, 3)
        # The repeated request is bounded and the verify stage never ran.
        self.assertTrue(all('proposal' not in data for data in compile_calls))

    def test_empty_needs_cards_is_not_accepted_as_final(self):
        self.supplied_mandatory()
        compile_calls = []
        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'proposal' in data:
                return {'approved': True, 'findings': []}, {}
            compile_calls.append(data)
            if len(compile_calls) == 1:
                return {'needs_cards': []}, {}
            return {'operations': []}, {}
        result = self.run_model(model)
        self.assertEqual(result['processed'], 1)
        self.assertEqual(len(compile_calls), 2)
        self.assertIn({'id': None, 'status': 'empty_request_final_required'},
                      compile_calls[1]['read_feedback'])

    def test_foreign_needs_cards_request_still_blocks(self):
        self.supplied_mandatory()
        compile_calls = []
        def model(cmd, prompt, timeout, env=None):
            compile_calls.append(json.loads(prompt))
            return {'needs_cards': ['f' * 32]}, {}
        with patch('tongle.worker.invoke_json', side_effect=model) as call:
            result = self.ev.process(force=True)
        self.assertEqual(result['failed'], 1)
        row = self.work()
        self.assertIn('stage call limit', row['error'])
        self.assertEqual(call.call_count, 3)


class MandatoryClosureTests(_Harness):
    def test_noop_with_oversize_mandatory_dependency_fails(self):
        src = self.capture('规则来自这份材料。')
        self.ev.cards.seed('project', '超大依据', 'memory', '方案', '超大依据',
                           '完整条件。' * 11000,
                           evidence=[{'source_id': src['source_id'], 'version': 1,
                                      'quote': self.e.store.source(src['source_id'])['text']}])
        called = []
        def model(cmd, prompt, timeout, env=None):
            called.append(json.loads(prompt)); return {'operations': []}, {}
        result = self.run_model(model)
        self.assertEqual(called, [])          # blocked before any no-op is asked
        self.assertEqual(result['failed'], 1)
        row = self.work()
        self.assertEqual(row['state'], 'failed')
        self.assertIn('mandatory cards exceed body budget', row['error'])

    def test_foreign_basis_dependency_blocks_final(self):
        card = self.hidden('目标卡', '普通正文')
        self.capture('新的输入。')
        foreign = 'f' * 32
        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'proposal' in data:
                return {'approved': True, 'findings': []}, {}
            return {'operations': [{'op': 'update', 'id': card['id'], 'expected_version': 1,
                    'body': '更新后的正文。', 'evidence': [], 'reason': '依据',
                    'basis_cards': [{'card_id': foreign, 'version': 1}]}]}, {}
        result = self.run_model(model)
        self.assertEqual(result['failed'], 1)
        row = self.work()
        self.assertIn('unresolved basis dependency', row['error'])

    def test_closure_report_flags_overflow(self):
        cards = [{'id': '%032d' % i, 'basis_cards': [{'card_id': '%032d' % (i + 1)}]}
                 for i in range(3)]
        ids, problems = basis_closure_report(cards, ['%032d' % 0], limit=2)
        self.assertEqual(len(ids), 2)
        self.assertTrue(any(p['reason'] == 'overflow' for p in problems))


class CrashRecoveryTests(_Harness):
    def _update_op(self, data, card):
        s = data['sources'][-1]
        return {'op': 'update', 'id': card['id'], 'expected_version': 1,
                'body': '更新正文。',
                'evidence': [{'source_id': s['source_id'], 'version': s['version'],
                              'quote': s['text']}], 'reason': '补充'}

    def test_transient_failure_preserves_counters_readsets_and_proposal(self):
        self.capture('材料。')
        card = self.hidden('保留卡', '普通正文')
        def fail(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'proposal' in data:
                raise RuntimeError('model timeout')
            return {'operations': [self._update_op(data, card)]}, {}
        with patch('tongle.worker.invoke_json', side_effect=with_fact_audit(fail)):
            self.ev.process(force=True)
        row = self.work()
        self.assertEqual(row['state'], 'retry')
        self.assertIsNotNone(row['proposal'])
        bundle = json.loads(row['bundle'])
        self.assertGreaterEqual(bundle['stage_calls']['verify'], 1)
        self.assertGreaterEqual(bundle['stage_calls']['compile'], 1)
        self.assertIn(card['id'], bundle['read_sets']['compile'])
        with self.e.transaction():
            self.e.db.execute("UPDATE knowledge_work SET state='pending'")
        stages = []
        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            stages.append('verify' if 'proposal' in data else 'compile')
            if 'proposal' in data:
                return {'approved': True, 'findings': []}, {}
            return {'operations': []}, {}
        with patch('tongle.worker.invoke_json', side_effect=with_fact_audit(model)):
            result = self.ev.process(force=True)
        self.assertEqual(result['processed'], 1)
        self.assertEqual(stages, ['verify'])   # compile was not re-run

    def test_budget_wait_does_not_consume_a_stage_slot(self):
        self.capture('材料。')
        day = time.strftime('%Y-%m-%d', time.gmtime())
        with self.e.transaction():
            self.e.db.execute(
                "INSERT INTO knowledge_calls(id,work_id,stage,day,reserved,actual_input,"
                "actual_output,elapsed,created_at) VALUES('used','w','compile',?,?,1,1,1.0,?)",
                (day, 1, time.time()))
        self.cfg['knowledge_daily_call_budget'] = 1
        self.write_cfg()
        with patch('tongle.worker.invoke_json') as call:
            result = self.ev.process(force=True)
            call.assert_not_called()
        self.assertTrue(result['budget_wait'])
        bundle = json.loads(self.work()['bundle'])
        self.assertEqual(bundle['stage_calls']['compile'], 0)


class LegacyRecoveryTests(_Harness):
    def test_legacy_coverage_recovered_once(self):
        self.capture('材料。'); self.ev.queue(force=True)
        row = self.work()
        self.ev.db.execute("UPDATE knowledge_work SET state='coverage_wait' WHERE id=?", (row['id'],))
        self.e.db.commit()
        first = self.ev.recover_legacy_coverage()
        self.assertEqual(first['recovered'], 1)
        self.assertTrue(self.ev.recover_legacy_coverage()['already_done'])
        self.assertEqual(self.ev.db.execute('SELECT state FROM knowledge_work WHERE id=?',
                                            (row['id'],)).fetchone()['state'], 'pending')

    def test_legacy_recovery_excludes_committed(self):
        self.capture('材料。'); self.ev.queue(force=True)
        row = self.work()
        self.ev.db.execute("UPDATE knowledge_work SET state='coverage_wait' WHERE id=?", (row['id'],))
        self.ev.db.execute("INSERT INTO knowledge_batches(batch_id,project,op_hash,review_hash,"
                           "changed_ids,created_at) VALUES(?,?,?,?,?,?)",
                           (row['id'], 'project', 'o', 'r', '[]', time.time()))
        self.e.db.commit()
        self.assertEqual(self.ev.recover_legacy_coverage()['recovered'], 0)


class ConsumptionTests(_Harness):
    def _checks_model(self, verdict='applied', quote='完成方案'):
        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            return {'consumption_checks': [{'receipt_id': data['receipt_id'],
                    'evidence': [{'card_id': data['offered'][0]['id'], 'verdict': verdict,
                                  'quote': quote, 'reason': '核对实际输出'}]}]}, {}
        return model

    def test_awaiting_check_runs_without_main_work(self):
        card, got = self.offer()
        self.capture('接下来完成方案。', 'answer', 'assistant', 's2')
        with patch('tongle.worker.invoke_json', side_effect=self._checks_model()):
            result = self.ev.process(force=True)
        self.assertEqual(result['consumption']['called'], 1)
        self.assertEqual(result['consumption']['checked'], 1)
        row = self.e.db.execute('SELECT state,checks FROM knowledge_consumption WHERE id=?',
                                (got['receipt_id'],)).fetchone()
        self.assertEqual(row['state'], 'checked')
        checks = json.loads(row['checks'])
        self.assertEqual(checks['evidence'][0]['card_id'], card['id'])
        self.assertEqual(checks['evidence'][0]['verdict'], 'applied')

    def test_oversize_first_does_not_starve_next(self):
        out = self.capture('实际输出完成方案。', 'out', 'assistant', 's4')
        huge = json.dumps([{'id': 'a' * 32, 'version': 1, 'title': 'x',
                            'body': 'b' * 200000, 'delivery': 'full'}])
        normal = json.dumps([{'id': 'c' * 32, 'version': 1, 'title': 'n',
                              'body': '必须先完成方案。', 'delivery': 'full'}])
        now = time.time()
        with self.e.transaction():
            self.e.db.execute(
                "INSERT INTO knowledge_consumption(id,project,session,query,offered,"
                "output_source,state,created_at) VALUES(?,?,?,?,?,?,'awaiting_check',?)",
                ('0' * 32, 'project', 's4', 'q', huge, out['source_id'], now))
            self.e.db.execute(
                "INSERT INTO knowledge_consumption(id,project,session,query,offered,"
                "output_source,state,created_at) VALUES(?,?,?,?,?,?,'awaiting_check',?)",
                ('1' * 32, 'project', 's4', 'q', normal, out['source_id'], now + 1))
        with patch('tongle.worker.invoke_json', side_effect=self._checks_model()):
            result = self.ev.process(force=True)
        self.assertEqual(result['consumption']['called'], 1)
        self.assertEqual(self.e.db.execute('SELECT state FROM knowledge_consumption WHERE id=?',
                                           ('1' * 32,)).fetchone()['state'], 'checked')
        first = self.e.db.execute('SELECT state,checks FROM knowledge_consumption WHERE id=?',
                                  ('0' * 32,)).fetchone()
        self.assertEqual(first['state'], 'unknown')
        self.assertEqual(json.loads(first['checks'])['reason'], 'oversize_offered')

    def test_partial_and_invalid_evidence_never_checked(self):
        card, got = self.offer()
        self.capture('完成方案的输出。', 'answer', 'assistant', 's2')
        offered = self.ev._feedback_inputs({'project': 'project', 'session': 's2'})
        self.ev._save_checks(offered, [{'receipt_id': got['receipt_id'],
            'evidence': [{'card_id': card['id'], 'verdict': 'applied', 'quote': '不存在的引文'}]}])
        row = self.e.db.execute('SELECT state,checks FROM knowledge_consumption WHERE id=?',
                                (got['receipt_id'],)).fetchone()
        self.assertNotEqual(row['state'], 'checked')
        self.assertEqual(json.loads(row['checks'])['evidence'], [])
        self.ev._save_checks(offered, [{'receipt_id': got['receipt_id'],
            'evidence': [{'card_id': card['id'], 'verdict': 'not_used', 'quote': '', 'reason': '未使用'}]}])
        row = self.e.db.execute('SELECT state FROM knowledge_consumption WHERE id=?',
                                (got['receipt_id'],)).fetchone()
        self.assertEqual(row['state'], 'checked')
        self.assertEqual(self.e.db.execute('SELECT COUNT(*) FROM knowledge_feedback').fetchone()[0], 0)

    def test_bounded_retry_exhausts_to_unknown(self):
        card, got = self.offer()
        self.capture('完成方案的输出。', 'answer', 'assistant', 's2')
        def boom(cmd, prompt, timeout, env=None):
            raise RuntimeError('consumption unavailable')
        for _ in range(CONSUMPTION_MAX_ATTEMPTS):
            with patch('tongle.worker.invoke_json', side_effect=boom):
                self.ev.process(force=True)
        attempts = self.e.db.execute('SELECT attempts FROM knowledge_consumption_lease WHERE receipt_id=?',
                                     (got['receipt_id'],)).fetchone()['attempts']
        self.assertEqual(attempts, CONSUMPTION_MAX_ATTEMPTS)
        with patch('tongle.worker.invoke_json', side_effect=boom) as call:
            self.ev.process(force=True)
            call.assert_not_called()
        row = self.e.db.execute('SELECT state,checks FROM knowledge_consumption WHERE id=?',
                                (got['receipt_id'],)).fetchone()
        self.assertEqual(row['state'], 'unknown')
        self.assertEqual(json.loads(row['checks'])['reason'], 'attempts_exhausted')

    def test_misapplied_yields_single_feedback_item(self):
        card, got = self.offer()
        self.capture('完成方案的输出。', 'answer', 'assistant', 's2', receipt_id=got['receipt_id'])
        with patch('tongle.worker.invoke_json', side_effect=self._checks_model('misapplied')):
            self.ev.process(force=True)
        self.assertEqual(self.e.db.execute('SELECT COUNT(*) FROM knowledge_feedback').fetchone()[0], 1)
        self.assertEqual(self.e.db.execute(
            "SELECT COUNT(*) FROM sources WHERE session='knowledge-feedback'").fetchone()[0], 0)

    def test_consumption_error_stops_after_one_attempt(self):
        card, got = self.offer()
        answer = self.capture('完成方案的输出。', 'answer', 'assistant', 's2')
        with self.e.transaction():
            self.e.db.execute(
                "INSERT INTO knowledge_consumption(id,project,session,query,offered,"
                "output_source,state,created_at) VALUES(?,?,?,?,?,?,'awaiting_check',?)",
                ('z' * 32, 'project', 's2', 'q',
                 json.dumps([{'id': card['id'], 'version': 1, 'title': 'x',
                              'body': '', 'delivery': 'full'}]),
                 answer['source_id'], time.time() + 1))
        def boom(cmd, prompt, timeout, env=None):
            raise RuntimeError('model down')
        with patch('tongle.worker.invoke_json', side_effect=boom) as call:
            result = self.ev._independent_consumption(self.cfg)
        self.assertEqual(result['called'], 1)
        self.assertEqual(call.call_count, 1)
        for rid in (got['receipt_id'], 'z' * 32):
            self.assertEqual(self.e.db.execute(
                'SELECT state FROM knowledge_consumption WHERE id=?', (rid,)).fetchone()['state'],
                'awaiting_check')

    def test_live_lease_skipped_before_attempt_limit(self):
        card, got = self.offer()
        self.capture('完成方案的输出。', 'answer', 'assistant', 's2')
        with self.e.transaction():
            self.e.db.execute(
                "INSERT INTO knowledge_consumption_lease(receipt_id,attempts,lease_token,"
                "lease_until,updated_at) VALUES(?,?,?,?,?)",
                (got['receipt_id'], CONSUMPTION_MAX_ATTEMPTS, 'live', time.time() + 600, time.time()))
        self.assertFalse(self.ev._mark_consumption_unknown(got['receipt_id'], 'attempts_exhausted'))
        with patch('tongle.worker.invoke_json') as call:
            result = self.ev._independent_consumption(self.cfg)
            call.assert_not_called()
        self.assertEqual(result['called'], 0)
        self.assertEqual(self.e.db.execute(
            'SELECT state FROM knowledge_consumption WHERE id=?', (got['receipt_id'],)).fetchone()['state'],
            'awaiting_check')

    def test_direct_verifier_does_not_race_live_lease(self):
        card, got = self.offer()
        self.capture('完成方案的输出。', 'answer', 'assistant', 's2')
        offered = self.ev._feedback_inputs({'project': 'project', 'session': 's2'})
        with self.e.transaction():
            self.e.db.execute(
                "INSERT INTO knowledge_consumption_lease(receipt_id,attempts,lease_token,"
                "lease_until,updated_at) VALUES(?,?,?,?,?)",
                (got['receipt_id'], 1, 'live', time.time() + 600, time.time()))
        self.ev._save_checks(offered, [{'receipt_id': got['receipt_id'],
            'evidence': [{'card_id': card['id'], 'verdict': 'applied',
                          'quote': '完成方案', 'reason': 'x'}]}])
        row = self.e.db.execute('SELECT state FROM knowledge_consumption WHERE id=?',
                                (got['receipt_id'],)).fetchone()
        self.assertEqual(row['state'], 'awaiting_check')
        self.assertEqual(self.e.db.execute('SELECT COUNT(*) FROM knowledge_feedback').fetchone()[0], 0)

    def test_merge_preserves_prior_card_evidence(self):
        answer = self.capture('完成方案的输出。', 'answer', 'assistant', 's4')
        a, b = 'a' * 32, 'b' * 32
        offered_json = json.dumps([
            {'id': a, 'version': 1, 'title': 'A', 'body': '', 'delivery': 'full'},
            {'id': b, 'version': 1, 'title': 'B', 'body': '', 'delivery': 'full'}])
        with self.e.transaction():
            self.e.db.execute(
                "INSERT INTO knowledge_consumption(id,project,session,query,offered,"
                "output_source,state,created_at) VALUES('r1','project','s4','q',?,?,"
                "'awaiting_check',?)", (offered_json, answer['source_id'], time.time()))
        item = {'receipt_id': 'r1', 'offered': json.loads(offered_json),
                'output_source': answer['source_id'], 'output': '完成方案的输出。'}
        self.ev._save_checks([item], [{'receipt_id': 'r1', 'evidence': [
            {'card_id': a, 'verdict': 'applied', 'quote': '完成方案', 'reason': 'x'}]}])
        row = self.e.db.execute("SELECT state,checks FROM knowledge_consumption WHERE id='r1'").fetchone()
        self.assertEqual(row['state'], 'awaiting_check')
        self.assertEqual([e['card_id'] for e in json.loads(row['checks'])['evidence']], [a])
        self.ev._save_checks([item], [{'receipt_id': 'r1', 'evidence': [
            {'card_id': b, 'verdict': 'not_used', 'quote': '', 'reason': 'y'}]}])
        row = self.e.db.execute("SELECT state,checks FROM knowledge_consumption WHERE id='r1'").fetchone()
        self.assertEqual(row['state'], 'checked')
        self.assertEqual({e['card_id'] for e in json.loads(row['checks'])['evidence']}, {a, b})

    def test_mixed_locator_receipt_is_unknown_with_evidence(self):
        answer = self.capture('完成方案的输出。', 'answer', 'assistant', 's4')
        a, b = 'a' * 32, 'b' * 32
        offered_json = json.dumps([
            {'id': a, 'version': 1, 'title': 'A', 'body': '', 'delivery': 'full'},
            {'id': b, 'version': 1, 'title': 'B', 'body': '', 'delivery': 'locator'}])
        with self.e.transaction():
            self.e.db.execute(
                "INSERT INTO knowledge_consumption(id,project,session,query,offered,"
                "output_source,state,created_at) VALUES('r2','project','s4','q',?,?,"
                "'awaiting_check',?)", (offered_json, answer['source_id'], time.time()))
        item = {'receipt_id': 'r2', 'offered': json.loads(offered_json),
                'output_source': answer['source_id'], 'output': '完成方案的输出。'}
        self.ev._save_checks([item], [{'receipt_id': 'r2', 'evidence': [
            {'card_id': a, 'verdict': 'applied', 'quote': '完成方案', 'reason': 'x'}]}])
        row = self.e.db.execute("SELECT state,checks FROM knowledge_consumption WHERE id='r2'").fetchone()
        self.assertEqual(row['state'], 'unknown')
        checks = json.loads(row['checks'])
        self.assertEqual(checks['reason'], 'locator_only_remaining')
        self.assertEqual([e['card_id'] for e in checks['evidence']], [a])

    def test_empty_offered_receipt_is_unknown_not_checked(self):
        answer = self.capture('完成方案的输出。', 'answer', 'assistant', 's4')
        rid = 'e' * 32
        with self.e.transaction():
            self.e.db.execute(
                "INSERT INTO knowledge_consumption(id,project,session,query,offered,"
                "output_source,state,created_at) VALUES(?,'project','s4','q','[]',?,"
                "'awaiting_check',?)", (rid, answer['source_id'], time.time()))
        item = {'receipt_id': rid, 'offered': [],
                'output_source': answer['source_id'], 'output': '完成方案的输出。'}
        self.ev._save_checks([item], [{'receipt_id': rid, 'evidence': []}])
        row = self.e.db.execute(
            'SELECT state,checks FROM knowledge_consumption WHERE id=?', (rid,)).fetchone()
        self.assertEqual(row['state'], 'unknown')
        checks = json.loads(row['checks'])
        self.assertEqual(checks['reason'], 'no_offered_evidence')
        self.assertEqual(checks['evidence'], [])

    def test_migration_of_empty_evidence_checked_to_unknown(self):
        with self.e.transaction():
            self.e.db.execute(
                "INSERT INTO knowledge_consumption(id,project,session,query,offered,state,created_at)"
                " VALUES(?,?,?,?,?, 'checked', ?)", ('9' * 32, 'project', 's', 'q', '[]', time.time()))
        result = self.ev.migrate_consumption_truth()
        self.assertEqual(result['migrated'], 1)
        row = self.e.db.execute('SELECT state,checks FROM knowledge_consumption WHERE id=?',
                                ('9' * 32,)).fetchone()
        self.assertEqual(row['state'], 'unknown')
        checks = json.loads(row['checks'])
        self.assertEqual(checks['reason'], 'migrated_empty_evidence_to_unknown')
        self.assertEqual(checks['evidence'], [])
        self.assertTrue(self.ev.migrate_consumption_truth()['already_done'])


class DailySessionTests(unittest.TestCase):
    def test_context_session_is_forwarded_to_core(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            argsfile = tmp / 'args.txt'
            core = tmp / 'core.py'
            core.write_text('#!' + sys.executable + '\nimport sys\n'
                            'open(' + repr(str(argsfile)) + ',"w").write("\\n".join(sys.argv[1:]))\n')
            core.chmod(0o755)
            (tmp / 'root').mkdir()
            active = tmp / 'active.json'
            active.write_text(json.dumps({'enabled': True, 'home': str(tmp / 'home'),
                'root': str(tmp / 'root'), 'python': str(core), 'project': 'p',
                'vault': str(tmp / 'vault'), 'projects': {str(tmp): 'p'}}))
            env = dict(os.environ, TONGLE_NEXT_INSTALL=str(active))
            run = subprocess.run([sys.executable, str(ROOT / 'scripts/daily.py'),
                                  '--cwd', str(tmp), '--session', 'sess42',
                                  '--text', 'hi', 'context'],
                                 env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(run.returncode, 0, run.stderr)
            args = argsfile.read_text().splitlines()
        self.assertIn('--session', args)
        self.assertEqual(args[args.index('--session') + 1], 'sess42')


if __name__ == '__main__':
    unittest.main()
