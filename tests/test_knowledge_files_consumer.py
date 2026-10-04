import tempfile,unittest,hashlib,json
from pathlib import Path
from unittest.mock import patch
from tongle.engine import Engine
from tongle.knowledge_store import KnowledgeStore
from tongle.knowledge_files import KnowledgeFiles,_parse_wrapper

class PublicationTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name).resolve()
  self.e=Engine(self.root/'state');self.addCleanup(self.e.store.close);self.e.init(self.root/'vault')
  self.k=KnowledgeStore(self.e);self.f=KnowledgeFiles(self.e,self.k)
  self.c=self.k.seed('示例项目','边界','memory','项目协作','当前边界','---\n标签: 项目\n---\n先做设计，实施等待确认。\n')
 def file(self):return self.root/'vault'/self.k.get(self.c['id'])['path']
 def test_publish_preserves_yaml_and_unchanged_identity(self):
  self.assertEqual(self.f.publish()['published'],1);text=self.file().read_text()
  self.assertTrue(text.startswith('---\n标签:'));self.assertIn('项目/示例项目/',self.file().as_posix());self.assertEqual(_parse_wrapper(text),(self.c['id'],1))
  self.assertEqual(self.f.publish()['unchanged'],1);self.assertEqual(self.f.sync()['changed'],0)
 def test_edit_is_kept_across_sync_publish_and_replay(self):
  self.f.publish();p=self.file();p.write_text(p.read_text().replace('先做设计','人工改成先做原型'))
  self.assertEqual(self.f.sync()['changed'],1);self.assertTrue(self.k.get(self.c['id'])['manually_edited'])
  self.assertEqual(self.f.publish()['published'],1);self.assertIn('人工改成先做原型',p.read_text())
  self.assertEqual(_parse_wrapper(p.read_text()),(self.c['id'],2));self.assertEqual(self.f.sync()['changed'],0)
 def test_missing_is_not_resurrected(self):
  self.f.publish();p=self.file();p.unlink();self.f.sync();self.assertIn(self.c['id'],self.f.publish()['missing']);self.assertFalse(p.exists())
 def test_rename_preserves_id_and_does_not_duplicate(self):
  self.f.publish();p=self.file();q=p.with_name('改名后的边界.md');p.rename(q)
  self.assertEqual(self.f.sync()['moved'],1);self.assertEqual(self.file(),q);self.assertFalse(self.k.get(self.c['id'])['manually_edited']);self.f.publish();self.assertFalse(p.exists())
 def test_duplicate_and_tampered_identity_cannot_be_consumed(self):
  self.f.publish();p=self.file();p.with_name('复制.md').write_text(p.read_text());self.assertIn(self.c['id'],self.f.sync()['conflicts'])
  self.assertEqual(self.f._tracking(self.c['id'])['state'],'conflict');self.assertEqual(self.f.publish()['published'],0)
  p.with_name('复制.md').unlink();p.write_text(p.read_text().replace('"version":1','"version":7'));self.assertIn(self.c['id'],self.f.sync()['conflicts'])
 def test_crash_after_write_recovers_without_new_card(self):
  with patch.object(self.f,'_commit_receipt',side_effect=RuntimeError('power loss')):
   with self.assertRaises(RuntimeError):self.f.publish()
  self.assertTrue(self.file().exists());self.assertEqual(self.e.db.execute('SELECT COUNT(*) FROM knowledge_publications').fetchone()[0],1)
  self.assertEqual(self.f.recover(),[]);self.assertEqual(self.f.publish()['unchanged'],1)
 def test_crash_before_write_recovers_exclusive_new_path(self):
  with patch.object(self.f,'_write_atomic',side_effect=RuntimeError('power loss')):
   with self.assertRaises(RuntimeError):self.f.publish()
  self.assertFalse(self.file().exists());self.assertEqual(self.f.recover(),[]);self.assertTrue(self.file().exists())
 def test_mid_write_manual_change_not_overwritten(self):
  self.f.publish();p=self.file();self.k.manual_edit(self.c['id'],1,'模型还没看到的人工正文')
  original=self.f._write_atomic
  def race(target,payload,exclusive,expected_hash=None):
   target.write_text('用户正在修改的文字');return original(target,payload,exclusive,expected_hash)
  with patch.object(self.f,'_write_atomic',side_effect=race):self.assertIn(self.c['id'],self.f.publish()['conflicts'])
  self.assertEqual(p.read_text(),'用户正在修改的文字');self.assertIn(self.c['id'],self.f.recover())
 def test_crlf_only_sync_is_unchanged_and_version_stable(self):
  self.f.publish();p=self.file();p.write_bytes(p.read_bytes().replace(b'\n',b'\r\n'))
  r=self.f.sync();self.assertEqual(r['changed'],0);self.assertEqual(r['conflicts'],[])
  self.assertEqual(self.k.get(self.c['id'])['version'],1)
  self.assertEqual(_parse_wrapper(self.file().read_text()),(self.c['id'],1))
 def test_crlf_duplicate_copy_still_conflicts(self):
  self.f.publish();p=self.file();p.with_name('复制.md').write_bytes(p.read_bytes().replace(b'\n',b'\r\n'))
  self.assertIn(self.c['id'],self.f.sync()['conflicts'])
 def test_real_manual_change_saved_crlf_syncs_and_publishes(self):
  self.f.publish();p=self.file()
  p.write_bytes(p.read_bytes().replace('先做设计'.encode(),'人工改成先做原型'.encode()).replace(b'\n',b'\r\n'))
  r=self.f.sync();self.assertEqual(r['changed'],1);self.assertEqual(r['conflicts'],[])
  self.assertTrue(self.k.get(self.c['id'])['manually_edited'])
  self.assertEqual(self.k.get(self.c['id'])['version'],2)
  self.f.publish();data=p.read_bytes()
  self.assertTrue(data.startswith('---\r\n标签:'.encode()))
  self.assertIn('人工改成先做原型'.encode(),data)
  text=data.decode('utf-8')
  self.assertEqual(_parse_wrapper(text),(self.c['id'],2))
  self.assertEqual(text.count('tongle-card:'),1)
 def test_adopt_and_symlink_target(self):
  p=self.root/'vault'/'项目'/'示例项目'/'已有正文.md';p.parent.mkdir(parents=True);p.write_text(self.c['body'])
  self.f.adopt(self.c['id'],p.relative_to(self.f.root),hashlib.sha256(p.read_bytes()).hexdigest())
  self.f.publish();self.assertIn(self.c['body'].strip(),p.read_text().replace('# 当前边界\n\n',''))
  p.unlink();other=self.root/'outside.md';other.write_text('私人原件');p.symlink_to(other)
  self.assertIn(self.c['id'],self.f.sync()['conflicts']);self.assertEqual(other.read_text(),'私人原件')

if __name__=='__main__':unittest.main()
