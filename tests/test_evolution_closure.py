import json
from pathlib import Path
import test_evolution_consumer as base
import unittest
from tongle.evolution import fingerprint
from unittest.mock import patch
from tongle.knowledge_files import KnowledgeFiles
from tongle.views import refresh
try:  # sibling-tests import under ``unittest discover -s tests``
 from fact_review_stub import with_fact_audit
except ImportError:  # pragma: no cover - package-import invocation
 from tests.fact_review_stub import with_fact_audit

class ClosureTests(unittest.TestCase):
 setUp=base.EvolutionConsumerTests.setUp
 write_cfg=base.EvolutionConsumerTests.write_cfg
 capture=base.EvolutionConsumerTests.capture
 compile=base.EvolutionConsumerTests.compile
 model=base.EvolutionConsumerTests.model
 process=base.EvolutionConsumerTests.process
 def test_explicit_pending_correction_temporarily_withholds_old_state(self):
  self.capture('本次先完成两张方案图。');self.process()
  self.capture('更正：改为三张方案图，仍不开发接口。','correction')
  got=self.e.context('project','这次几张',session='new');self.assertEqual(got['results'],[]);self.assertIn('三张',got['notices'][0]);self.assertIn('尚未',got['notices'][0])
  self.process();got=self.e.context('project','几张');self.assertEqual(got['notices'],[]);self.assertIn('三张',got['results'][0]['text'])
 def test_punctuation_only_source_update_does_not_withhold_unchanged_meaning(self):
  p=self.root/'材料.md';p.write_text('先完成方案，实施暂缓。');self.e.store.ingest('project',str(p));self.process()
  p.write_text('先完成方案；实施暂缓。');self.e.store.ingest('project',str(p));self.assertTrue(self.e.context('project','方案')['results'])
 def test_verified_misapplication_is_staged_once_before_evaluation(self):
  self.capture('先完成方案，实施暂缓。');self.process();got=self.e.context('project','继续',session='s2');cid=got['results'][0]['id']
  self.capture('接下来立即开发接口。','answer','assistant','s2',receipt_id=got['receipt_id']);offered=self.ev._feedback_inputs({'project':'project','session':'s2'})
  checks=[{'receipt_id':got['receipt_id'],'evidence':[{'card_id':cid,'verdict':'misapplied','quote':'立即开发接口','reason':'违反实施暂缓'}]}]
  self.ev._save_checks(offered,checks);self.ev._save_checks(offered,checks)
  self.assertEqual(self.e.db.execute("SELECT COUNT(*) FROM sources WHERE session='knowledge-feedback'").fetchone()[0],0)
  self.ev.queue(force=True);self.assertEqual(self.e.db.execute("SELECT COUNT(*) FROM knowledge_work WHERE session='knowledge-feedback'").fetchone()[0],0)
  self.assertEqual(self.e.db.execute("SELECT state FROM knowledge_feedback").fetchone()[0],'candidate')
 def test_navigation_contains_real_guide_and_archives(self):
  self.capture('先完成方案。');self.process();refresh(self.e)
  for rel in ['首页.md','使用说明.md','随笔/总览.md','归档/总览.md']:self.assertTrue((self.root/'vault'/rel).is_file())
 def test_human_edit_can_evolve_after_fresh_preservation_review_but_not_stale_review(self):
  self.capture('本期先完成方案。');self.process();k=self.ev.cards;c=k.list('project')[0]
  c=k.manual_edit(c['id'],1,'本期先完成方案。人工补充：不得动正式环境。')
  ops=[{'op':'update','id':c['id'],'expected_version':2,'body':c['body']+' 接口验证仅在隔离环境进行。','evidence':c['evidence'],'reason':'保留人工限制，补充验证方式'}]
  review={'approved':True,'findings':[],'proposal_hash':fingerprint(ops),'human_checks':[{'id':c['id'],'version':2,'body_sha256':c['body_sha256'],'preserved':True,'reason':'人工补充的不得动正式环境仍逐字保留，新句只允许隔离验证'}]}
  k.apply('fresh-manual-review','project',ops,review);self.assertIn('不得动正式环境',k.get(c['id'])['body'])
  k.manual_edit(c['id'],3,'新决定：隔离验证也暂停。')
  ops[0]['expected_version']=4;review['proposal_hash']=fingerprint(ops)
  with self.assertRaises(ValueError):k.apply('stale-manual-review','project',ops,review)

 def test_numeric_sign_change_invalidates_old_evidence(self):
  p=self.root/'材料.md';p.write_text('阈值为-1.5%，超过才处理。');self.e.store.ingest('project',str(p));self.process()
  p.write_text('阈值为15%，超过才处理。');self.e.store.ingest('project',str(p));self.assertEqual(self.e.context('project','阈值')['results'],[])
 def test_feedback_outbox_recovered_after_publication_failure(self):
  self.capture('先完成方案，实施暂缓。');self.process();got=self.e.context('project','继续',session='s2');cid=got['results'][0]['id']
  self.capture('接下来立即开发接口。','answer','assistant','s2',receipt_id=got['receipt_id'])
  def model(cmd,prompt,timeout,env=None):
   p=json.loads(prompt)
   if 'proposal' in p:return {'approved':True,'findings':[],'consumption_checks':[{'receipt_id':got['receipt_id'],'evidence':[{'card_id':cid,'verdict':'misapplied','quote':'立即开发接口','reason':'未遵守实施暂缓'}]}]},{}
   return self.compile(p),{}
  real=KnowledgeFiles.publish
  def fail(files,ids=None):
   if ids is not None:raise OSError('disk unavailable')
   return real(files,ids)
  with patch('tongle.worker.invoke_json',side_effect=with_fact_audit(model)),patch.object(KnowledgeFiles,'publish',fail):self.ev.process(force=True)
  self.assertEqual(self.e.db.execute("SELECT COUNT(*) FROM knowledge_work WHERE state='publication_pending'").fetchone()[0],1)
  self.cfg['model_command']=None;self.write_cfg();self.ev.process(force=True)
  self.assertEqual(self.e.db.execute('SELECT state FROM knowledge_consumption WHERE id=?',(got['receipt_id'],)).fetchone()[0],'checked')
  self.assertEqual(self.e.db.execute('SELECT COUNT(*) FROM knowledge_feedback').fetchone()[0],1)
