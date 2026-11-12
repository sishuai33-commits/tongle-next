"""Bounded operator migration / rollback for the isolated v2 delta pipeline.

This module owns ONLY the operator-facing seam that transfers v5 (legacy)
knowledge responsibility onto the isolated v2 pipeline, and the reversible
park/unpark of live v2 work rows. It is deliberately small and body-free:

* :func:`prepare` is a *pure* deterministic reader. It validates the operator
  request against the authoritative source bytes, reconstructs a corrective
  no-op from its trusted completion audit, and returns a body-free plan whose
  ``plan_sha`` binds every input/owner/version/text digest, parent identity and
  read-only card hint. It performs no writes, no model calls, no queue reset.
* :func:`apply` is the one durable transition. Under the existing engine lock
  and one transaction it recomputes the plan (exact canonical equality),
  inserts one new-protocol pending child per selected parent event, reassigns
  every ``knowledge_seen`` claim with an expected-owner CAS, marks the parents
  ``obsolete`` with an explicit responsibility-transferred reason, appends a
  body-free audit and writes a unique grant receipt. Any failure rolls the
  whole transaction back. Replaying an identical plan is a zero-write read.
* :func:`park` / :func:`unpark` pause and restore every live v2 row with an
  exact, body-free receipt so no old worker may advance a paused row and no
  repeat call performs a second write.

The operator intent carried in a request is a bounded *hint*, never a
cryptographic human authentication; the SHA-256 fields bind reviewed evidence
only. This module never makes a model call, never opens the network, never
touches schema, the daemon, dependencies or card bodies.
"""
from __future__ import annotations

import hashlib
import json
import time

from . import delta_routing

__all__ = ['prepare', 'apply', 'park', 'unpark', 'V2_PROTOCOL',
           'LEGACY_PROTOCOL', 'MAX_PARENTS', 'MAX_SOURCES', 'MAX_CARDS']

V2_PROTOCOL = delta_routing.V2_PROTOCOL
LEGACY_PROTOCOL = delta_routing.LEGACY_PROTOCOL

# The operator exception completion state (owned by ``delta_operator``). Parked
# rows only ever return to it when their recorded prior state WAS that state AND
# their real operator intent chain still validates; an ordinary committed batch
# still returns to generic ``publication_pending``.
_OPERATOR_STATE = 'operator_publication_pending'

MIGRATION_META_PREFIX = 'delta_migration_v1:'
PARK_META_PREFIX = 'delta_park_v1:'
PARENT_AUDIT = 'delta_migration_parent_v1'
CHILD_AUDIT = 'delta_migration_child_v1'
PARK_AUDIT = 'delta_park_v1'

# Hard, explicit bounds. Every list/field is capped so a malformed or hostile
# request cannot expand work without limit.
MAX_PARENTS = 100
MAX_SOURCES = 300
MAX_CARDS = 12
MAX_TEXT_CHARS = 32000
_SLUG_MAX = 128
_HEX_CHARS = frozenset('0123456789abcdef')

# The only legacy states a parent may be migrated from. Anything else (a live
# lease, a committed batch, a terminal done/obsolete row) is rejected instead
# of being silently skipped.
_UNCOMMITTED_STATES = ('failed', 'budget_wait', 'pending', 'retry', 'blocked')
_NOOP_MODE = 'correct_false_noop'
_UNCOMMITTED_MODE = 'uncommitted'


# --------------------------------------------------------------- digests

