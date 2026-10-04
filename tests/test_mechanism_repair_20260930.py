"""Counterexamples for the 2026-09-30 bounded-catalog / succession repair.

These exercise the real ``Evolution.process`` / ``_stage_loop`` / source-ledger
paths with synthetic fixtures only. No live vault, model, credentials or
network is touched. Where a model fixture is used it proves machine behavior,
not semantic quality.
"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tongle.engine import Engine
from tongle.evolution import Evolution, SelectionChanged
from tongle.evidence_selection import (basis_closure_report, duplicate_report,
                                       lexical_hits, mandatory_ids, search_page,
                                       validate_search_request)
from tongle.comparison_review import (MAX_COMPARE_CANDIDATES_PER_PAGE,
                                      ComparePlanError, plan_compare_pages,
                                      validate_compare_response)
from tongle.knowledge_files import KnowledgeFiles
from tongle.views import status_snapshot, refresh

try:  # sibling import under ``unittest discover -s tests``
    from fact_review_stub import with_fact_audit
except ImportError:  # pragma: no cover - package-import invocation
    from tests.fact_review_stub import with_fact_audit


def _alias(existing, key):
    for row in list(existing.get('cards') or []) + list(existing.get('catalog') or []):
        if row.get('key') == key and isinstance(row.get('id'), str):
            return row['id']
    raise AssertionError('no alias for key ' + repr(key))


class _Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.e = Engine(self.root / 'state')
        self.e.init(self.root / 'vault'); self.addCleanup(self.e.store.close)
        (self.e.home / 'config.json').write_text(json.dumps(
            {'knowledge_mode': 'evolving', 'model_command': ['stub'],
             'knowledge_quiet_seconds': 0}))
        self.ev = Evolution(self.e)

    def capture(self, text, event='one', role='user', session='session'):
        rec = self.e.store.capture('project', session, role, event, text)
        self.ev.captured(rec['source_id'], role, session)
        return rec

    def write_config(self, **overrides):
        cfg = {'knowledge_mode': 'evolving', 'model_command': ['stub'],
               'knowledge_quiet_seconds': 0}
        cfg.update(overrides)
        (self.e.home / 'config.json').write_text(json.dumps(cfg))

    def work(self, state=None, order='created_at'):
        sql = 'SELECT * FROM knowledge_work'
        args = ()
        if state:
            sql += ' WHERE state=?'
            args = (state,)
        sql += ' ORDER BY ' + order
        return [dict(r) for r in self.e.db.execute(sql, args).fetchall()]

    def run_model(self, model, force=True):
        with patch('tongle.worker.invoke_json', side_effect=with_fact_audit(model)):
            return self.ev.process(force=force)

    def run_attempts(self, model, times=3):
        """Drive the real process through the bounded attempt ceiling."""
        result = None
        for _ in range(times):
            result = self.run_model(model)
            with self.e.transaction():
                self.e.db.execute('UPDATE knowledge_work SET next_at=0')
        return result


class BoundedCatalogTests(_Harness):
    def test_large_directory_local_update_is_not_blocked(self):
        # Enough long rows that the old *complete* catalog serialization would
        # exceed the fixed 100000-byte bound and fail before any model call.
        for i in range(400):
            self.ev.cards.seed('project', '长卡%03d' % i, 'knowledge', '主题%d' % (i % 5),
                               '很长的标题' * 30 + str(i), '正文内容' * 40 + str(i))
        self.capture('触发一次局部维护。')
        payloads = []

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'proposal' in data:
                return {'approved': True, 'findings': []}, {}
            payloads.append(data)
            return {'operations': []}, {}

        result = self.run_model(model)
        self.assertEqual(result['processed'], 1)
        self.assertEqual(result['failed'], 0)
        self.assertTrue(payloads, 'compile was called')
        rows = payloads[0]['existing']['catalog']
        self.assertLessEqual(len(rows), 80)
        self.assertEqual(payloads[0]['existing']['candidate_scope'], 'bounded')
        self.assertEqual(payloads[0]['existing']['authorized_card_count'], 400)
        # The model never receives the whole 400-card library at once.
        self.assertLess(len(json.dumps(payloads[0]['existing']['catalog'], ensure_ascii=False)),
                        100000)

    def test_many_unrelated_cards_do_not_expand_model_working_set(self):
        self.capture('触发一次局部维护。')
        seen = []

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'proposal' in data:
                return {'approved': True, 'findings': []}, {}
            seen.append(len(data['existing']['catalog']))
            return {'operations': []}, {}

        self.run_model(model)
        first = seen[0]
        for i in range(300):
            self.ev.cards.seed('project', '无关%03d' % i, 'knowledge', '无关主题',
                               '无关标题%03d' % i, '无关正文%03d' % i)
        self.e.db.execute("UPDATE knowledge_work SET state='pending',bundle=NULL")
        self.e.db.commit()
        self.run_model(model)
        self.assertLessEqual(seen[-1], 80)
        self.assertLessEqual(seen[-1], max(first, 80))

    def test_unrelated_cards_never_enter_hint_or_expand_payload(self):
        # A genuinely relevant card (shares a token with the batch) that fits the
        # initial body budget is supplied in both runs.
        self.ev.cards.seed('project', '局部维护', 'memory', '方案', '局部维护',
                           '局部维护的正文。')
        for i in range(20):
            self.ev.cards.seed('project', '无关%03d' % i, 'knowledge', '无关主题',
                               '无关标题%03d' % i, '无关正文%03d' % i)
        self.capture('触发一次局部维护。')
        payloads = []

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'proposal' in data:
                return {'approved': True, 'findings': []}, {}
            payloads.append(data)
            return {'operations': []}, {}

        self.run_model(model)
        first = payloads[0]
        first_keys = [c['key'] for c in first['existing']['catalog']]
        first_body_keys = [c['key'] for c in first['existing']['cards']]
        first_size = len(json.dumps(first, ensure_ascii=False).encode())
        self.assertIn('局部维护', first_keys)
        self.assertFalse(any(k.startswith('无关') for k in first_keys))
        # Grow the library with 180 more *unrelated* cards and rebuild.
        for i in range(20, 200):
            self.ev.cards.seed('project', '无关%03d' % i, 'knowledge', '无关主题',
                               '无关标题%03d' % i, '无关正文%03d' % i)
        self.e.db.execute("UPDATE knowledge_work SET state='pending',bundle=NULL")
        self.e.db.commit()
        self.run_model(model)
        last = payloads[-1]
        last_keys = [c['key'] for c in last['existing']['catalog']]
        self.assertEqual(last_keys, first_keys)
        self.assertEqual([c['key'] for c in last['existing']['cards']], first_body_keys)
        # Payload is stable (only the informational universe count can differ).
        self.assertLessEqual(abs(len(json.dumps(last, ensure_ascii=False).encode()) - first_size), 8)

    def test_unrelated_shared_cards_do_not_enter_hint(self):
        for i in range(200):
            self.ev.cards.seed('shared', '共享无关%03d' % i, 'knowledge', '无关主题',
                               '共享无关标题%03d' % i, '共享无关正文%03d' % i)
        self.capture('触发一次局部维护。')
        payloads = []

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'proposal' in data:
                return {'approved': True, 'findings': []}, {}
            payloads.append(data)
            return {'operations': []}, {}

        result = self.run_model(model)
        self.assertEqual(result['processed'], 1)
        self.assertEqual(len(payloads[0]['existing']['catalog']), 0)
        self.assertEqual(payloads[0]['existing']['authorized_card_count'], 200)

    def test_ranking_is_independent_of_corpus_size(self):
        from tongle.evolution import rank
        relevant = [
            {'id': 'a' * 32, 'key': 'k1', 'title': '冷链密封', 'topic': '物流',
             'body': '冷链密封许可。', 'version': 1, 'updated_at': 1},
            {'id': 'b' * 32, 'key': 'k2', 'title': '容器运输', 'topic': '物流',
             'body': '容器运输封签。', 'version': 1, 'updated_at': 2},
        ]
        before = {c['id']: s for s, _t, c in rank(relevant, '冷链密封许可')}
        extra = [{'id': ('%032d' % i), 'key': 'z%d' % i, 'title': 'z',
                  'topic': '无关', 'body': '无关正文。', 'version': 1, 'updated_at': i}
                 for i in range(200)]
        after = {c['id']: s for s, _t, c in rank(relevant + extra, '冷链密封许可')}
        for cid, score in before.items():
            self.assertAlmostEqual(after[cid], score)


class NeedsSearchTests(_Harness):
    def _fillers(self, n=95):
        for i in range(n):
            self.ev.cards.seed('project', '填充%03d' % i, 'memory', '填充',
                               '填充标题%03d' % i, '无关内容%03d。' % i)

    def test_initial_miss_is_recovered_by_restricted_search(self):
        target = self.ev.cards.seed('project', '目标概念', 'memory', '方案',
                                    '目标概念', '目标概念的完整条件。')
        self._fillers(95)   # pushes the older target out of the 80-row hint
        self.capture('触发一次局部维护。')
        payloads = []

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'proposal' in data:
                return {'approved': True, 'findings': []}, {}
            payloads.append(data)
            if len(payloads) == 1:
                self.assertNotIn('目标概念',
                                 [c['key'] for c in data['existing']['cards']])
                return {'needs_search': {'query': '目标概念', 'cursor': 0}}, {}
            alias = _alias(data['existing'], '目标概念')
            s = data['sources'][-1]
            return {'operations': [{'op': 'update', 'id': alias, 'expected_version': 1,
                    'body': '目标概念的新条件。',
                    'evidence': [{'source_id': s['source_id'], 'version': s['version'],
                                  'quote': s['text']}],
                    'reason': '检索找回旧卡后更新'}]}, {}

        result = self.run_model(model)
        self.assertEqual(result['processed'], 1)
        self.assertEqual(self.ev.cards.get(target['id'])['body'], '目标概念的新条件。')
        self.assertIn('search_results', payloads[1])

    def test_invalid_search_requests_are_bounded_and_rejected(self):
        self.capture('触发一次局部维护。')
        self._fillers(5)
        calls = []

        def model(cmd, prompt, timeout, env=None):
            calls.append(json.loads(prompt))
            return {'needs_search': {'query': 'x', 'scope': 'project'}}, {}

        result = self.run_model(model)
        self.assertEqual(result['failed'], 1)
        self.assertEqual(len(calls), 3)
        self.assertIn('stage call limit', self.work('failed')[0]['error'])

    def test_search_validation_rejects_unsafe_shapes(self):
        for bad in (None, 'x', {'query': ''}, {'query': 'a' * 300},
                    {'query': 'ok', 'cursor': -1}, {'query': 'ok', 'cursor': 10 ** 9},
                    {'query': 'ok', 'cursor': True}, {'query': 'ok\x00bad'},
                    {'query': 'ok', 'project': 'other'}, {'query': 'ok', 'extra': 1}):
            info, error = validate_search_request(bad)
            self.assertIsNone(info, bad)
            self.assertIsNotNone(error, bad)
        info, error = validate_search_request({'query': '合法查询'})
        self.assertEqual(info, {'query': '合法查询', 'cursor': 0})
        self.assertIsNone(error)

    def test_search_is_idf_free_and_deterministic(self):
        cards = [{'id': 'a' * 32, 'key': 'k1', 'title': '目标概念', 'topic': 'x',
                  'body': '目标概念的说明。'}]
        page, total = search_page(cards, '目标概念', 0)
        self.assertEqual(total, 1)
        # Adding unrelated cards must not change the result identity.
        more = cards + [{'id': ('b' * 31) + str(i), 'key': 'z%d' % i, 'title': 'z',
                         'topic': 'z', 'body': 'z'} for i in range(9)]
        page2, total2 = search_page(more, '目标概念', 0)
        self.assertEqual([c['id'] for _, c in page], [c['id'] for _, c in page2])


class DuplicateRecallTests(_Harness):
    def _compile_create(self, key, title, body):
        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'proposal' in data:
                return {'approved': True, 'findings': []}, {}
            s = data['sources'][-1]
            return {'operations': [{'op': 'create', 'key': key, 'kind': 'memory',
                    'topic': '方案', 'title': title, 'body': body,
                    'evidence': [{'source_id': s['source_id'], 'version': s['version'],
                                  'quote': s['text']}], 'reason': '新增'}]}, {}
        return model

    def test_retired_same_key_create_after_read_is_rejected(self):
        self.ev.cards.seed('project', '退役概念', 'memory', '方案', '退役概念',
                           '退役正文。', state='retired')
        self.capture('新增材料。')
        result = self.run_attempts(self._compile_create('退役概念', '退役概念', '全新正文。'))
        self.assertEqual(result['failed'], 1)
        self.assertIn('create duplicates existing card', self.work('failed')[0]['error'])
        self.assertEqual(len(self.ev.cards.list('project', include_inactive=True)), 1)

    def test_body_norm_duplicate_after_read_is_rejected(self):
        self.ev.cards.seed('project', 'k1', 'memory', '方案', '标题一', '共享规范正文内容。')
        self.capture('新增材料。')
        result = self.run_attempts(self._compile_create('k2', '标题二', '共享规范正文内容。'))
        self.assertEqual(result['failed'], 1)
        self.assertEqual(len(self.ev.cards.list('project')), 1)

    def test_retired_body_only_duplicate_is_recalled_before_create(self):
        # The retired card's *title* shares nothing with the proposed create, but
        # its body carries the same claim. The mandatory independent comparison
        # recalls it and blocks the create; it is never silently published.
        self.ev.cards.seed('project', '容器运输封签', 'memory', '物流',
                           '容器运输封签', '冷链密封许可的有效条件。',
                           state='retired')
        self.capture('新增材料。')
        compared = {'bodies': set()}

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'frozen_proposal' in data:
                cand = {c['card_id']: c for c in data['page']['candidates']}
                out = []
                for p in data['page']['pairs']:
                    body = (cand.get(p['card_id'], {}).get('body') or '')
                    compared['bodies'].add(body)
                    if '冷链密封许可' in body:
                        out.append({'op': p['op'], 'card_id': p['card_id'],
                                    'relation': 'duplicate', 'quote': body[:60],
                                    'reason': 'the proposed create recreates this retired card claim'})
                    else:
                        out.append({'op': p['op'], 'card_id': p['card_id'],
                                    'relation': 'distinct', 'quote': body[:20] or '正文',
                                    'reason': 'scope and claim differ from the proposed change here'})
                return {'pairs': out}, {}
            if 'proposal' in data:
                return {'approved': True, 'findings': []}, {}
            s = data['sources'][-1]
            return {'operations': [{'op': 'create', 'key': '冷链密封许可',
                    'kind': 'memory', 'topic': '物流', 'title': '冷链密封许可',
                    'body': '冷链密封许可的新说明。',
                    'evidence': [{'source_id': s['source_id'], 'version': s['version'],
                                  'quote': s['text']}], 'reason': '新增'}]}, {}

        result = self.run_model(model)
        self.assertEqual(result['processed'], 0)
        self.assertEqual(result['failed'], 1)
        self.assertTrue(any('冷链密封许可的有效条件。' in b for b in compared['bodies']),
                        'the independent comparison read the retired body')
        # The create was never silently published as a new card.
        self.assertEqual([c['key'] for c in self.ev.cards.list('project', include_inactive=True)],
                         ['容器运输封签'])

    def test_same_batch_create_duplicates_rejected(self):
        self.capture('新增材料。')

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'proposal' in data:
                return {'approved': True, 'findings': []}, {}
            s = data['sources'][-1]
            op = lambda body: {'op': 'create', 'key': '同键', 'kind': 'memory',
                               'topic': '方案', 'title': '同题', 'body': body,
                               'evidence': [{'source_id': s['source_id'],
                                             'version': s['version'],
                                             'quote': s['text']}], 'reason': '重复'}
            return {'operations': [op('正文一。'), op('正文二。')]}, {}

        result = self.run_attempts(model)
        self.assertEqual(result['failed'], 1)
        self.assertIn('within batch', self.work('failed')[0]['error'])
        self.assertEqual(self.ev.cards.list('project'), [])

    def test_publication_replay_blocks_new_related_candidate(self):
        self.capture('新增材料。')
        ops = [{'op': 'create', 'key': '新键', 'kind': 'memory', 'topic': '方案',
                'title': '新题', 'body': '新正文。', 'evidence': []}]
        base = {'project': 'project', 'dedup_snapshot': {'exact': [], 'related': []},
                'search_receipts': []}
        # No matching card yet: replay is clean.
        self.ev._replay_related(base, ops)
        # A newly appearing related candidate must block the old approval.
        self.ev.cards.seed('project', '新键', 'memory', '方案', '新题', '别的正文。')
        with self.assertRaises(ValueError):
            self.ev._replay_related(base, ops)

    def test_publication_replay_blocks_changed_search_hit(self):
        card = self.ev.cards.seed('project', '检索目标', 'memory', '方案',
                                  '检索目标', '检索目标的正文。')
        page, _total = search_page(self.ev.cards.list('project'), '检索目标', 0)
        returned = [[c['id'], c['version']] for _, c in page]
        base = {'project': 'project', 'dedup_snapshot': {'exact': [], 'related': []},
                'search_receipts': [{'query': '检索目标', 'cursor': 0,
                                     'returned': returned}]}
        self.ev._replay_related(base, [])
        self.ev.cards.manual_edit(card['id'], 1, '检索目标的正文（人工修订）。')
        with self.assertRaises(ValueError):
            self.ev._replay_related(base, [])

    def test_initial_retrieval_replay_blocks_new_related_card_for_noop(self):
        # The batch is a no-op, so there is no create for duplicate_report to
        # catch; only the initial-retrieval related-hit receipt can notice a new
        # relevant card appearing during processing.
        query = '容器运输规则需要更新。'
        cards = self.ev._authorized_catalog('project')
        base = {'project': 'project', 'sources': [{'text': query}],
                'dedup_snapshot': {'exact': [], 'related': []},
                'related_hits': lexical_hits(cards, query)}
        self.ev._replay_related(base, [])
        # An unrelated card must NOT invalidate the approval.
        self.ev.cards.seed('project', '完全无关卡', 'memory', '无关主题',
                           '完全无关卡', '空气温度改变。')
        self.ev._replay_related(base, [])
        # A genuinely related card must block the old approval.
        self.ev.cards.seed('project', '容器运输例外', 'memory', '物流',
                           '容器运输例外', '容器运输规则的例外条件。')
        with self.assertRaises(ValueError):
            self.ev._replay_related(base, [])

    def test_replay_compares_search_hits_as_a_set(self):
        card = self.ev.cards.seed('project', '检索目标', 'memory', '方案',
                                  '检索目标', '检索目标的正文。')
        page, _total = search_page(self.ev.cards.list('project'), '检索目标', 0)
        returned = [[c['id'], c['version']] for _, c in page]
        # Same membership in a different order must still be accepted: replay
        # compares the set, never the page ranking.
        base = {'project': 'project', 'dedup_snapshot': {'exact': [], 'related': []},
                'search_receipts': [{'query': '检索目标', 'cursor': 0,
                                     'returned': list(reversed(returned))}]}
        self.ev._replay_related(base, [])


class GuardReuseTests(_Harness):
    def test_guard_tracks_only_relevant_cards(self):
        target = self.ev.cards.seed('project', '目标', 'memory', '方案', '目标', '正文。')
        other = self.ev.cards.seed('project', '无关', 'memory', '方案', '无关', '正文。')
        base = {'guard': {target['id']: target['version']}}
        self.assertTrue(self.ev._guard_still_current(base))
        # An unrelated card change (not in the guard) is reusable.
        self.ev.cards.manual_edit(other['id'], 1, '无关正文改动。')
        self.assertTrue(self.ev._guard_still_current(base))
        # A relevant card change invalidates the cache.
        self.ev.cards.manual_edit(target['id'], target['version'], '目标正文改动。')
        self.assertFalse(self.ev._guard_still_current(base))


class SuccessionTests(_Harness):
    def _two_documents(self):
        a = self.root / 'A.md'; a.write_text('甲来源的内容。')
        b = self.root / 'B.md'; b.write_text('乙来源的内容。')
        sa = self.e.store.ingest('project', str(a))
        sb = self.e.store.ingest('project', str(b))
        return a, b, sa, sb

    def _noop_model(self, cmd, prompt, timeout, env=None):
        data = json.loads(prompt)
        if 'proposal' in data:
            return {'approved': True, 'findings': []}, {}
        return {'operations': []}, {}

    def _queue_original(self):
        return self.ev.queue(force=True)

    def test_source_update_creates_successor_and_moves_seen(self):
        a, b, sa, sb = self._two_documents()
        self._queue_original()
        original = self.work('pending')
        self.assertEqual(len(original), 1)
        # A is updated before the batch runs.
        a.write_text('甲来源更新后的内容。')
        self.e.store.ingest('project', str(a))
        result = self.run_model(self._noop_model)
        self.assertEqual(result.get('superseded'), 1)
        rows = {r['id']: r for r in self.work()}
        self.assertEqual(rows[original[0]['id']]['state'], 'obsolete')
        successors = [r for r in rows.values() if r['succession_depth'] == 1]
        self.assertEqual(len(successors), 1)
        succ = successors[0]
        inputs = json.loads(succ['inputs'])
        refs = [p for p in inputs if p.get('responsibility') == 'reference']
        self.assertTrue(any(p['source_id'] == sa['source_id'] and p['version'] == 2 for p in refs))
        # B v1 ownership moved to the successor; A v2 is new material (no seen).
        seen_b = self.e.db.execute(
            'SELECT work_id FROM knowledge_seen WHERE source_id=? AND version=1 AND part=0',
            (sb['source_id'],)).fetchone()
        self.assertEqual(seen_b['work_id'], succ['id'])
        seen_a2 = self.e.db.execute(
            'SELECT work_id FROM knowledge_seen WHERE source_id=? AND version=2',
            (sa['source_id'],)).fetchone()
        # A v2 is new material queued as its own responsibility, never a
        # duplicate pending responsibility inside the successor.
        self.assertIsNotNone(seen_a2)
        self.assertNotIn(seen_a2['work_id'], (original[0]['id'], succ['id']))
        # The successor can then run normally.
        for _ in range(3):
            with patch('tongle.worker.invoke_json', side_effect=self._noop_model):
                self.ev.process(force=True)
            if self.e.db.execute(
                    'SELECT state FROM knowledge_work WHERE id=?', (succ['id'],)).fetchone()['state'] == 'done':
                break
        self.assertEqual(self.e.db.execute(
            'SELECT state FROM knowledge_work WHERE id=?', (succ['id'],)).fetchone()['state'],
            'done')

    def test_succession_is_idempotent(self):
        a, b, sa, sb = self._two_documents()
        self._queue_original()
        original = self.work('pending')[0]
        a.write_text('甲来源更新后的内容。'); self.e.store.ingest('project', str(a))
        first = self.ev._handle_supersession(original, None, {})
        self.assertTrue(first['created'])
        second = self.ev._handle_supersession(dict(self.e.db.execute(
            'SELECT * FROM knowledge_work WHERE id=?', (original['id'],)).fetchone()), None, {})
        self.assertFalse(second['created'])
        self.assertEqual(len([r for r in self.work() if r['succession_depth'] == 1]), 1)

    def test_succession_depth_limit_stops_chain(self):
        a, b, sa, sb = self._two_documents()
        self.ev.queue(force=True)
        a.write_text('甲来源更新后的内容。'); self.e.store.ingest('project', str(a))
        # Build a synthetic depth-4 row whose carried part is B v1 (unchanged)
        # and whose superseded part is A v1.
        base = self.work('pending')[0]
        inputs = json.loads(base['inputs'])
        carried = [p for p in inputs if p['source_id'] == sb['source_id']]
        superseded = [p for p in inputs if p['source_id'] == sa['source_id']]
        payload = json.dumps(carried + superseded)
        self.e.db.execute(
            "INSERT INTO knowledge_work(id,project,session,inputs,state,created_at,succession_depth)"
            " VALUES('deep','project','documents',?,'pending',?,4)", (payload, time.time()))
        self.e.db.commit()
        row = dict(self.e.db.execute("SELECT * FROM knowledge_work WHERE id='deep'").fetchone())
        out = self.ev._handle_supersession(row, None, {})
        self.assertFalse(out['created'])
        self.assertEqual(out['reason'], 'depth_limit')
        final = self.e.db.execute("SELECT state,error FROM knowledge_work WHERE id='deep'").fetchone()
        self.assertEqual(final['state'], 'failed')
        self.assertIn('depth limit', final['error'])
        self.assertEqual(len([r for r in self.work() if r['succession_depth'] >= 5]), 0)

    def test_succession_keeps_reference_read_only(self):
        a, b, sa, sb = self._two_documents()
        self.ev.queue(force=True)
        a.write_text('甲来源更新后的内容。'); self.e.store.ingest('project', str(a))
        original = self.work('pending')[0]
        first = self.ev._handle_supersession(original, None, {})
        succ = first['successor']
        # A second move must refresh the reference, never promote it to owned
        # responsibility.
        a.write_text('甲来源再次更新。'); self.e.store.ingest('project', str(a))
        succ_row = dict(self.e.db.execute('SELECT * FROM knowledge_work WHERE id=?', (succ,)).fetchone())
        second = self.ev._handle_supersession(succ_row, None, {})
        self.assertTrue(second['created'])
        succ2 = dict(self.e.db.execute('SELECT * FROM knowledge_work WHERE id=?',
                                       (second['successor'],)).fetchone())
        inputs = json.loads(succ2['inputs'])
        responsible = [p for p in inputs if p.get('responsibility') != 'reference']
        refs = [p for p in inputs if p.get('responsibility') == 'reference']
        self.assertEqual({p['source_id'] for p in responsible}, {sb['source_id']})
        self.assertEqual({(p['source_id'], p['version']) for p in refs},
                         {(sa['source_id'], 3)})

    def test_blocked_batch_also_transfers_successor(self):
        a, b, sa, sb = self._two_documents()
        self.ev.queue(force=True)
        original = self.work('pending')[0]
        self.e.db.execute("UPDATE knowledge_work SET state='blocked' WHERE id=?", (original['id'],))
        self.e.db.commit()
        a.write_text('甲来源更新后的内容。'); self.e.store.ingest('project', str(a))
        self.ev.queue(force=True)
        self.assertEqual(self.e.db.execute(
            'SELECT state FROM knowledge_work WHERE id=?', (original['id'],)).fetchone()['state'],
            'obsolete')
        self.assertEqual(len([r for r in self.work() if r['succession_depth'] == 1]), 1)


class CatalogRecoveryTests(_Harness):
    def _insert(self, wid, state, error, committed=False):
        with self.e.transaction():
            self.e.db.execute(
                "INSERT INTO knowledge_work(id,project,session,inputs,state,created_at,error)"
                " VALUES(?,?,?,?,?,?,?)", (wid, 'project', 's', '[]', state, time.time(), error))
            if committed:
                self.e.db.execute(
                    "INSERT INTO knowledge_batches(batch_id,project,op_hash,review_hash,changed_ids,created_at)"
                    " VALUES(?,'project','o','r','[]',?)", (wid, time.time()))

    def test_catalog_overflow_failed_recovered_once(self):
        self._insert('overflow', 'failed',
                     'StageLimit: authorized catalog exceeds fixed bound; cannot decide without complete catalog')
        self._insert('other', 'failed', 'ValueError: something else')
        self._insert('obsolete', 'obsolete', 'manual')
        self._insert('done', 'done', None)
        self._insert('committed', 'failed', 'StageLimit: authorized catalog exceeds fixed bound', committed=True)
        first = self.ev.recover_catalog_overflow()
        self.assertEqual(first['recovered'], 1)
        self.assertTrue(self.ev.recover_catalog_overflow()['already_done'])
        states = {r['id']: r['state'] for r in self.work()}
        self.assertEqual(states['overflow'], 'pending')
        self.assertEqual(states['other'], 'failed')
        self.assertEqual(states['obsolete'], 'obsolete')
        self.assertEqual(states['done'], 'done')
        self.assertEqual(states['committed'], 'failed')

    def test_non_catalog_input_bound_is_not_misrecovered(self):
        # A different input error that merely contains 'exceeds fixed bound'
        # must never be treated as the retired catalog-overflow deadlock.
        self._insert('bytebuf', 'failed',
                     'ValueError: unrelated byte buffer exceeds fixed bound')
        result = self.ev.recover_catalog_overflow()
        self.assertEqual(result['recovered'], 0)
        self.assertEqual(self.work('failed')[0]['id'], 'bytebuf')

    def test_overflow_with_moved_and_unchanged_sources_hands_off_responsibility(self):
        from tongle.evolution import canonical as ev_canonical
        a = self.root / 'A.md'; a.write_text('甲来源。')
        b = self.root / 'B.md'; b.write_text('乙来源。')
        sa = self.e.store.ingest('project', str(a))
        sb = self.e.store.ingest('project', str(b))
        parts = [
            {'source_id': sa['source_id'], 'version': 1, 'role': 'document',
             'kind': 'document', 'part': 0, 'start': 0, 'end': 4, 'source_length': 4,
             'text': '甲来源。', 'partial': False},
            {'source_id': sb['source_id'], 'version': 1, 'role': 'document',
             'kind': 'document', 'part': 0, 'start': 0, 'end': 4, 'source_length': 4,
             'text': '乙来源。', 'partial': False},
        ]
        with self.e.transaction():
            self.e.db.execute(
                "INSERT INTO knowledge_work(id,project,session,inputs,state,created_at,error)"
                " VALUES('overflow','project','documents',?,'failed',?,?)",
                (ev_canonical(parts), time.time(),
                 'StageLimit: authorized catalog exceeds fixed bound; cannot decide'))
            for sid in (sa['source_id'], sb['source_id']):
                self.e.db.execute("INSERT INTO knowledge_seen VALUES(?,?,?,?)",
                                  (sid, 1, 0, 'overflow'))
        a.write_text('甲来源v2。'); self.e.store.ingest('project', str(a))
        result = self.ev.recover_catalog_overflow()
        self.assertEqual(result['recovered'], 0)
        overflow = self.e.db.execute(
            "SELECT state,error FROM knowledge_work WHERE id='overflow'").fetchone()
        self.assertEqual(overflow['state'], 'obsolete')
        self.assertIn('successor', overflow['error'])
        successors = [r for r in self.work() if (r['succession_depth'] or 0) == 1]
        self.assertEqual(len(successors), 1)
        succ = successors[0]
        inputs = json.loads(succ['inputs'])
        responsible = [p for p in inputs if p.get('responsibility') != 'reference']
        self.assertEqual({p['source_id'] for p in responsible}, {sb['source_id']})
        seen_b = self.e.db.execute(
            'SELECT work_id FROM knowledge_seen WHERE source_id=? AND version=1',
            (sb['source_id'],)).fetchone()
        self.assertEqual(seen_b['work_id'], succ['id'])
        # Marker is committed atomically with the hand-off.
        self.assertIsNotNone(self.e.db.execute(
            "SELECT value FROM meta WHERE key='knowledge_recovery_catalog_overflow_v1'").fetchone())


class RelatedReplayProcessTests(_Harness):
    """Initial-retrieval replay through the real process path."""

    def _update_proposal(self, target):
        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'proposal' in data:
                return {'approved': True, 'findings': []}, {}
            s = data['sources'][-1]
            return {'operations': [{'op': 'update', 'id': target['id'],
                    'expected_version': 1, 'body': '容器运输规则的新正文。',
                    'evidence': [{'source_id': s['source_id'], 'version': s['version'],
                                  'quote': s['text']}], 'reason': '更新'}]}, {}
        return model

    def test_relevant_card_appearing_during_review_blocks_update(self):
        target = self.ev.cards.seed('project', '容器运输规则', 'memory', '物流',
                                    '容器运输规则', '容器运输规则的旧正文。')
        self.capture('容器运输规则需要更新。')
        base_model = self._update_proposal(target)
        seeded = {'done': False}

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'proposal' in data and not seeded['done']:
                seeded['done'] = True
                self.ev.cards.seed('project', '容器运输例外', 'memory', '物流',
                                   '容器运输例外', '容器运输规则的例外条件。')
            return base_model(cmd, prompt, timeout, env)

        result = self.run_model(model)
        self.assertEqual(result['processed'], 0)
        row = self.work('retry')[0]
        self.assertIn('related hits changed', row['error'])
        self.assertEqual(self.ev.cards.get(target['id'])['body'], '容器运输规则的旧正文。')
        # The next bounded attempt rebuilds against the new relevant card and
        # can publish the update instead of failing permanently.
        second = self.run_model(model)
        self.assertEqual(second['processed'], 1)
        self.assertEqual(self.ev.cards.get(target['id'])['body'], '容器运输规则的新正文。')

    def test_unrelated_card_appearing_during_review_does_not_block(self):
        target = self.ev.cards.seed('project', '容器运输规则', 'memory', '物流',
                                    '容器运输规则', '容器运输规则的旧正文。')
        self.capture('容器运输规则需要更新。')
        base_model = self._update_proposal(target)
        seeded = {'done': False}

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'proposal' in data and not seeded['done']:
                seeded['done'] = True
                self.ev.cards.seed('project', '完全无关卡', 'memory', '无关主题',
                                   '完全无关卡', '空气温度改变。')
            return base_model(cmd, prompt, timeout, env)

        result = self.run_model(model)
        self.assertEqual(result['processed'], 1)
        self.assertEqual(self.ev.cards.get(target['id'])['body'], '容器运输规则的新正文。')


class ReviewRebuildTests(_Harness):
    """A relevant card discovered at review triggers a bounded rebuild, not a
    permanent capacity failure or a silent acceptance against the old snapshot."""

    def test_new_related_card_at_review_triggers_bounded_rebuild(self):
        self.capture('新增材料。')
        seeded = {'done': False}
        supplied_with_related = {'seen': False}
        related_read = {'body': None}

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'frozen_proposal' in data:
                # The synthetic related card genuinely overlaps the proposed
                # create, so the frozen comparison names a real blocking
                # relation (with an exact old-card quote and a specific reason)
                # -- never a rubber-stamp distinct that would publish the
                # duplicate just to finish the attempt.
                cand = {c['card_id']: c for c in data['page']['candidates']}
                pairs = []
                for p in data['page']['pairs']:
                    body = (cand.get(p['card_id'], {}).get('body') or '') or '正文'
                    pairs.append({'op': p['op'], 'card_id': p['card_id'],
                                  'relation': 'duplicate', 'quote': body[:60],
                                  'reason': 'the proposed create recreates this '
                                            'overlapping existing card claim'})
                return {'pairs': pairs}, {}
            if 'proposal' in data:
                if not seeded['done']:
                    seeded['done'] = True
                    # A genuinely related card arrives at review, after the
                    # compiler had already decided against the old snapshot.
                    self.ev.cards.seed('project', '容器运输封签', 'memory', '物流',
                                       '容器运输封签', '冷链密封许可的有效条件。')
                # Independently confirm each open obligation (none on the first
                # attempt, the appeared related target on the final one).
                recon = data.get('reconciliation') or []
                confirmations = [
                    {'card_id': r['card_id'], 'decision': 'confirmed',
                     'reason': '独立确认该相关卡在当前完整正文下确无增量价值'}
                    for r in recon]
                return {'approved': True, 'findings': [],
                        'reconciliation_confirmations': confirmations}, {}
            keys = [c.get('key') for c in (data['existing'].get('cards') or [])]
            keys += [c.get('key') for c in (data['existing'].get('catalog') or [])]
            if '容器运输封签' in keys:
                # The whole related card is now supplied: read it in full and
                # record an intentional no-op (no new duplicate card), with the
                # concrete overall reason and one explicit per-target no-op
                # resolution the current contract requires.
                supplied_with_related['seen'] = True
                for card in (data['existing'].get('cards') or []):
                    if card.get('key') == '容器运输封签':
                        related_read['body'] = card.get('body')
                recon = data.get('reconciliation') or []
                resolutions = [
                    {'card_id': r['card_id'], 'decision': 'noop',
                     'reason': '该相关卡已承载同一冷链密封主张，本轮确无增量价值不新建'}
                    for r in recon]
                return {'operations': [],
                        'reason': '该相关卡已承载同一主张，本轮确无增量价值',
                        'reconciliation_resolutions': resolutions}, {}
            if data.get('retry_findings'):
                # The bounded revision carries the blocked comparison finding.
                # The related card is only a related hint, not a bounded
                # candidate, so request its whole body through the real
                # restricted-search protocol (needs_search) before deciding.
                return {'needs_search': {'query': '冷链密封许可', 'cursor': 0}}, {}
            s = data['sources'][-1]
            return {'operations': [{'op': 'create', 'key': '冷链密封许可',
                    'kind': 'memory', 'topic': '物流', 'title': '冷链密封许可',
                    'body': '冷链密封许可的新说明。',
                    'evidence': [{'source_id': s['source_id'], 'version': s['version'],
                                  'quote': s['text']}], 'reason': '新增'}]}, {}

        # First bounded attempt: the related card appears during verify, so the
        # old review cannot publish against the stale snapshot.
        first = self.run_model(model)
        self.assertEqual(first['processed'], 0)
        row = self.work('retry')[0]
        self.assertNotIn('stage call limit', row['error'])
        self.assertNotIn('failed', row['state'])
        # The refusal is the currency/publication guard, never a silent accept.
        self.assertIn('SelectionChanged', row['error'])
        with self.e.transaction():
            self.e.db.execute('UPDATE knowledge_work SET next_at=0')
        # Second bounded attempt re-compiles the frozen proposal; the
        # independent comparison now names the genuinely overlapping card as a
        # blocking finding instead of approving it.
        second = self.run_model(model)
        self.assertEqual(second['processed'], 0)
        row2 = self.work('retry')[0]
        # The second attempt was stopped by the independent comparison finding,
        # not by a repeated currency block or a capacity failure.
        self.assertIn('comparison', row2['error'])
        self.assertIsNone(self.e.db.execute(
            'SELECT 1 FROM knowledge_batches').fetchone())
        with self.e.transaction():
            self.e.db.execute('UPDATE knowledge_work SET next_at=0')
        # Third bounded attempt reads the whole overlapping card (the feedback
        # target) and records an intentional no-op: it completes without a
        # duplicate and without a permanent failure.
        third = self.run_model(model)
        self.assertEqual(third['processed'], 1)
        self.assertTrue(supplied_with_related['seen'])
        self.assertEqual(related_read['body'], '冷链密封许可的有效条件。')
        self.assertEqual([c['key'] for c in self.ev.cards.list('project')],
                         ['容器运输封签'])

    def test_repeated_concurrent_change_is_bounded_not_stuck(self):
        self.capture('新增材料。')
        count = {'n': 0}

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'frozen_proposal' in data:
                cand = {c['card_id']: c for c in data['page']['candidates']}
                out = []
                for p in data['page']['pairs']:
                    body = (cand.get(p['card_id'], {}).get('body') or '') or '正文'
                    out.append({'op': p['op'], 'card_id': p['card_id'],
                                'relation': 'distinct', 'quote': body[:20],
                                'reason': 'scope and claim differ from the proposed change here'})
                return {'pairs': out}, {}
            if 'proposal' in data:
                count['n'] += 1
                self.ev.cards.seed('project', '相关卡%03d' % count['n'], 'memory', '物流',
                                   '相关卡%03d' % count['n'],
                                   '新增材料的关联条件%d。' % count['n'])
                return {'approved': True, 'findings': []}, {}
            s = data['sources'][-1]
            return {'operations': [{'op': 'create', 'key': '新增材料键',
                    'kind': 'memory', 'topic': '物流', 'title': '新增材料键',
                    'body': '新增材料的内容。',
                    'evidence': [{'source_id': s['source_id'], 'version': s['version'],
                                  'quote': s['text']}], 'reason': '新增'}]}, {}

        for _ in range(3):
            self.run_model(model)
        row = self.e.db.execute('SELECT state,error FROM knowledge_work').fetchone()
        # Bounded by the existing attempt ceiling, not an infinite rebuild loop.
        self.assertEqual(row['state'], 'failed')
        self.assertIn('SelectionChanged', row['error'])


class SupersessionOwnershipTests(_Harness):
    def _queued_job(self):
        a = self.root / 'A.md'; a.write_text('甲来源的内容。')
        b = self.root / 'B.md'; b.write_text('乙来源的内容。')
        self.e.store.ingest('project', str(a))
        self.e.store.ingest('project', str(b))
        self.ev.queue(force=True)
        return dict(self.work('pending')[0])

    def test_stale_owner_cannot_obsolete_new_owners_batch(self):
        job = self._queued_job()
        a = self.root / 'A.md'; a.write_text('甲来源更新后的内容。')
        self.e.store.ingest('project', str(a))
        with self.e.transaction():
            self.e.db.execute(
                "UPDATE knowledge_work SET state='running',lease_token='new-owner',lease_until=?"
                " WHERE id=?", (time.time() + 600, job['id']))
        before_seen = [dict(r) for r in self.e.db.execute('SELECT * FROM knowledge_seen').fetchall()]
        out = self.ev._handle_supersession(job, 'stale-owner', {})
        self.assertEqual(out['reason'], 'stale_owner')
        row = self.e.db.execute(
            'SELECT state,lease_token FROM knowledge_work WHERE id=?', (job['id'],)).fetchone()
        self.assertEqual(row['state'], 'running')
        self.assertEqual(row['lease_token'], 'new-owner')
        self.assertEqual(self.e.db.execute(
            'SELECT COUNT(*) FROM knowledge_work WHERE succession_depth>0').fetchone()[0], 0)
        self.assertEqual(self.e.db.execute(
            'SELECT COUNT(*) FROM knowledge_failures WHERE work_id=?', (job['id'],)).fetchone()[0], 0)
        self.assertEqual([dict(r) for r in self.e.db.execute(
            'SELECT * FROM knowledge_seen').fetchall()], before_seen)

    def test_tokenless_caller_cannot_touch_a_running_batch(self):
        job = self._queued_job()
        with self.e.transaction():
            self.e.db.execute(
                "UPDATE knowledge_work SET state='running',lease_token='new-owner',lease_until=?"
                " WHERE id=?", (time.time() + 600, job['id']))
        out = self.ev._handle_supersession(job, None, {})
        self.assertEqual(out['reason'], 'active_lease')
        row = self.e.db.execute(
            'SELECT state,lease_token FROM knowledge_work WHERE id=?', (job['id'],)).fetchone()
        self.assertEqual(row['state'], 'running')
        self.assertEqual(row['lease_token'], 'new-owner')


class SuccessionMixedTests(_Harness):
    def _three_documents(self):
        out = {}
        for name, text in (('A', '甲来源。'), ('B', '乙来源。'), ('C', '丙来源。')):
            p = self.root / (name + '.md'); p.write_text(text)
            out[name] = (p, self.e.store.ingest('project', str(p)))
        self.ev.queue(force=True)
        return out

    def test_unavailable_sibling_blocks_instead_of_stranding_unchanged(self):
        docs = self._three_documents()
        job = dict(self.work('pending')[0])
        docs['A'][0].write_text('甲来源v2。')
        self.e.store.ingest('project', str(docs['A'][0]))
        with self.e.transaction():
            self.e.db.execute(
                "INSERT OR REPLACE INTO source_status(source_id,state,updated_at)"
                " VALUES(?,'missing',?)", (docs['B'][1]['source_id'], time.time()))
        out = self.ev._handle_supersession(job, None, {})
        self.assertEqual(out['reason'], 'blocked')
        row = self.e.db.execute(
            'SELECT state FROM knowledge_work WHERE id=?', (job['id'],)).fetchone()
        self.assertEqual(row['state'], 'blocked')
        self.assertEqual(self.e.db.execute(
            'SELECT COUNT(*) FROM knowledge_work WHERE succession_depth>0').fetchone()[0], 0)
        # C's unchanged responsibility keeps its seen ownership on the original.
        seen_c = self.e.db.execute(
            'SELECT work_id FROM knowledge_seen WHERE source_id=? AND version=1',
            (docs['C'][1]['source_id'],)).fetchone()
        self.assertEqual(seen_c['work_id'], job['id'])

    def test_all_responsibility_superseded_makes_no_reference_only_successor(self):
        p = self.root / 'A.md'; p.write_text('甲来源。')
        self.e.store.ingest('project', str(p))
        self.ev.queue(force=True)
        job = dict(self.work('pending')[0])
        p.write_text('甲来源v2。')
        self.e.store.ingest('project', str(p))
        out = self.ev._handle_supersession(job, None, {})
        self.assertEqual(out['reason'], 'no_unchanged_material')
        self.assertEqual(self.e.db.execute(
            'SELECT COUNT(*) FROM knowledge_work WHERE succession_depth>0').fetchone()[0], 0)

    def test_unreadable_source_record_blocks_rather_than_stranding(self):
        # source_available() is fail-open for unknown ids; a batch naming a
        # source with no readable record must block (keep seen ownership), not
        # be obsoleted as if there were no unchanged material.
        missing = 'd' * 32
        with self.e.transaction():
            self.e.db.execute(
                "INSERT INTO knowledge_work(id,project,session,inputs,state,created_at)"
                " VALUES('w','project','documents',?,'pending',?)",
                (json.dumps([{'source_id': missing, 'version': 1, 'role': 'document',
                              'kind': 'document', 'part': 0, 'start': 0, 'end': 1,
                              'source_length': 1, 'text': 'x', 'partial': False}]),
                 time.time()))
            self.e.db.execute("INSERT INTO knowledge_seen VALUES(?,?,?,?)",
                              (missing, 1, 0, 'w'))
        job = dict(self.e.db.execute("SELECT * FROM knowledge_work WHERE id='w'").fetchone())
        out = self.ev._handle_supersession(job, None, {})
        self.assertEqual(out['reason'], 'blocked')
        row = self.e.db.execute("SELECT state FROM knowledge_work WHERE id='w'").fetchone()
        self.assertEqual(row['state'], 'blocked')
        seen = self.e.db.execute(
            'SELECT work_id FROM knowledge_seen WHERE source_id=?', (missing,)).fetchone()
        self.assertEqual(seen['work_id'], 'w')


class NoProgressTests(_Harness):
    def test_repeated_empty_search_stops_at_stage_cap(self):
        self.capture('触发一次局部维护。')
        calls = []

        def model(cmd, prompt, timeout, env=None):
            calls.append(json.loads(prompt))
            return {'needs_search': {'query': '不存在的词xyz', 'cursor': 0}}, {}

        result = self.run_model(model)
        self.assertEqual(result['failed'], 1)
        self.assertEqual(len(calls), 3)
        self.assertIn('stage call limit', self.work('failed')[0]['error'])


class ViewsGroupingTests(_Harness):
    def test_failed_is_stopped_not_pending_with_safe_reason(self):
        with self.e.transaction():
            self.e.db.execute(
                "INSERT INTO knowledge_work(id,project,session,inputs,state,created_at,error)"
                " VALUES('p','project','s','[]','pending',?,NULL)", (time.time(),))
            self.e.db.execute(
                "INSERT INTO knowledge_work(id,project,session,inputs,state,created_at,error)"
                " VALUES('f','project','s','[]','failed',?,?)",
                (time.time(), 'StageLimit: create duplicates existing card: SECRET_CANARY'))
        snap = status_snapshot(self.e, {'active': 0, 'conflict': 0})
        self.assertEqual(snap['pending'], 1)
        self.assertEqual(snap['failed'], 1)
        refresh(self.e)
        page = (self.root / 'vault/运行情况.md').read_text()
        self.assertIn('已停止自动重试', page)
        self.assertIn('新增与既有卡重复', page)
        self.assertNotIn('SECRET_CANARY', page)
        self.assertNotIn('create duplicates existing card', page)

    def test_failed_label_is_neutral_and_not_model_blame(self):
        with self.e.transaction():
            self.e.db.execute(
                "INSERT INTO knowledge_work(id,project,session,inputs,state,created_at,error)"
                " VALUES('f','project','s','[]','failed',?,?)",
                (time.time(), 'OSError: disk temporarily unavailable'))
        refresh(self.e)
        page = (self.root / 'vault/运行情况.md').read_text()
        self.assertIn('已停止自动重试', page)
        self.assertNotIn('模型处理失败', page)

    def test_catalog_bound_label_is_precise_to_catalog_errors(self):
        with self.e.transaction():
            self.e.db.execute(
                "INSERT INTO knowledge_work(id,project,session,inputs,state,created_at,error)"
                " VALUES('cat','project','s','[]','failed',?,?)",
                (time.time(), 'StageLimit: authorized catalog exceeds fixed bound'))
            self.e.db.execute(
                "INSERT INTO knowledge_work(id,project,session,inputs,state,created_at,error)"
                " VALUES('buf','project','s','[]','failed',?,?)",
                (time.time(), 'ValueError: unrelated byte buffer exceeds fixed bound'))
        refresh(self.e)
        page = (self.root / 'vault/运行情况.md').read_text()
        self.assertIn('候选目录超出固定上限（旧机制）', page)
        # The label appears once, for the catalog row only: the byte-buffer row
        # must not be mislabelled as a catalog bound.
        self.assertEqual(page.count('候选目录超出固定上限（旧机制）'), 1)

    def test_oldest_pending_age_excludes_failed_rows(self):
        now = time.time()
        with self.e.transaction():
            self.e.db.execute(
                "INSERT INTO knowledge_work(id,project,session,inputs,state,created_at,error)"
                " VALUES('old_failed','project','s','[]','failed',?,NULL)", (now - 99999,))
            self.e.db.execute(
                "INSERT INTO knowledge_work(id,project,session,inputs,state,created_at,error)"
                " VALUES('p','project','s','[]','pending',?,NULL)", (now - 3600,))
        snap = status_snapshot(self.e, {'active': 0, 'conflict': 0})
        self.assertGreaterEqual(snap['oldest_age'], 3600)
        self.assertLess(snap['oldest_age'], 90000)


class RequiredClosureReplayTests(_Harness):
    """Pre-publication replay must recompute the full required closure.

    The old replay only recomputed lexical hits, so a card created during
    verify whose evidence cites a batch source -- but whose key/title/topic/body
    share no token with it -- changed the required reading set without changing
    ``lexical_hits``. That phantom must block an old no-op approval.
    """

    def _noop(self, cmd, prompt, timeout, env=None):
        data = json.loads(prompt)
        if 'proposal' in data:
            return {'approved': True, 'findings': []}, {}
        return {'operations': []}, {}

    def test_new_evidence_source_dependent_card_blocks_noop_publish(self):
        rec = self.capture('本批中文来源内容。')
        seeded = {'done': False}
        payloads = []

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            payloads.append(data)
            if 'proposal' in data and not seeded['done']:
                seeded['done'] = True
                # English definition card: evidence precisely cites the batch
                # Chinese source, but key/title/topic/body share no token.
                self.ev.cards.seed(
                    'project', 'english-def', 'knowledge', 'vocabulary',
                    'English definition',
                    'A concise English gloss sharing no token with the source.',
                    evidence=[{'source_id': rec['source_id'],
                               'version': rec['version'],
                               'quote': '本批中文来源内容。'}])
            return self._noop(cmd, prompt, timeout, env)

        first = self.run_model(model)
        self.assertEqual(first['processed'], 0)
        row = self.work('retry')[0]
        self.assertIn('required closure changed', row['error'])
        # No batch was committed under the phantom-blind approval.
        self.assertIsNone(self.e.db.execute(
            'SELECT 1 FROM knowledge_batches').fetchone())
        # The phantom is invisible to the lexical replay (the old bug), proving
        # the closure comparison, not lexical hits, caught it.
        self.assertEqual(
            lexical_hits(self.ev._authorized_catalog('project'), '本批中文来源内容。'),
            [])
        # The next bounded attempt rebuilds, reads the required body and finishes.
        second = self.run_model(model)
        self.assertEqual(second['processed'], 1)
        self.assertTrue(any(c.get('key') == 'english-def'
                            for p in payloads for c in p['existing']['cards']))
        self.assertEqual(self.e.db.execute(
            'SELECT COUNT(*) FROM knowledge_batches').fetchone()[0], 1)

    def test_recursive_basis_addition_blocks_publish(self):
        rec = self.capture('来源甲。')
        m = self.ev.cards.seed('project', '源依赖卡', 'memory', '方案', '源依赖卡',
                               '源依赖卡的完整正文。',
                               evidence=[{'source_id': rec['source_id'],
                                          'version': rec['version'],
                                          'quote': '来源甲。'}])
        seeded = {'done': False}

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'proposal' in data and not seeded['done']:
                seeded['done'] = True
                # The new card enters the required closure only through the
                # reverse-basis / recursive walk, not through direct evidence.
                q = self.ev.cards.seed('project', '派生卡', 'framework', '方案',
                                       '派生卡', '基于源依赖卡的派生判断。')
                self.ev.cards.relations.set(
                    q['id'], [{'card_id': m['id'], 'version': m['version']}])
            return self._noop(cmd, prompt, timeout, env)

        first = self.run_model(model)
        self.assertEqual(first['processed'], 0)
        self.assertIn('required closure changed', self.work('retry')[0]['error'])
        self.assertIsNone(self.e.db.execute(
            'SELECT 1 FROM knowledge_batches').fetchone())
        second = self.run_model(model)
        self.assertEqual(second['processed'], 1)
        self.assertIsNotNone(self.e.db.execute(
            'SELECT 1 FROM knowledge_batches').fetchone())

    def test_unrelated_source_evidence_does_not_block_publish(self):
        self.capture('本批中文来源内容。')
        seeded = {'done': False}

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'proposal' in data and not seeded['done']:
                seeded['done'] = True
                # Cites an out-of-batch source id and shares no token: neither
                # required nor lexically related, so it must not invalidate.
                self.ev.cards.seed('project', '无关证据卡', 'memory', '无关主题',
                                   '无关证据卡', '无关证据卡的正文。',
                                   evidence=[{'source_id': 'f' * 32, 'version': 1,
                                              'quote': '无关。'}])
            return self._noop(cmd, prompt, timeout, env)

        result = self.run_model(model)
        self.assertEqual(result['processed'], 1)

    def test_replay_does_not_compare_raw_mandatory_to_closure(self):
        # Stored ``mandatory`` is the *closure* (direct + transitive basis), not
        # the raw mandatory set. Replay recomputes the same closure and must not
        # raise merely because raw mandatory_ids is a strict subset.
        rec = self.capture('来源甲。')
        m = self.ev.cards.seed('project', '依赖卡', 'memory', '方案', '依赖卡',
                               '依赖卡正文。',
                               evidence=[{'source_id': rec['source_id'],
                                          'version': rec['version'], 'quote': '来源甲。'}])
        p = self.ev.cards.seed('project', '基础卡', 'memory', '方案', '基础卡',
                               '基础卡正文。')
        self.ev.cards.relations.set(
            m['id'], [{'card_id': p['id'], 'version': p['version']}])
        cards = self.ev._authorized_catalog('project')
        required, problems = basis_closure_report(
            cards, mandatory_ids(cards, [{'source_id': rec['source_id']}]))
        self.assertTrue(m['id'] in required and p['id'] in required)
        sources = [{'source_id': rec['source_id'], 'text': '来源甲。'}]
        base = {'project': 'project', 'sources': sources,
                'dedup_snapshot': {'exact': [], 'related': []},
                'related_hits': lexical_hits(cards, '来源甲。'),
                'existing': {'mandatory': sorted(required),
                             'required_problems': problems}}
        self.ev._replay_related(base, [])  # must not raise


class ScopeFingerprintTests(_Harness):
    """Effective authorization scope is a host field checked before reuse,
    before every model dispatch, and before publish; scope changes rebuild."""

    def _transient_then_successful(self):
        """Run once to cache a bundle, then make the retry immediately eligible."""
        calls = {'compile': 0}

        def transient(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'proposal' in data:
                raise OSError('transient upstream')
            calls['compile'] += 1
            return {'operations': []}, {}
        self.run_model(transient)
        row = self.work('retry')[0]
        self.assertIn('OSError', row['error'])
        self.e.db.execute('UPDATE knowledge_work SET next_at=0')
        self.e.db.commit()
        return calls

    def _successful_model(self, calls, payloads):
        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            payloads.append(data)
            if 'proposal' in data:
                return {'approved': True, 'findings': []}, {}
            calls['compile'] += 1
            return {'operations': []}, {}
        return model

    def test_narrowed_scope_blocks_reuse_and_drops_shared(self):
        self.ev.cards.seed('shared', '共享规则', 'memory', '方案', '共享规则',
                           '共享规则的完整正文。')
        self.capture('共享规则需要更新。')
        calls = self._transient_then_successful()
        # Narrow: this project may no longer read shared cards.
        self.write_config(library_excluded_projects=['project'])
        payloads = []
        result = self.run_model(self._successful_model(calls, payloads))
        self.assertEqual(result['processed'], 1)
        # The cached shared body was not reused; the scope change forced a rebuild.
        self.assertEqual(calls['compile'], 2)
        for data in payloads:
            self.assertNotIn('共享规则',
                             [c.get('key') for c in data['existing']['cards']])
            # Scope is a host field and never reaches the model.
            self.assertNotIn('scope_fingerprint', data)
            self.assertNotIn('scope_fingerprint', data.get('existing') or {})

    def test_widened_scope_forces_rebuild_with_shared(self):
        self.write_config(library_excluded_projects=['project'])
        self.ev.cards.seed('shared', '共享规则', 'memory', '方案', '共享规则',
                           '共享规则的完整正文。')
        self.capture('共享规则需要更新。')
        calls = self._transient_then_successful()
        # Widen again: shared is authorized once more.
        self.write_config()
        payloads = []
        result = self.run_model(self._successful_model(calls, payloads))
        self.assertEqual(result['processed'], 1)
        self.assertEqual(calls['compile'], 2)
        self.assertTrue(any('共享规则' in
                            [c.get('key') for c in p['existing']['catalog']]
                            for p in payloads))

    def test_narrowed_scope_between_compile_and_verify_blocks_dispatch(self):
        # A shared body is authorized when the bundle is selected, so the
        # compiler receives it. The compiler narrows this project's scope on
        # return, revoking shared. The verify dispatch must re-read the live
        # scope and refuse to send that now-unauthorized body -- the defect
        # let it through under the config cached at process start and only
        # rejected at publish. A bounded retry, not a stale approval, must own
        # the unfinished responsibility afterwards.
        self.ev.cards.seed('shared', '共享规则', 'memory', '方案', '共享规则',
                           '共享规则的完整正文。')
        self.capture('共享规则需要更新。')
        verify_payloads = []

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'proposal' in data:
                # Reached only if the verify provider call was dispatched with
                # a stale scope; with the fix the pre-dispatch check fires first.
                verify_payloads.append(data)
                return {'approved': True, 'findings': []}, {}
            # Compile returns after narrowing scope, revoking the shared card.
            self.write_config(library_excluded_projects=['project'])
            return {'operations': []}, {}

        result = self.run_model(model)
        # The revoked body was never dispatched to verify.
        self.assertEqual(verify_payloads, [])
        self.assertEqual(result['failed'], 1)
        row = self.work('retry')[0]
        self.assertIn('SelectionChanged', row['error'])
        # The stale approval did not publish and its frozen bundle is dropped,
        # so the bounded retry re-owns the responsibility from a live scope.
        self.assertIsNone(self.e.db.execute(
            'SELECT 1 FROM knowledge_batches').fetchone())
        self.assertIsNone(row['bundle'])

    def test_other_project_scope_change_does_not_invalidate(self):
        self.capture('本项目材料。')
        calls = self._transient_then_successful()
        # Only another project's scope changes; this project is unaffected.
        self.write_config(library_excluded_projects=['other-project'],
                          knowledge_project_topics={'other-project': ['X']})
        payloads = []
        result = self.run_model(self._successful_model(calls, payloads))
        self.assertEqual(result['processed'], 1)
        # Reuse succeeded: compile was not called again for this project.
        self.assertEqual(calls['compile'], 1)


class SearchNeedsCardsBudgetTests(_Harness):
    def test_run_search_returns_post_search_remaining_budget(self):
        a = self.ev.cards.seed('project', '检索目标甲', 'memory', '方案',
                               '检索目标甲', '甲' * 600)
        b = self.ev.cards.seed('project', '检索目标乙', 'memory', '方案',
                               '检索目标乙', '乙' * 600)
        catalog = [{'id': c['id'], 'project': c['project'], 'version': c['version']}
                   for c in (a, b)]
        base = {'project': 'project', 'existing': {'cards': [], 'catalog': catalog}}
        rows, loaded, receipt, left = self.ev._run_search(
            base, {'query': '检索目标甲', 'cursor': 0}, 'compile', 1, 1000)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(left, 1000 - len(loaded[0]['body']))
        other = b if loaded[0]['id'] == a['id'] else a
        # Using the stale pre-search budget would load the second body and
        # exceed the budget; the reported remainder refuses it.
        stale, _rej, _ = self.ev._hydrate(base, [other['id']], set(), 1000)
        self.assertEqual([c['id'] for c in stale], [other['id']])
        added, rejected, _ = self.ev._hydrate(
            base, [other['id']], {c['id'] for c in loaded}, left)
        self.assertEqual(added, [])
        self.assertTrue(any(r.get('reason') == 'exceeds_body_budget' for r in rejected))

    def test_mixed_reply_cannot_exceed_on_demand_body_budget(self):
        # Small on-demand budget; two relevant cards each too large for the
        # initial bodies. A single mixed search+needs_cards reply must share one
        # budget, so the stage can never dispatch both full bodies in one call.
        self.write_config(knowledge_on_demand_body_chars=26000,
                          knowledge_initial_body_chars=2000)
        self.ev.cards.seed('project', '检索目标甲', 'memory', '方案',
                           '检索目标甲', '甲' * 15000)
        self.ev.cards.seed('project', '检索目标乙', 'memory', '方案',
                           '检索目标乙', '乙' * 15000)
        self.capture('检索目标需要复核。')
        payloads = []

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            payloads.append(data)
            alias = _alias(data['existing'], '检索目标乙')
            return {'needs_search': {'query': '检索目标甲', 'cursor': 0},
                    'needs_cards': [alias]}, {}

        result = self.run_model(model)
        self.assertEqual(result['failed'], 1)
        self.assertIn('stage call limit', self.work('failed')[0]['error'])
        for data in payloads:
            total = sum(len(c.get('body') or '') for c in data['existing']['cards'])
            self.assertLessEqual(total, 26000)


class RequiredRepackTests(_Harness):
    """Final-response required-priority repacking of the fixed body budget.

    An earlier optional body must never permanently crowd out a complete
    required set that by itself fits the fixed total. The stage may evict
    optional bodies and re-dispatch the same stage in an existing slot, but
    only for the whole required union and only when a slot remains.
    """

    _QUERY = 'alpha beta gamma delta epsilon'

    def _config(self, initial=26000, total=52000):
        cfg = {'knowledge_mode': 'evolving', 'model_command': ['stub'],
               'knowledge_quiet_seconds': 0,
               'knowledge_initial_body_chars': initial,
               'knowledge_on_demand_body_chars': total}
        (self.e.home / 'config.json').write_text(json.dumps(cfg))
        return cfg

    def _optional(self, size=25900, key='optional'):
        pad = (self._QUERY + ' x' * ((size - len(self._QUERY)) // 2)
               + 'x' * ((size - len(self._QUERY)) % 2))
        return self.ev.cards.seed('project', key, 'knowledge', '方案', key, pad)

    def _target(self, key='target', size=30000):
        # Shares one query token so it appears in the bounded catalog hint, but
        # ranks below the optional card and does not fit the initial budget.
        return self.ev.cards.seed('project', key, 'knowledge', '主题', key,
                                  'alpha ' + 'z' * (size - 6))

    def _base_and_job(self, cfg, wid='repack'):
        rec = self.capture(self._QUERY)
        part = list(self.ev._source_parts(self.e.store.source(rec['source_id'])))[0]
        selected = self.ev._related('project', [part], cfg)
        guard = selected.pop('guard')
        base = {'project': 'project', 'sources': [part], 'existing': selected,
                'catalog_cards': self.ev._authorized_catalog('project'),
                'scope_fingerprint': self.ev._scope_fingerprint('project'),
                'guard': guard, 'stage_calls': {},
                'read_sets': {'compile': {}, 'verify': {}}}
        with self.e.transaction():
            self.e.db.execute(
                'INSERT INTO knowledge_work(id,project,session,inputs,state,'
                'lease_token,lease_until,created_at) VALUES(?,?,?,?,?,?,?,?)',
                (wid, 'project', 's', json.dumps([part]), 'running', 'tok',
                 time.time() + 600, time.time()))
        job = dict(self.e.db.execute(
            'SELECT * FROM knowledge_work WHERE id=?', (wid,)).fetchone())
        return base, job

    def _drive(self, cfg, base, job, script):
        """Run one compile stage with a scripted reply per provider call."""
        payloads = []
        mandatory = set(base['existing'].get('mandatory') or ())

        def call(_job, stage, payload, _cfg):
            payloads.append({
                'stage': stage,
                'keys': [c['key'] for c in payload['existing']['cards']],
                'chars': sum(len(c.get('body') or '')
                             for c in payload['existing']['cards']),
            })
            return script[min(len(payloads) - 1, len(script) - 1)]

        def requirements(response):
            ops = response.get('operations') if isinstance(response, dict) else None
            ids = {op['id'] for op in (ops or [])
                   if isinstance(op, dict) and isinstance(op.get('id'), str)}
            return ids | mandatory

        error = None
        with patch.object(self.ev, '_call', side_effect=call):
            try:
                self.ev._stage_loop(job, 'compile', base, 'TASK', cfg, 'tok',
                                    base['read_sets']['compile'],
                                    requirements=requirements)
            except Exception as exc:  # noqa: BLE001 - captured for assertions
                error = type(exc).__name__ + ': ' + str(exc)
        return payloads, error

    def test_crowded_optional_is_evicted_and_required_dispatched(self):
        cfg = self._config()
        optional = self._optional()
        target = self._target()
        base, job = self._base_and_job(cfg)
        reply = {'operations': [{'op': 'update', 'id': target['id']}]}
        payloads, error = self._drive(cfg, base, job, [reply, reply])
        self.assertIsNone(error)
        self.assertEqual([p['keys'] for p in payloads],
                         [['optional'], ['target']])
        # The fixed total is respected and the optional body is gone.
        self.assertLessEqual(payloads[-1]['chars'], 52000)
        compile_read = base['read_sets']['compile']
        self.assertIn(target['id'], compile_read)
        self.assertNotIn(optional['id'], compile_read)

    def test_evicted_card_is_resent_when_wanted_changes(self):
        cfg = self._config()
        optional = self._optional()
        target = self._target()
        base, job = self._base_and_job(cfg)
        reply = lambda cid: {'operations': [{'op': 'update', 'id': cid}]}
        script = [reply(target['id']), reply(optional['id']),
                  reply(optional['id'])]
        payloads, error = self._drive(cfg, base, job, script)
        self.assertIsNone(error)
        # The evicted optional is never treated as already read: it must be
        # re-dispatched before the changed final can be accepted.
        self.assertEqual([p['keys'] for p in payloads],
                         [['optional'], ['target'], ['optional']])
        self.assertIn(optional['id'], base['read_sets']['compile'])
        self.assertNotIn(target['id'], base['read_sets']['compile'])

    def test_third_call_repack_cannot_make_fourth_call(self):
        cfg = self._config()
        optional = self._optional()
        target = self._target()
        base, job = self._base_and_job(cfg)
        reply = lambda cid: {'operations': [{'op': 'update', 'id': cid}]}
        script = [reply(target['id']), reply(optional['id']),
                  reply(target['id'])]
        payloads, error = self._drive(cfg, base, job, script)
        self.assertEqual(len(payloads), 3)
        self.assertIsNotNone(error)
        self.assertIn('no stage slot', error)
        diag = base['capacity_diagnostic']
        self.assertEqual(diag['reason'], 'required_fits_but_no_stage_slot')
        stored = json.loads(self.e.db.execute(
            'SELECT bundle FROM knowledge_work WHERE id=?',
            (job['id'],)).fetchone()['bundle'])
        self.assertEqual(stored['capacity_diagnostic']['reason'],
                         'required_fits_but_no_stage_slot')

    def test_truly_over_budget_required_fails_with_size_diagnostic(self):
        cfg = self._config(initial=2000)
        first = self._target('first', 30000)
        second = self._target('second', 23000)
        base, job = self._base_and_job(cfg)
        reply = {'operations': [{'op': 'update', 'id': first['id']},
                                {'op': 'update', 'id': second['id']}]}
        payloads, error = self._drive(cfg, base, job, [reply])
        self.assertEqual(len(payloads), 1)
        self.assertIsNotNone(error)
        self.assertIn('exceeds body budget', error)
        diag = base['capacity_diagnostic']
        self.assertEqual(diag['reason'], 'required_set_exceeds_body_budget')
        self.assertEqual(diag['required_cards'], 2)
        self.assertEqual(diag['required_chars'], 53000)
        self.assertEqual(diag['budget_chars'], 52000)
        self.assertEqual(diag['current_chars'], 0)
        # Diagnostics carry aggregate sizes only, never body/source text.
        self.assertNotIn('zzzzzzzzzz', json.dumps(diag))

    def test_foreign_required_is_not_rescued(self):
        cfg = self._config()
        self._optional()
        base, job = self._base_and_job(cfg)
        reply = {'operations': [{'op': 'update', 'id': 'f' * 32}]}
        payloads, error = self._drive(cfg, base, job, [reply])
        self.assertEqual(len(payloads), 1)
        self.assertIsNotNone(error)
        self.assertIn('needs_cards could not be satisfied', error)
        self.assertNotIn('capacity_diagnostic', base)

    def test_stale_required_is_not_rescued(self):
        cfg = self._config()
        self._optional()
        target = self._target()
        base, job = self._base_and_job(cfg)
        with self.e.transaction():
            self.e.db.execute('UPDATE knowledge_cards SET version=version+1 WHERE id=?',
                              (target['id'],))
        reply = {'operations': [{'op': 'update', 'id': target['id']}]}
        payloads, error = self._drive(cfg, base, job, [reply])
        self.assertEqual(len(payloads), 1)
        self.assertIsNotNone(error)
        self.assertIn('needs_cards could not be satisfied', error)
        self.assertNotIn('capacity_diagnostic', base)

    def test_full_compile_and_verify_pass_under_crowding(self):
        # End-to-end: compile repacks, verify is reached, and the batch is
        # published within the bounded stage slots.
        self.write_config(knowledge_initial_body_chars=26000,
                          knowledge_on_demand_body_chars=52000)
        optional = self._optional()
        target = self._target()
        self.capture(self._QUERY)
        seen = []

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'proposal' in data:
                seen.append(('verify', None))
                return {'approved': True, 'findings': []}, {}
            seen.append(('compile',
                         [c['key'] for c in data['existing']['cards']]))
            alias = _alias(data['existing'], 'target')
            source = data['sources'][-1]
            return {'operations': [{'op': 'update', 'id': alias,
                    'expected_version': 1, 'body': 'target 已更新。',
                    'evidence': [{'source_id': source['source_id'],
                                  'version': source['version'],
                                  'quote': source['text']}],
                    'reason': 'crowding repack'}]}, {}

        result = self.run_model(model)
        self.assertEqual(result['processed'], 1)
        self.assertEqual(result['failed'], 0)
        self.assertEqual(seen[0], ('compile', ['optional']))
        self.assertEqual(seen[1], ('compile', ['target']))
        self.assertEqual(seen[2][0], 'verify')
        self.assertEqual(len([s for s in seen if s[0] == 'compile']), 2)
        self.assertEqual(self.ev.cards.get(target['id'])['body'], 'target 已更新。')
        # The optional card is only evicted from the payload, never deleted.
        self.assertIsNotNone(self.ev.cards.get(optional['id']))


class _CompareHarness(unittest.TestCase):
    """Harness for the 2026-10-02 phase2 frozen-proposal comparison tests.

    Synthetic only: no live vault, model, credentials or network. A model stub
    proves machine behaviour, not semantic quality. Kept separate from the
    module ``_Harness`` so neither phase's fixtures interfere.
    """

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
               # Synthetic-only, generous finite daily allowance so a multi-page
               # comparison (several provider calls per attempt) is bounded by
               # the attempt/dispatch ceilings, never by an accidental token
               # wall. Production input/output/attempt caps are untouched.
               'knowledge_daily_token_budget': 100000000,
               'knowledge_daily_call_budget': 100000}
        cfg.update(overrides)
        (self.e.home / 'config.json').write_text(json.dumps(cfg))

    def _record_call(self, job, stage='compare'):
        """Synthetic stand-in for the real ``_call`` dispatch-identity row.

        Settled with an explicit ``elapsed=0`` so it matches the production
        ``_call`` contract (``elapsed IS NOT NULL`` marks a finished call whose
        receipt may be reused); an unsettled row must never be reusable.
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

    def work(self, state=None):
        sql = 'SELECT * FROM knowledge_work'
        args = ()
        if state:
            sql += ' WHERE state=?'; args = (state,)
        return [dict(r) for r in self.e.db.execute(sql, args).fetchall()]

    def _source(self, rec):
        return self.e.store.source(rec['source_id'])

    def _unit_context(self, text=None, wid='w'):
        """A frozen-bundle-shaped base plus a leased work row for unit tests."""
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

    def _distinct(self, data):
        cand = {c['card_id']: c for c in data['page']['candidates']}
        out = []
        for p in data['page']['pairs']:
            body = (cand.get(p['card_id'], {}).get('body') or '') or '正文'
            out.append({'op': p['op'], 'card_id': p['card_id'],
                        'relation': 'distinct', 'quote': body[:20],
                        'reason': 'scope and claim differ from the proposed change here'})
        return {'pairs': out}

    def _create_op(self, key, title, body, source):
        return {'op': 'create', 'key': key, 'kind': 'memory', 'topic': '主题',
                'title': title, 'body': body,
                'evidence': [{'source_id': source['source_id'],
                              'version': source['version'], 'quote': source['text']}],
                'reason': '新增材料'}


