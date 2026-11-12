"""Exact local edits over a versioned inline note; all writes use InlineNotes.

No new ledger or alternate write path. Historical bases make native event
retries deterministic even after the first attempt advanced the note version.
"""
import json

from .inline_notes import InlineNotes, _body, _text

_FIELDS = {'id', 'expected_version', 'search_id', 'statement_changes',
           'condition_changes', 'add_conditions', 'evidence', 'revision_reason',
           'correction_event_ids'}


def _changes(value, name, limit, allow_delete=False):
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError(name + ' must be a bounded list')
    for change in value:
        if not isinstance(change, dict) or set(change) != {'old', 'new'}:
            raise ValueError(name + ' entries require exactly old and new')
        _text(change['old'], name + '.old', 6000 if name == 'statement_changes' else 400)
        new = change['new']
        if allow_delete and new is None:
            continue
        if not isinstance(new, str):
            raise ValueError(name + '.new must be text' + (' or null' if allow_delete else ''))
        if len(new) > (6000 if name == 'statement_changes' else 400):
            raise ValueError(name + '.new is too long')
    return value


def _statement(text, changes):
    spans = []
    for change in changes:
        old = change['old']
        start = text.find(old)
        # Search from start+1, so overlapping occurrences (aaa / aa) are
        # ambiguous too. Every edit refers to the original, never new text.
        if start < 0 or text.find(old, start + 1) >= 0:
            raise ValueError('statement old must match exactly once in the base version')
        spans.append((start, start + len(old), change['new']))
    spans.sort()
    if any(left[1] > right[0] for left, right in zip(spans, spans[1:])):
        raise ValueError('statement changes overlap in the base version')
    for start, end, new in reversed(spans):
        text = text[:start] + new + text[end:]
    return text


def _conditions(original, changes, additions):
    replacements = {}
    for change in changes:
        old = change['old']
        if original.count(old) != 1:
            raise ValueError('condition old must match exactly one complete base condition')
        index = original.index(old)
        if index in replacements:
            raise ValueError('condition changes target the same base condition')
        replacements[index] = change['new']
    result = [replacements.get(index, condition) for index, condition in enumerate(original)]
    return [condition for condition in result if condition is not None] + additions


def _base(notes, project, cid, version):
    current = notes.cards.get(cid)
    if current and current['project'] == 'shared' and project != 'shared':
        raise ValueError('shared cards are read-only')
    if not current or current['project'] != project:
        raise ValueError('card not found in project')
    if current['version'] == version:
        card = current
    else:
        rows = notes.db.execute('SELECT snapshot FROM knowledge_revisions WHERE card_id=? AND old_version=?',
                                (cid, version)).fetchall()
        if len(rows) != 1:
            raise ValueError('amend base version unavailable; read the note and use normal update')
        try:
            card = json.loads(rows[0]['snapshot'])
            card['evidence'] = json.loads(card['evidence'])
        except (TypeError, KeyError, ValueError) as exc:
            raise ValueError('invalid amend history; use normal update') from exc
    # Check the expected-version base, not later dependencies on the current
    # card: an already-applied event must remain a harmless replay.
    if isinstance(card, dict) and card.get('basis_cards'):
        raise ValueError('amend cannot preserve basis_cards; use an existing dependency-preserving maintenance path or explicit review')
    try:
        if (card['id'] != cid or card['project'] != project or card['version'] != version
                or not isinstance(card['body'], str) or len(card['body']) > 12000):
            raise ValueError('invalid or oversized base')
        meta = notes._meta(card)
        if not meta:
            raise ValueError('no verified inline metadata')
        base = {key: card[key] for key in ('key', 'title', 'topic')}
        base.update({key: meta[key] for key in
                     ('statement', 'conditions', 'scope', 'authority', 'status', 'content_type')})
        base.update(id=cid, expected_version=version, evidence=card['evidence'])
        # Reuse the existing limits and verify the complete body, not merely
        # its statement prefix. No unknown suffix or condition is discarded.
        notes._validate(base, 'update')
        if _body(base) != card['body']:
            raise ValueError('base cannot be reconstructed exactly')
        return base
    except (TypeError, KeyError, ValueError) as exc:
        raise ValueError('amend requires a bounded note with valid metadata; use normal update') from exc


def amend(engine, project, event_key, payload):
    """Expand exact local changes into one ordinary, fully guarded update."""
    if not isinstance(payload, dict) or set(payload) - _FIELDS:
        raise ValueError('invalid amend fields')
    _text(project, 'project', 200); _text(event_key, 'event_key', 200)
    cid = _text(payload.get('id'), 'id', 200)
    version = payload.get('expected_version')
    if type(version) is not int or version < 1:
        raise ValueError('invalid expected_version')
    search_id = _text(payload.get('search_id'), 'search_id', 200)
    reason = _text(payload.get('revision_reason'), 'revision_reason', 800)
    statements = _changes(payload.get('statement_changes', []), 'statement_changes', 12)
    conditions = _changes(payload.get('condition_changes', []), 'condition_changes', 8, True)
    additions = payload.get('add_conditions', [])
    if not isinstance(additions, list) or len(additions) > 8:
        raise ValueError('add_conditions must contain at most 8 conditions')
    additions = [_text(value, 'condition', 400) for value in additions]
    evidence = payload.get('evidence')
    if not isinstance(evidence, list) or not 1 <= len(evidence) <= 12:
        raise ValueError('amend requires 1 to 12 evidence entries')
    if not (statements or conditions or additions):
        raise ValueError('amend requires at least one local change')
    notes = InlineNotes(engine)
    with engine.locked():
        proposal = _base(notes, project, cid, version)
        proposal['statement'] = _statement(proposal['statement'], statements)
        proposal['conditions'] = _conditions(proposal['conditions'], conditions, additions)
        merged = list(proposal['evidence'])
        for ref in evidence:
            if ref not in merged:
                merged.append(ref)
        if len(merged) > 12:
            raise ValueError('amend would exceed 12 evidence entries; use normal update without silently dropping sources')
        proposal.update(search_id=search_id, evidence=merged, revision_reason=reason)
        if 'correction_event_ids' in payload:
            proposal['correction_event_ids'] = payload['correction_event_ids']
        # No transaction wraps publication. The existing path owns CAS,
        # manual/authority/correction guards, atomic metadata and event replay.
        return notes.update(project, event_key, proposal)