def _canon(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def _hash(obj):
    return hashlib.sha256(_canon(obj).encode()).hexdigest()


def _sha_text(text):
    return hashlib.sha256((text or '').encode()).hexdigest()


def _is_hex(value):
    return isinstance(value, str) and len(value) == 64 and all(c in _HEX_CHARS for c in value)


def _is_slug(value):
    if not isinstance(value, str) or not value or len(value) > _SLUG_MAX:
        return False
    # A bounded, filesystem/meta-safe slug: no colon (the meta key separator),
    # no whitespace, no NUL. This is a label, not a secret.
    return all(c not in ':\x00' and not c.isspace() for c in value)


def _as_int(value):
    return value if type(value) is int else None


def _reject_extra(obj, allowed, label):
    """Reject any key outside the exact allowed schema (unknown keys reject)."""
    extra = sorted(set(obj) - set(allowed))
    if extra:
        raise ValueError('%s has unknown keys: %s' % (label, extra))


# ------------------------------------------------------- request validation

def _validate_request(request):
    """Exact-shape validation; raises ValueError on any missing/mismatch.

    Returns a normalized, canonical request dict. A malformed or unsupported
    field is always rejected, never skipped.
    """
    if not isinstance(request, dict):
        raise ValueError('request must be an object')
    _reject_extra(request, ('grant_id', 'code_sha', 'acceptance_sha', 'parents',
                            'required_cards'), 'request')
    grant_id = request.get('grant_id')
    if not _is_slug(grant_id):
        raise ValueError('grant_id must be a nonempty bounded slug')
    code_sha = request.get('code_sha')
    acceptance_sha = request.get('acceptance_sha')
    if not _is_hex(code_sha):
        raise ValueError('code_sha must be a hex64 digest')
    if not _is_hex(acceptance_sha):
        raise ValueError('acceptance_sha must be a hex64 digest')

    parents = request.get('parents')
    if not isinstance(parents, list) or not parents or len(parents) > MAX_PARENTS:
        raise ValueError('parents must be a nonempty bounded list')
    norm_parents = []
    seen_ids = set()
    for entry in parents:
        if not isinstance(entry, dict):
            raise ValueError('parent entry must be an object')
        _reject_extra(entry, ('id', 'mode', 'review_sha'), 'parent entry')
        pid = entry.get('id')
        if not isinstance(pid, str) or not pid:
            raise ValueError('parent id must be a nonempty string')
        if pid in seen_ids:
            raise ValueError('parent ids must be unique')
        seen_ids.add(pid)
        mode = entry.get('mode')
        if mode not in (_UNCOMMITTED_MODE, _NOOP_MODE):
            raise ValueError('unsupported parent mode: %r' % (mode,))
        review_sha = entry.get('review_sha')
        if mode == _NOOP_MODE:
            if not _is_hex(review_sha):
                raise ValueError('review_sha (hex64) is required for correct_false_noop')
        else:
            if review_sha is not None:
                raise ValueError('review_sha is only valid for correct_false_noop')
        norm_parents.append({'id': pid, 'mode': mode,
                             'review_sha': review_sha if mode == _NOOP_MODE else None})

    cards = request.get('required_cards')
    if cards is None:
        cards = {}
    if not isinstance(cards, dict):
        raise ValueError('required_cards must be a mapping')
    norm_cards = {}
    for source_id, ids in cards.items():
        if not isinstance(source_id, str) or not source_id:
            raise ValueError('required_cards keys must be nonempty strings')
        if not isinstance(ids, list):
            raise ValueError('required_cards values must be lists')
        if len(ids) > MAX_CARDS:
            raise ValueError('required_cards exceeds the per-source cap')
        seen = set()
        clean = []
        for cid in ids:
            if not isinstance(cid, str) or not cid:
                raise ValueError('card id must be a nonempty string')
            if cid in seen:
                raise ValueError('required_cards ids must be unique per source')
            seen.add(cid)
            clean.append(cid)
        norm_cards[source_id] = clean

    return {
        'grant_id': grant_id,
        'code_sha': code_sha,
        'acceptance_sha': acceptance_sha,
        'parents': sorted(norm_parents, key=lambda p: p['id']),
        'required_cards': {k: norm_cards[k] for k in sorted(norm_cards)},
    }


# --------------------------------------------------- source part validation

def _part_signature(part):
    """Body-free identity/digest signature of one authoritative source part."""
    return {
        'source_id': part.get('source_id'),
        'version': part.get('version'),
        'part': part.get('part'),
        'role': part.get('role'),
        'kind': part.get('kind'),
        'start': part.get('start'),
        'end': part.get('end'),
        'source_length': part.get('source_length'),
        'partial': bool(part.get('partial')),
        'sha256': _sha_text(part.get('text')),
    }


_INPUT_KEYS = ('source_id', 'version', 'part', 'role', 'kind', 'start', 'end',
               'source_length', 'partial', 'text', 'sha256')
_INT_INPUT_KEYS = ('version', 'part', 'start', 'end', 'source_length')


def _validate_inputs(evo, provided, authoritative):
    """Every provided input must exactly match an authoritative part.

    ``provided`` is a bounded list of expected identity/type/bounds/text parts;
    ``authoritative`` is the real ``evo._source_parts`` list for the current
    version. Any missing, extra, reordered or mismatched part is rejected. The
    provided part's own ``text`` field must hash to the authoritative part's
    text; an optional ``sha256`` field, when present, must match too.

    The outer schema is exact (unknown keys reject), integer identity/bounds
    fields must be true ints (never bool), and ``partial`` must be a real bool
    so a 0/1 integer is never silently coerced to a boolean.
    """
    if not isinstance(provided, list) or not provided:
        raise ValueError('inputs must be a nonempty list')
    if len(provided) != len(authoritative):
        raise ValueError('inputs must match every authoritative part')
    for index, item in enumerate(provided):
        if not isinstance(item, dict):
            raise ValueError('input part must be an object')
        _reject_extra(item, _INPUT_KEYS, 'input part')
        real = authoritative[index]
        real_sha = _sha_text(real.get('text'))
        for key in ('source_id', 'role', 'kind'):
            if item.get(key) != real.get(key):
                raise ValueError('input part %s mismatch' % key)
        for key in _INT_INPUT_KEYS:
            if type(item.get(key)) is not int or item.get(key) != real.get(key):
                raise ValueError('input part %s mismatch' % key)
        if type(item.get('partial')) is not bool:
            raise ValueError('input part partial must be a bool')
        if item.get('partial') != bool(real.get('partial')):
            raise ValueError('input part partial mismatch')
        text = item.get('text')
        if not isinstance(text, str) or _sha_text(text) != real_sha:
            raise ValueError('input part text does not match source')
        if 'sha256' in item and item['sha256'] != real_sha:
            raise ValueError('input part text does not match source')
    return [_part_signature(p) for p in authoritative]


def _load_parts(evo, source, raw_inputs):
    """Parse stored JSON inputs (a raw string) back to a list of dicts."""
    if isinstance(raw_inputs, list):
        return raw_inputs
    if isinstance(raw_inputs, str):
        try:
            value = json.loads(raw_inputs or '[]')
        except ValueError:
            raise ValueError('stored inputs malformed')
        return value if isinstance(value, list) else []
    return []


def _source_guard(evo, source_id):
    """Return the live source dict or raise on any unavailability mismatch."""
    if not isinstance(source_id, str) or not source_id:
        raise ValueError('source id must be a nonempty string')
    if not evo.engine.source_available(source_id):
        raise ValueError('source unavailable: %s' % source_id)
    try:
        source = evo.engine.store.source(source_id)
    except (KeyError, ValueError, OSError):
        raise ValueError('source missing: %s' % source_id)
    return source


def _check_source_identity(source, project, session):
    """The source must belong to the parent's project and canonical session.

    A legacy row stores the real session (never the legacy ``documents`` label),
    so the canonical source session ``(source.session or 'documents')`` must
    equal the row session exactly for both migration modes.
    """
    if source.get('project') != project:
        raise ValueError('source project does not match parent project')
    canonical_session = source.get('session') or 'documents'
    if canonical_session != session:
        raise ValueError('source session does not match parent session')


def _source_eligible(evo, source):
    """A v2-eligible event source: role/kind/session/length and full window."""
    if source.get('role') not in ('user', 'assistant'):
        raise ValueError('source role unsupported')
    if source.get('kind') != 'event':
        raise ValueError('source kind must be event')
    session = source.get('session')
    if session in delta_routing._SYNTHETIC_SESSIONS:
        raise ValueError('synthetic session not migratable')
    text = source.get('text')
    if not isinstance(text, str) or not (1 <= len(text) <= MAX_TEXT_CHARS):
        raise ValueError('source length out of bounds')
    return text


# ----------------------------------------------------- uncommitted parent

def _validate_uncommitted_parent(evo, parent):
    work_id = parent['id']
    row = evo.db.execute('SELECT * FROM knowledge_work WHERE id=?', (work_id,)).fetchone()
    if row is None:
        raise ValueError('parent work missing: %s' % work_id)
    if row['pipeline_protocol'] != LEGACY_PROTOCOL:
        raise ValueError('parent is not legacy protocol: %s' % work_id)
    if row['state'] not in _UNCOMMITTED_STATES:
        raise ValueError('parent state not migratable: %s' % row['state'])
    if evo.db.execute('SELECT 1 FROM knowledge_batches WHERE batch_id=?', (work_id,)).fetchone():
        raise ValueError('parent already has a committed batch')
    if (row['lease_until'] or 0) > time.time() and row['lease_token']:
        raise ValueError('parent has a live lease')

    parts = _load_parts(evo, row['project'], row['inputs'])
    if not parts:
        raise ValueError('parent inputs empty')
    if len({p.get('source_id') for p in parts if isinstance(p, dict)}) > MAX_SOURCES:
        raise ValueError('parent inputs exceed the source bound')
    # Every input's source must be available, CURRENT, and match exactly.
    grouped = {}
    for part in parts:
        if not isinstance(part, dict):
            raise ValueError('parent input part malformed')
        sid = part.get('source_id')
        source = _source_guard(evo, sid)
        _check_source_identity(source, row['project'], row['session'])
        text = _source_eligible(evo, source)
        if part.get('version') != source['current_version']:
            raise ValueError('parent input version is not current')
        grouped.setdefault(sid, (source, text))
    checked = []
    for sid, (source, text) in grouped.items():
        authoritative = list(evo._source_parts(source))
        owned = [p for p in parts if p.get('source_id') == sid]
        _validate_inputs(evo, owned, authoritative)
        checked.append((source, authoritative))
    return {'work_id': work_id, 'state': row['state'],
            'project': row['project'], 'session': row['session'],
            'sources': checked, 'inputs_sha': _sha_text(_canon(parts)),
            'raw_inputs_sha': _sha_text(row['inputs'] or ''),
            'inputs': parts}


# ------------------------------------------------ corrective no-op parent

def _completion_audit(evo, work_id):
    rows = evo.db.execute(
        "SELECT error FROM knowledge_failures WHERE work_id=? AND state='completion'"
        " ORDER BY created_at", (work_id,)).fetchall()
    if len(rows) != 1:
        raise ValueError('no-op parent must have exactly one completion audit')
    raw = rows[0]['error']
    try:
        audit = json.loads(raw)
    except (ValueError, TypeError):
        raise ValueError('completion audit malformed')
    if not isinstance(audit, dict):
        raise ValueError('completion audit malformed')
    return audit, raw


def _validate_noop_parent(evo, parent):
    work_id = parent['id']
    row = evo.db.execute('SELECT * FROM knowledge_work WHERE id=?', (work_id,)).fetchone()
    if row is None:
        raise ValueError('parent work missing: %s' % work_id)
    if row['pipeline_protocol'] != LEGACY_PROTOCOL:
        raise ValueError('parent is not legacy protocol: %s' % work_id)
    if row['state'] != 'done':
        raise ValueError('correct_false_noop parent must be done')

    batch = evo.db.execute('SELECT * FROM knowledge_batches WHERE batch_id=?',
                           (work_id,)).fetchone()
    if batch is None:
        raise ValueError('no-op parent has no batch')
    try:
        changed = json.loads(batch['changed_ids'])
    except (ValueError, TypeError):
        raise ValueError('no-op batch changed_ids malformed')
    if type(changed) is not list or changed != []:
        raise ValueError('no-op parent batch is not empty')
    if batch['project'] != row['project']:
        raise ValueError('no-op batch project does not match parent')

    audit, raw_audit = _completion_audit(evo, work_id)
    count = audit.get('operation_count')
    if type(count) is not int or count != 0:
        raise ValueError('completion audit operation_count is not zero')
    if type(audit.get('changed_ids')) is not list or audit.get('changed_ids') != []:
        raise ValueError('completion audit changed_ids is not empty')
    claimed = audit.get('inputs')
    if not isinstance(claimed, list):
        raise ValueError('completion audit inputs malformed')
    if len(claimed) > MAX_SOURCES * 8:
        raise ValueError('completion audit inputs exceeds bound')

    # ``knowledge_seen`` claims name the trusted (source, version, part) set the
    # old completion owned; a real source part must still exist for each.
    claims = evo.db.execute(
        'SELECT source_id,version,part FROM knowledge_seen WHERE work_id=?'
        ' ORDER BY source_id,version,part', (work_id,)).fetchall()
    claim_sig = {_claim_key(c) for c in claims}
    audit_sig = set()
    for entry in claimed:
        if not isinstance(entry, dict):
            raise ValueError('completion audit input entry malformed')
        sid = entry.get('source_id')
        version = entry.get('version')
        part = entry.get('part')
        sha = entry.get('sha256')
        if not isinstance(sid, str) or not sid:
            raise ValueError('completion audit source id malformed')
        if type(version) is not int or type(part) is not int:
            raise ValueError('completion audit version/part malformed')
        if not _is_hex(sha):
            raise ValueError('completion audit sha256 malformed')
        if (sid, version, part) in audit_sig:
            raise ValueError('completion audit has duplicate input tuples')
        audit_sig.add((sid, version, part))
        source = _source_guard(evo, sid)
        _check_source_identity(source, row['project'], row['session'])
        _source_eligible(evo, source)
        if version != source['current_version']:
            raise ValueError('completion audit version is not current')
        authoritative = list(evo._source_parts(source))
        if part < 0 or part >= len(authoritative):
            raise ValueError('completion audit part out of range')
        real_sha = _sha_text(authoritative[part]['text'])
        if real_sha != sha:
            raise ValueError('completion audit text sha mismatch')

    if audit_sig != claim_sig or not claim_sig:
        raise ValueError('completion audit does not match owned claims')
    # Rebuild the exact source part list in audit claim order for reconstruction.
    rebuilt = []
    grouped = {}
    for entry in sorted(claimed, key=lambda e: (e['source_id'], e['version'], e['part'])):
        source = _source_guard(evo, entry['source_id'])
        _source_eligible(evo, source)
        authoritative = list(evo._source_parts(source))
        part = authoritative[entry['part']]
        rebuilt.append(part)
        grouped.setdefault(entry['source_id'], [source, []])[1].append(part)
    batch_digest = _hash({'op_hash': batch['op_hash'], 'review_hash': batch['review_hash'],
                          'changed_ids': changed, 'project': batch['project']})
    return {'work_id': work_id, 'state': row['state'], 'project': row['project'],
            'session': row['session'], 'kind': 'no_op',
            'audit_raw_sha': _sha_text(raw_audit), 'batch_digest': batch_digest,
            'raw_inputs_sha': _sha_text(row['inputs'] or ''),
            'review_sha': parent['review_sha'], 'claims': sorted(claim_sig),
            'inputs': rebuilt,
            'sources': [(grouped[sid][0], grouped[sid][1]) for sid in sorted(grouped)],
            'inputs_sha': _sha_text(_canon(sorted(
                (_part_signature(p) for p in rebuilt),
                key=lambda s: (s['source_id'], s['part']))))}


def _claim_key(row):
    return (row['source_id'], row['version'], row['part'])


# ---------------------------------------------------------- plan assembly

def _owner_map(evo):
    """Current ``knowledge_seen`` ownership as a dict of claim -> work_id."""
    out = {}
    for row in evo.db.execute('SELECT source_id,version,part,work_id FROM knowledge_seen'):
        out[(row['source_id'], row['version'], row['part'])] = row['work_id']
    return out


def _child_id(project, session, source_id, version):
    return _hash([V2_PROTOCOL, project, session, source_id, version])[:32]


def _validate_required_cards(evo, source_projects, required_cards):
    """Resolve authorized read hints to their actual complete card objects.

    Only current authorized card IDs may be named; every hint is bound by its
    current version and body SHA. Hints confer no extra rights and never revive
    a retired card's privileges; they are read hints only. The authorization
    catalog is read against the *owning* project of each source.
    """
    catalogs = {}
    bounds = {}
    for source_id in sorted(required_cards):
        if source_id not in source_projects:
            raise ValueError('required_cards names an unselected source')
        project = source_projects[source_id]
        if project not in catalogs:
            catalogs[project] = {c['id']: c for c in evo._authorized_catalog(project)}
        by_id = catalogs[project]
        rows = []
        for cid in required_cards[source_id]:
            card = by_id.get(cid)
            if card is None:
                raise ValueError('required_cards id is not authorized: %s' % cid)
            rows.append({'id': card['id'], 'version': card['version'],
                         'body_sha': card.get('body_sha256') or _sha_text(card.get('body'))})
        bounds[source_id] = rows
    return bounds


def _parent_binding(evo, parent):
    if parent['mode'] == _NOOP_MODE:
        return _validate_noop_parent(evo, parent)
    return _validate_uncommitted_parent(evo, parent)


def prepare(evo, request):
    """Return a body-free deterministic migration plan; performs no writes.

    The plan binds every parent identity/state/protocol/input digest, the
    completion audit and claims for a corrective no-op, every source version
    and per-part text digest, the owner map, the read-only card hints (including
    an explicit empty hint set) and the deterministic per-event child IDs. There
    are no wall-clock timestamps, so a replay or re-prepare is byte-identical.
    """
    norm = _validate_request(request)
    parents = []
    source_ids = set()
    for parent in norm['parents']:
        binding = _parent_binding(evo, parent)
        parents.append(binding)
        for source, parts in binding['sources']:
            source_ids.add(source['source_id'])

    if len(source_ids) > MAX_SOURCES:
        raise ValueError('selected sources exceed the source bound')

    # Every actual part of every selected source must be owned by exactly one
    # selected parent, and every parent claim must correspond to a selected
    # validated input. No partial outside owner, no hidden extra claim.
    owners = _owner_map(evo)
    selected_work = {p['work_id'] for p in parents}
    expected_claim_owner = {}
    for binding in parents:
        for source, parts in binding['sources']:
            sid = source['source_id']
            for part in parts:
                claim = (sid, source['current_version'], part['part'])
                prior_owner = expected_claim_owner.get(claim)
                if prior_owner is not None and prior_owner != binding['work_id']:
                    raise ValueError('one source part claimed by two parents')
                expected_claim_owner[claim] = binding['work_id']
    for claim, owner in expected_claim_owner.items():
        live = owners.get(claim)
        if live is None:
            raise ValueError('source part has no live owner')
        if live not in selected_work:
            raise ValueError('source part owned outside selected parents')
        if live != owner:
            raise ValueError('source part owner mismatch')
    for claim, owner in owners.items():
        if owner in selected_work and claim not in expected_claim_owner:
            raise ValueError('parent has a hidden claim not in its inputs')

    if len(expected_claim_owner) > MAX_SOURCES * 32:
        raise ValueError('selected sources exceed bound')
    if len(source_ids) > MAX_SOURCES:
        raise ValueError('actual source count exceeds the source bound')

    card_bounds = _validate_required_cards(
        evo,
        {s[0]['source_id']: s[0]['project'] for p in parents for s in p['sources']},
        norm['required_cards'])

    # One child per full event, ordered stably by capture time then source ID.
    events = {}
    for binding in parents:
        for source, parts in binding['sources']:
            sid = source['source_id']
            events[sid] = {'source': source, 'parts': parts,
                           'parent': binding['work_id'],
                           'created_at': source.get('created_at')}
    ordered = sorted(events, key=lambda sid: ((events[sid]['created_at'] or ''),
                                              sid))

    children = []
    for sid in ordered:
        event = events[sid]
        source = event['source']
        cid = _child_id(source['project'], source['session'], sid,
                        source['current_version'])
        owner_map = {str(p['part']): event['parent'] for p in event['parts']}
        children.append({
            'child_id': cid,
            'parent': event['parent'],
            'project': source['project'],
            'session': source['session'],
            'source_id': sid,
            'version': source['current_version'],
            'text_sha': _sha_text(source['text']),
            'parts': [_part_signature(p) for p in event['parts']],
            'owner_map': owner_map,
            'required_cards': card_bounds.get(sid, []),
        })

    # Deterministic child collisions are rejected at prepare time: a child id
    # that already names a work row or a committed batch can never be inserted,
    # so the plan must be refused before an operator reviews it (not deferred
    # to apply). Collisions among selected children are impossible by
    # construction but are checked all the same.
    seen_children = set()
    for child in children:
        cid = child['child_id']
        if cid in seen_children:
            raise ValueError('duplicate child id in plan')
        seen_children.add(cid)
        if evo.db.execute('SELECT 1 FROM knowledge_work WHERE id=?', (cid,)).fetchone():
            raise ValueError('child already exists: %s' % cid)
        if evo.db.execute('SELECT 1 FROM knowledge_batches WHERE batch_id=?', (cid,)).fetchone():
            raise ValueError('child id collides with a batch')

    body = {
        'request': norm,
        'parents': [
            {'work_id': p['work_id'], 'state': p['state'],
             'project': p['project'], 'session': p['session'],
             'kind': p.get('kind', 'uncommitted'),
             'inputs_sha': p['inputs_sha'],
             'raw_inputs_sha': p['raw_inputs_sha'],
             'audit_raw_sha': p.get('audit_raw_sha'),
             'batch_digest': p.get('batch_digest'),
             'review_sha': p.get('review_sha'),
             'claims': [list(c) for c in p.get('claims', [])],
             'sources': [{'source_id': s['source_id'],
                          'version': s['current_version'],
                          'text_sha': _sha_text(s['text']),
                          'parts': [_part_signature(x) for x in parts]}
                         for s, parts in p['sources']]}
            for p in parents],
        'children': children,
        'required_cards': {k: card_bounds[k] for k in sorted(card_bounds)},
    }
    plan = dict(body)
    plan['plan_sha'] = _hash(body)
    _validate_plan_schema(plan)
    return plan


def _validate_plan_schema(plan):
    if not isinstance(plan, dict):
        raise ValueError('plan must be an object')
    for key in ('request', 'parents', 'children', 'plan_sha', 'required_cards'):
        if key not in plan:
            raise ValueError('plan missing %s' % key)
    _reject_extra(plan, ('request', 'parents', 'children', 'plan_sha',
                         'required_cards'), 'plan')
    if not _is_hex(plan['plan_sha']):
        raise ValueError('plan_sha malformed')
    body = {k: v for k, v in plan.items() if k != 'plan_sha'}
    if _hash(body) != plan['plan_sha']:
        raise ValueError('plan self-hash mismatch')


# ---------------------------------------------------------------- apply

def _receipt_for(plan):
    children = [{'source_id': c['source_id'], 'child_id': c['child_id'],
                 'parent': c['parent'], 'version': c['version']}
                for c in plan['children']]
    return {
        'grant_id': plan['request']['grant_id'],
        'request': plan['request'],
        'plan_sha': plan['plan_sha'],
        'code_sha': plan['request']['code_sha'],
        'acceptance_sha': plan['request']['acceptance_sha'],
        'parents': [p['work_id'] for p in plan['parents']],
        'children': children,
        'responsibility_transferred': True,
        'semantic_completion': False,
        'replayed': False,
        # The full body-free prepared plan is retained so a caller crash after
        # commit recovers the exact reviewed mapping from the authoritative
        # original receipt alone (never a re-prepare, which could drift).
        'plan': plan,
    }


def apply(evo, plan):
    """Durably transfer responsibility from legacy parents to v2 children.

    Runs under the existing engine lock and one transaction. A repeated
    identical plan returns the stored receipt with ``replayed=True`` and zero
    writes, even if the children have since evolved. Any failure rolls back.
    """
    _validate_plan_schema(plan)
    grant_key = MIGRATION_META_PREFIX + plan['request']['grant_id']

    with evo.engine.locked():
        existing = evo.db.execute('SELECT value FROM meta WHERE key=?',
                                  (grant_key,)).fetchone()
        if existing is not None:
            try:
                stored = json.loads(existing['value'])
            except ValueError:
                raise ValueError('stored migration receipt malformed')
            if stored.get('plan_sha') != plan['plan_sha']:
                raise ValueError('grant_id already bound to a different plan')
            if _canon(stored.get('request')) != _canon(plan['request']):
                raise ValueError('grant_id request mismatch')
            replay = dict(stored)
            replay['replayed'] = True
            return replay

        with evo.engine.transaction():
            # Recompute under the lock/tx; the canonical plan must be identical.
            fresh = prepare(evo, plan['request'])
            if _canon(fresh) != _canon(plan):
                raise ValueError('plan is stale or not canonical')

            owners = _owner_map(evo)
            for child in fresh['children']:
                if evo.db.execute('SELECT 1 FROM knowledge_work WHERE id=?',
                                  (child['child_id'],)).fetchone():
                    raise ValueError('child already exists: %s' % child['child_id'])
                if evo.db.execute('SELECT 1 FROM knowledge_batches WHERE batch_id=?',
                                  (child['child_id'],)).fetchone():
                    raise ValueError('child id collides with a batch')

            # Insert children as pending v2 rows with their source-derived
            # inputs, then reassign ownership with an expected-owner CAS. One
            # real epoch timestamp is captured for the whole apply; child rows
            # are ordered by a tiny monotonic offset so their audit order is
            # stable without inventing extra clock reads.
            now = time.time()
            for index, child in enumerate(fresh['children']):
                source = _source_guard(evo, child['source_id'])
                parts = list(evo._source_parts(source))
                created_at = now + index * 1e-6
                evo.db.execute(
                    "INSERT INTO knowledge_work"
                    "(id,project,session,inputs,state,created_at,pipeline_protocol)"
                    " VALUES(?,?,?,?,'pending',?,?)",
                    (child['child_id'], child['project'], child['session'],
                     _canon(parts), created_at, V2_PROTOCOL))
                for part in parts:
                    claim = (child['source_id'], child['version'], part['part'])
                    expected = owners.get(claim)
                    if expected is None:
                        raise ValueError('owner claim missing at apply')
                    cur = evo.db.execute(
                        'UPDATE knowledge_seen SET work_id=? WHERE source_id=?'
                        ' AND version=? AND part=? AND work_id=?',
                        (child['child_id'], claim[0], claim[1], claim[2], expected))
                    if cur.rowcount != 1:
                        raise ValueError('owner reassignment CAS failed')

            # Mark parents obsolete with a bounded, body-free reason and clear
            # only the lease. Old bundle/inputs/proposal/review/calls/audits/
            # batches/attempts are preserved verbatim; dead payloads are swept
            # later by the bounded reclaim step, never in this audit write.
            for parent in fresh['parents']:
                reason = ('responsibility transferred: %s'
                          % fresh['request']['grant_id'])[:200]
                cur = evo.db.execute(
                    "UPDATE knowledge_work SET state='obsolete',error=?,"
                    " lease_token=NULL,lease_until=0 WHERE id=?"
                    " AND pipeline_protocol=?",
                    (reason, parent['work_id'], LEGACY_PROTOCOL))
                if cur.rowcount != 1:
                    raise ValueError('parent could not be obsoleted')
                evo.db.execute(
                    'INSERT INTO knowledge_failures(work_id,state,error,created_at)'
                    ' VALUES(?,?,?,?)',
                    (parent['work_id'], PARENT_AUDIT,
                     _canon({'grant_id': fresh['request']['grant_id'],
                             'plan_sha': fresh['plan_sha'],
                             'child_ids': [c['child_id'] for c in fresh['children']
                                           if c['parent'] == parent['work_id']]}),
                     now))

            # Child audit state is body-free: grant/plan/parent/source identity
            # and the reviewed card hints only.
            for index, child in enumerate(fresh['children']):
                evo.db.execute(
                    'INSERT INTO knowledge_failures(work_id,state,error,created_at)'
                    ' VALUES(?,?,?,?)',
                    (child['child_id'], CHILD_AUDIT,
                     _canon({'grant_id': fresh['request']['grant_id'],
                             'plan_sha': fresh['plan_sha'],
                             'parent': child['parent'],
                             'source_id': child['source_id'],
                             'version': child['version'],
                             'text_sha': child['text_sha'],
                             'work_id': child['child_id'],
                             'project': child['project'],
                             'required_cards': child['required_cards']}),
                     now + index * 1e-6))

            receipt = _receipt_for(fresh)
            # One-shot marker: a plain INSERT, never OR REPLACE. The lock and
            # the existence check above already serialize; a race would raise
            # here rather than silently overwrite a committed receipt.
            evo.db.execute('INSERT INTO meta(key,value) VALUES(?,?)',
                           (grant_key, _canon(receipt)))
        result = dict(receipt)
        result['replayed'] = False
        return result


# ------------------------------------------------------------ park/unpark

def _operator_pending_state(evo, work_id):
    """Validate a REAL operator-pending chain, or RAISE.

    A row whose recorded prior state was ``operator_publication_pending`` is only
    restored to that state when its REAL operator intent chain still validates
    under the existing lock: a unique matching operator meta intent + intent
    audit and a real committed batch that agree. A missing/malformed/ambiguous/
    mismatched chain is NOT silently downgraded to generic
    ``publication_pending`` (which would let an ordinary resume publish a
    committed operator batch); it raises so the caller leaves the row
    ``protocol_paused`` untouched.
    """
    from . import delta_operator
    meta = delta_operator.validate_operator_intent(evo, work_id)
    if meta.get('work_id') != work_id:
        raise ValueError('operator intent work id mismatch for %s' % work_id)
    return _OPERATOR_STATE


def _park_key(park_id):
    if not _is_slug(park_id):
        raise ValueError('park_id must be a nonempty bounded slug')
    return PARK_META_PREFIX + park_id


def _cache_sha(rows):
    """Stable, body-free fingerprint of the paused rows' cache/protocol state.

    Binds each row's id, protocol, state, next_at, was_committed flag and the
    exact digest of its stored inputs/cache columns, so a later drift in any of
    them (a stale worker, an edit) is detectable at unpark without embedding the
    raw values.
    """
    entries = []
    for row in sorted(rows, key=lambda r: r['id']):
        entries.append({
            'id': row['id'],
            'pipeline_protocol': row['pipeline_protocol'],
            'state': row['state'],
            'next_at': row['next_at'],
            'attempts': row['attempts'],
            'inputs_sha': _sha_text(row['inputs'] or ''),
            'bundle_sha': _sha_text(row['bundle'] or ''),
            'proposal_sha': _sha_text(row['proposal'] or ''),
            'review_sha': _sha_text(row['review'] or ''),
            'has_batch': bool(row['has_batch']),
            'lease_token': row['lease_token'],
            'lease_until': row['lease_until'],
        })
    return _hash(entries)


def _parkable_rows(evo):
    out = []
    for row in evo.db.execute(
            "SELECT * FROM knowledge_work WHERE pipeline_protocol=?"
            " AND state NOT IN ('done','obsolete','protocol_paused')", (V2_PROTOCOL,)):
        item = dict(row)
        item['has_batch'] = bool(evo.db.execute(
            'SELECT 1 FROM knowledge_batches WHERE batch_id=?', (row['id'],)).fetchone())
        out.append(item)
    return out


def park(evo, park_id):
    """Pause every live v2 row under the lock, reversibly and idempotently.

    Inputs, cache, batches, calls, attempts and ownership are preserved; only
    state (``protocol_paused``) and the lease are changed. The receipt records
    each row's prior state, ``next_at``, committed flag, identity, protocol and a
    stable cache digest, so no old worker may advance a paused row.
    """
    key = _park_key(park_id)
    with evo.engine.locked():
        existing = evo.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        if existing is not None:
            try:
                stored = json.loads(existing['value'])
            except ValueError:
                raise ValueError('stored park receipt malformed')
            replay = dict(stored)
            replay['replayed'] = True
            return replay

        with evo.engine.transaction():
            now = time.time()
            rows = _parkable_rows(evo)
            for index, row in enumerate(rows):
                # The short explicit pause error overwrites any lingering prior
                # error, so old legacy recovery code (which keys off the row
                # error text) cannot act on a stale reason while the row is
                # parked. Only state, error and the lease change here.
                evo.db.execute(
                    "UPDATE knowledge_work SET state='protocol_paused',"
                    " error='protocol paused',"
                    " lease_token=NULL,lease_until=0 WHERE id=?"
                    " AND pipeline_protocol=? AND state=?",
                    (row['id'], V2_PROTOCOL, row['state']))
                evo.db.execute(
                    'INSERT INTO knowledge_failures(work_id,state,error,created_at)'
                    ' VALUES(?,?,?,?)',
                    (row['id'], PARK_AUDIT,
                     _canon({'park_id': park_id, 'restore_state': row['state'],
                             'restore_next_at': row['next_at'],
                             'was_committed': row['has_batch']}),
                     now + index * 1e-6))

            # The cache digest describes the POST-park (paused) row state, which
            # is exactly what unpark must find unchanged: state, next_at,
            # attempts, the cache columns, batch presence and the cleared lease.
            paused = []
            for row in rows:
                item = dict(row)
                item['state'] = 'protocol_paused'
                item['lease_token'] = None
                item['lease_until'] = 0.0
                paused.append(item)

            receipt = {
                'park_id': park_id,
                'cache_sha': _cache_sha(paused),
                'rows': [{'id': r['id'], 'pipeline_protocol': r['pipeline_protocol'],
                          'prior_state': r['state'], 'next_at': r['next_at'],
                          'was_committed': r['has_batch']} for r in rows],
                'count': len(rows),
                'replayed': False,
            }
            evo.db.execute('INSERT INTO meta(key,value) VALUES(?,?)',
                           (key, _canon(receipt)))
        return receipt


def unpark(evo, park_id):
    """Restore exactly the rows saved by :func:`park`, all-or-none.

    Requires the exact parked receipt, unchanged caches/protocol/batch presence
    and still-paused rows. Restores the prior state (``running`` becomes
    ``pending``) and prior ``next_at`` with the same attempts/calls, resets no
    budget and restores committed work to ``publication_pending``. A repeated
    unpark is a zero-write replay. Unrelated or newly paused rows are untouched.
    """
    key = _park_key(park_id)
    with evo.engine.locked():
        park_row = evo.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        if park_row is None:
            raise ValueError('no park receipt for %s' % park_id)
        try:
            parked = json.loads(park_row['value'])
        except ValueError:
            raise ValueError('stored park receipt malformed')

        unpark_key = key + ':unparked'
        done = evo.db.execute('SELECT value FROM meta WHERE key=?',
                              (unpark_key,)).fetchone()
        if done is not None:
            try:
                stored = json.loads(done['value'])
            except ValueError:
                raise ValueError('stored unpark receipt malformed')
            replay = dict(stored)
            replay['replayed'] = True
            return replay

        with evo.engine.transaction():
            saved = {r['id']: r for r in parked['rows']}
            live = []
            for rid in sorted(saved):
                row = evo.db.execute('SELECT * FROM knowledge_work WHERE id=?',
                                     (rid,)).fetchone()
                if row is None:
                    raise ValueError('paused row missing: %s' % rid)
                if row['pipeline_protocol'] != saved[rid]['pipeline_protocol']:
                    raise ValueError('paused row protocol changed: %s' % rid)
                if row['state'] != 'protocol_paused':
                    raise ValueError('row is no longer paused: %s' % rid)
                item = dict(row)
                item['has_batch'] = bool(evo.db.execute(
                    'SELECT 1 FROM knowledge_batches WHERE batch_id=?', (rid,)).fetchone())
                # No normalization: any changed live lease (a stray worker that
                # re-acquired the row) must fail the digest comparison below,
                # never be silently absorbed.
                live.append(item)

            if _cache_sha(live) != parked['cache_sha']:
                raise ValueError('paused cache drift detected')

            # Compute EVERY row's restore state BEFORE any write. In particular
            # a recorded operator-pending prior state must validate its real
            # operator chain here (which may RAISE); because nothing has been
            # written yet, a failure leaves every row protocol_paused and the
            # transaction rolls back, so no row is ever silently treated as an
            # ordinary committed batch.
            restore_plan = []
            for row in live:
                prior = saved[row['id']]
                if prior['was_committed']:
                    # A committed batch is normally publication_pending -- EXCEPT
                    # a real operator exception completion, which must return to
                    # its own ``operator_publication_pending`` state so the
                    # original operator intent (never a generic v5/v2 resume) is
                    # preserved. The prior state itself is the authoritative
                    # signal recorded at park time; a claimed operator state with
                    # no real chain raises above.
                    restored = (_operator_pending_state(evo, row['id'])
                                if prior.get('prior_state') == _OPERATOR_STATE
                                else 'publication_pending')
                elif prior['prior_state'] == 'running':
                    restored = 'pending'
                else:
                    restored = prior['prior_state']
                restore_plan.append((row['id'], restored))

            now = time.time()
            for index, (rid, restored) in enumerate(restore_plan):
                prior = saved[rid]
                # Ordinary restores clear the error; a restored ``failed`` row
                # keeps a short reason pointing at its preserved prior audit
                # instead of an ambiguous NULL. Previous state/attempts/next_at
                # semantics and the derived caps are otherwise unchanged.
                error = ('restored failure; inspect prior audit'
                         if restored == 'failed' else None)
                cur = evo.db.execute(
                    "UPDATE knowledge_work SET state=?,next_at=?,error=?,"
                    " lease_token=NULL,lease_until=0 WHERE id=?"
                    " AND pipeline_protocol=? AND state='protocol_paused'",
                    (restored, prior['next_at'], error, rid, V2_PROTOCOL))
                if cur.rowcount != 1:
                    raise ValueError('paused row could not be restored: %s' % rid)
                evo.db.execute(
                    'INSERT INTO knowledge_failures(work_id,state,error,created_at)'
                    ' VALUES(?,?,?,?)',
                    (rid, PARK_AUDIT + '_restored',
                     _canon({'park_id': park_id, 'restored_state': restored}),
                     now + index * 1e-6))

            # The receipt records the ACTUAL restored state for every row, so a
            # replay can never report a state that was not really written.
            receipt = {
                'park_id': park_id,
                'restored': [{'id': rid, 'prior_state': saved[rid]['prior_state'],
                              'restored_state': restored}
                             for rid, restored in restore_plan],
                'count': len(live),
                'replayed': False,
            }
            evo.db.execute('INSERT INTO meta(key,value) VALUES(?,?)',
                           (unpark_key, _canon(receipt)))
        return receipt