class OverflowCompletionTests(_CompareHarness):
    """The 8*8125=65000 overflow completes only after every body is compared."""

    def test_65k_eight_weak_overlap_cards_complete_only_after_all_compared(self):
        for i in range(8):
            # Unique per-card body identity (same total size) so the test can
            # assert every complete body was compared: payload card ids are
            # per-call aliases (card001..) and cannot be compared across pages.
            self.seed('卡%d' % i, '%d共同主题' % i + '据' * 8120)
        rec = self.capture()
        source = self._source(rec)
        seen = {'compare_calls': 0, 'body_chars': 0, 'bodies': set()}

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'frozen_proposal' in data:
                seen['compare_calls'] += 1
                seen['body_chars'] += sum(len(c['body']) for c in data['page']['candidates'])
                seen['bodies'].update(c['body'] for c in data['page']['candidates'])
                return self._distinct(data), {}
            if 'proposal' in data:
                return {'approved': True, 'findings': []}, {}
            return {'operations': [self._create_op('新主题', '新主题', '共同主题的新说明。', source)]}, {}

        result = self.run_model(model)
        self.assertEqual(result['processed'], 1)
        self.assertEqual(result['failed'], 0)
        self.assertEqual(seen['body_chars'], 65000)
        self.assertEqual(len(seen['bodies']), 8)
        self.assertEqual(seen['compare_calls'], 2)
        self.assertEqual([c['key'] for c in self.ev.cards.list('project')
                          if c['key'] == '新主题'], ['新主题'])
        rows = self.e.db.execute(
            "SELECT error FROM knowledge_failures WHERE state='completion'").fetchall()
        record = json.loads(rows[0]['error'])
        self.assertEqual(record['comparison']['page_count'], 2)
        self.assertEqual(record['comparison']['pair_count'], 8)
        self.assertTrue(record['comparison']['all_distinct'])

    def test_two_creates_sixteen_candidates_are_all_covered(self):
        for i in range(8):
            # Unique per-card body identity (same total size), so all 16 complete
            # bodies are counted instead of the per-payload card aliases.
            self.seed('甲卡%d' % i, '组甲%d' % i + '甲' * 3995)
            self.seed('乙卡%d' % i, '组乙%d' % i + '乙' * 3995)
        rec = self.capture()
        source = self._source(rec)
        seen = {'pairs': set()}

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'frozen_proposal' in data:
                cand = {c['card_id']: c for c in data['page']['candidates']}
                for p in data['page']['pairs']:
                    seen['pairs'].add((p['op'], cand.get(p['card_id'], {}).get('body')))
                return self._distinct(data), {}
            if 'proposal' in data:
                return {'approved': True, 'findings': []}, {}
            return {'operations': [
                self._create_op('键甲', '题甲', '组甲的新说明。', source),
                self._create_op('键乙', '题乙', '组乙的新说明。', source)]}, {}

        result = self.run_model(model)
        self.assertEqual(result['processed'], 1)
        self.assertEqual(len(seen['pairs']), 16)
        ops = {}
        for op, _body in seen['pairs']:
            ops[op] = ops.get(op, 0) + 1
        self.assertEqual(ops, {0: 8, 1: 8})


