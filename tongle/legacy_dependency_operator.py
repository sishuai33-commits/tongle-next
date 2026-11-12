"""Explicit ROOT operator completion for FAILED legacy v5
knowledge-dependency batches.

A legacy (v5) work row whose inputs are complete assistant
``knowledge-dependency`` dependency notices has, by construction, no model
retry left: each notice only reports that a dependent target's basis version
changed. The ROOT operator may read the current FULL target and basis bodies
and attest that no semantic change is needed -- the only real fix is to
refresh the existing basis version links. This module performs exactly that
and nothing else:

* :func:`prepare` is a pure deterministic reader. It validates an explicit
  operator request, resolves ALL 1..8 fully owned, current assistant
  ``knowledge-dependency`` event sources (each notice strictly parsed),
  requires every notified basis id to be an EXISTING relation of the notified
  target and every target/basis to be an active authorized card, and returns a
  canonical plan binding the exact work snapshot/scope, every owned source
  identity/part, every notice->target->basis mapping, the full current target
  + basis bodies and the normalized update operations. It never creates a
  card, never uses the notification text as external factual evidence, makes
  no model call and performs no write.
* :func:`apply` is the one durable transition, under the existing engine lock.
  It resyncs manual files, recomputes the plan (exact canonical equality),
  calls the ORIGINAL ``KnowledgeStore.apply`` with the true ``ROOT_operator``
  origin, writes a body-free durable intent + intent audit, moves the row to
  ``legacy_operator_publication_pending`` (no lease), publishes via the
  existing files, and only after a confirmed publication writes the dedicated
  completion audit and ``done``. A publication failure leaves the row pending
  with its intent/cache; re-applying the same plan resumes WITHOUT a second
  ``store.apply``.
* :func:`verify_completed` re-proves the whole chain read-only.

The SHA-256 bindings are structural audit markers, NOT cryptographic identity
or semantic proof. ROOT owns the semantics; nothing here is labelled
model-reviewed. No scheduler, table, queue or daemon is created.
"""
from __future__ import annotations

import json
import time

from . import delta_migration as dm
from . import delta_operator as dop
from . import dependency_inputs

__all__ = [
    'prepare', 'apply', 'publish_one', 'resume_pending', 'verify_completed',
    'has_legacy_operator_completion', 'validate_legacy_operator_intent',
    'PROTOCOL', 'LEGACY_OPERATOR_STATE', 'LEGACY_OPERATOR_INTENT_AUDIT',
    'LEGACY_OPERATOR_COMPLETE_AUDIT', 'LEGACY_OPERATOR_META_PREFIX',
    'OPERATOR_ORIGIN', 'OPERATOR_REVIEW_IDENTITY', 'SCHEMA',
    'MAX_SOURCES', 'MAX_HUMAN_CHECKS', 'MAX_RATIONALE',
]

PROTOCOL = dm.LEGACY_PROTOCOL
OPERATOR_ORIGIN = dop.OPERATOR_ORIGIN
OPERATOR_REVIEW_IDENTITY = dop.OPERATOR_REVIEW_IDENTITY

LEGACY_OPERATOR_STATE = 'legacy_operator_publication_pending'
LEGACY_OPERATOR_INTENT_AUDIT = 'legacy_operator_intent_v1'
LEGACY_OPERATOR_COMPLETE_AUDIT = 'legacy_operator_complete_v1'
LEGACY_OPERATOR_META_PREFIX = 'legacy_operator_v1:'

SCHEMA = 1
MAX_SOURCES = 8
MAX_HUMAN_CHECKS = 12
MAX_RATIONALE = 2000

_UPDATE_REASON = (
    'ROOT operator: 依据卡版本已更新，仅刷新既有依赖版本链接，'
    '正文、证据、其余依据与人工编辑均按原样保留。')

_REQUEST_KEYS = ('grant_id', 'work_id', 'code_sha', 'acceptance_sha',
                 'review_sha', 'rationale', 'human_checks')

_BINDING_KEYS = (
    'grant_id', 'work_id', 'project', 'session', 'rationale_sha',
    'request_sha', 'plan_sha', 'code_sha', 'acceptance_sha', 'review_sha',
    'scope_fingerprint', 'sources', 'owned_inputs', 'notices', 'targets',
    'operations_sha',
)

_canon = dop._canon
_hash = dop._hash
_sha_text = dop._sha_text
_is_hex = dop._is_hex
_is_slug = dop._is_slug
_is_int = dop._is_int


def _call_ledger(evo, work_id):
    """Digest + count of the complete model call ledger for this work (read-only)."""
    rows = evo.db.execute(
        'SELECT * FROM knowledge_calls WHERE work_id=? ORDER BY id',
        (work_id,)).fetchall()
    entries = [dict(row) for row in rows]
    return {'count': len(entries), 'sha': _hash(entries)}


# ------------------------------------------------------ request validation

def _validate_request(request):
    if not isinstance(request, dict):
        raise ValueError('request must be an object')
    dop._reject_extra(request, _REQUEST_KEYS, 'request')
    grant_id = request.get('grant_id')
    if not _is_slug(grant_id):
        raise ValueError('grant_id must be a nonempty bounded slug')
    work_id = request.get('work_id')
    if not isinstance(work_id, str) or not work_id or len(work_id) > 128:
        raise ValueError('work_id must be a nonempty bounded string')
    for name in ('code_sha', 'acceptance_sha', 'review_sha'):
        if not _is_hex(request.get(name)):
            raise ValueError('%s must be a hex64 digest' % name)
    rationale = request.get('rationale')
    if not isinstance(rationale, str) or not (1 <= len(rationale) <= MAX_RATIONALE):
        raise ValueError('rationale must be a nonempty bounded string')
    human_checks = request.get('human_checks')
    if human_checks is None:
        human_checks = []
    if not isinstance(human_checks, list) or len(human_checks) > MAX_HUMAN_CHECKS:
        raise ValueError('human_checks must be a bounded list')
    for check in human_checks:
        if not isinstance(check, dict):
            raise ValueError('human_checks entries must be objects')
    return {
        'grant_id': grant_id,
        'work_id': work_id,
        'code_sha': request['code_sha'],
        'acceptance_sha': request['acceptance_sha'],
        'review_sha': request['review_sha'],
        'rationale': rationale,
        'human_checks': [dict(check) for check in human_checks],
    }


# ------------------------------------------------------ scope / ownership

def _validate_scope(evo, row):
    """Existing ``failed`` legacy v5 work only: no lease, no committed batch."""
    if row is None:
        raise ValueError('work missing')
    if row['pipeline_protocol'] != PROTOCOL:
        raise ValueError('work is not the legacy v5 protocol')
    if row['state'] != 'failed':
        raise ValueError('legacy operator completion requires a failed work row')
    if dop._active_lease(row):
        raise ValueError('work has a live lease')
    if dop._has_batch(evo, row['id']):
        raise ValueError('work already has a committed batch')


