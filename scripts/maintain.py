#!/usr/bin/env python3
"""Explicit local maintenance process. No global service or Vault discovery."""
import argparse
from contextlib import closing
import hashlib
import json
import os
import plistlib
import re
from pathlib import Path, PurePosixPath
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from tongle.portability import IS_WINDOWS, lock_file, process_options, terminate_tree


def atomic_json(path, value):
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with tmp.open('x', encoding='utf8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_json(path, default):
    if not path.exists():
        return default
    if path.is_symlink() or path.stat().st_size > 2_000_000:
        raise ValueError('invalid maintenance metadata')
    return json.loads(path.read_text(encoding='utf-8'))


def live_lock(directory):
    try:
        handle = (directory/'lock').open('r+b' if IS_WINDOWS else 'rb')
    except FileNotFoundError:
        return False
    with handle as f:
        try:
            lock_file(f, blocking=False)
        except BlockingIOError:
            return True
        return False


def launchd_status(home):
    """Probe the loaded job, not merely its on-disk installation intention."""
    label='com.tongle-next.maintain'
    plist=Path.home()/'Library'/'LaunchAgents'/(label+'.plist')
    result={'installed':False,'loaded':False,'state':'absent'}
    def matches(argv):
        if not isinstance(argv,list) or '--home' not in argv:return False
        try:
            return (Path(argv[argv.index('--home')+1]).resolve()==Path(home).resolve()
                    and any(Path(x).name=='maintain.py' for x in argv))
        except (ValueError,IndexError,TypeError):return False
    try:
        if plist.is_file():
            if plist.is_symlink() or plist.stat().st_size>100000:raise ValueError('unsafe plist')
            data=plistlib.loads(plist.read_bytes())
            result['installed']=data.get('Label')==label and matches(data.get('ProgramArguments'))
    except (OSError,ValueError,plistlib.InvalidFileException):
        result['state']='invalid_plist'
    if sys.platform!='darwin':
        result['state']='unsupported' if not result['installed'] else 'unloaded'
        return result
    try:
        probe=subprocess.run(['launchctl','print',f'gui/{os.getuid()}/{label}'],
                             capture_output=True,text=True,timeout=3)
    except (OSError,subprocess.TimeoutExpired):
        result['state']='unknown' if result['installed'] else 'absent'
        return result
    if probe.returncode:
        result['state']='unloaded' if result['installed'] else result['state']
        return result
    args=re.search(r'(?m)^\s*arguments = \{\s*\n(.*?)^\s*\}',probe.stdout,re.S)
    if args:
        argv=[line.strip() for line in args.group(1).splitlines() if line.strip()]
        result['loaded']=matches(argv)
        result['state']='loaded' if result['loaded'] else 'other_instance'
    else:
        # A loaded job with unparseable identity must not start a competing loop.
        result['state']='unknown' if result['installed'] else 'other_instance'
    return result


def launchd_agent_manages(home):
    return launchd_status(home)['loaded']


def _open_state_db(home):
    """Read-only connection to the actual state database (state.sqlite3).

    Never migrates and never creates the file: a fake or missing DB is an
    unknown, not an empty database. Returns None when the database cannot be
    opened read-only rather than inventing a schema.
    """
    import sqlite3
    db = Path(home) / 'state.sqlite3'
    if not db.is_file():
        return None
    try:
        return sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True)
    except sqlite3.Error:
        return None


def failed_work_count(home):
    """Read-only count of *unhandled* failed work, or None when unreadable.

    A terminal content/capacity failure parked by an explicit triage decision
    is still visible in ``scripts/doctor.py`` and keeps its inputs/audit, but it
    is not an unresolved operational alarm. Transient failures stay counted.
    Missing database, missing table, and test fakes still return None (unknown)
    instead of a fabricated zero.
    """
    import sqlite3
    conn = _open_state_db(home)
    if conn is None:
        return None
    try:
        from tongle.memory_policy import read_home_config,processing_allowed,uses_knowledge_store
        try:cfg=read_home_config(home)
        except (ValueError,OSError):return None
        if not uses_knowledge_store(cfg):
            return conn.execute("select count(*) from jobs where state='failed'").fetchone()[0]
        conn.create_function('tongle_process',1,lambda p:int(processing_allowed(cfg,p)))
        try:
            row = conn.execute(
                "select count(*) from knowledge_work w where w.state='failed' and tongle_process(w.project)"
                " and not exists (select 1 from knowledge_triage t"
                "                 where t.work_id=w.id and t.action='parked')").fetchone()
        except sqlite3.Error:
            row = conn.execute(
                "select count(*) from knowledge_work where state='failed' and tongle_process(project)").fetchone()
        return int(row[0]) if row else None
    except sqlite3.Error:
        return None  # Unknown/corrupt schema must not crash backup health reporting.
    finally:
        conn.close()


def _metadata_error(path, exc):
    return str(exc)[:200] or (path.name + ' unreadable')


def _read_backup_state(directory):
    """Best-effort read of backup.json plus an explicit read error.

    Returns (state, error). Malformed JSON, a non-dict payload, an OSError, or
    a backup.json that is a directory all yield ({}, message) instead of
    raising, so the read-only status path never crashes and the failure stays
    visible to operators.
    """
    path = directory / 'backup.json'
    try:
        value = read_json(path, {})
    except (OSError, ValueError) as exc:
        return {}, _metadata_error(path, exc)
    if not isinstance(value, dict):
        return {}, 'backup.json is not a JSON object'
    return value, None