class CollisionRevisionTests(_CompareHarness):
    def _seed_overflow_with_late_duplicate(self):
        for i in range(7):
            self.seed('甲卡%d' % i, '组甲' + '甲' * 7996)
        self.seed('乙旧卡', '组乙的有效条件为温度低于零下十八度。', state='retired')
        self.seed('乙填充0', '组乙' + '乙' * 7996)

    def test_later_page_duplicate_blocks_publish(self):
        self._seed_overflow_with_late_duplicate()
        rec = self.capture()
        source = self._source(rec)
        page_seen = {'n': 0}

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'frozen_proposal' in data:
                page_seen['n'] += 1
                cand = {c['card_id']: c for c in data['page']['candidates']}
                out = []
                for p in data['page']['pairs']:
                    c = cand.get(p['card_id'], {})
                    body = c.get('body') or ''
                    if c.get('state') == 'retired':
                        out.append({'op': p['op'], 'card_id': p['card_id'],
                                    'relation': 'duplicate', 'quote': body[:60],
                                    'reason': 'the proposed create recreates this retired card claim'})
                    else:
                        out.append({'op': p['op'], 'card_id': p['card_id'],
                                    'relation': 'distinct', 'quote': body[:20] or '正文',
                                    'reason': 'scope and claim differ from the proposed change here'})
                return {'pairs': out}, {}
            if 'proposal' in data:
                return {'approved': True, 'findings': []}, {}
            operations = [
                self._create_op('键甲', '题甲', '组甲的新说明。', source),
                self._create_op('键乙', '题乙', '组乙的新条件：温度需低于零下十八度。', source)]
            # Supply only the metadata the reconciliation contract requires for
            # the settled targets; the deliberately invalid duplicate proposal
            # is unchanged, so the rejection stays a real comparison finding
            # rather than a missing-schema error.
            recon = data.get('reconciliation') or []
            resolutions = [{'card_id': r['card_id'], 'decision': 'noop',
                            'reason': '该退役目标已被独立比较判为重复，本轮不再新建该身份'}
                           for r in recon]
            return {'operations': operations,
                    'reconciliation_resolutions': resolutions}, {}

        for _ in range(3):
            self.run_model(model)
            with self.e.transaction():
                self.e.db.execute('UPDATE knowledge_work SET next_at=0')
        row = self.work('failed')[0]
        keys = [c['key'] for c in self.ev.cards.list('project', include_inactive=True)]
        self.assertNotIn('键乙', keys)
        self.assertNotIn('键甲', keys)
        self.assertIsNone(self.e.db.execute('SELECT 1 FROM knowledge_batches').fetchone())
        self.assertIn('comparison', row['error'])
        self.assertGreaterEqual(page_seen['n'], 2)


