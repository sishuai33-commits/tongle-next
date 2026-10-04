"""Regressions for final independent review and attribution checks."""
import json
import sys
from pathlib import Path
import unittest
import test_acceptance as base
from tongle.engine import Engine
from tongle.worker import prepare, process_jobs
from tongle.synthesis import queue_synthesis
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'adapters'))
from hook import parse_cc_transcript_line
from common import render_context_result

class FinalReview(unittest.TestCase):
    setUp=base.Acceptance.setUp
    tearDown=base.Acceptance.tearDown
    call=base.Acceptance.call
    def fixture(self):
        for sid,text in [('a','原始资料甲。'),('b','原始资料乙。')]:
            path=self.base/(sid+'.md');path.write_text(text)
            self.call('ingest','--project','study','--path',str(path),'--source-id',sid)
        engine=Engine(self.home);prepare(engine)
        engine.db.execute("UPDATE jobs SET state='done'")
        jid=queue_synthesis(engine,'study')['job_id']
        return engine,jid
    def test_secondary_source_return_resumes_and_revoke_obsoletes(self):
        e,jid=self.fixture()
        e.source_state('study','b','missing')
        e.db.execute("UPDATE jobs SET state='blocked' WHERE id=?",(jid,))
        e.source_state('study','b','available')
        self.assertEqual(e.db.execute('SELECT state FROM jobs WHERE id=?',(jid,)).fetchone()[0],'pending')
        e.db.execute("UPDATE jobs SET state='blocked' WHERE id=?",(jid,))
        e.source_state('study','b','revoked')
        self.assertEqual(e.db.execute('SELECT state FROM jobs WHERE id=?',(jid,)).fetchone()[0],'obsolete')
    def test_stale_secondary_persisted_output_obsoleted_without_model_call(self):
        e,jid=self.fixture()
        job=e.db.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone()
        sid=job['source_id'];other='b' if sid=='a' else 'a'
        refs=[{'source_id':s,'version':1,'quote':e.store.source(s)['text']} for s in ['a','b']]
        output={'records':[{'text':'两份资料综合。','kind':'synthesis','quote':e.store.source(sid)['text'],'evidence':refs}]}
        e.db.execute('UPDATE jobs SET result_json=? WHERE id=?',(json.dumps(output),jid))
        path=self.base/(other+'.md');path.write_text('资料已修订。')
        e.store.ingest('study',str(path),source_id=other)
        e.db.execute("UPDATE jobs SET state='done' WHERE id!=?",(jid,))
        (self.home/'config.json').write_text(json.dumps({'model_command':['must-not-execute'],'auto_synthesize':False}))
        process_jobs(e,1)
        row=e.db.execute('SELECT state,attempts,error FROM jobs WHERE id=?',(jid,)).fetchone()
        self.assertEqual(row['state'],'obsolete',dict(row));self.assertEqual(row['attempts'],1)
    def test_mixed_tool_result_not_user_statement(self):
        entry={'type':'user','uuid':'x','message':{'role':'user','content':[{'type':'tool_result','content':'结果'},{'type':'text','text':'系统附注'}]}}
        self.assertIsNone(parse_cc_transcript_line(json.dumps(entry)))
        entry['message']={'role':'assistant','content':'建议'}
        self.assertIsNone(parse_cc_transcript_line(json.dumps(entry)))
    def test_synthesis_context_retains_both_source_references(self):
        text,error=render_context_result(json.dumps({'results':[{'text':'综合结论','status':'candidate','evidence':[{'source_id':'a','version':1},{'source_id':'b','version':2}]}]}))
        self.assertIsNone(error);self.assertIn('a@1',text);self.assertIn('b@2',text)
