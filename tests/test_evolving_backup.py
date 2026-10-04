import json,tempfile,unittest,zipfile,sqlite3
from pathlib import Path
from tongle.engine import Engine
from tongle.evolution import Evolution
from tongle.knowledge_files import KnowledgeFiles
from tongle.archive import export_archive,restore_archive

class EvolvingBackupTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.r=Path(self.tmp.name).resolve()
  self.e=Engine(self.r/'state');self.e.init(self.r/'vault');self.addCleanup(self.e.store.close);(self.e.home/'config.json').write_text(json.dumps({'knowledge_mode':'evolving','model_command':['must-not-export']}))
  self.ev=Evolution(self.e);self.card=self.ev.cards.seed('演示','边界','memory','测试','项目边界','先做原型，保留原件。');KnowledgeFiles(self.e).publish()
 def backup(self):
  p=self.r/'backup.zip';export_archive(self.e,p);return p
 def test_full_visible_assets_database_and_consumption_round_trip(self):
  p=self.r/'vault/资料/外部原文.txt';p.parent.mkdir(parents=True);p.write_text('原始文档\n不能丢失')
  q=self.r/'vault/随笔/复盘.md';q.parent.mkdir();q.write_text('人工笔记')
  result=restore_archive(self.r/'restored',self.backup(),self.r/'restored-vault');self.assertTrue(result['model_disabled'])
  e=Engine(self.r/'restored');self.addCleanup(e.store.close);got=e.context('演示','边界');self.assertIn('保留原件',got['results'][0]['text'])
  self.assertEqual((self.r/'restored-vault/资料/外部原文.txt').read_bytes(),p.read_bytes());self.assertEqual((self.r/'restored-vault/随笔/复盘.md').read_bytes(),q.read_bytes())
  self.assertEqual(json.loads((e.home/'config.json').read_text()),{'knowledge_mode':'evolving','library_excluded_projects':[],'knowledge_project_topics':{},'knowledge_project_names':{}});self.assertEqual(len(Evolution(e).cards.list()),1)
 def test_missing_user_deleted_card_is_not_resurrected(self):
  p=self.r/'vault'/self.ev.cards.get(self.card['id'])['path'];p.unlink();KnowledgeFiles(self.e).sync()
  restore_archive(self.r/'restored',self.backup(),self.r/'restored-vault');self.assertFalse((self.r/'restored-vault'/self.ev.cards.get(self.card['id'])['path']).exists())
 def test_symlink_source_is_refused_not_silently_dropped(self):
  (self.r/'vault/资料').mkdir();p=self.r/'secret.txt';p.write_text('private');(self.r/'vault/资料/链接.txt').symlink_to(p)
  with self.assertRaises(ValueError):self.backup()
 def test_unsafe_database_path_rejected_before_destination_created(self):
  self.e.db.execute("UPDATE knowledge_cards SET path='../越界.md'");self.e.db.commit()
  # Serialize a malformed DB as an attacker could; zip file hashes are not trust.
  db=self.r/'bad.sqlite3';other=sqlite3.connect(db);self.e.db.backup(other);other.close()
  import hashlib
  b=db.read_bytes();z=self.r/'bad.zip'
  with zipfile.ZipFile(z,'w') as f:f.writestr('state.sqlite3',b);f.writestr('manifest.json',json.dumps({'format':2,'schema':1,'files':{'state.sqlite3':hashlib.sha256(b).hexdigest()}}))
  with self.assertRaises(ValueError):restore_archive(self.r/'reject',z,self.r/'reject-vault')
  self.assertFalse((self.r/'reject').exists())

if __name__=='__main__':unittest.main()
