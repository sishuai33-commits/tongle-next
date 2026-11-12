"""Compact, body-free completion receipt for the isolated v2 delta pipeline.

A v2 work row may only clear its bulky ``inputs``/``proposal``/``review``/
``bundle`` AFTER the real publication and a durable completion receipt, in the
same ``done`` transaction. This module builds and verifies that receipt. The
receipt holds identity, version and SHA-256 digests only -- never source text,
card bodies, quotes or model prose -- so a completion proof cannot re-create
the oversized source/card bundle whose duplication caused the earlier outage.

The authoritative truth stays where it always was: source versions, card
history (``knowledge_revisions``), the batch row, the call ledger, the stage
audits and ``knowledge_seen`` ownership. Verification therefore proves that the
historical bytes a past commit bound still exist and are unchanged; it does NOT
re-run semantic approval and does NOT decide current consumer eligibility
(a later edit or revocation may legitimately leave this proof intact while a
separate consumer rule refuses the material today).

Digests here are structural integrity markers, NOT authentication.
"""

import copy
import hashlib
import json
import time

PROTOCOL = 'tongle-delta-pipeline-v1'
STAGE_AUDIT = 'delta_stage_v1'
COMPLETE_AUDIT = 'delta_complete_v1'
RECEIPT_SCHEMA = 2

OWNED = 'owned'
REFERENCE = 'reference'
DECISIONS = ('material', 'covered', 'nonmaterial', 'unresolved')
STAGES = ('delta_plan', 'delta_review')
_HEX_CHARS = frozenset('0123456789abcdef')

__all__ = ['PROTOCOL', 'COMPLETE_AUDIT', 'RECEIPT_SCHEMA', 'build_receipt',
           'verify_completed']


# ---------------------------------------------------------------- digests

def _canon(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False)


def _sha(data):
    if isinstance(data, str):
        data = data.encode('utf-8')
    return hashlib.sha256(data).hexdigest()


def _hash(obj):
    return _sha(_canon(obj))


def _hex(value):
    return (isinstance(value, str) and len(value) == 64
            and all(c in _HEX_CHARS for c in value))


def _get(obj, key, default=None):
    try:
        return obj[key]
    except (IndexError, KeyError, TypeError):
        return default


def _row(evo, work_id):
    return evo.db.execute("SELECT * FROM knowledge_work WHERE id=?", (work_id,)).fetchone()


# -------------------------------------------------------- source metadata

def _window_sources(context):
    """Ordered, body-free metadata for every span in the frozen window.

    Records the exact short ref -> (span id, position, text hash) numbering the
    contract uses, plus per-part identity/bounds. No ``text`` is copied.
    """
    window = context['window']
    spans = {s.get('id'): s for s in window.get('spans', []) if isinstance(s, dict)}
    out = []
    counter = 0
    for part in window.get('parts', []):
        entries = []
        for span_id in part.get('span_ids') or []:
            counter += 1
            span = spans.get(span_id)
            text = span.get('text') if isinstance(span, dict) else None
            entries.append({
                'ref': 's%d' % counter,
                'position': counter,
                'span_id': span_id,
                'text_sha': _sha(text) if isinstance(text, str) else None,
            })
        out.append({
            'source_id': part.get('source_id'),
            'version': part.get('version'),
            'part': part.get('part'),
            'role': part.get('role'),
            'kind': part.get('kind'),
            'session': part.get('session'),
            'ledger_captured_at': part.get('ledger_captured_at'),
            'ledger_version_at': part.get('ledger_version_at'),
            'responsibility': part.get('responsibility'),
            'source_bounds': copy.deepcopy(part.get('source_bounds')),
            'text_sha': part.get('text_sha256'),
            'spans': entries,
        })
    return out


def _span_metadata(window):
    """Ordered body-free span metadata in the same short-ref numbering."""
    spans = {s.get('id'): s for s in window.get('spans', []) if isinstance(s, dict)}
    out = []
    counter = 0
    for part in window.get('parts', []):
        for span_id in part.get('span_ids') or []:
            counter += 1
            span = spans.get(span_id)
            text = span.get('text') if isinstance(span, dict) else None
            out.append({
                'ref': 's%d' % counter,
                'position': counter,
                'span_id': span_id,
                'text_sha': _sha(text) if isinstance(text, str) else None,
            })
    return out