class ReceiptBindingTests(_CompareHarness):
    def _op(self, body='共同主题的新说明。'):
        return {'op': 'create', 'key': '新键', 'kind': 'memory', 'topic': '主题',
                'title': '新题', 'body': body,
                'evidence': [{'source_id': 'f' * 32, 'version': 1, 'quote': 'x'}],
                'reason': '新增'}

    def _unit_base_with_receipts(self):
        for i in range(8):
            self.seed('卡%d' % i, '共同主题' + '据' * 7996)
        base, job, _part = self._unit_context()
        op = self._op()

        def call(_job, stage, payload, _cfg):
            self._record_call(_job, stage)
            return self._distinct(payload)
        with patch.object(self.ev, '_call', side_effect=call):
            reports, findings = self.ev._compare_stage(job, base, [op], 'tok', {})
        self.assertEqual(findings, [])
        self.assertTrue(reports)
        return base, job, op

    def test_edited_proposal_cannot_reuse_receipts(self):
        base, _job, op = self._unit_base_with_receipts()
        self.ev._verify_compare(base, [op])
        changed = dict(op, body='共同主题的另一版说明。')
        with self.assertRaises(SelectionChanged):
            self.ev._verify_compare(base, [changed])

    def test_card_change_after_page_blocks_old_publish(self):
        base, _job, op = self._unit_base_with_receipts()
        self.ev._verify_compare(base, [op])
        card = self.ev.cards.list('project', include_inactive=True)[0]
        self.ev.cards.manual_edit(card['id'], card['version'], '被人工改动的正文。')
        with self.assertRaises(SelectionChanged):
            self.ev._verify_compare(base, [op])

    def test_new_related_card_after_page_blocks_old_publish(self):
        base, _job, op = self._unit_base_with_receipts()
        self.ev._verify_compare(base, [op])
        self.seed('新增相关卡', '共同主题的补充说明。')
        with self.assertRaises(SelectionChanged):
            self.ev._verify_compare(base, [op])

    def test_scope_change_between_pages_blocks(self):
        for i in range(8):
            self.seed('卡%d' % i, '共同主题' + '据' * 7996)
        base, job, _part = self._unit_context()
        op = self._op()
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
        for i in range(8):
            self.seed('卡%d' % i, '共同主题' + '据' * 7996)
        base, job, _part = self._unit_context()
        op = self._op()
        calls = {'n': 0}

        def call(_job, stage, payload, _cfg):
            calls['n'] += 1
            self._record_call(_job, stage)
            if calls['n'] == 1:
                # Supersede the *same* source by re-ingesting its path.
                self._unit_path.write_text('来源的新版本内容。')
                self.e.store.ingest('project', str(self._unit_path))
            return self._distinct(payload)
        with patch.object(self.ev, '_call', side_effect=call):
            with self.assertRaises(ValueError):
                self.ev._compare_stage(job, base, [op], 'tok', {})

    def test_card_change_between_pages_blocks(self):
        for i in range(8):
            self.seed('卡%d' % i, '共同主题' + '据' * 7996)
        base, job, _part = self._unit_context()
        op = self._op()
        calls = {'n': 0}

        def call(_job, stage, payload, _cfg):
            calls['n'] += 1
            self._record_call(_job, stage)
            if calls['n'] == 1:
                # A later-page card must have changed before that page's dispatch.
                for card in self.ev.cards.list('project', include_inactive=True):
                    self.ev.cards.manual_edit(card['id'], card['version'],
                                              '页面之间的改动正文。')
            return self._distinct(payload)
        with patch.object(self.ev, '_call', side_effect=call):
            with self.assertRaises(SelectionChanged):
                self.ev._compare_stage(job, base, [op], 'tok', {})


