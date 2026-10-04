"""Behavioral tests for the worker request-count guard in model_budget.ts.

Runs the real installed Node on the real extension with a minimal mock Pi API.
No provider network and no prompts/secrets are involved: only the extension's
own synchronous exit + bounded stderr line are exercised. The hook is invoked
directly, so Pi's internal HTTP retries are out of scope (see the extension).

Skipped when no ``node`` is on PATH. Assertions never print the environment.
"""
import json
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
EXT = REPO / "scripts" / "model_budget.ts"
NODE = shutil.which("node")


def run_node(extension, mock_body, env_extra):
    """Run a Node harness with the extension mounted as a Pi extension."""
    harness = textwrap.dedent(
        """
        import fs from 'node:fs';
        import { pathToFileURL } from 'node:url';
        const ext = await import(pathToFileURL(process.argv[2]).href);
        const calls = [];
        const pi = { on: (name, handler) => { calls.push({ name, handler }); } };
        ext.default(pi);
        __MOCK__
        """
    ).replace("__MOCK__", mock_body)
    with tempfile.TemporaryDirectory(prefix="tongle-budget-") as tmp:
        path = Path(tmp) / "harness.mjs"
        path.write_text(harness)
        return subprocess.run(
            [NODE, str(path), str(extension)],
            capture_output=True, text=True, timeout=30, env=env_extra,
        )


def base_env(extra=None):
    import os
    env = {k: v for k, v in os.environ.items()
           if k in ("PATH", "HOME", "SystemRoot", "TMPDIR")}
    env.update(extra or {})
    return env


class RequestBudget(unittest.TestCase):
    def setUp(self):
        if NODE is None:
            self.skipTest("node not installed")

    def test_first_allowed_second_exits_8_code_request_limit(self):
        mock = (
            "const h = calls[0].handler;\n"
            "const first = h({ payload: { max_tokens: 10 } });\n"
            "fs.writeSync(1, JSON.stringify({ first: first.max_tokens }) + '\\n');\n"
            "h({ payload: {} });\n"  # second call must terminate
            "fs.writeSync(1, 'SENT_AFTER_LIMIT\\n');\n"  # unreachable
        )
        p = run_node(EXT, mock, base_env({"TONGLE_MODEL_MAX_REQUESTS": "1"}))
        self.assertEqual(p.returncode, 8)
        self.assertNotIn("SENT_AFTER_LIMIT", p.stdout)
        self.assertEqual(json.loads(p.stdout.strip().splitlines()[0])["first"], 4096)
        line = json.loads(p.stderr.strip().splitlines()[-1])
        self.assertEqual(line["code"], "request_limit")
        self.assertEqual(set(line), {"error", "code", "limit"})
        self.assertEqual(line["limit"], 1)

    def test_invalid_max_fails_closed_before_any_request(self):
        mock = "fs.writeSync(1, 'HOOK_REGISTERED\\n');\n"
        p = run_node(EXT, mock, base_env({"TONGLE_MODEL_MAX_REQUESTS": "0"}))
        self.assertEqual(p.returncode, 8)
        self.assertNotIn("HOOK_REGISTERED", p.stdout)
        line = json.loads(p.stderr.strip().splitlines()[-1])
        self.assertEqual((line["code"], line["limit"]), ("request_limit", None))

    def test_output_cap_reaches_handler(self):
        mock = (
            "const out = calls[0].handler({ payload: { max_tokens: 999999 } });\n"
            "console.log(JSON.stringify(out.max_tokens));\n"
        )
        p = run_node(EXT, mock, base_env({
            "TONGLE_MODEL_MAX_REQUESTS": "1", "TONGLE_MODEL_MAX_OUTPUT": "1234"}))
        self.assertEqual(p.returncode, 0)
        self.assertEqual(json.loads(p.stdout.strip()), 1234)

    def test_message_whitelist_is_bounded(self):
        mock = ("calls[0].handler({ payload: {} });\n"
                "calls[0].handler({ payload: {} });\n"
                "calls[0].handler({ payload: {} });\n")
        p = run_node(EXT, mock, base_env({"TONGLE_MODEL_MAX_REQUESTS": "2"}))
        self.assertEqual(p.returncode, 8)
        line = json.loads(p.stderr.strip().splitlines()[-1])
        self.assertIn(line["code"], ("request_limit",))
        self.assertNotIn("payload", json.dumps(line))
        self.assertNotIn(EXT.name, line.get("error", ""))

    def test_stderr_failure_cannot_make_hook_fail_open(self):
        mock = ('fs.writeSync=()=>{throw new Error("ENOSPC")};'
                'try{calls[0].handler({payload:{}});calls[0].handler({payload:{}})}catch{};'
                'console.log("SENT_AFTER_LIMIT");')
        p=run_node(EXT,mock,base_env({"TONGLE_MODEL_MAX_REQUESTS":"1"}))
        self.assertEqual(p.returncode,8)
        self.assertNotIn('SENT_AFTER_LIMIT',p.stdout)


if __name__ == "__main__":
    unittest.main()
