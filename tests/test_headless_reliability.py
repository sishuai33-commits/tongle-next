"""Regressions for prior-error diagnostics and backup attention semantics."""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SENTINEL = 'SECRET_CANARY_DO_NOT_LEAK'


def _load_maintain(clock=None):
    """Load scripts/maintain.py without letting scripts/tongle.py shadow it."""
    spec = importlib.util.spec_from_file_location(
        'maintain_under_test', ROOT / 'scripts' / 'maintain.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _run_pi(messages, exit_code=0, stderr=None):
    """Run scripts/pi_model.py against a fake `pi` shim; return (code, out, err)."""
    with tempfile.TemporaryDirectory(prefix='tongle-pi-') as tmp:
        shim = Path(tmp) / 'pi'
        lines = []
        for message in messages:
            lines.append('print(' + repr(json.dumps({'type': 'message_end', 'message': message})) + ')')
        if stderr is not None:
            lines.append('print(' + repr(json.dumps(stderr)) + ',file=sys.stderr)')
        lines.append('sys.exit(' + str(exit_code) + ')')
        shim.write_text('#!/usr/bin/env python3\nimport json,sys\n' + '\n'.join(lines) + '\n')
        shim.chmod(0o700)
        proc = subprocess.run(
            [sys.executable, str(ROOT / 'scripts/pi_model.py'), '--pi', str(shim)],
            input='{}', capture_output=True, text=True, timeout=30)
        return proc.returncode, proc.stdout, proc.stderr


def _stderr_entries(stderr):
    entries = []
    for line in (stderr or '').splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            entries.append(value)
    return entries


def _setup_home(tmp):
    home = Path(tmp) / 'state'
    home.mkdir()
    (home / 'config.json').write_text(json.dumps({
        'knowledge_mode': 'evolving',
        'knowledge_backup_interval_seconds': 3600,
    }))
    (home / 'maintenance').mkdir()
    return home


def _make_db(home, failed=0):
    """Create the real state database at its actual filename, state.sqlite3."""
    conn = sqlite3.connect(str(home / 'state.sqlite3'))
    conn.execute('CREATE TABLE knowledge_work(id TEXT, state TEXT)')
    for index in range(failed):
        conn.execute("INSERT INTO knowledge_work VALUES(?, 'failed')", ('w%d' % index,))
    conn.commit()
    conn.close()


class _FakeClock:
    """In-process clock that only maintain's own time.time calls see."""

    def __init__(self, value):
        self.value = value
        self._real = time.time

    def __call__(self):
        return self.value


def _call_maintain(args, env, home, clock):
    """Run maintain in-process so a patched clock actually applies.

    A subprocess cannot see a patch made in the parent test, so in-process
    execution is required for fake-time assertions. The ``env`` mapping is a
    TEST-ONLY scoped patch of ``os.environ`` for the duration of the call: the
    in-process maintain honors ``TONGLE_TEST_CORE`` so it invokes the synthetic
    core rather than the real one, and every key is restored afterward.
    """
    module = _load_maintain()
    old_argv = list(sys.argv)
    old_env = dict(os.environ)
    try:
        sys.argv = ['maintain.py', '--home', str(home)] + list(args)
        os.environ.clear()
        os.environ.update(env)
        # Ensure the fake core location is what maintain's invoke() reads. Tests
        # legitimately pass it through ``env``; reapply defensively so a fake
        # core is never shadowed inside this in-process call.
        core = env.get('TONGLE_TEST_CORE')
        if core:
            os.environ['TONGLE_TEST_CORE'] = core
        buf_out, buf_err = [], []
        with patch.object(module, 'time', _module_time(module, clock)), \
             patch.object(sys, 'stdout', _Buffer(buf_out)), \
             patch.object(sys, 'stderr', _Buffer(buf_err)):
            module.main()
        return _Completed(0, ''.join(buf_out), ''.join(buf_err))
    except SystemExit as exc:
        return _Completed(int(exc.code or 0), '', '')
    finally:
        sys.argv = old_argv
        os.environ.clear()
        os.environ.update(old_env)


def _module_time(module, clock):
    """Proxy time module replacement honoring the fake clock for time.time."""
    real = time
    return _TimeProxy(real, clock)


class _TimeProxy:
    def __init__(self, real, clock):
        self._real = real
        self._clock = clock

    def time(self):
        return self._clock()

    def __getattr__(self, name):
        return getattr(self._real, name)


class _Buffer:
    def __init__(self, sink):
        self._sink = sink

    def write(self, text):
        self._sink.append(text)
        return len(text)

    def flush(self):
        pass


class _Completed:
    def __init__(self, returncode, stdout, stderr):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class PriorErrorDiagnostic(unittest.TestCase):
    def test_prior_error_then_request_limit_keeps_guard_cause(self):
        prior = dict(role='assistant', stopReason='error', content=[],
                     usage={'input': 5}, errorMessage='429 rate limit ' + SENTINEL)
        code, out, err = _run_pi([prior], exit_code=8,
                                 stderr={'code': 'request_limit', 'category': 'provider_failure'})
        self.assertNotEqual(code, 0)
        entries = _stderr_entries(err)
        final = [e for e in entries if e.get('code') == 'request_limit']
        self.assertTrue(final, err)
        self.assertEqual(final[-1]['category'], 'provider_failure')
        self.assertEqual(final[-1]['prior_error_code'], 'prior_rate_limited')
        diagnostic = [e for e in entries if e.get('prior_error_code')]
        self.assertTrue(diagnostic, err)
        # The only prior value crossing the boundary is the whitelisted label.
        self.assertEqual(diagnostic[-1]['prior_error_code'], 'prior_rate_limited')
        self.assertNotIn(SENTINEL, err)
        self.assertNotIn(SENTINEL, out)

    def test_prior_network_error_labeled(self):
        prior = dict(role='assistant', stopReason='error', content=[],
                     errorMessage='ECONNRESET ' + SENTINEL)
        code, out, err = _run_pi([prior], exit_code=8,
                                 stderr={'code': 'request_limit', 'category': 'provider_failure'})
        self.assertNotEqual(code, 0)
        entries = _stderr_entries(err)
        self.assertTrue([e for e in entries
                         if e.get('prior_error_code') == 'prior_network_error'], err)
        self.assertNotIn(SENTINEL, err)

    def test_no_previous_error_emits_no_prior_diagnostic(self):
        code, out, err = _run_pi([], exit_code=8,
                                 stderr={'code': 'request_limit', 'category': 'provider_failure'})
        self.assertNotEqual(code, 0)
        entries = _stderr_entries(err)
        final = [e for e in entries if e.get('code') == 'request_limit']
        self.assertTrue(final, err)
        self.assertIsNone(final[-1].get('prior_error_code'))

    def test_known_usage_preserved_through_request_limit(self):
        first = dict(role='assistant', stopReason='stop', content=[],
                     usage={'input': 4, 'output': 2, 'cacheRead': 1, 'cacheWrite': 1})
        second = dict(role='assistant', stopReason='error', content=[],
                      usage={'input': 4, 'output': 2, 'cacheRead': 1, 'cacheWrite': 1},
                      errorMessage='provider error')
        code, out, err = _run_pi([first, second], exit_code=8,
                                 stderr={'code': 'request_limit', 'category': 'provider_failure'})
        self.assertNotEqual(code, 0)
        entries = _stderr_entries(err)
        usage = {}
        for entry in entries:
            if isinstance(entry.get('usage'), dict) and entry['usage']:
                usage = entry['usage']
        self.assertEqual(usage.get('input'), 8)
        self.assertEqual(usage.get('output'), 4)
        self.assertNotIn(SENTINEL, err)


class WorkerPriorError(unittest.TestCase):
    """The worker must keep the guard cause while noting a prior failure."""

    def _invoke(self, messages, exit_code=8, stderr=None):
        from tongle.worker import invoke_json
        with tempfile.TemporaryDirectory(prefix='tongle-worker-') as tmp:
            shim = Path(tmp) / 'pi'
            lines = []
            for message in messages:
                lines.append('print(' + repr(json.dumps({'type': 'message_end', 'message': message})) + ')')
            if stderr is not None:
                lines.append('print(' + repr(json.dumps(stderr)) + ',file=sys.stderr)')
            lines.append('sys.exit(' + str(exit_code) + ')')
            shim.write_text('#!/usr/bin/env python3\nimport json,sys\n' + '\n'.join(lines) + '\n')
            shim.chmod(0o700)
            return invoke_json(
                [sys.executable, str(ROOT / 'scripts/pi_model.py'), '--pi', str(shim)],
                '{}', 30)

    def test_prior_label_survives_into_summary(self):
        from tongle.model_errors import ModelError
        prior = dict(role='assistant', stopReason='error', content=[],
                     errorMessage='429 rate limit ' + SENTINEL)
        with self.assertRaises(ModelError) as got:
            self._invoke([prior], exit_code=8,
                         stderr={'code': 'request_limit', 'category': 'provider_failure'})
        exc = got.exception
        self.assertEqual(exc.code, 'request_limit')
        self.assertEqual(exc.prior_error_code, 'prior_rate_limited')
        self.assertIn('prior=prior_rate_limited', exc.summary())
        self.assertNotIn(SENTINEL, exc.summary())

    def test_no_prior_keeps_request_limit_only(self):
        from tongle.model_errors import ModelError
        with self.assertRaises(ModelError) as got:
            self._invoke([], exit_code=8,
                         stderr={'code': 'request_limit', 'category': 'provider_failure'})
        exc = got.exception
        self.assertEqual(exc.code, 'request_limit')
        self.assertIsNone(exc.prior_error_code)
        self.assertNotIn('prior=', exc.summary())

    def test_usage_fidelity_unchanged(self):
        from tongle.model_errors import ModelError
        message = dict(role='assistant', stopReason='stop', content=[],
                       usage={'input': 7, 'output': 3})
        with self.assertRaises(ModelError) as got:
            self._invoke([message], exit_code=8,
                         stderr={'code': 'request_limit', 'category': 'provider_failure'})
        self.assertEqual(got.exception.usage.get('input'), 7)
        self.assertEqual(got.exception.usage.get('output'), 3)


class BackupAttention(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='tongle-bk-')
        self.addCleanup(self.tmp.cleanup)
        self.home = _setup_home(self.tmp.name)
        self.env = {k: v for k, v in os.environ.items() if k != 'TONGLE_TEST_CORE'}
        self.clock = [1_000_000.0]
        self.fail_flag = self.home / 'fail-export'
        self.core = self.home / 'fake-core.py'
        self.core.write_text('#!/usr/bin/env python3\n'
                             'import json,sys,os\n'
                             'from pathlib import Path\n'
                             'argv=sys.argv[1:]\n'
                             'if "export" in argv:\n'
                             '    home=os.environ.get("TONGLE_FAKE_CORE_HOME")\n'
                             '    if home is None:\n'
                             '        home=str(Path(__file__).resolve().parent)\n'
                             '    if Path(home).joinpath("fail-export").exists():\n'
                             '        print(json.dumps({"error":"export failed"}))\n'
                             '        sys.exit(1)\n'
                             '    target=argv[argv.index("--to")+1]\n'
                             '    Path(target).parent.mkdir(parents=True,exist_ok=True)\n'
                             '    Path(target).write_bytes(b"archive")\n'
                             'print(json.dumps({"ok":True}))\n')
        self.core.chmod(0o700)
        self.env['TONGLE_TEST_CORE'] = str(self.core)
        # The fake core lives in the synthetic home, so its own directory is a
        # safe, explicit fixture location for the fail flag (no dependency on
        # how argv[0] is interpreted).
        self.env['TONGLE_FAKE_CORE_HOME'] = str(self.home)

    def _once(self):
        proc = _call_maintain(['--interval', '0.2', '--timeout', '20', 'once'],
                              self.env, self.home, lambda: self.clock[0])
        if proc.returncode != 0:
            raise AssertionError(proc.stdout + proc.stderr)
        return json.loads(proc.stdout)

    def _status(self):
        return json.loads(_call_maintain(['status'], self.env, self.home,
                                         lambda: self.clock[0]).stdout)

    def _state(self):
        return json.loads((self.home / 'maintenance' / 'backup.json').read_text())

    def test_state_db_is_actually_named_state_sqlite3(self):
        # The real Engine writes state.sqlite3 with the evolving schema; a
        # failed batch must be discoverable from the actual status path.
        from tongle.engine import Engine
        from tongle.evolution import Evolution
        engine = Engine(self.home)
        engine.init(self.home / 'vault')
        Evolution(engine)
        self.addCleanup(engine.store.close)
        now = time.time()
        engine.db.execute(
            "insert into knowledge_work(id,project,session,inputs,state,created_at,error)"
            " values('bad','p','s','[]','failed',?,NULL)", (now,))
        engine.db.commit()
        self.assertTrue((self.home / 'state.sqlite3').is_file())
        payload = self._status()
        self.assertEqual(payload['failed_batches'], 1)
        self.assertTrue(payload['needs_attention'])

    def test_missing_database_is_unknown_not_false(self):
        # A nonexistent fake DB must not be reported as clean.
        payload = self._status()
        self.assertIsNone(payload['failed_batches'])
        self.assertIsNone(payload['needs_attention'])

    def test_pending_failure_within_success_interval_attempts_export(self):
        # Known repro: last_success_at is still inside the daily interval, but a
        # pending error must trigger only the 300s failure backoff.
        (self.home / 'maintenance' / 'backup.json').write_text(json.dumps({
            'last_success_at': 90000.0, 'last_attempt_at': 99400.0,
            'error': 'previous failure'}))
        (self.home / 'config.json').write_text(json.dumps({
            'knowledge_mode': 'evolving',
            'knowledge_backup_interval_seconds': 86400,
        }))
        self.clock[0] = 100000.0
        result = self._once()
        self.assertIn('written', result['backup'])
        state = self._state()
        self.assertNotIn('error', state)
        self.assertEqual(state['last_success_at'], self.clock[0])

    def test_failure_backoff_boundary(self):
        (self.home / 'maintenance' / 'backup.json').write_text(json.dumps({
            'last_success_at': 90000.0, 'last_attempt_at': 100000.0,
            'error': 'previous failure'}))
        (self.home / 'config.json').write_text(json.dumps({
            'knowledge_mode': 'evolving',
            'knowledge_backup_interval_seconds': 86400,
        }))
        self.fail_flag.touch()
        self.clock[0] = 100200.0
        inside = self._once()
        self.assertEqual(inside['backup'].get('skipped'), 'retry backoff')
        self.clock[0] = 100300.0
        boundary = self._once()
        self.assertIn('error', boundary['backup'])
        self.assertTrue(boundary['needs_attention'])

    def test_normal_success_interval_enforced_without_error(self):
        (self.home / 'maintenance' / 'backup.json').write_text(json.dumps({
            'last_success_at': 90000.0}))
        (self.home / 'config.json').write_text(json.dumps({
            'knowledge_mode': 'evolving',
            'knowledge_backup_interval_seconds': 86400,
        }))
        self.clock[0] = 91000.0
        result = self._once()
        self.assertEqual(result['backup'].get('skipped'), 'interval not reached')

    def test_legacy_failed_finished_at_is_not_success(self):
        (self.home / 'maintenance' / 'backup.json').write_text(json.dumps({
            'finished_at': 90000.0, 'error': 'old failure'}))
        (self.home / 'config.json').write_text(json.dumps({
            'knowledge_mode': 'evolving',
            'knowledge_backup_interval_seconds': 86400,
        }))
        self.clock[0] = 90100.0
        self.fail_flag.touch()
        result = self._once()
        # Legacy finished_at with an error must not count as a success, so the
        # failure backoff applies rather than the daily interval.
        self.assertIn('error', result['backup'])
        self.assertNotEqual(result['backup'].get('skipped'), 'interval not reached')

    def test_successful_retry_clears_error_and_resets_cadence(self):
        # Synthetic timestamps must put last_attempt more than 300s in the past
        # so the bounded failure backoff has elapsed and the export is allowed.
        (self.home / 'maintenance' / 'backup.json').write_text(json.dumps({
            'last_success_at': 90000.0, 'last_attempt_at': 99600.0,
            'error': 'previous failure'}))
        (self.home / 'config.json').write_text(json.dumps({
            'knowledge_mode': 'evolving',
            'knowledge_backup_interval_seconds': 3600,
        }))
        self.clock[0] = 100000.0
        result = self._once()
        self.assertIn('written', result['backup'])
        state = self._state()
        self.assertNotIn('error', state)
        self.assertEqual(state['last_success_at'], self.clock[0])
        # Immediately afterward the normal interval applies again.
        self.clock[0] += 10
        again = self._once()
        self.assertEqual(again['backup'].get('skipped'), 'interval not reached')

    def test_backup_json_directory_does_not_crash_attention(self):
        (self.home / 'maintenance' / 'backup.json').unlink(missing_ok=True)
        (self.home / 'maintenance' / 'backup.json').mkdir()
        self.addCleanup(lambda: (self.home / 'maintenance' / 'backup.json').rmdir()
                        if (self.home / 'maintenance' / 'backup.json').is_dir() else None)
        self.fail_flag.touch()
        result = self._once()
        self.assertTrue(result['needs_attention'])
        payload = self._status()
        self.assertTrue(payload['needs_attention'])

    def test_malformed_backup_json_marks_attention_true(self):
        (self.home / 'maintenance' / 'backup.json').write_text('{not json')
        payload = self._status()
        self.assertTrue(payload['needs_attention'])
        self.assertTrue(payload.get('backup_error'))

    def test_failure_meta_write_denied_with_healthy_db_yields_attention(self):
        (self.home / 'maintenance' / 'backup.json').unlink(missing_ok=True)
        (self.home / 'maintenance' / 'backup.json').mkdir()
        self.addCleanup(lambda: (self.home / 'maintenance' / 'backup.json').rmdir()
                        if (self.home / 'maintenance' / 'backup.json').is_dir() else None)
        _make_db(self.home, failed=0)
        self.fail_flag.touch()
        result = self._once()
        self.assertIn('error', result['backup'])
        self.assertTrue(result['needs_attention'])
        payload = self._status()
        self.assertTrue(payload['needs_attention'])

    def test_empty_cycle_does_not_erase_unresolved_backup_error(self):
        (self.home / 'maintenance' / 'backup.json').unlink(missing_ok=True)
        (self.home / 'maintenance' / 'backup.json').mkdir()
        self.addCleanup(lambda: (self.home / 'maintenance' / 'backup.json').rmdir()
                        if (self.home / 'maintenance' / 'backup.json').is_dir() else None)
        self.fail_flag.touch()
        first = self._once()
        self.assertTrue(first['needs_attention'])
        original_at = json.loads(
            (self.home / 'maintenance' / 'last-cycle.json').read_text())['backup_error_at']
        # Remove the unwritable directory so backup.json looks clean. Seed a
        # stale, still-in-interval successful timestamp so this next cycle truly
        # SKIPS the normal interval (it does not perform a new failed export),
        # and the unresolved error must keep its ORIGINAL time.
        (self.home / 'maintenance' / 'backup.json').rmdir()
        (self.home / 'config.json').write_text(json.dumps({
            'knowledge_mode': 'evolving',
            'knowledge_backup_interval_seconds': 86400,
        }))
        self.fail_flag.unlink()
        self.clock[0] += 1
        (self.home / 'maintenance' / 'backup.json').write_text(json.dumps({
            'last_success_at': self.clock[0] - 100.0, 'finished_at': self.clock[0] - 100.0}))
        second = self._once()
        self.assertEqual(second['backup'].get('skipped'), 'interval not reached')
        self.assertTrue(second['needs_attention'])
        carried = json.loads(
            (self.home / 'maintenance' / 'last-cycle.json').read_text())
        self.assertEqual(carried.get('backup_error_at'), original_at)
        payload = self._status()
        self.assertTrue(payload['needs_attention'])
        # A later success (past the daily interval) finally clears it.
        self.clock[0] += 90000
        third = self._once()
        self.assertIn('written', third['backup'])
        self.assertFalse(third['needs_attention'])

    def test_newer_success_clears_stale_last_cycle_error(self):
        # Give this specific test a known healthy state DB so that a newer
        # success clears attention; a missing DB would legitimately be unknown,
        # not healthy, and must not be coerced to False.
        _make_db(self.home, failed=0)
        (self.home / 'maintenance' / 'backup.json').unlink(missing_ok=True)
        (self.home / 'maintenance' / 'backup.json').mkdir()
        self.addCleanup(lambda: (self.home / 'maintenance' / 'backup.json').rmdir()
                        if (self.home / 'maintenance' / 'backup.json').is_dir() else None)
        self.fail_flag.touch()
        first = self._once()
        self.assertTrue(first['needs_attention'])
        # Repair metadata and take a NEWER successful backup in the current
        # cycle; the stale last-cycle error must be cleared for status too.
        (self.home / 'maintenance' / 'backup.json').rmdir()
        self.fail_flag.unlink()
        self.clock[0] += 1000
        second = self._once()
        self.assertIn('written', second['backup'])
        self.assertFalse(second['needs_attention'])
        payload = self._status()
        self.assertFalse(payload['needs_attention'])

    def test_original_error_time_survives_and_success_clears_it(self):
        # Regression: carried backup_error_at=1000, last-cycle.finished_at=2000,
        # and a clean last_success_at=1500. The status must clear the OLD error
        # by comparing the success against the ORIGINAL error time, not the
        # stale later finished_at.
        _make_db(self.home, failed=0)
        (self.home / 'maintenance' / 'backup.json').write_text(json.dumps({
            'last_success_at': 1500.0}))
        (self.home / 'maintenance' / 'last-cycle.json').write_text(json.dumps({
            'backup_error': 'old failure', 'backup_error_at': 1000.0,
            'finished_at': 2000.0, 'started_at': 2000.0}))
        self.clock[0] = 3000.0
        payload = self._status()
        self.assertFalse(payload['needs_attention'])
        self.assertNotIn('backup_error', payload)
    def test_failure_preserves_legacy_success_then_backs_off(self):
        # Seed a legacy successful finished_at, then force one failure.
        (self.home / 'config.json').write_text(json.dumps({
            'knowledge_mode': 'evolving',
            'knowledge_backup_interval_seconds': 7200,
        }))
        (self.home / 'maintenance' / 'backup.json').write_text(json.dumps({
            'finished_at': self.clock[0] - 7200.0, 'path': 'old-archive'}))
        self.fail_flag.touch()
        first = self._once()
        self.assertIn('error', first['backup'])
        state = self._state()
        self.assertIn('error', state)
        self.assertNotIn('finished_at', state)
        self.assertEqual(state.get('last_success_at'), self.clock[0] - 7200.0)
        # Error persists past the daily interval: retry short backoff, not 24h.
        self.clock[0] += 100
        second = self._once()
        self.assertEqual(second['backup'].get('skipped'), 'retry backoff')
        self.assertIn('error', second['backup'])
        self.assertTrue(second['needs_attention'])
        # Past the backoff, a success resets the daily cadence and clears error.
        self.clock[0] += 400
        self.fail_flag.unlink()
        third = self._once()
        self.assertIn('written', third['backup'])
        state = self._state()
        self.assertIn('last_success_at', state)
        self.assertNotIn('error', state)

    def test_backup_failure_reported_even_when_meta_write_fails(self):
        # Make backup.json unwritable by replacing it with a directory, so the
        # failure metadata write itself fails; the cycle must still surface it.
        (self.home / 'maintenance' / 'backup.json').unlink(missing_ok=True)
        (self.home / 'maintenance' / 'backup.json').mkdir()
        self.addCleanup(lambda: (self.home / 'maintenance' / 'backup.json').rmdir()
                        if (self.home / 'maintenance' / 'backup.json').is_dir() else None)
        self.fail_flag.touch()
        proc = _call_maintain(['--interval', '0.2', '--timeout', '20', 'once'],
                              self.env, self.home, lambda: self.clock[0])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads(proc.stdout)
        self.assertIn('error', result['backup'])
        self.assertTrue(result['needs_attention'])


if __name__ == '__main__':
    unittest.main()