def _cycle_backup_error(directory):
    """Extract a failed backup error from last-cycle.json, when present.

    ``at`` prefers the exact original attempt time carried in
    ``backup_error_at`` (or the failing backup's own ``at``), so an inherited
    error keeps its ORIGINAL timestamp across empty cycles. The stale
    last-cycle ``finished_at`` is only a legacy fallback when no carried value
    exists.
    """
    path = directory / 'last-cycle.json'
    try:
        value = read_json(path, None)
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    carried = value.get('backup_error_at')
    backup = value.get('backup')
    if isinstance(backup, dict) and backup.get('error'):
        at = backup.get('at')
        if not isinstance(at, (int, float)):
            at = carried
        if not isinstance(at, (int, float)):
            at = value.get('finished_at') or value.get('started_at')
        return {'error': backup['error'], 'at': at}
    if value.get('backup_error'):
        at = carried
        if not isinstance(at, (int, float)):
            at = value.get('finished_at') or value.get('started_at')
        return {'error': value['backup_error'], 'at': at}
    return None


def attention(directory, home):
    """Tri-state attention evidence for operators.

    ``needs_attention`` is True while the latest backup failed or the state
    database reports unreadable evidence; None means the database could not be
    read (unknown), never a fabricated False. ``error`` surfaces a backup
    failure even when the metadata write itself failed, including a failure
    persisted only in last-cycle.json. A newer successful backup clears an
    older last-cycle failure; timestamps are compared explicitly.
    """
    state, read_error = _read_backup_state(directory)
    backup_error = state.get('error') if state.get('error') else read_error
    last_success = state.get('last_success_at')
    if not isinstance(last_success, (int, float)) and 'error' not in state:
        legacy = state.get('finished_at')
        if isinstance(legacy, (int, float)):
            last_success = legacy
    if not backup_error:
        cycle_error = _cycle_backup_error(directory)
        if cycle_error is not None:
            at = cycle_error['at']
            newer_success = isinstance(last_success, (int, float)) and \
                isinstance(at, (int, float)) and last_success > at
            if not newer_success:
                backup_error = cycle_error['error']
    failed = failed_work_count(home)
    backup_error_at = None
    if backup_error:
        needs = True
        cycle_error = _cycle_backup_error(directory)
        if cycle_error is not None and cycle_error['error'] == backup_error:
            backup_error_at = cycle_error['at']
    elif failed is None:
        needs = None
    else:
        needs = failed > 0
    result = {'needs_attention': needs, 'failed_batches': failed,
              'backup_error': backup_error}
    if backup_error_at is not None:
        result['backup_error_at'] = backup_error_at
    return result


def needs_attention(directory, home):
    """Boolean convenience wrapper: True only on positively-known trouble."""
    return attention(directory, home)['needs_attention'] is True


def _wal_allows_unchanged_check(database):
    """Only a quiescent WAL permits hashing the main database alone.

    Committed changes may still live entirely in WAL. A nonempty or unreadable
    WAL disables the optional unchanged optimisation; the normal backup still
    runs on its existing schedule and captures a consistent SQLite snapshot.
    """
    try:
        return Path(str(database) + '-wal').stat().st_size == 0
    except FileNotFoundError:
        return True
    except OSError:
        return False


def _fingerprint_config(home, seen):
    """Hash the config because the archive carries its backup settings."""
    path = Path(home) / 'config.json'
    try:
        if not path.is_file():
            seen.update(b'config:missing\n')
            data=None
        else:data = path.read_bytes()
    except OSError:
        return False
    if data is not None:seen.update(b'config\0' + hashlib.sha256(data).hexdigest().encode('ascii') + b'\n')
    registry=Path(home)/'maintenance/sources.json'
    try:
        if registry.is_symlink():return False
        if registry.exists():seen.update(b'registry\0'+registry.read_bytes()+b'\n')
    except OSError:return False
    return True


def _read_database_uri(database):
    database=Path(database).resolve()
    suffix='?mode=ro' if not _wal_allows_unchanged_check(database) else '?mode=ro&immutable=1'
    return database.as_uri()+suffix


def _database_signature(database):
    values=[]
    for path in (database,Path(str(database)+'-wal')):
        try:
            st=path.stat();values.append((st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns))
        except FileNotFoundError:values.append(None)
    return tuple(values)


def _fingerprint_database(database,seen):
    """Hash authoritative rows; indexes and telemetry must not trigger backups."""
    from tongle.archive import backup_fingerprint_exclusions,derived_triggers,_normalize_sql
    excluded=backup_fingerprint_exclusions();triggers=derived_triggers()
    try:
        with closing(sqlite3.connect(_read_database_uri(database),uri=True)) as conn:
            conn.execute('BEGIN')
            for name,sql in conn.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name"):
                if name in triggers and _normalize_sql(sql)==_normalize_sql(triggers[name]):continue
                seen.update(json.dumps(['trigger',name,sql],ensure_ascii=False).encode())
            for name,sql in conn.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall():
                if name in excluded or name.startswith('sqlite_'):continue
                seen.update(json.dumps(['table',name,sql],ensure_ascii=False).encode())
                quoted='"'+name.replace('"','""')+'"'
                columns=list(conn.execute('PRAGMA table_info('+quoted+')'))
                pk=[row[1] for row in sorted(columns,key=lambda row:row[5]) if row[5]]
                order=','.join('"'+c.replace('"','""')+'"' for c in (pk or [r[1] for r in columns]))
                for row in conn.execute('SELECT * FROM '+quoted+(' ORDER BY '+order if order else '')):
                    if name=='meta' and row[0] in ('knowledge_index_revision','source_index_revision','semantic_index_revision'):
                        continue
                    values=[{'blob':value.hex()} if isinstance(value,bytes) else value for value in row]
                    seen.update(json.dumps(values,ensure_ascii=False,separators=(',',':')).encode()+b'\n')
    except (OSError,sqlite3.Error,ValueError,TypeError):return False
    return True


