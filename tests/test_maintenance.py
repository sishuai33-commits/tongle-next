"""Supervisor operations tests with fake core, no model or live data."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
ROOT=Path(__file__).resolve().parents[1]

class Maintenance(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='tongle-maint-')
        self.home=Path(self.tmp.name)
        self.log=self.home/'calls.jsonl'
        self.fake=self.home/'fake'
        self.fake.write_text('#!'+sys.executable+'\n'+'''import sys,json,os
from pathlib import Path
with open(os.environ['TEST_LOG'],'a') as f:f.write(json.dumps(sys.argv[1:])+'\\n')
if sys.argv[1]=='ingest':
 if Path(os.environ['TEST_FAIL']).exists():print('{"error":"fake failure"}');sys.exit(1)
 print('{"source_id":"s1","version":1}')
else:print('{"ok":true}')
''')
        self.fake.chmod(0o755)
        self.env=dict(os.environ,TONGLE_TEST_CORE=str(self.fake),TEST_LOG=str(self.log),TEST_FAIL=str(self.home/'fail'))
    def tearDown(self):
        try:
            self.call('stop')
            until=time.monotonic()+5
            while time.monotonic()<until:
                if not self.call('status')['running']:break
                time.sleep(.1)
        finally:self.tmp.cleanup()
    def call(self,command):
        p=subprocess.run([sys.executable,str(ROOT/'scripts/maintain.py'),'--home',str(self.home),
                          '--interval','.2','--timeout','5',command],env=self.env,capture_output=True,text=True,timeout=10)
        self.assertEqual(p.returncode,0,p.stdout+p.stderr)
        return json.loads(p.stdout)
    def register(self):
        d=self.home/'maintenance';d.mkdir(exist_ok=True)
        src=self.home/'source.md';src.write_text('合成资料。')
        (d/'sources.json').write_text(json.dumps({'sources':[{'project':'p','source_id':'s1','path':str(src)}]}))
        return src
    def ingests(self):
        return [json.loads(s) for s in self.log.read_text().splitlines() if json.loads(s)[0]=='ingest']
    def test_incremental_and_unregistered_files(self):
        src=self.register();(self.home/'unregistered.md').write_text('忽略。')
        self.call('once');self.call('once');self.assertEqual(len(self.ingests()),1)
        src.write_text('修订资料。');self.call('once');self.assertEqual(len(self.ingests()),2)
    def test_failed_ingest_not_acknowledged(self):
        self.register();(self.home/'fail').touch()
        r=self.call('once');self.assertFalse(r['ok'])
        (self.home/'fail').unlink();self.call('once');self.assertEqual(len(self.ingests()),2)
    def test_start_twice_and_stop(self):
        first=self.call('start');self.assertTrue(first.get('started'))
        self.assertTrue(self.call('start').get('already_running'))
        self.assertTrue(self.call('status')['running'])
        self.assertTrue(self.call('stop')['stop_requested'])
        until=time.monotonic()+5
        while time.monotonic()<until and self.call('status')['running']:time.sleep(.1)
        self.assertFalse(self.call('status')['running'])
    def test_zero_exit_bad_json_is_failure(self):
        self.fake.write_text('#!'+sys.executable+'\nprint("not-json")\n')
        self.assertFalse(self.call('once')['ok'])

    def test_timeout_allows_core_to_clean_up(self):
        import importlib.util
        from types import SimpleNamespace
        spec=importlib.util.spec_from_file_location('maint_timeout',ROOT/'scripts/maintain.py')
        mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
        marker=self.home/'terminated'
        self.fake.write_text('#!'+sys.executable+'\n'+
            'import signal,time,sys\nfrom pathlib import Path\n'+
            'def stop(*args):\n Path('+repr(str(marker))+').write_text("cleaned")\n sys.exit(0)\n'+
            'signal.signal(signal.SIGTERM,stop)\ntime.sleep(30)\n')
        from unittest.mock import patch
        args=SimpleNamespace(python=sys.executable,home=self.home,root=ROOT,timeout=1.5)
        with patch.dict(os.environ,{'TONGLE_TEST_CORE':str(self.fake)}):
            with self.assertRaisesRegex(RuntimeError,'timed out'):
                mod.invoke(args,['process'])
        self.assertEqual(marker.read_text(),'cleaned')


class DailyBackup(unittest.TestCase):
    """The single source of truth gets copied on a schedule, without a second scheduler."""
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='tongle-backup-')
        self.home=Path(self.tmp.name)/'state';self.vault=Path(self.tmp.name)/'vault'
        subprocess.run([sys.executable,'-m','tongle','--home',str(self.home),'init',
                        '--vault',str(self.vault),'--evolving'],
                       cwd=ROOT,check=True,capture_output=True)
        self.env={k:v for k,v in os.environ.items() if k!='TONGLE_TEST_CORE'}
    def tearDown(self):self.tmp.cleanup()
    def cfg(self,**extra):
        data={'knowledge_mode':'evolving','model_command':['synthetic-stub']};data.update(extra)
        (self.home/'config.json').write_text(json.dumps(data))
    def once(self):
        p=subprocess.run([sys.executable,str(ROOT/'scripts/maintain.py'),'--home',str(self.home),
                          '--root',str(ROOT),'--python',sys.executable,'--interval','.2',
                          '--timeout','30','once'],
                         env=self.env,capture_output=True,text=True,timeout=120)
        self.assertEqual(p.returncode,0,p.stdout+p.stderr)
        return json.loads(p.stdout)
    def test_first_cycle_writes_an_archive_and_the_next_is_skipped(self):
        self.cfg(knowledge_backup_interval_seconds=86400)
        first=self.once()
        self.assertIn('written',first['backup'],first['backup'])
        self.assertEqual(len(list((self.home/'backups').glob('tongle-*'))),1)
        second=self.once()
        self.assertEqual(second['backup'].get('skipped'),'interval not reached')
        self.assertEqual(len(list((self.home/'backups').glob('tongle-*'))),1)
    def test_retention_keeps_the_newest_three(self):
        for _ in range(4):
            self.cfg(knowledge_backup_interval_seconds=1)
            self.once();time.sleep(1.05)
        archives=sorted(p.name for p in (self.home/'backups').glob('tongle-*'))
        self.assertEqual(len(archives),3,archives)
    def test_a_broken_backup_never_fails_the_cycle(self):
        self.cfg(knowledge_backup_interval_seconds=1)
        (self.home/'backups').write_text('not a directory')
        result=self.once()
        self.assertTrue(result['ok'])
        self.assertIn('error',result['backup'])
