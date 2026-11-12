"""Bounded, local, metadata-only observations. Readers never migrate the DB.

An observed native denominator is not proof of all native host events. Unknown
cost, delivery and benefit remain unknown. Writes are explicit, best effort and
never commit an enclosing caller transaction. No prompt/body/path is accepted.
"""
from collections import Counter
from contextlib import contextmanager
import hashlib
import json
import math
from pathlib import Path
import re
import time

MAX_EVENTS = 10000
EVENT_DAYS = 14
MAX_AGGREGATES = 10000
AGGREGATE_DAYS = 90
MAX_ALERTS = 256
HOSTS = ('pi', 'codex', 'dsh')
NATIVE_MANIFEST_HOSTS = ('pi', 'codex')
OPERATIONS = {'capture', 'context', 'ingest', 'knowledge_read', 'knowledge_finish', 'knowledge_review', 'knowledge_write', 'publication', 'index',
              'maintenance', 'host_delivery', 'answer', 'model_call'}
STATUSES = {'expected', 'success', 'failed', 'degraded', 'skipped', 'no_change',
            'unknown', 'incomplete', 'prepared'}
VERSION_KEYS = ('code', 'config', 'retrieval', 'index', 'model')
STAGES = {'total', 'parse', 'normalize', 'capture', 'sync', 'index', 'candidates',
          'rank', 'render', 'record', 'commit', 'publish', 'read', 'write',
          'selection', 'lexical', 'semantic', 'rerank', 'backup', 'graph'}
METRICS = {'graph_added_candidates', 'graph_discovered_candidates',
           'graph_examined_edges', 'graph_pending'}
OPERATION_STAGES = {
    'capture': ('parse', 'capture', 'commit'),
    'ingest': ('read', 'parse', 'index', 'commit'),
    'knowledge_read': ('read',), 'knowledge_finish': ('read', 'commit'), 'knowledge_review': ('read', 'commit'),
    'context': ('sync', 'index', 'lexical', 'semantic', 'rank', 'selection', 'render', 'record', 'graph'),
    'knowledge_write': ('read', 'write', 'commit'),
    'publication': ('publish',), 'index': ('index',),
    'maintenance': ('sync', 'index', 'backup'), 'answer': ('capture',),
}
_CODE_CACHE = {}


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def _digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _table(engine, name):
    return bool(engine.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def _code_hash(root, names):
    paths = sorted(p for name in names for p in root.glob(name) if p.is_file())
    signature = tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in paths)
    if signature not in _CODE_CACHE:
        digest = hashlib.sha256()
        for p in paths:
            digest.update(str(p.relative_to(root)).encode())
            digest.update(p.read_bytes())
        if len(_CODE_CACHE) > 8:
            _CODE_CACHE.clear()
        _CODE_CACHE[signature] = digest.hexdigest() if paths else 'unknown'
    return _CODE_CACHE[signature]


def version_fingerprint(engine):
    """Hash actual implementation and config bytes, never expose config values."""
    root = Path(__file__).resolve().parents[1]
    try:
        code = _code_hash(root, ('tongle/*.py', 'scripts/*.py', 'scripts/*.ts', 'adapters/*.ts', 'adapters/*.py'))
        retrieval = _code_hash(root, ('tongle/memory*.py', 'tongle/context*.py', 'tongle/retriev*.py',
                                      'tongle/knowledge_index.py', 'tongle/semantic_index.py',
                                      'tongle/local_models.py', 'tongle/evolution.py',
                                      'tongle/source_memory.py', 'tongle/inline_notes.py',
                                      'tongle/knowledge_files.py', 'tongle/knowledge_organization.py',
                                      'tongle/online_*.py', 'scripts/retrieval_judge.py',
                                      'scripts/retrieval_judge_guard.ts'))
    except OSError:
        code = retrieval = 'unknown'
    settings = {}
    try:
        raw = (engine.home / 'config.json').read_bytes()
        cfg = hashlib.sha256(raw).hexdigest()
        decoded = json.loads(raw)
        settings = decoded if isinstance(decoded, dict) else {}
    except (OSError, ValueError):
        cfg = 'unknown'
    configured = settings.get('retrieval', {})
    models = {}
    if isinstance(configured, dict) and configured.get('strategy') in ('hybrid', 'rerank', 'online_admission'):
        roles = ('embedding_model',) if configured.get('strategy') == 'online_admission' else ('embedding_model', 'reranker')
        for role in roles:
            model = configured.get(role)
            if not isinstance(model, dict) or not isinstance(model.get('revision'), str) or not model['revision']:
                continue
            # This is declared provenance, not a claim that inference succeeded.
            models[role] = {field: model.get(field, 'unknown') for field in
                            ('revision', 'sha256', 'tokenizer_sha256')}
            models[role]['configuration_key'] = _digest(_json(model))
        if configured.get('strategy') == 'online_admission':
            judge = configured.get('online_admission')
            if isinstance(judge, dict):
                # Declared configuration only; an observed successful call is
                # still required to establish provider/model availability.
                models['online_admission'] = {'configuration_key': _digest(_json(judge))}
    model_digest = _digest(_json(models)) if models else 'unknown'
    index_digest = 'unknown'
    if _table(engine, 'meta'):
        revisions = {}
        for name, table in (('knowledge_index_revision', 'knowledge_search'),
                            ('source_index_revision', 'source_memory_indexed'),
                            ('semantic_index_revision', 'knowledge_vectors')):
            if not _table(engine, table):
                continue
            row = engine.db.execute('SELECT value FROM meta WHERE key=?', (name,)).fetchone()
            if row and str(row[0]).isdigit():
                revisions[name] = str(row[0])
        if revisions:
            index_digest = _digest(_json({'generations': revisions, 'model': model_digest}))
    return {'code': code, 'config': cfg, 'retrieval': retrieval,
            'index': index_digest, 'model': model_digest}


