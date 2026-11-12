"""Native file locking, durable file publication and maintenance processes."""
import errno
import os
import signal
import subprocess
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


def lock_file(handle, *, blocking=True):
    """Take an exclusive lock until the file handle closes."""
    if not IS_WINDOWS:
        flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        fcntl.flock(handle, flags)
        return

    fd = _fileno(handle)
    position = os.lseek(fd, 0, os.SEEK_CUR)
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        # Lock byte zero even when the lock file is empty. No sentinel is
        # written, so probing a lock never modifies its contents.
        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return
            except OSError as exc:
                if exc.errno not in _CONTENTION_ERRNOS:
                    raise
                if not blocking:
                    raise BlockingIOError(exc.errno, exc.strerror) from None
                time.sleep(_RETRY_SLEEP)
    finally:
        os.lseek(fd, position, os.SEEK_SET)


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


def process_options():
    """Start an owned child group for maintenance and timeout handling."""
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
    """Terminate the owned child group without signalling the caller."""
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
