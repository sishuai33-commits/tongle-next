"""Failure-oriented product lifecycle tests. Fabricated host records are harness
fixtures only: these tests never claim native task benefit or contact a model."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from tongle import evaluate
from tongle import evaluation_lifecycle as life
from tongle.engine import Engine
from tongle.evolution import Evolution
from tongle.knowledge_files import KnowledgeFiles

FIXTURE = Path(__file__).resolve().parents[1]/'examples/evaluation/status-boundary.json'


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.fixture = json.loads(FIXTURE.read_text())
        evaluate.prepare(self.root/'live', self.fixture)
        self.engine = Engine(self.root/'live/home'); self.addCleanup(self.engine.store.close)
        self.ev = Evolution(self.engine); self.files = KnowledgeFiles(self.engine)
        cards = self.ev.cards.list(self.fixture['project'])
        self.case = life.snapshot(self.engine.home, self.fixture['project'], [c['id'] for c in cards],
                                  'real-fixture', self.fixture['tasks'])
        sid_by_text = {s['text']: s['key'] for s in self.case['sources']}
        source_ids = {}
        for s in self.fixture['sources']:
            actual = self.engine.db.execute('SELECT id FROM sources WHERE project=? AND session=? AND id IN (SELECT source_id FROM versions WHERE text=?)',
                                            (self.case['project'],'fixture',s['text'])).fetchone()
            source_ids[s['key']] = actual[0]
        # The synthetic newer source is not cited by old cards: explicitly import
        # only the candidate's selected excerpt, just as a real correction case.
        refs = [{'source_id':source_ids[r['source']], 'version':1, 'quote':r['quote']}
                for c in self.fixture['candidate'] for r in c['evidence']]
        self.case = life.snapshot(self.engine.home,self.fixture['project'],[c['id'] for c in cards],
                                  'real-fixture',self.fixture['tasks'],refs)
        self.case['candidate'] = copy.deepcopy(self.fixture['candidate'])
        for c in self.case['candidate']:
            for r in c['evidence']:r['source'] = source_ids[r['source']]
        self.case['review'] = {'approved':True,'findings':[], 'reviewer':'independent-test-fixture',
                               'proposal_hash':evaluate.digest(self.case['candidate'])}
        self.grant = self.root/'grant.json'
        self.grant.write_text(json.dumps({'case_hash':evaluate.digest(self.case),'provider':'test','model':'test',
                              'revoked':False,'expires_at':time.time()+3600,'approved_by':'test','scope':'fixture'}))

    def fake_run(self):
        """Build clearly synthetic evidence to exercise the gate, no model invoked."""
        run = self.root/'run'; run.mkdir(); evaluate.save(run/'case.json',self.case)
        evaluate.save(run/'manifest.json',{'case_hash':evaluate.digest(self.case),'native_consumer':True,
            'arms':['off','current','candidate'],'provider':'test','model':'test'})
        for i,t in enumerate(self.case['tasks']):
            for a in ['off','current','candidate']:
                name='%02d-%s'%(i,a);r=run/name
                evaluate.prepare(r,self.case,candidate=a=='candidate')
                answer=json.dumps(t['expect'] if a=='candidate' or i>0 else {})
                native=[{'type':'message_end','message':{'role':'user','content':[{'type':'text','text':t['prompt']}]}}]
                e=Engine(r/'home')
                try:
                    ev=Evolution(e)
                    if a!='off':
                        receipt=ev.context(self.case['project'],t['prompt'],session='s')['receipt_id']
                        offered=json.loads(e.db.execute('SELECT offered FROM knowledge_consumption WHERE id=?',(receipt,)).fetchone()[0])
                        native.append({'type':'message_end','message':{'role':'custom','customType':'tongle-next-context',
                            'content':'\n'.join(x['body'] for x in offered), 'details':{'receiptId':receipt,
                            'promptHash':hashlib.sha256(t['prompt'].encode()).hexdigest()}}})
                        out=e.store.capture(self.case['project'],'s','assistant','a',answer)
                        ev.captured(out['source_id'],'assistant','s',receipt_id=receipt)
                finally:e.store.close()
                m={'role':'assistant','content':[{'type':'text','text':answer}],'provider':'test','model':'test','stopReason':'stop'}
                native.append({'type':'message_end','message':m})
                (r/'events.jsonl').write_text('\n'.join(json.dumps(x) for x in native)+'\n')
                evaluate.save(run/(name+'.json'),{'task':t['id'],'arm':a,'status':'completed','answer':answer})
        return run

    def review(self,run):
        return {'approved':True,'findings':[],'reviewer':'independent-test-fixture','proof_hash':evaluate.digest(life.gate(self.case,run))}

    def test_snapshot_is_excerpt_only_and_readonly(self):
        life.preflight(self.case)
        self.assertEqual({s['key'] for s in self.case['sources']},set(self.case['origin']['sources']))
        self.assertNotIn('text',next(iter(self.case['origin']['sources'].values())))

    def test_new_correction_invalidates_even_unchanged_selected_sources(self):
        self.engine.store.capture(self.case['project'],'new','user','new','A new actual correction.')
        with self.assertRaisesRegex(ValueError,'material changed'):life.preflight(self.case)

    def test_manual_disk_edit_and_revocation_reject_before_apply(self):
        c=next(iter(self.case['origin']['cards'].values()))['card']
        path=self.files.root/c['path']; original=path.read_text(); path.write_text(original+'Manual condition')
        with self.assertRaises(ValueError):life.preflight(self.case)
        path.write_text(original)
        sid=self.case['sources'][0]['key'];self.engine.source_state(self.case['project'],sid,'revoked')
        with self.assertRaises(ValueError):life.check_grant(self.case,self.grant,'test','test')

    def test_grant_exact_identity_expiry_and_revocation(self):
        life.check_grant(self.case,self.grant,'test','test')
        for edits in ({'model':'alias'},{'expires_at':0},{'revoked':True},{'case_hash':'different'}):
            v=json.loads(self.grant.read_text());v.update(edits);p=self.root/(str(len(list(self.root.iterdir())))+'.json');p.write_text(json.dumps(v))
            with self.assertRaises(ValueError):life.check_grant(self.case,p,'test','test')

    def test_full_matrix_required_even_if_report_claims_pass(self):
        r=self.fake_run();life.gate(self.case,r)
        evaluate.save(r/'report.json',{'pass_rate':1})
        (r/'00-current.json').unlink()
        with self.assertRaises(OSError):life.gate(self.case,r)

    def test_modified_answer_and_unknown_identity_rejected(self):
        r=self.fake_run();p=r/'00-candidate/events.jsonl';s=p.read_text();p.write_text(s.replace('"model": "test"','"model": "other"'))
        with self.assertRaises(ValueError):life.gate(self.case,r)

    def test_promotion_replay_and_new_manual_edit(self):
        r=self.fake_run();review=self.review(r)
        result=life.promote(self.case,r,review,self.root/'published.json')
        self.assertEqual(result['state'],'published')
        again=life.promote(self.case,r,review,self.root/'replay.json')
        self.assertTrue(again['replayed'])
        cid=result['changed_ids'][0];c=self.ev.cards.get(cid)
        self.ev.cards.manual_edit(cid,c['version'],c['body']+'New human condition.')
        with self.assertRaises(ValueError):life.promote(self.case,r,review,self.root/'blocked.json')

    def test_crash_after_apply_before_journal_can_retry(self):
        r=self.fake_run();review=self.review(r)
        with patch.object(KnowledgeFiles,'publish',side_effect=OSError('simulated crash')):
            with self.assertRaises(OSError):life.promote(self.case,r,review,self.root/'crash.json')
        again=life.promote(self.case,r,review,self.root/'retry.json')
        self.assertTrue(again['replayed']);self.assertEqual(again['state'],'published')

    def test_pending_retry_after_revoke_never_recovers_old_payload(self):
        r=self.fake_run();review=self.review(r)
        with patch.object(KnowledgeFiles,'_write_atomic',side_effect=OSError('crash')):
            with self.assertRaises(OSError):life.promote(self.case,r,review,self.root/'crash.json')
        cid=life.operations(self.case)[0]['id']; c=self.ev.cards.get(cid);p=self.files.root/c['path'];before=p.read_bytes()
        sid=c['evidence'][0]['source_id'];self.engine.source_state(self.case['project'],sid,'revoked')
        with self.assertRaises(ValueError):life.promote(self.case,r,review,self.root/'reject.json')
        self.assertIn(cid,self.files.recover())
        self.assertEqual(p.read_bytes(),before)

    def test_review_cannot_sign_different_operations(self):
        r=self.fake_run();review=self.review(r)
        self.case['candidate'][0]['body']+='Untested claim.'
        self.case['review']['proposal_hash']=evaluate.digest(self.case['candidate'])
        with self.assertRaises(ValueError):life.promote(self.case,r,review,self.root/'bad.json')

    def test_full_offering_but_truncated_native_injection_does_not_prove_gain(self):
        r=self.fake_run();p=r/'00-candidate/events.jsonl'
        events=[json.loads(x) for x in p.read_text().splitlines()]
        for e in events:
            if e['message'].get('customType'):e['message']['content']='truncated context'
        p.write_text('\n'.join(json.dumps(x) for x in events))
        with self.assertRaisesRegex(ValueError,'no observed target correction'):life.gate(self.case,r)

    def test_correct_answer_with_wrong_materialized_card_rejects(self):
        r=self.fake_run();e=Engine(r/'00-candidate/home')
        try:
            with e.transaction():e.db.execute("UPDATE knowledge_cards SET body='unrelated card'")
        finally:e.store.close()
        with self.assertRaisesRegex(ValueError,'materialized candidate'):life.gate(self.case,r)

    def test_nonfinite_grant_rejected(self):
        for expiry in (float('nan'),float('inf')):
            v=json.loads(self.grant.read_text());v['expires_at']=expiry;self.grant.write_text(json.dumps(v))
            with self.assertRaises(ValueError):life.check_grant(self.case,self.grant,'test','test')

    def test_document_source_identity_survives_isolation(self):
        path=self.root/'report.md';path.write_text('Document evidence: execution observed, acceptance not observed.')
        ack=self.engine.store.ingest(self.case['project'],str(path))
        old=self.case
        refs=[{'source_id':sid,'version':binding['version'],'quote':part}
              for sid,binding in old['origin']['sources'].items()
              for part in next(s['text'] for s in old['sources'] if s['key']==sid).split('\n\n')]
        refs.append({'source_id':ack['source_id'],'version':1,'quote':path.read_text()})
        self.case=life.snapshot(self.engine.home,old['project'],list(old['origin']['cards']),old['id'],old['tasks'],refs)
        self.case.update(candidate=old['candidate'],review=old['review'])
        r=self.fake_run();self.assertTrue(life.gate(self.case,r)['scores'])

    def test_same_text_distinct_source_ids_are_not_ambiguous(self):
        text='Two actual distinct user events with the same words.'
        refs=[]
        for event in ('same-1','same-2'):
            ack=self.engine.store.capture(self.case['project'],'live','user',event,text)
            refs.append({'source_id':ack['source_id'],'version':1,'quote':text})
        old=self.case
        refs.extend({'source_id':sid,'version':b['version'],'quote':part} for sid,b in old['origin']['sources'].items()
                    for part in next(s['text'] for s in old['sources'] if s['key']==sid).split('\n\n'))
        self.case=life.snapshot(self.engine.home,old['project'],list(old['origin']['cards']),old['id'],old['tasks'],refs)
        self.case.update(candidate=old['candidate'],review=old['review'])
        life.gate(self.case,self.fake_run())

    def test_actual_provider_hook_checks_resolved_identity_before_send(self):
        import shutil,subprocess
        node=shutil.which('node')
        if not node:self.skipTest('node unavailable')
        case_path=self.root/'guard-case.json';evaluate.save(case_path,self.case)
        ext=self.root/'guard.ts';life.guard_extension(ext,str(case_path),str(self.grant))
        harness=self.root/'guard-test.mjs'
        harness.write_text('import ext from "./guard.ts";let h;ext({on:(n,f)=>h=f});'
            'h({}, {model:{provider:"test",id:"alias"}});console.log("SENT");')
        result=subprocess.run([node,str(harness)],capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,74);self.assertNotIn('SENT',result.stdout)
        harness.write_text(harness.read_text().replace('id:"alias"','id:"test"'))
        result=subprocess.run([node,str(harness)],capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0);self.assertIn('SENT',result.stdout)
        # Pi catches hook errors; stderr failure must still terminate first.
        harness.write_text('import fs from "node:fs";import ext from "./guard.ts";'
            'fs.writeSync=()=>{throw new Error("ENOSPC")};let h;ext({on:(n,f)=>h=f});'
            'try{h({}, {model:{provider:"test",id:"wrong"}})}catch{};console.log("SENT");')
        result=subprocess.run([node,str(harness)],capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,74);self.assertNotIn('SENT',result.stdout)


    def test_review_findings_are_bounded_preserved_and_do_not_expose_answers(self):
        findings=['Preserve user scope and prior evidence without inventing acceptance.']
        with self.assertRaises(ValueError):
            life.propose(self.case,self.root/'old-grant',self.grant,'pi','test','test',findings=findings)
        g=json.loads(self.grant.read_text());g['findings_hash']=evaluate.digest(findings);self.grant.write_text(json.dumps(g))
        seen={}
        def consumer(root,case,task,*args):
            seen['prompt']=task['prompt']
            return {'provider':'test','model':'test','answer':'{"candidate":[]}','usage':None}
        with patch.object(evaluate,'consume',side_effect=consumer):
            result=life.propose(self.case,self.root/'proposal',self.grant,'pi','test','test',findings=findings)
        self.assertEqual(result['state'],'unapproved')
        self.assertEqual(json.loads((self.root/'proposal/review-findings.json').read_text()),findings)
        self.assertIn(findings[0],seen['prompt'])
        self.assertNotIn('"expect":',seen['prompt'])
        with self.assertRaises(ValueError):
            life.check_grant(self.case,self.grant,'test','test',findings=['new unapproved text'])
        for bad in ([],['x'*2001],['x']*9,{'claim':'accept'}):
            with patch.object(life,'check_grant') as guard, self.assertRaises(ValueError):
                life.propose(self.case,self.root/'invalid',self.grant,'pi','test','test',findings=bad)
            guard.assert_not_called()

    def test_native_hook_rejects_feedback_file_changed_after_prompt_freeze(self):
        import shutil,subprocess
        node=shutil.which('node')
        if not node:self.skipTest('node unavailable')
        c=self.root/'fcase.json';evaluate.save(c,self.case)
        f=self.root/'findings.json';findings=['Approved bounded feedback'];evaluate.save(f,findings)
        h=evaluate.digest(findings)
        g=json.loads(self.grant.read_text());g['findings_hash']=h;self.grant.write_text(json.dumps(g))
        ext=self.root/'feedback-guard.ts';life.guard_extension(ext,str(c),str(self.grant),str(f),h)
        script=self.root/'feedback-test.mjs';script.write_text('import ext from "./feedback-guard.ts";let h;ext({on:(n,f)=>h=f});try{h({}, {model:{provider:"test",id:"test"}})}catch{};console.log("SENT");')
        result=subprocess.run([node,str(script)],capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0)
        f.write_text(json.dumps(['Different unapproved data']))
        result=subprocess.run([node,str(script)],capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,74);self.assertNotIn('SENT',result.stdout)


if __name__=='__main__':unittest.main()
