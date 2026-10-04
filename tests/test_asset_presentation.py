import json,unittest
from pathlib import Path
import test_acceptance as base
from tongle.engine import Engine,BEGIN,END
from tongle.archive import export_archive,restore_archive
class AssetPresentation(unittest.TestCase):
 setUp=base.Acceptance.setUp
 tearDown=base.Acceptance.tearDown
 call=base.Acceptance.call
 source=base.Acceptance.source
 def note(self,e,source,key='first',title='中文可读标题'):
  return e.record('harbor',key,'candidate',source['source_id'],1,{'text':'保留原始记录。','quote':'保留原始记录。','title':title})
 def test_human_title_hidden_identity_and_collision_replay(self):
  s=self.source('harbor','保留原始记录。');e=Engine(self.home)
  one=self.note(e,s);two=self.note(e,s,'second')
  files=list(e.vault().rglob('*.md'));self.assertEqual(len(files),2)
  self.assertEqual({p.name for p in files},{'中文可读标题.md','中文可读标题（2）.md'})
  front=files[0].read_text().split('---',2)[1];self.assertNotIn('source_id',front)
  self.assertEqual(e._parse(files[0])[1],'保留原始记录。')
  self.note(e,s);self.assertEqual(len(list(e.vault().rglob('*.md'))),2)
 def test_renamed_note_stays_single_after_replay(self):
  s=self.source('harbor','保留原始记录。');e=Engine(self.home);self.note(e,s)
  p=next(e.vault().rglob('*.md'));new=p.with_name('我修改的标题.md');p.rename(new);e.sync();self.note(e,s)
  self.assertEqual(list(e.vault().rglob('*.md')),[new])
 def test_new_managed_root_restores_in_same_layout(self):
  s=self.source('harbor','保留原始记录。');e=Engine(self.home);e.db.execute("INSERT INTO meta VALUES('managed_dir','记忆')")
  self.note(e,s);backup=self.base/'backup.zip';export_archive(e,backup)
  restored=self.base/'restored';v=self.base/'restored-vault';restore_archive(restored,backup,v)
  self.assertTrue(list((v/'记忆').rglob('*.md')));self.assertFalse((v/'tongle').exists())
  self.assertEqual(len(Engine(restored).query('harbor','原始记录')['results']),1)
 def test_revoked_unpublished_record_does_not_reappear(self):
  s=self.source('harbor','保留原始记录。');e=Engine(self.home);r=self.note(e,s)
  p=next(e.vault().rglob('*.md'));p.unlink();e.db.execute('UPDATE records SET published=0 WHERE id=?',(r['id'],))
  e.source_state('harbor',s['source_id'],'revoked');e.sync();self.assertFalse(list(e.vault().rglob('*.md')))
 def test_legacy_note_still_parses(self):
  s=self.source('harbor','保留原始记录。');e=Engine(self.home);r=self.note(e,s)
  p=next(e.vault().rglob('*.md'))
  p.write_text('---\ntongle_id: '+json.dumps(r['id'])+'\nproject: "harbor"\nsource_id: '+json.dumps(s['source_id'])+'\nsource_version: 1\n---\n\n<!-- tongle:body -->\n保留原始记录。\n<!-- /tongle:body -->\n')
  self.assertEqual(e.sync()['conflicts'],0);self.assertEqual(len(e.query('harbor','原始记录')['results']),1)
 def test_malformed_hidden_metadata_is_not_accepted(self):
  s=self.source('harbor','保留原始记录。');e=Engine(self.home);self.note(e,s);p=next(e.vault().rglob('*.md'))
  p.write_text(p.read_text().replace('\n%%\n\n#','\n#',1))
  with self.assertRaises(ValueError):e._parse(p)

 def test_reinit_respects_migrated_root(self):
  e=Engine(self.home);old=e.vault();old.rmdir()
  e.db.execute("INSERT INTO meta VALUES('managed_dir','记忆')");e.init(self.base/'vault')
  self.assertTrue(e.vault().is_dir());self.assertFalse(old.exists())
 def test_hidden_identity_fields_are_validated(self):
  s=self.source('harbor','保留原始记录。');e=Engine(self.home);self.note(e,s);p=next(e.vault().rglob('*.md'));original=p.read_text()
  import re
  for value in ('[]','{}','null'):
   p.write_text(re.sub(r'tongle-meta: [^\n]+','tongle-meta: '+value,original))
   self.assertIn(str(p.relative_to(e.vault())),e.sync()['invalid_notes'])