class ResumeAndVerdictTests(_CompareHarness):
    def _seed_overflow(self):
        for i in range(8):
            self.seed('卡%d' % i, '共同主题' + '据' * 7996)

    def test_successful_page_and_transient_error_resume(self):
        self._seed_overflow()
        rec = self.capture()
        source = self._source(rec)
        calls = {'compare': 0}
        phase = {'fail_once': True}

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'frozen_proposal' in data:
                calls['compare'] += 1
                if phase['fail_once'] and calls['compare'] == 2:
                    raise OSError('transient upstream')
                return self._distinct(data), {}
            if 'proposal' in data:
                return {'approved': True, 'findings': []}, {}
            return {'operations': [self._create_op('新主题', '新主题', '共同主题的新说明。', source)]}, {}

        first = self.run_model(model)
        self.assertEqual(first['processed'], 0)
        row = self.work('retry')[0]
        self.assertIn('OSError', row['error'])
        stored = json.loads(row['bundle'])
        self.assertEqual(len(stored['compare']['pages']), 1)
        self.assertGreaterEqual(row['compare_dispatches'], 2)
        self.assertIsNotNone(row['proposal'])
        phase['fail_once'] = False
        with self.e.transaction():
            self.e.db.execute('UPDATE knowledge_work SET next_at=0')
        second = self.run_model(model)
        self.assertEqual(second['processed'], 1)
        self.assertEqual(calls['compare'], 3)

    def test_malformed_verdict_fails(self):
        self._seed_overflow()
        rec = self.capture()
        source = self._source(rec)

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'frozen_proposal' in data:
                return {'pairs': []}, {}
            if 'proposal' in data:
                return {'approved': True, 'findings': []}, {}
            return {'operations': [self._create_op('新主题', '新主题', '共同主题的新说明。', source)]}, {}

        for _ in range(3):
            self.run_model(model)
            with self.e.transaction():
                self.e.db.execute('UPDATE knowledge_work SET next_at=0')
        row = self.work('failed')[0]
        self.assertIn('comparison', row['error'])
        self.assertIsNone(self.e.db.execute('SELECT 1 FROM knowledge_batches').fetchone())


