"""Codex acceptance probes: actual subprocess boundaries and ledger semantics."""
import json,os,sys,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from tongle.engine import Engine
from tongle.evolution import Evolution,canonical
from tongle.model_errors import ModelError
from tongle.worker import invoke_json
from tongle.views import status_snapshot,refresh
ROOT=Path(__file__).resolve().parents[1]
ZERO=dict(input=0,output=0,cacheRead=0,cacheWrite=0)
class IndependentRuntime(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
 def runner(self,messages,exit_code=0,stderr=None):
  shim=self.root/'pi';shim.write_text('#!'+sys.executable+'\nimport json,sys\n'+''.join('print('+repr(json.dumps({'type':'message_end','message':m}))+')\n' for m in messages)+('print('+repr(json.dumps(stderr))+',file=sys.stderr)\n' if stderr else '')+'sys.exit('+str(exit_code)+')\n');shim.chmod(0o700)
  return invoke_json([sys.executable,str(ROOT/'scripts/pi_model.py'),'--pi',str(shim)],'{}',8)
 def test_length_valid_json_never_accepted(self):
  m=dict(role='assistant',stopReason='length',content=[{'type':'text','text':'{}'}],usage=dict(ZERO,output=3))
  with self.assertRaises(ModelError) as got:self.runner([m])
  self.assertEqual((got.exception.category,got.exception.stop_reason),('output_truncated','length'));self.assertEqual(got.exception.usage['output'],3)
 def test_zero_provider_error_remains_unknown_and_classified(self):
  m=dict(role='assistant',stopReason='error',content=[],usage=ZERO,errorMessage='429 rate limit SECRET_CANARY')
  with self.assertRaises(ModelError) as got:self.runner([m])
  self.assertEqual(got.exception.code,'rate_limited');self.assertEqual(got.exception.usage,{});self.assertNotIn('SECRET',str(got.exception))
 def test_real_runner_guard_exit_known_zero(self):
  with self.assertRaises(ModelError) as got:self.runner([],7,{'code':'input_too_large','bytes_actual':20,'bytes_limit':1})
  self.assertEqual(got.exception.category,'input_limited');self.assertEqual(got.exception.usage,ZERO)
 def test_multiple_message_missing_cache_not_complete(self):
  first=dict(role='assistant',stopReason='stop',content=[],usage={'input':3,'output':2})
  second=dict(role='assistant',stopReason='stop',content=[{'type':'text','text':'{}'}],usage=dict(ZERO,input=2))
  _,usage=self.runner([first,second]);self.assertEqual(usage['input'],5);self.assertNotIn('cacheRead',usage)
 def test_diagnostic_without_usage_does_not_clear_observation(self):
  from tongle.worker import err_fields
  log=json.dumps({'usage':dict(ZERO,input=20)})+'\n'+json.dumps({'category':'timeout','code':'timeout'})
  self.assertEqual(err_fields(log)[4],dict(ZERO,input=20))
  self.assertEqual(err_fields(log+'\n'+json.dumps({'usage':{}}))[4],{})
 def engine(self):
  e=Engine(self.root/'home');e.init(self.root/'vault');self.addCleanup(e.store.close);v=Evolution(e);return e,v
 def test_escape_guard_uses_fixed_cap_and_local_rejection_releases(self):
  from tongle.evidence_selection import DEFAULT_MAX_INPUT_BYTES
  e,v=self.engine();p={'text':'\\"'*10000};cfg={'model_command':['stub']}
  with patch('tongle.worker.invoke_json',return_value=({},dict(ZERO,input=3))) as call:
   v._call({'id':'one'},'compile',p,cfg)
  cap=int(call.call_args.kwargs['env']['TONGLE_MODEL_MAX_INPUT_BYTES'])
  # The cap is the fixed default and never self-scales from the request size.
  self.assertEqual(cap,DEFAULT_MAX_INPUT_BYTES)
  with patch('tongle.worker.invoke_json',return_value=({},dict(ZERO,input=3))) as call:
   v._call({'id':'big'},'compile',p,dict(cfg,knowledge_max_input_bytes=900000))
  self.assertEqual(int(call.call_args.kwargs['env']['TONGLE_MODEL_MAX_INPUT_BYTES']),DEFAULT_MAX_INPUT_BYTES)
  with patch('tongle.worker.invoke_json') as call:
   with self.assertRaises(ModelError):v._call({'id':'two'},'compile',p,dict(cfg,knowledge_max_input_bytes=1))
   call.assert_not_called()
  row=e.db.execute("select actual_input,actual_output from knowledge_calls where work_id='two'").fetchone();self.assertEqual(tuple(row),(0,0))
 def test_status_budget_wait_oldest_daily_window_and_unknown(self):
  e,v=self.engine();now=time.time();day=time.strftime('%Y-%m-%d',time.gmtime(now))
  for cid,date,elapsed,error,ai,ao in [('today',day,1.,'timeout',None,None),('running',day,None,None,None,None),('old','2000-01-01',1.,None,1,1)]:
   e.db.execute('insert into knowledge_calls(id,work_id,stage,day,reserved,actual_input,actual_output,elapsed,error,created_at) values(?,?,?,?,?,?,?,?,?,?)',(cid,'w','compile',date,200,ai,ao,elapsed,error,now))
  for wid,state in [('w','budget_wait'),('o','obsolete'),('p','publication_pending')]:
   e.db.execute('insert into knowledge_work(id,project,session,inputs,state,created_at,next_at) values(?,?,?,?,?,?,?)',(wid,'p','s','[]',state,now-7200,now+600))
  snap=status_snapshot(e,{'active':0,'conflict':0});self.assertEqual(snap['calls'],2);self.assertEqual(snap['error_rate'],100.);self.assertEqual(snap['in_flight'],1);self.assertEqual(snap['reserved_tokens'],400);self.assertEqual(snap['pending'],1);self.assertGreaterEqual(snap['oldest_age'],7200);self.assertIsNotNone(snap['next_retry'])
  e.db.execute("insert into knowledge_work(id,project,session,inputs,state,created_at,error) values('fail','p','s','[]','failed',?,'SECRET_CANARY')",(now-100000,))
  snap2=status_snapshot(e,{'active':0,'conflict':0});self.assertGreaterEqual(snap2['oldest_age'],7200);self.assertLess(snap2['oldest_age'],9000)
  refresh(e);page=(self.root/'vault/运行情况.md').read_text();self.assertIn('已停止自动重试',page);self.assertNotIn('模型处理失败',page);self.assertNotIn('达到本轮处理上限',page);self.assertNotIn('SECRET_CANARY',page);self.assertIn('400 tokens',page)
if __name__=='__main__':unittest.main()
