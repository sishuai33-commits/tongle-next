#!/usr/bin/env python3
"""Explicit local maintenance process. No global service or Vault discovery."""
import argparse
import hashlib
import json
import os
import signal
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
# Front-insert the package root so scripts/tongle.py (if present) cannot shadow
# the tongle package import from this script's own directory.
if str(ROOT) in sys.path:
    sys.path.remove(str(ROOT))
sys.path.insert(0, str(ROOT))
from tongle.portability import (lock_file, unlock_file, process_options,
                                terminate_tree, configure_stdio)


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
    with (directory/'lock').open('a') as f:
        try:
            lock_file(f, blocking=False)
        except BlockingIOError:
            return True
        return False


def launchd_agent_manages(home):
    """True when a loaded LaunchAgent schedules cycles for *this* home.

    Keyed on the managed home rather than on "a plist exists": a test or a
    second instance with its own home must not be locked out by an unrelated
    agent, and an agent for another home must not be mistaken for ours.
    """
    plist=Path.home()/'Library'/'LaunchAgents'/'com.tongle-next.maintain.plist'
    if not plist.is_file():
        return False
    try:
        return str(home) in plist.read_text(encoding='utf-8')
    except OSError:
        return False


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
    """Read-only count of failed knowledge_work, or None when unreadable.

    Missing database, missing table, and test fakes all return None (unknown)
    instead of a fabricated zero, so health cannot claim cleanliness it did
    not verify.
    """
    import sqlite3
    conn = _open_state_db(home)
    if conn is None:
        return None
    try:
        try:
            row = conn.execute("select count(*) from knowledge_work where state='failed'").fetchone()
            return int(row[0]) if row else None
        except sqlite3.Error:
            return None
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
    elif isinstance(last_success, (int, float)) and now - last_success < interval:
        return {'skipped': 'interval not reached', 'last_backup_at': last_success}
    attempt_at = now
    keep = 3
    backups = args.home / 'backups'
    target = backups / ('tongle-' + time.strftime('%Y%m%d-%H%M%S', time.localtime(now)))
    try:
        invoke(args, ['export', '--to', str(target)])
        if not target.is_file():
            raise RuntimeError('export acknowledged but produced no archive')
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
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
    atomic_json(directory / 'backup.json', {'finished_at': now,
                                            'last_success_at': attempt_at,
                                            'last_attempt_at': attempt_at,
                                            'path': str(target), 'removed': removed})
    return {'written': str(target), 'removed': removed}


def invoke(args, command, required=None):
    fake = os.environ.get('TONGLE_TEST_CORE')
    argv = [fake] if fake else [args.python, '-m', 'tongle', '--home', str(args.home)]
    child = subprocess.Popen(argv + command, cwd=args.root, text=True,
                             encoding='utf-8',
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             **process_options(detached=True))
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


def cycle(args, directory):
    started = time.time()
    result = {'started_at': started, 'errors': [], 'ingested': 0}
    registry = read_json(directory/'sources.json', {'sources': []})
    cache = read_json(directory/'digests.json', {})
    if not isinstance(registry, dict) or not isinstance(registry.get('sources'), list):
        raise ValueError('sources.json must have sources array')
    for index, source in enumerate(registry['sources']):
        try:
            if not isinstance(source, dict):
                raise ValueError('invalid source registration')
            path = Path(source['path'])
            project, sid = source['project'], source['source_id']
            if not path.is_absolute() or not isinstance(project, str) or not isinstance(sid, str):
                raise ValueError('source requires absolute path and project/source_id strings')
            if path.is_symlink():
                raise ValueError('registered source is a symlink')
            path = path.resolve()
            if not path.is_file():
                invoke(args,['source-state','--project',project,'--id',sid,'--state','missing'])
                raise ValueError('registered source missing; derived knowledge temporarily excluded')
            if path.suffix.lower() not in ['.txt', '.md', '.docx'] or path.stat().st_size > 5*1024*1024:
                raise ValueError('unsupported or oversized source')
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            key = json.dumps([project, sid, str(path)], ensure_ascii=False)
            if cache.get(key) == digest:
                invoke(args,['source-state','--project',project,'--id',sid,'--state','available'])
                continue
            invoke(args, ['ingest','--project',project,'--path',str(path),'--source-id',sid],
                   required=['source_id','version'])
            invoke(args,['source-state','--project',project,'--id',sid,'--state','available'])
            cache[key] = digest
            atomic_json(directory/'digests.json', cache)
            result['ingested'] += 1
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.TimeoutExpired) as exc:
            # No provider output or source content in operations status.
            result['errors'].append({'stage': 'source', 'index': index, 'error': str(exc)[:200]})
    for command in [['sync'], ['process','--limit',str(args.limit)]]:
        try:
            data = invoke(args, command)
            if command[0]=='process':
                result['process'] = {k:data[k] for k in ('processed','failed','pending','disabled') if k in data}
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            result['errors'].append({'stage': command[0], 'error': str(exc)[:200]})
    try:
        result['backup'] = daily_backup(args, directory)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        result['backup'] = {'error': str(exc)[:200]}
    result['finished_at'] = time.time()
    result['seconds'] = round(result['finished_at'] - started, 3)
    result['ok'] = not result['errors']
    evidence = attention(directory, args.home)
    result['needs_attention'] = evidence['needs_attention']
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
                    if read_json(directory/'stop.json', {}).get('token') == token:
                        return {'stopped':True}
                    time.sleep(min(0.2,max(0,deadline-time.monotonic())))
        finally:
            state.update(running=False, phase='stopped', stopped_at=time.time())
            atomic_json(directory/'state.json', state)