def _owned_refs_from_window(window):
    """Exact required NONBLANK owned short refs for a frozen window."""
    spans = {s.get('id'): s for s in window.get('spans', []) if isinstance(s, dict)}
    out = []
    counter = 0
    for part in window.get('parts', []):
        for span_id in part.get('span_ids') or []:
            counter += 1
            if part.get('responsibility') != OWNED:
                continue
            span = spans.get(span_id)
            text = span.get('text') if isinstance(span, dict) else None
            if isinstance(text, str) and text.strip():
                out.append('s%d' % counter)
    return out


def _short_refs(context):
    refs = {}
    for part in _window_sources(context):
        for span in part['spans']:
            refs[span['ref']] = {
                'span_id': span['span_id'],
                'position': span['position'],
                'text_sha': span['text_sha'],
                'responsibility': part['responsibility'],
            }
    return refs


def _owned_refs(context):
    return _owned_refs_from_window(context['window'])


def _owned_inputs(row):
    raw = _get(row, 'inputs')
    try:
        parts = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        raise ValueError('owned inputs malformed')
    if not isinstance(parts, list) or not parts:
        raise ValueError('owned inputs missing')
    out = []
    for part in parts:
        if not isinstance(part, dict):
            raise ValueError('owned input malformed')
        text = part.get('text')
        out.append({
            'source_id': part.get('source_id'),
            'version': part.get('version'),
            'part': part.get('part'),
            'start': part.get('start'),
            'end': part.get('end'),
            'source_length': part.get('source_length'),
            'partial': part.get('partial'),
            'text_sha': _sha(text) if isinstance(text, str) else None,
        })
    return out


# ------------------------------------------------------------- card proofs

def _card_proof(card_id, version, body, title, topic, state, evidence):
    body_sha = _sha(body) if isinstance(body, str) else None
    evidence_sha = _hash(evidence) if isinstance(evidence, list) else None
    core = {'id': card_id, 'version': version, 'body_sha': body_sha,
            'state': state, 'title': title, 'topic': topic,
            'evidence_sha': evidence_sha}
    return {'id': card_id, 'version': version, 'body_sha': body_sha,
            'evidence_sha': evidence_sha, 'snapshot_sha': _hash(core)}


def _read_card_proofs(context):
    out = []
    for entry in context.get('cards', []):
        snap = entry.get('snapshot') if isinstance(entry, dict) else None
        if not isinstance(snap, dict):
            raise ValueError('read card snapshot malformed')
        out.append(_card_proof(snap.get('id'), snap.get('version'),
                               snap.get('body'), snap.get('title'),
                               snap.get('topic'), snap.get('state'),
                               entry.get('evidence')))
    return out


def _card_at(evo, cid, version):
    """Authoritative historical card content for one (id, version), or None.

    Prefers the live card when it is still at that version; otherwise reads the
    frozen ``knowledge_revisions`` snapshot. A card that advanced past the
    proven version therefore keeps its past proof.
    """
    live = evo.cards.get(cid)
    if live is not None and live.get('version') == version:
        return {'id': cid, 'version': version, 'body': live.get('body'),
                'title': live.get('title'), 'topic': live.get('topic'),
                'state': live.get('state'), 'evidence': live.get('evidence')}
    row = evo.db.execute(
        "SELECT snapshot FROM knowledge_revisions WHERE card_id=? AND old_version=?"
        " ORDER BY id DESC LIMIT 1", (cid, version)).fetchone()
    if row is None:
        return None
    try:
        snap = json.loads(row['snapshot'])
    except (ValueError, TypeError):
        return None
    if not isinstance(snap, dict):
        return None
    evidence = snap.get('evidence')
    if isinstance(evidence, str):
        try:
            evidence = json.loads(evidence)
        except ValueError:
            return None
    return {'id': snap.get('id'), 'version': version, 'body': snap.get('body'),
            'title': snap.get('title'), 'topic': snap.get('topic'),
            'state': snap.get('state'), 'evidence': evidence}


# --------------------------------------------------------- ledger / batch

def _clean_ids(changed):
    if not isinstance(changed, list):
        raise ValueError('changed ids malformed')
    out = []
    for cid in changed:
        if not isinstance(cid, str) or not cid:
            raise ValueError('changed id malformed')
        if cid in out:
            raise ValueError('duplicate changed id')
        out.append(cid)
    return out


def _clean_versions(applied):
    if not isinstance(applied, dict):
        raise ValueError('applied versions malformed')
    out = {}
    for cid, ver in applied.items():
        if not isinstance(cid, str) or not cid:
            raise ValueError('applied card id malformed')
        if isinstance(ver, bool) or not isinstance(ver, int) or ver < 1:
            raise ValueError('applied version malformed')
        out[cid] = ver
    return out


