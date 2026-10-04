"""Independent slow-model fixture: validates races and durability, not semantics."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
import test_acceptance as base
ROOT=Path(__file__).resolve().parents[1]

class WorkerBoundaries(unittest.TestCase):
    setUp=base.Acceptance.setUp
    tearDown=base.Acceptance.tearDown
    call=base.Acceptance.call
    source=base.Acceptance.source
    record=base.Acceptance.record
    query=base.Acceptance.query
    def runner(self,quote,wait=False,fail=False):
        marker=self.base/'model-started';release=self.base/'release'
        script=self.base/'runner.py'
        script.write_text('import sys,time,json\nfrom pathlib import Path\n'+
                          f'Path({str(marker)!r}).touch()\n'+
                          (f'while not Path({str(release)!r}).exists():time.sleep(.03)\n' if wait else '')+
                          ('sys.exit(1)\n' if fail else 'print('+repr(json.dumps({'records':[{'key':'scope','text':quote,'quote':quote,'kind':'memory'}]}))+')\n'))
        (self.home/'config.json').write_text(json.dumps({'model_command':[sys.executable,str(script)],'model_timeout_seconds':10}))
        return marker,release
    def process_start(self):
        return subprocess.Popen([sys.executable,'-m','tongle','--home',str(self.home),'process','--limit','1'],
                                cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    def await_marker(self,path):
        deadline=time.monotonic()+5
        while time.monotonic()<deadline and not path.exists():time.sleep(.03)
        self.assertTrue(path.exists())
    def finish(self,process):
        out,err=process.communicate(timeout=15)
        self.assertEqual(process.returncode,0,err+out);return json.loads(out)
    def test_failed_model_preserves_event_and_retry_job(self):
        self.source('harbor','需要可靠保存。');self.runner('需要可靠保存。',fail=True)
        result=self.call('process','--limit','1')
        self.assertEqual(result['failed'],1)
        status=self.call('status');self.assertEqual(status['events'],1)
        self.assertEqual(status['jobs'].get('retry'),1)
    def test_source_change_during_compilation_rejects_stale_result(self):
        path=self.base/'article.md';path.write_text('旧资料只允许五分钟。')
        self.call('ingest','--project','harbor','--path',str(path),'--source-id','doc')
        marker,release=self.runner(path.read_text(),wait=True);process=self.process_start()
        try:
            self.await_marker(marker);path.write_text('新资料只允许一分钟。')
            self.call('ingest','--project','harbor','--path',str(path),'--source-id','doc')
            release.touch();result=self.finish(process)
            self.assertEqual(result['failed'],1);self.assertFalse(self.query('harbor','五分钟'))
        finally:
            if process.poll() is None:process.kill();process.communicate()
    def test_capture_does_not_wait_for_compiler(self):
        self.source('harbor','后台原始资料。');marker,release=self.runner('后台原始资料。',wait=True)
        process=self.process_start()
        try:
            self.await_marker(marker);start=time.monotonic()
            self.source('harbor','前台及时保存。','new')
            self.assertLess(time.monotonic()-start,2)
            release.touch();self.finish(process)
        finally:
            if process.poll() is None:process.kill();process.communicate()
    def test_revoked_source_does_not_return_and_missing_can_return(self):
        path=self.base/'article.md';path.write_text('资料包含缓存边界。')
        s=self.call('ingest','--project','harbor','--path',str(path),'--source-id','doc')
        self.record(s,'harbor',path.read_text(),status='candidate')
        self.call('source-state','--project','harbor','--id','doc','--state','missing')
        self.assertFalse(self.query('harbor','缓存'))
        self.call('source-state','--project','harbor','--id','doc','--state','available')
        self.assertTrue(self.query('harbor','缓存'))
        self.call('source-state','--project','harbor','--id','doc','--state','revoked')
        self.call('ingest','--project','harbor','--path',str(path),'--source-id','doc')
        self.assertFalse(self.query('harbor','缓存'))
        self.call('source-state','--project','harbor','--id','doc','--state','available',ok=False)
    def test_withdrawn_evidence_not_leaked_through_raw_context(self):
        s=self.source('harbor','临时口令已经废止。');r=self.record(s,'harbor','临时口令已经废止。')
        self.call('withdraw','--project','harbor','--id',r['id'])
        context=self.call('context','--project','harbor','--text','口令')
        self.assertFalse(context['results']);self.assertFalse(context['recent_statements'])

    def test_malformed_fence_is_retryable_not_worker_crash(self):
        self.source('harbor','可靠保存模型错误。')
        script=self.base/'bad.py';script.write_text('print("``````")')
        (self.home/'config.json').write_text(json.dumps({'model_command':[sys.executable,str(script)]}))
        result=self.call('process','--limit','1')
        self.assertEqual(result['failed'],1)
        self.assertEqual(self.call('status')['jobs'].get('retry'),1)