class NoCompareWhenUnnecessaryTests(_CompareHarness):
    def _model(self, ops):
        stages = {'compare': 0}

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'frozen_proposal' in data:
                stages['compare'] += 1
                return self._distinct(data), {}
            if 'proposal' in data:
                return {'approved': True, 'findings': []}, {}
            return {'operations': ops}, {}
        return model, stages

    def test_noop_spends_no_comparison_call(self):
        self.seed('既有', '共同主题的既有说明。')
        self.capture()
        model, stages = self._model([])
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
        model, stages = self._model([op])
        result = self.run_model(model)
        self.assertEqual(result['processed'], 1)
        self.assertEqual(stages['compare'], 0)


class TruthfulCapacityTests(_CompareHarness):
    def test_single_oversize_required_candidate_stays(self):
        big = self.seed('大卡', '共同令牌' + '大' * 56000)
        rec = self.capture()
        source = self._source(rec)

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'frozen_proposal' in data:
                return self._distinct(data), {}
            if 'proposal' in data:
                return {'approved': True, 'findings': []}, {}
            return {'operations': [self._create_op('新令牌', '新令牌', '共同令牌的新说明。', source)]}, {}

        result = self.run_model(model)
        self.assertEqual(result['failed'], 1)
        row = self.work('failed')[0]
        self.assertIn('oversize_required', row['error'])
        self.assertIsNotNone(self.ev.cards.get(big['id']))

    def test_compile_and_verify_full_targets_unchanged_under_delegation(self):
        target = self.seed('更新目标', '共同主题的旧目标正文。')
        for i in range(8):
            self.seed('卡%d' % i, '共同主题' + '据' * 7996)
        rec = self.capture()
        source = self._source(rec)
        payloads = []

        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            payloads.append(data)
            if 'frozen_proposal' in data:
                return self._distinct(data), {}
            if 'proposal' in data:
                return {'approved': True, 'findings': []}, {}
            update = {'op': 'update', 'id': target['id'],
                      'expected_version': target['version'],
                      'body': '共同主题的新目标正文。',
                      'evidence': [{'source_id': source['source_id'],
                                    'version': source['version'], 'quote': source['text']}],
                      'reason': '更新'}
            return {'operations': [update,
                    self._create_op('新主题', '新主题', '共同主题的新说明。', source)]}, {}

        result = self.run_model(model)
        self.assertEqual(result['processed'], 1)
        verify = [p for p in payloads if 'proposal' in p and 'comparison_reports' in p]
        self.assertTrue(verify)
        bodies = [c.get('body') for c in verify[-1]['existing']['cards']]
        self.assertIn('共同主题的旧目标正文。', bodies)