def _collect_owned_sources(evo, row):
    """Resolve ALL 1..8 fully owned, current, assistant event sources.

    Every distinct source named by the stored inputs is re-proved with the
    existing ``delta_operator._owned_event`` helper (one filtered row copy per
    source). A missing part, a partial/forged part, a duplicate part, a
    foreign owner, a superseded version or an unavailable source is refused.
    """
    parts = dop._load_inputs(row['inputs'])
    if not parts:
        raise ValueError('work inputs empty')
    order = []
    seen = set()
    seen_parts = set()
    for part in parts:
        if not isinstance(part, dict):
            raise ValueError('stored input part malformed')
        sid = part.get('source_id')
        if not isinstance(sid, str) or not sid:
            raise ValueError('stored input source id malformed')
        key = (sid, part.get('version'), part.get('part'))
        if key in seen_parts:
            raise ValueError('stored input part duplicated')
        seen_parts.add(key)
        if sid not in seen:
            seen.add(sid)
            order.append(sid)
    if not 1 <= len(order) <= MAX_SOURCES:
        raise ValueError('legacy operator requires 1..%d fully owned sources'
                         % MAX_SOURCES)
    out = []
    for sid in sorted(order):
        subset = [p for p in parts
                  if isinstance(p, dict) and p.get('source_id') == sid]
        fake = dict(row)
        fake['inputs'] = _canon(subset)
        owned = dop._owned_event(evo, fake)
        if owned['role'] != 'assistant' or owned['kind'] != 'event':
            raise ValueError('owned source must be an assistant event')
        if owned['session'] != dependency_inputs.DEPENDENCY_SESSION:
            raise ValueError('owned source must be a knowledge-dependency source')
        owned = dict(owned)
        owned['owned_inputs'] = [
            {'source_id': owned['source_id'], 'version': owned['version'],
             'part': p['part'], 'text_sha': p['sha256']}
            for p in owned['parts']]
        out.append(owned)
    _verify_seen_set(evo, row['id'], [p for source in out for p in source['owned_inputs']])
    return out


# --------------------------------------------------------- dependency parse

def _strict_notice(text, project):
    """Strictly parse one authoritative dependency observation text."""
    if not isinstance(text, str):
        raise ValueError('dependency observation text missing')
    _ids, error = dependency_inputs._parse_observation(text, project)
    if error is not None:
        raise ValueError('dependency observation is not strict: %s' % error)
    payload = text[len(dependency_inputs.DEPENDENCY_PREFIX):]
    try:
        data = json.loads(payload)
    except ValueError:
        raise ValueError('dependency observation payload malformed')
    if not isinstance(data, dict):
        raise ValueError('dependency observation payload malformed')
    return data


def _parse_notices(evo, project, sources):
    notices = [_observed_notice(evo, project, source['source_id'],
                                source['version'], source.get('text_sha'))
               for source in sources]
    notices.sort(key=lambda n: (n['source_id'], n['version']))
    return notices


# ------------------------------------------------------------- target scope

def _full_card(evo, cid):
    card = evo.cards.get(cid)
    if card is None:
        raise ValueError('card missing: %s' % cid)
    return card


def _basis_full_proof(card):
    return {
        'id': card['id'],
        'version': card['version'],
        'state': card['state'],
        'title': card.get('title'),
        'topic': card.get('topic'),
        'body_sha': _sha_text(card.get('body') or ''),
        'evidence_sha': _hash(card.get('evidence') or []),
        'card_digest': dop._card_digest(card),
        'card_full_digest': dop._card_full_digest(card),
    }


def _evidence_copy(card):
    return [dict(e) for e in (card.get('evidence') or [])
            if isinstance(e, dict)]


def _build_targets(evo, project, notices, human_checks):
    """Read every full current target + basis card and build refresh ops.

    A notification basis id must be an EXISTING relation of its notified
    target, and every target/basis must be an active authorized card. An
    update is produced ONLY for a target whose stored basis version differs
    from the current basis card version; every basis is mapped to its current
    version; body, title, topic, state and the exact evidence list are
    preserved verbatim. Duplicated bases are refused, never collapsed.
    """
    catalog = dop._authorized_by_id(evo, project)
    order = []
    mapping = {}
    for notice in notices:
        tid = notice['target_card_id']
        if tid not in mapping:
            mapping[tid] = []
            order.append(tid)
        mapping[tid].append(notice)
    targets = []
    operations = []
    for tid in sorted(order):
        if tid not in catalog:
            raise ValueError('target card is not authorized: %s' % tid)
        card = _full_card(evo, tid)
        if card['state'] != 'active':
            raise ValueError('target card is not active: %s' % tid)
        stored_basis = []
        seen_stored = set()
        for b in (card.get('basis_cards') or []):
            if not isinstance(b, dict):
                raise ValueError('target basis relation malformed: %s' % tid)
            bid = b.get('card_id')
            if not isinstance(bid, str) or not bid:
                raise ValueError('target basis id malformed: %s' % tid)
            if bid in seen_stored:
                raise ValueError('duplicate target basis: %s' % bid)
            seen_stored.add(bid)
            stored_basis.append({'card_id': bid, 'version': b.get('version')})
        rel_ids = set(seen_stored)
        for notice in mapping[tid]:
            seen_notified = set()
            for entry in notice['changed_basis']:
                bid = entry['card_id']
                if bid in seen_notified:
                    raise ValueError('duplicate notified basis: %s' % bid)
                seen_notified.add(bid)
                if bid not in rel_ids:
                    raise ValueError('notification basis is not a target relation: %s'
                                     % bid)
                if bid not in catalog:
                    raise ValueError('basis card is not authorized: %s' % bid)
                bcard = _full_card(evo, bid)
                if bcard['state'] != 'active':
                    raise ValueError('basis card is not active: %s' % bid)
        current_basis = []
        basis_full = []
        changed = False
        for b in stored_basis:
            bid = b['card_id']
            if bid not in catalog:
                raise ValueError('target basis card is not authorized: %s' % bid)
            bcard = _full_card(evo, bid)
            if bcard['state'] != 'active':
                raise ValueError('target basis card is not active: %s' % bid)
            current_basis.append({'card_id': bid, 'version': bcard['version']})
            basis_full.append(_basis_full_proof(bcard))
            if bcard['version'] != b['version']:
                changed = True
        target = {
            'id': tid,
            'version': card['version'],
            'state': card['state'],
            'title': card.get('title'),
            'topic': card.get('topic'),
            'body': card.get('body'),
            'evidence': _evidence_copy(card),
            'stored_basis': stored_basis,
            'current_basis': current_basis,
            'basis_cards_full': basis_full,
            'changed': bool(changed),
            'card_digest': dop._card_digest(card),
            'card_full_digest': dop._card_full_digest(card),
        }
        targets.append(target)
        if changed:
            if card.get('manually_edited'):
                dop._require_manual_check(card, human_checks, tid)
            operations.append({
                'op': 'update',
                'id': tid,
                'expected_version': card['version'],
                'body': card.get('body'),
                'state': card['state'],
                'title': card.get('title'),
                'topic': card.get('topic'),
                'evidence': _evidence_copy(card),
                'reason': _UPDATE_REASON,
                'basis_cards': [dict(b) for b in current_basis],
            })
    return targets, operations


