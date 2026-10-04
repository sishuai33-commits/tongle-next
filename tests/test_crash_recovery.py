"""Real SIGKILL process death; lease expiry is advanced explicitly for fast test."""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest
import test_acceptance as base
ROOT=Path(__file__).resolve().parents[1]

class CrashRecovery(unittest.TestCase):
    setUp=base.Acceptance.setUp
    tearDown=base.Acceptance.tearDown
    call=base.Acceptance.call
    source=base.Acceptance.source
    query=base.Acceptance.query
    def test_kill_mid_application_replays_persisted_output_without_new_model_call(self):
        self.source('harbor','第一条流程边界。第二条流程限制。')
        counter=self.base/'model-calls'
        runner=self.base/'runner.py'
        runner.write_text('import json\nfrom pathlib import Path\np=Path('+repr(str(counter))+')\np.write_text(str(int(p.read_text())+1) if p.exists() else "1")\nprint('+repr(json.dumps({'records':[{'key':'one','text':'第一条流程边界。','quote':'第一条流程边界。'},
                                                                                  {'key':'two','text':'第二条流程限制。','quote':'第二条流程限制。'}]}))+')\n')
        (self.home/'config.json').write_text(json.dumps({'model_command':[sys.executable,str(runner)],'auto_synthesize':False}))
        code='''import os,signal
from tongle.engine import Engine
from tongle.worker import process_jobs
engine=Engine(HOME)
original=engine._publish
def kill_after_first(rec):
 original(rec)
 os.kill(os.getpid(),signal.SIGKILL)
engine._publish=kill_after_first
process_jobs(engine,1)
'''.replace('HOME',repr(str(self.home)))
        process=subprocess.run([sys.executable,'-c',code],cwd=ROOT,capture_output=True,text=True,timeout=15)
        self.assertLess(process.returncode,0,process.stdout+process.stderr)
        with sqlite3.connect(self.home/'state.sqlite3') as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM records').fetchone()[0],1)
            self.assertIsNotNone(db.execute('SELECT result_json FROM jobs').fetchone()[0])
            db.execute('UPDATE jobs SET lease_until=0')  # simulate lease elapsed, not power loss
        result=self.call('process','--limit','1')
        self.assertEqual(result['processed'],1)
        self.assertEqual(len(self.query('harbor','流程')),2)
        self.assertEqual(counter.read_text(),'1')