def _verify_batch(evo, work_id, project, op_hash, review_hash, changed):
    batch = evo.db.execute(
        "SELECT * FROM knowledge_batches WHERE batch_id=?", (work_id,)).fetchone()
    if batch is None:
        raise ValueError('missing batch')
    if _get(batch, 'project') != project:
        raise ValueError('batch project mismatch')
    if _get(batch, 'op_hash') != op_hash:
        raise ValueError('batch op hash mismatch')
    if _get(batch, 'review_hash') != review_hash:
        raise ValueError('batch review hash mismatch')
    raw = _get(batch, 'changed_ids')
    try:
        stored = json.loads(raw) if isinstance(raw, str) else list(raw)
    except (ValueError, TypeError):
        raise ValueError('batch changed ids malformed')
    if list(stored) != list(changed):
        raise ValueError('batch changed ids mismatch')


def _require_stage(evo, work_id, proof):
    stage = proof.get('stage')
    call_id = proof.get('call_id')
    if stage not in STAGES or not isinstance(call_id, str) or not call_id:
        raise ValueError('stage proof malformed')
    ledger = evo.db.execute(
        "SELECT id FROM knowledge_calls WHERE id=? AND work_id=? AND stage=?"
        " AND error IS NULL AND elapsed IS NOT NULL",
        (call_id, work_id, stage)).fetchone()
    if ledger is None:
        raise ValueError('stage ledger missing')
    matches = []
    for r in evo.db.execute(
            "SELECT error FROM knowledge_failures WHERE work_id=? AND state=?",
            (work_id, STAGE_AUDIT)).fetchall():
        try:
            obj = json.loads(r['error'])
        except (ValueError, TypeError):
            continue
        if (isinstance(obj, dict) and obj.get('call_id') == call_id
                and obj.get('stage') == stage):
            matches.append(obj)
    if len(matches) != 1:
        raise ValueError('stage audit not unique')
    expected = {
        'protocol': PROTOCOL,
        'work_id': work_id,
        'stage': stage,
        'binding': proof.get('binding'),
        'payload_hash': proof.get('payload_hash'),
        'response_hash': proof.get('response_hash'),
        'call_id': call_id,
    }
    if matches[0] != expected:
        raise ValueError('stage audit mismatch')


def _proof(evo, work_id, stage, binding, step):
    if not isinstance(step, dict):
        raise ValueError('stage step missing')
    if step.get('response_hash') != _hash(step.get('response')):
        raise ValueError('stage response hash mismatch')
    proof = {'call_id': step.get('call_id'), 'stage': stage, 'binding': binding,
             'payload_hash': step.get('payload_hash'),
             'response_hash': step.get('response_hash')}
    _require_stage(evo, work_id, proof)
    return proof


def _verify_stages(evo, work_id, stages, binding):
    if not isinstance(stages, list) or len(stages) != 2:
        raise ValueError('stage proofs malformed')
    seen = set()
    for proof in stages:
        if not isinstance(proof, dict):
            raise ValueError('stage proof malformed')
        stage = proof.get('stage')
        if stage not in STAGES or stage in seen:
            raise ValueError('stage proof invalid')
        if proof.get('binding') != binding:
            raise ValueError('stage proof binding mismatch')
        if not _hex(proof.get('payload_hash')) or not _hex(proof.get('response_hash')):
            raise ValueError('stage proof digest malformed')
        seen.add(stage)
        _require_stage(evo, work_id, proof)
    if seen != set(STAGES):
        raise ValueError('stage proofs incomplete')


def _verify_history_stages(evo, work_id, revision, repair, stages, binding):
    """Body-free repair provenance for the revision-0 history round.

    A revision-0 completion must carry no repair info. A revision-1 completion
    must carry a bounded repair kind and the REAL ledger/audit proofs of the
    historical steps the executor already replayed before finalize; only the
    kind and digests are kept -- never the raw draft, error label or model
    prose. Every historical proof is bound to the receipt's composite binding.
    """
    if revision == 0:
        if repair is not None or stages:
            raise ValueError('unexpected repair info')
        return
    if revision != 1:
        raise ValueError('repair revision malformed')
    if not isinstance(repair, dict):
        raise ValueError('repair info malformed')
    if repair.get('kind') not in ('plan', 'unresolved', 'review'):
        raise ValueError('repair kind invalid')
    if not _hex(repair.get('feedback_sha')) or not _hex(repair.get('draft_sha')):
        raise ValueError('repair digest malformed')
    if not isinstance(stages, list) or not stages:
        raise ValueError('repair history proofs missing')
    seen = set()
    for proof in stages:
        if not isinstance(proof, dict):
            raise ValueError('repair stage proof malformed')
        stage = proof.get('stage')
        if stage not in STAGES or stage in seen:
            raise ValueError('repair stage proof invalid')
        if proof.get('binding') != binding:
            raise ValueError('repair stage proof binding mismatch')
        if not _hex(proof.get('payload_hash')) or not _hex(proof.get('response_hash')):
            raise ValueError('repair stage proof digest malformed')
        seen.add(stage)
        _require_stage(evo, work_id, proof)
    if 'delta_plan' not in seen:
        raise ValueError('repair history plan proof missing')


