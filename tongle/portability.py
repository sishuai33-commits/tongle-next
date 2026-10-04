"""Cross-platform primitives: file locks, directory sync, process options,
process-tree termination, and path splitting.

stdlib only. No behavior change on POSIX; adds native support on Windows
without any no-op fallback for locking or termination.
"""
import errno
import os
import shlex
import signal
import subprocess
import sys
import time

IS_WINDOWS = os.name == 'nt'

if IS_WINDOWS:
    import msvcrt
else:
    import fcntl


_CONTENTION_ERRNOS = frozenset(
    e for e in (getattr(errno, 'EACCES', None),
                getattr(errno, 'EAGAIN', None),
                getattr(errno, 'EDEADLK', None))
    if e is not None
)

_RETRY_SLEEP = 0.05


def _fileno(file_or_fd):
    if isinstance(file_or_fd, int):
        return file_or_fd
    return file_or_fd.fileno()


def lock_file(file_or_fd, blocking=True):
    """Acquire an exclusive lock on an open file object or fd.

    POSIX: fcntl.flock LOCK_EX (LOCK_NB when non-blocking).
    Windows: msvcrt.locking on byte 0 with length 1, position restored.
    Blocking on Windows retries on contention errnos only; non-blocking
    converts contention to BlockingIOError. Empty files are supported
    without appending a sentinel on every lock: byte 0 of a zero-length
    file is lockable by msvcrt.locking. Closing the descriptor releases.
    """
    if not IS_WINDOWS:
        flags = fcntl.LOCK_EX
        if not blocking:
            flags |= fcntl.LOCK_NB
        fcntl.flock(_fileno(file_or_fd), flags)
        return

    fd = _fileno(file_or_fd)
    if not blocking:
        pos = os.lseek(fd, 0, os.SEEK_CUR)
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                if exc.errno in _CONTENTION_ERRNOS:
                    raise BlockingIOError(exc.errno, exc.strerror) from None
                raise
        finally:
            os.lseek(fd, pos, os.SEEK_SET)
        return

    while True:
        pos = os.lseek(fd, 0, os.SEEK_CUR)
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return
            except OSError as exc:
                if exc.errno not in _CONTENTION_ERRNOS:
                    raise
        finally:
            os.lseek(fd, pos, os.SEEK_SET)
        time.sleep(_RETRY_SLEEP)


def unlock_file(file_or_fd):
    """Release a lock previously taken by lock_file.

    Real OS errors from the unlock syscall propagate: silently swallowing
    them hides a still-held lock on a descriptor the caller believes it has
    released. The close of the descriptor remains the caller's responsibility.
    """
    if not IS_WINDOWS:
        fcntl.flock(_fileno(file_or_fd), fcntl.LOCK_UN)
        return

    fd = _fileno(file_or_fd)
    pos = os.lseek(fd, 0, os.SEEK_CUR)
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    finally:
        os.lseek(fd, pos, os.SEEK_SET)


def fsync_directory(path):
    """POSIX: fsync the directory to persist link/replace metadata.

    Windows: directory fsync is unsupported by the OS, so skip it and rely
    on the already-performed file fsync plus exclusive link/replace.
    """
    if IS_WINDOWS:
        return
    path = os.fspath(path)
    dfd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def process_options(detached=False):
    """Keyword args for subprocess.Popen controlling process lifetime.

    POSIX: the child always starts a new session (start_new_session=True),
    regardless of the ``detached`` argument, because terminate_tree owns
    this process group by calling os.killpg(child.pid). Without a fresh
    session the child would inherit the caller's process group and a
    SIGTERM/SIGKILL to that group would strike unrelated processes.
    Windows: CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW so the child
    never steals the console and survives the guide window closing.
    """
    if IS_WINDOWS:
        flags = getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
        flags |= getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        return {'creationflags': flags}
    return {'start_new_session': True}


def _taskkill_path():
    root = os.environ.get('SystemRoot')
    if not root:
        root = r'C:\Windows'
    return os.path.join(root, 'System32', 'taskkill.exe')


def terminate_tree(child, force=False):
    """Terminate an owned Popen process and its children.

    POSIX: os.killpg on the child's process group via child.pid (the pgid),
    TERM then KILL, catching only ProcessLookupError. The pid is used
    directly as the group id: the child was started as its own session
    leader, so pgid == pid. Calling os.getpgid(pid) would fail once the
    parent has reaped the child and, worse, could resolve to an inherited
    caller group and signal unrelated processes. PermissionError is not
    retried into a SIGKILL: a real EPERM must surface rather than being
    converted into an unsafe second signal.
    Windows: absolute taskkill.exe /PID /T /F with shell=False, bounded
    timeout, invalid pid rejected and already-finished processes handled.
    taskkill is invoked while the parent is still alive so /T walks the
    live child tree; if it fails while the process is still alive, raise;
    never falsely claim cleanup.
    """
    if child is None:
        return
    pid = getattr(child, 'pid', None)
    if not isinstance(pid, int) or pid <= 0:
        raise ValueError('invalid process pid')

    if not IS_WINDOWS:
        sig = signal.SIGKILL if force else signal.SIGTERM
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            pass
        return

    if child.poll() is not None:
        return

    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    argv = [_taskkill_path(), '/PID', str(pid), '/T', '/F']
    try:
        result = subprocess.run(
            argv,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            creationflags=flags,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError('taskkill timed out terminating process tree')
    except OSError as exc:
        raise RuntimeError('taskkill failed to launch: %s' % exc)

    if result.returncode != 0 and child.poll() is None:
        raise RuntimeError('taskkill failed to terminate process tree')


def _windows_split(raw):
    parts = []
    buf = []
    in_quotes = False
    for ch in raw:
        if ch == '"':
            in_quotes = not in_quotes
            continue
        if ch.isspace() and not in_quotes:
            if buf:
                parts.append(''.join(buf))
                buf = []
            continue
        buf.append(ch)
    if in_quotes:
        raise ValueError('unclosed quote in path list')
    if buf:
        parts.append(''.join(buf))
    return parts


def split_paths(raw, windows=None):
    """Split a raw drag payload into individual paths.

    POSIX: shlex.split. Windows: whitespace separation outside doublequotes,
    backslashes (including UNC) preserved, unclosed quotes rejected. No shell
    evaluation, globbing or variable expansion on any platform.
    """
    if not isinstance(raw, str):
        raise ValueError('path list must be a string')
    if windows is None:
        windows = IS_WINDOWS
    if windows:
        return _windows_split(raw)
    return shlex.split(raw)


def configure_stdio():
    """Make child Python output UTF-8 and normalize our own streams.

    Forces PYTHONUTF8/PYTHONIOENCODING for child Python processes: setdefault
    is insufficient if the parent already inherited a wrong encoding. Reconfigures
    stdin/stdout/stderr to UTF-8 only when they are not a TTY: a native Windows
    console already handles Unicode, and forcing UTF-8 there would corrupt its
    output. Test buffers may be None/StringIO or lack isatty/reconfigure; all
    checks are guarded and failures are ignored.
    """
    os.environ['PYTHONUTF8'] = '1'
    os.environ['PYTHONIOENCODING'] = 'utf-8'
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if stream is None:
            continue
        isatty = getattr(stream, 'isatty', None)
        try:
            if isatty is not None and isatty():
                continue
        except Exception:
            pass
        reconfigure = getattr(stream, 'reconfigure', None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding='utf-8')
        except (AttributeError, ValueError, OSError):
            pass
