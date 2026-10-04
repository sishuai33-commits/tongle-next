"""Supervisor regressions for actual adapter code; fake core only, no live-host claim."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class AdapterRegressions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='adapter-regress-')
        self.base = Path(self.tmp.name)
        self.log = self.base / 'captures.jsonl'
        self.fail = self.base / 'fail'
        self.fake = self.base / 'fake-core'
        self.fake.write_text('#!' + sys.executable + '\n' + '''import sys,os,json
from pathlib import Path
args=sys.argv[1:]
if args[0]=='capture':
 if Path(os.environ['FAIL_FILE']).exists():
  print('{"error":"simulated unavailable"}');sys.exit(1)
 with open(os.environ['CAPTURE_LOG'],'a') as f:
  f.write(json.dumps({'argv':args,'text':sys.stdin.read()})+'\\n')
 print('{"source_id":"synthetic","version":1}')
else: print('{"results":[]}')
''')
        self.fake.chmod(0o755)
        self.env = dict(os.environ, TONGLE_NEXT_HOME=str(self.base), TONGLE_NEXT_PROJECT='synthetic',
                        TONGLE_NEXT_CLI=str(self.fake), TONGLE_NEXT_HOST='cc',
                        FAIL_FILE=str(self.fail), CAPTURE_LOG=str(self.log))
        self.transcript = self.base / 'session.jsonl'

    def tearDown(self):
        self.tmp.cleanup()

    def hook(self, event='Stop', **kw):
        payload = dict(hook_event_name=event, session_id='s1', cwd=str(self.base),
                       transcript_path=str(self.transcript), **kw)
        return subprocess.run([sys.executable, str(ROOT/'adapters/hook.py')],
                              input=json.dumps(payload), capture_output=True, text=True,
                              env=self.env, timeout=20)

    def messages(self):
        return [json.loads(s) for s in self.log.read_text().splitlines()] if self.log.exists() else []

    def entry(self, uid='a1'):
        return json.dumps({'type':'assistant','uuid':uid,'message':{'role':'assistant',
                           'content':[{'type':'text','text':'合成答复需要可靠保存。'}]}})+'\n'

    def test_partial_transcript_line_is_retried(self):
        line = self.entry()
        half = len(line)//2
        self.transcript.write_text(line[:half])
        self.hook()
        with self.transcript.open('a') as f:f.write(line[half:])
        self.hook()
        self.assertEqual(len(self.messages()), 1, 'partial JSONL cursor advanced past uncommitted line')

    def test_failed_capture_retries_same_transcript_line(self):
        self.transcript.write_text(self.entry())
        self.fail.touch()
        self.hook()
        self.assertEqual(len(self.messages()), 0)
        self.fail.unlink()
        self.hook()
        self.assertEqual(len(self.messages()), 1, 'cursor advanced despite failed durable capture')

    def test_same_text_two_actual_cc_turns_not_collapsed(self):
        self.transcript.write_text('')
        self.hook('UserPromptSubmit', prompt='继续。')
        # A real completed turn changes the transcript boundary before next prompt.
        self.transcript.write_text(self.entry())
        self.hook('UserPromptSubmit', prompt='继续。')
        ids=[m['argv'][m['argv'].index('--event-id')+1] for m in self.messages()]
        self.assertEqual(len(set(ids)), 2, 'identical text in distinct turns has same event id')

    def test_capture_zero_exit_without_valid_ack_warns(self):
        self.fake.write_text('#!'+sys.executable+'\nprint("not JSON")\n')
        result=self.hook('UserPromptSubmit', prompt='合成记录。')
        message=result.stdout+result.stderr
        self.assertTrue(any(s in message.lower() for s in ('not saved','failed','invalid','warning','无法','失败')),
                        'malformed zero-exit response reported as successful save')