def _fingerprint_managed(database, seen):
    """Fingerprint the managed files when the full Vault is not archived."""
    try:
        with closing(sqlite3.connect(_read_database_uri(database), uri=True)) as conn:
            row = conn.execute("SELECT value FROM meta WHERE key='vault'").fetchone()
            rows = conn.execute(
                "SELECT path FROM knowledge_files WHERE state='ok' ORDER BY path").fetchall()
    except sqlite3.Error:
        return False
    if not row or not row[0]:
        return False
    vault = Path(row[0])
    if vault.is_symlink() or not vault.is_dir():
        return False
    for (rel,) in rows:
        if not isinstance(rel, str):
            return False
        parsed = PurePosixPath(rel)
        if (parsed.is_absolute() or '..' in parsed.parts
                or any(part.startswith('.') for part in parsed.parts)):
            return False
        path = vault / rel
        try:
            if path.is_symlink() or not path.is_file():
                return False
            data = path.read_bytes()
        except OSError:
            return False
        seen.update(('managed\0%s\0%s\n'
                     % (rel, hashlib.sha256(data).hexdigest())).encode('utf-8'))
    return True


def _source_fingerprint(home, include_vault=True):
    """Fingerprint the inputs a backup would actually carry.

    Authoritative database rows are hashed; rebuildable indexes and operational
    observations alone do not create a new knowledge backup.
    Config is hashed because the archive embeds backup settings. Vault and
    managed files are content-hashed, matching the same hidden and managed-only
    rules as the exporter, so a same-size/mtime edit cannot hide. Returns
    ``None`` when any input cannot be read: unreadable never looks unchanged.
    """
    database = Path(home) / 'state.sqlite3'
    try:
        database_before = _database_signature(database)
    except OSError:
        return None
    seen = hashlib.sha256()
    if not _fingerprint_database(database,seen):return None
    if not _fingerprint_config(home, seen):
        return None
    if include_vault:
        try:
            with closing(sqlite3.connect(_read_database_uri(database), uri=True)) as conn:
                row = conn.execute("SELECT value FROM meta WHERE key='vault'").fetchone()
            vault = Path(row[0]) if row and row[0] else None
        except sqlite3.Error:
            return None
        if vault is None or vault.is_symlink() or not vault.is_dir():
            return None
        try:
            for path in sorted(vault.rglob('*')):
                rel = path.relative_to(vault).as_posix()
                parts = PurePosixPath(rel).parts
                if any(part.startswith('.') for part in parts):
                    continue
                if path.is_symlink():
                    return None
                if path.is_dir():
                    continue
                try:
                    if not path.is_file():
                        return None
                    data = path.read_bytes()
                except OSError:
                    return None
                seen.update(('%s\0%s\n' % (rel, hashlib.sha256(data).hexdigest())).encode('utf-8'))
        except OSError:
            return None
    else:
        if not _fingerprint_managed(database, seen):
            return None
    try:
        database_after = _database_signature(database)
    except OSError:
        return None
    # The SQL read transaction includes committed WAL rows. Any concurrent
    # commit/checkpoint across database/file hashing disables the optimization.
    if database_before!=database_after:
        return None
    return seen.hexdigest()


def _last_archive_intact(state):
    """Only skip when the previously successful archive still exists intact."""
    path = state.get('path')
    if not isinstance(path, str) or not path:
        return False
    try:
        info = Path(path).stat()
    except OSError:
        return False
    if not Path(path).is_file() or info.st_size <= 0:
        return False
    if state.get('bytes') != info.st_size:
        return False
    expected = state.get('archive_sha256')
    if not isinstance(expected, str) or len(expected) != 64:
        return False
    digest = hashlib.sha256()
    try:
        with Path(path).open('rb') as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b''):
                digest.update(chunk)
    except OSError:
        return False
    return digest.hexdigest() == expected


