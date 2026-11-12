"""Isolated v2 (delta pipeline) queue/process routing for Tongle.

This module owns ONLY the routing seam between the existing v5 knowledge
pipeline and the isolated v2 delta pipeline:

* ``resolve_protocol`` reads the operator's explicit
  ``knowledge_pipeline_protocol`` choice (default v5) and refuses an unknown
  value *before* any queue mutation. An existing v5 row is therefore never
  silently re-pinned.
* ``queue_events`` queues one complete, AVAILABLE event source per v2 work row
  (row + ALL ``knowledge_seen`` claims in one transaction, no truncation)
  when -- and only when -- v2 is opted in. It reports the eligible source ids
  so the legacy grouping can skip them even when the quiet period or the
  one-event-per-session rule leaves them unqueued this cycle.
* ``process_one`` runs one leased v2 work row through the real
  ``delta_execution.run_job`` and maps its existing failure families onto the
  existing work row, without ever mutating the caller's bundle, call ledger or
  audit receipts.

No new DB table, queue, service or model call is created here.
"""
from __future__ import annotations

import time

from .evolution import (BudgetWait, CoverageWait, SelectionChanged, StageLimit,
                        _TRANSIENT_CATEGORIES, _int_or, canonical, fingerprint)
from .model_errors import ModelError
from .triage import CAPACITY, CONTENT, classify

# The existing v5 contract label and the isolated v2 pipeline label. These are
# the only two protocols any queue/lease code may act on.
LEGACY_PROTOCOL = 'tongle-knowledge-contract-v5'
V2_PROTOCOL = 'tongle-delta-pipeline-v1'
VALID_PROTOCOLS = (LEGACY_PROTOCOL, V2_PROTOCOL)

# One complete event source, bounded by the v2 full-source window limit.
MAX_EVENT_CHARS = 32000

# Host-generated maintenance sessions stay explicit legacy v5.
_SYNTHETIC_SESSIONS = ('framework-maintenance', 'knowledge-dependency',
                       'knowledge-feedback')


def resolve_protocol(cfg):
    """The explicit pipeline protocol, defaulting to the existing v5 contract.

    Only v5 and the v2 delta pipeline are accepted. Any other value is an
    operator error and raises rather than silently defaulting: an invalid
    config must never re-pin old jobs or drop new ones.
    """
    value = cfg.get('knowledge_pipeline_protocol', LEGACY_PROTOCOL)
    if value not in VALID_PROTOCOLS:
        raise ValueError('unknown knowledge_pipeline_protocol: %r' % (value,))
    return value