# ------------------------------------------------------------- dispositions

def _dispositions(plan, context, refs):
    cards = context.get('cards', [])
    out = []
    for disp in plan.get('dispositions', []):
        if disp.get('decision') == 'unresolved':
            # A completion may never record an unresolved disposition.
            raise ValueError('unresolved disposition in complete receipt')
        entry = {
            'decision': disp.get('decision'),
            'refs': list(disp.get('source_refs') or []),
            'span_ids': [],
            'positions': [],
            'span_shas': [],
            'change_refs': list(disp.get('change_refs') or []),
            'covered': [],
        }
        for ref in disp.get('source_refs') or []:
            info = refs.get(ref)
            if info is None:
                raise ValueError('disposition unknown source ref')
            entry['span_ids'].append(info['span_id'])
            entry['positions'].append(info['position'])
            entry['span_shas'].append(info['text_sha'])
        for cref in disp.get('card_refs') or []:
            owner, _, seg_no = cref.partition('g')
            idx = int(owner[1:]) - 1
            seg_idx = int(seg_no) - 1
            if idx < 0 or idx >= len(cards):
                raise ValueError('covered card ref out of range')
            snap = cards[idx].get('snapshot')
            segs = snap.get('segments') if isinstance(snap, dict) else None
            if not isinstance(segs, list) or seg_idx < 0 or seg_idx >= len(segs):
                raise ValueError('covered segment ref out of range')
            seg = segs[seg_idx]
            entry['covered'].append({
                'ref': cref,
                'card_id': snap.get('id'),
                'version': snap.get('version'),
                'segment_id': seg.get('id'),
                'segment_sha': _sha(seg.get('text') or ''),
            })
        out.append(entry)
    return out


# ------------------------------------------------------------------- build

