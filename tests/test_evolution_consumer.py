import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from tongle.engine import Engine
from tongle.evolution import Evolution
from tongle.knowledge_files import KnowledgeFiles

try:  # sibling import under ``unittest discover -s tests``
 from fact_review_stub import with_fact_audit
except ImportError:  # pragma: no cover - package-import invocation
 from tests.fact_review_stub import with_fact_audit

class EvolutionConsumerTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name).resolve()
  self.e=Engine(self.root/'state');self.e.init(self.root/'vault');self.addCleanup(self.e.store.close)
  self.cfg={'knowledge_mode':'evolving','model_command':['synthetic-stub'],'knowledge_quiet_seconds':0}
  self.write_cfg();self.ev=Evolution(self.e)
 def write_cfg(self): (self.e.home/'config.json').write_text(json.dumps(self.cfg))
 def capture(self,text,event='one',role='user',session='session',receipt_id=None):
  rec=self.e.store.capture('project',session,role,event,text);self.ev.captured(rec['source_id'],role,session,receipt_id=receipt_id);return rec
 def compile(self,payload):
  source=payload['sources'][-1];old=payload['existing']['cards'];text=source['text']
  if text=='知道了':return {'operations':[]}
  ref={'source_id':source['source_id'],'version':source['version'],'quote':text}
  if old:
   op={'op':'update','id':old[0]['id'],'expected_version':old[0]['version'],'body':text,'evidence':[ref],'reason':'用户纠正条件'}
  else:op={'op':'create','key':'交付边界','kind':'memory','topic':'方案设计','title':'当前交付范围','body':text,'evidence':[ref],'reason':'用户明确边界'}
  return {'operations':[op]}
 def model(self,command,prompt,timeout,env=None):
  payload=json.loads(prompt)
  if 'proposal' in payload:return {'approved':True,'findings':[]}, {'input':100,'output':60}
  return self.compile(payload),{'input':200,'output':80}
 def process(self):
  with patch('tongle.worker.invoke_json',side_effect=with_fact_audit(self.model)):return self.ev.process(force=True)
 def test_semantic_update_same_identity_and_cross_session_consumption(self):
  self.capture('先完成方案，实施暂缓。');self.assertEqual(self.process()['processed'],1)
  cid=self.ev.cards.list('project')[0]['id']
  self.capture('先完成方案；已确认的小型修复可以执行，其他实施仍暂缓。','two')
  self.assertEqual(self.process()['processed'],1);cards=self.ev.cards.list('project');self.assertEqual(len(cards),1);self.assertEqual(cards[0]['id'],cid)
  got=self.e.context('project','下一步做什么',session='new');self.assertIn('小型修复',got['results'][0]['text']);self.assertEqual(got['recent_statements'],[])
  self.assertEqual(self.e.context('foreign','下一步')['results'],[])
 def test_twenty_acknowledgements_do_not_create_extra_cards(self):
  self.capture('先完成方案。');self.process()
  for i in range(20):self.capture('知道了',str(i));self.process()
  self.assertEqual(len(self.ev.cards.list('project')),1);self.assertEqual(self.ev.cards.list('project')[0]['version'],1)
 def test_false_approval_and_budget_leave_assets_untouched(self):
  self.capture('先完成方案。')
  def reject(cmd,prompt,timeout,env=None):
   data=json.loads(prompt)
   if 'proposal' in data:return {'approved':False,'findings':['未保留条件']},{}
   return self.compile(data),{}
  with patch('tongle.worker.invoke_json',side_effect=with_fact_audit(reject)):self.assertEqual(self.ev.process(force=True)['failed'],1)
  self.assertEqual(self.ev.cards.list(),[])
  self.capture('实施暂缓。','second');self.cfg['knowledge_daily_call_budget']=2;self.write_cfg()
  with patch('tongle.worker.invoke_json') as call:
   result=self.ev.process(force=True);self.assertTrue(result['budget_wait']);call.assert_not_called()
 def test_source_change_and_revocation_stop_old_knowledge_use(self):
  p=self.root/'材料.md';p.write_text('先完成方案。');src=self.e.store.ingest('project',str(p));self.process()
  self.assertTrue(self.e.context('project','方案')['results']);p.write_text('撤回原来的方案要求。');self.e.store.ingest('project',str(p))
  self.assertEqual(self.e.context('project','方案')['results'],[]);self.process();self.assertTrue(self.e.context('project','方案')['results'])
  self.e.source_state('project',src['source_id'],'revoked');self.assertEqual(self.e.context('project','方案')['results'],[])
 def test_feedback_binds_actual_output_and_rejects_invented_evidence(self):
  self.capture('先完成方案，实施暂缓。');self.process();got=self.e.context('project','继续',session='s2');cid=got['results'][0]['id']
  output=self.capture('接下来完成方案。','answer','assistant','s2');row=dict(self.e.db.execute('SELECT * FROM knowledge_consumption WHERE id=?',(got['receipt_id'],)).fetchone())
  self.assertEqual(row['output_source'],output['source_id']);offered=self.ev._feedback_inputs({'project':'project','session':'s2'})
  self.ev._save_checks(offered,[{'receipt_id':got['receipt_id'],'evidence':[{'card_id':cid,'verdict':'applied','quote':'不存在的依据'}]}])
  checked=json.loads(self.e.db.execute('SELECT checks FROM knowledge_consumption WHERE id=?',(got['receipt_id'],)).fetchone()[0]);self.assertEqual(checked['evidence'],[])
 def test_long_document_parts_cover_every_character(self):
  text='前段。'*9000+'末尾例外：只适用于测试。';p=self.root/'长文.md';p.write_text(text);src=self.e.store.ingest('project',str(p));source=self.e.store.source(src['source_id'])
  parts=list(self.ev._source_parts(source));self.assertEqual(parts[0]['start'],0);self.assertEqual(parts[-1]['end'],len(text))
  for a,b in zip(parts,parts[1:]):self.assertEqual(a['end'],b['start'])
 def test_crash_after_proposal_reuses_saved_proposal(self):
  self.capture('先完成方案。');calls=[]
  def fail(cmd,prompt,timeout,env=None):
   data=json.loads(prompt);calls.append('verify' if 'proposal' in data else 'compile')
   if 'proposal' in data:raise RuntimeError('model timeout')
   return self.compile(data),{}
  with patch('tongle.worker.invoke_json',side_effect=with_fact_audit(fail)):self.ev.process(force=True)
  self.e.db.execute("UPDATE knowledge_work SET state='pending'");self.e.db.commit()
  with patch('tongle.worker.invoke_json',side_effect=with_fact_audit(self.model)) as model:self.assertEqual(self.ev.process()['processed'],1);self.assertEqual(model.call_count,1)

if __name__=='__main__':unittest.main()