def _next_utc_midnight():
    return (int(time.time()) // 86400 + 1) * 86400


def _safe_error(exc):
    """Compact, bounded, source-free failure label."""
    if isinstance(exc, ModelError):
        return exc.summary()[:220]
    text = ' '.join(str(exc).split())
    label = type(exc).__name__
    if not text:
        return label
    return (label + ': ' + text)[:220]


def _eligible_source(evo, row):
    """Whether one source qualifies as a v2 owned event, or None.

    Eligibility is deliberately strict: exactly one AVAILABLE ``event`` source
    with a user/assistant role, 1..32000 characters, every ``_source_parts``
    piece nonempty, and NO part already owned by ``knowledge_seen``. A
    partially-owned source, an oversize source, a non-event source or a
    host-generated maintenance session is left to the explicit legacy v5 path.
    """
    if row['kind'] != 'event':
        return None
    if not evo.engine.source_available(row['id']):
        return None
    try:
        source = evo.engine.store.source(row['id'])
    except (KeyError, ValueError, OSError):
        return None
    if source.get('role') not in ('user', 'assistant'):
        return None
    session = source.get('session')
    if session in _SYNTHETIC_SESSIONS:
        return None
    text = source.get('text')
    if not isinstance(text, str) or not (1 <= len(text) <= MAX_EVENT_CHARS):
        return None
    parts = list(evo._source_parts(source))
    if not parts:
        return None
    for part in parts:
        if not isinstance(part.get('text'), str) or not part['text']:
            return None
    for part in parts:
        owned = evo.db.execute(
            'SELECT 1 FROM knowledge_seen WHERE source_id=? AND version=? AND part=?',
            (source['source_id'], source['current_version'], part['part'])).fetchone()
        if owned is not None:
            return None
    return source, parts


def queue_events(evo, cfg, force, now):
    """Queue isolated v2 work rows and report the sources legacy must skip.

    Returns ``{'queued': n, 'skipped': [...], 'eligible': [...]}``. ``skipped``
    are eligible sources not queued this cycle (quiet period / one-per-session);
    ``eligible`` is EVERY eligible v2 source, so the legacy grouping skips them
    all and never steals them into a v5 row.
    """
    protocol = resolve_protocol(cfg)
    if protocol != V2_PROTOCOL:
        return {'queued': 0, 'skipped': [], 'eligible': []}

    quiet = max(0, float(cfg.get('knowledge_quiet_seconds', 300)))
    revision = cfg.get('knowledge_revision', 1)

    eligible = []
    for row in evo.db.execute(
            'SELECT id,project,session,kind,created_at FROM sources'
            ' ORDER BY created_at,id').fetchall():
        from .memory_runtime import source_allowed
        if not source_allowed(evo.engine,row['id']): continue
        resolved = _eligible_source(evo, row)
        if resolved is None:
            continue
        source, parts = resolved
        eligible.append((row, source, parts))

    eligible_ids = sorted({row['id'] for row, _source, _parts in eligible})
    # Assistant reports are evidence/report material, not user decisions. They
    # are curated only for explicitly opted-in projects; elsewhere they stay
    # raw and searchable. This prevents long AI reports from becoming a new
    # oversized card every session, which was the dominant content-failure
    # source in 2026-10.
    assistant_projects = cfg.get('knowledge_assistant_curation_projects')
    if assistant_projects is None:
        # Backward compatible: no policy configured means curate both roles.
        assistant_all, assistant_projects = True, set()
    elif isinstance(assistant_projects, list):
        assistant_all = '*' in assistant_projects
        assistant_projects = {p for p in assistant_projects if isinstance(p, str)}
    else:
        # A malformed policy must not silently disable assistant curation.
        assistant_all, assistant_projects = True, set()

    # One earliest new event per project/session per cycle. The rest remain
    # eligible (and are rediscovered next cycle); they are never marked seen.
    skipped_ids = []
    chosen = {}
    for row, source, parts in eligible:
        if (source.get('role') == 'assistant' and not assistant_all
                and row['project'] not in assistant_projects):
            # Keep it in ``eligible``/``skipped`` so the legacy grouping cannot
            # silently steal it into a v5 batch instead.
            skipped_ids.append(row['id'])
            continue
        key = (row['project'], row['session'])
        current = chosen.get(key)
        if (current is None
                or (row['created_at'], row['id'])
                < (current[0]['created_at'], current[0]['id'])):
            chosen[key] = (row, source, parts)

    queued_ids = []
    for key in sorted(chosen, key=lambda k: (str(k[0]), str(k[1]))):
        row, source, parts = chosen[key]
        session = row['session']
        # Same quiet-period logic as the existing session groups, but the real
        # session is kept (never replaced by the legacy 'documents' label).
        if session != 'documents' and not force and len(parts) < 8:
            last = evo.db.execute(
                'SELECT MAX(created_at) FROM sources WHERE project=? AND session=?',
                (row['project'], session)).fetchone()[0]
            if last:
                from datetime import datetime
                try:
                    age = now - datetime.fromisoformat(last).timestamp()
                except (ValueError, TypeError):
                    age = quiet
                if age < quiet:
                    skipped_ids.append(row['id'])
                    continue
        work_id = fingerprint([protocol, row['project'], session, parts,
                               revision])[:32]
        evo.db.execute(
            "INSERT OR IGNORE INTO knowledge_work"
            "(id,project,session,inputs,state,created_at,pipeline_protocol)"
            " VALUES(?,?,?,?,'pending',?,?)",
            (work_id, row['project'], session, canonical(parts), now, protocol))
        for part in parts:
            evo.db.execute(
                'INSERT OR IGNORE INTO knowledge_seen VALUES(?,?,?,?)',
                (part['source_id'], part['version'], part['part'], work_id))
        queued_ids.append(row['id'])

    return {'queued': len(queued_ids),
            'skipped': sorted(set(eligible_ids) - set(queued_ids)),
            'eligible': eligible_ids}


def _handle_error(evo, job, token, cfg, exc):
    """Map one v2 failure onto the existing work row WITHOUT touching receipts.

    The bundle, proposal, review, call ledger and audit rows are all left
    exactly as the execution layer wrote them. Only the work state/error is
    changed, and only while this worker still owns the live running lease.

    A ``failed`` row deliberately KEEPS its frozen bundle: the operator
    completion path re-derives the original frozen window reference parts from
    it (``delta_operator._frozen_window_reference_parts``). A *resumable*
    outcome (``retry``/``budget_wait``/``publication_pending``) keeps the cache
    for the same reason. Dead payloads on an obsoleted row are therefore not
    erased here -- that is deliberately deferred to the bounded reclamation
    sweep (``tongle/reclaim.py``), which leaves this audited write with every
    column byte-identical.
    """
    work_id = job['id']
    error = _safe_error(exc)
    max_attempts = max(1, min(5, _int_or(cfg.get('knowledge_max_attempts'), 3)))

    budget_wait = False
    if isinstance(exc, BudgetWait):
        state, next_at, refund, audit = 'budget_wait', _next_utc_midnight(), True, False
        budget_wait = True
    elif isinstance(exc, CoverageWait):
        # v2 has no automatic coverage-rebuild loop. Preserve the frozen
        # evidence and expose an explicit attention item, not a promise that
        # changing context will automatically resume an incompatible cache.
        state, next_at, refund, audit = 'failed', 0, False, True
        error = 'needs review: ' + error
    elif isinstance(exc, SelectionChanged):
        state, next_at, refund, audit = 'failed', 0, False, True
    elif isinstance(exc, StageLimit):
        state, next_at, refund, audit = 'failed', 0, False, True
    elif isinstance(exc, (TypeError, KeyError)):
        # Unknown programming defect: fail, never re-prompt the model.
        state, next_at, refund, audit = 'failed', 0, False, True
    elif isinstance(exc, (OSError, ModelError, RuntimeError)):
        # Transient upstream failure: bounded retry inside the existing attempt
        # ceiling (the 2-call per-stage cap still applies on the next attempt).
        if job.get('attempts', 0) < max_attempts - 1:
            state, next_at, refund, audit = 'retry', time.time() + 60, False, True
        else:
            state, next_at, refund, audit = 'failed', 0, False, True
    else:
        # A semantic ValueError (or anything else) is terminal: no unbounded
        # or silent rebuild.
        state, next_at, refund, audit = 'failed', 0, False, True

    with evo.engine.locked(), evo.engine.transaction():
        row = evo.db.execute(
            'SELECT pipeline_protocol,state,lease_token,lease_until'
            ' FROM knowledge_work WHERE id=?', (work_id,)).fetchone()
        # Ownership is protocol + token + unexpired lease. A stale/paused/lost
        # worker writes nothing. A committed batch has already moved the row to
        # publication_pending but keeps its lease token, so it is allowed below.
        owns = bool(row and row['pipeline_protocol'] == V2_PROTOCOL
                    and row['lease_token'] == token
                    and (row['lease_until'] or 0) > time.time())
        if not owns:
            # A stale/paused/lost worker writes nothing at all.
            return {'stale': 1}
        if row['state'] not in ('running', 'publication_pending'):
            # A pause/terminal transition takes precedence even if an old
            # lease token and an already-committed batch still exist.
            return {'stale': 1}
        committed = evo.db.execute(
            'SELECT 1 FROM knowledge_batches WHERE batch_id=?',
            (work_id,)).fetchone() is not None
        if committed:
            # A committed batch is ALWAYS publication_pending: it is never
            # re-run through the model nor finalized through the v5 path.
            evo.db.execute(
                "UPDATE knowledge_work SET state='publication_pending',error=?"
                " WHERE id=? AND lease_token=?",
                (error, work_id, token))
            return {'publication_pending': True}
        if row['state'] != 'running':
            return {'stale': 1}
        if refund:
            evo.db.execute(
                "UPDATE knowledge_work SET state=?,error=?,next_at=?,"
                " attempts=MAX(0,attempts-1) WHERE id=? AND lease_token=?"
                " AND state='running'",
                (state, error, next_at, work_id, token))
        else:
            evo.db.execute(
                "UPDATE knowledge_work SET state=?,error=?,next_at=?"
                " WHERE id=? AND lease_token=? AND state='running'",
                (state, error, next_at, work_id, token))
        if state == 'failed':
            evo.db.execute("UPDATE knowledge_work SET lease_token=NULL,lease_until=0 "
                           "WHERE id=? AND lease_token=? AND state='failed' "
                           "AND NOT EXISTS (SELECT 1 FROM knowledge_calls WHERE work_id=? AND elapsed IS NULL)",
                           (work_id, token, work_id))
            # Same rule as the v5 terminal path: a content/capacity rejection is
            # an explicit parked decision, not a permanent per-session alarm.
            # Unknown/programming errors stay visible.
            category = classify(error)
            if category in (CONTENT, CAPACITY):
                evo.db.execute(
                    'INSERT OR REPLACE INTO knowledge_triage'
                    '(work_id,class,action,note,actor,created_at) VALUES(?,?,?,?,?,?)',
                    (work_id, category, 'parked',
                     'terminal content/capacity failure; not retried blindly',
                     'auto-park', time.time()))
        if audit:
            evo.db.execute(
                'INSERT INTO knowledge_failures(work_id,state,error,created_at)'
                ' VALUES(?,?,?,?)',
                (work_id, state, error, time.time()))

    result = {}
    if budget_wait:
        result['budget_wait'] = True
    elif state == 'coverage_wait':
        result['coverage_wait'] = True
    else:
        result['failed'] = 1
        if state == 'retry':
            result['retry'] = True
    category = getattr(exc, 'category', None)
    if category in _TRANSIENT_CATEGORIES:
        trip = evo.breaker_trip(category)
        result['breaker'] = trip
        if trip['open']:
            result['breaker_open'] = True
            result['breaker_until'] = trip['until']
    return result


def process_one(evo, job, token, cfg, files):
    """Execute one leased v2 work row; return bounded result counters."""
    from . import delta_context, delta_execution
    try:
        outcome = delta_execution.run_job(
            evo, job, token, cfg, files,
            context_factory=lambda: delta_context.build_live_context(evo, job))
    except Exception as exc:
        return _handle_error(evo, job, token, cfg, exc)

    # A successful v2 batch proves the upstream channel is usable again.
    evo.breaker_clear()
    result = {}
    if outcome.get('processed'):
        result['processed'] = outcome['processed']
    if outcome.get('records'):
        result['records'] = outcome['records']
    if outcome.get('no_op_done'):
        result['no_op_done'] = outcome['no_op_done']
    if outcome.get('replayed'):
        result['replayed'] = True
    if outcome.get('publication_attention'):
        result['publication_attention'] = True
    return result