def _target_binding(target):
    """Body-free binding of one reviewed target.

    Carries the ids, versions and digests AND every basis full proof (each of
    which is itself body-free: hashes only). This keeps the durable target
    binding able to prove the reviewed basis full texts.
    """
    return {
        'id': target['id'],
        'version': target['version'],
        'state': target['state'],
        'card_digest': target['card_digest'],
        'card_full_digest': target['card_full_digest'],
        'stored_basis': [dict(b) for b in target['stored_basis']],
        'current_basis': [dict(b) for b in target['current_basis']],
        'basis_cards_full': [dict(b) for b in target['basis_cards_full']],
        'changed': bool(target['changed']),
    }


# ---------------------------------------------------------- plan assembly

def prepare(evo, request):
    """Return a deterministic legacy-operator plan; performs no writes."""
    norm = _validate_request(request)
    row = dop._work_row(evo, norm['work_id'])
    _validate_scope(evo, row)
    _reject_foreign_completion(evo, row['id'])
    if row['session'] != dependency_inputs.DEPENDENCY_SESSION:
        raise ValueError('work session is not the knowledge-dependency session')
    sources = _collect_owned_sources(evo, row)
    scope = evo._scope_fingerprint(row['project'])
    notices = _parse_notices(evo, row['project'], sources)
    targets, operations = _build_targets(evo, row['project'], notices,
                                         norm['human_checks'])
    normalized_operations = []
    changed_target_ids = []
    if operations:
        verdict = evo.cards.validate_operations(operations, row['project'])
        if not isinstance(verdict, dict) or verdict.get('ok') is not True:
            problems = verdict.get('problems') if isinstance(verdict, dict) else None
            raise ValueError('operations rejected by store: %s'
                             % _canon(problems or [])[:300])
        normalized_operations = verdict.get('operations')
        if not isinstance(normalized_operations, list) or not normalized_operations:
            raise ValueError('normalized operations empty')
        changed_target_ids = sorted({op['id'] for op in normalized_operations
                                     if isinstance(op.get('id'), str)})
        expected_ids = sorted(t['id'] for t in targets if t['changed'])
        if changed_target_ids != expected_ids:
            raise ValueError('normalized operations do not match changed targets')
    else:
        if any(t['changed'] for t in targets):
            raise ValueError('targets require basis refresh but no operations built')
    owned_inputs = []
    for source in sources:
        owned_inputs.extend(source['owned_inputs'])
    owned_inputs.sort(key=lambda x: (x['source_id'], x['version'], x['part']))
    target_bindings = [_target_binding(t) for t in targets]
    body = {
        'protocol': PROTOCOL,
        'operator_review_identity': OPERATOR_REVIEW_IDENTITY,
        'request': norm,
        'request_sha': _hash(norm),
        'grant_id': norm['grant_id'],
        'work_id': norm['work_id'],
        'project': row['project'],
        'session': row['session'],
        'rationale_sha': _sha_text(norm['rationale']),
        'code_sha': norm['code_sha'],
        'acceptance_sha': norm['acceptance_sha'],
        'review_sha': norm['review_sha'],
        'scope_fingerprint': scope,
        'work_snapshot': dop._work_snapshot(row),
        'sources': sources,
        'owned_inputs': owned_inputs,
        'notices': notices,
        'targets': targets,
        'target_bindings': target_bindings,
        'changed_target_ids': changed_target_ids,
        'operations_sha': _hash(normalized_operations),
        'normalized_operations': normalized_operations,
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
                'grant_id', 'work_id', 'project', 'session', 'rationale_sha',
                'code_sha', 'acceptance_sha', 'review_sha', 'scope_fingerprint',
                'work_snapshot', 'sources', 'owned_inputs', 'notices', 'targets',
                'target_bindings', 'changed_target_ids', 'operations_sha',
                'normalized_operations', 'human_checks_sha', 'plan_sha')
    dop._reject_extra(plan, required, 'plan')
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


def _plan_binding(plan):
    """Body-free binding shared by the durable intent and the receipt."""
    return {
        'grant_id': plan['grant_id'],
        'work_id': plan['work_id'],
        'project': plan['project'],
        'session': plan['session'],
        'rationale_sha': plan['rationale_sha'],
        'request_sha': plan['request_sha'],
        'plan_sha': plan['plan_sha'],
        'code_sha': plan['code_sha'],
        'acceptance_sha': plan['acceptance_sha'],
        'review_sha': plan['review_sha'],
        'scope_fingerprint': plan['scope_fingerprint'],
        'sources': [dict(s) for s in plan['sources']],
        'owned_inputs': [dict(x) for x in plan['owned_inputs']],
        'notices': [dict(n) for n in plan['notices']],
        'targets': [dict(t) for t in plan['target_bindings']],
        'operations_sha': plan['operations_sha'],
    }


def _approval(plan):
    """The operator attestation handed to the ORIGINAL ``KnowledgeStore.apply``."""
    return {
        'approved': True,
        'findings': [],
        'proposal_hash': dop._proposal_hash(plan['normalized_operations']),
        'human_checks': [dict(c) for c in plan['request']['human_checks']],
        'origin': OPERATOR_ORIGIN,
        'operator_review_identity': OPERATOR_REVIEW_IDENTITY,
    }


# ------------------------------------------------------------- durable records

def _intent_key(grant_id):
    return LEGACY_OPERATOR_META_PREFIX + grant_id


def _stored_intent(evo, grant_id):
    row = evo.db.execute('SELECT value FROM meta WHERE key=?',
                         (_intent_key(grant_id),)).fetchone()
    if row is None:
        return None
    try:
        value = json.loads(row['value'])
    except ValueError:
        raise ValueError('stored legacy operator intent malformed')
    if not isinstance(value, dict):
        raise ValueError('stored legacy operator intent malformed')
    return value


