"""Bounded repair: thinking control and output/input limits (no model upgrade).

These tests read only the local sources. They never touch a production Vault,
credentials, or the network.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def _node():
    for name in ('node', 'nodejs'):
        try:
            subprocess.run([name, '--version'], capture_output=True, check=True)
            return name
        except (OSError, subprocess.CalledProcessError):
            continue
    return None


def _harness_source(tmp):
    """Strip TS-only syntax and wrap model_budget.ts in a Node harness."""
    source = (ROOT / 'scripts' / 'model_budget.ts').read_text()
    source = source.replace(': any', '')
    source = source.replace(' as Record<string, unknown>', '')
    source = source.replace(': any)', ')')
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


def run_budget(env):
    """Run the real callback in Node; returns the CompletedProcess."""
    node = _node()
    if not node:
        raise unittest.SkipTest('node not available')
    with tempfile.TemporaryDirectory() as d:
        js = _harness_source(Path(d))
        return subprocess.run([node, str(js)], env=env,
                              capture_output=True, text=True)


class BudgetExtensionTests(unittest.TestCase):
    """Run the real model_budget.ts callback in Node after stripping types."""

    def _js(self, tmp):
        return _harness_source(tmp)

    def _run(self, env):
        return run_budget(env)

    def test_absent_output_var_defaults_to_4096(self):
        env = dict(os.environ, PAYLOAD=json.dumps({'messages': []}))
        env.pop('TONGLE_MODEL_MAX_OUTPUT', None)
        run = self._run(env)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)['out']['max_tokens'], 4096)

    def test_non_finite_output_var_falls_back_to_4096(self):
        for bogus in ('NaN', 'Infinity', 'not-a-number'):
            with self.subTest(bogus=bogus):
                env = dict(os.environ, PAYLOAD=json.dumps({'messages': []}),
                           TONGLE_MODEL_MAX_OUTPUT=bogus)
                run = self._run(env)
                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertEqual(json.loads(run.stdout)['out']['max_tokens'], 4096)

    def test_output_cap_is_clamped_to_32768(self):
        env = dict(os.environ, PAYLOAD=json.dumps({'messages': []}),
                   TONGLE_MODEL_MAX_OUTPUT='999999')
        run = self._run(env)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)['out']['max_tokens'], 32768)

    def test_output_cap_integer_and_floor_clamped(self):
        env = dict(os.environ, PAYLOAD=json.dumps({'messages': []}),
                   TONGLE_MODEL_MAX_OUTPUT='100.9')
        run = self._run(env)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)['out']['max_tokens'], 256)

    def test_max_completion_tokens_variant_is_capped(self):
        env = dict(os.environ, PAYLOAD=json.dumps({'messages': [], 'max_completion_tokens': 10}),
                   TONGLE_MODEL_MAX_OUTPUT='4096')
        run = self._run(env)
        self.assertEqual(run.returncode, 0, run.stderr)
        out = json.loads(run.stdout)['out']
        self.assertEqual(out['max_completion_tokens'], 4096)
        self.assertNotIn('max_tokens', out)

    def test_input_guard_still_rejects_before_send(self):
        env = dict(os.environ,
                   PAYLOAD=json.dumps({'messages': [{'role': 'user', 'content': 'x' * 5000}]}),
                   TONGLE_MODEL_MAX_INPUT_BYTES='100')
        run = self._run(env)
        self.assertNotEqual(run.returncode, 0)
        self.assertEqual(run.stdout, '')  # handler never returned -> nothing sent
        line = json.loads([l for l in run.stderr.splitlines() if l.strip()][-1])
        self.assertEqual(line['code'], 'input_too_large')
        self.assertEqual(line['bytes_limit'], 100)

    def test_thinking_disabled_only_for_explicit_zhipu_off(self):
        base = json.dumps({'messages': []})
        glm = dict(os.environ, PAYLOAD=base, TONGLE_MODEL_PROVIDER='zhipu',
                   TONGLE_MODEL_MODEL='glm-5.3-flash')
        out = json.loads(self._run(dict(glm, TONGLE_MODEL_THINKING='off')).stdout)['out']
        self.assertEqual(out['thinking'], {'type': 'disabled'})
        # Same provider+model with a non-off level is untouched (adaptive mapping
        # is covered separately by AdaptiveThinkingTests).
        for provider, model, thinking in (('openai', 'glm-5.3-flash', 'off'),
                                          ('anthropic', 'glm-5.3-flash', 'off'),
                                          ('zhipu', 'glm-4.6', 'off')):
            with self.subTest(provider=provider, model=model, thinking=thinking):
                env = dict(os.environ, PAYLOAD=base, TONGLE_MODEL_PROVIDER=provider,
                           TONGLE_MODEL_MODEL=model, TONGLE_MODEL_THINKING=thinking)
                out = json.loads(self._run(env).stdout)['out']
                self.assertNotIn('thinking', out)

    def test_thinking_absent_without_env(self):
        env = dict(os.environ, PAYLOAD=json.dumps({'messages': []}))
        for key in ('TONGLE_MODEL_PROVIDER', 'TONGLE_MODEL_THINKING',
                    'TONGLE_MODEL_MODEL'):
            env.pop(key, None)
        out = json.loads(self._run(env).stdout)['out']
        self.assertNotIn('thinking', out)


class AdaptiveThinkingTests(unittest.TestCase):
    """GLM 5.3 Flash must map Pi --thinking onto output_config.effort.

    models.json sets no forceAdaptiveThinking, so Pi would otherwise take the
    legacy budget_tokens path. The mapping is strictly scoped to
    provider=zhipu, model=glm-5.3-flash and a non-off thinking level.
    """

    def _out(self, provider, model, thinking, payload=None):
        base = json.dumps({'messages': []} if payload is None else payload)
        env = dict(os.environ, PAYLOAD=base)
        for key, value in (('TONGLE_MODEL_PROVIDER', provider),
                           ('TONGLE_MODEL_MODEL', model),
                           ('TONGLE_MODEL_THINKING', thinking)):
            env.pop(key, None)
            if value is not None:
                env[key] = value
        return self._runner(env)

    def _runner(self, env):
        run = run_budget(env)
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout)['out']

    def test_low_and_minimal_map_to_low_effort(self):
        for thinking in ('minimal', 'low'):
            with self.subTest(thinking=thinking):
                out = self._out('zhipu', 'glm-5.3-flash', thinking)
                self.assertEqual(out['thinking'], {'type': 'adaptive'})
                self.assertEqual(out['output_config'], {'effort': 'low'})

    def test_medium_and_high_map_to_high_effort(self):
        for thinking in ('medium', 'high'):
            with self.subTest(thinking=thinking):
                out = self._out('zhipu', 'glm-5.3-flash', thinking)
                self.assertEqual(out['thinking'], {'type': 'adaptive'})
                self.assertEqual(out['output_config']['effort'], 'high')

    def test_existing_output_config_fields_are_preserved(self):
        out = self._out('zhipu', 'glm-5.3-flash', 'low',
                        payload={'messages': [],
                                 'output_config': {'format': 'json', 'effort': 'stale'}})
        self.assertEqual(out['output_config'], {'format': 'json', 'effort': 'low'})
        self.assertEqual(out['thinking'], {'type': 'adaptive'})

    def test_other_provider_or_model_is_untouched(self):
        other = [
            ('zhipu', 'glm-4.6', 'low'),
            ('openai', 'glm-5.3-flash', 'low'),
            ('anthropic', 'glm-5.3-flash', 'medium'),
            ('zhipu', None, 'high'),
        ]
        for provider, model, thinking in other:
            with self.subTest(provider=provider, model=model, thinking=thinking):
                out = self._out(provider, model, thinking)
                self.assertNotIn('thinking', out)
                self.assertNotIn('output_config', out)


class PiCommandTests(unittest.TestCase):
    """Inspect the arguments pi_model.py generates, without any provider call."""

    def _shim(self, tmp):
        """Executable fake `pi` that records argv and prints one JSON result."""
        out = tmp / 'argv.json'
        script = tmp / 'fake_pi.py'
        script.write_text(
            'import json,sys,os\n'
            'sys.stdin.read()\n'
            f'open({str(out)!r},"w").write(json.dumps(sys.argv[1:]))\n'
            'print(json.dumps({"type":"message_end","message":{"role":"assistant",'
            '"stopReason":"endTurn","content":[{"type":"text","text":"{}"}],"usage":{}}}))\n')
        shim = tmp / 'pi'
        shim.write_text('#!' + sys.executable + '\nimport runpy\nrunpy.run_path('
                        + repr(str(script)) + ', run_name="__main__")\n')
        shim.chmod(0o755)
        return shim, out

    def _invoke(self, tmp, extra=()):
        shim, out = self._shim(tmp)
        argv = [sys.executable, str(ROOT / 'scripts' / 'pi_model.py'),
                '--pi', str(shim), '--model', 'm', '--timeout', '30', *extra]
        run = subprocess.run(argv, input='{}', capture_output=True, text=True, cwd=tmp)
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(out.read_text())

    def test_generated_command_does_not_force_low(self):
        with tempfile.TemporaryDirectory() as d:
            argv = self._invoke(Path(d))
        self.assertIn('--thinking', argv)
        # zhipu default must be a supported (non-off) level: the live endpoint
        # rejects thinking-off with 400/1210.
        self.assertEqual(argv[argv.index('--thinking') + 1], 'low')

    def test_explicit_thinking_argument_is_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            argv = self._invoke(Path(d), ('--thinking', 'high'))
        self.assertEqual(argv[argv.index('--thinking') + 1], 'high')

    def test_provider_is_forwarded_on_command(self):
        with tempfile.TemporaryDirectory() as d:
            argv = self._invoke(Path(d), ('--provider', 'zhipu'))
        self.assertEqual(argv[argv.index('--provider') + 1], 'zhipu')


class PiEnvTests(unittest.TestCase):
    """pi_model.py must export the thinking/provider hint to the extension."""

    def _shim(self, tmp):
        out = tmp / 'env.json'
        script = tmp / 'fake_pi.py'
        script.write_text(
            'import json,sys,os\n'
            'sys.stdin.read()\n'
            f'open({str(out)!r},"w").write(json.dumps({{'
            '"provider":os.environ.get("TONGLE_MODEL_PROVIDER"),'
            '"model":os.environ.get("TONGLE_MODEL_MODEL"),'
            '"thinking":os.environ.get("TONGLE_MODEL_THINKING")}))\n'
            'print(json.dumps({"type":"message_end","message":{"role":"assistant",'
            '"stopReason":"endTurn","content":[{"type":"text","text":"{}"}],"usage":{}}}))\n')
        shim = tmp / 'pi'
        shim.write_text('#!' + sys.executable + '\nimport runpy\nrunpy.run_path('
                        + repr(str(script)) + ', run_name="__main__")\n')
        shim.chmod(0o755)
        return shim, out

    def test_env_hint_is_exported(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            shim, out = self._shim(tmp)
            argv = [sys.executable, str(ROOT / 'scripts' / 'pi_model.py'),
                    '--pi', str(shim), '--provider', 'zhipu',
                    '--model', 'glm-5.3-flash',
                    '--thinking', 'off', '--timeout', '30']
            run = subprocess.run(argv, input='{}', capture_output=True, text=True, cwd=tmp)
            self.assertEqual(run.returncode, 0, run.stderr)
            seen = json.loads(out.read_text())
        self.assertEqual(seen['provider'], 'zhipu')
        self.assertEqual(seen['thinking'], 'off')
        # The extension scopes adaptive/disabled thinking by exact model name.
        self.assertEqual(seen['model'], 'glm-5.3-flash')


class EvolutionContextTests(unittest.TestCase):
    """knowledge_on_demand_body_chars is a fixed, bounded body budget.

    Unlike the retired expanding-context knob, this never scales from corpus
    size: absent/garbage config falls back to the fixed default, and any value
    is clamped into a fixed floor/ceiling.
    """

    def _budget(self, cfg):
        sys.path.insert(0, str(ROOT))
        from tongle.evolution import Evolution
        return Evolution.__new__(Evolution)._body_budget(cfg)

    def test_default_and_bounds(self):
        self.assertEqual(self._budget({}), 52000)
        self.assertEqual(self._budget({'knowledge_on_demand_body_chars': 52000}), 52000)
        self.assertEqual(self._budget({'knowledge_on_demand_body_chars': 200000}), 200000)
        self.assertEqual(self._budget({'knowledge_on_demand_body_chars': 400000}), 400000)

    def test_clamped_to_bounds(self):
        self.assertEqual(self._budget({'knowledge_on_demand_body_chars': 10}), 26000)
        self.assertEqual(self._budget({'knowledge_on_demand_body_chars': 999999}), 400000)

    def test_non_integer_falls_back_to_default(self):
        for bogus in ('200000', 200000.5, True, None, [200000]):
            with self.subTest(bogus=bogus):
                self.assertEqual(
                    self._budget({'knowledge_on_demand_body_chars': bogus}), 52000)


if __name__ == '__main__':
    unittest.main()
