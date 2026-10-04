"""Mechanical gate tests. Audit rows below are explicitly synthetic fixtures;
passing these tests is not evidence of provider quality or natural benefit.
"""
import json
import time
import unittest
import uuid
from unittest.mock import patch

from tests import test_learning as contract
FACT = contract.FACT
from tongle.evolution import BudgetWait, config
from tongle.knowledge_files import KnowledgeFiles
from tongle.evaluate import digest


class LearningLifecycleTests(unittest.TestCase):
    setUp = contract.LearningContractTests.setUp

    def _prepare(self, mode='gain', audited=True):
        self.rec = self.learning.observe('project', self.ctx['receipt_id'], self.outcome['source_id'])
        self.learning.set_cases(self.rec['id'], {'id': 'frozen', 'version': 1, 'tasks': [
            {'id': 'new-task', 'source_id': self.task_source['source_id'], 'source_version': 1, 'expect': {'answer': False}}]})
        self.packets = []
        def call(job, stage, payload, cfg):
            self.packets.append((stage, payload))
            cid = uuid.uuid4().hex
            self.ev._last_call_id = cid
            if audited:
                with self.e.transaction():
                    self.e.db.execute('INSERT INTO knowledge_calls(id,work_id,stage,day,reserved,actual_input,actual_output,elapsed,error,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                                      (cid,job['id'],stage,'synthetic-test-fixture',1,1,1,0.01,None,time.time()))
            if stage == 'learning_propose':
                return {'decision':'candidate','reason':'synthetic method fixture', 'candidate': {
                    'title':'验收状态分层法','topic':'完成边界',
                    'body':'验收状态应分层报告：确认接收仅说明请求进入处理流程，不能说明业务已经完成。实际检查缺失时，应报告结果未知，并保留可供后续验收的依据；在得到相应完成证据后才能更新业务完成状态。',
                    'evidence':[{'source_id':self.src['source_id'],'version':1,'quote':FACT}],
                    'basis_cards':[{'card_id':self.card['id'],'version':1}], 'reason':'synthetic candidate grounded in fixture'}}
            if stage == 'learning_review':
                return {'approved':True,'findings':[],'proposal_hash':payload['proposal_hash']}
            candidate = len(payload['knowledge']) > 1
            return {'answer': False if candidate or mode == 'no_gain' else True}
        self.ev._call = call
        return self.rec['id']

    def _advance(self, cycles=4):
        for _ in range(cycles):
            result = self.learning.cycle(config(self.e))
            self.assertLessEqual(result['calls'], 2)
        return self.learning.get(self.rec['id'])

    def test_gain_real_core_publish_context_and_pause(self):
        rid = self._prepare()
        self.assertEqual(self._advance()['state'], 'publication_pending')
        rec = self.learning.publish(rid, config(self.e))
        cid = rec['payload']['publication']['card_id']
        self.assertTrue(self.ev._published(cid))
        ctx = self.ev.context('project','验收状态分层法 接收 完成',session='new-context')
        offered = json.loads(self.e.db.execute('SELECT offered FROM knowledge_consumption WHERE id=?',(ctx['receipt_id'],)).fetchone()[0])
        self.assertTrue(any(r['id']==cid and r['delivery']=='full' for r in offered))
        self.assertEqual(self.learning.publish(rid,config(self.e))['state'],'published')
        self.assertEqual(self.learning.pause(rid,'synthetic pause verification')['state'],'paused')
        self.assertTrue(self.ev._published(cid))
        self.assertEqual(self.learning.pause(rid,'retry')['state'],'paused')
        ctx = self.ev.context('project','验收状态分层法 接收 完成',session='after-pause')
        if ctx.get('receipt_id'):
            offered = json.loads(self.e.db.execute('SELECT offered FROM knowledge_consumption WHERE id=?',(ctx['receipt_id'],)).fetchone()[0])
            self.assertNotIn(cid,[r['id'] for r in offered])

    def test_no_gain_cannot_publish(self):
        rid = self._prepare('no_gain')
        self.assertEqual(self._advance()['state'],'no_gain')
        with self.assertRaises(ValueError):self.learning.publish(rid,config(self.e))
        self.assertEqual(self.e.db.execute('SELECT COUNT(*) FROM knowledge_cards').fetchone()[0],1)

    def test_call_without_real_audit_is_not_evidence(self):
        self._prepare(audited=False)
        rec=self._advance(1)
        self.assertEqual(rec['state'],'needs_evidence')
        self.assertIn('audit',rec['payload']['reason'])

    def test_frozen_cases_never_enter_proposer_or_reviewer(self):
        self._prepare(); self._advance()
        text=self.e.store.source(self.task_source['source_id'])['text']
        for stage,payload in self.packets:
            if stage!='learning_consume':self.assertNotIn(text,json.dumps(payload,ensure_ascii=False))
            else:self.assertNotIn('expect',payload)
        self.assertEqual([s for s,p in self.packets],['learning_propose','learning_review','learning_consume','learning_consume'])

    def test_publication_recovers_after_core_apply_without_new_model(self):
        rid=self._prepare();self._advance()
        with patch.object(KnowledgeFiles,'publish',side_effect=OSError('synthetic disk failure')):
            with self.assertRaises(OSError):self.learning.publish(rid,config(self.e))
        self.assertEqual(self.e.db.execute('SELECT COUNT(*) FROM knowledge_cards').fetchone()[0],2)
        self.assertEqual(self.learning.get(rid)['state'],'publication_pending')
        before=len(self.packets)
        rec=self.learning.publish(rid,config(self.e))
        self.assertEqual(rec['state'],'published')
        self.assertEqual(len(self.packets),before)
        self.assertEqual(self.e.db.execute('SELECT COUNT(*) FROM knowledge_cards').fetchone()[0],2)

    def test_changed_source_blocks_publication(self):
        rid=self._prepare();self._advance()
        self.e.source_state('project',self.src['source_id'],'revoked')
        with self.assertRaises(ValueError):self.learning.publish(rid,config(self.e))

    def test_tampered_result_blocks_publication(self):
        rid=self._prepare();self._advance()
        rec=self.learning.get(rid)
        rec['payload']['trials']['0:current']['answer']={'answer':False}
        with self.e.transaction():self.learning._save(rec)
        with self.assertRaises(ValueError):self.learning.publish(rid,config(self.e))

    def test_manual_changed_card_is_not_retired(self):
        rid=self._prepare();self._advance();rec=self.learning.publish(rid,config(self.e))
        cid=rec['payload']['publication']['card_id']
        self.ev.cards.manual_edit(cid,1,'用户手工修改的内容应保留，不继承此前验收结果。')
        with self.assertRaises(ValueError):self.learning.pause(rid,'new problem')
        self.assertEqual(self.ev.cards.get(cid)['state'],'active')
        self.learning.cycle(config(self.e))
        self.assertEqual(self.learning.get(rid)['state'],'attention')

    def test_retry_errors_do_not_count_as_baseline_failure(self):
        self._prepare(); self._advance(2)
        def fail(*a,**k):raise RuntimeError('synthetic infrastructure failure')
        self.ev._call=fail
        rec=self._advance(2)
        self.assertEqual(rec['state'],'needs_evidence')
        self.assertEqual(rec['payload']['trials'],{})
        self.assertNotIn('comparison',rec['payload'])

    def test_budget_wait_refunds_attempt(self):
        self._prepare()
        def wait(*a,**k):raise BudgetWait()
        self.ev._call=wait
        for _ in range(3):self.learning.cycle(config(self.e))
        rec=self.learning.get(self.rec['id'])
        self.assertEqual(rec['state'],'observed')
        self.assertEqual(rec['payload']['stage_attempts']['propose'],0)

    def test_changed_configuration_while_calling_cannot_settle(self):
        self._prepare();original=self.ev._call
        def revoke(*args):
            answer=original(*args)
            cfg=config(self.e);cfg['knowledge_learning_projects']=[]
            (self.e.home/'config.json').write_text(json.dumps(cfg))
            return answer
        self.ev._call=revoke
        rec=self._advance(1)
        self.assertEqual(rec['state'],'needs_evidence')
        self.assertNotIn('operation',rec['payload'])

    def test_holdout_cannot_reuse_training_source(self):
        rec=self.learning.observe('project',self.ctx['receipt_id'],self.outcome['source_id'])
        with self.assertRaises(ValueError):
            self.learning.set_cases(rec['id'],{'id':'invalid','version':1,'tasks':[
                {'id':'leaked','source_id':self.src['source_id'],'source_version':1,'expect':{'answer':False}}]})

    def test_existing_followup_capture_links_actual_user_source(self):
        source=self.e.store.capture('project','task','user','followup','纠正：刚才业务还没有完成，实际验收失败。')
        result=self.ev.captured(source['source_id'],'user','task')
        self.assertIn('learning_observation',result)
        rec=self.learning.get(result['learning_observation'])
        self.assertEqual(rec['payload']['outcomes'][0]['role'],'user')
        self.assertEqual(rec['payload']['outcomes'][0]['id'],source['source_id'])

    def test_followup_on_published_version_precautionarily_pauses(self):
        rid=self._prepare();self._advance();published=self.learning.publish(rid,config(self.e))
        cid=published['payload']['publication']['card_id']
        ctx=self.ev.context('project','验收状态分层法 接收 完成',session='followup-use')
        out=self.e.store.capture('project','followup-use','assistant','followup-answer','接收已经确认，业务是否完成仍待核对。')
        self.ev.captured(out['source_id'],'assistant','followup-use',receipt_id=ctx['receipt_id'])
        correction=self.e.store.capture('project','followup-use','user','followup-correction','纠正：实际验收失败，刚才的方法遗漏了一项约束。')
        captured=self.ev.captured(correction['source_id'],'user','followup-use')
        self.assertIn('learning_observation',captured)
        self.learning._monitor(['project'])
        self.assertEqual(self.learning.get(rid)['state'],'paused')
        self.assertEqual(self.ev.cards.get(cid)['state'],'retired')

    def test_active_lease_prevents_second_dispatch(self):
        rid=self._prepare()
        token=self.learning._claim(rid,config(self.e))
        self.assertTrue(token)
        self.assertIsNone(self.learning._claim(rid,config(self.e)))
        result=self.learning.cycle(config(self.e))
        self.assertEqual(result['calls'],0)
        self.assertEqual(self.packets,[])

    def test_schema_error_is_bounded_and_preserves_both_responses(self):
        rid=self._prepare();original=self.ev._call
        def invalid(*args):
            answer=original(*args);answer['decision']='invalid-enum';return answer
        self.ev._call=invalid
        self.assertEqual(self._advance(1)['state'],'observed')
        rec=self._advance(1)
        self.assertEqual(rec['state'],'needs_evidence')
        self.assertEqual(len(rec['payload']['responses']),2)
        self._advance(2)
        self.assertEqual(len(self.packets),2)

if __name__=='__main__':unittest.main()
