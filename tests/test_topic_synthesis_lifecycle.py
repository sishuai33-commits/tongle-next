"""Mechanical lifecycle tests. Fake model answers are not semantic validation."""
import json
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
from tongle.engine import Engine
from tongle.evolution import Evolution, BudgetWait
from tongle.knowledge_files import KnowledgeFiles
from tongle.topic_synthesis import TopicSynthesis, compact
from tongle.evaluate import digest


class TopicLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name); self.e = Engine(root/'home')
        self.e.init(root/'vault', evolving=True); self.addCleanup(self.e.store.close)
        self.cfg = {'knowledge_mode':'evolving', 'model_command':['unused'],
                    'knowledge_synthesis_topics':{'p':['主题']}}
        (self.e.home/'config.json').write_text(json.dumps(self.cfg))
        self.ev = Evolution(self.e); self.ts = TopicSynthesis(self.ev)
        self.a = self.add('a', '条件甲时采用方法甲。')
        self.calls = []
        self.ev._call = self.model

    def add(self, key, text):
        src = self.e.store.capture('p','fixture','user',key,text)
        card = self.ev.cards.seed('p',key,'knowledge','主题',key,text,
            evidence=[{'source_id':src['source_id'],'version':1,'quote':text}])
        self.ev.baseline(); KnowledgeFiles(self.e).publish()
        return card

    def model(self, job, stage, packet, cfg):
        self.calls.append((stage, packet)); cid=uuid.uuid4().hex; self.ev._last_call_id=cid
        with self.e.transaction():
            self.e.db.execute('INSERT INTO knowledge_calls(id,work_id,stage,day,reserved,elapsed,created_at) VALUES(?,?,?,?,?,?,?)',
                (cid,job['id'],stage,'fixture',1,0,time.time()))
        if stage=='topic_propose':
            return {'body':'有条件理解：'+ '\n'.join(s['text'] for s in packet['snapshot']['sources']), 'reason':'根据明确来源保留条件'}
        return {'approved':True,'findings':[], 'proposal_hash':packet['proposal_hash'],'human_checks':[]}

    def done(self):
        self.ts.cycle(self.cfg); r=self.ts.list()[0]
        self.assertEqual(r['state'],'published',r['payload'].get('reason')); return r

    def test_publish_update_same_id_no_change_zero_calls(self):
        r=self.done(); cid=r['target_id']; self.assertEqual(len(self.calls),2)
        self.assertEqual(self.ts.cycle(self.cfg)['calls'],0)
        self.add('b','条件乙是方法甲的例外。'); r=self.done()
        self.assertEqual(r['target_id'],cid); self.assertEqual(self.ev.cards.get(cid)['version'],2)
        self.assertEqual(len(self.ts.list()),1); self.assertEqual(self.ts.cycle(self.cfg)['calls'],0)
        self.assertEqual(self.ts.status()['utility'],'unknown')

    def test_new_card_before_file_publish_is_eventually_discovered(self):
        self.done(); src=self.e.store.capture('p','fixture','user','unpublished','新材料乙')
        self.ev.cards.seed('p','unpublished','knowledge','主题','乙','新材料乙',
            evidence=[{'source_id':src['source_id'],'version':1,'quote':'新材料乙'}])
        self.assertEqual(self.ts.cycle(self.cfg)['calls'],0)
        KnowledgeFiles(self.e).publish(); self.done(); self.assertEqual(len(self.calls),4)

    def test_revoke_stops_old_context_then_rebuilds_remaining(self):
        b=self.add('b','保留条件乙。'); r=self.done(); cid=r['target_id']
        self.e.source_state('p',self.a['evidence'][0]['source_id'],'revoked')
        self.assertFalse(self.ev._valid_card(self.ev.cards.get(cid)))
        r=self.done(); card=self.ev.cards.get(cid)
        self.assertEqual([b['id']],[x['card_id'] for x in card['basis_cards']]); self.assertTrue(self.ev._valid_card(card))
        self.assertNotIn('方法甲',card['body'])

    def test_all_revoked_wait_no_call(self):
        r=self.done(); self.e.source_state('p',self.a['evidence'][0]['source_id'],'revoked')
        self.assertEqual(self.ts.cycle(self.cfg)['calls'],0)
        self.assertEqual(self.ts.list()[0]['state'],'waiting')
        self.assertFalse(self.ev._valid_card(self.ev.cards.get(r['target_id'])))

    def test_stale_during_review_never_publishes(self):
        real=self.model
        def model(job,stage,packet,cfg):
            answer=real(job,stage,packet,cfg)
            if stage=='topic_review':self.e.source_state('p',self.a['evidence'][0]['source_id'],'revoked')
            return answer
        self.ev._call=model; self.ts.cycle(self.cfg)
        self.assertIsNone(self.ts.list()[0]['target_id'])
        self.assertEqual(self.e.db.execute("SELECT COUNT(*) FROM knowledge_cards WHERE kind='framework'").fetchone()[0],0)

    def test_publish_io_replay_zero_extra_calls(self):
        original=KnowledgeFiles.publish
        def fail(obj,*a,**kw):raise OSError('simulated file failure')
        with patch.object(KnowledgeFiles,'publish',fail):
            self.ts.cycle(self.cfg)
        r=self.ts.list()[0]; self.assertEqual(r['state'],'publication_pending')
        cid=r['target_id']; self.assertIsNotNone(cid)
        self.assertEqual(self.ts.cycle(self.cfg)['calls'],0)
        self.assertEqual(self.ts.list()[0]['state'],'published'); self.assertEqual(self.ev.cards.get(cid)['version'],1)

    def test_manual_edit_during_publication_not_clobbered(self):
        with patch.object(KnowledgeFiles,'publish',side_effect=OSError('simulated')):self.ts.cycle(self.cfg)
        r=self.ts.list()[0]; c=self.ev.cards.get(r['target_id'])
        self.ev.cards.manual_edit(c['id'],c['version'],'人工内容必须保留。')
        self.ts.cycle(self.cfg)
        self.assertEqual(self.ev.cards.get(c['id'])['body'],'人工内容必须保留。')
        self.assertEqual(self.ts.list()[0]['state'],'attention')

    def test_manual_preservation_gate(self):
        r=self.done(); c=self.ev.cards.get(r['target_id'])
        self.ev.cards.manual_edit(c['id'],c['version'],c['body']+'\n人工条款。'); KnowledgeFiles(self.e).publish()
        before=self.ev.cards.get(c['id']); self.ts.cycle(self.cfg); self.ts.cycle(self.cfg)
        self.assertEqual(self.ev.cards.get(c['id']),before)
        self.assertEqual(self.ts.list()[0]['state'],'attention')

    def test_budget_wait_not_an_attempt(self):
        def budget(*a):raise BudgetWait('test daily budget')
        self.ev._call=budget; self.assertEqual(self.ts.cycle(self.cfg)['calls'],0)
        self.assertEqual(self.ts.list()[0]['payload']['attempts']['propose'],0)
        self.ev._call=self.model; self.done()

    def test_unreviewed_or_tampered_proposal_refused(self):
        self.ts.cycle(self.cfg,max_calls=1); r=self.ts.list()[0]
        with self.assertRaises(ValueError):self.ts.publish(r['id'],self.cfg)
        # Stop before publication to tamper the approved operation.
        with patch.object(self.ts,'publish',side_effect=OSError('stop before publish')):self.ts.cycle(self.cfg,max_calls=1)
        r=self.ts.list()[0]; r['payload']['operation']['body']='fabricated'
        with self.e.transaction():self.ts._save(r)
        with self.assertRaises(ValueError):self.ts.publish(r['id'],self.cfg)

    def test_rejected_review_one_repair_only(self):
        real=self.model
        def reject(job,stage,packet,cfg):
            answer=real(job,stage,packet,cfg)
            if stage=='topic_review':answer.update(approved=False,findings=['缺少明确例外'])
            return answer
        self.ev._call=reject
        for _ in range(4):self.ts.cycle(self.cfg)
        self.assertEqual(len(self.calls),4); self.assertEqual(self.ts.list()[0]['state'],'attention')

    def test_review_sees_full_body(self):
        self.done(); packet=self.calls[1][1]
        self.assertIn('条件甲',packet['operation']['body'])
        self.assertTrue(packet['operation']['basis_cards'])

    def test_synth_and_dependent_do_not_trigger_self(self):
        r=self.done(); c=self.ev.cards.get(r['target_id'])
        self.ev.cards.apply('derived','p',[{'op':'create','key':'child','kind':'framework','topic':'主题','title':'child',
            'body':'依赖综合产物','basis_cards':[{'card_id':c['id'],'version':c['version']}], 'evidence':[], 'reason':'test'}],
            {'approved':True,'findings':[],'proposal_hash':digest([{'op':'create','key':'child','kind':'framework','topic':'主题','title':'child','body':'依赖综合产物','basis_cards':[{'card_id':c['id'],'version':c['version']}], 'evidence':[], 'reason':'test'}])})
        KnowledgeFiles(self.e).publish()
        # Addition is accounted for as excluded; must not cause fresh synthesis.
        self.assertEqual(self.ts.cycle(self.cfg)['calls'],0)

    def test_scope_change_mid_call_denies_publication(self):
        real=self.model
        def changing(job,stage,packet,cfg):
            answer=real(job,stage,packet,cfg)
            (self.e.home/'config.json').write_text(json.dumps(dict(self.cfg,knowledge_synthesis_topics={})))
            return answer
        self.ev._call=changing;self.ts.cycle(self.cfg)
        self.assertIsNone(self.ts.list()[0]['target_id'])

    def test_cross_partition_new_exception_invalidates_and_updates_old(self):
        for i in range(5):self.add('base'+str(i),'相关规则条款'+str(i))
        old=self.done(); cid=old['target_id']; self.add('exception','特殊条件下方法甲禁止使用。')
        self.assertFalse(self.ev._valid_card(self.ev.cards.get(cid)))
        for _ in range(4):self.ts.cycle(self.cfg)
        card=self.ev.cards.get(cid)
        self.assertIn('特殊条件',card['body']); self.assertEqual(card['version'],2)
        self.assertTrue(self.ev._valid_card(card))
        self.assertEqual(self.ts.cycle(self.cfg)['calls'],0)

    def test_unread_changes_over_bound_never_declared_current(self):
        for i in range(5):self.add('base'+str(i),'相关规则条款'+str(i))
        old=self.done(); cid=old['target_id']
        for i in range(7):self.add('new'+str(i),'新增反例条件'+str(i))
        self.ts.discover(self.cfg)
        self.assertEqual(self.ts.get(old['id'])['state'],'attention')
        self.assertFalse(self.ev._valid_card(self.ev.cards.get(cid)))
        snap=self.ts.get(old['id'])['payload']['snapshot']
        self.assertEqual(len(snap['members']),13)

    def test_frozen_snapshot_tamper_not_dispatched(self):
        self.ts.discover(self.cfg);r=self.ts.list()[0]
        r['payload']['snapshot']['sources'][0]['text']='伪造规则'
        with self.e.transaction():self.ts._save(r)
        self.ts.cycle(self.cfg)
        self.assertEqual(len(self.calls),0);self.assertIsNone(self.ts.list()[0]['target_id'])

    def test_rejected_call_cannot_be_rebound_as_approved(self):
        real=self.model
        def reject(job,stage,packet,cfg):
            ans=real(job,stage,packet,cfg)
            if stage=='topic_review':ans.update(approved=False,findings=['拒绝'])
            return ans
        self.ev._call=reject;self.ts.cycle(self.cfg)
        r=self.ts.list()[0];pr=r['payload']['calls']['review']
        pr['answer'].update(approved=True,findings=[]);pr['answer_hash']=digest(pr['answer']);r['state']='publication_pending'
        with self.e.transaction():self.ts._save(r)
        with self.assertRaisesRegex(ValueError,'independent host receipt'):self.ts.publish(r['id'],self.cfg)

    def test_sources_deduplicated_and_partition_members_stable(self):
        src=self.a['evidence'][0]
        self.ev.cards.seed('p','same-source','knowledge','主题','另一条关系','条件甲与方法甲存在前置关系。',evidence=[src])
        KnowledgeFiles(self.e).publish();self.ts.discover(self.cfg)
        r=self.ts.list()[0];self.assertEqual(len(r['payload']['snapshot']['sources']),1)
        for i in range(6):self.add('extra'+str(i),'不同材料'+str(i))
        self.ts.discover(self.cfg);rows=self.ts.list()
        self.assertEqual(len(rows),1);self.assertEqual(len(rows[0]['members']),8)
        prior=[(x['id'],x['members'],x['payload']['run']) for x in rows]
        self.ts.discover(self.cfg);self.assertEqual(prior,[(x['id'],x['members'],x['payload']['run']) for x in self.ts.list()])

    def test_foreign_project_and_disabled_have_no_work(self):
        src=self.e.store.capture('q','fixture','user','foreign','无权材料')
        self.ev.cards.seed('q','foreign','knowledge','主题','外部','无权材料',evidence=[{'source_id':src['source_id'],'version':1,'quote':'无权材料'}])
        KnowledgeFiles(self.e).publish();self.ts.discover(self.cfg)
        self.assertEqual(len(self.ts.list()),1)
        self.assertEqual(len(self.ts.list()[0]['payload']['snapshot']['cards']),1)
        self.assertEqual(self.ts.cycle(dict(self.cfg,knowledge_synthesis_topics={}))['calls'],0)

    def test_huge_original_not_truncated(self):
        self.add('huge','长原文'+('文'*50000))
        self.ts.discover(self.cfg)
        huge=next(r for r in self.ts.list() if r['state']=='attention')
        self.assertTrue(any(len(s['text'])>50000 for s in huge['payload']['snapshot']['sources']))

    def test_initial_exception_is_read_even_after_six_cards(self):
        for i in range(5):self.add('base'+str(i),'基础条款'+str(i))
        self.add('late','关键例外：方法甲在条件乙禁止。')
        r=self.done();self.assertIn('关键例外',self.ev.cards.get(r['target_id'])['body'])
        self.assertEqual(len(r['payload']['snapshot']['members']),7)

    def test_forged_commit_marker_cannot_republish_stale_content(self):
        old=self.done();self.add('new','新的关键例外。');self.ts.discover(self.cfg)
        r=self.ts.get(old['id']); c=self.ev.cards.get(r['target_id'])
        r['payload']['committed']={'card_id':c['id'],'version':c['version'],'hash':digest(compact(c))}
        r['state']='publication_pending'
        with self.e.transaction():self.ts._save(r)
        with self.assertRaises((ValueError,KeyError)):self.ts.publish(r['id'],self.cfg)
        self.assertFalse(self.ev._valid_card(c))

    def test_catalog_stamp_alone_cannot_restore_old_consumer(self):
        old=self.done();self.add('new','新的未审例外。')
        r=self.ts.get(old['id']);p=r['payload']
        catalog={c['id']:c for c in self.ev._authorized_catalog('p')}
        p['snapshot']['catalog_state']=self.ts._catalog_state('p','主题',catalog)
        p['input_hash']=self.ts._input_hash(p['snapshot'])
        with self.e.transaction():self.ts._save(r)
        self.assertFalse(self.ev._valid_card(self.ev.cards.get(r['target_id'])))

    def test_file_sync_commits_do_not_break_topic_transaction(self):
        r=self.done();card=self.ev.cards.get(self.a['id'])
        root=Path(self.e.db.execute("SELECT value FROM meta WHERE key='vault'").fetchone()[0])
        path=root/card['path'];text=path.read_text()
        path.write_text(text.replace('条件甲时采用方法甲。','条件甲时采用方法甲。新增人工条件乙。'))
        self.ts.discover(self.cfg)
        updated=self.ev.cards.get(self.a['id'])
        self.assertGreater(updated['version'],card['version'])
        self.assertTrue(updated['manually_edited'])
        self.assertEqual(self.ts.get(r['id'])['state'],'waiting')
        KnowledgeFiles(self.e).publish();self.ts.discover(self.cfg)
        self.assertEqual(self.ts.get(r['id'])['state'],'propose')
