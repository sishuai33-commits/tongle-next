"""Best-effort CLI outcome observation without initializing the core schema.

This module never reads stdin, prompts or note bodies. Exception messages are
matched only against fixed built-in contracts; no message text is persisted. It
records observed outcomes, not all host events or every retry attempt. Stable
outcome suffixes let a failed event later succeed without counting a replay twice.
"""
from contextlib import contextmanager
import math
import json
from pathlib import Path
import sqlite3
import uuid

from .observability import record_event, STAGES, METRICS


def failure_reason(error):
    """Closed labels only: expected guard rejections are not system failures."""
    # Exact contracts avoid substring guessing on arbitrary private exception text.
    message = error.args[0] if error.args and isinstance(error.args[0], str) else None
    contracts = {
        'assistant inference must remain hypothesis': 'validation_authority',
        'external claims must remain reported': 'validation_authority',
        'user authority requires user evidence': 'evidence_authority',
        'external authority requires document evidence': 'evidence_authority',
        'evidence source project mismatch': 'scope_rejected',
        'evidence source version mismatch': 'state_changed',
        'unknown source': 'evidence_identity',
        'evidence source unknown (not a source id)': 'evidence_identity',
        'update target not in search candidates': 'search_scope',
        'unknown project-bound search_id; search first': 'search_required',
        'amend requires a bounded note with valid metadata; use normal update': 'amend_requires_update',
        'catalog changed; search again': 'state_changed',
        'target version changed': 'state_changed',
        'event_key reused with different action or content': 'identity_conflict',
        'shared cards are read-only': 'scope_rejected',
        'card not found in project': 'scope_rejected',
        'source outside project': 'scope_rejected',
        'source unavailable': 'source_unavailable',
        'learned note must have been written by this session': 'session_scope',
    }
    if isinstance(error, (ValueError, KeyError)) and message in contracts:
        return contracts[message]
    if isinstance(message, str):
        for kind, prefix, code in (
            (ValueError, 'evidence source unknown (not a source id): ', 'evidence_identity'),
            (ValueError, 'evidence source unavailable: ', 'source_unavailable'),
            (ValueError, 'evidence quote must be an exact substring', 'evidence_quote'),
            (KeyError, 'unknown source: ', 'evidence_identity'),
            (KeyError, 'unknown source/version ', 'evidence_identity')):
            if isinstance(error, kind) and message.startswith(prefix): return code
    if isinstance(error, json.JSONDecodeError): return 'invalid_json'
    if isinstance(error, TimeoutError): return 'operation_timeout'
    if isinstance(error, PermissionError): return 'permission_denied'
    if isinstance(error, FileNotFoundError): return 'file_unavailable'
    if isinstance(error, sqlite3.Error):
        code = getattr(error, 'sqlite_errorcode', 0) or 0
        return 'database_busy' if code & 255 in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) else 'database_error'
    if isinstance(error, OSError): return 'io_error'
    if isinstance(error, ValueError): return 'validation_rejected'
    return 'operation_error_unknown'


def retrieval_reason(retrieval):
    """Use existing closed transport/guard codes, never caller-supplied labels."""
    from .online_admission import TRANSPORT_FAILURE_CODES, GUARD_FAILURE_CODES
    from .online_transport import RESPONSE_VALIDATION_CODES
    raw = retrieval.get('raw')
    raw = raw if isinstance(raw, dict) else {}
    online = raw.get('online_admission', retrieval.get('online_admission'))
    online = online if isinstance(online, dict) else {}
    transport = online.get('transport')
    transport = transport if isinstance(transport, dict) else {}
    validation = online.get('response_validation_code', transport.get('response_validation_code'))
    if isinstance(validation, str) and validation in RESPONSE_VALIDATION_CODES:
        return 'response_' + validation
    code = online.get('failure_code')
    if isinstance(code, str) and code in TRANSPORT_FAILURE_CODES | GUARD_FAILURE_CODES:
        return 'retrieval_' + code
    known = {'semantic_index_partial', 'semantic_index_not_ready', 'lexical_index_partial',
             'source_index_partial', 'dependency_budget_exceeded', 'correction_scope_lookup_failed',
             'online_configuration_changed_or_disabled', 'online_deadline_exhausted',
             'online_invalid_selection', 'online_state_changed', 'online_transport_failed',
             'online_guard_or_configuration_failed'}
    for code in (online.get('degraded'), retrieval.get('degraded'), raw.get('degraded')):
        if isinstance(code, str) and code in known: return code
    return 'retrieval_degraded_unknown'


