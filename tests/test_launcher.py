"""Scoped setup and argv forwarding through a fake Pi executable."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
ROOT=Path(__file__).resolve().parents[1]

class Launcher(unittest.TestCase):
    def test_pi_mode_not_swallowed_as_model(self):
        with tempfile.TemporaryDirectory(prefix='tongle-launch-') as temp:
            base=Path(temp).resolve();home=base/'state';vault=base/'vault';binpath=base/'bin';binpath.mkdir()
            fake=binpath/'pi';fake.write_text('#!'+sys.executable+'\nimport sys,json\nprint(json.dumps(sys.argv[1:]))\n');fake.chmod(0o755)
            env=dict(os.environ,PATH=str(binpath)+os.pathsep+os.environ['PATH'])
            setup=[sys.executable,str(ROOT/'scripts/tongle.py'),'setup','--home',str(home),'--vault',str(vault),'--project','test']
            dry=subprocess.run(setup,env=env,text=True,capture_output=True,check=True)
            self.assertTrue(json.loads(dry.stdout)['dry_run']);self.assertFalse(home.exists())
            subprocess.run(setup+['--write'],env=env,text=True,capture_output=True,check=True)
            try:
                run=subprocess.run([str(home/'bin/tongle-pi'),'--provider','host-provider','--model','host-model','--mode','json','-p','hello'],
                                   env=env,text=True,capture_output=True,timeout=10)
                self.assertEqual(run.returncode,0,run.stderr)
                argv=json.loads(run.stdout)
                self.assertEqual(argv[argv.index('--model')+1],'host-model')
                self.assertEqual(argv[argv.index('--mode')+1],'json')
                self.assertEqual(argv[argv.index('--provider')+1],'host-provider')
                self.assertIn(str(ROOT/'adapters/pi.ts'),argv)
            finally:
                subprocess.run([sys.executable,str(ROOT/'scripts/tongle.py'),'stop','--home',str(home)],capture_output=True,timeout=10)
                until=time.monotonic()+5
                while time.monotonic()<until:
                    status=subprocess.run([sys.executable,str(ROOT/'scripts/maintain.py'),'--home',str(home),'status'],text=True,capture_output=True)
                    if not json.loads(status.stdout)['running']:break
                    time.sleep(.1)