def _verify_backup(args, target):
    """Exercise the real restore entry in disposable directories, once per archive."""
    before = hashlib.sha256(target.read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory(prefix='tongle-backup-verify-') as temp:
        base = Path(temp).resolve()
        home, vault = base/'home', base/'vault'
        isolated = argparse.Namespace(**dict(vars(args), home=home))
        restored = invoke(isolated, ['restore', '--from', str(target), '--vault', str(vault)],
                          required=('restored', 'model_disabled'))
        if restored['restored'] is not True or restored['model_disabled'] is not True:
            raise RuntimeError('backup restore verification not acknowledged')
        database = home/'state.sqlite3'
        if not database.is_file():
            raise RuntimeError('backup restore verification database missing')
        with closing(sqlite3.connect(database.as_uri()+'?mode=ro', uri=True)) as conn:
            if conn.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise RuntimeError('backup restored database integrity failure')
    if hashlib.sha256(target.read_bytes()).hexdigest() != before:
        raise RuntimeError('backup changed during restore verification')
    return {'method': 'isolated_restore_v1', 'sha256': before, 'verified_at': time.time()}


def _restore_verified(state):
    check = state.get('restore_verification')
    return (isinstance(check, dict) and check.get('method') == 'isolated_restore_v1'
            and isinstance(state.get('archive_sha256'), str)
            and check.get('sha256') == state['archive_sha256'])


def daily_backup(args, directory):
    """Bounded daily export of the single source of truth.

    The database and the Vault are the only copy of everything the engine
    knows, and until now nothing copied them on a schedule. Rather than add a
    second scheduler, this runs inside the existing bounded cycle and decides
    from its own state file whether the interval has elapsed.
    """
    cfg = read_json(args.home / 'config.json', {})
    interval = cfg.get('knowledge_backup_interval_seconds', 86400)
    if isinstance(interval, bool) or not isinstance(interval, int) or interval <= 0:
        return {'skipped': 'disabled or invalid knowledge_backup_interval_seconds'}
    state, _read_error = _read_backup_state(directory)
    fingerprint = None
    # Separate the last *successful* backup from the last *attempt*. Only a
    # success may advance the normal daily cadence; a failure retries after a
    # short bounded backoff and keeps exposing the unresolved error.
    last_success = state.get('last_success_at')
    if not isinstance(last_success, (int, float)):
        legacy = state.get('finished_at')
        # Legacy state had only finished_at; without a recorded error that was a
        # success. With an error it must not count as one.
        if isinstance(legacy, (int, float)) and 'error' not in state:
            last_success = legacy
    now = time.time()
    # A previous failure retries after a short bounded backoff, not the daily
    # interval, but stays visible until a success clears it. This check must
    # come BEFORE the normal success interval: a pending error means the daily
    # cadence is not consulted at all.
    if state.get('error'):
        last_attempt = state.get('last_attempt_at')
        if isinstance(last_attempt, (int, float)) and now - last_attempt < 300:
            skipped = {'skipped': 'retry backoff', 'last_attempt_at': last_attempt,
                       'error': state['error']}
            return skipped
    else:
        # Old 'success' means export-only. Verify its exact archive once before
        # allowing the normal interval or unchanged-content fast path.
        if state.get('path') and not _restore_verified(state):
            try:
                target = Path(state['path'])
                if not _last_archive_intact(state):
                    raise RuntimeError('previous backup missing or checksum mismatch')
                verification = _verify_backup(args, target)
                state = dict(state, restore_verification=verification)
                atomic_json(directory / 'backup.json', state)
            except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.TimeoutExpired) as exc:
                failed = dict(state, last_attempt_at=now, error=str(exc)[:200], backup_error_at=now)
                failed.pop('finished_at', None)
                if isinstance(last_success, (int, float)):
                    failed.setdefault('last_success_at', last_success)
                try: atomic_json(directory / 'backup.json', failed)
                except OSError: pass
                return {'error': str(exc)[:200], 'at': now}
        # The cadence is about checking, not about writing: an unchanged store
        # should not produce a second identical archive. last_check_at falls
        # back to the last success so legacy state keeps its old behaviour.
        last_check = state.get('last_check_at')
        if not isinstance(last_check, (int, float)):
            last_check = last_success
        if isinstance(last_check, (int, float)) and now - last_check < interval:
            return {'skipped': 'interval not reached', 'last_backup_at': last_success}
        include_vault = cfg.get('knowledge_backup_include_vault', True) is not False
        fingerprint = _source_fingerprint(args.home, include_vault)
        if (fingerprint and fingerprint == state.get('source_fingerprint')
                and _restore_verified(state) and _last_archive_intact(state)):
            unchanged = dict(state)
            unchanged['last_check_at'] = now
            try:
                atomic_json(directory / 'backup.json', unchanged)
            except OSError:
                pass
            return {'skipped': 'content unchanged', 'source_fingerprint': fingerprint,
                    'last_backup_at': last_success}
    attempt_at = now
    keep = 3
    backups = args.home / 'backups'
    target = backups / ('tongle-' + time.strftime('%Y%m%d-%H%M%S', time.localtime(now)))
    try:
        backups.mkdir(parents=True, exist_ok=True)
        # Failed attempts never fill the backup directory or displace a good
        # archive. Publish only the exact bytes that passed a real restore.
        with tempfile.TemporaryDirectory(prefix='.backup-candidate-', dir=backups) as temp:
            candidate = Path(temp)/'archive'
            result = invoke(args, ['export', '--to', str(candidate)])
            if not candidate.is_file():
                raise RuntimeError('export acknowledged but produced no archive')
            verification = _verify_backup(args, candidate)
            if target.exists():
                raise RuntimeError('backup destination already exists')
            os.replace(candidate, target)
    except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.TimeoutExpired) as exc:
        # A missing backup must never take the maintenance path down with it,
        # but it must be visible: recorded here and in last-cycle.json. Keep
        # the previous success time and drop the stale finished_at so a failure
        # cannot erase the last known good success.
        failed = dict(state)
        failed.pop('finished_at', None)
        if isinstance(last_success, (int, float)) and 'last_success_at' not in failed:
            failed['last_success_at'] = last_success
        failed['last_attempt_at'] = attempt_at
        failed['error'] = str(exc)[:200]
        failed['backup_error_at'] = attempt_at
        try:
            atomic_json(directory / 'backup.json', failed)
        except OSError:
            # Metadata write itself failed; still report the failure upward so
            # last-cycle.json and the caller can expose it.
            pass
        return {'error': str(exc)[:200], 'at': attempt_at}
    archives = sorted(p for p in backups.glob('tongle-*') if p.is_file())
    keepers = {p.name for p in archives[-keep:]}
    removed = []
    for path in archives:
        if path.name in keepers:
            continue
        path.unlink()
        removed.append(path.name)
    # Record the written archive's size so the injected host status line can
    # warn weeks before the fixed export bound is reached, instead of only
    # after the daily export starts failing outright.
    try:
        written_bytes = target.stat().st_size
    except OSError:
        written_bytes = None
    state = {'finished_at': now,
             'last_success_at': attempt_at,
             'last_attempt_at': attempt_at,
             'last_check_at': attempt_at,
             'path': str(target), 'removed': removed,
             'restore_verification': verification,
             'archive_sha256': verification['sha256']}
    if fingerprint:
        state['source_fingerprint'] = fingerprint
    if written_bytes is not None:
        state['bytes'] = written_bytes
    # The bound that actually governs is the UNCOMPRESSED payload size, which is
    # ~3x the written zip. Record it so the size trend warns against the real
    # limit instead of a number that only passes it after the export broke.
    content_bytes = (result or {}).get('content_bytes') if isinstance(result, dict) else None
    if isinstance(content_bytes, int) and content_bytes >= 0:
        state['content_bytes'] = content_bytes
    atomic_json(directory / 'backup.json', state)
    return {'written': str(target), 'removed': removed}