def _versions(engine, supplied):
    versions = version_fingerprint(engine) if supplied is None else supplied
    if not isinstance(versions, dict) or set(versions) - set(VERSION_KEYS):
        raise ValueError('invalid version metadata')
    result = {}
    for key in VERSION_KEYS:
        value = versions.get(key, 'unknown')
        if value is None or value == 'unknown':
            result[key] = 'unknown'
        elif isinstance(value, str) and value and len(value) <= 256:
            # Labels can accidentally include private paths; persist only hashes.
            result[key] = value if re.fullmatch('[a-f0-9]{64}', value) else _digest(value)
        else:
            raise ValueError('invalid version fingerprint')
    return result


def _timing(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
        raise ValueError('invalid stage timing')
    return float(value)


@contextmanager
def _transaction(engine):
    # Call only after checking there is no caller-owned transaction. SQLite
    # can leave a transaction open when COMMIT itself fails.
    try:
        with engine.transaction():
            yield
    except BaseException:
        if engine.db.in_transaction:
            engine.db.rollback()
        raise


def _schema(engine):
    engine.db.execute('''CREATE TABLE IF NOT EXISTS runtime_observations (
        event_key TEXT PRIMARY KEY, project TEXT NOT NULL, host TEXT NOT NULL,
        operation TEXT NOT NULL, status TEXT NOT NULL, observed_at REAL NOT NULL,
        native_key TEXT, expected INTEGER NOT NULL, elapsed_ms REAL,
        stages TEXT NOT NULL, versions TEXT NOT NULL, reason_code TEXT,
        payload_hash TEXT NOT NULL, cache_state TEXT NOT NULL DEFAULT 'unknown')''')
    columns = {row[1] for row in engine.db.execute('PRAGMA table_info(runtime_observations)')}
    if 'cache_state' not in columns:
        engine.db.execute("ALTER TABLE runtime_observations ADD COLUMN cache_state TEXT NOT NULL DEFAULT 'unknown'")
    engine.db.execute('''CREATE TABLE IF NOT EXISTS runtime_daily (
        bucket TEXT PRIMARY KEY, day INTEGER NOT NULL, project TEXT NOT NULL,
        host TEXT NOT NULL, operation TEXT NOT NULL, status TEXT NOT NULL,
        versions TEXT NOT NULL, count INTEGER NOT NULL, elapsed_known INTEGER NOT NULL,
        elapsed_unknown INTEGER NOT NULL, elapsed_sum REAL NOT NULL)''')
    engine.db.execute('''CREATE TABLE IF NOT EXISTS runtime_alerts (
        alert_key TEXT PRIMARY KEY, project TEXT NOT NULL, category TEXT NOT NULL,
        severity INTEGER NOT NULL, reason_code TEXT NOT NULL, first_seen REAL NOT NULL,
        last_seen REAL NOT NULL, changed_at REAL NOT NULL, occurrences INTEGER NOT NULL,
        transitions INTEGER NOT NULL, transition TEXT NOT NULL, acknowledged INTEGER NOT NULL)''')
    # Additive tables keep the old fixed-column writes usable on code rollback.
    engine.db.execute('''CREATE TABLE IF NOT EXISTS runtime_observation_metrics (
        event_key TEXT PRIMARY KEY, metrics TEXT NOT NULL)''')
    engine.db.execute('''CREATE TABLE IF NOT EXISTS runtime_reason_daily (
        bucket TEXT PRIMARY KEY, day INTEGER NOT NULL, project TEXT NOT NULL,
        host TEXT NOT NULL, operation TEXT NOT NULL, status TEXT NOT NULL,
        reason_code TEXT NOT NULL, count INTEGER NOT NULL)''')


def record_event(engine, *, event_id, project, host, operation, status,
                 elapsed_ms=None, stages=None, native_event_id=None, expected=False,
                 version_info=None, reason_code=None, now=None, cache_state='unknown', metrics=None):
    """Return recorded/replayed/warning. Stable event ids are idempotent while retained.

    A retry with changed metadata is rejected, not counted twice. ``expected``
    requires a native identity and marks independently observed denominator
    evidence; consumers must never derive it from their own successful writes.
    """
    try:
        if not isinstance(event_id, str) or not event_id or not isinstance(project, str) or not project:
            raise ValueError('missing event identity')
        if cache_state not in ('cold', 'warm', 'bypass', 'unknown'):
            raise ValueError('invalid cache state')
        if host not in HOSTS + ('cc', 'unknown') or operation not in OPERATIONS or status not in STATUSES:
            raise ValueError('invalid event enum')
        if type(expected) is not bool or (expected and not native_event_id):
            raise ValueError('expected event requires native identity')
        if native_event_id is not None and (not isinstance(native_event_id, str) or not native_event_id):
            raise ValueError('invalid native identity')
        if reason_code is not None and (not isinstance(reason_code, str) or not re.fullmatch('[a-z0-9_]{1,64}', reason_code)):
            raise ValueError('invalid reason code')
        stage_values = {} if stages is None else stages
        if not isinstance(stage_values, dict) or set(stage_values) - STAGES:
            raise ValueError('invalid timing stage')
        stage_values = {k: _timing(v) for k, v in stage_values.items()}
        metrics = {} if metrics is None else metrics
        if (not isinstance(metrics, dict) or set(metrics) - METRICS or
                any(type(v) is not int or not 0 <= v <= 1000000 for v in metrics.values())):
            raise ValueError('invalid observation metric')
        elapsed_ms = _timing(elapsed_ms)
        versions = _json(_versions(engine, version_info))
        observed = time.time() if now is None else _timing(now)
        if observed is None:
            raise ValueError('invalid observation time')
        key = _digest(_json([project, host, operation, event_id]))
        native_key = _digest(native_event_id) if native_event_id else None
        stages_json = _json(stage_values)
        # Elapsed timing can differ on retries; identity/status/evidence may not.
        payload = _digest(_json([project, host, operation, status, native_key,
                                expected, versions, reason_code]))
        with engine.locked():
            if engine.db.in_transaction:
                return {'recorded': False, 'warning': 'observation_active_transaction'}
            with _transaction(engine):
                _schema(engine)
                old = engine.db.execute('SELECT payload_hash FROM runtime_observations WHERE event_key=?', (key,)).fetchone()
                if old:
                    if old[0] != payload:
                        return {'recorded': False, 'warning': 'observation_identity_conflict'}
                    return {'recorded': True, 'replayed': True}
                engine.db.execute('INSERT INTO runtime_observations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                  (key, project, host, operation, status, observed, native_key,
                                   int(expected), elapsed_ms, stages_json, versions, reason_code, payload, cache_state))
                if metrics or 'graph' in stage_values:
                    engine.db.execute('INSERT INTO runtime_observation_metrics VALUES(?,?)', (key, _json(metrics)))
                day = int(observed // 86400)
                bucket = _digest(_json([day, project, host, operation, status, versions]))
                engine.db.execute('''INSERT INTO runtime_daily VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(bucket) DO UPDATE SET count=count+1,
                    elapsed_known=elapsed_known+excluded.elapsed_known,
                    elapsed_unknown=elapsed_unknown+excluded.elapsed_unknown,
                    elapsed_sum=elapsed_sum+excluded.elapsed_sum''',
                    (bucket, day, project, host, operation, status, versions, 1,
                     int(elapsed_ms is not None), int(elapsed_ms is None), elapsed_ms or 0.0))
                if status in ('failed', 'degraded'):
                    cause = reason_code or 'unknown'
                    reason_bucket = _digest(_json([day, project, host, operation, status, cause]))
                    engine.db.execute('''INSERT INTO runtime_reason_daily VALUES(?,?,?,?,?,?,?,1)
                        ON CONFLICT(bucket) DO UPDATE SET count=count+1''',
                        (reason_bucket, day, project, host, operation, status, cause))
                engine.db.execute('DELETE FROM runtime_observations WHERE observed_at<?', (observed - EVENT_DAYS * 86400,))
                engine.db.execute('DELETE FROM runtime_observations WHERE rowid NOT IN (SELECT rowid FROM runtime_observations ORDER BY rowid DESC LIMIT ?)', (MAX_EVENTS,))
                engine.db.execute('DELETE FROM runtime_daily WHERE day<?', (day - AGGREGATE_DAYS + 1,))
                engine.db.execute('DELETE FROM runtime_daily WHERE rowid NOT IN (SELECT rowid FROM runtime_daily ORDER BY day DESC, rowid DESC LIMIT ?)', (MAX_AGGREGATES,))
                engine.db.execute('DELETE FROM runtime_observation_metrics WHERE event_key NOT IN (SELECT event_key FROM runtime_observations)')
                engine.db.execute('DELETE FROM runtime_reason_daily WHERE day<?', (day - AGGREGATE_DAYS + 1,))
                engine.db.execute('DELETE FROM runtime_reason_daily WHERE rowid NOT IN (SELECT rowid FROM runtime_reason_daily ORDER BY day DESC,rowid DESC LIMIT ?)', (MAX_AGGREGATES,))
        return {'recorded': True, 'replayed': False}
    except Exception as exc:
        # Do not leak exception text, native identity or private paths.
        return {'recorded': False, 'warning': 'observation_' + type(exc).__name__}


def record_alert_state(engine, *, project, category, severity, reason_code='none', now=None):
    """Persist state transitions; notify only on new/change/worsening/recovery.

    Acknowledgement is deliberately independent of resolution. Calling this
    method is explicit maintenance work; report/attention readers do not call it.
    """
    if type(severity) is not int or not 0 <= severity <= 3:
        raise ValueError('invalid alert severity')
    if not all(isinstance(v, str) and re.fullmatch('[a-z0-9_]{1,64}', v) for v in (category, reason_code)):
        raise ValueError('invalid alert code')
    stamp = time.time() if now is None else _timing(now)
    key = _digest(_json([project, category]))
    with engine.locked():
        if engine.db.in_transaction:
            return {'recorded': False, 'notify': False, 'warning': 'observation_active_transaction'}
        with _transaction(engine):
            _schema(engine)
            row = engine.db.execute('SELECT * FROM runtime_alerts WHERE alert_key=?', (key,)).fetchone()
            old = dict(row) if row else None
            changed = old is None or old['severity'] != severity or old['reason_code'] != reason_code
            transition = ('recovered' if old and old['severity'] and not severity else
                          'new' if not old else 'worsened' if severity > old['severity'] else 'changed')
            notify = changed and bool(severity or (old and old['severity']))
            if not old:
                engine.db.execute('INSERT INTO runtime_alerts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    (key, project, category, severity, reason_code, stamp, stamp, stamp, 1, 1, transition, 0))
            else:
                engine.db.execute('''UPDATE runtime_alerts SET severity=?, reason_code=?, last_seen=?,
                    changed_at=?, occurrences=occurrences+1, transitions=?, transition=?, acknowledged=? WHERE alert_key=?''',
                    (severity, reason_code, stamp, stamp if changed else old['changed_at'],
                     old['transitions'] + int(changed), transition if changed else old['transition'],
                     0 if changed else old['acknowledged'], key))
            engine.db.execute('DELETE FROM runtime_alerts WHERE alert_key NOT IN (SELECT alert_key FROM runtime_alerts ORDER BY last_seen DESC, rowid DESC LIMIT ?)', (MAX_ALERTS,))
    return {'recorded': True, 'notify': notify, 'transition': transition if changed else 'unchanged', 'severity': severity}


def acknowledge_alert(engine, project, category):
    """Acknowledge visibility; leave the observed fault severity unchanged."""
    with engine.locked():
        if engine.db.in_transaction or not _table(engine, 'runtime_alerts'):
            return False
        with _transaction(engine):
            result = engine.db.execute('UPDATE runtime_alerts SET acknowledged=1 WHERE alert_key=?', (_digest(_json([project, category])),))
        return bool(result.rowcount)


def _distribution(values, total):
    values = sorted(v for v in values if type(v) in (int, float) and math.isfinite(v) and v >= 0)
    def percentile(p):
        return values[max(0, math.ceil(len(values) * p) - 1)] if values else None
    return {'known': len(values), 'unknown': total - len(values),
            'p50_ms': percentile(.5), 'p95_ms': percentile(.95)}


def _decoded(value, fallback):
    try:
        result = json.loads(value)
        return result if isinstance(result, dict) else fallback
    except (ValueError, TypeError):
        return fallback


def observation_report(engine, project, days=30, now=None):
    """Strictly read-only report, including on a legacy/uninitialised schema."""
    if type(days) is not int or not 1 <= days <= 3650:
        raise ValueError('observation days must be between 1 and 3650')
    stamp = time.time() if now is None else _timing(now)
    result = {'project': project, 'window_days': days, 'available': False,
        'retention': {'events': MAX_EVENTS, 'event_days': EVENT_DAYS,
                      'aggregate_days': AGGREGATE_DAYS, 'aggregate_buckets': MAX_AGGREGATES,
                      'pruning': 'on writes; readers hide expired records without deleting them'},
        'history': 'retained observations only; idempotency and native pairing bounded by event retention',
        'host_delivery': 'unknown', 'benefit': 'unknown', 'cost': {'value': None, 'state': 'unknown'},
        'hosts': {}, 'operations': {}, 'timings': {}, 'cache_timings': {}, 'versions': [], 'alerts': [],
        'aggregates': [], 'running_state': 'unknown', 'native_manifests': [],
        'manifest_scope': 'only explicitly imported native metadata exports; none proves all host events',
        'self_reported_origin': True,
        'manifest_overlap_note': 'exports may overlap; host denominators deduplicate native event identities',
        'unmatched_expected_note': 'unmatched observations are not proof of missed capture; retention or instrumentation can omit the numerator'}
    rows = []
    if _table(engine, 'runtime_observations'):
        rows = [dict(r) for r in engine.db.execute('SELECT * FROM runtime_observations WHERE project=? AND observed_at>=? AND observed_at<=? ORDER BY observed_at', (project, stamp - min(days, EVENT_DAYS) * 86400, stamp))]
        result['available'] = True
    for host in HOSTS:
        host_rows = [r for r in rows if r['host'] == host]
        expected = {r['native_key'] for r in host_rows if r['operation'] == 'capture' and r['expected'] and r['native_key']}
        captured = {r['native_key'] for r in host_rows if r['operation'] == 'capture' and r['status'] == 'success' and r['native_key']}
        result['hosts'][host] = {'observations': len(host_rows), 'expected_native_events': len(expected) if expected else None,
            'captured_expected_events': len(expected & captured) if expected else None,
            'received_capture_events': len(captured),
            'observed_capture_rate': round(len(expected & captured) / len(expected), 6) if expected else None,
            'all_native_capture_rate': None, 'denominator_scope': 'independently observed expected native events; all native events unknown',
            'state': 'observed' if host_rows else 'unknown',
            'native_manifest_support': 'supported' if host in NATIVE_MANIFEST_HOSTS else 'unsupported'}
    result['excluded_host_observations'] = dict(Counter(r['host'] for r in rows if r['host'] not in HOSTS))
    for operation in sorted({r['operation'] for r in rows}):
        selected = [r for r in rows if r['operation'] == operation]
        result['operations'][operation] = dict(Counter(r['status'] for r in selected))
        result['cache_timings'][operation] = {cache: _distribution(
            [r['elapsed_ms'] for r in selected if r.get('cache_state', 'unknown') == cache and r['elapsed_ms'] is not None],
            sum(r.get('cache_state', 'unknown') == cache for r in selected))
            for cache in ('cold', 'warm', 'bypass', 'unknown')}
        times = {'total': [r['elapsed_ms'] for r in selected if r['elapsed_ms'] is not None]}
        decoded = []
        for row in selected:
            try:
                decoded.append(json.loads(row['stages']))
            except (ValueError, TypeError):
                decoded.append({})
        for stage in sorted(set(OPERATION_STAGES.get(operation, ())) | {s for d in decoded if isinstance(d, dict) for s in d}):
            if stage == 'total':
                continue
            times[stage] = [d[stage] for d in decoded if isinstance(d, dict) and d.get(stage) is not None]
        result['timings'][operation] = {stage: _distribution(values, len(selected)) for stage, values in times.items()}
    versions = Counter(r['versions'] for r in rows)
    result['versions'] = [{'fingerprints': _decoded(v, {'state': 'unknown'}), 'observations': count} for v, count in versions.items()]
    result['diagnostic_coverage'] = 'Recorded outcomes only; SQLite contention can omit events. Reason buckets start with instrumented code, not backfilled history.'
    result['reason_aggregates'] = None
    if _table(engine, 'runtime_reason_daily'):
        result['reason_aggregates'] = [dict(r) for r in engine.db.execute('''
            SELECT day,host,operation,status,reason_code,count FROM runtime_reason_daily
            WHERE project=? AND day>=? AND day<=? ORDER BY day,host,operation,reason_code''',
            (project, int(stamp//86400)-min(days, AGGREGATE_DAYS)+1, int(stamp//86400)))]
    result['metrics'] = None
    if _table(engine, 'runtime_observation_metrics'):
        measured = [_decoded(r[0], {}) for r in engine.db.execute('''
            SELECT m.metrics FROM runtime_observation_metrics m JOIN runtime_observations o USING(event_key)
            WHERE o.project=? AND o.observed_at>=? AND o.observed_at<=?''',
            (project, stamp-min(days, EVENT_DAYS)*86400, stamp))]
        result['metrics'] = {key: {'known': len(values), 'sum': sum(values) if values else None}
            for key in sorted(METRICS)
            for values in [[d[key] for d in measured if type(d.get(key)) is int and 0<=d[key]<=1000000]]}
    if _table(engine, 'runtime_daily'):
        result['aggregates'] = [dict(r) for r in engine.db.execute('''SELECT day,host,operation,status,versions,count,elapsed_known,elapsed_unknown,elapsed_sum
            FROM runtime_daily WHERE project=? AND day>=? AND day<=? ORDER BY day,host,operation''', (project, int(stamp//86400)-min(days, AGGREGATE_DAYS)+1, int(stamp//86400))) ]
        for item in result['aggregates']:
            item['versions'] = _decoded(item['versions'], {'state': 'unknown'})
        result['aggregate_note'] = 'UTC calendar-day buckets; boundary day may include events before window start; quantiles use retained events only'
    if _table(engine, 'runtime_alerts'):
        result['alerts'] = [dict(r) for r in engine.db.execute('''SELECT category,severity,reason_code,first_seen,last_seen,changed_at,occurrences,transitions,transition,acknowledged
            FROM runtime_alerts WHERE project=? ORDER BY severity DESC,changed_at DESC''', (project,))]
    if _table(engine, 'runtime_native_manifests'):
        result['native_manifests'] = [dict(row, self_reported_origin=True,
            verification='not_independently_verified') for row in engine.db.execute(
            'SELECT * FROM runtime_native_manifests WHERE project=? AND imported_at>=? ORDER BY imported_at DESC',
            (project, stamp-AGGREGATE_DAYS*86400))]
        for manifest in result['native_manifests']:
            manifest.setdefault('origin', 'native_session_export')
            manifest['denominator_scope'] = ('received native hook events only; missing-hook coverage unknown'
                if manifest['origin'] == 'native_hook_audit' else 'listed native session events only; all-host coverage unknown')
            manifest['timestamp_semantics'] = 'observation time, not event occurrence time'
    return result


def pending_native_capture(home, project):
    """Read bounded adapter metadata only; never infer a native denominator."""
    path = Path(home) / 'adapter-state' / 'transcripts.json'
    result = {'state': 'unknown', 'pending_turns': None, 'expected_native_events': None}
    try:
        if not path.exists():
            return result
        if path.is_symlink() or path.parent.is_symlink():
            return dict(result, reason='unsafe_adapter_state')
        with path.open('rb') as stream:
            raw = stream.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            return dict(result, reason='adapter_state_exceeds_bound')
        state = json.loads(raw)
        groups = state.get('codex_pending_prompts', {})
        replies = state.get('codex_reply_pending', {})
        if not isinstance(groups, dict) or len(groups) > 128 or not isinstance(replies,dict) or len(replies)>128:
            return dict(result, reason='invalid_adapter_state')
        selected = [g for g in groups.values() if isinstance(g, dict) and g.get('project') == project]
        statuses = Counter(g.get('status') if g.get('status') in (
            'awaiting_native_message', 'native_messages_captured', 'identity_unresolved') else 'unknown' for g in selected)
        return dict(result, state='observed', pending_turns=sum(g.get('status') != 'native_messages_captured'
            or bool(g.get('receipt_pending')) for g in selected), retained_turns=len(selected), statuses=dict(statuses),
            receipt_association_pending=sum(bool(g.get('receipt_pending')) for g in selected),
            pending_answer_receipts=sum(isinstance(g,dict) and g.get('project')==project for g in replies.values()),
            partial_scan_turns=sum(bool(g.get('partial_scan')) for g in selected),
            note='observed hook queue only; no prompt bodies, paths or inferred native coverage')
    except (OSError, ValueError, TypeError, AttributeError):
        return dict(result, reason='adapter_state_unavailable')


def report(engine, project, days=30):
    """Unified runtime + consumption + maintenance report for a read-only CLI."""
    from .usage_report import usage_report
    from .health import maintenance_snapshot
    result = observation_report(engine, project, days)
    result['consumption'] = usage_report(engine, project, days)
    if project != '__instance__':
        result['instance'] = observation_report(engine, '__instance__', days)
    result['maintenance'] = maintenance_snapshot(engine.home)
    result['running_state'] = result['maintenance']['state']
    result['native_capture_pending'] = pending_native_capture(engine.home, project)
    return result


def read_only_report(home, project, days=30):
    """CLI-safe reader: never construct Engine/Store, which initialize schemas."""
    import sqlite3
    from types import SimpleNamespace
    home = Path(home).expanduser().absolute()
    path = home / 'state.sqlite3'
    if not path.is_file() or path.is_symlink():
        raise ValueError('report requires an existing regular database')
    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA query_only=ON')
        connection.execute('PRAGMA trusted_schema=OFF')
        connection.execute('BEGIN')
        return report(SimpleNamespace(home=home, db=connection), project, days)
    finally:
        connection.close()


MAX_NATIVE_MANIFESTS = 64
MAX_MANIFEST_EVENTS = 1000


def _validate_native_manifest(host, project, data, now):
    required = {'schema', 'host', 'project', 'origin', 'source_sha256', 'exported_at', 'events'}
    if not isinstance(data, dict) or set(data) != required or type(data.get('schema')) is not int or data['schema'] != 1:
        raise ValueError('invalid native manifest schema or fields')
    if host not in NATIVE_MANIFEST_HOSTS or data['host'] != host or data['project'] != project:
        raise ValueError('native manifest host/project mismatch')
    if not isinstance(project, str) or not project or len(project) > 200:
        raise ValueError('invalid manifest project')
    if data['origin'] not in ('native_session_export', 'native_hook_audit'):
        raise ValueError('expected denominator must not derive from Tongle sources/events/receipts')
    if not isinstance(data['source_sha256'], str) or not re.fullmatch('[a-f0-9]{64}', data['source_sha256']):
        raise ValueError('native export digest required')
    exported = _timing(data['exported_at'])
    if exported is None or exported <= 0 or exported > now + 300:
        raise ValueError('invalid native export timestamp')
    events = data['events']
    if not isinstance(events, list) or not 1 <= len(events) <= MAX_MANIFEST_EVENTS:
        raise ValueError('native manifest must contain between 1 and 1000 metadata events')
    seen = set()
    for event in events:
        if not isinstance(event, dict) or set(event) != {'event_id', 'session_id', 'role', 'observed_at'}:
            raise ValueError('native event metadata fields only; bodies and derived records are forbidden')
        identity, session, role = event['event_id'], event['session_id'], event['role']
        if any(not isinstance(value, str) or not value or len(value) > bound or any(ord(c) < 32 for c in value)
               for value, bound in ((identity, 600), (session, 200))):
            raise ValueError('invalid native event identity')
        if role not in ('user', 'assistant') or not identity.startswith(host + ':' + session + ':'):
            raise ValueError('native event role/session/host mismatch')
        suffix = identity[len(host + ':' + session + ':'):]
        if host == 'codex':
            if not suffix.endswith(':' + role) or not suffix[:-len(role)-1]:
                raise ValueError('Codex native turn and role identity required')
            if suffix.startswith('fallback:'):
                raise ValueError('fallback identity is not independently native')
        elif not suffix.startswith('u:' if role == 'user' else 'a:') or len(suffix) <= 2:
            raise ValueError('Pi native role identity required')
        observed = _timing(event['observed_at'])
        if observed is None or observed <= 0 or observed > exported:
            raise ValueError('invalid native event timestamp')
        if identity in seen:
            raise ValueError('duplicate native event identity')
        seen.add(identity)
    return sorted(events, key=lambda event: event['event_id'])


def import_native_manifest(engine, host, project, data):
    """Import independently sourced *metadata*, with self-reported provenance.

    The origin/hash are operator declarations, not independent verification.
    This cannot establish all-host completeness. Missing captured observations
    may reflect retention or instrumentation, rather than missed capture.
    Validation completes before any mutation. Partial write failures are visible
    and retryable; each native expected identity is stable and idempotent.
    """
    now = time.time()
    events = _validate_native_manifest(host, project, data, now)
    canonical = dict(data, events=events)
    digest = _digest(_json(canonical))
    scope = {'manifest_sha256': digest, 'source_sha256': data['source_sha256'],
        'host': host, 'project': project, 'event_count': len(events),
        'origin': data['origin'], 'self_reported_origin': True,
        'verification': 'not_independently_verified',
        'timestamp_semantics': 'observed_at is observation time, not claimed event occurrence time',
        'denominator_scope': ('only received native hook events listed in this audit; missing-hook coverage and all native events remain unknown'
            if data['origin'] == 'native_hook_audit' else
            'only events listed in this native session metadata export; all host sessions/events remain unknown')}
    with engine.locked():
        if engine.db.in_transaction:
            return {'state': 'not_imported', 'warning': 'observation_active_transaction', 'manifest_scope': scope}
        if _table(engine, 'runtime_native_manifests'):
            row = engine.db.execute('SELECT state FROM runtime_native_manifests WHERE manifest_sha256=?', (digest,)).fetchone()
            if row and row[0] == 'complete':
                return {'state': 'complete', 'imported': 0, 'replayed': len(events), 'failed': 0, 'manifest_scope': scope}
    imported = replayed = failed = 0
    warnings = set()
    for event in events:
        result = record_event(engine, event_id='native-manifest:' + event['event_id'],
            project=project, host=host, operation='capture', status='expected',
            native_event_id=event['event_id'], expected=True,
            version_info={}, reason_code='native_manifest_expected', now=event['observed_at'])
        if result.get('recorded'):
            replayed += bool(result.get('replayed'))
            imported += not bool(result.get('replayed'))
        else:
            failed += 1; warnings.add(result.get('warning', 'observation_not_recorded'))
    state = 'partial' if failed else 'complete'
    with engine.locked():
        if engine.db.in_transaction:
            return {'state': 'partial', 'imported': imported, 'replayed': replayed, 'failed': failed,
                    'manifest_scope': scope, 'warning': 'manifest_receipt_not_committed'}
        with _transaction(engine):
            engine.db.execute('''CREATE TABLE IF NOT EXISTS runtime_native_manifests (
                manifest_sha256 TEXT PRIMARY KEY, source_sha256 TEXT NOT NULL,
                project TEXT NOT NULL, host TEXT NOT NULL, imported_at REAL NOT NULL,
                exported_at REAL NOT NULL, event_count INTEGER NOT NULL, state TEXT NOT NULL,
                origin TEXT NOT NULL DEFAULT 'native_session_export')''')
            columns = {row[1] for row in engine.db.execute('PRAGMA table_info(runtime_native_manifests)')}
            if 'origin' not in columns:
                engine.db.execute("ALTER TABLE runtime_native_manifests ADD COLUMN origin TEXT NOT NULL DEFAULT 'native_session_export'")
            engine.db.execute('''INSERT INTO runtime_native_manifests VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(manifest_sha256) DO UPDATE SET state=excluded.state''',
                (digest, data['source_sha256'], project, host, now, data['exported_at'], len(events), state, data['origin']))
            engine.db.execute('DELETE FROM runtime_native_manifests WHERE imported_at<?', (now-AGGREGATE_DAYS*86400,))
            engine.db.execute('DELETE FROM runtime_native_manifests WHERE rowid NOT IN (SELECT rowid FROM runtime_native_manifests ORDER BY imported_at DESC,rowid DESC LIMIT ?)', (MAX_NATIVE_MANIFESTS,))
    result = {'state': state, 'imported': imported, 'replayed': replayed, 'failed': failed, 'manifest_scope': scope}
    if warnings:
        result['warning'] = ','.join(sorted(warnings))
    return result
