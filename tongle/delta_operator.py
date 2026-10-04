"""Finite, explicit OPERATOR exception completion for the isolated v2 pipeline.

A v2 delta row that FAILED (a real semantic/structural/stage failure, a lost
lease, an unresolved material need) has no automatic retry left and no model
may lease it again. This module lets an authorised local OPERATOR (ROOT)
*explicitly* finish exactly one such row:

* :func:`prepare` is a pure deterministic reader. It validates the operator
  request against the authoritative stored bytes and returns a canonical plan
  (NOT body-free: it carries the request and normalized operations) whose
  ``plan_sha`` binds the full request hash, the work snapshot (state/attempts/
  protocol/cache digests), the single fully owned real event source (version +
  every authoritative part text/text-sha + seen owner), every frozen new
  reference source identity/version/text-sha/role/session/time, the
  covered/referenced/target card version + canonical card digest AND current
  full digest, the project scope fingerprint and the normalized operations
  hash. It performs no writes, no model calls, no queue reset. Only the durable
  operator meta intent and the intent/completion audits are body-free.
* :func:`apply` is the one durable transition. Under the existing engine lock
  it resyncs manual files, recomputes the plan (exact canonical equality),
  re-checks the scope guards (failed, no lease, no committed batch), calls the
  ORIGINAL ``KnowledgeStore.apply`` under ``batch_id=work_id`` with the true
  normalized operations/proposal_hash and the operator attestation, writes
  body-free intent metadata and a body-free operator intent audit, and moves the
  row to ``operator_publication_pending`` (no lease) keeping its cache. The lock
  stays held through the real publication and verification; only AFTER a
  confirmed publication does one transaction finalize ``done`` + the unique
  operator completion receipt and clear the bulky columns. A publication
  failure leaves the row ``operator_publication_pending`` with its intent and
  cache for :func:`resume_pending` -- no model call, no second apply.
* :func:`resume_pending` resumes only rows with a matching, unique, real
  operator intent, up to a bounded count.
* :func:`verify_operator_completed` (called by
  ``delta_receipts.verify_completed`` only when a unique operator completion
  origin exists) re-proves the whole chain read-only.

The SHA-256 fields are structural audit bindings, NOT cryptographic identity or
proof by themselves; ROOT is responsible for the SEMANTICS and this module never
labels anything "model-reviewed". The API is local operator-only: it is never
part of a model payload, an automatic worker or a prompt.

No new table, queue, daemon or second storage engine is created here.
"""
from __future__ import annotations

import hashlib
import json
import time

from . import delta_routing

__all__ = [
    'prepare', 'apply', 'resume_pending', 'verify_operator_completed',
    'publish_one', 'validate_operator_intent', 'PROTOCOL', 'OPERATOR_STATE',
    'OPERATOR_INTENT_AUDIT', 'OPERATOR_COMPLETE_AUDIT', 'OPERATOR_META_PREFIX',
    'OPERATOR_REVIEW_IDENTITY', 'OPERATOR_ORIGIN', 'MAX_OPERATIONS',
    'MAX_COVERED', 'MAX_RATIONALE',
]

PROTOCOL = delta_routing.V2_PROTOCOL
OPERATOR_STATE = 'operator_publication_pending'
OPERATOR_INTENT_AUDIT = 'delta_operator_intent_v1'
OPERATOR_COMPLETE_AUDIT = 'delta_operator_complete_v1'
OPERATOR_META_PREFIX = 'delta_operator_v1:'
OPERATOR_REVIEW_IDENTITY = 'ROOT_local_review'
OPERATOR_ORIGIN = 'ROOT_operator'

SCHEMA = 1
MAX_OPERATIONS = 8
MAX_COVERED = 12
MAX_RATIONALE = 2000
MAX_HUMAN_CHECKS = 12
_SLUG_MAX = 128
_HEX_CHARS = frozenset('0123456789abcdef')

_DECISIONS = ('material', 'covered', 'nonmaterial')
_OWNED_ROLES = ('user', 'assistant')

# The exact request keys (unknown keys are rejected).
_REQUEST_KEYS = ('grant_id', 'work_id', 'code_sha', 'acceptance_sha',
                 'review_sha', 'rationale', 'decision', 'covered_ids',
                 'operations', 'human_checks')


# --------------------------------------------------------------- digests