def _intent(binding, batch, applied_versions, calls_before):
    intent = dict(binding)
    intent.update({
        'schema': SCHEMA,
        'origin': OPERATOR_ORIGIN,
        'protocol': PROTOCOL,
        'operator_review_identity': OPERATOR_REVIEW_IDENTITY,
        'op_hash': batch['op_hash'],
        'review_hash': batch['review_hash'],
        'changed_ids': list(batch['changed_ids']),
        'applied_versions': dict(applied_versions),
        'calls_before': dict(calls_before),
    })
    return intent


def _completion_receipt(binding, batch, applied_versions, applied_cards,
                        calls_before):
    receipt = dict(binding)
    receipt.update({
        'schema': SCHEMA,
        'origin': OPERATOR_ORIGIN,
        'protocol': PROTOCOL,
        'operator_review_identity': OPERATOR_REVIEW_IDENTITY,
        'op_hash': batch['op_hash'],
        'review_hash': batch['review_hash'],
        'changed_ids': list(batch['changed_ids']),
        'applied_versions': dict(applied_versions),
        'applied_cards': applied_cards,
        'calls_before': dict(calls_before),
        'time': time.time(),
    })
    return receipt


def _operator_audits(evo, work_id, state):
    return evo.db.execute(
        'SELECT error FROM knowledge_failures WHERE work_id=? AND state=?'
        ' ORDER BY created_at', (work_id, state)).fetchall()


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


def has_legacy_operator_completion(evo, work_id):
    return len(_operator_audits(evo, work_id, LEGACY_OPERATOR_COMPLETE_AUDIT)) > 0


def _verify_intent_batch(evo, intent):
    work_id = intent['work_id']
    batch = evo.db.execute('SELECT * FROM knowledge_batches WHERE batch_id=?',
                           (work_id,)).fetchone()
    if batch is None:
        raise ValueError('intent batch missing')
    if batch['project'] != intent['project']:
        raise ValueError('intent batch project mismatch')
    if batch['op_hash'] != intent['op_hash']:
        raise ValueError('intent op hash mismatch')
    if batch['review_hash'] != intent['review_hash']:
        raise ValueError('intent review hash mismatch')
    try:
        changed = json.loads(batch['changed_ids'])
    except (ValueError, TypeError):
        raise ValueError('batch changed ids malformed')
    if not isinstance(changed, list) or list(changed) != list(intent['changed_ids']):
        raise ValueError('intent changed ids mismatch')
    for cid in changed:
        if not isinstance(cid, str) or not cid:
            raise ValueError('batch changed id malformed')
    applied = intent.get('applied_versions')
    if not isinstance(applied, dict):
        raise ValueError('applied versions malformed')
    if set(applied) != set(changed):
        raise ValueError('applied versions do not match changed ids')
    for ver in applied.values():
        if not _is_int(ver) or ver < 1:
            raise ValueError('applied version malformed')
    return batch


def _validated_intent(evo, work_id):
    row = dop._work_row(evo, work_id)
    if row is None:
        raise ValueError('work missing')
    if row['pipeline_protocol'] != PROTOCOL:
        raise ValueError('work is not the legacy v5 protocol')
    if row['session'] != dependency_inputs.DEPENDENCY_SESSION:
        raise ValueError('work session is not the knowledge-dependency session')
    intent_audit = _unique_operator_audit(evo, work_id,
                                          LEGACY_OPERATOR_INTENT_AUDIT)
    if intent_audit.get('origin') != OPERATOR_ORIGIN:
        raise ValueError('intent origin mismatch')
    if intent_audit.get('schema') != SCHEMA:
        raise ValueError('intent schema mismatch')
    if intent_audit.get('protocol') != PROTOCOL:
        raise ValueError('intent protocol mismatch')
    if intent_audit.get('work_id') != work_id:
        raise ValueError('intent work id mismatch')
    if intent_audit.get('operator_review_identity') != OPERATOR_REVIEW_IDENTITY:
        raise ValueError('intent operator identity mismatch')
    grant_id = intent_audit.get('grant_id')
    if not _is_slug(grant_id):
        raise ValueError('intent grant id malformed')
    meta = _stored_intent(evo, grant_id)
    if meta is None:
        raise ValueError('intent meta missing')
    body = {k: v for k, v in intent_audit.items() if k != 'phase'}
    if _canon(meta) != _canon(body):
        raise ValueError('meta/intent mismatch')
    if meta.get('grant_id') != grant_id:
        raise ValueError('intent grant mismatch')
    if meta.get('work_id') != work_id:
        raise ValueError('intent work binding mismatch')
    if meta.get('project') != row['project']:
        raise ValueError('intent project mismatch')
    if meta.get('session') != row['session']:
        raise ValueError('intent session mismatch')
    _verify_intent_batch(evo, meta)
    return row, intent_audit, meta


# ---------------------------------------------------------------- apply

def _resume_plan(intent):
    return {
        'work_id': intent['work_id'],
        'grant_id': intent['grant_id'],
        'project': intent['project'],
        'session': intent['session'],
        'rationale_sha': intent['rationale_sha'],
        'request_sha': intent['request_sha'],
        'plan_sha': intent['plan_sha'],
        'code_sha': intent['code_sha'],
        'acceptance_sha': intent['acceptance_sha'],
        'review_sha': intent['review_sha'],
        'scope_fingerprint': intent['scope_fingerprint'],
        'sources': [dict(s) for s in intent['sources']],
        'owned_inputs': [dict(x) for x in intent['owned_inputs']],
        'notices': [dict(n) for n in intent['notices']],
        'target_bindings': [dict(t) for t in intent['targets']],
        'operations_sha': intent['operations_sha'],
        'normalized_operations': [],
    }