def build_receipt(evo, work_id, bundle, plan, approval, changed_ids, applied_versions):
    """Validate the finished delta work and return a compact body-free receipt.

    Purely a read: this never marks the row done, publishes or erases anything.
    The caller embeds the returned dict in the ``COMPLETE_AUDIT`` row and clears
    the bulky columns in the same transaction that sets ``state='done'``.
    """
    row = _row(evo, work_id)
    if row is None:
        raise ValueError('work missing')
    if _get(row, 'pipeline_protocol') != PROTOCOL:
        raise ValueError('protocol mismatch')
    if not isinstance(bundle, dict) or bundle.get('protocol') != PROTOCOL:
        raise ValueError('bundle malformed')
    from .delta_versions import contract_for
    delta_contract = contract_for(bundle=bundle)
    context = delta_contract.revalidate_context(bundle.get('context'))
    binding = bundle.get('binding')
    if binding != _hash({'scope': bundle.get('scope'), 'binding': context.get('binding')}):
        raise ValueError('binding mismatch')
    steps = bundle.get('steps')
    if not isinstance(steps, dict):
        raise ValueError('steps malformed')
    plan_step = steps.get('delta_plan')
    review_step = steps.get('delta_review')
    plan2 = delta_contract.compile_plan(
        context, plan_step.get('response') if isinstance(plan_step, dict) else None)
    if _canon(plan2) != _canon(plan):
        raise ValueError('plan recompilation mismatch')
    approval2 = delta_contract.approve_review(
        context, plan2, review_step.get('response') if isinstance(review_step, dict) else None)
    if _canon(approval2) != _canon(approval):
        raise ValueError('approval mismatch')
    stages = [_proof(evo, work_id, stage, binding, step)
              for stage, step in (('delta_plan', plan_step), ('delta_review', review_step))]
    # Body-free repair provenance: revision, bounded kind and the REAL
    # ledger/audit proofs of the revision-0 history steps. The source/semantic
    # repair replay was already proven by the executor before finalize; the
    # receipt keeps only identity digests, never the draft/error/model prose.
    from . import delta_stage_policy as policy
    stage_local = policy.require_identity(evo, work_id, bundle)
    policy_fields = policy.receipt_fields(evo, work_id, bundle) if stage_local else None
    revision = bundle.get('revision', 0)
    if not stage_local and revision not in (0, 1):
        raise ValueError('revision malformed')
    repair = None
    history_stages = []
    if not stage_local and revision == 1:
        feedback = bundle.get('repair_feedback')
        if not isinstance(feedback, dict) or feedback.get('kind') not in (
                'plan', 'unresolved', 'review'):
            raise ValueError('repair feedback malformed')
        draft = feedback.get('prior_draft')
        try:
            repair = {'kind': feedback.get('kind'),
                      'feedback_sha': _hash(feedback),
                      'draft_sha': _hash(draft)}
        except (TypeError, ValueError):
            raise ValueError('repair feedback not canonical')
        history = bundle.get('history')
        if not isinstance(history, list) or len(history) != 1:
            raise ValueError('repair history malformed')
        entry = history[0]
        hsteps = entry.get('steps') if isinstance(entry, dict) else None
        if not isinstance(hsteps, dict) or 'delta_plan' not in hsteps:
            raise ValueError('repair history steps malformed')
        for stage in STAGES:
            if stage in hsteps:
                history_stages.append(_proof(evo, work_id, stage, binding, hsteps[stage]))
        if not history_stages:
            raise ValueError('repair history proofs missing')
    changed = _clean_ids(changed_ids)
    applied = _clean_versions(applied_versions)
    if set(applied) != set(changed):
        raise ValueError('applied version keys mismatch')
    op_hash = _hash(plan2.get('operations') or [])
    review_hash = _hash(approval2)
    _verify_batch(evo, work_id, _get(row, 'project'), op_hash, review_hash, changed)
    applied_cards = []
    for cid in changed:
        hist = _card_at(evo, cid, applied[cid])
        if hist is None:
            raise ValueError('applied card historical version missing')
        applied_cards.append(_card_proof(hist['id'], hist['version'], hist['body'],
                                         hist['title'], hist['topic'], hist['state'],
                                         hist['evidence']))
    receipt = {
        'schema': policy.RECEIPT_SCHEMA if stage_local else RECEIPT_SCHEMA,
        'protocol': PROTOCOL,
        'work_id': work_id,
        'project': _get(row, 'project'),
        'kind': 'applied' if changed else 'no_op',
        'binding': binding,
        'scope': bundle.get('scope'),
        'context_binding': context.get('binding'),
        'window_id': (context.get('window') or {}).get('window_id'),
        'op_hash': op_hash,
        'review_hash': review_hash,
        'plan_hash': plan2.get('plan_hash'),
        'final_response_hash': review_step.get('response_hash'),
        'time': time.time(),
        'changed_ids': changed,
        'applied_versions': applied,
        'stages': stages,
        'window_sources': _window_sources(context),
        'owned_refs': _owned_refs(context),
        'owned_inputs': _owned_inputs(row),
        'read_cards': _read_card_proofs(context),
        'applied_cards': applied_cards,
        'dispositions': _dispositions(plan2, context, _short_refs(context)),
        'revision': revision,
        'repair': repair,
        'history_stages': history_stages,
    }
    if stage_local:
        receipt.update(policy_fields)
        policy.verify_receipt(evo, work_id, receipt)
    return receipt


# ------------------------------------------------------------------[ verify

def _verify_bounds(bounds, text):
    if bounds is None:
        return
    if not isinstance(bounds, dict):
        raise ValueError('source bounds malformed')
    names = ('start', 'end', 'source_length', 'text_start', 'text_end')
    for name in names:
        if name not in bounds:
            raise ValueError('source bounds field missing')
        value = bounds.get(name)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError('source bounds field malformed')
    if not isinstance(bounds.get('partial'), bool):
        raise ValueError('source bounds partial malformed')
    if not (0 <= bounds['text_start'] <= bounds['start'] <= bounds['end']
            <= bounds['text_end'] <= bounds['source_length']):
        raise ValueError('source bounds ordering violated')
    if len(text) != bounds['text_end'] - bounds['text_start']:
        raise ValueError('source bounds text length mismatch')


