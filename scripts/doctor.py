#!/usr/bin/env python3
"""Read-only operational diagnosis for a tongle home.

The goal is to answer three questions in one screen:
  * Is capture / scheduling / backup healthy right now?
  * What is stuck, since when, and why?
  * What is the next safe action (retry, park, inspect, or nothing)?

It never writes to the database, config, queue, or vault. Exit code is 0
when no action is required, 1 when action is required, 2 when the state
cannot be read.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Direct execution puts scripts/ first, where tongle.py shadows the package.
# PYTHONPATH may already contain ROOT later; it still must take precedence.
sys.path.insert(0, str(ROOT))

from tongle.triage import ACTIONS, classify


def _connect(home: Path) -> sqlite3.Connection:
    db = home / 'state.sqlite3'
    if not db.is_file():
        raise FileNotFoundError(db)
    uri = db.resolve().as_uri() + '?mode=ro'
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _ts(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    text = str(value)
    try:
        return datetime.fromisoformat(text.replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


def _iso(value):
    if value is None:
        return None
    return datetime.fromtimestamp(value).astimezone().isoformat(timespec='seconds')


def classify_failure(reason: str) -> tuple[str, str]:
    """Return (class, suggested_action) for a terminal failure reason."""
    cls = classify(reason)
    return cls, ACTIONS[cls]


def _counts(conn):
    return {r['state']: r['n'] for r in conn.execute(
        'select state, count(*) n from knowledge_work group by state')}


def _triage_rows(conn):
    try:
        return {r['work_id']: dict(r) for r in conn.execute(
            'select work_id, class, action, note, actor, created_at from knowledge_triage')}
    except sqlite3.Error:
        return {}


def _failed_rows(conn):
    now = time.time()
    triage = _triage_rows(conn)
    rows = []
    for r in conn.execute(
            'select id, project, session, attempts, error, created_at, finished_at'
            ' from knowledge_work where state=\'failed\' order by created_at'):
        cls, action = classify_failure(r['error'] or '')
        created = _ts(r['created_at'])
        handled = triage.get(r['id'])
        if handled and handled.get('action') != 'parked':
            handled = None
        rows.append({
            'id': r['id'],
            'project': r['project'],
            'session': r['session'],
            'attempts': r['attempts'],
            'reason': (r['error'] or '')[:240],
            'class': cls,
            'age_hours': round((now - created) / 3600, 1) if created else None,
            'suggested_action': action,
            'triage': ({'action': handled.get('action'),
                        'note': handled.get('note'),
                        'at': _iso(_ts(handled.get('created_at')))}
                       if handled else None),
        })
    return rows


def _stale_dependencies(conn):
    rows = []
    for r in conn.execute(
            'select k.id owner, k.project, k.title, d.basis_id,'
            ' d.basis_version recorded, b.version current, b.state'
            ' from knowledge_dependencies d'
            ' join knowledge_cards k on k.id=d.card_id'
            ' left join knowledge_cards b on b.id=d.basis_id'
            ' where k.state=\'active\' and (b.id is null or b.state!=\'active\''
            ' or b.version!=d.basis_version) order by k.project, k.title'):
        rows.append(dict(r))
    return rows


def _files(conn):
    result = {'ok': 0, 'other': {}}
    for r in conn.execute('select state, count(*) n from knowledge_files group by state'):
        if r['state'] == 'ok':
            result['ok'] = r['n']
        else:
            result['other'][r['state']] = r['n']
    return result


def _cards(conn):
    result = {}
    for r in conn.execute('select state, count(*) n from knowledge_cards group by state'):
        result[r['state']] = r['n']
    return result


def _oversized_cards(conn):
    rows = []
    for r in conn.execute(
            'select id, project, version, length(body) body_chars, title'
            ' from knowledge_cards where state=\'active\' and length(body)>8000'
            ' order by length(body) desc'):
        rows.append(dict(r))
    return {'count': len(rows), 'cards': rows[:12]}


def _queue_age(conn):
    now = time.time()
    row = conn.execute(
        "select count(*) n, min(created_at) oldest from knowledge_work"
        " where state in ('pending','retry','budget_wait','blocked')").fetchone()
    oldest = row['oldest']
    return {'ready': row['n'],
            'oldest_hours': round((now - oldest) / 3600, 1) if oldest else None}


def _sources(conn):
    latest = conn.execute('select max(created_at) t from sources').fetchone()['t']
    return {'latest_source_at': latest,
            'latest_source_ts': _ts(latest)}


def _backup(home: Path):
    path = home / 'maintenance' / 'backup.json'
    if not path.is_file():
        return {'state': 'missing'}
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        return {'state': 'unreadable', 'error': str(exc)[:160]}
    last = value.get('last_success_at') or value.get('finished_at')
    age = (time.time() - last) / 3600 if isinstance(last, (int, float)) else None
    backups = sorted(p.name for p in (home / 'backups').glob('tongle-*') if p.is_file())
    state = 'error' if value.get('error') else 'ok'
    error = value.get('error')
    archive = value.get('path')
    # A recorded success is not proof that the archive still exists intact.
    # Check the cheap facts on every doctor run; the checksum is present only
    # for backups written after 2026-10-19.
    if state == 'ok' and isinstance(archive, str) and archive:
        try:
            path = Path(archive)
            info = path.stat()
            if not path.is_file():
                raise ValueError('archive missing')
            if isinstance(value.get('bytes'), int) and info.st_size != value['bytes']:
                raise ValueError('archive size changed')
            checksum = value.get('archive_sha256')
            if isinstance(checksum, str) and len(checksum) == 64:
                digest = hashlib.sha256()
                with path.open('rb') as handle:
                    for chunk in iter(lambda: handle.read(1 << 20), b''):
                        digest.update(chunk)
                if digest.hexdigest() != checksum:
                    raise ValueError('archive checksum changed')
        except (OSError, ValueError) as exc:
            state = 'corrupt'
            error = str(exc)[:160]
    return {'state': state,
            'last_success_at': _iso(last) if isinstance(last, (int, float)) else None,
            'age_hours': round(age, 1) if age is not None else None,
            'bytes': value.get('bytes'),
            'content_bytes': value.get('content_bytes'),
            'archives': backups,
            'error': error}


def _cycle(home: Path):
    path = home / 'maintenance' / 'last-cycle.json'
    if not path.is_file():
        return {'state': 'missing'}
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        return {'state': 'unreadable', 'error': str(exc)[:160]}
    started = _ts(value.get('started_at'))
    return {'state': 'ok' if value.get('ok') else 'error',
            'started_at': _iso(started) if started else None,
            'age_seconds': round(time.time() - started, 1) if started else None,
            'errors': value.get('errors') or [],
            'failed_batches': value.get('failed_batches'),
            'process': value.get('process'),
            'backup': value.get('backup')}


def _launchd(label='com.tongle-next.maintain'):
    if sys.platform != 'darwin':
        return {'state': 'unsupported'}
    try:
        process = subprocess.run(['launchctl', 'print', f'gui/{os.getuid()}/{label}'],
                                 capture_output=True, text=True, timeout=4)
    except (OSError, subprocess.TimeoutExpired):
        return {'state': 'unknown'}
    if process.returncode:
        return {'state': 'not_loaded'}
    out = process.stdout
    result = {'state': 'loaded'}
    for pattern, key in ((r'\bruns = (\d+)', 'runs'),
                         (r'last exit code = (\d+)', 'last_exit'),
                         (r'run interval = (\d+)', 'interval_seconds')):
        m = re.search(pattern, out)
        if m:
            result[key] = int(m.group(1))
    return result


def _code(root: Path):
    result = {'root': str(root)}
    try:
        result['head'] = subprocess.run(
            ['git', 'rev-parse', '--short', 'HEAD'], cwd=root,
            capture_output=True, text=True, timeout=4).stdout.strip()
        result['dirty'] = bool(subprocess.run(
            ['git', 'status', '--porcelain'], cwd=root,
            capture_output=True, text=True, timeout=8).stdout.strip())
        remotes = subprocess.run(
            ['git', 'remote', '-v'], cwd=root,
            capture_output=True, text=True, timeout=4).stdout.strip()
        result['remote'] = bool(remotes)
    except (OSError, subprocess.TimeoutExpired) as exc:
        result['error'] = str(exc)[:160]
    return result


def _config(home: Path):
    from tongle.memory_policy import read_home_config, uses_knowledge_store
    value = read_home_config(home)
    uses_knowledge_store(value)  # Malformed mode/config is unknown, never healthy legacy.
    return {
        'model': value.get('model_name'),
        'daily_calls': value.get('knowledge_daily_call_budget'),
        'daily_tokens': value.get('knowledge_daily_token_budget'),
        'mode': value.get('knowledge_mode'),
        'dependency_projects': value.get('knowledge_dependency_projects'),
        'learning_projects': value.get('knowledge_learning_projects'),
        'synthesis_topics': value.get('knowledge_synthesis_topics'),
    }


def _stale_is_actionable(config):
    """Stale dependency rows only need action when auto-refresh is enabled.

    With ``knowledge_dependency_projects`` empty (the memory-mode setup),
    recorded basis versions are historical by design. Reporting them as
    ACTION REQUIRED would make the health check permanently red and hide the
    alerts that still matter.
    """
    projects = config.get('dependency_projects')
    return isinstance(projects, list) and bool(projects)


def _consume_today(conn):
    today = datetime.now().strftime('%Y-%m-%d')
    row = conn.execute(
        'select count(*) calls, coalesce(sum(coalesce(actual_input,0)+coalesce(actual_output,0)),0) tokens,'
        ' sum(case when error is not null then 1 else 0 end) errors'
        ' from knowledge_calls where day=?', (today,)).fetchone()
    return dict(row)


def _legacy_state(conn):
    """Diagnose the original records/jobs backend without creating new tables."""
    counts = {r['state']:r['n'] for r in conn.execute('select state,count(*) n from jobs group by state')}
    failed = []
    for row in conn.execute('select j.id,s.project,s.session,j.attempts,j.error,j.created_at '
                            "from jobs j join sources s on s.id=j.source_id where j.state='failed'"):
        cls, action = classify_failure(row['error'] or '')
        created = _ts(row['created_at'])
        failed.append({'id':row['id'],'project':row['project'],'session':row['session'],
                       'attempts':row['attempts'],'reason':(row['error'] or '')[:240],
                       'class':cls,'age_hours':round((time.time()-created)/3600,1) if created else None,
                       'suggested_action':action,'triage':None})
    pending = conn.execute("select count(*) n,min(created_at) oldest from jobs where state in ('pending','retry','running')").fetchone()
    oldest = _ts(pending['oldest'])
    usage = dict(conn.execute("select count(*) calls,coalesce(sum(coalesce(input_tokens,0)+coalesce(output_tokens,0)),0) tokens,"
                             "sum(case when error is not null then 1 else 0 end) errors from usage "
                             "where date(created_at,'localtime')=date('now','localtime')").fetchone())
    return {'work':counts,'failed':failed,'stale_dependencies':[],
            'files':{'ok':None,'other':{},'scope':'legacy file reconciliation not performed by this read-only diagnostic'},
            'cards':{r['status']:r['n'] for r in conn.execute('select status,count(*) n from records group by status')},
            'oversized_cards':{'count':0,'cards':[],'scope':'knowledge cards not applicable to legacy backend'},
            'queue_age':{'ready':pending['n'],'oldest_hours':round((time.time()-oldest)/3600,1) if oldest else None},
            'usage_today':usage}


def diagnose(home: Path, root: Path):
    home = Path(home).resolve()
    conn = _connect(home)
    try:
        cfg = _config(home)
        legacy = cfg['mode'] not in ('memory','evolving')
        if legacy:
            backend = _legacy_state(conn)
        else:
            backend = {'work':_counts(conn),'failed':_failed_rows(conn),
                       'stale_dependencies':_stale_dependencies(conn),'files':_files(conn),
                       'cards':_cards(conn),'oversized_cards':_oversized_cards(conn),
                       'queue_age':_queue_age(conn),'usage_today':_consume_today(conn)}
        failed = backend['failed'];stale = backend['stale_dependencies']
        result = {
            'generated_at': datetime.now().astimezone().isoformat(timespec='seconds'),
            'home': str(home),
            'config': cfg,
            'backend': 'legacy' if legacy else 'knowledge',
            'scheduler': _launchd(),
            'cycle': _cycle(home),
            'capture': _sources(conn),
            'failed_by_class': {},
            'backup': _backup(home),
            'code': _code(root),
            **backend,
        }
    finally:
        conn.close()

    by_class = {}
    triaged = {}
    for item in failed:
        bucket = by_class if item['triage'] is None else triaged
        bucket.setdefault(item['class'], 0)
        bucket[item['class']] += 1
    result['failed_by_class'] = by_class
    result['triaged_by_class'] = triaged
    result['untriaged_failed'] = sum(by_class.values())

    reasons = []
    backup = result['backup']
    if backup.get('state') != 'ok':
        reasons.append('backup:' + str(backup.get('state')))
    elif isinstance(backup.get('age_hours'), (int, float)) and backup['age_hours'] > 36:
        reasons.append('backup older than 36h')
    cycle = result['cycle']
    if cycle.get('state') != 'ok':
        reasons.append('last cycle:' + str(cycle.get('state')))
    elif isinstance(cycle.get('age_seconds'), (int, float)) and cycle['age_seconds'] > 120:
        reasons.append('maintainer stale >120s')
    if result['failed_by_class'].get('unknown'):
        reasons.append('unknown untriaged failure class')
    if result['untriaged_failed']:
        reasons.append(f"{result['untriaged_failed']} untriaged failure(s)")
    if stale and _stale_is_actionable(result['config']):
        reasons.append(f'{len(stale)} stale dependency card(s)')
    if isinstance(result['queue_age'].get('oldest_hours'), (int, float)) and result['queue_age']['oldest_hours'] > 6:
        reasons.append('oldest ready work >6h')
    if result['code'].get('dirty'):
        reasons.append('working tree dirty')
    if not result['code'].get('remote'):
        reasons.append('no git remote')

    result['action_required'] = bool(reasons)
    result['reasons'] = reasons
    return result


def render_text(data):
    def line(label, value):
        return f'{label:<22} {value}'
    out = [f"tongle doctor  {data['generated_at']}"]
    out.append(line('home', data['home']))
    cfg = data['config']
    out.append(line('model', cfg.get('model')))
    out.append(line('budget/day', f"{cfg.get('daily_calls')} calls, {cfg.get('daily_tokens')} tokens"))
    out.append(line('dependency auto', 'on' if cfg.get('dependency_projects') else 'off'))
    sched = data['scheduler']
    out.append(line('scheduler', f"{sched.get('state')} runs={sched.get('runs')} exit={sched.get('last_exit')}"))
    cyc = data['cycle']
    out.append(line('last cycle', f"{cyc.get('state')} {cyc.get('age_seconds')}s errors={cyc.get('errors')}"))
    out.append(line('capture', f"latest={data['capture'].get('latest_source_at')}"))
    out.append(line('work', json.dumps(data['work'], ensure_ascii=False, sort_keys=True)))
    out.append(line('failed untriaged', json.dumps(data['failed_by_class'], ensure_ascii=False)))
    out.append(line('failed triaged', json.dumps(data.get('triaged_by_class', {}), ensure_ascii=False)))
    for item in data['failed']:
        mark = ' [已分类:' + str(item['triage']['action']) + ']' if item.get('triage') else ''
        out.append(f"  - {item['id'][:8]} {item['class']:<18} {item['age_hours']}h{mark} {item['reason'][:90]}")
        out.append(f"    下一步: {item['suggested_action']}")
    stale_names = ', '.join(x['owner'][:8] for x in data['stale_dependencies']) or 'none'
    if data['stale_dependencies'] and not _stale_is_actionable(cfg):
        stale_names += ' (auto off; historical)'
    out.append(line('stale deps', stale_names))
    out.append(line('files', f"ok={data['files']['ok']} other={data['files']['other']}"))
    out.append(line('cards', json.dumps(data['cards'], ensure_ascii=False, sort_keys=True)))
    out.append(line('oversized cards', f"{data['oversized_cards']['count']} >8000 chars"))
    for c in data['oversized_cards']['cards'][:5]:
        out.append(f"  - {c['id'][:8]} {c['body_chars']} chars v{c['version']} {c['title'][:50]}")
    out.append(line('queue age', json.dumps(data['queue_age'], ensure_ascii=False)))
    b = data['backup']
    out.append(line('backup', f"{b.get('state')} age={b.get('age_hours')}h size={b.get('bytes')} content={b.get('content_bytes')}"))
    out.append(line('usage today', json.dumps(data['usage_today'], ensure_ascii=False)))
    c = data['code']
    out.append(line('code', f"head={c.get('head')} dirty={c.get('dirty')} remote={c.get('remote')}"))
    out.append(line('status', 'ACTION REQUIRED' if data['action_required'] else 'OK'))
    if data['reasons']:
        out.append('reasons: ' + '; '.join(data['reasons']))
    return '\n'.join(out)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', default=str(ROOT / '.runtime'))
    parser.add_argument('--root', default=str(ROOT))
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    try:
        data = diagnose(Path(args.home), Path(args.root))
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({'error': str(exc)[:240]}))
        return 2
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    else:
        print(render_text(data))
    return 1 if data['action_required'] else 0


if __name__ == '__main__':
    sys.exit(main())