def invoke(args, command, required=None):
    fake = os.environ.get('TONGLE_TEST_CORE')
    argv = [fake] if fake else [args.python, '-m', 'tongle', '--home', str(args.home)]
    child = subprocess.Popen(argv + command, cwd=args.root, text=True,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             **process_options())
    try:
        out, err = child.communicate(timeout=args.timeout)
    except subprocess.TimeoutExpired:
        # Give the core's signal handler time to stop its isolated model runner.
        terminate_tree(child)
        try:
            child.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            terminate_tree(child, force=True)
            child.communicate()
        raise RuntimeError('core '+command[0]+' timed out; termination requested')
    result = subprocess.CompletedProcess(argv + command, child.returncode, out, err)
    if result.returncode:
        raise RuntimeError('core '+command[0]+' failed (exit '+str(result.returncode)+')')
    try:
        data = json.loads(result.stdout)
    except ValueError:
        raise RuntimeError('core '+command[0]+' returned invalid JSON')
    if not isinstance(data, dict) or 'error' in data:
        raise RuntimeError('core '+command[0]+' did not acknowledge success')
    if required and not all(k in data for k in required):
        raise RuntimeError('core '+command[0]+' returned incomplete acknowledgment')
    return data


def _attempt_fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()


def _source_input(source,runtime=None):
    """Input identity, including permission/content changes that unblock retries."""
    details=[source,runtime]
    try:
        path=Path(source.get('path',''))
        info=path.lstat()
        details.append([info.st_mode,info.st_size,info.st_mtime_ns])
        if not path.is_symlink() and path.is_file() and info.st_size<=5*1024*1024:
            details.append(hashlib.sha256(path.read_bytes()).hexdigest())
    except (OSError,ValueError,TypeError,AttributeError) as exc:details.append(type(exc).__name__)
    return _attempt_fingerprint(details)


def _source_revoked(home,sid):
    db=_open_state_db(home)
    if db is None:return False
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_status'").fetchone():return False
        row=db.execute('SELECT state FROM source_status WHERE source_id=?',(sid,)).fetchone()
        return bool(row and row[0]=='revoked')
    finally:db.close()


def _index_input(home,root):
    details=[]
    try:
        config=read_json(Path(home)/'config.json',{});details.append(config)
        retrieval=config.get('retrieval',{})
        if isinstance(retrieval,dict):
            for kind in ('embedding_model','reranker'):
                model=retrieval.get(kind,{})
                path=model.get('path') if isinstance(model,dict) else None
                if path:
                    try:
                        info=Path(path).stat();details.append([path,info.st_mode,info.st_size,info.st_mtime_ns])
                    except OSError:details.append([path,'missing'])
        with closing(sqlite3.connect((Path(home)/'state.sqlite3').resolve().as_uri()+'?mode=ro',uri=True)) as db:
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'knowledge_cards' in tables:
                details.append(db.execute('SELECT id,version,state FROM knowledge_cards ORDER BY id').fetchall())
            if 'versions' in tables:
                details.append(db.execute('SELECT source_id,version,hash FROM versions ORDER BY source_id,version').fetchall())
        for name in ('knowledge_index.py','source_memory.py','semantic_index.py'):
            details.append(hashlib.sha256((Path(root)/'tongle'/name).read_bytes()).hexdigest())
        return _attempt_fingerprint(details)
    except (OSError,ValueError,sqlite3.Error):return None


def _retry_blocked(retries,key,fingerprint):
    previous=retries.get(key,{})
    return bool(fingerprint and previous.get('fingerprint')==fingerprint and previous.get('attempts',0)>=3)


def _retry_failure(retries,key,fingerprint,exc):
    previous=retries.get(key,{})
    attempts=previous.get('attempts',0) if previous.get('fingerprint')==fingerprint else 0
    # Validation/permissions require changed input, not the same failed mutation.
    count=3 if isinstance(exc,(PermissionError,ValueError,TypeError,KeyError)) else min(3,attempts+1)
    retries[key]={'fingerprint':fingerprint,'attempts':count,'blocked':count>=3,
                  'reason':'input_requires_change' if count>=3 else 'bounded_retry','at':time.time()}


