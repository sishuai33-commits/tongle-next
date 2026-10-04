"""Focused synthetic regressions for the always-on frozen comparison contract.

These exercise the real ``Evolution._compare_stage`` / ``_verify_compare`` /
``process`` paths with synthetic fixtures only: no live vault, model,
credentials or network is touched. A model stub proves machine behaviour, not
semantic quality.
"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tongle.engine import Engine
from tongle.evolution import Evolution, SelectionChanged, StageLimit
from tongle.comparison_review import (COMPARE_PROTOCOL, MAX_COMPARE_REPORT_CHARS,
                                      _bounded_report, validate_compare_response)
from tongle.evidence_selection import related_pairs

try:  # sibling import under ``unittest discover -s tests``
    from fact_review_stub import with_fact_audit
except ImportError:  # pragma: no cover - package-import invocation
    from tests.fact_review_stub import with_fact_audit


class _GroupedHarness(unittest.TestCase):
    MAIN_TEXT = '触发一次局部维护。'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.e = Engine(self.root / 'state')
        self.e.init(self.root / 'vault'); self.addCleanup(self.e.store.close)
        self.write_config()
        self.ev = Evolution(self.e)
        self._call_seq = [0]

    def write_config(self, **overrides):
        cfg = {'knowledge_mode': 'evolving', 'model_command': ['stub'],
               'knowledge_quiet_seconds': 0,
               # Synthetic-only, generous finite daily allowance so the bounded
               # comparison is limited by the attempt/dispatch ceilings, never
               # by an accidental token wall. Production caps are untouched.
               'knowledge_daily_token_budget': 100000000,
               'knowledge_daily_call_budget': 100000}
        cfg.update(overrides)
        (self.e.home / 'config.json').write_text(json.dumps(cfg))

    def seed(self, key, body, **kw):
        return self.ev.cards.seed('project', key, 'memory', kw.pop('topic', '主题'),
                                  key, body, **kw)

    def capture(self, text=None, event='e', role='user', session='session'):
        rec = self.e.store.capture('project', session, role, event,
                                   text or self.MAIN_TEXT)
        self.ev.captured(rec['source_id'], role, session)
        return rec

    def run_model(self, model, force=True):
        with patch('tongle.worker.invoke_json', side_effect=with_fact_audit(model)):
            return self.ev.process(force=force)

    def run_attempts(self, model, times=3):
        result = None
        for _ in range(times):
            result = self.run_model(model)
            with self.e.transaction():
                self.e.db.execute('UPDATE knowledge_work SET next_at=0')
        return result

    def work(self, state=None):
        sql = 'SELECT * FROM knowledge_work'; args = ()
        if state:
            sql += ' WHERE state=?'; args = (state,)
        return [dict(r) for r in self.e.db.execute(sql, args).fetchall()]

    def _source(self, rec):
        return self.e.store.source(rec['source_id'])

    def _create_op(self, key, title, body, source):
        return {'op': 'create', 'key': key, 'kind': 'memory', 'topic': '主题',
                'title': title, 'body': body,
                'evidence': [{'source_id': source['source_id'],
                              'version': source['version'], 'quote': source['text']}],
                'reason': '新增材料'}

    def _distinct(self, data):
        cand = {c['card_id']: c for c in data['page']['candidates']}
        out = []
        for p in data['page']['pairs']:
            body = (cand.get(p['card_id'], {}).get('body') or '') or '正文'
            out.append({'op': p['op'], 'card_id': p['card_id'], 'relation': 'distinct',
                        'quote': body[:20],
                        'reason': 'scope and claim differ from the proposed change here'})
        return {'pairs': out}

    def _record_call(self, job, stage='compare'):
        """Synthetic stand-in for the real ``_call`` dispatch-identity row.

        Settled with an explicit ``elapsed=0`` so it matches the production
        ``_call`` contract, where ``elapsed IS NOT NULL`` marks a finished call
        whose receipt may be reused. An unsettled row (``elapsed`` NULL) must
        never be reusable.
        """
        self._call_seq[0] += 1
        call_id = 'call%04d' % self._call_seq[0]
        with self.e.transaction():
            self.e.db.execute(
                'INSERT INTO knowledge_calls(id,work_id,stage,day,reserved,elapsed,created_at,error)'
                ' VALUES(?,?,?,?,?,0,?,NULL)',
                (call_id, job['id'], stage, '2026-01-01', 1, time.time()))
        self.ev._last_call_id = call_id
        return call_id

    def _unit_context(self, text=None, wid='w'):
        path = self.root / (wid + '.md')
        path.write_text(text or self.MAIN_TEXT)
        info = self.e.store.ingest('project', str(path))
        source = self.e.store.source(info['source_id'])
        part = list(self.ev._source_parts(source))[0]
        self._unit_path = path
        related = self.ev._related('project', [part], None)
        guard = related.pop('guard', {})
        related.pop('related_hits', None)
        authorized = self.ev._authorized_catalog('project')
        catalog_cards = [{'id': c['id'], 'version': c['version'], 'project': c['project'],
                          'state': c['state'], 'basis_cards': c.get('basis_cards') or []}
                         for c in authorized]
        base = {'project': 'project', 'sources': [part], 'existing': related,
                'source_snapshot': {part['source_id']: part['version']},
                'catalog_cards': catalog_cards, 'guard': guard,
                'scope_fingerprint': self.ev._scope_fingerprint('project'),
                'stage_calls': {'compile': 0, 'verify': 0},
                'read_sets': {'compile': {}, 'verify': {}},
                'read_feedback': {'compile': [], 'verify': []}}
        with self.e.transaction():
            self.e.db.execute(
                'INSERT OR REPLACE INTO knowledge_work(id,project,session,inputs,state,'
                'lease_token,lease_until,created_at) VALUES(?,?,?,?,?,?,?,?)',
                (wid, 'project', 'session', json.dumps([part]), 'running', 'tok',
                 time.time() + 600, time.time()))
        job = dict(self.e.db.execute(
            'SELECT * FROM knowledge_work WHERE id=?', (wid,)).fetchone())
        return base, job, part

    def _unit_op(self):
        return {'op': 'create', 'key': '新键', 'kind': 'memory', 'topic': '主题',
                'title': '新题', 'body': '共同主题的新说明。',
                'evidence': [{'source_id': 'f' * 32, 'version': 1, 'quote': 'x'}],
                'reason': '新增'}

    def _base_with_receipts(self, count=8):
        for i in range(count):
            self.seed('卡%d' % i, '共同主题' + '据' * 7996)
        base, job, _part = self._unit_context()
        op = self._unit_op()
        calls = {'n': 0}

        def call(_job, stage, payload, _cfg):
            calls['n'] += 1
            self._record_call(_job, stage)
            return self._distinct(payload)
        with patch.object(self.ev, '_call', side_effect=call):
            reports, findings = self.ev._compare_stage(job, base, [op], 'tok', {})
        self.assertEqual(findings, [])
        self.assertTrue(reports)
        return base, job, op, calls


class SmallSetAlwaysCompareTests(_GroupedHarness):
    def test_small_related_set_still_uses_frozen_comparison(self):
        self.seed('共同卡', '共同主题的既有说明。')
        rec = self.capture()
        source = self._source(rec)
        seen = {'compare': 0}

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'frozen_proposal' in data:
                seen['compare'] += 1
                return self._distinct(data), {}
            if 'proposal' in data:
                return {'approved': True, 'findings': []}, {}
            return {'operations': [self._create_op('新键', '新题', '共同主题的新说明。', source)]}, {}

        result = self.run_model(model)
        self.assertEqual(result['processed'], 1)
        self.assertGreaterEqual(seen['compare'], 1,
                                'small sets use the frozen comparison too')


class SharedCandidateTests(_GroupedHarness):
    def test_one_shared_candidate_is_not_duplicated_per_body(self):
        self.seed('共同卡', '共同令牌的旧说明。')
        rec = self.capture()
        source = self._source(rec)
        pages = []

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'frozen_proposal' in data:
                pages.append(data)
                return self._distinct(data), {}
            if 'proposal' in data:
                return {'approved': True, 'findings': []}, {}
            return {'operations': [
                self._create_op('键%d' % i, '题%d' % i, '共同令牌的说明%d。' % i, source)
                for i in range(8)]}, {}

        result = self.run_model(model)
        self.assertEqual(result['processed'], 1)
        self.assertEqual(len(pages), 1)
        # One whole candidate body, with all eight actual matched op pairs.
        self.assertEqual(len(pages[0]['page']['candidates']), 1)
        self.assertEqual(len(pages[0]['page']['pairs']), 8)
        ops = sorted(p['op'] for p in pages[0]['page']['pairs'])
        self.assertEqual(ops, list(range(8)))


class RelationGroundingTests(_GroupedHarness):
    def _page(self):
        page = {'pairs': [[0, 'a' * 32]], 'cards': ['a' * 32]}
        bodies = {'a' * 32: '旧卡甲的完整正文。'}
        return page, bodies

    def test_every_relation_requires_exact_quote_and_substantive_reason(self):
        page, bodies = self._page()
        for relation in ('distinct', 'duplicate', 'extends', 'conflict', 'uncertain'):
            missing = validate_compare_response(
                {'pairs': [{'op': 0, 'card_id': 'a' * 32, 'relation': relation,
                            'reason': 'scope and claim clearly differ here'}]}, page, bodies)
            self.assertFalse(missing['ok'], relation)
            fabricated = validate_compare_response(
                {'pairs': [{'op': 0, 'card_id': 'a' * 32, 'relation': relation,
                            'quote': '不是正文里的引文', 'reason': 'scope and claim clearly differ here'}]},
                page, bodies)
            self.assertFalse(fabricated['ok'], relation)
            short = validate_compare_response(
                {'pairs': [{'op': 0, 'card_id': 'a' * 32, 'relation': relation,
                            'quote': '旧卡甲的完整正文。', 'reason': 'differs'}]}, page, bodies)
            self.assertFalse(short['ok'], relation)
            ok = validate_compare_response(
                {'pairs': [{'op': 0, 'card_id': 'a' * 32, 'relation': relation,
                            'quote': '旧卡甲的完整正文。',
                            'reason': 'scope and claim clearly differ here'}]}, page, bodies)
            self.assertTrue(ok['ok'], relation)
            self.assertEqual(ok['dispositions'][0]['quote'], '旧卡甲的完整正文。')
            self.assertEqual(bool(ok['findings']), relation != 'distinct')

    def test_bad_field_types_and_oversize_are_rejected_not_truncated(self):
        page, bodies = self._page()
        cases = [
            {'op': 0, 'card_id': 'a' * 32, 'relation': 'distinct',
             'quote': 123, 'reason': 'scope and claim clearly differ here'},
            {'op': 0, 'card_id': 'a' * 32, 'relation': 'distinct',
             'quote': '旧卡甲的完整正文。', 'reason': 123},
            {'op': 0, 'card_id': 'a' * 32, 'relation': 'distinct',
             'quote': '旧卡甲的完整正文。', 'reason': 'x' * 400},
            {'op': 0, 'card_id': 'a' * 32, 'relation': 'distinct',
             'quote': 'y' * 200, 'reason': 'scope and claim clearly differ here'},
        ]
        for case in cases:
            self.assertFalse(validate_compare_response({'pairs': [case]}, page, bodies)['ok'], case)


class ReportCoverageTests(_GroupedHarness):
    def test_legal_maximum_64_pairs_all_reported(self):
        items = [{'op': i % 8, 'card_id': '%032d' % i, 'relation': 'distinct',
                  'quote': '引' * 120, 'reason': 'r' * 160} for i in range(64)]
        report = _bounded_report(items)
        self.assertEqual(report['pair_count'], 64)
        self.assertEqual(len(report['pairs']), 64)
        self.assertFalse(report['truncated'])
        self.assertEqual(report['omitted_pairs'], 0)
        self.assertGreater(MAX_COMPARE_REPORT_CHARS, 0)

    def test_end_to_end_all_pairs_delivered(self):
        # Eight distinct neutral ASCII subject tokens, each with eight unique
        # whole-card bodies. Every create names only its own subject, so each
        # create matches exactly its own eight cards: no generic shared body
        # term can fill the top-eight related-pair ties and collapse the plan.
        subjects = ['alpha', 'bravo', 'charlie', 'delta',
                    'echo', 'foxtrot', 'golf', 'hotel']
        for subject in subjects:
            for j in range(8):
                self.seed('%scard%d' % (subject, j), '%s body%d' % (subject, j))
        rec = self.capture()
        source = self._source(rec)
        seen = set()
        bodies = set()
        verify_payloads = []

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'frozen_proposal' in data:
                cand = {c['card_id']: c for c in data['page']['candidates']}
                for p in data['page']['pairs']:
                    body = cand.get(p['card_id'], {}).get('body')
                    seen.add((p['op'], body))
                    bodies.add(body)
                return self._distinct(data), {}
            if 'proposal' in data:
                verify_payloads.append(data)
                return {'approved': True, 'findings': []}, {}
            return {'operations': [
                self._create_op('%snew' % subject, '%snew' % subject,
                                '%s freshnote' % subject, source)
                for subject in subjects]}, {}

        result = self.run_model(model)
        self.assertEqual(result['processed'], 1)
        # All 64 op/card pairs were compared AND all 64 distinct whole-card
        # bodies were actually read (never a shrunken 8-card tie).
        self.assertEqual(len(seen), 64)
        self.assertEqual(len(bodies), 64)
        rows = self.e.db.execute(
            "SELECT error FROM knowledge_failures WHERE state='completion'").fetchall()
        record = json.loads(rows[0]['error'])
        self.assertEqual(record['comparison']['pair_count'], 64)
        self.assertEqual(record['comparison']['page_count'], 16)
        self.assertTrue(record['comparison']['all_distinct'])
        # Every page's grounded report reaches the final review whole.
        self.assertTrue(verify_payloads)
        reports = verify_payloads[-1].get('comparison_reports') or []
        self.assertEqual(len(reports), 16)
        self.assertEqual(sum(r['pair_count'] for r in reports), 64)
        self.assertEqual(sum(r['omitted_pairs'] for r in reports), 0)
        self.assertTrue(all(r['pairs'] for r in reports))


class ReceiptIntegrityTests(_GroupedHarness):
    def test_matrix_plan_covers_every_pair(self):
        base, job, op, calls = self._base_with_receipts()
        pairs = related_pairs(self.ev._authorized_catalog('project'), [op])
        planned = [tuple(p) for r in base['compare']['pages'].values()
                   for p in r.get('pairs') or []]
        self.assertEqual(sorted((p['op'], p['card_id']) for p in pairs),
                         sorted(planned))

    def test_erased_cache_receipt_cannot_publish_or_skip(self):
        base, job, op, calls = self._base_with_receipts()
        state = base['compare']
        pid = sorted(state['pages'])[0]
        entry = state['pages'][pid]
        entry['dispositions'] = []
        entry.pop('findings', None)
        entry.pop('report', None)
        with self.assertRaises(SelectionChanged):
            self.ev._verify_compare(base, [op])
        # Resume must re-dispatch the erased page, never skip it.
        before = calls['n']
        with patch.object(self.ev, '_call',
                          side_effect=lambda _j, s, p, _c: (self._record_call(_j, s),
                                                            self._distinct(p))[1]):
            reports, findings = self.ev._compare_stage(job, base, [op], 'tok', {})
        self.assertGreaterEqual(calls['n'], before)
        self.assertEqual(findings, [])

    def test_tampered_quote_cannot_publish(self):
        base, job, op, calls = self._base_with_receipts()
        state = base['compare']
        pid = sorted(state['pages'])[0]
        state['pages'][pid]['dispositions'][0]['quote'] = '伪造的引文'
        with self.assertRaises(SelectionChanged):
            self.ev._verify_compare(base, [op])

    def test_erased_findings_cannot_hide_a_blocking_verdict(self):
        base, job, op, calls = self._base_with_receipts()
        state = base['compare']
        pid = sorted(state['pages'])[0]
        receipt = state['pages'][pid]
        # Forge a blocking relation but erase the findings field; the replay
        # must recompute it from the dispositions.
        body = receipt['report']['pairs'][0]['quote']
        receipt['dispositions'][0]['relation'] = 'duplicate'
        receipt['dispositions'][0]['quote'] = body
        receipt['findings'] = []
        with self.assertRaises(StageLimit):
            self.ev._verify_compare(base, [op])

    def test_old_protocol_receipt_is_invalid(self):
        base, job, op, calls = self._base_with_receipts()
        for entry in base['compare']['pages'].values():
            entry['protocol'] = 'tongle-compare-v1'
        with self.assertRaises(SelectionChanged):
            self.ev._verify_compare(base, [op])

    def test_receipt_call_identity_is_required(self):
        base, job, op, calls = self._base_with_receipts()
        for entry in base['compare']['pages'].values():
            entry['call_id'] = 'no-such-call'
        with self.assertRaises(SelectionChanged):
            self.ev._verify_compare(base, [op])

    def test_foreign_work_call_identity_is_required(self):
        base, job, op, calls = self._base_with_receipts()
        with self.e.transaction():
            self.e.db.execute(
                'INSERT INTO knowledge_calls(id,work_id,stage,day,reserved,elapsed,created_at,error)'
                " VALUES('other-call','other-work','compare','2026-01-01',1,0,?,NULL)",
                (time.time(),))
        for entry in base['compare']['pages'].values():
            entry['call_id'] = 'other-call'
        with self.assertRaises(SelectionChanged):
            self.ev._verify_compare(base, [op])

    def test_unsettled_call_row_is_not_reusable(self):
        # A call row left with a NULL ``elapsed`` (a crash before ``_call``
        # settled it) is a real same-work/same-stage/errorless row, yet it must
        # never back a reusable receipt.
        base, job, op, calls = self._base_with_receipts()
        call_id = next(iter(base['compare']['pages'].values()))['call_id']
        with self.e.transaction():
            self.e.db.execute(
                'UPDATE knowledge_calls SET elapsed=NULL WHERE id=?', (call_id,))
        with self.assertRaises(SelectionChanged):
            self.ev._verify_compare(base, [op])

    def test_restart_roundtrip_reuses_receipts_and_publishes(self):
        base, job, op, calls = self._base_with_receipts()
        self.ev._verify_compare(base, [op])
        restored = json.loads(json.dumps(base))
        self.ev._verify_compare(restored, [op])
        before = calls['n']
        with patch.object(self.ev, '_call',
                          side_effect=lambda *a, **k: (_ for _ in ()).throw(
                              AssertionError('re-dispatched a reusable page'))):
            reports, findings = self.ev._compare_stage(job, restored, [op], 'tok', {})
        self.assertEqual(findings, [])
        self.assertEqual(calls['n'], before)

    def test_unrelated_change_still_reuses(self):
        base, job, op, calls = self._base_with_receipts()
        self.ev._verify_compare(base, [op])
        self.seed('完全无关卡', '空气温度改变。')
        self.ev._verify_compare(base, [op])

    def test_lost_lease_stops_comparison(self):
        for i in range(8):
            self.seed('卡%d' % i, '共同主题' + '据' * 7996)
        base, job, _part = self._unit_context()
        op = self._unit_op()
        with self.assertRaises(ValueError):
            self.ev._compare_stage(job, base, [op], 'wrong-token', {})

    def test_dispatch_counter_persists_across_bundle_rebuild(self):
        for i in range(8):
            self.seed('卡%d' % i, '共同主题' + '据' * 7996)
        base, job, _part = self._unit_context()
        op = self._unit_op()
        with self.e.transaction():
            self.e.db.execute('UPDATE knowledge_work SET compare_dispatches=5 WHERE id=?',
                              (job['id'],))
        with patch.object(self.ev, '_call',
                          side_effect=lambda _j, s, p, _c: (self._record_call(_j, s),
                                                            self._distinct(p))[1]):
            self.ev._compare_stage(job, base, [op], 'tok', {})
        row = self.e.db.execute('SELECT compare_dispatches FROM knowledge_work WHERE id=?',
                                (job['id'],)).fetchone()
        self.assertGreater(row['compare_dispatches'], 5)


class CurrencyAndAuthorizationTests(_GroupedHarness):
    def _seed_overflow(self):
        for i in range(8):
            self.seed('卡%d' % i, '共同主题' + '据' * 7996)

    def test_scope_change_between_pages_blocks(self):
        self._seed_overflow()
        base, job, _part = self._unit_context()
        op = self._unit_op()
        calls = {'n': 0}

        def call(_job, stage, payload, _cfg):
            calls['n'] += 1
            self._record_call(_job, stage)
            if calls['n'] == 1:
                self.write_config(library_excluded_projects=['project'])
            return self._distinct(payload)
        with patch.object(self.ev, '_call', side_effect=call):
            with self.assertRaises(SelectionChanged):
                self.ev._compare_stage(job, base, [op], 'tok', {})

    def test_source_change_between_pages_blocks(self):
        self._seed_overflow()
        base, job, _part = self._unit_context()
        op = self._unit_op()
        calls = {'n': 0}

        def call(_job, stage, payload, _cfg):
            calls['n'] += 1
            self._record_call(_job, stage)
            if calls['n'] == 1:
                self._unit_path.write_text('来源的新版本内容。')
                self.e.store.ingest('project', str(self._unit_path))
            return self._distinct(payload)
        with patch.object(self.ev, '_call', side_effect=call):
            with self.assertRaises(ValueError):
                self.ev._compare_stage(job, base, [op], 'tok', {})

    def test_card_change_between_pages_blocks(self):
        self._seed_overflow()
        base, job, _part = self._unit_context()
        op = self._unit_op()
        calls = {'n': 0}

        def call(_job, stage, payload, _cfg):
            calls['n'] += 1
            self._record_call(_job, stage)
            if calls['n'] == 1:
                for card in self.ev.cards.list('project', include_inactive=True):
                    self.ev.cards.manual_edit(card['id'], card['version'],
                                              '页面之间的改动正文。')
            return self._distinct(payload)
        with patch.object(self.ev, '_call', side_effect=call):
            with self.assertRaises(SelectionChanged):
                self.ev._compare_stage(job, base, [op], 'tok', {})


class CollisionRepairTests(_GroupedHarness):
    def test_exact_collision_first_error_then_valid_correction(self):
        self.ev.cards.seed('project', '锁键', 'memory', '方案', '锁题', '既有正文。')
        rec = self.capture()
        source = self._source(rec)
        hits = {'n': 0}

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'frozen_proposal' in data:
                return self._distinct(data), {}
            if 'proposal' in data:
                return {'approved': True, 'findings': []}, {}
            hits['n'] += 1
            if hits['n'] == 1:
                return {'operations': [self._create_op('锁键', '锁题', '既有正文。', source)]}, {}
            return {'operations': [self._create_op('新锁键', '新锁题', '修正后的新正文。', source)]}, {}

        result = self.run_model(model)
        self.assertEqual(result['processed'], 1)
        keys = [c['key'] for c in self.ev.cards.list('project')]
        self.assertIn('锁键', keys)
        self.assertIn('新锁键', keys)

    def test_repeated_exact_collision_stops_at_three_attempts(self):
        self.ev.cards.seed('project', '锁键', 'memory', '方案', '锁题', '既有正文。')
        rec = self.capture()
        source = self._source(rec)

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'frozen_proposal' in data:
                return self._distinct(data), {}
            if 'proposal' in data:
                return {'approved': True, 'findings': []}, {}
            return {'operations': [self._create_op('锁键', '锁题', '既有正文。', source)]}, {}

        result = self.run_attempts(model)
        self.assertEqual(result['failed'], 1)
        row = self.work('failed')[0]
        self.assertIn('create duplicates existing card', row['error'])
        self.assertEqual(row['attempts'], 3)
        self.assertEqual([c['key'] for c in self.ev.cards.list('project')], ['锁键'])

    def test_within_batch_collision_first_error_then_valid_correction(self):
        rec = self.capture()
        source = self._source(rec)
        hits = {'n': 0}

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'frozen_proposal' in data:
                return self._distinct(data), {}
            if 'proposal' in data:
                return {'approved': True, 'findings': []}, {}
            hits['n'] += 1
            if hits['n'] == 1:
                return {'operations': [self._create_op('同键', '同题', '正文一。', source),
                                       self._create_op('同键', '同题', '正文二。', source)]}, {}
            return {'operations': [self._create_op('键甲', '题甲', '正文甲。', source),
                                   self._create_op('键乙', '题乙', '正文乙。', source)]}, {}

        result = self.run_model(model)
        self.assertEqual(result['processed'], 0)
        with self.e.transaction():
            self.e.db.execute('UPDATE knowledge_work SET next_at=0')
        result = self.run_model(model)
        self.assertEqual(result['processed'], 1)
        # The two keys are an unordered set: their CJK code-point order is
        # irrelevant to the collision-repair contract.
        keys = {c['key'] for c in self.ev.cards.list('project')}
        self.assertEqual(keys, {'键甲', '键乙'})

    def test_repeated_within_batch_collision_stops_at_three_attempts(self):
        rec = self.capture()
        source = self._source(rec)

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'frozen_proposal' in data:
                return self._distinct(data), {}
            if 'proposal' in data:
                return {'approved': True, 'findings': []}, {}
            return {'operations': [self._create_op('同键', '同题', '正文一。', source),
                                   self._create_op('同键', '同题', '正文二。', source)]}, {}

        result = self.run_attempts(model)
        self.assertEqual(result['failed'], 1)
        self.assertIn('within batch', self.work('failed')[0]['error'])
        self.assertEqual(self.ev.cards.list('project'), [])


class VerifyTaskCoherenceTests(_GroupedHarness):
    def test_base_verify_task_has_no_request_all_conflict(self):
        from tongle.evolution import VERIFY_TASK
        self.assertNotIn('Also request the likely-duplicate candidates', VERIFY_TASK)
        self.assertIn('When no comparison_reports are supplied', VERIFY_TASK)

    def test_delegated_verify_payload_carries_reports_and_grounded_note(self):
        self.seed('共同卡', '共同主题的既有说明。')
        rec = self.capture()
        source = self._source(rec)
        verify_payloads = []

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'frozen_proposal' in data:
                return self._distinct(data), {}
            if 'proposal' in data:
                verify_payloads.append(data)
                return {'approved': True, 'findings': []}, {}
            return {'operations': [self._create_op('新键', '新题', '共同主题的新说明。', source)]}, {}

        result = self.run_model(model)
        self.assertEqual(result['processed'], 1)
        self.assertTrue(verify_payloads)
        self.assertIn('comparison_reports', verify_payloads[-1])
        task = verify_payloads[-1].get('task', '')
        self.assertIn('already compared independently', task)
        self.assertIn('comparison_reports', task)
        # The appended note never claims the main reviewer itself read every
        # comparison body; it hands over grounded reports instead.
        self.assertNotIn('You read all comparison', task)


class NoCompareWhenUnnecessaryTests(_GroupedHarness):
    def test_noop_spends_no_comparison_call(self):
        self.seed('既有', '共同主题的既有说明。')
        self.capture()
        stages = {'compare': 0}

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'frozen_proposal' in data:
                stages['compare'] += 1
                return self._distinct(data), {}
            if 'proposal' in data:
                return {'approved': True, 'findings': []}, {}
            return {'operations': []}, {}

        result = self.run_model(model)
        self.assertEqual(result['processed'], 1)
        self.assertEqual(stages['compare'], 0)

    def test_update_only_spends_no_comparison_call(self):
        target = self.seed('更新目标', '共同主题的旧正文。')
        rec = self.capture()
        source = self._source(rec)
        op = {'op': 'update', 'id': target['id'], 'expected_version': target['version'],
              'body': '共同主题的新正文。',
              'evidence': [{'source_id': source['source_id'], 'version': source['version'],
                            'quote': source['text']}], 'reason': '更新'}
        stages = {'compare': 0}

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'frozen_proposal' in data:
                stages['compare'] += 1
                return self._distinct(data), {}
            if 'proposal' in data:
                return {'approved': True, 'findings': []}, {}
            return {'operations': [op]}, {}

        result = self.run_model(model)
        self.assertEqual(result['processed'], 1)
        self.assertEqual(stages['compare'], 0)


class ContractFingerprintTests(_GroupedHarness):
    def test_compare_contract_is_in_fingerprint(self):
        base = self.ev._contract_fingerprint()
        with patch('tongle.evolution.COMPARE_PROTOCOL', 'tongle-compare-x'):
            self.assertNotEqual(self.ev._contract_fingerprint(), base)
        with patch('tongle.evolution.COMPARE_TASK', 'X'):
            self.assertNotEqual(self.ev._contract_fingerprint(), base)
        with patch('tongle.evolution.VERIFY_TASK', 'X'):
            self.assertNotEqual(self.ev._contract_fingerprint(), base)

    def test_protocol_versions_bumped(self):
        from tongle.evolution import PROTOCOL_VERSION
        self.assertEqual(COMPARE_PROTOCOL, 'tongle-compare-v2')
        self.assertEqual(PROTOCOL_VERSION, 'tongle-knowledge-contract-v5')


if __name__ == '__main__':
    unittest.main()