def _canon(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def _hash(obj):
    return hashlib.sha256(_canon(obj).encode('utf-8')).hexdigest()


def _sha_text(text):
    return hashlib.sha256((text or '').encode('utf-8')).hexdigest()


def _is_hex(value):
    return (isinstance(value, str) and len(value) == 64
            and all(c in _HEX_CHARS for c in value))


def _is_slug(value):
    if not isinstance(value, str) or not value or len(value) > _SLUG_MAX:
        return False
    return all(c not in ':\x00' and not c.isspace() for c in value)


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _reject_extra(obj, allowed, label):
    extra = sorted(set(obj) - set(allowed))
    if extra:
        raise ValueError('%s has unknown keys: %s' % (label, extra))


# ------------------------------------------------------ request validation

def _validate_request(request):
    if not isinstance(request, dict):
        raise ValueError('request must be an object')
    _reject_extra(request, _REQUEST_KEYS, 'request')

    grant_id = request.get('grant_id')
    if not _is_slug(grant_id):
        raise ValueError('grant_id must be a nonempty bounded slug')
    work_id = request.get('work_id')
    if not isinstance(work_id, str) or not work_id:
        raise ValueError('work_id must be a nonempty string')
    for name in ('code_sha', 'acceptance_sha', 'review_sha'):
        if not _is_hex(request.get(name)):
            raise ValueError('%s must be a hex64 digest' % name)
    rationale = request.get('rationale')
    if not isinstance(rationale, str) or not (1 <= len(rationale) <= MAX_RATIONALE):
        raise ValueError('rationale must be a nonempty string within %d chars'
                         % MAX_RATIONALE)

    decision = request.get('decision')
    if decision not in _DECISIONS:
        raise ValueError('decision must be material|covered|nonmaterial')

    covered_ids = request.get('covered_ids')
    if not isinstance(covered_ids, list) or len(covered_ids) > MAX_COVERED:
        raise ValueError('covered_ids must be a bounded list')
    seen = set()
    norm_covered = []
    for cid in covered_ids:
        if not isinstance(cid, str) or not cid:
            raise ValueError('covered_ids entries must be nonempty strings')
        if cid in seen:
            raise ValueError('covered_ids entries must be unique')
        seen.add(cid)
        norm_covered.append(cid)

    operations = request.get('operations')
    if not isinstance(operations, list) or len(operations) > MAX_OPERATIONS:
        raise ValueError('operations must be a bounded list of at most %d'
                         % MAX_OPERATIONS)

    human_checks = request.get('human_checks')
    if human_checks is None:
        human_checks = []
    if not isinstance(human_checks, list) or len(human_checks) > MAX_HUMAN_CHECKS:
        raise ValueError('human_checks must be a bounded list')
    for check in human_checks:
        if not isinstance(check, dict):
            raise ValueError('human_checks entries must be objects')

    if decision == 'material':
        if not operations:
            raise ValueError('material requires a nonempty operation list')
        if norm_covered:
            raise ValueError('material must not carry covered_ids')
    elif decision == 'covered':
        if operations:
            raise ValueError('covered must not carry operations')
        if not norm_covered:
            raise ValueError('covered requires a nonempty covered_ids list')
    else:  # nonmaterial
        if operations:
            raise ValueError('nonmaterial must not carry operations')
        if norm_covered:
            raise ValueError('nonmaterial must not carry covered_ids')

    return {
        'grant_id': grant_id,
        'work_id': work_id,
        'code_sha': request['code_sha'],
        'acceptance_sha': request['acceptance_sha'],
        'review_sha': request['review_sha'],
        'rationale': rationale,
        'decision': decision,
        'covered_ids': norm_covered,
        'operations': [dict(op) for op in operations],
        'human_checks': [dict(check) for check in human_checks],
    }


# ------------------------------------------------------ scope / work guards

def _work_row(evo, work_id):
    return evo.db.execute('SELECT * FROM knowledge_work WHERE id=?',
                          (work_id,)).fetchone()


def _has_batch(evo, work_id):
    return evo.db.execute('SELECT 1 FROM knowledge_batches WHERE batch_id=?',
                          (work_id,)).fetchone() is not None


def _active_lease(row, now=None):
    now = time.time() if now is None else now
    return bool((row['lease_until'] or 0) > now and row['lease_token'])


def _load_inputs(raw):
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            value = json.loads(raw or '[]')
        except ValueError:
            raise ValueError('stored inputs malformed')
        return value if isinstance(value, list) else []
    return []


def _validate_scope(evo, row):
    """Existing ``failed`` v2 work only: no lease, no committed batch."""
    if row is None:
        raise ValueError('work missing')
    if row['pipeline_protocol'] != PROTOCOL:
        raise ValueError('work is not the v2 pipeline protocol')
    if row['state'] != 'failed':
        raise ValueError('operator completion requires a failed work row')
    if _active_lease(row):
        raise ValueError('work has a live lease')
    if _has_batch(evo, row['id']):
        raise ValueError('work already has a committed batch')


def _owned_event(evo, row):
    """Resolve exactly ONE fully owned real event source for the failed row.

    The row's stored ``inputs`` must name exactly one source; that source must
    be available, at its current version, in the row's project, a real user or
    assistant ``event``; EVERY authoritative ``_source_parts`` part must be
    present and match the stored input text/identity by SHA; and every
    ``knowledge_seen`` claim for that (source, version) must be owned by this
    work id. Anything else (multiple sources, a partial, a foreign owner, a
    superseded/revoked source, a synthetic session) is rejected.
    """
    parts = _load_inputs(row['inputs'])
    if not parts:
        raise ValueError('work inputs empty')

    distinct = set()
    for part in parts:
        if not isinstance(part, dict):
            raise ValueError('stored input part malformed')
        sid = part.get('source_id')
        ver = part.get('version')
        if not isinstance(sid, str) or not sid:
            raise ValueError('stored input source id malformed')
        if not _is_int(ver) or ver < 1:
            raise ValueError('stored input version malformed')
        distinct.add((sid, ver))
    if len(distinct) != 1:
        raise ValueError('operator completion requires exactly one owned source')
    sid, ver = next(iter(distinct))

    if not evo.engine.source_available(sid):
        raise ValueError('owned source unavailable')
    try:
        source = evo.engine.store.source(sid)
    except (KeyError, ValueError, OSError):
        raise ValueError('owned source missing')
    if source.get('version') != ver or source.get('current_version') != ver:
        raise ValueError('owned source version is not current')
    if source.get('project') != row['project']:
        raise ValueError('owned source project mismatch')
    if source.get('session') != row['session']:
        raise ValueError('owned source session mismatch')
    if source.get('kind') != 'event':
        raise ValueError('owned source kind must be event')
    if source.get('role') not in _OWNED_ROLES:
        raise ValueError('owned source role unsupported')
    text = source.get('text')
    if not isinstance(text, str) or not text:
        raise ValueError('owned source text missing')

    authoritative = list(evo._source_parts(source))
    if not authoritative:
        raise ValueError('owned source has no parts')
    stored_by_part = {}
    for part in parts:
        pnum = part.get('part')
        if not _is_int(pnum) or pnum < 0:
            raise ValueError('stored input part number malformed')
        if pnum in stored_by_part:
            raise ValueError('duplicate stored input part')
        stored_by_part[pnum] = part
    if set(stored_by_part) != {p['part'] for p in authoritative}:
        raise ValueError('stored inputs do not cover every authoritative part')
    for real in authoritative:
        stored = stored_by_part[real['part']]
        if stored.get('text') != real.get('text'):
            raise ValueError('stored input part text does not match source')
        for field in ('role', 'kind'):
            if stored.get(field) != real.get(field):
                raise ValueError('stored input part %s mismatch' % field)
        for field in ('start', 'end', 'source_length'):
            if not _is_int(stored.get(field)) or stored.get(field) != real.get(field):
                raise ValueError('stored input part %s mismatch' % field)
        if not isinstance(stored.get('partial'), bool) \
                or stored['partial'] != bool(real.get('partial')):
            raise ValueError('stored input part partial mismatch')

    rows = evo.db.execute(
        'SELECT part, work_id FROM knowledge_seen WHERE source_id=? AND version=?',
        (sid, ver)).fetchall()
    owners = {r['part']: r['work_id'] for r in rows}
    for real in authoritative:
        owner = owners.get(real['part'], '__absent__')
        if owner == '__absent__':
            raise ValueError('owned source part has no seen claim')
        if owner != row['id']:
            raise ValueError('owned source part owned by another work')

    return {
        'source_id': sid,
        'version': ver,
        'project': source['project'],
        'session': source['session'],
        'role': source['role'],
        'kind': source['kind'],
        'text_sha': _sha_text(text),
        'created_at': source.get('created_at'),
        'parts': [{'part': p['part'], 'start': p['start'], 'end': p['end'],
                   'source_length': p['source_length'], 'partial': bool(p['partial']),
                   'sha256': _sha_text(p['text'])} for p in authoritative],
        'owned_refs': [p['part'] for p in authoritative],
    }


def _work_snapshot(row):
    """Body-free snapshot of the exact work row/cache the plan binds."""
    return {
        'id': row['id'],
        'project': row['project'],
        'session': row['session'],
        'pipeline_protocol': row['pipeline_protocol'],
        'state': row['state'],
        'attempts': row['attempts'],
        'next_at': row['next_at'],
        'lease_token': row['lease_token'],
        'lease_until': row['lease_until'],
        'inputs_sha': _sha_text(row['inputs'] or ''),
        'bundle_sha': _sha_text(row['bundle'] or ''),
        'proposal_sha': _sha_text(row['proposal'] or ''),
        'review_sha': _sha_text(row['review'] or ''),
        'error_sha': _sha_text(row['error'] or ''),
    }


# ------------------------------------------------------ card / op binding

def _card_digest(card):
    """HISTORICAL-STABLE canonical card digest over the fields a historical
    revision reliably preserves (identity, version, state, heading, full body
    hash, evidence digest). This matches the field set the auto receipt's card
    proofs use, so a later legitimate revision can never erase a past proof. It
    is a structural integrity marker, NOT authentication, and is used ONLY for
    durable historical verification (the completion receipt).
    """
    core = {
        'id': card.get('id'),
        'version': card.get('version'),
        'state': card.get('state'),
        'title': card.get('title'),
        'topic': card.get('topic'),
        'body_sha': _sha_text(card.get('body') or ''),
        'evidence_sha': _hash(card.get('evidence') or []),
    }
    return _hash(core)


def _card_full_digest(card):
    """CURRENT full canonical card digest, INCLUDING the actual evidence
    entries and the basis/dependency references.

    This binds the complete current target body (not merely a version number or
    a body hash): two same-version states whose evidence or basis differ
    therefore produce different digests, so a same-version body/evidence/basis
    drift on a NONMANUAL target cannot silently satisfy a stale operator plan.
    It is used for fresh plan equality (caller plan + ``prepare`` recompute);
    durable historical verification keeps the historical-stable
    :func:`_card_digest` instead, because a later legitimate revision must not
    erase a past proof.
    """
    basis = card.get('basis_cards') or []
    core = {
        'id': card.get('id'),
        'version': card.get('version'),
        'state': card.get('state'),
        'title': card.get('title'),
        'topic': card.get('topic'),
        'body_sha': _sha_text(card.get('body') or ''),
        'evidence': card.get('evidence') or [],
        'basis_cards': sorted(
            ({'card_id': b.get('card_id'), 'version': b.get('version')}
             for b in basis if isinstance(b, dict)),
            key=lambda b: (_canon(b))),
    }
    return _hash(core)


def _authorized_by_id(evo, project):
    out = {}
    for card in evo._authorized_catalog(project):
        cid = card.get('id')
        if isinstance(cid, str) and cid not in out:
            out[cid] = card
    return out


def _target_proof(evo, catalog, cid, where):
    card = catalog.get(cid)
    if card is None:
        raise ValueError('%s card is not authorized: %s' % (where, cid))
    return {
        'id': cid,
        'version': card.get('version'),
        'state': card.get('state'),
        # Historical-stable digest: durable proof for a later revision.
        'card_digest': _card_digest(card),
        # CURRENT full digest incl. evidence + basis: fresh plan equality.
        'card_full_digest': _card_full_digest(card),
    }


def _covered_binding(proof):
    """Body-free covered-proof binding: id + version + BOTH digests.

    The current full digest is included so a same-version covered-card body
    change is a plan-visible drift; the historical-stable digest is retained so
    the durable receipt can still prove the past coverage after a later
    legitimate revision.
    """
    return {'id': proof['id'], 'version': proof['version'],
            'card_digest': proof['card_digest'],
            'card_full_digest': proof['card_full_digest']}


def _frozen_window_reference_parts(evo, row):
    """Body-free snapshot of the row's ORIGINAL frozen window reference parts.

    A failed v2 row keeps its frozen bundle; the window may carry read-only
    prior-conversation reference parts (``responsibility == 'reference'``). Those
    are the ONLY non-owned, non-live references an operator completion may name
    besides the actual live prior-two events, so their exact
    identity/version/role/text-sha are extracted here (never guessed from the
    current session). A missing/malformed bundle yields no reference parts.
    """
    raw = row['bundle']
    if not raw:
        return []
    try:
        bundle = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(bundle, dict):
        return []
    context = bundle.get('context')
    if not isinstance(context, dict):
        return []
    window = context.get('window')
    if not isinstance(window, dict):
        return []
    parts = window.get('parts')
    if not isinstance(parts, list):
        return []
    out = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get('responsibility') != 'reference':
            continue
        sid = part.get('source_id')
        ver = part.get('version')
        text = part.get('text')
        if not isinstance(sid, str) or not sid:
            continue
        if not _is_int(ver) or ver < 1:
            continue
        if not isinstance(text, str):
            continue
        out.append({'source_id': sid, 'version': ver,
                    'role': part.get('role'), 'kind': part.get('kind'),
                    'text_sha': _sha_text(text)})
    return out


def _live_prior_reference_ids(evo, owned):
    """The actual live prior-two same-session event ids (delta_context model).

    Mirrors ``delta_context._prior_references`` selection exactly where it
    matters for admissibility: only real, available ``event`` sources in the
    SAME project+session, captured strictly BEFORE the owned source's capture
    time; newest first, tie-broken by id; at most two. No budget/char sizing is
    applied here because this never assembles a window -- it only decides which
    reference identities a plan may legitimately name. An unusable owned
    timestamp admits NOTHING (never guess an ordering).
    """
    session = owned.get('session')
    project = owned.get('project')
    owned_ts = owned.get('created_at')
    if not isinstance(session, str) or not session:
        return set()
    if not isinstance(project, str) or not project:
        return set()
    if owned_ts is None:
        return set()
    rows = evo.db.execute(
        "SELECT id, project, session, kind, role, created_at FROM sources"
        " WHERE project=? AND session=?", (project, session)).fetchall()
    candidates = []
    for row in rows:
        sid = row['id']
        if sid == owned['source_id']:
            continue
        if row['project'] != project or row['session'] != session:
            continue
        if row['kind'] != 'event' or row['role'] not in _OWNED_ROLES:
            continue
        if not evo.engine.source_available(sid):
            continue
        ts = row['created_at']
        if not isinstance(ts, str):
            continue
        if ts >= owned_ts:
            continue
        candidates.append((ts, sid))
    candidates.sort(key=lambda t: (-_ts_epoch(t[0]), t[1]))
    return {sid for _ts, sid in candidates[:2]}


def _ts_epoch(value):
    """Comparable epoch for an ISO ledger timestamp; a bad value sorts oldest."""
    try:
        from datetime import datetime
        return datetime.fromisoformat(value).timestamp()
    except (ValueError, TypeError):
        return float('-inf')


def _reference_source_snapshot(evo, owned, sid, quote, project, session,
                               frozen_refs, prior_ids):
    """Body-free identity snapshot for an admissible REFERENCE evidence entry.

    A genuinely NEW reference must be a real, available same-project ``event``
    whose quote is an exact substring of its CURRENT text, and it must be
    either (a) an exact original frozen-window reference part
    (source_id+version+text-sha+role) or (b) one of the actual live prior-two
    same-session events. A reference from another session that was never in
    the frozen window and is not a live prior event is refused (no borrowing
    other-session new refs). The snapshot freezes identity/version/textsha/
    role/session/time so any mutation between prepare and apply changes the
    plan hash. Returns None when inadmissible.
    """
    if not isinstance(sid, str) or not sid:
        return None
    if not evo.engine.source_available(sid):
        return None
    try:
        src = evo.engine.store.source(sid)
    except (KeyError, ValueError):
        return None
    if src.get('project') != project:
        return None
    if src.get('kind') != 'event' or src.get('role') not in _OWNED_ROLES:
        return None
    text = src.get('text')
    if not isinstance(text, str) or not text:
        return None
    if not isinstance(quote, str) or quote == '' or quote not in text:
        return None
    ver = src.get('version')
    text_sha = _sha_text(text)
    admitted = False
    for ref in frozen_refs:
        if (ref['source_id'] == sid and ref['version'] == ver
                and ref['text_sha'] == text_sha):
            if ref.get('role') in (None, src.get('role')):
                admitted = True
                break
    if not admitted and sid in prior_ids:
        if src.get('session') == session:
            admitted = True
    if not admitted:
        # A same-session source is still not automatically admissible: it must
        # be an actual prior-two event, never an arbitrary same-session row.
        return None
    return {'source_id': sid, 'version': ver, 'role': src.get('role'),
            'kind': src.get('kind'), 'session': src.get('session'),
            'project': src.get('project'), 'text_sha': text_sha,
            'created_at': src.get('created_at')}


def _inherited_evidence_tuples(evo, card_id):
    """The exact persisted evidence tuples of an existing target card.

    Read verbatim from the authoritative card row; each is the real historical
    provenance (source_id, version, quote). These are the ONLY entries that may
    bypass the owned/frozen/prior-reference admission: the ORIGINAL
    ``KnowledgeStore`` still re-validates their source/project/version/quote at
    apply time, and the target digest binds the card they came from. They keep
    their real (possibly other-session) history -- it is never rewritten.
    """
    if not isinstance(card_id, str) or not card_id:
        return set()
    card = evo.cards.get(card_id)
    if card is None:
        return set()
    out = set()
    for entry in card.get('evidence') or []:
        if not isinstance(entry, dict):
            continue
        sid = entry.get('source_id')
        ver = entry.get('version')
        quote = entry.get('quote')
        if not isinstance(sid, str) or not sid:
            continue
        if not _is_int(ver) or ver < 1:
            continue
        if not isinstance(quote, str) or not quote:
            continue
        out.add((sid, ver, quote))
    return out


def _validate_operations(evo, owned, operations, human_checks, row):
    """Validate a material operation list through the ORIGINAL KnowledgeStore.

    Every operation must be a legitimate op under the store's own schema. Its
    evidence entries are split into three admitted classes:

    * the OWNED source itself;
    * EXACT persisted target-evidence tuples for an update/retire (inherited
      provenance, genuinely possibly from another session -- accepted here and
      still re-validated by the core store);
    * a genuinely NEW reference, which must be an original frozen-window
      reference part or an actual live prior-two same-session event, and whose
      full identity/version/textsha/role/session/time is frozen into the plan.

    At least one real operation must carry the OWNED source. A manually edited
    update target additionally requires a human_check bound to its exact
    id/version/full-body digest, as the original store enforces at apply time.
    Returns ``(normalized, references)`` where ``references`` is the frozen,
    sorted body-free snapshot list of every new reference actually used.
    """
    project = owned['project']
    frozen_refs = _frozen_window_reference_parts(evo, row)
    prior_ids = _live_prior_reference_ids(evo, owned)
    verdict = evo.cards.validate_operations(operations, project)
    if not isinstance(verdict, dict) or verdict.get('ok') is not True:
        problems = verdict.get('problems') if isinstance(verdict, dict) else None
        raise ValueError('operations rejected by store: %s'
                         % _canon(problems or [])[:300])
    normalized = verdict.get('operations')
    if not isinstance(normalized, list) or not normalized:
        raise ValueError('operations normalized empty')

    for op in normalized:
        if op.get('op') != 'update' or not isinstance(op.get('id'), str):
            continue
        card = evo.cards.get(op['id'])
        if card is not None and card.get('manually_edited'):
            _require_manual_check(card, human_checks, op['id'])

    references = {}
    carries_own = False
    for op in normalized:
        evidence = op.get('evidence') or []
        if not isinstance(evidence, list) or not evidence:
            raise ValueError('every operator operation must carry evidence')
        # The exact persisted target evidence of THIS op's card is inherited in
        # full (update/retire only). Any entry matching one of those tuples is
        # accepted verbatim; every other entry must pass admission.
        inherited = set()
        if op.get('op') in ('update', 'retire') and isinstance(op.get('id'), str):
            inherited = _inherited_evidence_tuples(evo, op['id'])
        for entry in evidence:
            if not isinstance(entry, dict):
                raise ValueError('operation evidence entry malformed')
            sid = entry.get('source_id')
            quote = entry.get('quote')
            ver = entry.get('version')
            if sid == owned['source_id']:
                carries_own = True
                continue
            if _is_int(ver) and (sid, ver, quote) in inherited:
                # Inherited provenance: keep its real other-session history.
                continue
            snapshot = _reference_source_snapshot(
                evo, owned, sid, quote, project, owned['session'],
                frozen_refs, prior_ids)
            if snapshot is None:
                raise ValueError('operation evidence is neither the owned source, '
                                 'inherited target evidence, a frozen-window '
                                 'reference, nor a live prior-two reference')
            references[snapshot['source_id']] = snapshot
    if not carries_own:
        raise ValueError('at least one operation must carry the owned source')
    frozen = [references[sid] for sid in sorted(references)]
    return normalized, frozen


def _require_manual_check(card, human_checks, card_id):
    """Require a human_check bound to the manual card's true version/body."""
    body_sha = _sha_text(card.get('body') or '')
    for check in human_checks or []:
        if not isinstance(check, dict):
            continue
        if (check.get('id') == card_id
                and check.get('version') == card.get('version')
                and check.get('body_sha256') == body_sha
                and check.get('preserved') is True
                and isinstance(check.get('reason'), str)
                and len(check['reason'].strip()) >= 10):
            return
    raise ValueError('manual target %s requires a bound preservation check'
                     % card_id)


# ---------------------------------------------------------- plan assembly

def prepare(evo, request):
    """Return a deterministic operator plan; performs no writes.

    The CALLER-FACING plan binds the full request hash, the work snapshot/cache
    digests, the single owned event source (version + every part text-sha + seen
    owner), every new reference source identity/version/text-sha/role/session/
    time, the covered/target card version + BOTH the current full canonical
    digest (incl. evidence/basis) and the historical-stable digest, the project
    scope fingerprint and the normalized operations hash. There is no wall-clock
    timestamp, so a replay or re-prepare is byte-identical.

    NOTE: this plan is NOT body-free -- it carries the caller's request and the
    store-normalized operations (which may embed card bodies). Only the durable
    operator meta intent and the completion/intent audits are body-free.
    """
    norm = _validate_request(request)
    row = _work_row(evo, norm['work_id'])
    _validate_scope(evo, row)
    owned = _owned_event(evo, row)
    scope = evo._scope_fingerprint(row['project'])

    references = []
    targets = []
    if norm['decision'] == 'material':
        normalized_ops, references = _validate_operations(
            evo, owned, norm['operations'], norm['human_checks'], row)
        operations_sha = _hash(normalized_ops)
        target_ids = sorted({op['id'] for op in normalized_ops
                             if isinstance(op.get('id'), str)})
        # Freeze the CURRENT full canonical digest (incl. evidence + basis) of
        # every existing target card, so a same-version body/evidence/basis
        # drift on a nonmanual target cannot satisfy a stale plan. The
        # historical-stable digest is kept separately for durable verification.
        catalog = _authorized_by_id(evo, row['project'])
        targets = [_target_proof(evo, catalog, cid, 'target')
                   for cid in target_ids]
        covered_proofs = []
        covered_sha = None
    elif norm['decision'] == 'covered':
        catalog = _authorized_by_id(evo, row['project'])
        covered_proofs = [_target_proof(evo, catalog, cid, 'covered')
                          for cid in norm['covered_ids']]
        for proof in covered_proofs:
            if proof['state'] != 'active':
                raise ValueError('covered card is not active: %s' % proof['id'])
        # Bind BOTH the historical-stable digest and the CURRENT full digest.
        covered_sha = _hash([_covered_binding(p) for p in covered_proofs])
        normalized_ops = []
        operations_sha = _hash([])
        target_ids = list(norm['covered_ids'])
    else:  # nonmaterial
        normalized_ops = []
        operations_sha = _hash([])
        covered_proofs = []
        covered_sha = None
        target_ids = []

    request_binding = dict(norm)
    body = {
        'protocol': PROTOCOL,
        'operator_review_identity': OPERATOR_REVIEW_IDENTITY,
        'request': request_binding,
        'request_sha': _hash(request_binding),
        'grant_id': norm['grant_id'],
        'work_id': norm['work_id'],
        'project': row['project'],
        'decision': norm['decision'],
        'rationale_sha': _sha_text(norm['rationale']),
        'code_sha': norm['code_sha'],
        'acceptance_sha': norm['acceptance_sha'],
        'review_sha': norm['review_sha'],
        'scope_fingerprint': scope,
        'work_snapshot': _work_snapshot(row),
        'source': owned,
        'operations_sha': operations_sha,
        'normalized_operations': normalized_ops,
        'references': references,
        'target_ids': target_ids,
        'targets': targets,
        'covered': covered_proofs,
        'covered_sha': covered_sha,
        'human_checks_sha': _hash(norm['human_checks']),
    }
    plan = dict(body)
    plan['plan_sha'] = _hash(body)
    _validate_plan_schema(plan)
    return plan


def _validate_plan_schema(plan):
    if not isinstance(plan, dict):
        raise ValueError('plan must be an object')
    required = ('protocol', 'operator_review_identity', 'request', 'request_sha',
                'grant_id', 'work_id', 'project', 'decision', 'rationale_sha',
                'code_sha', 'acceptance_sha', 'review_sha', 'scope_fingerprint',
                'work_snapshot', 'source', 'operations_sha',
                'normalized_operations', 'references', 'target_ids', 'targets',
                'covered', 'covered_sha', 'human_checks_sha', 'plan_sha')
    _reject_extra(plan, required, 'plan')
    for key in required:
        if key not in plan:
            raise ValueError('plan missing %s' % key)
    if plan['protocol'] != PROTOCOL:
        raise ValueError('plan protocol mismatch')
    if plan['operator_review_identity'] != OPERATOR_REVIEW_IDENTITY:
        raise ValueError('plan operator identity mismatch')
    if not _is_hex(plan['plan_sha']):
        raise ValueError('plan_sha malformed')
    body = {k: v for k, v in plan.items() if k != 'plan_sha'}
    if _hash(body) != plan['plan_sha']:
        raise ValueError('plan self-hash mismatch')


# --------------------------------------------------------------- approval

def _proposal_hash(operations):
    """The EXACT op hash KnowledgeStore.apply computes for a batch."""
    return hashlib.sha256(
        json.dumps(operations, ensure_ascii=False, sort_keys=True,
                   separators=(',', ':')).encode('utf-8')).hexdigest()


def _approval(plan):
    """The operator attestation handed to the ORIGINAL ``KnowledgeStore.apply``.

    It is shaped exactly like an approved review (approved/findings/
    proposal_hash/human_checks) because that is the store's real contract. The
    ``origin`` label records that the approval came from a local operator, NOT
    from a model review; it never claims semantic model success.
    """
    return {
        'approved': True,
        'findings': [],
        'proposal_hash': _proposal_hash(plan['normalized_operations']),
        'human_checks': [dict(c) for c in plan['request']['human_checks']],
        'origin': OPERATOR_ORIGIN,
        'operator_review_identity': OPERATOR_REVIEW_IDENTITY,
    }


# ------------------------------------------------------------- receipts

def _applied_cards(evo, changed_ids, applied_versions):
    from . import delta_receipts
    proofs = []
    for cid in changed_ids:
        hist = delta_receipts._card_at(evo, cid, applied_versions[cid])
        if hist is None:
            raise ValueError('applied card historical version missing')
        proofs.append({
            'id': cid,
            'version': applied_versions[cid],
            'card_digest': _card_digest(hist),
        })
    return proofs


def _call_ledger(evo, work_id):
    """Digest + count of the real model call ledger for this work (read-only)."""
    rows = evo.db.execute(
        'SELECT id, stage, error, elapsed FROM knowledge_calls WHERE work_id=?'
        ' ORDER BY created_at, id', (work_id,)).fetchall()
    entries = [{'id': r['id'], 'stage': r['stage'], 'error': r['error'],
                'elapsed': r['elapsed']} for r in rows]
    return {'count': len(entries), 'sha': _hash(entries)}


def _intent(plan, batch, applied_versions, calls_before):
    """Body-free durable operator intent written at apply/commit time.

    ``calls_before`` is the REAL call-ledger snapshot taken DURING the commit
    transaction (never recomputed at finalize), so a later fabricated call or a
    drift between commit and resume can never be silently absorbed. The frozen
    new-reference identities and the target/covered current digests are also
    bound here, so the durable comparison is over every relevant field rather
    than a subset.
    """
    return {
        'schema': SCHEMA,
        'origin': OPERATOR_ORIGIN,
        'protocol': PROTOCOL,
        'operator_review_identity': OPERATOR_REVIEW_IDENTITY,
        'grant_id': plan['grant_id'],
        'work_id': plan['work_id'],
        'project': plan['project'],
        'decision': plan['decision'],
        'plan_sha': plan['plan_sha'],
        'request_sha': plan['request_sha'],
        'rationale_sha': plan['rationale_sha'],
        'code_sha': plan['code_sha'],
        'acceptance_sha': plan['acceptance_sha'],
        'review_sha': plan['review_sha'],
        'scope_fingerprint': plan['scope_fingerprint'],
        'operations_sha': plan['operations_sha'],
        'op_hash': batch['op_hash'],
        'review_hash': batch['review_hash'],
        'changed_ids': list(batch['changed_ids']),
        'applied_versions': dict(applied_versions),
        'source': plan['source'],
        'source_id': plan['source']['source_id'],
        'source_version': plan['source']['version'],
        'source_sha': plan['source']['text_sha'],
        'references': list(plan.get('references') or []),
        'targets': list(plan.get('targets') or []),
        'covered': plan['covered'],
        'covered_sha': plan['covered_sha'],
        'calls_before': dict(calls_before),
    }


def _completion_receipt(plan, batch, applied_versions, applied_cards, calls_before):
    """Body-free operator completion receipt (the durable historical proof).

    ``calls_before`` is the ORIGINAL commit-time ledger snapshot carried in the
    intent -- never recomputed at finalize.
    """
    return {
        'schema': SCHEMA,
        'origin': OPERATOR_ORIGIN,
        'protocol': PROTOCOL,
        'operator_review_identity': OPERATOR_REVIEW_IDENTITY,
        'work_id': plan['work_id'],
        'project': plan['project'],
        'grant_id': plan['grant_id'],
        'decision': plan['decision'],
        'rationale_sha': plan['rationale_sha'],
        'request_sha': plan['request_sha'],
        'plan_sha': plan['plan_sha'],
        'code_sha': plan['code_sha'],
        'acceptance_sha': plan['acceptance_sha'],
        'review_sha': plan['review_sha'],
        'scope_fingerprint': plan['scope_fingerprint'],
        'source': plan['source'],
        'operations_sha': plan['operations_sha'],
        'op_hash': batch['op_hash'],
        'review_hash': batch['review_hash'],
        'changed_ids': list(batch['changed_ids']),
        'applied_versions': dict(applied_versions),
        'applied_cards': applied_cards,
        'references': list(plan.get('references') or []),
        'targets': list(plan.get('targets') or []),
        'covered': plan['covered'],
        'covered_sha': plan['covered_sha'],
        'calls_before': dict(calls_before),
        'time': time.time(),
    }


# ---------------------------------------------------------------- apply

def _intent_key(grant_id):
    return OPERATOR_META_PREFIX + grant_id


def _stored_intent(evo, grant_id):
    row = evo.db.execute('SELECT value FROM meta WHERE key=?',
                         (_intent_key(grant_id),)).fetchone()
    if row is None:
        return None
    try:
        value = json.loads(row['value'])
    except ValueError:
        raise ValueError('stored operator intent malformed')
    if not isinstance(value, dict):
        raise ValueError('stored operator intent malformed')
    return value


def apply(evo, plan, files):
    """Durably complete exactly one failed v2 row from an operator plan.

    Runs under the existing engine lock. A repeated identical plan whose grant
    was already committed returns the stored receipt with ``replayed=True`` and
    zero writes; a different plan on the same grant is rejected. Any failure
    rolls back. Publication and verification happen under the SAME lock; only a
    confirmed publication finalizes ``done``.
    """
    _validate_plan_schema(plan)
    grant_key = _intent_key(plan['grant_id'])
    work_id = plan['work_id']

    with evo.engine.locked():
        stored_meta = evo.db.execute('SELECT value FROM meta WHERE key=?',
                                     (grant_key,)).fetchone()
        if stored_meta is not None:
            try:
                stored = json.loads(stored_meta['value'])
            except ValueError:
                raise ValueError('stored operator intent malformed')
            if stored.get('plan_sha') != plan['plan_sha']:
                raise ValueError('grant_id already bound to a different plan')
            replay = {'origin': OPERATOR_ORIGIN, 'grant_id': plan['grant_id'],
                      'work_id': work_id, 'plan_sha': plan['plan_sha'],
                      'replayed': True}
            # A committed completion must be PROVEN, never merely echoed: verify
            # the whole operator chain (a malformed/mixed completion raises).
            replay['receipt'] = None
            if _operator_audits(evo, work_id, OPERATOR_COMPLETE_AUDIT):
                verified = verify_operator_completed(evo, work_id)
                replay['receipt'] = verified['receipt']
                replay['completed'] = True
                return replay
            row = _work_row(evo, work_id)
            if row is not None and row['state'] == OPERATOR_STATE:
                # The batch is already committed but the row has not been
                # finalized (a prior publication failure). The SAFE publisher
                # validates the real intent and NEVER calls store.apply again.
                try:
                    resumed = publish_one(evo, work_id, files)
                except (ValueError, KeyError, OSError):
                    resumed = {'publication_attention': True}
                replay['resumed'] = True
                if resumed.get('receipt') is not None:
                    replay['receipt'] = resumed['receipt']
                if resumed.get('publication_attention'):
                    replay['publication_attention'] = True
            return replay

        # Resync manual files BEFORE the atomic transition (publish/sync manage
        # their own commit boundaries, so they run outside any transaction).
        files.sync()

        with evo.engine.transaction():
            fresh = prepare(evo, plan['request'])
            if _canon(fresh) != _canon(plan):
                raise ValueError('plan is stale or not canonical')
            row = _work_row(evo, work_id)
            _validate_scope(evo, row)

            approval = _approval(plan)
            result = evo.cards.apply(work_id, plan['project'],
                                     plan['normalized_operations'], approval)
            if result.get('replayed'):
                # A committed batch already existed under this id: the scope
                # guard above should have blocked it, so treat as a failure.
                raise ValueError('batch id already committed')
            changed_ids = list(result['changed_ids'])
            applied_versions = {}
            for cid in changed_ids:
                card = evo.cards.get(cid)
                if card is not None:
                    applied_versions[cid] = card.get('version')
            batch_row = evo.db.execute(
                'SELECT * FROM knowledge_batches WHERE batch_id=?', (work_id,)).fetchone()
            if batch_row is None:
                raise ValueError('batch was not written by the store')
            batch = {'op_hash': batch_row['op_hash'],
                     'review_hash': batch_row['review_hash'],
                     'changed_ids': changed_ids}

            intent = _intent(plan, batch, applied_versions,
                             _call_ledger(evo, work_id))
            evo.db.execute(
                'INSERT INTO meta(key,value) VALUES(?,?)',
                (grant_key, _canon(intent)))
            evo.db.execute(
                'INSERT INTO knowledge_failures(work_id,state,error,created_at)'
                ' VALUES(?,?,?,?)',
                (work_id, OPERATOR_INTENT_AUDIT,
                 _canon(dict(intent, phase='committed')), time.time()))
            cur = evo.db.execute(
                "UPDATE knowledge_work SET state=?,error=NULL,lease_token=NULL,"
                " lease_until=0 WHERE id=? AND pipeline_protocol=? AND state='failed'",
                (OPERATOR_STATE, work_id, PROTOCOL))
            if cur.rowcount != 1:
                raise ValueError('work could not be moved to operator pending')

        # Publication and verification under the SAME held lock. files.publish
        # commits its own receipt, so it runs OUTSIDE any transaction.
        return _publish_and_finalize(evo, plan, batch, applied_versions, files,
                                     intent)


def _publish_and_finalize(evo, plan, batch, applied_versions, files, intent=None):
    work_id = plan['work_id']
    changed_ids = list(batch['changed_ids'])
    outcome = {'origin': OPERATOR_ORIGIN, 'grant_id': plan['grant_id'],
               'work_id': work_id, 'plan_sha': plan['plan_sha'],
               'changed_ids': changed_ids, 'replayed': False}
    if changed_ids:
        from . import delta_execution
        files.publish(changed_ids)
        if not delta_execution._verify_publication(evo, files, changed_ids):
            outcome['publication_attention'] = True
            return outcome
    # The call ledger must match the ORIGINAL intent's commit-time snapshot: a
    # fabricated success (an extra/removed call) can never be finalized as a
    # verified completion.
    calls_now = _call_ledger(evo, work_id)
    if intent is not None and calls_now != intent.get('calls_before'):
        raise ValueError('operator call ledger changed since commit')
    applied_cards = _applied_cards(evo, changed_ids, applied_versions)
    calls_before = calls_now if intent is None else intent['calls_before']
    # Only AFTER a successful publication: one finalize transaction.
    with evo.engine.transaction():
        current = _work_row(evo, work_id)
        if current is None or current['state'] != OPERATOR_STATE:
            outcome['replayed'] = True
            return outcome
        receipt = _completion_receipt(plan, batch, applied_versions,
                                      applied_cards, calls_before)
        evo.db.execute(
            'INSERT INTO knowledge_failures(work_id,state,error,created_at)'
            ' VALUES(?,?,?,?)',
            (work_id, OPERATOR_COMPLETE_AUDIT, _canon(receipt), time.time()))
        evo.db.execute(
            "UPDATE knowledge_work SET state='done', inputs='[]',"
            " proposal=NULL, review=NULL, bundle=NULL, lease_token=NULL,"
            " lease_until=0, error=NULL, finished_at=? WHERE id=?",
            (time.time(), work_id))
    outcome['receipt'] = receipt
    return outcome


# ------------------------------------------------------------- resume

def _intent_for_row(evo, work_id):
    """The unique operator intent for a row, or raise if absent/ambiguous."""
    rows = evo.db.execute(
        'SELECT value FROM meta WHERE key LIKE ? ORDER BY key',
        (OPERATOR_META_PREFIX + '%',)).fetchall()
    matches = []
    for row in rows:
        try:
            value = json.loads(row['value'])
        except ValueError:
            continue
        if isinstance(value, dict) and value.get('work_id') == work_id:
            matches.append(value)
    if len(matches) != 1:
        raise ValueError('operator intent not unique for %s' % work_id)
    return matches[0]


def _verify_intent_batch(evo, intent):
    """Cross-check the intent against the real committed batch + cache.

    Validates the batch's PROJECT, op/review hashes, changed ids and the exact
    applied-version shape (ids == changed ids, each a positive int), so a
    malformed or substituted batch can never be published.
    """
    work_id = intent['work_id']
    batch = evo.db.execute('SELECT * FROM knowledge_batches WHERE batch_id=?',
                           (work_id,)).fetchone()
    if batch is None:
        raise ValueError('operator intent batch missing')
    if batch['project'] != intent['project']:
        raise ValueError('operator intent batch project mismatch')
    if batch['op_hash'] != intent['op_hash']:
        raise ValueError('operator intent op hash mismatch')
    if batch['review_hash'] != intent['review_hash']:
        raise ValueError('operator intent review hash mismatch')
    try:
        changed = json.loads(batch['changed_ids'])
    except (ValueError, TypeError):
        raise ValueError('operator batch changed ids malformed')
    if not isinstance(changed, list) or list(changed) != list(intent['changed_ids']):
        raise ValueError('operator intent changed ids mismatch')
    for cid in changed:
        if not isinstance(cid, str) or not cid:
            raise ValueError('operator batch changed id malformed')
    applied = intent.get('applied_versions')
    if not isinstance(applied, dict):
        raise ValueError('operator applied versions malformed')
    if set(applied) != set(changed):
        raise ValueError('operator applied versions do not match changed ids')
    for ver in applied.values():
        if not _is_int(ver) or ver < 1:
            raise ValueError('operator applied version malformed')
    return batch


def _validated_operator_intent(evo, work_id):
    """Validate the FULL operator intent chain for one row (read-only).

    Requires: an existing v2 row; EXACTLY ONE intent audit (missing, malformed
    or ambiguous fails closed); the durable meta intent exists; the body-free
    meta equals the unique intent audit MINUS the commit ``phase`` marker; and
    the real committed batch matches the intent's project/hashes/changed
    ids/applied versions. Returns ``(row, intent_audit, meta)``. A mixed
    auto+operator or already-completed origin is NOT checked here (that is a
    caller concern), but the caller may pass ``require_pending`` to enforce it.
    """
    row = _work_row(evo, work_id)
    if row is None:
        raise ValueError('work missing')
    if row['pipeline_protocol'] != PROTOCOL:
        raise ValueError('work is not the v2 pipeline protocol')
    intent_audit = _unique_operator_audit(evo, work_id, OPERATOR_INTENT_AUDIT)
    if intent_audit.get('origin') != OPERATOR_ORIGIN:
        raise ValueError('operator intent origin mismatch')
    if intent_audit.get('schema') != SCHEMA:
        raise ValueError('operator intent schema mismatch')
    if intent_audit.get('work_id') != work_id:
        raise ValueError('operator intent work id mismatch')
    grant_id = intent_audit.get('grant_id')
    if not _is_slug(grant_id):
        raise ValueError('operator intent grant id malformed')
    meta = _stored_intent(evo, grant_id)
    if meta is None:
        raise ValueError('operator intent meta missing')
    # The durable meta must equal the unique intent audit MINUS the phase note.
    body = {k: v for k, v in intent_audit.items() if k != 'phase'}
    if _canon(meta) != _canon(body):
        raise ValueError('operator meta/intent mismatch')
    _verify_intent_batch(evo, meta)
    return row, intent_audit, meta


def publish_one(evo, work_id, files):
    """Resume publication of ONE operator-pending row from its real intent.

    Runs under the existing engine lock. Makes NO model call and never repeats
    ``KnowledgeStore.apply``: the batch is already committed. Refuses a missing,
    paused, done, wrong-protocol, malformed or mixed-origin row/intent BEFORE
    any publish, requires the body-free meta to equal the unique intent audit
    minus ``phase``, and re-verifies the real batch/project/hashes/ids/applied
    version shape plus the owned/source historical and covered proofs.
    """
    with evo.engine.locked():
        row = _work_row(evo, work_id)
        if row is None:
            raise ValueError('work missing')
        if row['pipeline_protocol'] != PROTOCOL:
            raise ValueError('work is not the v2 pipeline protocol')
        if row['state'] == 'protocol_paused':
            raise ValueError('work is protocol_paused; unpark before publishing')
        if row['state'] == 'done':
            raise ValueError('work already done')
        if row['state'] != OPERATOR_STATE:
            raise ValueError('work is not operator_publication_pending')
        if _operator_audits(evo, work_id, OPERATOR_COMPLETE_AUDIT):
            raise ValueError('operator completion already recorded')
        from . import delta_receipts
        if _operator_audits(evo, work_id, delta_receipts.COMPLETE_AUDIT):
            raise ValueError('mixed auto completion present')
        row, intent, meta = _validated_operator_intent(evo, work_id)
        # Historical owned-source/covered proofs must still resolve before any
        # publication effect.
        _verify_owned_source(evo, work_id, row['project'], meta['source'])
        if meta['decision'] == 'covered':
            if not meta.get('covered'):
                raise ValueError('operator covered proofs missing')
            for proof in meta['covered']:
                _verify_covered_proof(evo, delta_receipts, proof)
        # Rebuild the minimal body-free plan view needed for the receipt from the
        # persisted intent (never a re-prepare, which could drift).
        plan = {
            'grant_id': intent['grant_id'],
            'work_id': work_id,
            'project': intent['project'],
            'decision': intent['decision'],
            'rationale_sha': intent['rationale_sha'],
            'request_sha': intent['request_sha'],
            'plan_sha': intent['plan_sha'],
            'code_sha': intent['code_sha'],
            'acceptance_sha': intent['acceptance_sha'],
            'review_sha': intent['review_sha'],
            'scope_fingerprint': intent['scope_fingerprint'],
            'source': intent['source'],
            'operations_sha': intent['operations_sha'],
            'references': list(intent.get('references') or []),
            'targets': list(intent.get('targets') or []),
            'covered': intent.get('covered') or [],
            'covered_sha': intent['covered_sha'],
        }
        batch = {'op_hash': intent['op_hash'], 'review_hash': intent['review_hash'],
                 'changed_ids': list(intent['changed_ids'])}
        return _publish_and_finalize(evo, plan, batch, intent['applied_versions'],
                                     files, intent)


def resume_pending(evo, files, limit=3):
    """Resume up to ``limit`` operator-pending rows from their real intents.

    Unknown/malformed intent fails closed and is reported, never silently
    skipped. No model calls; no repeated store apply.
    """
    if not _is_int(limit) or limit < 0:
        limit = 3
    limit = min(limit, 3)
    rows = evo.db.execute(
        "SELECT id FROM knowledge_work WHERE pipeline_protocol=? AND state=?"
        " ORDER BY created_at, id", (PROTOCOL, OPERATOR_STATE)).fetchall()
    published = 0
    attention = 0
    failed = 0
    for row in rows:
        if published + attention + failed >= limit:
            break
        work_id = row['id']
        try:
            outcome = publish_one(evo, work_id, files)
        except (ValueError, KeyError, OSError):
            failed += 1
            continue
        if outcome.get('publication_attention'):
            attention += 1
        else:
            published += 1
    return {'operator_pending': len(rows), 'operator_published': published,
            'operator_attention': attention, 'operator_failed': failed}


# --------------------------------------------------------------- verify

def validate_operator_intent(evo, work_id):
    """Public read-only validation of the FULL operator intent chain.

    Used by ``delta_migration.unpark`` (and any other read-only caller) to
    decide, under the existing engine lock, whether a row's recorded
    ``operator_publication_pending`` prior state is REAL: a unique matching
    operator intent audit + durable meta + a real committed batch all agree.
    Raises ValueError on any missing/malformed/ambiguous/mismatched chain.
    Returns the validated meta intent.
    """
    _row, _intent, meta = _validated_operator_intent(evo, work_id)
    return meta


def _operator_audits(evo, work_id, state):
    rows = evo.db.execute(
        'SELECT error FROM knowledge_failures WHERE work_id=? AND state=?'
        ' ORDER BY created_at', (work_id, state)).fetchall()
    return rows


def _unique_operator_audit(evo, work_id, state):
    rows = _operator_audits(evo, work_id, state)
    if len(rows) != 1:
        raise ValueError('operator audit (%s) not unique' % state)
    try:
        obj = json.loads(rows[0]['error'])
    except (ValueError, TypeError):
        raise ValueError('operator audit malformed')
    if not isinstance(obj, dict):
        raise ValueError('operator audit malformed')
    return obj


def has_operator_completion(evo, work_id):
    """Whether a unique operator completion origin exists for this row."""
    return len(_operator_audits(evo, work_id, OPERATOR_COMPLETE_AUDIT)) > 0


def verify_operator_completed(evo, work_id):
    """Prove a finished operator row read-only; never writes/publishes.

    Re-derives the whole chain from the authoritative store: the unique operator
    meta intent, the unique intent and completion audits, the real KnowledgeStore
    batch (op/review hashes + changed ids), the historical applied card versions
    and canonical digests, the owned source parts + seen owner, the scope
    fingerprint, the covered/target card proofs and the real call ledger digest.
    """
    from . import delta_receipts

    row = _work_row(evo, work_id)
    if row is None:
        raise ValueError('work missing')
    if row['pipeline_protocol'] != PROTOCOL:
        raise ValueError('protocol mismatch')
    if row['state'] != 'done':
        raise ValueError('work not done')

    # Reject a MIXED auto + operator completion: exactly one origin must exist.
    if len(_operator_audits(evo, work_id, OPERATOR_COMPLETE_AUDIT)) == 0:
        raise ValueError('operator completion audit missing')
    auto_rows = _operator_audits(evo, work_id, delta_receipts.COMPLETE_AUDIT)
    if auto_rows:
        raise ValueError('mixed auto and operator completions')

    intent = _unique_operator_audit(evo, work_id, OPERATOR_INTENT_AUDIT)
    receipt = _unique_operator_audit(evo, work_id, OPERATOR_COMPLETE_AUDIT)

    if intent.get('origin') != OPERATOR_ORIGIN \
            or receipt.get('origin') != OPERATOR_ORIGIN:
        raise ValueError('operator origin mismatch')
    if receipt.get('schema') != SCHEMA or intent.get('schema') != SCHEMA:
        raise ValueError('operator schema mismatch')
    if receipt.get('work_id') != work_id or intent.get('work_id') != work_id:
        raise ValueError('operator work id mismatch')
    if receipt.get('project') != row['project']:
        raise ValueError('operator project mismatch')

    # The durable META intent must exist and bind the same plan/ops as both the
    # intent audit and the completion receipt; a forged audit without the
    # original meta is rejected. This is the request->plan->batch hash chain:
    # request_sha -> plan_sha -> operations_sha/op_hash/review_hash -> changed.
    meta = _stored_intent(evo, receipt['grant_id'])
    if meta is None:
        raise ValueError('operator intent meta missing')
    for name in ('plan_sha', 'op_hash', 'review_hash', 'request_sha',
                 'operations_sha'):
        if meta.get(name) != receipt.get(name):
            raise ValueError('operator meta/receipt mismatch: %s' % name)
        if meta.get(name) != intent.get(name):
            raise ValueError('operator meta/intent mismatch: %s' % name)
    if meta.get('changed_ids') != list(receipt.get('changed_ids')):
        raise ValueError('operator meta changed ids mismatch')
    if receipt.get('operator_review_identity') != OPERATOR_REVIEW_IDENTITY:
        raise ValueError('operator identity mismatch')
    # The intent, the receipt and the durable meta must all bind the SAME owned
    # source identity (id/version/text sha).
    src = receipt['source']
    for name, value in (('source_id', src['source_id']),
                        ('source_version', src['version']),
                        ('source_sha', src['text_sha'])):
        if meta.get(name) != value or intent.get(name) != value:
            raise ValueError('operator source binding mismatch: %s' % name)
    for name in ('rationale_sha', 'code_sha', 'acceptance_sha', 'review_sha',
                 'scope_fingerprint', 'covered_sha', 'decision'):
        if receipt.get(name) != intent.get(name):
            raise ValueError('operator receipt/intent mismatch: %s' % name)
        if meta.get(name) != intent.get(name):
            raise ValueError('operator meta/intent mismatch: %s' % name)

    # The FULL owned-source structure (every part identity/bounds/text hash and
    # the owned refs) must be bound identically across meta/intent/receipt -- not
    # merely the three top-level source fields. A substituted or truncated part
    # structure in any one record is therefore refused.
    for name in ('source', 'references', 'targets', 'covered', 'changed_ids',
                 'applied_versions'):
        if _canon(meta.get(name)) != _canon(intent.get(name)):
            raise ValueError('operator meta/intent structural mismatch: %s' % name)
        if _canon(receipt.get(name)) != _canon(intent.get(name)):
            raise ValueError('operator receipt/intent structural mismatch: %s' % name)
    if meta.get('calls_before') != intent.get('calls_before'):
        raise ValueError('operator meta/intent calls mismatch')

    # Historical scope binding: the SAME frozen scope fingerprint must agree
    # across intent/meta/receipt. A later legitimate change to the project's
    # CURRENT permissions must NOT erase the proof of a past completion, so the
    # live fingerprint is deliberately NOT compared here; current permissions
    # are enforced at card-consumption time instead.
    for record, label in ((intent, 'intent'), (meta, 'meta')):
        if record.get('scope_fingerprint') != receipt.get('scope_fingerprint'):
            raise ValueError('operator historical scope binding mismatch: %s'
                             % label)

    # The REAL batch must exist and match the recorded op/review hashes.
    batch = evo.db.execute('SELECT * FROM knowledge_batches WHERE batch_id=? AND'
                           ' project=?', (work_id, row['project'])).fetchone()
    if batch is None:
        raise ValueError('operator batch missing')
    if batch['op_hash'] != receipt['op_hash']:
        raise ValueError('operator batch op hash mismatch')
    if batch['review_hash'] != receipt['review_hash']:
        raise ValueError('operator batch review hash mismatch')
    try:
        changed = json.loads(batch['changed_ids'])
    except ValueError:
        raise ValueError('operator batch changed ids malformed')
    if list(changed) != list(receipt['changed_ids']):
        raise ValueError('operator batch changed ids mismatch')

    # Historical applied card versions + canonical digests must still resolve.
    applied_versions = receipt['applied_versions']
    if not isinstance(applied_versions, dict) or set(applied_versions) != set(changed):
        raise ValueError('operator applied versions malformed')
    if not isinstance(receipt['applied_cards'], list):
        raise ValueError('operator applied cards malformed')
    # EXACT number/set/unique ids of applied_cards must equal the changed ids.
    proof_ids = [proof.get('id') if isinstance(proof, dict) else None
                 for proof in receipt['applied_cards']]
    if any(pid is None for pid in proof_ids):
        raise ValueError('operator applied card proof malformed')
    if len(proof_ids) != len(set(proof_ids)):
        raise ValueError('operator applied card proof ids not unique')
    if set(proof_ids) != set(changed):
        raise ValueError('operator applied cards do not match changed ids')
    for proof in receipt['applied_cards']:
        cid = proof['id']
        version = proof.get('version')
        if not _is_int(version) or version < 1:
            raise ValueError('operator applied card proof version malformed')
        if applied_versions.get(cid) != version:
            raise ValueError('operator applied card version mismatch')
        hist = delta_receipts._card_at(evo, cid, version)
        if hist is None:
            raise ValueError('operator applied card version missing')
        if _card_digest(hist) != proof.get('card_digest'):
            raise ValueError('operator applied card digest mismatch')

    # Owned source parts + seen owner: the SAME read-only helpers the receipt
    # module uses for auto completions.
    source = receipt['source']
    _verify_owned_source(evo, work_id, row['project'], source)

    # Covered/target card proofs (covered decision) must still resolve. A later
    # legitimate card revision must not erase the historical proof, so the
    # frozen (version, canonical digest) pair is checked against the HISTORICAL
    # revision via the same read-only helper the auto receipt uses.
    if receipt['decision'] == 'covered':
        if not receipt['covered']:
            raise ValueError('operator covered proofs missing')
        for proof in receipt['covered']:
            if not isinstance(proof, dict):
                raise ValueError('operator covered proof malformed')
            expected = _covered_binding(proof)
            if proof.get('card_full_digest') is None:
                raise ValueError('operator covered proof digest missing')
            _verify_covered_proof(evo, delta_receipts, proof)
            if expected.get('card_digest') != proof.get('card_digest'):
                raise ValueError('operator covered proof binding malformed')

    # No fictitious model provenance: the real call ledger digest is unchanged.
    calls_now = _call_ledger(evo, work_id)
    if calls_now != receipt['calls_before']:
        raise ValueError('operator call ledger changed')

    return {'verified': True, 'work_id': work_id, 'origin': OPERATOR_ORIGIN,
            'receipt': receipt, 'processed': 0, 'replayed': True}


def _recompute_source_parts(source, size=10000):
    """Recompute the authoritative part identity/bounds from stored text.

    Uses the SAME windowing algorithm as ``Evolution._source_parts`` so the
    historical proof can be rebuilt independently of the stored receipt: every
    part's number/start/end/source_length/partial and text sha are derived from
    the authoritative source bytes, never trusted from the receipt.
    """
    text = source['text']
    start = 0
    index = 0
    out = []
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            boundary = text.rfind('\n\n', start + size // 2, end)
            if boundary > start:
                end = boundary + 2
        out.append({
            'part': index, 'start': start, 'end': end,
            'source_length': len(text),
            'partial': start > 0 or end < len(text),
            'sha256': _sha_text(text[max(0, start - 300):min(len(text), end + 300)]),
        })
        start = end
        index += 1
    return out


def _verify_owned_source(evo, work_id, project, source):
    """Re-prove the owned source from its stored HISTORICAL bytes.

    Recomputes ALL authoritative parts (number/bounds/partial/text-sha) from the
    source text and requires the stored part set to match EXACTLY (same count,
    same part numbers, no dropped/extra part), revalidates each claimed part's
    type/bounds/text-sha, and confirms every authoritative part is owned by
    ``work_id``. A missing/malformed part or a superseded historical version is
    refused rather than merely checking each claimed owner.
    """
    sid = source['source_id']
    ver = source['version']
    if not isinstance(sid, str) or not sid:
        raise ValueError('operator owned source id malformed')
    if not _is_int(ver) or ver < 1:
        raise ValueError('operator owned source version malformed')
    # The HISTORICAL version must still resolve and hash identically; a later
    # legitimate source revision (or even a revocation) of the current version
    # must NOT erase the proof that the PAST commit happened against version
    # ``ver``. This matches the auto receipt's historical-source verification.
    try:
        live = evo.engine.store.source(sid, ver)
    except (KeyError, ValueError):
        raise ValueError('operator owned source version missing')
    if live.get('project') != project:
        raise ValueError('operator owned source project mismatch')
    if _sha_text(live.get('text')) != source['text_sha']:
        raise ValueError('operator owned source text hash mismatch')
    if not isinstance(source.get('parts'), list):
        raise ValueError('operator owned source parts malformed')
    real = _recompute_source_parts(live)
    if not real:
        raise ValueError('operator owned source has no parts')
    stored = source['parts']
    # Every stored part must be a strict object with real typed fields; a
    # bool is never accepted as an int and a missing field is a rejection.
    stored_by_part = {}
    for part in stored:
        if not isinstance(part, dict):
            raise ValueError('operator owned part malformed')
        pnum = part.get('part')
        if not _is_int(pnum) or pnum < 0:
            raise ValueError('operator owned part number malformed')
        if pnum in stored_by_part:
            raise ValueError('operator owned part duplicated')
        if not isinstance(part.get('partial'), bool):
            raise ValueError('operator owned part partial malformed')
        if not _is_hex(part.get('sha256')):
            raise ValueError('operator owned part text sha malformed')
        for field in ('start', 'end', 'source_length'):
            if not _is_int(part.get(field)):
                raise ValueError('operator owned part %s malformed' % field)
        stored_by_part[pnum] = part
    if set(stored_by_part) != {p['part'] for p in real}:
        raise ValueError('operator owned parts do not match authoritative set')
    for part in real:
        claimed = stored_by_part[part['part']]
        for field in ('start', 'end', 'source_length', 'partial', 'sha256'):
            if claimed.get(field) != part[field]:
                raise ValueError('operator owned part %s mismatch' % field)
    # Every authoritative part must be owned by this work id.
    for part in real:
        seen = evo.db.execute(
            'SELECT work_id FROM knowledge_seen WHERE source_id=? AND version=?'
            ' AND part=?', (sid, ver, part['part'])).fetchone()
        if seen is None or seen['work_id'] != work_id:
            raise ValueError('operator owned part owner mismatch')


def _verify_covered_proof(evo, delta_receipts, proof):
    """Re-prove a covered card proof against its HISTORICAL revision.

    A later legitimate revision must not erase the past coverage, so only the
    frozen (version, historical-stable digest) pair is checked historically;
    the CURRENT full digest is a fresh-plan binding and is not required to still
    match after a legitimate revision.
    """
    hist = delta_receipts._card_at(evo, proof['id'], proof['version'])
    if hist is None:
        raise ValueError('operator covered card version missing')
    if _card_digest(hist) != proof['card_digest']:
        raise ValueError('operator covered card digest mismatch')
