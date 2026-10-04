import json,tempfile,unittest
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'adapters'))
from production import resolve
from tongle.library import search
class Production(unittest.TestCase):
 def test_project_aliases_and_unknown_isolation(self):
  with tempfile.TemporaryDirectory() as d:
   b=Path(d);c=b/'active.json';c.write_text(json.dumps({'enabled':True,'projects':{str(b/'old'):'same',str(b/'new'):'same'}}))
   self.assertEqual(resolve(b/'old/sub',c)['project'],resolve(b/'new',c)['project'])
   self.assertNotEqual(resolve(b/'a',c)['project'],resolve(b/'b',c)['project'])
   (b/'new').mkdir();(b/'new/.tongle-next.json').write_text('{"project":"explicit"}')
   self.assertEqual(resolve(b/'new/sub',c)['project'],'explicit')
 def test_copied_card_read_edit_delete_and_archive(self):
  with tempfile.TemporaryDirectory() as d:
   b=Path(d);w=b/'vault/wiki';w.mkdir(parents=True);p=w/'缓存规则.md';p.write_text('缓存只作为历史参考。')
   (b/'config.json').write_text(json.dumps({'library_vault':str(b/'vault')}))
   r=search(b,'缓存');self.assertEqual(r[0]['status'],'legacy_unverified');version=r[0]['source_version']
   p.write_text('缓存必须标明采样时间。');self.assertNotEqual(search(b,'缓存')[0]['source_version'],version)
   (w/'_archived').mkdir();p.rename(w/'_archived/旧缓存.md');self.assertEqual(search(b,'缓存'),[])
 def test_recent_correction_survives_large_library_context(self):
  from common import render_context_result
  data={'results':[{'text':'旧结论'*5000,'status':'legacy_unverified'}],
        'recent_statements':[{'text':'最新决定：只做两张流程图。','role':'user','status':'user_statement'}]}
  rendered,error=render_context_result(json.dumps(data))
  self.assertIsNone(error);self.assertIn('只做两张',rendered[:5000])
 def test_bad_library_config_does_not_break_context(self):
  with tempfile.TemporaryDirectory() as d:
   (Path(d)/'config.json').write_text('not-json')
   self.assertEqual(search(d,'缓存'),[])
