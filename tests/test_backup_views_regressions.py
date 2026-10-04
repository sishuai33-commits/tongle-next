"""Synthetic backup/navigation adversarial checks, without live assets."""
import json,tempfile,unittest
from pathlib import Path
from tongle.engine import Engine
from tongle.evolution import Evolution
from tongle.knowledge_files import KnowledgeFiles
from tongle.archive import export_archive,restore_archive
from tongle.views import refresh

class BackupViewsRegressionTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name).resolve()
  self.e=Engine(self.root/'state');self.e.init(self.root/'vault');self.addCleanup(self.e.store.close)
  (self.e.home/'config.json').write_text(json.dumps({'knowledge_mode':'evolving','library_excluded_projects':['isolated'],'model_command':['must-not-export']}))
  self.ev=Evolution(self.e)
 def seed(self,project='shared'):
  c=self.ev.cards.seed(project,'方案边界','knowledge','方案','方案边界','方案应保留完整依据。');KnowledgeFiles(self.e).publish();return c
 def test_restore_preserves_shared_consumption_exclusions(self):
  self.seed();self.assertEqual(self.e.context('isolated','方案')['results'],[])
  export_archive(self.e,self.root/'backup.zip');restore_archive(self.root/'restore',self.root/'backup.zip',self.root/'restore-vault')
  restored=Engine(self.root/'restore');self.addCleanup(restored.store.close)
  self.assertEqual(restored.context('isolated','方案')['results'],[])
 def test_manual_navigation_is_preserved(self):
  self.seed();home=self.root/'vault/首页.md';home.write_text('我的手工首页')
  got=refresh(self.e);self.assertIn('首页.md',got['manual_views_preserved']);self.assertEqual(home.read_text(),'我的手工首页')
 def test_edited_generated_navigation_is_preserved(self):
  self.seed();refresh(self.e);home=self.root/'vault/首页.md';home.write_text(home.read_text()+'\n人工补充')
  got=refresh(self.e);self.assertIn('首页.md',got['manual_views_preserved']);self.assertIn('人工补充',home.read_text())
 def test_missing_card_not_displayed_as_published(self):
  c=self.seed();c=self.ev.cards.get(c['id']);(self.root/'vault'/c['path']).unlink()
  refresh(self.e);page=self.root/'vault'/Path(c['path']).parent/'总览.md'
  self.assertIn('文件待同步',page.read_text())
 def test_navigation_symlink_does_not_overwrite_outside(self):
  self.seed();outside=self.root/'outside.md';outside.write_text('private')
  (self.root/'vault/首页.md').symlink_to(outside);got=refresh(self.e)
  self.assertIn('首页.md',got['manual_views_preserved']);self.assertEqual(outside.read_text(),'private')

if __name__=='__main__':unittest.main()
