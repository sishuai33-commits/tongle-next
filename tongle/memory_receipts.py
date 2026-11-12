"""Optional, bounded evidence that a memory context was prepared locally.

A receipt can be bound to the exact captured user turn and later assistant
answer. These rows still prove neither provider delivery nor correct use.
Empty contexts are also recorded (zero UTF-16 units); a nonempty context may
contain sources/corrections without cards. Only the renderer's admitted cards
belong in ``rendered_cards``. No query, body, source excerpt, path or model
output is copied into this log.
"""
import hashlib
import json
import re
import time
import uuid

from .context_delivery import utf16_units


MAX_RECEIPTS = 1000  # Global bound across projects, including empty contexts.

_SCHEMA = """CREATE TABLE IF NOT EXISTS memory_context_receipts (
    id TEXT PRIMARY KEY,
    project TEXT NOT NULL,
    session TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state='prepared'),
    prepared_at REAL NOT NULL,
    rendered_cards TEXT NOT NULL,
    context_sha256 TEXT NOT NULL,
    context_utf16_units INTEGER NOT NULL,
    elapsed_ms REAL NOT NULL,
    user_source_id TEXT,
    user_source_version INTEGER,
    answer_source_id TEXT,
    answer_source_version INTEGER,
    answer_bound_at REAL,
    host TEXT NOT NULL DEFAULT 'unknown'
)"""

# The original opt-in table had no turn/answer binding. Upgrade only when the
# feature is used; disabled tracing must not create or migrate anything.
_BINDING_COLUMNS = {
    'user_source_id': 'TEXT',
    'user_source_version': 'INTEGER',
    'answer_source_id': 'TEXT',
    'answer_source_version': 'INTEGER',
    'answer_bound_at': 'REAL',
    'host': "TEXT NOT NULL DEFAULT 'unknown'",
    'version_fingerprints': 'TEXT',
    'stage_timings': 'TEXT',
}


def _ensure_schema(engine):
    engine.db.execute(_SCHEMA)
    existing = {r[1] for r in engine.db.execute(
        'PRAGMA table_info(memory_context_receipts)')}
    for name, kind in _BINDING_COLUMNS.items():
        if name not in existing:
            engine.db.execute(
                'ALTER TABLE memory_context_receipts ADD COLUMN '
                + name + ' ' + kind)