def main():
    configure_stdio()
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home',required=True,type=Path)
    parser.add_argument('--root',type=Path,default=ROOT)
    parser.add_argument('--python',default=sys.executable)
    parser.add_argument('--interval',type=float,default=15)
    parser.add_argument('--limit',type=int,default=1)
    parser.add_argument('--timeout',type=float,default=540)
    parser.add_argument('command',choices=['start','stop','status','once','run'])
    args=parser.parse_args()
    if not args.home.is_absolute() or not args.root.is_absolute() or not Path(args.python).is_absolute():
        raise ValueError('home/root/python must be absolute paths')
    if args.interval<0.1 or args.timeout<1 or args.limit<1:
        raise ValueError('invalid interval, timeout or limit')
    args.home=args.home.resolve()
    directory=args.home/'maintenance'
    if not args.home.is_dir():
        raise ValueError('initialize tongle home first')
    if directory.is_symlink():
        raise ValueError('maintenance directory cannot be symlink')
    directory.mkdir(exist_ok=True)
    if any(p.is_symlink() for p in directory.iterdir()):
        raise ValueError('maintenance metadata cannot be symlink')
    if args.command=='status':
        active=live_lock(directory)
        state=read_json(directory/'state.json',{})
        last_cycle=read_json(directory/'last-cycle.json',None)
        finished=(last_cycle or {}).get('finished_at')
        age=round(time.time()-finished,1) if isinstance(finished,(int,float)) else None
        evidence=attention(directory,args.home)
        # The lock only exists during a cycle, so a run-mode loop and an
        # externally scheduled `once` need different evidence. Reporting only
        # `running` made a healthy launchd setup look stopped, which is how an
        # operator ends up starting a second loop that then fights for the lock.
        if active:scheduler='run-loop'
        elif age is not None and age<=max(60.0,args.interval*4):scheduler='scheduled-once'
        else:scheduler='idle'
        payload={'running':active,'scheduler':scheduler,
                 'launchd_agent_installed':launchd_agent_manages(args.home),
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
        if launchd_agent_manages(args.home):
            raise RuntimeError(
                'launchd LaunchAgent com.tongle-next.maintain is installed for this home and already'
                ' runs one cycle at a time; a second run-mode loop would fight it for the lock.'
                ' Unload the agent first: launchctl bootout gui/$UID/com.tongle-next.maintain')
        if live_lock(directory):
            print(json.dumps({'already_running':True}));return
        argv=[args.python,str(Path(__file__).resolve()),'--home',str(args.home),'--root',str(args.root),
              '--python',args.python,'--interval',str(args.interval),'--timeout',str(args.timeout),
              '--limit',str(args.limit),'run']
        logfile=directory/'service.log'
        if logfile.exists() and logfile.stat().st_size>100000:
            os.replace(logfile,directory/'service.previous.log')
        env=dict(os.environ)
        env['PYTHONUTF8']='1'
        env['PYTHONIOENCODING']='utf-8'
        with logfile.open('ab') as log:
            child=subprocess.Popen(argv,stdin=subprocess.DEVNULL,stdout=log,stderr=log,env=env,**process_options(detached=True))
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