class _Handle:
    def __init__(self, home):
        self.home = Path(home).expanduser().absolute()
        path = self.home / 'state.sqlite3'
        if not path.is_file() or path.is_symlink():
            raise ValueError('observation requires an existing database')
        self.db = sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=.2, isolation_level=None)
        self.db.row_factory = sqlite3.Row

    @contextmanager
    def locked(self):
        # This handle only writes observation tables. BEGIN IMMEDIATE below
        # serializes those writes with every other SQLite writer (200ms bound).
        # The business lock can cover maintenance subprocesses/network waits;
        # taking it here discarded valid outcomes even while SQLite was free.
        yield

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield self.db
            self.db.execute('COMMIT')
        except BaseException:
            if self.db.in_transaction:
                self.db.rollback()
            raise


def _identity_host(identity):
    prefix = identity.split(':', 1)[0] if isinstance(identity, str) else ''
    return prefix if prefix in ('pi', 'codex', 'cc', 'dsh') else 'unknown'


def _source_identity(handle, project, source_id):
    if not isinstance(source_id, str) or not source_id:
        return None
    try:
        row = handle.db.execute('SELECT e.event_id FROM events e JOIN sources s ON s.id=e.source_id WHERE e.source_id=? AND s.project=? ORDER BY e.rowid DESC LIMIT 1', (source_id, project)).fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None


def _measured_metadata(value):
    """Only measured numeric stages and the retrieval's explicit cache enum.

    No elapsed differences, counts, strategy names or outcome statuses imply
    timings/cache hits. Unknown fields can contain private text and are dropped.
    """
    retrieval = value.get('retrieval')
    retrieval = retrieval if isinstance(retrieval, dict) else {}
    stages = {}
    for source in (retrieval.get('stage_timings'), value.get('stage_timings')):
        if not isinstance(source, dict):
            continue
        for name, elapsed in source.items():
            if (name in STAGES and type(elapsed) in (int, float)
                    and math.isfinite(elapsed) and elapsed >= 0):
                stages[name] = elapsed
    cache = retrieval.get('cache_state', 'unknown')
    return stages, cache if cache in ('cold', 'warm', 'bypass', 'unknown') else 'unknown'


def _graph_metadata(value):
    retrieval = value.get('retrieval')
    graph = retrieval.get('graph') if isinstance(retrieval, dict) else None
    if not isinstance(graph, dict): return {}, {}
    elapsed = graph.get('elapsed_ms')
    stages = {'graph': elapsed} if type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed >= 0 else {}
    metrics = {name: graph[name.removeprefix('graph_')] for name in METRICS
               if type(graph.get(name.removeprefix('graph_'))) is int
               and 0 <= graph[name.removeprefix('graph_')] <= 1000000}
    return stages, metrics