def cycle(args, directory):
    started = time.time()
    result = {'started_at': started, 'errors': [], 'ingested': 0}
    stage_times={}
    # Before any Engine is opened: capture subprocesses need its lock. Only
    # previously observed metadata in this instance is eligible for recovery.
    sys.path.insert(0, str(args.root/'adapters'))
    try:
        from codex_identity import reconcile_maintenance
        result['native_capture'] = reconcile_maintenance(args.home, args.root, args.python)
        if result['native_capture']['status'] == 'disabled':
            result.update(ok=True, disabled=True, finished_at=time.time(), seconds=round(time.time()-started,3))
            return result
        if result['native_capture'].get('errors'):
            result['errors'].append({'stage':'native_capture','error':'pending_native_capture_unresolved'})
    except (ImportError, OSError, ValueError, RuntimeError) as exc:
        result['native_capture'] = {'status':'unavailable','partial':True,'errors':1}
        result['errors'].append({'stage':'native_capture','error':'native_capture_stage_unavailable'})
    registry = read_json(directory/'sources.json', {'sources': []})
    cache = read_json(directory/'digests.json', {})
    retries=read_json(directory/'retries.json',{})
    retained_retry_keys={'index'}
    if not isinstance(registry, dict) or not isinstance(registry.get('sources'), list) or len(registry['sources'])>10000:
        raise ValueError('sources.json must have sources array')
    try:source_runtime=hashlib.sha256((args.root/'tongle/store.py').read_bytes()).hexdigest()
    except OSError:source_runtime=str(args.root)
    for index, source in enumerate(registry['sources']):
        key='source:'+_attempt_fingerprint([source.get('project'),source.get('source_id')] if isinstance(source,dict) else index)
        retained_retry_keys.add(key)
        if isinstance(source,dict) and (source.get('enabled') is False or source.get('needs_rebind')):
            retries.pop(key,None)
            continue
        revoked=False;state_error=False
        try:revoked=isinstance(source,dict) and _source_revoked(args.home,source.get('source_id'))
        except sqlite3.Error:state_error=True
        fingerprint=(_attempt_fingerprint([source,source_runtime,'revoked' if revoked else 'state_unknown'])
                     if revoked or state_error else _source_input(source,source_runtime))
        if _retry_blocked(retries,key,fingerprint):
            result['errors'].append({'stage':'source','index':index,'error':'retry_limit_reached; re-register or change source input'})
            continue
        try:
            if not isinstance(source, dict):
                raise ValueError('invalid source registration')
            if state_error:raise RuntimeError('source authorization state unavailable; ingestion deferred')
            path = Path(source['path'])
            project, sid = source['project'], source['source_id']
            if not path.is_absolute() or not isinstance(project, str) or not isinstance(sid, str):
                raise ValueError('source requires absolute path and project/source_id strings')
            if revoked:
                raise ValueError('registered source revoked; no further ingestion')
            if path.is_symlink():
                raise ValueError('registered source is a symlink')
            path = path.resolve()
            if not path.is_file():
                invoke(args,['source-state','--project',project,'--id',sid,'--state','missing'])
                raise ValueError('registered source missing; derived knowledge temporarily excluded')
            if path.suffix.lower() not in ['.txt', '.md', '.docx'] or path.stat().st_size > 5*1024*1024:
                raise ValueError('unsupported or oversized source')
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            digest_key = json.dumps([project, sid, str(path)], ensure_ascii=False)
            if cache.get(digest_key) == digest:
                invoke(args,['source-state','--project',project,'--id',sid,'--state','available'])
                retries.pop(key,None)
                continue
            invoke(args, ['ingest','--project',project,'--path',str(path),'--source-id',sid],
                   required=['source_id','version'])
            invoke(args,['source-state','--project',project,'--id',sid,'--state','available'])
            cache[digest_key] = digest
            atomic_json(directory/'digests.json', cache)
            retries.pop(key,None)
            result['ingested'] += 1
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.TimeoutExpired) as exc:
            _retry_failure(retries,key,fingerprint,exc)
            # No provider output or source content in operations status.
            result['errors'].append({'stage': 'source', 'index': index, 'error': str(exc)[:200]})
    # Reclaim AFTER the work cycle: terminal rows written by this cycle are
    # swept in the same run, and a large legacy backlog is drained a bounded
    # batch per cycle rather than blocking the loop.
    for command in [['sync'], ['process','--limit',str(args.limit)], ['reclaim'], ['index-refresh']]:
        stage_started=time.monotonic()
        index_fingerprint=_index_input(args.home,args.root) if command[0]=='index-refresh' else None
        if command[0]=='index-refresh' and _retry_blocked(retries,'index',index_fingerprint):
            result['errors'].append({'stage':'index','error':'retry_limit_reached; input/config/runtime change required'})
            continue
        try:
            data = invoke(args, command)
            if command[0]=='process':
                result['process'] = {k:data[k] for k in ('processed','failed','pending','disabled','processing_states') if k in data}
                index_errors=sum(len(x.get('errors',[])) for x in data.get('source_index',[]))
                if index_errors:
                    result['errors'].append({'stage':'source_index','error':'%d source versions failed indexing' % index_errors})
            elif command[0]=='reclaim':
                result['reclaim'] = {k:data[k] for k in ('cleared','vacuumed','bytes') if k in data}
            elif command[0]=='index-refresh':
                result['index']=data
                retries.pop('index',None)
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            if command[0]=='index-refresh':_retry_failure(retries,'index',index_fingerprint,exc)
            result['errors'].append({'stage': 'index' if command[0]=='index-refresh' else command[0], 'error': str(exc)[:200]})
        finally:
            if command[0] in ('sync','index-refresh'):
                stage_times['sync' if command[0]=='sync' else 'index']=round((time.monotonic()-stage_started)*1000,3)
    atomic_json(directory/'retries.json',{k:v for k,v in retries.items() if k in retained_retry_keys})
    stage_started=time.monotonic()
    try:
        result['backup'] = daily_backup(args, directory)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        result['backup'] = {'error': str(exc)[:200]}
    stage_times['backup']=round((time.monotonic()-stage_started)*1000,3)
    result['finished_at'] = time.time()
    result['seconds'] = round(result['finished_at'] - started, 3)
    result['ok'] = not result['errors']
    evidence = attention(directory, args.home)
    result['needs_attention'] = (evidence['needs_attention'] or bool(result['errors'])
        or bool(result['native_capture'].get('partial')))
    result['failed_batches'] = evidence['failed_batches']
    if evidence['backup_error']:
        result['backup_error'] = evidence['backup_error']
        # Carry the ORIGINAL error time forward unchanged so an empty cycle
        # cannot reset the timestamp that a later success is compared against.
        if isinstance(evidence.get('backup_error_at'), (int, float)):
            result['backup_error_at'] = evidence['backup_error_at']
    if isinstance(result.get('backup'), dict) and result['backup'].get('error'):
        # In-memory evidence from this cycle must survive a failed metadata
        # write; persist it in last-cycle.json when that file is writable.
        result['needs_attention'] = True
        result['backup_error'] = result['backup']['error']
        at = result['backup'].get('at')
        if isinstance(at, (int, float)):
            result['backup_error_at'] = at
    if (args.home/'state.sqlite3').is_file():
        try:
            from tongle.engine import Engine
            from tongle.observability import record_event,record_alert_state
            engine=Engine(args.home)
            try:
                status='failed' if result['errors'] else ('degraded' if result['needs_attention'] else 'success')
                reason='cycle_failed' if status=='failed' else ('maintenance_attention' if status=='degraded' else 'healthy')
                result['observation']=record_event(engine,event_id='maintenance:'+str(started),project='__instance__',
                    host='unknown',operation='maintenance',status=status,elapsed_ms=result['seconds']*1000,
                    stages=stage_times,reason_code=reason)
                result['alert']=record_alert_state(engine,project='__instance__',category='maintenance',
                    severity={'success':0,'degraded':1,'failed':2}[status],reason_code=reason)
            finally:engine.store.close()
        except (OSError,ValueError,RuntimeError,sqlite3.Error,ImportError):
            result['observation']={'recorded':False,'warning':'maintenance_observation_unavailable'}
    try:
        atomic_json(directory/'last-cycle.json', result)
    except OSError:
        pass
    return result