def _verify_window_sources(evo, project, sources, window_id, stored_spans,
                           required_owned):
    """Rebuild the frozen window from historical source texts and cross-check.

    The receipt only carries body-free metadata plus per-part text/span hashes.
    Verification re-freezes the window from the recorded (source_id, version,
    part) historical texts using the SAME ``source_windows.freeze_window``
    splitter and the SAME stored metadata (``event_time`` stays ``None``), then
    requires an exact ``window_id`` match, the exact ordered span metadata and
    the exact required NONBLANK owned refs. A later version or a revoke of the
    source stays valid; a moved span position can never hide behind an
    unchanged source text because the rebuilt ``window_id`` and ordered span
    digests both change. No new splitter and no new store are introduced.
    """
    from . import source_windows
    if not isinstance(sources, list) or not sources:
        raise ValueError('window sources missing')
    parts = []
    total = 0
    seen = set()
    for part in sources:
        if not isinstance(part, dict):
            raise ValueError('window source malformed')
        sid, ver, pnum = part.get('source_id'), part.get('version'), part.get('part')
        if not isinstance(sid, str) or not sid:
            raise ValueError('window source id malformed')
        if isinstance(ver, bool) or not isinstance(ver, int) or ver < 1:
            raise ValueError('window source version malformed')
        if isinstance(pnum, bool) or not isinstance(pnum, int) or pnum < 0:
            raise ValueError('window source part malformed')
        key = (sid, ver, pnum)
        if key in seen:
            raise ValueError('window source duplicate part')
        seen.add(key)
        try:
            src = evo.engine.store.source(sid, ver)
        except (KeyError, ValueError):
            raise ValueError('window source version missing')
        if src.get('project') != project:
            raise ValueError('window source project mismatch')
        text = src.get('text')
        if not isinstance(text, str) or _sha(text) != part.get('text_sha'):
            raise ValueError('window source text hash mismatch')
        _verify_bounds(part.get('source_bounds'), text)
        total += len(text)
        parts.append({
            'source_id': sid,
            'version': ver,
            'part': pnum,
            'role': part.get('role'),
            'kind': part.get('kind'),
            'text': text,
            'session': part.get('session'),
            'ledger_captured_at': part.get('ledger_captured_at'),
            'ledger_version_at': part.get('ledger_version_at'),
            'responsibility': part.get('responsibility'),
            'source_bounds': part.get('source_bounds'),
        })
    try:
        rebuilt = source_windows.freeze_window(project, parts, max_chars=max(total, 1))
    except ValueError as exc:
        raise ValueError('window re-freeze failed: ' + str(exc))
    if rebuilt.get('window_id') != window_id:
        raise ValueError('window id mismatch')
    if _span_metadata(rebuilt) != stored_spans:
        raise ValueError('window span metadata mismatch')
    computed_owned = _owned_refs_from_window(rebuilt)
    if list(required_owned) != computed_owned:
        raise ValueError('owned coverage mismatch')
    return computed_owned


def _verify_owned_inputs(evo, work_id, project, inputs):
    if not isinstance(inputs, list) or not inputs:
        raise ValueError('owned inputs missing')
    for part in inputs:
        sid, ver, pnum = part.get('source_id'), part.get('version'), part.get('part')
        if not isinstance(sid, str) or not sid:
            raise ValueError('owned input id malformed')
        if isinstance(ver, bool) or not isinstance(ver, int) or ver < 1:
            raise ValueError('owned input version malformed')
        if isinstance(pnum, bool) or not isinstance(pnum, int) or pnum < 0:
            raise ValueError('owned input part malformed')
        seen = evo.db.execute(
            "SELECT work_id FROM knowledge_seen WHERE source_id=? AND version=? AND part=?",
            (sid, ver, pnum)).fetchone()
        if seen is None or _get(seen, 'work_id') != work_id:
            raise ValueError('owned input not booked to work')
        try:
            src = evo.engine.store.source(sid, ver)
        except (KeyError, ValueError):
            raise ValueError('owned input version missing')
        if src.get('project') != project:
            raise ValueError('owned input project mismatch')
        text = src.get('text')
        if not isinstance(text, str):
            raise ValueError('owned input text missing')
        start, end = part.get('start'), part.get('end')
        if (isinstance(start, bool) or isinstance(end, bool)
                or not isinstance(start, int) or not isinstance(end, int)
                or start < 0 or end < start or end > len(text)):
            raise ValueError('owned input bounds malformed')
        if part.get('source_length') != len(text):
            raise ValueError('owned input source length mismatch')
        if part.get('partial') != (start > 0 or end < len(text)):
            raise ValueError('owned input partial mismatch')
        context_text = text[max(0, start - 300):min(len(text), end + 300)]
        if _sha(context_text) != part.get('text_sha'):
            raise ValueError('owned input text hash mismatch')