def _table_exists(engine):
    return bool(engine.db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='memory_context_receipts'").fetchone())


def _source_host(engine, source_id):
    row = engine.db.execute(
        'SELECT event_id FROM events WHERE source_id=? '
        'ORDER BY rowid DESC LIMIT 1', (source_id,)).fetchone()
    prefix = row[0].split(':', 1)[0] if row and isinstance(row[0], str) else ''
    return prefix if prefix in ('pi', 'cc', 'codex', 'dsh') else 'unknown'


def record_prepared_context(engine, project, session, rendered, delivered,
                            elapsed_ms, user_source_id=None, stage_timings=None,
                            version_info=None):
    """Return (durably committed id, warning), without interrupting retrieval.

    Called only after memory_trace_enabled is explicitly true. When supplied,
    ``user_source_id`` must be the captured user turn for this exact project
    and session; otherwise the receipt remains prepared but turn-unbound.
    The separate table never participates in knowledge_consumption or
    learning. Retention deletes only this disposable log, newest insertions
    first even if the clock moves backwards. An outer transaction cannot
    provide durable proof yet, so it returns a warning instead of committing
    another caller's work.
    """
    try:
        from .observability import _timing, _versions, STAGES
        elapsed_ms = _timing(elapsed_ms)
        if elapsed_ms is None:
            raise ValueError('receipt total timing required')
        if stage_timings is not None and (not isinstance(stage_timings, dict)
                or set(stage_timings) - STAGES):
            raise ValueError('invalid receipt timing stages')
        timings = {key: _timing(value) for key, value in (stage_timings or {}).items()}
        fingerprints = _versions(engine, version_info)
        cards = [{'id': card['id'], 'version': card['version'],
                  'delivery': card['delivery']} for card in delivered]
        receipt_id = uuid.uuid4().hex
        with engine.locked():
            if engine.db.in_transaction:
                return None, 'memory_trace_not_recorded: active_transaction'
            user_version = None
            if user_source_id is not None:
                source = engine.store.source(user_source_id)
                if (source['project'] != project or source['session'] != session
                        or source['role'] != 'user'
                        or not engine.source_available(user_source_id)):
                    raise ValueError('receipt user turn scope mismatch')
                user_version = source['current_version']
            host = _source_host(engine, user_source_id) if user_source_id else 'unknown'
            values = (receipt_id, project, session, 'prepared', time.time(),
                      json.dumps(cards, ensure_ascii=False, separators=(',', ':')),
                      hashlib.sha256(rendered.encode('utf-8')).hexdigest(),
                      utf16_units(rendered), elapsed_ms,
                      user_source_id, user_version, None, None, None, host,
                      json.dumps(fingerprints, sort_keys=True),
                      json.dumps(timings, sort_keys=True))
            try:
                with engine.transaction():
                    _ensure_schema(engine)
                    engine.db.execute(
                        'INSERT INTO memory_context_receipts '
                        '(id,project,session,state,prepared_at,rendered_cards,'
                        'context_sha256,context_utf16_units,elapsed_ms,'
                        'user_source_id,user_source_version,answer_source_id,'
                        'answer_source_version,answer_bound_at,host,version_fingerprints,stage_timings) '
                        'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', values)
                    engine.db.execute(
                        'DELETE FROM memory_context_receipts WHERE rowid NOT IN '
                        '(SELECT rowid FROM memory_context_receipts '
                        'ORDER BY rowid DESC LIMIT ?)', (MAX_RECEIPTS,))
            except Exception:
                # A failed COMMIT can leave our transaction open. Never let
                # optional tracing change a subsequent caller's transaction.
                if engine.db.in_transaction:
                    engine.db.rollback()
                raise
        return receipt_id, None
    except Exception as exc:
        # Exception messages may contain source/query text or private paths.
        return None, 'memory_trace_not_recorded: ' + type(exc).__name__


def bind_user(engine, project, session, receipt_id, user_source_id, user_source_version):
    """Associate a deferred native capture with its uniquely matched preparation.

    The adapter must prove the observed prompt/native-message/receipt match;
    this boundary enforces durable source scope, version and no reassignment.
    A caller reports capture and association separately if association fails.
    """
    if (not isinstance(receipt_id, str) or not receipt_id
            or not isinstance(user_source_id, str) or not user_source_id
            or type(user_source_version) is not int or user_source_version < 1):
        raise ValueError('invalid memory receipt user binding')
    with engine.locked(), engine.transaction():
        user = engine.store.source(user_source_id)
        if (user['project'] != project or user['session'] != session or user['role'] != 'user'
                or user['current_version'] != user_source_version
                or not engine.source_available(user_source_id)):
            raise ValueError('memory receipt user scope/version mismatch')
        if not _table_exists(engine):
            raise ValueError('memory receipt unavailable')
        _ensure_schema(engine)
        row = engine.db.execute('SELECT * FROM memory_context_receipts WHERE id=?', (receipt_id,)).fetchone()
        if not row or row['project'] != project or row['session'] != session or row['state'] != 'prepared':
            raise ValueError('memory receipt scope/state mismatch')
        if row['user_source_id']:
            if row['user_source_id'] != user_source_id or row['user_source_version'] != user_source_version:
                raise ValueError('memory receipt user already bound')
            return {'receipt_id': receipt_id, 'state': 'user_captured', 'replayed': True}
        if row['answer_source_id']:
            raise ValueError('memory receipt answer already bound')
        engine.db.execute('UPDATE memory_context_receipts SET user_source_id=?, user_source_version=?, host=? WHERE id=?',
            (user_source_id, user_source_version, _source_host(engine, user_source_id), receipt_id))
    return {'receipt_id': receipt_id, 'state': 'user_captured', 'replayed': False}


def _native_reply_relation(engine, project, session, user_source_id, answer_source_id, reply_to_event_id):
    """Validate an adapter-declared relation, not independently audited delivery.

    The adapter verifies rollout order. Core verifies the exact native user
    identity and same native turn/roles before replacing insertion chronology.
    Legacy turn-only user IDs cannot supply this message-level evidence.
    """
    if reply_to_event_id is None:
        return False
    parts = reply_to_event_id.split(':') if isinstance(reply_to_event_id, str) else []
    if (len(parts) != 5 or parts[0] != 'codex' or parts[1] != session or parts[-1] != 'user'
            or not all(re.fullmatch(r'[A-Za-z0-9_-]{1,200}', p) for p in parts[1:4])):
        raise ValueError('invalid native reply user identity')
    user_event = engine.db.execute('SELECT source_id FROM events WHERE project=? AND event_id=?',
                                  (project, reply_to_event_id)).fetchone()
    if not user_event or user_event['source_id'] != user_source_id:
        raise ValueError('native reply does not identify receipt user')
    answer_events = engine.db.execute('SELECT event_id FROM events WHERE project=? AND source_id=?',
                                     (project, answer_source_id)).fetchall()
    def matches(value):
        fields = value.split(':')
        return (len(fields) in (4, 5) and fields[:3] == parts[:3] and fields[-1] == 'assistant'
                and all(re.fullmatch(r'[A-Za-z0-9_-]{1,200}', p) for p in fields[1:-1]))
    if not any(matches(r['event_id']) for r in answer_events):
        raise ValueError('native reply answer turn/role mismatch')
    return True


def bind_answer(engine, project, session, receipt_id, answer_source_id,
                answer_source_version, reply_to_event_id=None):
    """Bind a captured assistant answer to one prepared, same-turn receipt.

    This is an association, not delivery, adoption, correctness or benefit.
    Replaying the same capture is idempotent; cross-turn, cross-project,
    cross-session and one-answer-to-many-receipt bindings fail closed.
    """
    if (not isinstance(receipt_id, str) or not receipt_id
            or not isinstance(answer_source_id, str) or not answer_source_id
            or type(answer_source_version) is not int
            or answer_source_version < 1):
        raise ValueError('invalid memory receipt answer binding')
    answer = engine.store.source(answer_source_id, answer_source_version)
    if (answer['project'] != project or answer['session'] != session
            or answer['role'] != 'assistant'):
        raise ValueError('receipt answer scope mismatch')
    with engine.locked(), engine.transaction():
        if not _table_exists(engine):
            raise ValueError('memory receipt unavailable')
        _ensure_schema(engine)
        row = engine.db.execute(
            'SELECT * FROM memory_context_receipts WHERE id=?',
            (receipt_id,)).fetchone()
        if (not row or row['project'] != project
                or row['session'] != session):
            raise ValueError('memory receipt scope mismatch')
        if not row['user_source_id']:
            raise ValueError('memory receipt has no bound user turn')
        user = engine.store.source(
            row['user_source_id'], row['user_source_version'])
        if (user['project'] != project or user['session'] != session
                or user['role'] != 'user'):
            raise ValueError('memory receipt user turn mismatch')
        native_reply = _native_reply_relation(engine, project, session, row['user_source_id'],
                                              answer_source_id, reply_to_event_id)
        if row['answer_source_id'] == answer_source_id:
            if row['answer_source_version'] != answer_source_version:
                raise ValueError('memory receipt answer version mismatch')
            return {'receipt_id': receipt_id, 'state': 'answer_captured',
                    'host_delivery': 'unknown', 'benefit': 'unknown',
                    'replayed': True}
        if row['answer_source_id']:
            raise ValueError('memory receipt already bound')
        if not engine.source_available(answer_source_id):
            raise ValueError('receipt answer unavailable for first binding')
        user_position = engine.db.execute(
            'SELECT rowid FROM sources WHERE id=?',
            (row['user_source_id'],)).fetchone()[0]
        answer_position = engine.db.execute(
            'SELECT rowid FROM sources WHERE id=?',
            (answer_source_id,)).fetchone()[0]
        if not native_reply and answer_position <= user_position:
            raise ValueError('memory receipt answer precedes user turn')
        later_user = engine.db.execute(
            "SELECT 1 FROM sources WHERE project=? AND session=? "
            "AND kind='event' AND role='user' AND rowid>? AND rowid<? LIMIT 1",
            (project, session, user_position, answer_position)).fetchone()
        if not native_reply and later_user:
            raise ValueError('memory receipt answer belongs to a later user turn')
        other = engine.db.execute(
            'SELECT id FROM memory_context_receipts '
            'WHERE answer_source_id=? AND answer_source_version=? AND id<>?',
            (answer_source_id, answer_source_version, receipt_id)).fetchone()
        if other:
            raise ValueError('answer already bound to another receipt')
        engine.db.execute(
            'UPDATE memory_context_receipts SET answer_source_id=?, '
            'answer_source_version=?, answer_bound_at=? WHERE id=?',
            (answer_source_id, answer_source_version, time.time(), receipt_id))
    return {'receipt_id': receipt_id, 'state': 'answer_captured',
            'host_delivery': 'unknown', 'benefit': 'unknown',
            'replayed': False}