def run_loop(args, directory):
    with (directory/'lock').open('a') as lock:
        try:
            lock_file(lock, blocking=False)
        except BlockingIOError:
            return {'already_running': True}
        token = uuid.uuid4().hex
        (directory/'stop.json').unlink(missing_ok=True)
        state = {'pid': os.getpid(), 'token': token, 'started_at': time.time(), 'running': True}
        try:
            while True:
                if (directory/'disabled.json').is_file():return {'stopped':True,'disabled':True}
                state['heartbeat_at'] = time.time()
                state['phase'] = 'processing'
                atomic_json(directory/'state.json', state)
                try:
                    cycle(args, directory)
                except (OSError, ValueError, RuntimeError) as exc:
                    atomic_json(directory/'last-cycle.json', {'ok':False,'errors':[{'stage':'cycle','error':str(exc)[:200]}],
                                                              'finished_at':time.time()})
                state['phase'] = 'waiting'
                state['heartbeat_at'] = time.time()
                atomic_json(directory/'state.json', state)
                deadline = time.monotonic()+args.interval
                while time.monotonic()<deadline:
                    if (directory/'disabled.json').is_file():return {'stopped':True,'disabled':True}
                    if read_json(directory/'stop.json', {}).get('token') == token:
                        return {'stopped':True}
                    time.sleep(min(0.2,max(0,deadline-time.monotonic())))
        finally:
            state.update(running=False, phase='stopped', stopped_at=time.time())
            atomic_json(directory/'state.json', state)