def _verify_card(evo, proof):
    cid, ver = proof.get('id'), proof.get('version')
    if not isinstance(cid, str) or not cid:
        raise ValueError('card proof id malformed')
    if isinstance(ver, bool) or not isinstance(ver, int) or ver < 1:
        raise ValueError('card proof version malformed')
    hist = _card_at(evo, cid, ver)
    if hist is None:
        raise ValueError('card historical version missing')
    rebuilt = _card_proof(cid, ver, hist['body'], hist['title'], hist['topic'],
                          hist['state'], hist['evidence'])
    if rebuilt['body_sha'] != proof.get('body_sha'):
        raise ValueError('card body hash mismatch')
    if rebuilt['snapshot_sha'] != proof.get('snapshot_sha'):
        raise ValueError('card snapshot hash mismatch')
    if rebuilt['evidence_sha'] != proof.get('evidence_sha'):
        raise ValueError('card evidence hash mismatch')


def _span_index(window_sources):
    index = {}
    for part in window_sources or []:
        for span in part.get('spans') or []:
            ref = span.get('ref')
            if ref in index:
                raise ValueError('duplicate window ref')
            index[ref] = span
    return index


def _verify_dispositions(dispositions, index, required):
    if not isinstance(dispositions, list):
        raise ValueError('dispositions malformed')
    seen = set()
    for disp in dispositions:
        if not isinstance(disp, dict):
            raise ValueError('disposition malformed')
        decision = disp.get('decision')
        if decision not in DECISIONS:
            raise ValueError('disposition decision invalid')
        if decision == 'unresolved':
            # An unresolved disposition can never be part of a COMPLETED work
            # row: completion requires a settled decision for every owned part.
            raise ValueError('unresolved disposition in complete receipt')
        refs = disp.get('refs')
        span_ids = disp.get('span_ids')
        positions = disp.get('positions')
        shas = disp.get('span_shas')
        if not all(isinstance(x, list) for x in (refs, span_ids, positions, shas)):
            raise ValueError('disposition arrays malformed')
        if len({len(refs), len(span_ids), len(positions), len(shas)}) != 1:
            raise ValueError('disposition arrays inconsistent')
        for ref, sid, pos, sha in zip(refs, span_ids, positions, shas):
            known = index.get(ref)
            if known is None or known.get('span_id') != sid \
                    or known.get('position') != pos or known.get('text_sha') != sha:
                raise ValueError('disposition span mismatch')
            if ref in seen:
                raise ValueError('duplicate disposed ref')
            seen.add(ref)
        changes = disp.get('change_refs')
        if not isinstance(changes, list):
            raise ValueError('disposition change refs malformed')
        if decision == 'material' and not changes:
            raise ValueError('material disposition without change')
        if decision != 'material' and changes:
            raise ValueError('non-material disposition links change')
        covered = disp.get('covered')
        if not isinstance(covered, list):
            raise ValueError('covered refs malformed')
        if decision == 'covered' and not covered:
            raise ValueError('covered disposition without card')
        for cov in covered:
            if not isinstance(cov, dict) or not isinstance(cov.get('card_id'), str) \
                    or not isinstance(cov.get('segment_id'), str):
                raise ValueError('covered card ref malformed')
            if isinstance(cov.get('version'), bool) or not isinstance(cov.get('version'), int):
                raise ValueError('covered card version malformed')
            if not _hex(cov.get('segment_sha')):
                raise ValueError('covered segment digest malformed')
    if seen != set(required):
        raise ValueError('owned coverage mismatch')


def _completion_audit(evo, work_id):
    rows = evo.db.execute(
        "SELECT error FROM knowledge_failures WHERE work_id=? AND state=? ORDER BY created_at",
        (work_id, COMPLETE_AUDIT)).fetchall()
    if len(rows) != 1:
        raise ValueError('completion audit not unique')
    try:
        obj = json.loads(rows[0]['error'])
    except (ValueError, TypeError):
        raise ValueError('completion audit malformed')
    if not isinstance(obj, dict) or obj.get('schema') not in (RECEIPT_SCHEMA, 3):
        raise ValueError('completion audit schema mismatch')
    return obj


