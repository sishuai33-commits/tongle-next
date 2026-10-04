import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tongle.engine import Engine
from tongle.knowledge_store import KnowledgeStore


class KnowledgeStoreConsumerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()
        self.engine = Engine(root / 'state'); self.addCleanup(self.engine.store.close)
        self.engine.init(root / 'vault'); self.cards = KnowledgeStore(self.engine)
        self.source = self.engine.store.capture('demo', 'one', 'user', 'one', '先交付设计方案，不进入实施；例外是已确认的小型修复。')
        self.evidence = [{'source_id': self.source['source_id'], 'version': 1,
                          'quote': '先交付设计方案，不进入实施'}]

    def review(self, ops):
        h = hashlib.sha256(json.dumps(ops, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()
        return {'approved': True, 'proposal_hash': h, 'findings': []}

    def create(self, key='交付边界'):
        return {'op': 'create', 'key': key, 'kind': 'memory', 'topic': '方案设计',
                'title': '当前交付边界', 'body': '本期先完成设计方案，暂不进入实施。',
                'reason': '用户明确范围', 'evidence': self.evidence}

    def apply(self, bid, ops):
        return self.cards.apply(bid, 'demo', ops, self.review(ops))

    def test_second_source_updates_same_identity(self):
        receipt = self.apply('first', [self.create()]); cid = receipt['changed_ids'][0]
        src = self.engine.store.capture('demo', 'two', 'user', 'two', '现在允许已确认的小型修复，其他实施仍不做。')
        ops = [{'op': 'update', 'id': cid, 'expected_version': 1,
                'body': '本期先做设计；已确认的小型修复可以实施，其余仍暂缓。',
                'reason': '补充例外', 'evidence': [{'source_id': src['source_id'],
                'version': 1, 'quote': '现在允许已确认的小型修复，其他实施仍不做。'}]}]
        self.apply('second', ops)
        self.assertEqual(len(self.cards.list('demo')), 1)
        self.assertEqual(self.cards.get(cid)['version'], 2)
        self.assertEqual(len(self.cards.history(cid)), 1)

    def test_review_must_match_proposal(self):
        ops = [self.create()]; review = self.review(ops); ops[0]['body'] = '已完成实施。'
        with self.assertRaises(ValueError): self.cards.apply('bad', 'demo', ops, review)
        self.assertEqual(self.cards.list('demo'), [])

    def test_invalid_second_operation_cannot_partially_write(self):
        first = self.create(); second = self.create('另一条'); second['body'] = '另一个判断'
        second['evidence'] = [dict(self.evidence[0], quote='原文中不存在')]
        with self.assertRaises(ValueError): self.apply('bad', [first, second])
        self.assertEqual(self.cards.list('demo'), [])

    def test_noop_batch_and_exact_replay_create_no_extra_card(self):
        self.assertEqual(self.apply('empty', [])['changed_ids'], [])
        ops = [self.create()]; first = self.apply('same', ops); replay = self.apply('same', ops)
        self.assertTrue(replay['replayed']); self.assertEqual(first['changed_ids'], replay['changed_ids'])
        self.assertEqual(len(self.cards.list('demo')), 1)
        with self.assertRaises(ValueError): self.apply('same', [])

    def test_manual_edit_and_old_batch_replay_preserve_human_text(self):
        ops = [self.create()]; cid = self.apply('first', ops)['changed_ids'][0]
        self.cards.manual_edit(cid, 1, '人工补充：本期范围以已确认方案为准。')
        self.apply('first', ops)
        update = {'op': 'update', 'id': cid, 'expected_version': 2, 'body': '模型改写',
                  'evidence': self.evidence, 'reason': '自动修订'}
        with self.assertRaises(ValueError): self.apply('overwrite', [update])
        self.assertIn('人工补充', self.cards.get(cid)['body'])

    def test_stale_version_cross_project_and_false_review_rejected(self):
        cid = self.apply('first', [self.create()])['changed_ids'][0]
        update = {'op': 'update', 'id': cid, 'expected_version': 2, 'body': '修订',
                  'reason': '修改', 'evidence': self.evidence}
        with self.assertRaises(ValueError): self.apply('stale', [update])
        with self.assertRaises(ValueError): self.cards.apply('foreign', 'other', [self.create()], self.review([self.create()]))
        with self.assertRaises(ValueError): self.cards.apply('false', 'demo', [], {'approved': False})
        self.assertEqual(self.cards.get(cid)['version'], 1)

    def test_duplicate_new_key_and_normalized_body_are_rejected(self):
        self.apply('first', [self.create()])
        with self.assertRaises(ValueError): self.apply('duplicate', [self.create()])
        other = self.create('同义原文'); other['body'] += '\n'
        with self.assertRaises(ValueError): self.apply('duplicate-body', [other])

    def test_retirement_retains_content_and_history(self):
        cid = self.apply('first', [self.create()])['changed_ids'][0]
        self.apply('retire', [{'op': 'retire', 'id': cid, 'expected_version': 1,
                              'evidence': self.evidence, 'reason': '范围已替代'}])
        self.assertEqual(self.cards.list('demo'), [])
        self.assertEqual(self.cards.get(cid)['state'], 'retired')
        self.assertIn('设计方案', self.cards.get(cid)['body'])
        self.assertTrue(self.cards.history(cid))

    def test_local_path_binding_has_no_identity_or_version_change(self):
        a = self.cards.seed('shared', '甲', 'knowledge', '测试', '甲', '甲的内容')
        b = self.cards.seed('shared', '乙', 'knowledge', '测试', '乙', '乙的内容')
        self.cards.set_path(a['id'], 1, '知识/测试/甲.md')
        self.assertEqual(self.cards.get(a['id'])['version'], 1)
        with self.assertRaises(ValueError): self.cards.set_path(b['id'], 1, '知识/测试/甲.md')
        for path in ('../甲.md', '知识/../甲.md', '/tmp/甲.md', '.隐藏/甲.md'):
            with self.subTest(path=path), self.assertRaises(ValueError): self.cards.set_path(b['id'], 1, path)


if __name__ == '__main__': unittest.main()
