"""Synthetic consumer regressions from the independent review; no live assets/models."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from tongle.engine import Engine
from tongle.evolution import Evolution
from tongle.knowledge_files import KnowledgeFiles

try:  # sibling import under ``unittest discover -s tests``
    from fact_review_stub import with_fact_audit
except ImportError:  # pragma: no cover - package-import invocation
    from tests.fact_review_stub import with_fact_audit

class EvolutionRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.e=Engine(self.root/'state')
        self.e.init(self.root/'vault');self.addCleanup(self.e.store.close)
        (self.e.home/'config.json').write_text(json.dumps({'knowledge_mode':'evolving','model_command':['stub'],'knowledge_quiet_seconds':0}))
        self.ev=Evolution(self.e);self.payloads=[]
    def seed(self,project='project',key='原决定',body='方案暂缓实施。'):
        return self.ev.cards.seed(project,key,'memory','方案',key,body)
    def capture(self,text='新方案需要保留审核步骤。'):
        return self.e.store.capture('project','session','user','one',text)
    def model(self,cmd,prompt,timeout,env=None):
        p=json.loads(prompt);self.payloads.append(p)
        if 'frozen_proposal' in p:
            cand={c['card_id']:c for c in p['page']['candidates']}
            out=[]
            for pr in p['page']['pairs']:
                body=(cand.get(pr['card_id'],{}).get('body') or '') or '正文'
                out.append({'op':pr['op'],'card_id':pr['card_id'],'relation':'distinct',
                            'quote':body[:20],
                            'reason':'scope and claim differ from the proposed change here'})
            return {'pairs':out},{}
        if 'proposal' in p:return {'approved':True,'findings':[]},{}
        s=p['sources'][-1]
        return {'operations':[{'op':'create','key':'新决定','kind':'memory','topic':'方案','title':'新决定','body':s['text'],'evidence':[{'source_id':s['source_id'],'version':s['version'],'quote':s['text']}],'reason':'新的明确决定'}]},{}
    def run_work(self):
        with patch('tongle.worker.invoke_json',side_effect=with_fact_audit(self.model)):return self.ev.process(force=True)
    def row(self):return dict(self.e.db.execute('SELECT * FROM knowledge_work LIMIT 1').fetchone())
    def test_thirteen_small_cards_have_full_coverage(self):
        for i in range(13):self.seed(key='已有决定'+str(i),body='历史独立决定'+str(i))
        self.capture();r=self.run_work()
        self.assertEqual(r['processed'],1);self.assertEqual(self.row()['state'],'done')
        self.assertEqual(len(self.payloads[0]['existing']['cards']),13)
    def test_mandatory_oversize_body_fails_with_explicit_reason(self):
        src=self.capture()
        self.ev.cards.seed('project','大卡','memory','方案','大卡','完整条件。'*11000,
                           evidence=[{'source_id':src['source_id'],'version':1,
                                     'quote':self.e.store.source(src['source_id'])['text']}])
        r=self.run_work()
        self.assertEqual(r['failed'],1)
        row=self.row()
        self.assertEqual(row['state'],'failed')
        self.assertIn('mandatory cards exceed body budget',row['error'])
        self.assertEqual(len(self.ev.cards.list('project')),1)   # seeded card untouched
    def test_temporary_failure_retries_without_manual_requeue(self):
        self.capture()
        with patch('tongle.worker.invoke_json',side_effect=RuntimeError('temporary unavailable')):self.ev.process(force=True)
        row=self.row();self.assertEqual(row['state'],'retry');self.assertGreater(row['next_at'],time.time())
        with patch('tongle.evolution.time.time',return_value=row['next_at']+1):r=self.run_work()
        self.assertEqual(r['processed'],1);self.assertEqual(self.row()['state'],'done');self.assertEqual(self.row()['attempts'],2)
    def test_shared_comparison_and_exact_duplicate_guard(self):
        body='方案只设计，暂时不实施。';self.seed('shared',body=body);self.seed('foreign',body='别的项目保密范围。');self.capture(body)
        self.run_work();old=self.payloads[0]['existing']
        self.assertTrue(old['shared_is_read_only']);self.assertEqual({c['project'] for c in old['cards']},{'shared'})
        self.assertEqual(self.ev.cards.list('project'),[])
        # The shared card is still readable knowledge, but the generic bare
        # consumer query now intentionally selects no topic (generic-topic
        # policy); the exact-duplicate and authorization guards above are what
        # this regression protects.
        got=self.e.context('project','方案');self.assertEqual(len(got['results']),0)
    def test_shared_exact_guard_even_without_lexical_match(self):
        self.seed('shared',body='是');self.capture('是');self.run_work()
        self.assertEqual(self.payloads[0]['existing']['cards'],[])
        self.assertEqual(self.ev.cards.list('project'),[])
    def test_failed_publication_continues_waiting_while_disk_unavailable(self):
        self.capture()
        with patch.object(KnowledgeFiles,'_write_atomic',side_effect=OSError('disk full')):
            self.run_work()
            with patch('tongle.worker.invoke_json') as model:
                result=self.ev.process(force=True);model.assert_not_called()
            self.assertEqual(result['pending'],1)
            self.assertEqual(self.row()['state'],'publication_pending')
    def test_untracked_bad_markdown_does_not_break_consumption(self):
        self.seed();KnowledgeFiles(self.e,self.ev.cards).publish()
        bad=self.root/'vault'/'知识'/'坏文件.md';bad.parent.mkdir(parents=True,exist_ok=True);bad.write_bytes(b'\xff')
        (bad.parent/'超限文件.md').write_bytes(b'x'*(2*1024*1024+1))
        self.assertTrue(self.e.context('project','方案')['results'])
    def test_tracked_bad_markdown_isolated_from_other_project(self):
        self.seed();self.seed('foreign',key='外部方案',body='另一个项目的方案。');KnowledgeFiles(self.e,self.ev.cards).publish()
        p=self.ev.cards.list('project')[0]['path'];(self.root/'vault'/p).write_bytes(b'\xff')
        self.assertTrue(self.e.context('foreign','方案')['results'])
        self.assertEqual(self.e.context('project','方案')['results'],[])
    def test_failed_publication_recovers_without_model_rerun(self):
        self.capture()
        with patch.object(KnowledgeFiles,'_write_atomic',side_effect=OSError('disk full')):self.run_work()
        self.assertEqual(self.row()['state'],'publication_pending')
        with patch('tongle.worker.invoke_json') as model:self.ev.process(force=True);model.assert_not_called()
        self.assertEqual(self.row()['state'],'done');self.assertTrue(self.e.context('project','方案')['results'])
    def test_manual_edit_available_on_first_context(self):
        self.seed();KnowledgeFiles(self.e,self.ev.cards).publish();p=self.root/'vault'/self.ev.cards.list('project')[0]['path']
        p.write_text(p.read_text().replace('方案暂缓实施。','方案只做已批准部分。'))
        got=self.e.context('project','方案');self.assertTrue(got['results']);self.assertIn('已批准部分',got['results'][0]['text'])
    def test_same_key_create_after_reading_candidate_is_rejected(self):
        # A create whose key/title exactly matches an existing card must read
        # the full candidate; persisting after reading it is a duplicate.
        self.capture('先完成方案，实施暂缓。');self.run_work()
        self.assertEqual(len(self.ev.cards.list('project')),1)
        rec=self.e.store.capture('project','s2','user','two','接下来立即开发接口。')
        self.ev.captured(rec['source_id'],'user','s2')
        def model(cmd,prompt,timeout,env=None):
            p=json.loads(prompt)
            if 'proposal' in p:return {'approved':True,'findings':[]},{}
            s=p['sources'][-1]
            return {'operations':[{'op':'create','key':'新决定','kind':'memory','topic':'方案',
                    'title':'新决定','body':s['text'],
                    'evidence':[{'source_id':s['source_id'],'version':s['version'],'quote':s['text']}],
                    'reason':'重复创建'}]},{}
        with patch('tongle.worker.invoke_json',side_effect=with_fact_audit(model)):
            for _ in range(3):
                result=self.ev.process(force=True)
                with self.e.transaction():
                    self.e.db.execute('UPDATE knowledge_work SET next_at=0')
        self.assertEqual(result['failed'],1)
        error=self.e.db.execute("SELECT error FROM knowledge_work WHERE state='failed'"
                                " ORDER BY created_at DESC LIMIT 1").fetchone()['error']
        self.assertIn('create duplicates existing card',error)
        self.assertEqual(len(self.ev.cards.list('project')),1)

if __name__=='__main__':unittest.main()
