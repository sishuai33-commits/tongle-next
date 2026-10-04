import json,unittest
from pathlib import Path
import test_acceptance as base
from tongle.engine import Engine
from tongle.library import search
class ChineseLayout(unittest.TestCase):
 setUp=base.Acceptance.setUp
 tearDown=base.Acceptance.tearDown
 call=base.Acceptance.call
 source=base.Acceptance.source
 def test_labels_preserve_project_identity(self):
  s=self.source('harbor','保留中文目录。');e=Engine(self.home)
  e.db.execute("INSERT INTO meta VALUES('project_labels',?)",(json.dumps({'harbor':'日常工作'}),))
  e.record('harbor','label','candidate',s['source_id'],1,{'text':'保留中文目录。','quote':'保留中文目录。'})
  self.assertTrue(list((e.vault()/'日常工作').glob('*.md')));self.assertEqual(len(e.query('harbor','中文')['results']),1);self.assertEqual(e.query('日常工作','中文')['results'],[])
 def test_chinese_library_and_excluded_archive(self):
  v=self.base/'library';(v/'知识/知识卡片').mkdir(parents=True);(v/'归档').mkdir()
  (v/'知识/知识卡片/证据.md').write_text('中文证据保留');(v/'归档/旧证据.md').write_text('中文证据保留')
  (Path(self.home)/'config.json').write_text(json.dumps({'library_vault':str(v),'library_dir':'知识'}))
  found=search(self.home,'中文证据');self.assertEqual(len(found),1);self.assertIn('知识/知识卡片',found[0]['source_id'])
 def test_invalid_library_path_cannot_escape(self):
  (Path(self.home)/'config.json').write_text(json.dumps({'library_vault':str(self.base),'library_dir':'../other'}))
  with self.assertRaises(ValueError):search(self.home,'中文')
