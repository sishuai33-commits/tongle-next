"""Outage breaker and gate accounting for the daily model budget.

Two behaviours that an outage used to turn into a day-long stall:
  1. consecutive *transient* upstream failures pause the queue instead of
     retrying every cycle and booking unknown-usage reservations;
  2. a finished call whose usage was never reported counts at a bounded
     nominal value, while an in-flight call still reserves in full.
The ledger keeps `actual_*` NULL either way: unknown is never rewritten as a
measured zero.
"""
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import test_evolution_consumer as base
try:  # sibling-tests import under ``unittest discover -s tests``
    from fact_review_stub import with_fact_audit
except ImportError:  # pragma: no cover - package-import invocation
    from tests.fact_review_stub import with_fact_audit
from tongle.evolution import BudgetWait
from tongle.model_errors import ModelError


class OutageBreakerTests(base.EvolutionConsumerTests):
    def _transient(self):
        raise ModelError("provider_failure", code="provider_error")

    def test_consecutive_transient_failures_open_the_breaker(self):
        for i in range(3):
            self.capture("第%d批材料。" % i, "e%d" % i, session="s%d" % i)
        calls = []

        def boom(*args, **kwargs):
            calls.append(1)
            self._transient()

        with patch("tongle.worker.invoke_json", side_effect=boom):
            self.ev.process(force=True, limit=3)
        self.assertEqual(len(calls), 3)
        self.assertTrue(self.ev.breaker_open())
        state = self.ev._breaker()
        self.assertEqual(state["streak"], 3)
        self.assertEqual(state["category"], "provider_failure")
        # An open breaker must not spend another call.
        with patch("tongle.worker.invoke_json", side_effect=boom) as after:
            result = self.ev.process(force=True, limit=3)
            after.assert_not_called()
        self.assertTrue(result["breaker_paused"])
        self.assertGreater(result["breaker_until"], time.time())

    def test_model_content_error_does_not_trip_the_breaker(self):
        self.capture("先完成方案。")
        with patch("tongle.worker.invoke_json",
                   side_effect=ValueError("invalid knowledge proposal")):
            self.ev.process(force=True)
        self.assertFalse(self.ev.breaker_open())
        self.assertEqual(self.ev._breaker()["streak"], 0)

    def test_a_successful_batch_clears_the_breaker(self):
        self.cfg["knowledge_breaker_cooldown_seconds"] = 1
        self.write_cfg()
        for _ in range(3):
            self.ev.breaker_trip("provider_failure")
        self.assertTrue(self.ev.breaker_open())
        time.sleep(1.05)                      # cooldown expires on its own
        self.assertFalse(self.ev.breaker_open())
        self.capture("先完成方案，实施暂缓。")
        self.assertEqual(self.process()["processed"], 1)
        self.assertEqual(self.ev._breaker()["streak"], 0)

    def test_breaker_state_survives_a_new_process_object(self):
        self.ev.breaker_trip("timeout")
        fresh = type(self.ev)(self.e)         # launchd starts a new process each cycle
        self.assertEqual(fresh._breaker()["streak"], 1)


    def test_content_failures_get_bounded_attempts(self):
        self.cfg["knowledge_max_attempts"] = 3
        self.write_cfg()
        self.capture("材料。")
        calls = []

        def invalid(*args, **kwargs):
            calls.append(1)
            raise ValueError("review findings must be an empty list")

        with patch("tongle.worker.invoke_json", side_effect=invalid):
            for _ in range(5):
                self.ev.process(force=True)
                with self.e.transaction():
                    self.e.db.execute("UPDATE knowledge_work SET next_at=0")
        self.assertEqual(len(calls), 3)          # bounded, then parked
        row = dict(self.e.db.execute(
            "SELECT state,attempts FROM knowledge_work").fetchone())
        self.assertEqual((row["state"], row["attempts"]), ("failed", 3))

    def test_retry_ceiling_is_bounded_by_config(self):
        self.cfg["knowledge_max_attempts"] = 1
        self.write_cfg()
        self.capture("材料。")
        calls = []

        def invalid(*args, **kwargs):
            calls.append(1)
            raise ValueError("review findings must be an empty list")

        with patch("tongle.worker.invoke_json", side_effect=invalid):
            self.ev.process(force=True)
        self.assertEqual(len(calls), 1)
        self.assertEqual(dict(self.e.db.execute(
            "SELECT state FROM knowledge_work").fetchone())["state"], "failed")


    def test_a_rejected_proposal_is_recompiled_on_retry(self):
        """A stored proposal/review must not make a retry a no-op.

        Regression: retries replayed the cached proposal and the rejecting
        review, so a validation failure failed all attempts without a single new
        model call.
        """
        self.capture("先完成方案，实施暂缓。")
        calls = []

        def reject(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            calls.append("verify" if "proposal" in data else "compile")
            if "proposal" in data:
                return {"approved": False, "findings": [{"detail": "未保留实施暂缓的条件"}]}, {}
            source = data["sources"][-1]
            return {"operations": [{"op": "create", "key": "交付边界", "kind": "memory",
                                    "topic": "方案", "title": "交付范围",
                                    "body": source["text"],
                                    "evidence": [{"source_id": source["source_id"],
                                                  "version": source["version"],
                                                  "quote": source["text"]}],
                                    "reason": "用户决定"}]}, {}

        with patch("tongle.worker.invoke_json", side_effect=with_fact_audit(reject)):
            for _ in range(3):
                self.ev.process(force=True)
                with self.e.transaction():
                    self.e.db.execute("UPDATE knowledge_work SET next_at=0")
        self.assertEqual(calls.count("verify"), 3)      # re-asked every attempt
        self.assertEqual(calls.count("compile"), 3)
        row = dict(self.e.db.execute(
            "SELECT state,proposal,review FROM knowledge_work").fetchone())
        self.assertEqual(row["state"], "failed")
        self.assertIsNone(row["proposal"])
        self.assertIsNone(row["review"])

    def test_transient_failure_keeps_the_cached_proposal(self):
        self.capture("先完成方案。")
        calls = []

        def flaky(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            calls.append("verify" if "proposal" in data else "compile")
            if "proposal" in data:
                raise ModelError("timeout", code="timeout")
            source = data["sources"][-1]
            return {"operations": [{"op": "create", "key": "边界", "kind": "memory",
                                    "topic": "方案", "title": "边界", "body": source["text"],
                                    "evidence": [{"source_id": source["source_id"],
                                                  "version": source["version"],
                                                  "quote": source["text"]}],
                                    "reason": "用户决定"}]}, {}

        with patch("tongle.worker.invoke_json", side_effect=with_fact_audit(flaky)):
            self.ev.process(force=True)
        row = dict(self.e.db.execute(
            "SELECT state,proposal FROM knowledge_work").fetchone())
        self.assertEqual(row["state"], "retry")
        self.assertIsNotNone(row["proposal"])           # cached answer survives a timeout


class GateAccountingTests(base.EvolutionConsumerTests):
    def _cfg(self, **extra):
        cfg = {"knowledge_mode": "evolving", "model_command": ["synthetic-stub"],
               "knowledge_daily_token_budget": 1_000_000,
               "knowledge_daily_call_budget": 180,
               "knowledge_unknown_nominal_tokens": 40000}
        cfg.update(extra)
        return cfg

    def _insert_call(self, cid, reserved, elapsed):
        day = time.strftime("%Y-%m-%d", time.gmtime())
        with self.e.transaction():
            self.e.db.execute(
                "INSERT INTO knowledge_calls(id,work_id,stage,day,reserved,elapsed,created_at)"
                " VALUES(?,?,?,?,?,?,?)", (cid, "w", "compile", day, reserved, elapsed, time.time()))

    def test_finished_unknown_is_bounded_in_flight_is_not(self):
        cfg = self._cfg()
        self._insert_call("finished-unknown", 5_000_000, 1.0)
        with patch("tongle.worker.invoke_json",
                   return_value=({"operations": []}, {"input": 1, "output": 1})):
            # Full reservation would exceed the budget; the nominal must not.
            self.ev._call({"id": "probe"}, "compile", {"task": "x"}, cfg)
        self._insert_call("still-running", 2_000_000, None)
        with patch("tongle.worker.invoke_json",
                   return_value=({"operations": []}, {"input": 1, "output": 1})):
            with self.assertRaises(BudgetWait):
                self.ev._call({"id": "probe2"}, "compile", {"task": "x"}, cfg)

    def test_measured_usage_still_counts_in_full(self):
        cfg = self._cfg()
        day = time.strftime("%Y-%m-%d", time.gmtime())
        with self.e.transaction():
            self.e.db.execute(
                "INSERT INTO knowledge_calls(id,work_id,stage,day,reserved,actual_input,"
                "actual_output,elapsed,created_at) VALUES('measured','w','compile',?,?,?,?,1.0,?)",
                (day, 5_000_000, 950_000, 100_000, time.time()))
        with patch("tongle.worker.invoke_json",
                   return_value=({"operations": []}, {"input": 1, "output": 1})):
            with self.assertRaises(BudgetWait):
                self.ev._call({"id": "probe"}, "compile", {"task": "x"}, cfg)

    def test_unknown_stays_unknown_in_the_ledger(self):
        cfg = self._cfg()
        calls = []

        def partial(command, prompt, timeout, env=None):
            calls.append(1)
            raise ModelError("provider_failure", code="provider_error")

        with patch("tongle.worker.invoke_json", side_effect=partial):
            with self.assertRaises(ModelError):
                self.ev._call({"id": "probe"}, "compile", {"task": "x"}, cfg)
        row = self.e.db.execute(
            "SELECT actual_input, actual_output, error FROM knowledge_calls").fetchone()
        self.assertIsNone(row["actual_input"])
        self.assertIsNone(row["actual_output"])
        self.assertIn("provider_failure", row["error"])


class RunnerCrashLabelTests(unittest.TestCase):
    """\"the provider failed\" and \"our runner died first\" are different facts."""

    def _fields(self, err):
        from tongle.worker import err_fields
        return err_fields(err)

    def test_non_protocol_stderr_is_runner_crash(self):
        cat, code, stop, status, usage, limit, actual = self._fields(
            'Traceback (most recent call last):\n  File "pi_model.py", line 1\n'
            "NameError: name 're' is not defined\n")
        self.assertEqual((cat, code), ("provider_failure", "runner_crash"))
        self.assertIsNone(stop)
        self.assertIsNone(status)
        self.assertEqual(usage, {})

    def test_empty_stderr_is_also_runner_crash(self):
        self.assertEqual(self._fields("")[1], "runner_crash")

    def test_provider_reported_failure_keeps_its_own_code(self):
        cat, code, stop, status, usage, limit, actual = self._fields(
            json.dumps({"category": "provider_failure", "code": "rate_limited",
                        "stop_reason": "error", "usage": {"input": 5}}))
        self.assertEqual((cat, code, stop), ("provider_failure", "rate_limited", "error"))
        self.assertEqual(usage, {"input": 5})

    def test_usage_only_line_does_not_count_as_a_provider_report(self):
        # pi_model emits this shape before any error line; on its own it proves
        # nothing about the provider having answered.
        only = json.dumps({"runner": "pi", "usage": {"input": 5}})
        cat, code, _, _, usage, _, _ = self._fields(only)
        self.assertEqual(code, "runner_crash")
        self.assertEqual(usage, {"input": 5})


class RunnerEnvironmentTests(unittest.TestCase):
    """The runner must not depend on the scheduler's PATH.

    Regression: under launchd the PATH is only /usr/bin:/bin:/usr/sbin:/sbin,
    so ``pi`` (shebang ``#!/usr/bin/env node``) died instantly with
    ``env: node: No such file or directory`` and every model call was labeled
    runner_crash.
    """

    def test_runner_directory_is_prepended_to_child_path(self):
        import os
        import subprocess
        import tempfile
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            marker = tmp / "path.txt"
            shim = tmp / "pi"
            shim.write_text(
                "#!" + sys.executable + "\nimport json,os,sys\n"
                "sys.stdin.read()\n"
                "open(" + repr(str(marker)) + ",'w').write(os.environ.get('PATH',''))\n"
                "print(json.dumps({'type':'message_end','message':{'role':'assistant',"
                "'stopReason':'stop','usage':{'input':1,'output':1},"
                "'content':[{'type':'text','text':'{}'}]}}))\n")
            shim.chmod(0o755)
            run = subprocess.run(
                [sys.executable, str(root / "scripts" / "pi_model.py"), "--pi", str(shim)],
                input="{}", capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertTrue(marker.read_text().startswith(str(tmp) + os.pathsep),
                            marker.read_text())


if __name__ == "__main__":
    unittest.main()