def resolve_runtime(args):
    """Use a managed instance's current runtime; explicit CLI overrides win.

    Status/stop must remain usable even if a previously bound environment has
    disappeared. Legacy homes without a launcher keep the original defaults.
    """
    managed = False
    if args.command in ('start', 'once', 'run') and (args.root is None or args.python is None):
        launcher_path = args.home/'launcher.json'
        if launcher_path.exists() or launcher_path.is_symlink():
            launcher = read_json(launcher_path, {})
            state = read_json(args.home/'instance.json', {})
            if not isinstance(state, dict) or state.get('owner') != 'tongle-next':
                raise ValueError('runtime binding has no tongle-next instance owner')
            if not isinstance(launcher, dict):
                raise ValueError('invalid instance runtime binding')
            for key in ('root', 'python'):
                if getattr(args, key) is None:
                    value = launcher.get(key)
                    if not isinstance(value, str) or not value:
                        raise ValueError('instance runtime binding is missing '+key)
                    setattr(args, key, Path(value) if key == 'root' else value)
            managed = True
    args.root = args.root if args.root is not None else ROOT
    args.python = args.python if args.python is not None else sys.executable
    if not args.root.is_absolute() or not Path(args.python).is_absolute():
        raise ValueError('home/root/python must be absolute paths')
    if managed:
        if not (args.root/'scripts/maintain.py').is_file() or not (args.root/'tongle/__main__.py').is_file():
            raise ValueError('bound runtime code is unavailable; repair the instance binding')
        if not Path(args.python).is_file() or not os.access(args.python, os.X_OK):
            raise ValueError('bound runtime Python is unavailable; repair the instance binding')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home',required=True,type=Path)
    parser.add_argument('--root',type=Path)
    parser.add_argument('--python')
    parser.add_argument('--interval',type=float,default=15)
    parser.add_argument('--limit',type=int,default=1)
    parser.add_argument('--timeout',type=float,default=540)
    parser.add_argument('command',choices=['start','stop','status','once','run'])
    args=parser.parse_args()
    if not args.home.is_absolute():
        raise ValueError('home/root/python must be absolute paths')
    if args.interval<0.1 or args.timeout<1 or args.limit<1:
        raise ValueError('invalid interval, timeout or limit')
    args.home=args.home.resolve()
    directory=args.home/'maintenance'
    if not args.home.is_dir():
        raise ValueError('initialize tongle home first')
    resolve_runtime(args)
    # Direct one-shot invocation also imports observations from the selected
    # checkout, matching the core subprocesses launched in args.root.
    sys.path.insert(0, str(args.root))
    if directory.is_symlink():
        raise ValueError('maintenance directory cannot be symlink')
    if args.command!='status':directory.mkdir(exist_ok=True)
    if directory.exists() and any(p.is_symlink() for p in directory.iterdir()):
        raise ValueError('maintenance metadata cannot be symlink')
    if args.command in ('start','once','run') and (directory/'disabled.json').is_file():
        print(json.dumps({'disabled':True,'started':False}));return
    if args.command=='status':
        active=live_lock(directory)
        state=read_json(directory/'state.json',{})
        last_cycle=read_json(directory/'last-cycle.json',None)
        finished=(last_cycle or {}).get('finished_at')
        age=round(time.time()-finished,1) if isinstance(finished,(int,float)) else None
        evidence=attention(directory,args.home)
        launchd=launchd_status(args.home)
        # The lock only exists during a cycle, so a run-mode loop and an
        # externally scheduled `once` need different evidence. Reporting only
        # `running` made a healthy launchd setup look stopped, which is how an
        # operator ends up starting a second loop that then fights for the lock.
        if launchd['loaded']:scheduler='launchd'
        elif active:scheduler='run-loop' if state.get('running') else 'active-cycle'
        elif age is not None and age<=max(60.0,args.interval*4):scheduler='scheduled-once'
        else:scheduler='idle'
        payload={'running':active,'scheduler':scheduler,
                 'launchd_agent_installed':launchd['installed'],'launchd':launchd,
                 'disabled':(directory/'disabled.json').is_file(),
                 'last_cycle_age_seconds':age,'state':state,'last_cycle':last_cycle,
                 'needs_attention':evidence['needs_attention'],
                 'failed_batches':evidence['failed_batches'],
                 'heartbeat_age_seconds':round(time.time()-state.get('heartbeat_at',time.time()),1) if active else None}
        if evidence['backup_error']:
            payload['backup_error']=evidence['backup_error']
        print(json.dumps(payload))
    elif args.command=='stop':
        state=read_json(directory/'state.json',{})
        active=live_lock(directory)
        if active and state.get('token'):
            atomic_json(directory/'stop.json',{'token':state['token']})
        note=('Stops after the current bounded cycle; no unrelated PID signaled.'
              +(' A launchd LaunchAgent still schedules one cycle at a time for this home; unload it to stop maintenance entirely.'
                if launchd_agent_manages(args.home) else ''))
        print(json.dumps({'stop_requested':active,'note':note}))
    elif args.command=='once':
        with (directory/'lock').open('a') as lock:
            try:
                lock_file(lock, blocking=False)
            except BlockingIOError:
                # A singleton lock is already owned by another cycle or loop.
                # That is not a failure of this scheduled run: say so instead of
                # spilling a raw errno into the scheduler log every interval.
                print(json.dumps({'skipped':'another maintenance cycle holds the lock'}))
                return
            print(json.dumps(cycle(args,directory),ensure_ascii=False))
    elif args.command=='run':
        print(json.dumps(run_loop(args,directory)))
    else:
        # One scheduler only. A run-mode loop and a launchd `once` agent both
        # need the same singleton lock, so running both means one of them fails
        # every interval and neither operator sees a clear reason.
        launchd=launchd_status(args.home)
        if launchd['loaded']:
            print(json.dumps({'already_running':True,'scheduler':'launchd','launchd':launchd}));return
        if launchd['state']=='unknown':
            print(json.dumps({'started':False,'degraded':True,'reason':'launchd_state_unknown','launchd':launchd}));return
        if live_lock(directory):
            state=read_json(directory/'state.json',{})
            if state.get('running') and state.get('token'):
                print(json.dumps({'already_running':True,'scheduler':'run-loop'}));return
            # A manually launched one-shot is active; its lock is not evidence
            # that future cycles are scheduled. Give a short cycle time to end.
            deadline=time.monotonic()+1
            while live_lock(directory) and time.monotonic()<deadline:time.sleep(.05)
            if live_lock(directory):
                print(json.dumps({'started':False,'degraded':True,'reason':'one_shot_cycle_active'}));return
        argv=[args.python,str(args.root/'scripts/maintain.py'),'--home',str(args.home),'--root',str(args.root),
              '--python',args.python,'--interval',str(args.interval),'--timeout',str(args.timeout),
              '--limit',str(args.limit),'run']
        logfile=directory/'service.log'
        if logfile.exists() and logfile.stat().st_size>100000:
            os.replace(logfile,directory/'service.previous.log')
        with logfile.open('ab') as log:
            child=subprocess.Popen(argv,stdin=subprocess.DEVNULL,stdout=log,stderr=log,**process_options())
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            if live_lock(directory):
                state=read_json(directory/'state.json',{})
                if state.get('running') and state.get('token'):
                    print(json.dumps({'started':True,'pid':state['pid']}));return
            if child.poll() is not None:break
            time.sleep(0.05)
        raise RuntimeError('maintenance did not acquire its singleton lock')


if __name__=='__main__':
    try:main()
    except (ValueError,OSError,RuntimeError) as exc:
        print(json.dumps({'error':str(exc)[:300]}));sys.exit(1)
