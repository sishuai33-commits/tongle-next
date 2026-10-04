"""Native hook protocol callbacks against the real core, no host benefit claim."""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from tongle import evaluate

REPO=Path(__file__).resolve().parents[1]


class CodexReceipt(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve()
        self.case=json.loads((REPO/'examples/evaluation/status-boundary.json').read_text())
        evaluate.prepare(self.root/'instance',self.case)
        self.home=self.root/'instance/home'
        self.env={k:v for k,v in os.environ.items() if not k.startswith('TONGLE_')}
        self.env.update(TONGLE_NEXT_HOME=str(self.home),TONGLE_NEXT_ROOT=str(REPO),
                        TONGLE_NEXT_PYTHON=sys.executable,TONGLE_NEXT_PROJECT=self.case['project'],TONGLE_NEXT_HOST='codex')

    def hook(self,event,turn=None,**kw):
        value={'hook_event_name':event,'session_id':'codex-protocol','cwd':str(self.root),**kw}
        if turn is not None:value['turn_id']=turn
        p=subprocess.run([sys.executable,str(REPO/'adapters/hook.py')],input=json.dumps(value),text=True,
                         capture_output=True,env=self.env,timeout=30)
        self.assertEqual(p.returncode,0)
        return json.loads(p.stdout)

    def rows(self):
        db=sqlite3.connect(self.home/'state.sqlite3');db.row_factory=sqlite3.Row
        try:return [dict(r) for r in db.execute('SELECT * FROM knowledge_consumption ORDER BY created_at')]
        finally:db.close()

    def test_restart_and_interleaved_turns_bind_exactly(self):
        self.hook('UserPromptSubmit','one',prompt='青岚巡检状态')
        self.hook('UserPromptSubmit','two',prompt='白鹭验证状态')
        self.hook('Stop','one',last_assistant_message='First answer.')
        self.assertEqual([r['binding'] for r in self.rows()],['receipt','legacy_recent'])
        self.assertTrue(self.rows()[0]['output_source']);self.assertIsNone(self.rows()[1]['output_source'])
        self.hook('Stop','one',last_assistant_message='First answer.')
        self.hook('Stop','two',last_assistant_message='Second answer.')
        self.assertEqual([r['binding'] for r in self.rows()],['receipt','receipt'])

    def test_missing_or_ambiguous_turn_never_uses_latest(self):
        self.hook('UserPromptSubmit','same',prompt='青岚巡检状态')
        self.hook('UserPromptSubmit','same',prompt='青岚巡检状态')
        self.hook('Stop','same',last_assistant_message='Ambiguous answer.')
        self.hook('Stop',None,last_assistant_message='No native turn identity.')
        self.assertTrue(all(r['output_source'] is None for r in self.rows()))


if __name__=='__main__':unittest.main()
