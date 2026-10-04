import unittest,tempfile,json,hashlib
from pathlib import Path
from tongle.engine import Engine
from tongle.evolution import Evolution
from tongle.knowledge_files import KnowledgeFiles

class DependencyConsumerTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.r=Path(self.tmp.name).resolve();self.e=Engine(self.r/'home');self.addCleanup(self.e.store.close);self.e.init(self.r/'vault')
  (self.e.home/'config.json').write_text(json.dumps({'knowledge_mode':'evolving'}));self.ev=Evolution(self.e);self.k=self.ev.cards
 def apply(self,ops,key='one',project='p'):
  review={'approved':True,'findings':[],'proposal_hash':hashlib.sha256(json.dumps(ops,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()}
  return self.k.apply(key,project,ops,review)
 def basis(self,project='shared',key='基础'):
  return self.k.seed(project,key,'knowledge','测试',key,'空间关联需要有效时间，以免历史结果错配到当前实体。')
 def frame(self,basis,key='框架'):
  op={'op':'create','key':key,'kind':'framework','topic':'测试','title':key,'body':'先确认实体关系的有效时间，再做跨期统计；缺失有效时间时不作历史归属判断。','evidence':[],'basis_cards':[{'card_id':basis['id'],'version':basis['version']}],'reason':'按已有知识形成判断步骤'}
  return self.apply([op],key)['changed_ids'][0]
 def test_parent_revision_immediately_withholds_derived_framework(self):
  a=self.basis();b=self.frame(a);KnowledgeFiles(self.e).publish();self.assertTrue(self.ev._valid_card(self.k.get(b)))
  self.k.manual_edit(a['id'],1,'补充：有时间字段仍须核验来源版本，不直接认定关联正确。');KnowledgeFiles(self.e).publish()
  self.assertFalse(self.ev._valid_card(self.k.get(b)));self.ev.queue_dependents();self.ev.queue_dependents()
  self.assertEqual(self.e.db.execute("SELECT COUNT(*) FROM sources WHERE session='knowledge-dependency'").fetchone()[0],1)
  self.assertEqual(self.k.relations.changed()[0]['card_id'],b)
 def test_revalidation_updates_same_framework_and_keeps_old_dependency_version(self):
  a=self.basis();b=self.frame(a);self.k.manual_edit(a['id'],1,'空间关联需要时间和版本。')
  op={'op':'update','id':b,'expected_version':1,'body':'先核对关系的时间与来源版本，再做归属判断。','evidence':[],'basis_cards':[{'card_id':a['id'],'version':2}],'reason':'新依据补全条件'}
  self.apply([op],'refresh');KnowledgeFiles(self.e).publish();self.assertTrue(self.ev._valid_card(self.k.get(b)));self.assertEqual(self.k.get(b)['version'],2)
  history=self.k.history(b);snapshot=history[0]['snapshot'];snapshot=json.loads(snapshot) if isinstance(snapshot,str) else snapshot
  self.assertEqual(snapshot['basis_cards'][0]['version'],1);self.assertEqual(self.k.relations.changed(),[])
 def test_scope_cycles_stale_ancestry_and_same_batch_ambiguity_rejected(self):
  a=self.basis('p');b=self.frame(a)
  bad={'op':'update','id':a['id'],'expected_version':1,'body':'循环判断','evidence':[],'basis_cards':[{'card_id':b,'version':1}],'reason':'bad cycle'}
  with self.assertRaises(ValueError):self.apply([bad],'cycle')
  with self.assertRaises(ValueError):self.k.relations.validate('foreign',None,[{'card_id':a['id'],'version':1}])
  self.k.manual_edit(a['id'],1,'依据已更新')
  with self.assertRaises(ValueError):self.k.relations.validate('p',None,[{'card_id':b,'version':1}])
  with self.assertRaises(ValueError):self.k.relations.validate('p',None,[{'card_id':a['id'],'version':2}],[a['id']])
 def test_diamond_dependency_is_allowed_not_mistaken_for_cycle(self):
  a=self.basis();b=self.frame(a,'框架乙');c=self.k.seed('p','框架丙','framework','测试','框架丙','另一种解释')
  self.k.relations.set(c['id'],[{'card_id':a['id'],'version':1}]);self.e.db.commit()
  self.assertEqual(len(self.k.relations.validate('p',None,[{'card_id':b,'version':1},{'card_id':c['id'],'version':1}])),2)

if __name__=='__main__':unittest.main()
