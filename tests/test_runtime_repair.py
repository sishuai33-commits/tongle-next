"""Runtime-repair regressions from the independent review.

These are synthetic and offline: no live provider, credentials, or assets. They
pin the bounded model-failure protocol, the budget worker's fail-closed exit,
the pi runner's truncation/usage fidelity, and the status snapshot behaviour.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from tongle.model_errors import (
    ModelError, from_exception, normalize_usage, usage_totals, known,
)
from tongle.engine import Engine
from tongle.evolution import Evolution

ROOT = Path(__file__).resolve().parents[1]
NODE = None


def _node():
    global NODE
    if NODE is None:
        for candidate in ('node', 'nodejs'):
            try:
                probe = subprocess.run([candidate, '--version'], capture_output=True, text=True)
                if probe.returncode == 0:
                    NODE = candidate
                    break
            except FileNotFoundError:
                continue
    return NODE


class ModelErrorProtocolTests(unittest.TestCase):
    def test_subclasses_runtime_error_for_outer_capture(self):
        # Outer layers (Evolution.process etc.) catch RuntimeError; a model
        # failure must not escape that net.
        self.assertTrue(issubclass(ModelError, RuntimeError))
        with self.assertRaises(RuntimeError):
            raise ModelError('provider_failure')

    def test_summary_never_carries_arbitrary_text(self):
        exc = ModelError('provider_failure', code='provider_error')
        self.assertFalse(hasattr(exc, 'detail'))
        self.assertNotIn('secret', exc.summary())
        # Even a caller trying to smuggle text finds no free-form field.
        with self.assertRaises(TypeError):
            ModelError('timeout', detail='SECRET TOKEN')  # type: ignore[call-arg]

    def test_structured_byte_context_is_numeric_only(self):
        exc = ModelError('input_limited', code='input_too_large',
                         bytes_actual=130000, bytes_limit=120000)
        self.assertEqual(exc.summary(), 'input_limited; input_too_large; bytes=130000>120000')
        self.assertEqual(exc.safe['bytes_actual'], 130000)
        self.assertIsNone(ModelError('timeout', bytes_actual='nope').bytes_actual)  # type: ignore[arg-type]

    def test_from_exception_never_copies_raw_text(self):
        exc = from_exception(ValueError('secret material here'))
        self.assertNotIn('secret', exc.summary())
        self.assertEqual(exc.category, 'provider_failure')

    def test_partial_cache_usage_is_not_padded_to_full(self):
        # Missing cache fields must not be treated as zero input total.
        self.assertEqual(usage_totals({'input': 100}), (None, None))
        self.assertEqual(usage_totals({'input': 100, 'cacheWrite': 5}), (None, None))
        # Complete triple -> real sum.
        self.assertEqual(usage_totals({'input': 100, 'cacheRead': 20, 'cacheWrite': 5, 'output': 7}), (125, 7))
        self.assertEqual(usage_totals({'output': 7}), (None, 7))

    def test_unknown_is_never_zero(self):
        self.assertFalse(known(0) is False)  # zero is a known count
        self.assertFalse(known(None))
        self.assertFalse(known(True))
        self.assertFalse(known(-1))
        self.assertEqual(normalize_usage({'input': 0, 'output': None, 'cacheRead': -4}), {'input': 0})


class BudgetWorkerTests(unittest.TestCase):
    """Run the real TS callback in Node after stripping the type-only import."""

    def _js(self, tmp):
        source = (ROOT / 'scripts' / 'model_budget.ts').read_text()
        source = source.replace('import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";', '')
        # Drop the remaining TypeScript-only annotations/casts used in this file.
        source = source.replace(': any', '')
        source = source.replace(' as Record<string, unknown>', '')
        source = source.replace(': any)', ')')
        # Node can run the import; the TS-only `export default` becomes a plain
        # function we then invoke with the fake pi so the handler is registered.
        source = source.replace('export default function(pi)', 'const register = function(pi)')
        harness = (
            "let handler=null;const pi={on:(_n,fn)=>{handler=fn;}};\n"
            + source
            + "\nregister(pi);\n"
            + "const payload=JSON.parse(process.env.PAYLOAD);\n"
            + "let sent=false;const out=handler({payload});sent=true;\n"
            + "process.stdout.write(JSON.stringify({sent,out}));\n"
        )
        js = tmp / 'budget.mjs'
        js.write_text(harness)
        return js

    def test_over_limit_exits_before_sent_flag(self):
        if not _node():
            self.skipTest('node not available')
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            js = self._js(tmp)
            env = dict(os.environ, PAYLOAD=json.dumps({'messages': [{'role': 'user', 'content': 'x' * 5000}]}),
                       TONGLE_MODEL_MAX_INPUT_BYTES='100')
            run = subprocess.run([_node(), str(js)], env=env, capture_output=True, text=True)
            self.assertNotEqual(run.returncode, 0)
            self.assertEqual(run.stdout, '')  # sent flag never written
            line = json.loads([l for l in run.stderr.splitlines() if l.strip()][-1])
            self.assertEqual(line['code'], 'input_too_large')
            self.assertEqual(line['bytes_limit'], 100)
            self.assertGreater(line['bytes_actual'], 100)

    def test_normal_payload_preserves_content_and_caps_output(self):
        if not _node():
            self.skipTest('node not available')
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            js = self._js(tmp)
            env = dict(os.environ, PAYLOAD=json.dumps({'messages': [{'role': 'user', 'content': '保持内容'}],
                                                       'temperature': 0.2}),
                       TONGLE_MODEL_MAX_OUTPUT='777')
            run = subprocess.run([_node(), str(js)], env=env, capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            out = json.loads(run.stdout)
            self.assertTrue(out['sent'])
            self.assertEqual(out['out']['messages'], [{'role': 'user', 'content': '保持内容'}])
            self.assertEqual(out['out']['temperature'], 0.2)
            self.assertEqual(out['out']['max_tokens'], 777)


class PiRunnerTests(unittest.TestCase):
    def _shim(self, tmp, body):
        """An executable `pi` whose stdout is the given Python body (reads stdin)."""
        script = tmp / 'fake_pi.py'
        script.write_text('import json,sys\nsys.stdin.read()\n' + body)
        shim = tmp / 'pi'
        shim.write_text('#!' + sys.executable + '\nimport runpy\nrunpy.run_path(' + repr(str(script)) + ', run_name="__main__")\n')
        shim.chmod(0o755)
        return shim

    def _invoke(self, tmp, shim):
        argv = [sys.executable, str(ROOT / 'scripts' / 'pi_model.py'),
                '--pi', str(shim), '--model', 'm', '--timeout', '30']
        return subprocess.run(argv, input='{}', capture_output=True, text=True, cwd=tmp)

    def _stderr_usage(self, run):
        observed = {}
        for line in run.stderr.splitlines():
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if isinstance(msg, dict) and isinstance(msg.get('usage'), dict):
                observed = msg['usage']
        return observed

    def test_import_path_is_reliable_outside_repo(self):
        with tempfile.TemporaryDirectory() as d:
            run = subprocess.run(
                [sys.executable, '-c',
                 "import sys;sys.path.insert(0,sys.argv[1]);"
                 "import runpy;runpy.run_path(sys.argv[2],run_name='pi_model_import');"
                 "from tongle.model_errors import ModelError;print('ok')",
                 str(ROOT), str(ROOT / 'scripts' / 'pi_model.py')],
                cwd=d, capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn('ok', run.stdout)

    def test_length_stop_is_truncation_even_when_json_valid(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            shim = self._shim(tmp,
                "msg={'role':'assistant','stopReason':'length','usage':{'input':10,'output':20},"
                "'content':[{'type':'text','text':json.dumps({'records':[]})}]}\n"
                "print(json.dumps({'type':'message_end','message':msg}))\n")
            run = self._invoke(tmp, shim)
            self.assertNotEqual(run.returncode, 0)
            self.assertEqual(run.stdout.strip(), '')
            self.assertEqual(self._stderr_usage(run), {'input': 10, 'output': 20})

    def test_usage_fidelity_and_error_usage_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            shim = self._shim(tmp,
                "msg={'role':'assistant','stopReason':'error','usage':{'input':10,'output':20},"
                "'content':[{'type':'text','text':'partial'}]}\n"
                "print(json.dumps({'type':'message_end','message':msg}))\n"
                "sys.exit(1)\n")
            run = self._invoke(tmp, shim)
            self.assertNotEqual(run.returncode, 0)
            self.assertEqual(self._stderr_usage(run), {'input': 10, 'output': 20})

    def test_partial_usage_stays_partial(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            shim = self._shim(tmp,
                "msg={'role':'assistant','stopReason':'error','usage':{'input':10},"
                "'content':[{'type':'text','text':'partial'}]}\n"
                "print(json.dumps({'type':'message_end','message':msg}))\n"
                "sys.exit(1)\n")
            run = self._invoke(tmp, shim)
            self.assertNotEqual(run.returncode, 0)
            usage = self._stderr_usage(run)
            self.assertEqual(usage, {'input': 10})
            self.assertNotIn('output', usage)


class WorkerErrorUsageTests(unittest.TestCase):
    def test_parse_error_preserves_observed_usage(self):
        from tongle.worker import invoke_json
        with tempfile.TemporaryDirectory() as d:
            script = Path(d) / 'bad.py'
            # Valid stderr usage line, invalid JSON on stdout.
            script.write_text(
                "import json,sys\n"
                "sys.stdin.read()\n"
                "print(json.dumps({'runner':'r','usage':{'input':11,'cacheRead':3,'cacheWrite':1,'output':5}}),file=sys.stderr)\n"
                "print('not json')\n")
            with self.assertRaises(ModelError) as ctx:
                invoke_json([sys.executable, str(script)], '{}', 30)
            exc = ctx.exception
            self.assertEqual(exc.category, 'invalid_json')
            self.assertEqual(exc.usage, {'input': 11, 'cacheRead': 3, 'cacheWrite': 1, 'output': 5})

    def test_real_subprocess_timeout_is_model_error_with_usage(self):
        from tongle.worker import invoke_json
        with tempfile.TemporaryDirectory() as d:
            script = Path(d) / 'slow.py'
            script.write_text(
                "import json,sys,time\n"
                "sys.stdin.read()\n"
                "print(json.dumps({'runner':'r','usage':{'input':9,'output':4}}),file=sys.stderr,flush=True)\n"
                "time.sleep(30)\n")
            with self.assertRaises(ModelError) as ctx:
                invoke_json([sys.executable, str(script)], '{}', 1)
            exc = ctx.exception
            self.assertEqual(exc.category, 'timeout')
            self.assertEqual(exc.code, 'timeout')
            # Usage flushed before the deadline is preserved, not discarded.
            self.assertEqual(exc.usage, {'input': 9, 'output': 4})

    def test_input_rejection_is_classified_and_zero_usage(self):
        from tongle.worker import invoke_json
        with tempfile.TemporaryDirectory() as d:
            script = Path(d) / 'reject.py'
            # Bounded runner emits a structured rejection with explicit zero
            # usage: no provider request was sent, so zero is real.
            script.write_text(
                "import json,sys\n"
                "sys.stdin.read()\n"
                "print(json.dumps({'error':'x','category':'input_limited','code':'input_too_large',"
                "'bytes_actual':130000,'bytes_limit':120000,'usage':{'input':0,'output':0}}),file=sys.stderr)\n"
                "sys.exit(1)\n")
            with self.assertRaises(ModelError) as ctx:
                invoke_json([sys.executable, str(script)], '{}', 30)
            exc = ctx.exception
            self.assertEqual(exc.category, 'input_limited')
            self.assertEqual(exc.code, 'input_too_large')
            self.assertEqual(exc.usage, {'input': 0, 'output': 0})
            self.assertEqual(exc.bytes_actual, 130000)
            self.assertEqual(exc.bytes_limit, 120000)

    def test_oversize_output_is_output_truncated_not_input_limited(self):
        from tongle.worker import invoke_json
        with tempfile.TemporaryDirectory() as d:
            script = Path(d) / 'big.py'
            script.write_text("import sys\nsys.stdin.read()\nprint('x'*200001)\n")
            with self.assertRaises(ModelError) as ctx:
                invoke_json([sys.executable, str(script)], '{}', 30)
            self.assertEqual(ctx.exception.category, 'output_truncated')
            self.assertEqual(ctx.exception.code, 'output_too_large')

    def test_empty_output_prefers_empty_over_provider_failure(self):
        from tongle.worker import invoke_json
        with tempfile.TemporaryDirectory() as d:
            script = Path(d) / 'empty.py'
            script.write_text("import sys\nsys.stdin.read()\n")
            with self.assertRaises(ModelError) as ctx:
                invoke_json([sys.executable, str(script)], '{}', 30)
            self.assertEqual(ctx.exception.category, 'empty_output')


class StatusSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.e = Engine(self.root / 'state')
        self.e.init(self.root / 'vault')
        self.addCleanup(self.e.store.close)
        # Building Evolution creates the knowledge_* tables the snapshot reads.
        Evolution(self.e)
        (self.e.home / 'config.json').write_text(
            json.dumps({'knowledge_mode': 'evolving', 'model_command': ['stub'], 'knowledge_quiet_seconds': 0}))

    def _call(self, input_tokens, output_tokens, error=None):
        # The snapshot accounts by the current UTC day, so the fixture must use
        # today's day string; a literal date only passes on that one day.
        day = time.strftime('%Y-%m-%d', time.gmtime())
        self.e.db.execute(
            "INSERT INTO knowledge_calls(id,work_id,stage,day,reserved,actual_input,actual_output,elapsed,error,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            ('c' + str(self.e.db.total_changes), 'w', 'compile', day, 1,
             input_tokens, output_tokens, 0.1, error, 0.0))

    def test_snapshot_uses_ledgers_reports_error_rate_and_beijing_time(self):
        from tongle.views import status_snapshot
        self._call(100, 20)
        self._call(200, 30, error='provider_failure')
        self.e.db.execute("INSERT INTO knowledge_work(id,project,session,inputs,state,created_at) VALUES('w','p','s','[]','done',0)")
        snap = status_snapshot(self.e, {'active': 2, 'conflict': 1})
        self.assertEqual(snap['calls'], 2)
        self.assertEqual(snap['failures'], 1)
        self.assertAlmostEqual(snap['error_rate'], 50.0, places=1)
        self.assertEqual(snap['counts'], {'active': 2, 'conflict': 1})
        self.assertRegex(snap['beijing_time'], r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$')

    def test_refresh_reports_error_rate_in_page_and_preserves_manual(self):
        from tongle.views import refresh
        self._call(5, 5, error='timeout')
        refresh(self.e)
        page = self.root / 'vault' / '运行情况.md'
        self.assertTrue(page.is_file())
        text = page.read_text()
        self.assertIn('错误率', text)
        self.assertIn('北京时间', text)
        # A hand-edited generated view is preserved, not overwritten.
        page.write_text('人工改动')
        got = refresh(self.e)
        self.assertIn('运行情况.md', got['manual_views_preserved'])
        self.assertEqual(page.read_text(), '人工改动')


if __name__ == '__main__':
    unittest.main()