class PlanAndValidationUnitTests(unittest.TestCase):
    def test_plan_prefers_small_pages_and_never_drops_oversize_silently(self):
        bodies = {c: 'x' * 100 for c in ('a', 'b', 'c', 'd', 'e')}
        pages = plan_compare_pages(
            [{'op': 0, 'card_id': c, 'version': 1} for c in bodies],
            lambda c: bodies[c], 250)
        self.assertEqual([len(p['cards']) for p in pages], [2, 2, 1])
        self.assertTrue(all(len(p['cards']) <= MAX_COMPARE_CANDIDATES_PER_PAGE
                            for p in pages))
        with self.assertRaises(ComparePlanError) as ctx:
            plan_compare_pages([{'op': 0, 'card_id': 'a', 'version': 1}],
                               lambda c: 'y' * 500, 250)
        self.assertEqual(ctx.exception.reason, 'oversize_required')

    def test_validation_requires_complete_coverage_and_exact_quote(self):
        page = {'pairs': [[0, 'a' * 32], [0, 'b' * 32]], 'cards': ['a' * 32, 'b' * 32]}
        bodies = {'a' * 32: '旧卡甲的完整正文。', 'b' * 32: '旧卡乙的完整正文。'}
        bad = validate_compare_response(
            {'pairs': [{'op': 0, 'card_id': 'a' * 32, 'relation': 'distinct',
                        'quote': '旧卡甲的完整正文。',
                        'reason': 'different claim and scope entirely'}]}, page, bodies)
        self.assertFalse(bad['ok'])
        self.assertEqual(bad['error'], 'incomplete pair coverage')
        bad2 = validate_compare_response(
            {'pairs': [{'op': 0, 'card_id': 'a' * 32, 'relation': 'duplicate',
                        'quote': '不存在的引文',
                        'reason': 'the proposal recreates the same claim'},
                       {'op': 0, 'card_id': 'b' * 32, 'relation': 'distinct',
                        'reason': 'claims and scopes differ entirely'}]}, page, bodies)
        self.assertFalse(bad2['ok'])
        good = validate_compare_response(
            {'pairs': [{'op': 0, 'card_id': 'a' * 32, 'relation': 'duplicate',
                        'quote': '旧卡甲的完整正文。', 'reason': 'recreates the same claim'},
                       {'op': 0, 'card_id': 'b' * 32, 'relation': 'distinct',
                        'quote': '旧卡乙的完整正文。',
                        'reason': 'claims and scopes differ entirely'}]}, page, bodies)
        self.assertTrue(good['ok'])
        self.assertEqual(len(good['dispositions']), 2)
        self.assertEqual(len(good['findings']), 1)
        self.assertEqual(good['findings'][0]['card_id'], 'a' * 32)

    def test_distinct_without_quote_or_substantive_reason_is_rejected(self):
        page = {'pairs': [[0, 'a' * 32]], 'cards': ['a' * 32]}
        bodies = {'a' * 32: '旧卡甲的完整正文。'}
        no_quote = validate_compare_response(
            {'pairs': [{'op': 0, 'card_id': 'a' * 32, 'relation': 'distinct',
                        'reason': 'scope and claim clearly differ here'}]}, page, bodies)
        self.assertFalse(no_quote['ok'])
        fabricated = validate_compare_response(
            {'pairs': [{'op': 0, 'card_id': 'a' * 32, 'relation': 'distinct',
                        'quote': ' fabricated not in body ', 'reason': 'scope and claim clearly differ here'}]},
            page, bodies)
        self.assertFalse(fabricated['ok'])
        short_reason = validate_compare_response(
            {'pairs': [{'op': 0, 'card_id': 'a' * 32, 'relation': 'distinct',
                        'quote': '旧卡甲的完整正文。', 'reason': 'differs'}]}, page, bodies)
        self.assertFalse(short_reason['ok'])
        accepted = validate_compare_response(
            {'pairs': [{'op': 0, 'card_id': 'a' * 32, 'relation': 'distinct',
                        'quote': '旧卡甲的完整正文。',
                        'reason': 'scope and claim clearly differ here'}]}, page, bodies)
        self.assertTrue(accepted['ok'])
        self.assertEqual(accepted['dispositions'][0]['quote'], '旧卡甲的完整正文。')

    def test_vague_approval_cannot_clear_a_page(self):
        page = {'pairs': [[0, 'a' * 32]], 'cards': ['a' * 32]}
        bodies = {'a' * 32: '正文。'}
        self.assertFalse(validate_compare_response({'approved': True}, page, bodies)['ok'])
        self.assertFalse(validate_compare_response({'pairs': []}, page, bodies)['ok'])


if __name__ == '__main__':
    unittest.main()