def observe_command(args, result=None, error=None, elapsed_ms=None):
    """Called after core completion; failure returns a safe warning, never raises."""
    command = getattr(args, 'command', None)
    if command not in ('capture', 'context', 'memory', 'ingest'):
        return {'recorded': False, 'skipped': 'unobserved_command'}
    handle = None
    try:
        handle = _Handle(args.home)
        project = getattr(args, 'project', None)
        if not isinstance(project, str) or not project:
            raise ValueError('missing project')
        value = result if isinstance(result, dict) else {}
        stable = getattr(args, 'event_id', None) or getattr(args, 'observation_event_key', None)
        if command == 'memory' and not stable:
            stable = value.get('event_key') or value.get('receipt_id')
        native = stable if isinstance(stable, str) and stable else None
        if command == 'context':
            native = _source_identity(handle, project, getattr(args, 'current_source_id', None))
            stable = value.get('memory_receipt_id') or value.get('receipt_id')
        elif command == 'ingest':
            stable = ('ingest:' + str(value['source_id']) + ':' + str(value['version'])) if value.get('source_id') and value.get('version') else None
            native = None
        if not isinstance(stable, str) or not stable:
            stable = 'explicit-observation:' + uuid.uuid4().hex
        host = _identity_host(native)
        operation = command
        reason = 'observed_outcome'
        status = 'failed' if error is not None else 'unknown'
        publication = None
        if command in ('capture', 'ingest') and error is None:
            status = 'success' if value.get('source_id') and type(value.get('version')) is int else 'unknown'
        elif command == 'context' and error is None:
            status = 'prepared' if isinstance(result, dict) and 'rendered_context' in value else 'unknown'
            if isinstance(value.get('retrieval'), dict) and value['retrieval'].get('degraded'):
                status = 'degraded'; reason = retrieval_reason(value['retrieval'])
        elif command == 'memory':
            action = getattr(args, 'action', '')
            operation = 'knowledge_review' if action == 'review' else 'knowledge_finish' if action == 'finish' else 'knowledge_write' if action in ('remember', 'update', 'amend', 'resolve') else 'knowledge_read'
            if error is None:
                outcome = value.get('outcome')
                state = value.get('status')
                if action == 'review':
                    status = 'success' if state == 'closed' or isinstance(value.get('reviews'), list) else 'incomplete'
                    reason = 'review_' + str(state or 'unknown')
                elif action == 'finish':
                    status = {'learned': 'success', 'no_change': 'no_change', 'incomplete': 'incomplete'}.get(outcome, 'unknown')
                elif operation == 'knowledge_read':
                    status = 'success' if isinstance(result, dict) else 'unknown'
                elif state in ('applied', 'amended'):
                    status = 'success'; reason = 'knowledge_saved'
                elif state in ('draft', 'needs_update', 'pending'):
                    status = 'incomplete'; reason = 'not_committed_as_knowledge'
                elif state in ('dismissed', 'no_change'):
                    status = 'no_change'
                publication = value.get('publication')
        if error is not None:
            reason = failure_reason(error)
        stages, cache_state = _measured_metadata(value)
        graph_stages, metrics = _graph_metadata(value)
        stages.update(graph_stages)
        if elapsed_ms is None:
            elapsed_ms = stages.get('total')
        observation = record_event(handle, event_id=stable + ':' + operation + ':' + status,
            project=project, host=host, operation=operation, status=status,
            elapsed_ms=elapsed_ms, stages=stages, cache_state=cache_state, metrics=metrics,
            native_event_id=native, expected=False, reason_code=reason)
        observation['identity_scope'] = 'native_outcome' if native else 'local_outcome_no_native_denominator'
        if publication in ('ok', 'superseded', 'publication_pending'):
            pub_status = 'success' if publication in ('ok', 'superseded') else 'incomplete'
            observation['publication'] = record_event(handle,
                event_id=stable + ':publication:' + publication, project=project, host=host,
                operation='publication', status=pub_status, native_event_id=native,
                reason_code=publication)
            if observation['publication'].get('warning'):
                observation.setdefault('warning', observation['publication']['warning'])
        return observation
    except Exception as exc:
        return {'recorded': False, 'warning': 'operation_observation_' + type(exc).__name__}
    finally:
        if handle is not None:
            try:
                handle.db.close()
            except sqlite3.Error:
                pass
