import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from tongle.engine import Engine
from tongle.knowledge_store import KnowledgeStore
from tongle.knowledge_files import KnowledgeFiles

class LinkMoveTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name).resolve()
  self.e=Engine(self.root/'state');self.e.init(self.root/'vault');self.addCleanup(self.e.store.close)
  self.cards=KnowledgeStore(self.e);self.files=KnowledgeFiles(self.e,self.cards)
  self.a=self.cards.seed('shared','原卡','knowledge','主题','原卡','完整来源和条件。');self.files.publish();self.old=self.cards.get(self.a['id'])['path']
 def reference(self,body):
  c=self.cards.seed('shared','引用卡','knowledge','主题','引用卡',body);self.files.publish();return c
 def move(self):
  self.new='知识/新主题/新名字.md';p=self.root/'vault'/self.new;p.parent.mkdir(parents=True);(self.root/'vault'/self.old).rename(p)
 def test_links_alias_anchor_codes_history_and_idempotence(self):
  old=self.old[:-3];body=f'正文 [[{old}#章节|别名]] [[原卡]] [[{self.old}]]\n`[[{old}]]`\n```md\n[[{old}]]\n```\n    [[{old}]]\n<!-- [[{old}]] -->'
  c=self.reference(body);self.move();r=self.files.sync();new=self.cards.get(c['id'])
  self.assertEqual(new['version'],2);self.assertEqual(new['manually_edited'],False)
  self.assertIn('[[知识/新主题/新名字#章节|别名]]',new['body']);self.assertIn('[[知识/新主题/新名字]]',new['body'])
  self.assertIn('[[知识/新主题/新名字.md]]',new['body']);self.assertIn(f'`[[{old}]]`',new['body']);self.assertIn(f'```md\n[[{old}]]\n```',new['body']);self.assertIn(f'    [[{old}]]',new['body'])
  self.assertEqual(self.cards.history(c['id'])[-1]['reason'],'link_path_repair')
  self.files.publish();self.assertEqual(self.files.sync()['changed'],0);self.assertEqual(self.cards.get(c['id'])['version'],2)
 def test_ambiguous_bare_name_not_rewritten_but_full_path_is(self):
  c=self.reference(f'[[原卡]] [[{self.old[:-3]}]]');other=self.root/'vault/随笔/原卡.md';other.parent.mkdir();other.write_text('unbound')
  self.move();self.files.sync();body=self.cards.get(c['id'])['body'];self.assertIn('[[原卡]]',body);self.assertIn('[[知识/新主题/新名字]]',body)
 def test_manual_change_same_sync_is_preserved_then_repaired_after_publish(self):
  c=self.reference(f'[[{self.old[:-3]}]]');p=self.root/'vault'/self.cards.get(c['id'])['path'];p.write_text(p.read_text().replace(']]',']] 人工补充',1));self.move()
  self.files.sync();self.assertIn('人工补充',p.read_text());self.assertNotIn('新名字',self.cards.get(c['id'])['body'])
  self.files.publish();self.files.sync();self.files.publish();self.assertIn('人工补充',p.read_text());self.assertIn('新名字',p.read_text())
 def test_unbound_and_archived_originals_untouched(self):
  body=f'[[{self.old[:-3]}]]';p=self.root/'vault/随笔/原始.md';p.parent.mkdir();p.write_text(body)
  c=self.reference(body);archive='归档/测试/引用历史.md';self.cards.set_path(c['id'],1,archive)
  prior=self.root/'vault'/self.files._tracking(c['id'])['path'];dest=self.root/'vault'/archive;dest.parent.mkdir(parents=True);prior.rename(dest);self.files.sync()
  self.move();self.files.sync();self.files.publish();self.assertEqual(p.read_text(),body);self.assertIn(body,dest.read_text())
 def test_edit_between_sync_and_publish_not_overwritten(self):
  c=self.reference(f'[[{self.old[:-3]}]]');self.move();self.files.sync();p=self.root/'vault'/self.cards.get(c['id'])['path'];p.write_text(p.read_text()+'\n人工并发追加')
  got=self.files.publish();self.assertIn(c['id'],got['conflicts']);self.assertIn('人工并发追加',p.read_text())
 def test_complex_code_and_frontmatter_are_opaque(self):
  old=self.old[:-3];body=f'---\nexample: "[[{old}]]"\n---\n``a ` [[{old}]] b``\n> ```\n> [[{old}]]\n> ```\n正文 [[{old}]]'
  c=self.reference(body);self.move();self.files.sync();new=self.cards.get(c['id'])['body']
  self.assertIn(f'example: "[[{old}]]"',new);self.assertIn(f'``a ` [[{old}]] b``',new);self.assertIn(f'> [[{old}]]',new);self.assertIn('正文 [[知识/新主题/新名字]]',new)
 def test_multiple_moves_keep_old_complete_path_receipts(self):
  c=self.reference(f'[[{self.old[:-3]}]]');self.move();self.files.sync();self.files.publish()
  final='知识/新主题/最终名称.md';(self.root/'vault'/self.new).rename(self.root/'vault'/final)
  self.files.sync();self.files.publish();self.assertIn('[[知识/新主题/最终名称]]',self.cards.get(c['id'])['body']);self.assertEqual(self.cards.get(c['id'])['version'],3)
 def test_occupied_old_path_not_redirected(self):
  c=self.reference(f'[[{self.old[:-3]}]]');self.move();(self.root/'vault'/self.old).write_text('another document')
  self.files.sync();self.assertNotIn('新名字',self.cards.get(c['id'])['body'])

if __name__=='__main__':unittest.main()
