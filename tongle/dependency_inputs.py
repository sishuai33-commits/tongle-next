"""Deterministic reading of synthetic knowledge-dependency observations.

``Evolution.queue_dependents`` captures one assistant event source per changed
dependency under the synthetic session ``knowledge-dependency``. Its text is a
fixed notice prefix plus the canonical JSON of
``{card_id, project, changed_basis:[{card_id, expected_version,
current_version, state}]}``. Nothing cites these new sources yet, so the
ordinary source-evidence mandatory scan cannot find the dependent target or
its changed basis. This module parses the observation strictly and returns the
referenced card ids so the caller can require the CURRENT authoritative cards
to be read in full (or explicitly block).

Pure data shaping: no writes, no model calls. The observation's
``expected_version``/``current_version`` fields are a historical notification
only; they are validated for shape but never trusted as live card state.
"""
import json

DEPENDENCY_SESSION = 'knowledge-dependency'
DEPENDENCY_PREFIX = (
    '知识依赖版本发生变化。请对照已更新的依据，核对原卡是否需要修正；'
    '不能把这条系统观察当作新的外部事实或用户决定。\n')

# Fixed, non-growing bounds on the observation shape.
MAX_OBSERVATION_CHARS = 64000
MAX_BASIS_ENTRIES = 32
MAX_ID_CHARS = 128
MAX_STATE_CHARS = 64
_MALFORMED = 'malformed_dependency_observation'


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'))


def _valid_id(value):
    return (isinstance(value, str) and value
            and len(value) <= MAX_ID_CHARS
            and not any(ord(c) < 32 or ord(c) == 127 for c in value))


def _valid_version(value):
    # A missing basis card is notified with a null current_version.
    return value is None or (
        isinstance(value, int) and not isinstance(value, bool) and value >= 0)


def _parse_observation(text, project):
    """Strictly parse one authoritative observation text.

    Returns ``(ids, error)``: ``ids`` is the deduplicated target-first list of
    referenced card ids, ``error`` a stable diagnostic label or None.
    """
    if not isinstance(text, str) or not text.startswith(DEPENDENCY_PREFIX):
        return None, 'bad_prefix'
    if len(text) > MAX_OBSERVATION_CHARS:
        return None, 'oversize'
    payload = text[len(DEPENDENCY_PREFIX):]
    try:
        data = json.loads(payload)
    except ValueError:
        return None, 'invalid_json'
    if not isinstance(data, dict):
        return None, 'not_object'
    if set(data) != {'card_id', 'project', 'changed_basis'}:
        return None, 'unexpected_shape'
    # The host emits canonical JSON; anything else (reordered keys, added
    # whitespace, duplicate keys that re-serialise differently) is not a
    # genuine observation and fails closed.
    if _canonical(data) != payload:
        return None, 'noncanonical'
    if not _valid_id(data.get('card_id')):
        return None, 'bad_card_id'
    if not isinstance(data.get('project'), str) or data['project'] != project:
        return None, 'project_mismatch'
    basis = data['changed_basis']
    if not isinstance(basis, list) or len(basis) > MAX_BASIS_ENTRIES:
        return None, 'bad_changed_basis'
    ids = [data['card_id']]
    for entry in basis:
        if (not isinstance(entry, dict)
                or set(entry) != {'card_id', 'expected_version',
                                  'current_version', 'state'}):
            return None, 'bad_basis_entry'
        if not _valid_id(entry['card_id']):
            return None, 'bad_basis_card_id'
        if not (_valid_version(entry['expected_version'])
                and _valid_version(entry['current_version'])):
            return None, 'bad_basis_version'
        state = entry['state']
        if not (state is None or (isinstance(state, str)
                                  and len(state) <= MAX_STATE_CHARS)):
            return None, 'bad_basis_state'
        if entry['card_id'] not in ids:
            ids.append(entry['card_id'])
    return ids, None


def dependency_observation_ids(project, inputs, source_lookup):
    """Collect mandatory card ids from knowledge-dependency input parts.

    ``inputs`` are the batch's source parts; ``source_lookup(source_id)``
    returns the authoritative full source dict (or None when unreadable). A
    part participates only when its authoritative source is an assistant
    ``event`` of the synthetic ``knowledge-dependency`` session, its current
    version equals the version the input carries, and its project matches.

    Returns ``(ids, problems)``. ``ids`` is a set of target/changed-basis card
    ids across ALL qualifying parts (deduplicated). A malformed synthetic
    observation never disappears silently and never expands scope: it yields a
    ``{'id': <source_id>, 'reason': 'malformed_dependency_observation'}``
    problem so the caller blocks before any model final. A source outside the
    synthetic session -- a normal conversation, even one whose text starts
    with the same prefix -- is ordinary data and is never parsed.
    """
    ids = set()
    problems = []
    seen_sources = set()
    for part in inputs or []:
        if not isinstance(part, dict):
            continue
        if part.get('role') != 'assistant' or part.get('kind') != 'event':
            continue
        sid = part.get('source_id')
        if not isinstance(sid, str) or not sid or sid in seen_sources:
            continue
        try:
            source = source_lookup(sid)
        except (KeyError, ValueError, OSError, TypeError):
            source = None
        if not isinstance(source, dict):
            continue
        if source.get('session') != DEPENDENCY_SESSION:
            continue
        seen_sources.add(sid)
        if source.get('project') != project:
            problems.append({'id': sid, 'reason': _MALFORMED})
            continue
        if source.get('role') != 'assistant' or source.get('kind') != 'event':
            continue
        if source.get('current_version') != part.get('version'):
            # A superseded input is rejected by the caller's own version gate;
            # never parse a stale notification as current state here.
            continue
        parsed, error = _parse_observation(source.get('text'), project)
        if error is not None:
            problems.append({'id': sid, 'reason': _MALFORMED})
            continue
        ids.update(parsed)
    return ids, problems[:8]
