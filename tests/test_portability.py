import multiprocessing
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tongle import engine
from tongle.portability import lock_file, unlock_file, split_paths


_CONTEND_FIXTURE = r'''
import sys, time
sys.path.insert(0, sys.argv[1])
from tongle.portability import lock_file, unlock_file
path = sys.argv[2]
hold = float(sys.argv[3])
ready = sys.argv[4]
with open(path, 'a') as f:
    lock_file(f)
    with open(ready, 'w') as r:
        r.write('ok')
    time.sleep(hold)
    unlock_file(f)
'''

_TRYLOCK_FIXTURE = r'''
import sys
sys.path.insert(0, sys.argv[1])
from tongle.portability import lock_file
path = sys.argv[2]
with open(path, 'a') as f:
    try:
        lock_file(f, blocking=False)
    except BlockingIOError:
        print('busy')
    else:
        print('acquired')
'''


def _run(fixture, argv, env=None):
    return subprocess.run(
        [sys.executable, '-c', fixture] + argv,
        capture_output=True, text=True, encoding='utf-8', env=env,
    )


class LockContentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(__file__).resolve().parents[1]
        self.lock = str(Path(self.tmp.name) / 'contend.lock')
        self.ready = str(Path(self.tmp.name) / 'ready')

    def _spawn_holder(self, hold=1.0):
        proc = subprocess.Popen(
            [sys.executable, '-c', _CONTEND_FIXTURE, str(self.root),
             self.lock, str(hold), self.ready],
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if os.path.exists(self.ready):
                return proc
            time.sleep(0.02)
        proc.kill()
        self.fail('holder did not start')

    def test_nonblocking_reports_contention(self):
        holder = self._spawn_holder(hold=1.5)
        try:
            result = _run(_TRYLOCK_FIXTURE, [str(self.root), self.lock])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'busy')
        finally:
            holder.wait(timeout=10)

    def test_lock_released_after_holder_exits(self):
        holder = self._spawn_holder(hold=0.5)
        holder.wait(timeout=10)
        result = _run(_TRYLOCK_FIXTURE, [str(self.root), self.lock])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'acquired')

    def test_repeated_lock_release_cycles(self):
        for _ in range(3):
            holder = self._spawn_holder(hold=0.3)
            holder.wait(timeout=10)
            result = _run(_TRYLOCK_FIXTURE, [str(self.root), self.lock])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), 'acquired')

    def test_exception_releases_lock(self):
        path = Path(self.tmp.name) / 'exc.lock'
        with path.open('a') as f:
            lock_file(f)
            try:
                raise RuntimeError('boom')
            except RuntimeError:
                unlock_file(f)
        result = _run(_TRYLOCK_FIXTURE, [str(self.root), str(path)])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'acquired')

    def test_closing_releases_lock_without_unlock(self):
        path = Path(self.tmp.name) / 'close.lock'
        f = path.open('a')
        lock_file(f)
        f.close()
        result = _run(_TRYLOCK_FIXTURE, [str(self.root), str(path)])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'acquired')


class SplitPathTests(unittest.TestCase):
    def test_posix_paths(self):
        self.assertEqual(split_paths('/a/b.md /c/d.md', windows=False),
                         ['/a/b.md', '/c/d.md'])

    def test_empty_returns_empty(self):
        self.assertEqual(split_paths('', windows=True), [])
        self.assertEqual(split_paths('   ', windows=True), [])
        self.assertEqual(split_paths('', windows=False), [])

    def test_windows_quoted_path_with_spaces(self):
        raw = r'"C:\My Docs\note.md" "D:\x\y.md"'
        self.assertEqual(split_paths(raw, windows=True),
                         [r'C:\My Docs\note.md', r'D:\x\y.md'])

    def test_windows_unc_backslashes_preserved(self):
        raw = r'"\\server\share\file.md"'
        self.assertEqual(split_paths(raw, windows=True), [r'\\server\share\file.md'])

    def test_windows_unclosed_quote_rejected(self):
        with self.assertRaises(ValueError):
            split_paths(r'"C:\path with space', windows=True)

    def test_windows_no_expansion(self):
        raw = r'%TEMP%\x.md $HOME/y.md'
        self.assertEqual(split_paths(raw, windows=True),
                         [r'%TEMP%\x.md', '$HOME/y.md'])


class AtomicCreateTests(unittest.TestCase):
    def test_atomic_create_chinese_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / '笔记-中文.md'
            engine.atomic_create(target, '内容'.encode('utf-8'))
            self.assertTrue(target.is_file())
            self.assertEqual(target.read_bytes(), '内容'.encode('utf-8'))

    def test_atomic_create_rejects_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'note.md'
            engine.atomic_create(target, b'one')
            with self.assertRaises(FileExistsError):
                engine.atomic_create(target, b'two')


if __name__ == '__main__':
    unittest.main()
