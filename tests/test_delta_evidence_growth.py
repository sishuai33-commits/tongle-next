"""Synthetic delta evidence-granularity regressions; no live assets, no models.

These tests pin the narrow ROOT correction isolated by the deterministic
recovery boundary error:

* a selected source span ref resolves to the full exact text of its frozen
  source PART (one evidence entry per source_id/version/full part text), so a
  single complete event with many paragraphs is one provenance quote instead of
  one stored evidence row per blank-line sub-span;
* an UPDATE inherits the changed card's already-persisted provenance verbatim
  and the inherited set carries no lifetime 12-entry cap, so a long-lived card
  can keep receiving updates whose incoming budget stays <=12 genuinely new
  unique entries;
* CREATE/RETIRE keep the unchanged <=12 incoming-entry budget; and
* every inherited and new entry still runs availability/project/current-version
  /exact-quote checks, with CAS/version protection unchanged.

The core apply runs through a REAL temp Engine + real KnowledgeStore; nothing
here claims real semantic model quality.
"""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tongle import delta_contract, source_windows
from tongle.engine import Engine
from tongle.knowledge_store import KnowledgeStore

PROJECT = 'project'


def _evidence(sid, ver, quote):
    return {'source_id': sid, 'version': ver, 'quote': quote}


class EvidenceGrowthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        (self.root / 'state').mkdir()
        (self.root / 'vault').mkdir()
        self.engine = Engine(self.root / 'state')
        self.engine.init(self.root / 'vault')
        self.addCleanup(self.engine.store.close)
        self.cards = KnowledgeStore(self.engine)

    # ------------------------------------------------------------ fixtures

    def _capture(self, event_id, text):
        receipt = self.engine.store.capture(PROJECT, 'session', 'user', event_id, text)
        return receipt['source_id']

    def _part(self, sid, part=0, responsibility='owned', text=None):
        src = self.engine.store.source(sid)
        if text is None:
            text = src['text']
        return {
            'source_id': sid,
            'version': src['current_version'],
            'part': part,
            'role': src['role'],
            'kind': src['kind'],
            'text': text,
            'session': src['session'],
            'ledger_captured_at': src['created_at'],
            'ledger_version_at': src['version_created_at'],
            'responsibility': responsibility,
        }

    def _context(self, sid, cards, text=None, responsibility='owned'):
        window = source_windows.freeze_window(
            PROJECT, [self._part(sid, responsibility=responsibility, text=text)])
        return delta_contract.build_context(window, cards)

    def _review(self, plan):
        checks = [{
            'index': i, 'E': 'pass', 'Q': 'pass', 'T': 'pass', 'N': 'pass',
            'reason': '证据与覆盖均复核通过',
        } for i in range(len(plan['dispositions']))]
        return {
            'checks': checks,
            'whole_window': {'status': 'pass', 'reason': '整个窗口复核通过'},
            'human_preservation': [],
        }

    def _card_payload(self, cid):
        return self.cards.get(cid)

    # -------------------------------------------------------------- tests

    def test_many_paragraphs_one_part_is_one_incoming_evidence(self):
        # One complete event, one source, one part, 13 blank-line paragraphs.
        text = '\n\n'.join(f'第{i}段事实' for i in range(1, 14))
        sid = self._capture('evt-many', text)
        ctx = self._context(sid, [])

        # 13 owned spans exist; the change cites the 12-ref maximum, all in the
        # single part, and every one must collapse to the SAME full-part quote.
        refs = [f's{i}' for i in range(1, 13)]
        change = {
            'ref': 'n1', 'action': 'create', 'source_refs': refs,
            'reason': '一整条事件需要作为一份证据保存', 'title': '多段事件',
            'topic': '事件', 'body': '一整条多段事件的正文。',
        }
        disps = [
            {'source_refs': refs, 'decision': 'material', 'change_refs': ['n1'],
             'card_refs': [], 'reason': '该完整事件形成可持久保存的事实'},
            {'source_refs': ['s13'], 'decision': 'nonmaterial', 'change_refs': [],
             'card_refs': [], 'reason': '该段为冗余承接，无独立持久价值'},
        ]
        plan = delta_contract.compile_plan(ctx, {'changes': [change], 'dispositions': disps})
        op = plan['operations'][0]
        self.assertEqual(op['op'], 'create')
        self.assertEqual(len(op['evidence']), 1)
        self.assertEqual(op['evidence'][0], _evidence(sid, 1, text))

        approval = delta_contract.approve_review(ctx, plan, self._review(plan))
        result = self.cards.apply('batch-many', PROJECT, plan['operations'], approval)
        cid = result['changed_ids'][0]
        self.assertEqual(self.cards.get(cid)['evidence'], [_evidence(sid, 1, text)])

    def test_create_cites_all_span_refs_of_one_many_paragraph_event(self):
        # SOURCE_REF breadth is the number of actual spans, not a flat 12-event
        # cap: a 13-paragraph single event may cite every span ref, keep all of
        # them material, and collapse to ONE whole-part quote. No fake
        # nonmaterial tail is needed to dodge a ref limit.
        text = '\n\n'.join(f'第{i}段事实' for i in range(1, 14))
        sid = self._capture('evt-allrefs', text)
        ctx = self._context(sid, [])
        refs = []
        counter = 0
        for part in ctx['window']['parts']:
            for _span in part['span_ids']:
                counter += 1
                refs.append(f's{counter}')
        self.assertEqual(len(refs), 13)
        # The dynamic wire schema already enumerates every real span alias with
        # no 12-item maximum, so the compiler must accept exactly these refs.
        schema = json.loads(delta_contract.plan_payload(ctx)['response_schema'])
        create_arm = next(
            v for v in schema['properties']['changes']['items']['oneOf']
            if v['title'] == 'create')
        self.assertEqual(create_arm['properties']['source_refs']['items']['enum'], refs)
        self.assertNotIn('maxItems', create_arm['properties']['source_refs'])

        change = {
            'ref': 'n1', 'action': 'create', 'source_refs': list(refs),
            'reason': '一整条多段事件需要作为一份证据保存', 'title': '多段事件',
            'topic': '事件', 'body': '一整条多段事件的正文。',
        }
        disps = [{'source_refs': list(refs), 'decision': 'material',
                  'change_refs': ['n1'], 'card_refs': [],
                  'reason': '该完整事件形成可持久保存的事实'}]
        plan = delta_contract.compile_plan(ctx, {'changes': [change], 'dispositions': disps})
        op = plan['operations'][0]
        self.assertEqual(op['op'], 'create')
        self.assertEqual(len(op['evidence']), 1)
        self.assertEqual(op['evidence'][0], _evidence(sid, 1, text))
        self.assertEqual([d['decision'] for d in plan['dispositions']], ['material'])

        approval = delta_contract.approve_review(ctx, plan, self._review(plan))
        result = self.cards.apply('batch-allrefs', PROJECT, plan['operations'], approval)
        self.assertEqual(self.cards.get(result['changed_ids'][0])['evidence'],
                         [_evidence(sid, 1, text)])

    def test_update_inherits_twelve_plus_one_new_with_exact_provenance(self):
        # A long-lived card already carries 12 distinct provenance tuples.
        twelve = '\n\n'.join(f'既存事实{i}' for i in range(1, 13))
        sid = self._capture('evt-inherit', twelve)
        inherited = [_evidence(sid, 1, f'既存事实{i}') for i in range(1, 13)]
        seeded = self.cards.seed(PROJECT, 'long-lived', 'knowledge', '主题',
                                 '长期卡', '长期卡正文段落。', evidence=inherited)
        cid = seeded['id']
        self.assertEqual(self.cards.get(cid)['evidence'], inherited)

        ctx = self._context(sid, [self._card_payload(cid)])
        refs = [f's{i}' for i in range(1, 13)]
        change = {
            'ref': 'n1', 'action': 'update', 'source_refs': refs,
            'reason': '新增一整条事件证据', 'target': 'c1',
            'edits': [{'op': 'insert_after', 'segment': 'c1g1', 'text': '新增一整条事件。'}],
        }
        disps = [{'source_refs': refs, 'decision': 'material', 'change_refs': ['n1'],
                  'card_refs': [], 'reason': '该事件产生需保存的新增内容'}]
        plan = delta_contract.compile_plan(ctx, {'changes': [change], 'dispositions': disps})
        op = plan['operations'][0]
        self.assertEqual(op['op'], 'update')
        self.assertEqual(op['expected_version'], 1)
        # 12 inherited retained verbatim + exactly 1 genuinely new entry.
        self.assertEqual(len(op['evidence']), 13)
        self.assertEqual(op['evidence'][:12], inherited)
        self.assertEqual(op['evidence'][12], _evidence(sid, 1, twelve))

        self.assertTrue(self.cards.validate_operations(plan['operations'], PROJECT)['ok'])
        approval = delta_contract.approve_review(ctx, plan, self._review(plan))
        result = self.cards.apply('batch-inherit', PROJECT, plan['operations'], approval)
        self.assertEqual(result['changed_ids'], [cid])

        card = self.cards.get(cid)
        self.assertEqual(card['version'], 2)
        self.assertEqual(len(card['evidence']), 13)
        self.assertEqual(card['evidence'][:12], inherited)
        self.assertEqual(card['evidence'][12], _evidence(sid, 1, twelve))
        # The pre-update provenance survives verbatim in history too.
        history = self.cards.history(cid)
        self.assertEqual(history[0]['old_version'], 1)
        self.assertEqual(history[0]['snapshot']['evidence'], inherited)

    def test_retire_still_capped_at_twelve_incoming(self):
        text = '\n\n'.join(f'事实{i}' for i in range(1, 14))
        sid = self._capture('evt-retire', text)
        card = self.cards.seed(PROJECT, 'retire-me', 'knowledge', '主题',
                               '待退休', '待退休正文。', evidence=[])
        cid = card['id']
        quotes = [f'事实{i}' for i in range(1, 14)]

        def _retire(evidence):
            return {
                'op': 'retire', 'id': cid, 'expected_version': 1,
                'evidence': evidence, 'reason': '该项已失效', 'basis_cards': [],
            }

        # RETIRE keeps the <=12 incoming budget: 12 is fine, 13 is rejected.
        self.assertTrue(self.cards.validate_operations(
            [_retire([_evidence(sid, 1, q) for q in quotes[:12]])], PROJECT)['ok'])
        verdict = self.cards.validate_operations(
            [_retire([_evidence(sid, 1, q) for q in quotes])], PROJECT)
        self.assertFalse(verdict['ok'])
        self.assertEqual(verdict['problems'][0]['field'], 'evidence')

    def test_create_keeps_twelve_entry_cap_unchanged(self):
        text = '\n\n'.join(f'条目{i}' for i in range(1, 14))
        sid = self._capture('evt-create', text)
        quotes = [f'条目{i}' for i in range(1, 14)]
        twelve = [_evidence(sid, 1, q) for q in quotes[:12]]
        thirteen = [_evidence(sid, 1, q) for q in quotes]
        op12 = {
            'op': 'create', 'key': 'k12', 'kind': 'memory', 'topic': 't', 'title': 'T12',
            'body': '十二条目正文。', 'state': 'active', 'evidence': twelve,
            'reason': '创建卡片', 'basis_cards': [],
        }
        self.assertTrue(self.cards.validate_operations([op12], PROJECT)['ok'])
        # A real apply of the 12-entry create succeeds with the true op hash.
        op_hash = hashlib.sha256(
            json.dumps([op12], ensure_ascii=False, sort_keys=True,
                       separators=(',', ':')).encode()).hexdigest()
        result = self.cards.apply('batch-create12', PROJECT, [op12],
                                  {'approved': True, 'findings': [],
                                   'proposal_hash': op_hash})
        self.assertEqual(self.cards.get(result['changed_ids'][0])['evidence'], twelve)

        op13 = dict(op12, key='k13', title='T13', body='十三条目正文。', evidence=thirteen)
        verdict = self.cards.validate_operations([op13], PROJECT)
        self.assertFalse(verdict['ok'])
        self.assertEqual(verdict['problems'][0]['field'], 'evidence')

    def test_more_than_twelve_genuinely_new_entries_rejected(self):
        text = '\n\n'.join(f'新增条目{i}' for i in range(1, 14))
        sid = self._capture('evt-new', text)
        card = self.cards.seed(PROJECT, 'grow-card', 'knowledge', '主题',
                               '增长卡', '增长卡正文。', evidence=[])
        cid = card['id']
        fresh13 = [_evidence(sid, 1, f'新增条目{i}') for i in range(1, 14)]
        op = {
            'op': 'update', 'id': cid, 'expected_version': 1, 'body': '更新后的正文。',
            'state': None, 'title': None, 'topic': None, 'evidence': fresh13,
            'reason': '新增十三条纯粹新证据', 'basis_cards': [],
        }
        verdict = self.cards.validate_operations([op], PROJECT)
        self.assertFalse(verdict['ok'])
        self.assertEqual(verdict['problems'][0]['field'], 'evidence')

    def test_quote_source_version_and_cas_still_reject(self):
        text = '\n\n'.join(f'校验条目{i}' for i in range(1, 5))
        sid = self._capture('evt-check', text)
        card = self.cards.seed(PROJECT, 'check-card', 'knowledge', '主题',
                               '校验卡', '校验卡正文。', evidence=[])
        cid = card['id']

        def _update(evidence, expected_version=1, body='更新后的校验正文。'):
            return {
                'op': 'update', 'id': cid, 'expected_version': expected_version,
                'body': body, 'state': None, 'title': None, 'topic': None,
                'evidence': evidence, 'reason': '校验分支', 'basis_cards': [],
            }

        # Non-exact quote is still rejected for a new entry.
        self.assertFalse(self.cards.validate_operations(
            [_update([_evidence(sid, 1, '不存在的引用文本')])], PROJECT)['ok'])
        # Unknown source id is still rejected.
        self.assertFalse(self.cards.validate_operations(
            [_update([_evidence('no-such-source', 1, '校验条目1')])], PROJECT)['ok'])
        # Wrong source version is still rejected.
        self.assertFalse(self.cards.validate_operations(
            [_update([_evidence(sid, 2, '校验条目1')])], PROJECT)['ok'])
        # CAS: a wrong expected_version is still rejected by apply.
        cas_op = _update([_evidence(sid, 1, '校验条目1')], expected_version=999)
        op_hash = hashlib.sha256(
            json.dumps([cas_op], ensure_ascii=False, sort_keys=True,
                       separators=(',', ':')).encode()).hexdigest()
        with self.assertRaises(ValueError):
            self.cards.apply('batch-cas', PROJECT, [cas_op],
                             {'approved': True, 'findings': [],
                              'proposal_hash': op_hash})

    def test_stale_inherited_provenance_is_still_rechecked(self):
        # An inherited tuple whose quote is no longer in the frozen source must
        # not silently ride along: every inherited entry is re-validated.
        text = '\n\n'.join(f'当前事实{i}' for i in range(1, 3))
        sid = self._capture('evt-stale', text)
        stale = [_evidence(sid, 1, '早已不存在的旧引用')]
        card = self.cards.seed(PROJECT, 'stale-card', 'knowledge', '主题',
                               '陈旧卡', '陈旧卡正文。', evidence=stale)
        cid = card['id']
        op = {
            'op': 'update', 'id': cid, 'expected_version': 1, 'body': '更新后的正文。',
            'state': None, 'title': None, 'topic': None,
            'evidence': stale + [_evidence(sid, 1, '当前事实1')],
            'reason': '保留旧证据并新增', 'basis_cards': [],
        }
        verdict = self.cards.validate_operations([op], PROJECT)
        self.assertFalse(verdict['ok'])
        self.assertEqual(verdict['problems'][0]['field'], 'evidence.quote')

    def test_ordinary_under_twelve_may_replace_stale_source_version_ref(self):
        # LEGACY semantics preserved: when the incoming evidence list is the
        # ordinary <=12 case it is the whole provenance, so an independently
        # reviewed correction may replace a stale source version ref. The old
        # row is not consulted and its now-stale tuple need not be retained.
        path = self.root / '材料.md'
        path.write_text('旧版本要求。', encoding='utf-8')
        first = self.engine.store.ingest(PROJECT, str(path))
        sid = first['source_id']
        self.assertEqual(first['version'], 1)
        card = self.cards.seed(PROJECT, 'legacy-replace', 'knowledge', '主题',
                               '旧引用卡', '旧引用卡正文。',
                               evidence=[_evidence(sid, 1, '旧版本要求。')])
        cid = card['id']
        # The source advances: the card's persisted tuple is now a stale ref.
        path.write_text('新版本要求。', encoding='utf-8')
        advanced = self.engine.store.ingest(PROJECT, str(path))
        self.assertEqual(advanced['version'], 2)
        op = {
            'op': 'update', 'id': cid, 'expected_version': 1,
            'body': '修订后的正文。', 'state': None, 'title': None, 'topic': None,
            'evidence': [_evidence(sid, 2, '新版本要求。')],
            'reason': '以新版本替换陈旧来源版本引用', 'basis_cards': [],
        }
        verdict = self.cards.validate_operations([op], PROJECT)
        self.assertTrue(verdict['ok'], verdict)
        op_hash = hashlib.sha256(
            json.dumps([op], ensure_ascii=False, sort_keys=True,
                       separators=(',', ':')).encode()).hexdigest()
        result = self.cards.apply('batch-legacy-replace', PROJECT, [op],
                                  {'approved': True, 'findings': [],
                                   'proposal_hash': op_hash})
        self.assertEqual(result['changed_ids'], [cid])
        self.assertEqual(self.cards.get(cid)['evidence'],
                         [_evidence(sid, 2, '新版本要求。')])

    def test_over_twelve_foreign_evidence_cannot_borrow_allowance(self):
        # A card already carries 12 valid inherited tuples. Presenting 13
        # entries that are NOT those inherited tuples at len > 12 must not
        # borrow any inheritance allowance: they fit neither the <=12
        # genuinely-new budget nor the retain-all-inherited requirement.
        own = '\n\n'.join(f'甲条目{i}' for i in range(1, 13))
        sid_own = self._capture('evt-own', own)
        inherited = [_evidence(sid_own, 1, f'甲条目{i}') for i in range(1, 13)]
        card = self.cards.seed(PROJECT, 'foreign-card', 'knowledge', '主题',
                               '外来卡', '外来卡正文。', evidence=inherited)
        cid = card['id']
        foreign_text = '\n\n'.join(f'乙外来条目{i}' for i in range(1, 15))
        sid_foreign = self._capture('evt-foreign', foreign_text)
        foreign = [_evidence(sid_foreign, 1, f'乙外来条目{i}')
                   for i in range(1, 14)]
        self.assertEqual(len(foreign), 13)
        self.assertEqual(set(tuple(sorted(e.items())) for e in foreign)
                         & set(tuple(sorted(e.items())) for e in inherited), set())
        op = {
            'op': 'update', 'id': cid, 'expected_version': 1,
            'body': '更新后的正文。', 'state': None, 'title': None, 'topic': None,
            'evidence': foreign, 'reason': '十三条外来新证据', 'basis_cards': [],
        }
        verdict = self.cards.validate_operations([op], PROJECT)
        self.assertFalse(verdict['ok'])
        self.assertEqual(verdict['problems'][0]['field'], 'evidence')


if __name__ == '__main__':
    unittest.main()