def _verify_receipt(evo, row, receipt):
    work_id = _get(row, 'id')
    project = _get(row, 'project')
    if receipt.get('protocol') != PROTOCOL or receipt.get('work_id') != work_id:
        raise ValueError('receipt identity mismatch')
    if receipt.get('project') != project:
        raise ValueError('receipt project mismatch')
    for name in ('binding', 'context_binding', 'window_id', 'op_hash',
                 'review_hash', 'plan_hash'):
        if not _hex(receipt.get(name)):
            raise ValueError('receipt ' + name + ' malformed')
    if receipt.get('binding') != _hash(
            {'scope': receipt.get('scope'), 'binding': receipt.get('context_binding')}):
        raise ValueError('receipt binding mismatch')
    changed = _clean_ids(receipt.get('changed_ids'))
    applied = _clean_versions(receipt.get('applied_versions'))
    if set(applied) != set(changed):
        raise ValueError('receipt applied versions mismatch')
    kind = receipt.get('kind')
    if kind not in ('applied', 'no_op') or (kind == 'applied') != bool(changed):
        raise ValueError('receipt kind invalid')
    from . import delta_stage_policy as policy
    stage_local = receipt.get('schema') == policy.RECEIPT_SCHEMA
    if stage_local:
        policy.verify_receipt(evo, work_id, receipt)
    elif 'execution_policy' in receipt or evo.db.execute(
            'SELECT 1 FROM knowledge_failures WHERE work_id=? AND state=?',
            (work_id, policy.AUDIT)).fetchone():
        raise ValueError('legacy receipt has execution policy')
    revision = receipt.get('revision', 0)
    if not stage_local and revision not in (0, 1):
        raise ValueError('receipt revision malformed')
    _verify_batch(evo, work_id, project, receipt.get('op_hash'),
                  receipt.get('review_hash'), changed)
    _verify_stages(evo, work_id, receipt.get('stages'), receipt.get('binding'))
    if not stage_local:
        _verify_history_stages(evo, work_id, revision, receipt.get('repair'),
                               receipt.get('history_stages'), receipt.get('binding'))
    required = receipt.get('owned_refs')
    if not isinstance(required, list) or len(set(required)) != len(required):
        raise ValueError('owned coverage malformed')
    stored_spans = []
    for part in receipt.get('window_sources') or []:
        if not isinstance(part, dict):
            raise ValueError('window source malformed')
        stored_spans.extend(part.get('spans') or [])
    _verify_window_sources(evo, project, receipt.get('window_sources'),
                           receipt.get('window_id'), stored_spans, required)
    _verify_owned_inputs(evo, work_id, project, receipt.get('owned_inputs'))
    for proof in receipt.get('read_cards') or []:
        _verify_card(evo, proof)
    if not isinstance(receipt.get('applied_cards'), list):
        raise ValueError('applied cards malformed')
    applied_ids = []
    for proof in receipt['applied_cards']:
        _verify_card(evo, proof)
        pid = proof.get('id')
        if applied.get(pid) != proof.get('version'):
            # The proven historical version must be EXACTLY the version the
            # completion recorded as applied for that card.
            raise ValueError('applied card version mismatch')
        applied_ids.append(pid)
    if sorted(applied_ids) != sorted(changed):
        raise ValueError('applied cards mismatch')
    _verify_dispositions(receipt.get('dispositions'),
                         _span_index(receipt.get('window_sources')), required)


def verify_completed(evo, work_id):
    """Verify a finished v2 row from its durable receipt; never writes/publishes.

    Returns a compact verified mapping (with the receipt). A normal executor
    may report ``processed=0`` / ``replayed=True``; a replay reads only.

    Completion has exactly ONE origin. When a unique OPERATOR completion audit
    exists the row is verified by ``delta_operator`` (a real operator exception
    completion, NOT automatic semantic/model success). A row carrying BOTH an
    automatic and an operator completion -- or a forged operator audit without
    its original meta -- is refused rather than guessed.
    """
    row = _row(evo, work_id)
    if row is None:
        raise ValueError('work missing')
    from . import dependency_maintenance
    if dependency_maintenance.has_completion(evo, work_id):
        return dependency_maintenance.verify_completed(evo, work_id)
    if _get(row, 'pipeline_protocol') != PROTOCOL:
        raise ValueError('protocol mismatch')
    if _get(row, 'state') != 'done':
        raise ValueError('work not done')
    from . import delta_operator
    operator_rows = evo.db.execute(
        "SELECT 1 FROM knowledge_failures WHERE work_id=? AND state=?",
        (work_id, delta_operator.OPERATOR_COMPLETE_AUDIT)).fetchone()
    if operator_rows is not None:
        # A mixed auto + operator completion can never be trusted: each origin
        # claims the same single finalize, so two records mean a forged or
        # double completion. The operator verifier itself also re-checks this.
        return delta_operator.verify_operator_completed(evo, work_id)
    receipt = _completion_audit(evo, work_id)
    _verify_receipt(evo, row, receipt)
    return {'verified': True, 'work_id': work_id, 'receipt': receipt,
            'processed': 0, 'replayed': True}