def apply(evo, plan, files):
    """Durably complete exactly one failed legacy v5 row from an operator plan.

    Runs under the existing engine lock. A repeated identical plan whose grant
    was already committed returns a read-only proven replay with zero writes;
    a different plan on the same grant is rejected. Publication and
    verification happen under the SAME held lock; only a confirmed publication
    finalizes ``done``.
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
                raise ValueError('stored legacy operator intent malformed')
            if not isinstance(stored, dict) or stored.get('plan_sha') != plan['plan_sha']:
                raise ValueError('grant_id already bound to a different plan')
            replay = {'origin': OPERATOR_ORIGIN, 'grant_id': plan['grant_id'],
                      'work_id': work_id, 'plan_sha': plan['plan_sha'],
                      'replayed': True, 'receipt': None}
            if _operator_audits(evo, work_id, LEGACY_OPERATOR_COMPLETE_AUDIT):
                verified = verify_completed(evo, work_id)
                replay['receipt'] = verified['receipt']
                replay['completed'] = True
                return replay
            row = dop._work_row(evo, work_id)
            if row is not None and row['state'] == LEGACY_OPERATOR_STATE:
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

        # Resync manual files BEFORE the atomic transition.
        files.sync()

        with evo.engine.transaction():
            fresh = prepare(evo, plan['request'])
            if _canon(fresh) != _canon(plan):
                raise ValueError('plan is stale or not canonical')
            row = dop._work_row(evo, work_id)
            _validate_scope(evo, row)

            approval = _approval(plan)
            result = evo.cards.apply(work_id, plan['project'],
                                     plan['normalized_operations'], approval)
            if result.get('replayed'):
                raise ValueError('batch id already committed')
            changed_ids = list(result.get('changed_ids') or [])
            applied_versions = {}
            for cid in changed_ids:
                card = evo.cards.get(cid)
                if card is not None:
                    applied_versions[cid] = card.get('version')
            batch_row = evo.db.execute(
                'SELECT * FROM knowledge_batches WHERE batch_id=?',
                (work_id,)).fetchone()
            if batch_row is None:
                raise ValueError('batch was not written by the store')
            batch = {'op_hash': batch_row['op_hash'],
                     'review_hash': batch_row['review_hash'],
                     'changed_ids': changed_ids}
            binding = _plan_binding(plan)
            intent = _intent(binding, batch, applied_versions,
                             _call_ledger(evo, work_id))
            evo.db.execute('INSERT INTO meta(key,value) VALUES(?,?)',
                           (grant_key, _canon(intent)))
            evo.db.execute(
                'INSERT INTO knowledge_failures(work_id,state,error,created_at)'
                ' VALUES(?,?,?,?)',
                (work_id, LEGACY_OPERATOR_INTENT_AUDIT,
                 _canon(dict(intent, phase='committed')), time.time()))
            cur = evo.db.execute(
                "UPDATE knowledge_work SET state=?, error=NULL, lease_token=NULL,"
                " lease_until=0 WHERE id=? AND pipeline_protocol=? AND state='failed'",
                (LEGACY_OPERATOR_STATE, work_id, PROTOCOL))
            if cur.rowcount != 1:
                raise ValueError('work could not be moved to legacy operator pending')

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
    calls_now = _call_ledger(evo, work_id)
    if intent is not None and calls_now != intent.get('calls_before'):
        raise ValueError('legacy operator call ledger changed since commit')
    applied_cards = dop._applied_cards(evo, changed_ids, applied_versions)
    calls_before = calls_now if intent is None else intent['calls_before']
    with evo.engine.transaction():
        current = dop._work_row(evo, work_id)
        if current is None or current['state'] != LEGACY_OPERATOR_STATE:
            outcome['replayed'] = True
            return outcome
        binding = _plan_binding(plan)
        receipt = _completion_receipt(binding, batch, applied_versions,
                                      applied_cards, calls_before)
        evo.db.execute(
            'INSERT INTO knowledge_failures(work_id,state,error,created_at)'
            ' VALUES(?,?,?,?)',
            (work_id, LEGACY_OPERATOR_COMPLETE_AUDIT, _canon(receipt),
             time.time()))
        evo.db.execute(
            "UPDATE knowledge_work SET state='done', inputs='[]',"
            " proposal=NULL, review=NULL, bundle=NULL, lease_token=NULL,"
            " lease_until=0, error=NULL, finished_at=? WHERE id=?",
            (time.time(), work_id))
    outcome['receipt'] = receipt
    return outcome


def publish_one(evo, work_id, files):
    """Resume publication of ONE operator-pending row from its real intent.

    Makes NO model call and never repeats ``KnowledgeStore.apply``. Refuses a
    missing/paused/done/wrong-protocol/mixed row, or a malformed intent, BEFORE
    any publish effect, and re-proves the historical source/notice/target/basis
    mapping before the first write.
    """
    with evo.engine.locked():
        row = dop._work_row(evo, work_id)
        if row is None:
            raise ValueError('work missing')
        if row['pipeline_protocol'] != PROTOCOL:
            raise ValueError('work is not the legacy v5 protocol')
        if row['state'] == 'protocol_paused':
            raise ValueError('work is protocol_paused; unpark before publishing')
        if row['state'] == 'done':
            raise ValueError('work already done')
        if row['state'] != LEGACY_OPERATOR_STATE:
            raise ValueError('work is not legacy_operator_publication_pending')
        if _operator_audits(evo, work_id, LEGACY_OPERATOR_COMPLETE_AUDIT):
            raise ValueError('legacy operator completion already recorded')
        _reject_foreign_completion(evo, work_id)
        _row, _intent, meta = _validated_intent(evo, work_id)
        if _call_ledger(evo, work_id) != meta['calls_before']:
            raise ValueError('call ledger changed before publication')
        _prove_mapping(evo, work_id, row, meta['sources'], meta['owned_inputs'],
                       meta['notices'], meta['targets'], meta['changed_ids'],
                       meta['applied_versions'])
        plan = _resume_plan(meta)
        batch = {'op_hash': meta['op_hash'], 'review_hash': meta['review_hash'],
                 'changed_ids': list(meta['changed_ids'])}
        return _publish_and_finalize(evo, plan, batch, meta['applied_versions'],
                                     files, meta)


def resume_pending(evo, files, limit=3):
    """Resume up to ``limit`` legacy-operator-pending rows from real intents."""
    if not _is_int(limit) or limit < 0:
        limit = 3
    limit = min(limit, 3)
    rows = evo.db.execute(
        "SELECT id FROM knowledge_work WHERE pipeline_protocol=? AND state=?"
        " ORDER BY created_at, id", (PROTOCOL, LEGACY_OPERATOR_STATE)).fetchall()
    published = 0
    attention = 0
    failed = 0
    for row in rows:
        if published + attention + failed >= limit:
            break
        try:
            outcome = publish_one(evo, row['id'], files)
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

def validate_legacy_operator_intent(evo, work_id):
    """Read-only validation of the FULL legacy operator intent chain."""
    _row, _intent, meta = _validated_intent(evo, work_id)
    return meta


def _historical_card(evo, cid, version):
    """Return the FULL frozen card at ``version`` (incl. ``basis_cards``).

    A live card already sitting at exactly ``version`` is read directly; it
    carries the real basis relations. Otherwise the frozen
    ``knowledge_revisions`` snapshot for that old version is decoded, with the
    JSON evidence and the recorded basis relations retained. Unlike
    ``delta_receipts._card_at`` this never drops the basis relations, so the
    full-digest comparison stays meaningful.
    """
    live = evo.cards.get(cid)
    if live is not None and live.get('version') == version:
        return dict(live)
    row = evo.db.execute(
        'SELECT snapshot FROM knowledge_revisions WHERE card_id=? AND old_version=?'
        ' ORDER BY id DESC LIMIT 1', (cid, version)).fetchone()
    if row is None:
        return None
    try:
        snap = json.loads(row['snapshot'])
    except (ValueError, TypeError):
        return None
    if not isinstance(snap, dict):
        return None
    snap = dict(snap)
    evidence = snap.get('evidence')
    if isinstance(evidence, str):
        try:
            evidence = json.loads(evidence)
        except ValueError:
            return None
    snap['evidence'] = evidence
    snap['version'] = version
    if not isinstance(snap.get('basis_cards'), list):
        snap['basis_cards'] = []
    return snap


def verify_completed(evo, work_id):
    """Prove a finished legacy operator row read-only; never writes/publishes."""
    row = dop._work_row(evo, work_id)
    if row is None:
        raise ValueError('work missing')
    if row['pipeline_protocol'] != PROTOCOL:
        raise ValueError('protocol mismatch')
    if row['state'] != 'done':
        raise ValueError('work not done')
    if row['session'] != dependency_inputs.DEPENDENCY_SESSION:
        raise ValueError('work session is not the knowledge-dependency session')

    if len(_operator_audits(evo, work_id, LEGACY_OPERATOR_COMPLETE_AUDIT)) != 1:
        raise ValueError('legacy operator completion audit not unique')
    _reject_foreign_completion(evo, work_id)

    intent = _unique_operator_audit(evo, work_id, LEGACY_OPERATOR_INTENT_AUDIT)
    receipt = _unique_operator_audit(evo, work_id, LEGACY_OPERATOR_COMPLETE_AUDIT)

    if intent.get('origin') != OPERATOR_ORIGIN or receipt.get('origin') != OPERATOR_ORIGIN:
        raise ValueError('origin mismatch')
    if intent.get('schema') != SCHEMA or receipt.get('schema') != SCHEMA:
        raise ValueError('schema mismatch')
    if intent.get('protocol') != PROTOCOL or receipt.get('protocol') != PROTOCOL:
        raise ValueError('protocol binding mismatch')
    if intent.get('work_id') != work_id or receipt.get('work_id') != work_id:
        raise ValueError('work id mismatch')
    if intent.get('project') != row['project'] or receipt.get('project') != row['project']:
        raise ValueError('project mismatch')
    if intent.get('session') != row['session'] or receipt.get('session') != row['session']:
        raise ValueError('session mismatch')
    if receipt.get('operator_review_identity') != OPERATOR_REVIEW_IDENTITY:
        raise ValueError('operator identity mismatch')
    if intent.get('operator_review_identity') != OPERATOR_REVIEW_IDENTITY:
        raise ValueError('operator intent identity mismatch')

    grant_id = receipt.get('grant_id')
    if not _is_slug(grant_id):
        raise ValueError('receipt grant id malformed')
    meta = _stored_intent(evo, grant_id)
    if meta is None:
        raise ValueError('legacy operator intent meta missing')
    if _canon(meta) != _canon({k: v for k, v in intent.items() if k != 'phase'}):
        raise ValueError('complete meta/intent mismatch')
    for name in ('grant_id', 'plan_sha', 'op_hash', 'review_hash', 'request_sha',
                 'operations_sha', 'project', 'session', 'rationale_sha',
                 'code_sha', 'acceptance_sha', 'review_sha',
                 'scope_fingerprint'):
        if meta.get(name) != receipt.get(name) or meta.get(name) != intent.get(name):
            raise ValueError('meta/receipt/intent mismatch: %s' % name)
    for name in ('sources', 'owned_inputs', 'notices', 'targets',
                 'changed_ids', 'applied_versions'):
        if _canon(meta.get(name)) != _canon(intent.get(name)):
            raise ValueError('meta/intent structural mismatch: %s' % name)
        if _canon(receipt.get(name)) != _canon(intent.get(name)):
            raise ValueError('receipt/intent structural mismatch: %s' % name)
    if meta.get('calls_before') != intent.get('calls_before'):
        raise ValueError('meta/intent calls mismatch')
    if receipt.get('calls_before') != intent.get('calls_before'):
        raise ValueError('receipt/intent calls mismatch')

    _verify_intent_batch(evo, receipt)

    if not isinstance(receipt.get('changed_ids'), list):
        raise ValueError('changed ids malformed')
    if not isinstance(receipt.get('applied_versions'), dict):
        raise ValueError('applied versions malformed')
    changed = list(receipt['changed_ids'])
    applied_cards = receipt.get('applied_cards')
    if not isinstance(applied_cards, list):
        raise ValueError('applied cards malformed')
    proof_ids = []
    for proof in applied_cards:
        if not isinstance(proof, dict):
            raise ValueError('applied card proof malformed')
        cid = proof.get('id')
        version = proof.get('version')
        if not isinstance(cid, str) or not cid:
            raise ValueError('applied card proof malformed')
        if not _is_int(version) or version < 1:
            raise ValueError('applied card proof version malformed')
        if cid in proof_ids:
            raise ValueError('duplicate applied card proof')
        proof_ids.append(cid)
        if receipt['applied_versions'].get(cid) != version:
            raise ValueError('applied card version mismatch')
        hist = _historical_card(evo, cid, version)
        if hist is None:
            raise ValueError('applied card version missing')
        if dop._card_digest(hist) != proof.get('card_digest'):
            raise ValueError('applied card digest mismatch')
    if len(proof_ids) != len(changed) or set(proof_ids) != set(changed):
        raise ValueError('applied cards do not match changed ids')

    _prove_mapping(evo, work_id, row, receipt['sources'],
                   receipt['owned_inputs'], receipt['notices'],
                   receipt['targets'], receipt['changed_ids'],
                   receipt['applied_versions'])

    calls_now = _call_ledger(evo, work_id)
    if calls_now != receipt['calls_before']:
        raise ValueError('call ledger changed')

    return {'verified': True, 'work_id': work_id, 'origin': OPERATOR_ORIGIN,
            'receipt': receipt, 'processed': 0, 'replayed': True}

def _owned_input_sort(item):
    return (item.get('source_id'), item.get('version'), item.get('part'))

def _basis_map(basis):
    """Return an ordered ``{card_id: version}`` map, rejecting malformed or
    duplicated basis relations rather than silently collapsing them."""
    if basis is None:
        return {}
    if not isinstance(basis, list):
        raise ValueError('basis relations malformed')
    out = {}
    for entry in basis:
        if not isinstance(entry, dict):
            raise ValueError('basis relation malformed')
        cid = entry.get('card_id')
        version = entry.get('version')
        if not isinstance(cid, str) or not cid:
            raise ValueError('basis relation card id malformed')
        if not _is_int(version) or version < 1:
            raise ValueError('basis relation version malformed')
        if cid in out:
            raise ValueError('duplicate basis relation: %s' % cid)
        out[cid] = version
    return out

def _observed_notice(evo, project, source_id, version, text_sha):
    """Re-derive one authoritative notice from the live source record."""
    try:
        live = evo.engine.store.source(source_id, version)
    except (KeyError, ValueError):
        raise ValueError('owned source version missing')
    if live is None:
        raise ValueError('owned source version missing')
    data = _strict_notice(live.get('text'), project)
    tid = data.get('card_id')
    if not isinstance(tid, str) or not tid:
        raise ValueError('dependency observation target malformed')
    changed = data.get('changed_basis')
    if not isinstance(changed, list) or not changed:
        raise ValueError('dependency observation basis malformed')
    basis = []
    for entry in changed:
        if not isinstance(entry, dict):
            raise ValueError('dependency observation basis malformed')
        cid = entry.get('card_id')
        if not isinstance(cid, str) or not cid:
            raise ValueError('dependency observation basis malformed')
        basis.append({
            'card_id': cid,
            'expected_version': entry.get('expected_version'),
            'current_version': entry.get('current_version'),
            'state': entry.get('state'),
        })
    return {'source_id': source_id, 'version': version, 'text_sha': text_sha,
            'target_card_id': tid, 'changed_basis': basis}

def _verify_source_binding(evo, row, source):
    """Prove historical source identity, every part and its exact projection."""
    if not isinstance(source, dict):
        raise ValueError('receipt source malformed')
    sid, version = source.get('source_id'), source.get('version')
    if not isinstance(sid, str) or not sid or not _is_int(version) or version < 1:
        raise ValueError('source identity malformed')
    live = evo.engine.store.source(sid, version)
    for field, expected in (('project', row['project']),
                            ('role', 'assistant'), ('kind', 'event'),
                            ('session', dependency_inputs.DEPENDENCY_SESSION)):
        if source.get(field) != expected or live.get(field) != expected:
            raise ValueError('source identity binding mismatch: ' + field)
    dop._verify_owned_source(evo, row['id'], row['project'], source)
    # Never feed hash-only receipt parts into _owned_event, which requires
    # complete current input text. The helper above proves historical parts.
    derived = [{'source_id': sid, 'version': version, 'part': part['part'],
                'text_sha': part['sha256']} for part in source['parts']]
    actual = source.get('owned_inputs')
    if not isinstance(actual, list) or len(actual) != len(derived):
        raise ValueError('source owned inputs malformed')
    for part in actual:
        if (not isinstance(part, dict) or set(part) !=
                {'source_id', 'version', 'part', 'text_sha'} or
                not _is_int(part.get('part')) or part['part'] < 0):
            raise ValueError('source owned input malformed')
    if _canon(sorted(actual, key=_owned_input_sort)) != _canon(sorted(derived, key=_owned_input_sort)):
        raise ValueError('source owned input projection mismatch')
    if source.get('owned_refs') != [part['part'] for part in source['parts']]:
        raise ValueError('source owned refs mismatch')

def _prove_basis_full(evo, target, current):
    """Prove every reviewed basis full proof against the frozen card."""
    proofs = target.get('basis_cards_full')
    if not isinstance(proofs, list):
        raise ValueError('basis full proofs malformed')
    if len(proofs) != len(current):
        raise ValueError('basis full proofs dropped or extra')
    seen = set()
    for proof in proofs:
        if not isinstance(proof, dict):
            raise ValueError('basis full proof malformed')
        bid = proof.get('id')
        bver = proof.get('version')
        if not isinstance(bid, str) or not bid or bid in seen:
            raise ValueError('basis full proof id malformed')
        seen.add(bid)
        if not _is_int(bver) or bver < 1:
            raise ValueError('basis full proof version malformed')
        if current.get(bid) != bver:
            raise ValueError('basis full proof version mismatch: %s' % bid)
        hist = _historical_card(evo, bid, bver)
        if hist is None:
            raise ValueError('basis version missing: %s' % bid)
        if hist.get('state') != proof.get('state'):
            raise ValueError('basis state mismatch: %s' % bid)
        if _sha_text(hist.get('body') or '') != proof.get('body_sha'):
            raise ValueError('basis body digest mismatch: %s' % bid)
        if _hash(hist.get('evidence') or []) != proof.get('evidence_sha'):
            raise ValueError('basis evidence digest mismatch: %s' % bid)
        if dop._card_digest(hist) != proof.get('card_digest'):
            raise ValueError('basis digest mismatch: %s' % bid)
        if dop._card_full_digest(hist) != proof.get('card_full_digest'):
            raise ValueError('basis full digest mismatch: %s' % bid)
    if seen != set(current):
        raise ValueError('basis full proofs do not match current basis')

def _prove_applied(evo, cid, target, applied_version):
    """Prove one applied version preserved the reviewed target and refreshed
    its basis ids to exactly the reviewed current basis versions."""
    if not _is_int(applied_version) or applied_version < 1:
        raise ValueError('applied version malformed')
    version = target.get('version')
    if not _is_int(version) or version < 1:
        raise ValueError('reviewed target version malformed')
    original = _historical_card(evo, cid, version)
    if original is None:
        raise ValueError('reviewed target version missing: %s' % cid)
    applied = _historical_card(evo, cid, applied_version)
    if applied is None:
        raise ValueError('applied card version missing: %s' % cid)
    if (applied.get('body') or '') != (original.get('body') or ''):
        raise ValueError('applied card changed body: %s' % cid)
    if _canon(applied.get('evidence') or []) != _canon(original.get('evidence') or []):
        raise ValueError('applied card changed evidence: %s' % cid)
    for field in ('title', 'topic', 'state'):
        if applied.get(field) != original.get(field):
            raise ValueError('applied card changed %s: %s' % (field, cid))
    applied_basis = _basis_map(applied.get('basis_cards'))
    stored_basis = _basis_map(target.get('stored_basis'))
    current_basis = _basis_map(target.get('current_basis'))
    if set(applied_basis) != set(stored_basis):
        raise ValueError('applied card changed basis ids: %s' % cid)
    if applied_basis != current_basis:
        raise ValueError('applied basis does not match reviewed current basis: %s'
                         % cid)

def _prove_mapping(evo, work_id, row, sources, owned_inputs, notices, targets,
                   changed_ids, applied_versions):
    """Shared read-only proof of the historical source -> notice -> target ->
    basis mapping plus the applied preservation. Used identically for pending
    and done states. Rejects dropped, extra or duplicated entries instead of
    silently collapsing them in a set. Performs no write."""
    if not isinstance(sources, list) or not (1 <= len(sources) <= MAX_SOURCES):
        raise ValueError('receipt sources malformed')
    source_ids = []
    for source in sources:
        _verify_source_binding(evo, row, source)
        sid = source['source_id']
        if sid in source_ids:
            raise ValueError('duplicate source binding')
        source_ids.append(sid)

    derived_inputs = []
    for source in sources:
        parts = source.get('parts')
        if not isinstance(parts, list) or not parts:
            raise ValueError('source parts malformed')
        for part in parts:
            if not isinstance(part, dict):
                raise ValueError('source part malformed')
            derived_inputs.append({
                'source_id': source['source_id'],
                'version': source['version'],
                'part': part.get('part'),
                'text_sha': part.get('sha256')})
    if not isinstance(owned_inputs, list) or not owned_inputs:
        raise ValueError('owned inputs malformed')
    if len(owned_inputs) != len(derived_inputs):
        raise ValueError('owned inputs dropped or extra')
    actual_inputs = []
    for x in owned_inputs:
        if not isinstance(x, dict):
            raise ValueError('owned input malformed')
        sid = x.get('source_id')
        version = x.get('version')
        part = x.get('part')
        sha = x.get('text_sha')
        if not isinstance(sid, str) or not sid:
            raise ValueError('owned input malformed')
        if not _is_int(version) or version < 1:
            raise ValueError('owned input malformed')
        if not _is_int(part) or part < 0:
            raise ValueError('owned input malformed')
        if not _is_hex(sha):
            raise ValueError('owned input malformed')
        actual_inputs.append({'source_id': sid, 'version': version,
                              'part': part, 'text_sha': sha})
    actual_inputs.sort(key=_owned_input_sort)
    derived_inputs.sort(key=_owned_input_sort)
    if _canon(actual_inputs) != _canon(derived_inputs):
        raise ValueError('owned inputs mismatch')
    _verify_seen_set(evo, work_id, derived_inputs)

    if not isinstance(notices, list) or not notices:
        raise ValueError('notices malformed')
    derived_notices = sorted(
        (_observed_notice(evo, row['project'], source['source_id'],
                          source['version'], source.get('text_sha'))
         for source in sources),
        key=lambda n: (n['source_id'], n['version']))
    given_notices = sorted((dict(n) for n in notices),
                           key=lambda n: (n['source_id'], n['version']))
    if len(given_notices) != len(derived_notices):
        raise ValueError('notices dropped or extra')
    if _canon(given_notices) != _canon(derived_notices):
        raise ValueError('notice mapping mismatch')

    if not isinstance(targets, list) or not targets:
        raise ValueError('target bindings malformed')
    target_index = {}
    target_order = []
    for target in targets:
        if not isinstance(target, dict):
            raise ValueError('target binding malformed')
        tid = target.get('id')
        if not isinstance(tid, str) or not tid:
            raise ValueError('target id malformed')
        if tid in target_index:
            raise ValueError('duplicate target binding')
        target_index[tid] = target
        target_order.append(tid)

    noticed_ids = []
    for notice in given_notices:
        tid = notice.get('target_card_id')
        if not isinstance(tid, str) or not tid:
            raise ValueError('notice target malformed')
        if tid not in noticed_ids:
            noticed_ids.append(tid)
    if sorted(target_order) != sorted(noticed_ids):
        raise ValueError('targets do not map exactly from source notices')

    for tid in target_order:
        target = target_index[tid]
        version = target.get('version')
        if not _is_int(version) or version < 1:
            raise ValueError('target version malformed')
        hist = _historical_card(evo, tid, version)
        if hist is None:
            raise ValueError('target version missing')
        if dop._card_digest(hist) != target.get('card_digest'):
            raise ValueError('target digest mismatch')
        if dop._card_full_digest(hist) != target.get('card_full_digest'):
            raise ValueError('target full digest mismatch')
        stored = _basis_map(target.get('stored_basis'))
        current = _basis_map(target.get('current_basis'))
        hist_basis = _basis_map(hist.get('basis_cards'))
        if stored != hist_basis:
            raise ValueError('target stored basis mismatch')
        if stored == current:
            if target.get('changed'):
                raise ValueError('target marked changed without a basis change')
        elif not target.get('changed'):
            raise ValueError('target changed but not marked reviewed')
        _prove_basis_full(evo, target, current)

    ids = list(changed_ids)
    if len(ids) != len(set(ids)):
        raise ValueError('duplicate changed id')
    changed_targets = sorted(t for t in target_order
                             if target_index[t].get('changed'))
    if sorted(ids) != changed_targets:
        raise ValueError('changed ids do not match reviewed targets')

    if not isinstance(applied_versions, dict):
        raise ValueError('applied versions malformed')
    if set(applied_versions) != set(ids):
        raise ValueError('applied versions do not match changed ids')

    for notice in given_notices:
        target = target_index[notice['target_card_id']]
        stored_ids = set(_basis_map(target.get('stored_basis')))
        changed_basis = notice.get('changed_basis')
        if not isinstance(changed_basis, list):
            raise ValueError('notice basis malformed')
        seen = set()
        for entry in changed_basis:
            if not isinstance(entry, dict):
                raise ValueError('notice basis malformed')
            bid = entry.get('card_id')
            if not isinstance(bid, str) or not bid:
                raise ValueError('notice basis malformed')
            if bid in seen:
                raise ValueError('duplicate notice basis: %s' % bid)
            seen.add(bid)
            if bid not in stored_ids:
                raise ValueError('notice basis not a target relation')

    for cid in ids:
        _prove_applied(evo, cid, target_index[cid], applied_versions[cid])

def _foreign_completion_states():
    """Exact existing audit namespaces; no guessed compatibility names."""
    from . import delta_receipts
    return ('completion', delta_receipts.COMPLETE_AUDIT,
            dop.OPERATOR_COMPLETE_AUDIT, dop.OPERATOR_INTENT_AUDIT)

def _reject_foreign_completion(evo, work_id):
    """Refuse any ordinary legacy / delta auto / v2 operator completion audit
    mixed onto the row. Read-only."""
    for state in _foreign_completion_states():
        if _operator_audits(evo, work_id, state):
            raise ValueError('mixed completion audit present: %s' % state)


def _verify_seen_set(evo, work_id, inputs):
    """Do not lose a whole source or extra part owned by this work."""
    expected = sorted((p['source_id'], p['version'], p['part']) for p in inputs)
    actual = sorted(tuple(row) for row in evo.db.execute(
        'SELECT source_id,version,part FROM knowledge_seen WHERE work_id=?',
        (work_id,)))
    if len(expected) != len(set(expected)) or actual != expected:
        raise ValueError('work seen responsibility set mismatch')
