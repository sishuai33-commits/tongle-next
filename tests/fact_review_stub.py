"""Test-only transport wrapper for the focused factual-audit gate.

The production ``Evolution._call`` runs a short focused FACT_REVIEW dispatch
immediately before the full verify task (inside the same bounded verify
slots). Legacy process-level tests drive the real machine through a synthetic
``tongle.worker.invoke_json`` responder whose replies are written for the old
compile/compare/verify sequence; that responder would otherwise consume the
extra audit dispatch or misparse its payload.

``with_fact_audit(responder)`` wraps such a responder. For EXACTLY the
focused-audit task it returns a deterministic, *structural* reply with one
``supported`` check per audited operation (a real non-empty body/title
substring plus a real non-empty exact source/old-body quote). For every other
payload it calls the original responder unchanged, so its return value,
exceptions and side effects are preserved exactly.

THIS IS A SYNTHETIC STRUCTURAL RESPONSE, NOT A SEMANTIC JUDGEMENT. The stub
fixtures are often intentionally irrelevant to one another, so the reply only
exists to exercise the downstream compile/compare/verify machinery of the old
tests; it makes no claim about actual factuality. Real faithfulness cases are
driven by real DS transports elsewhere, and dedicated ``test_fact_review``
covers rejection. Nothing here weakens a production assertion or skips the
gate: the host still validates coverage and exact substrings.
"""
import json

from tongle.fact_review import FACT_REVIEW_SCHEMA, FACT_REVIEW_TASK

# The exact focused-audit task string the gate puts in the dispatched payload.
FACT_AUDIT_TASK = FACT_REVIEW_TASK + FACT_REVIEW_SCHEMA

# Bounded so a synthetic quote/substring stays small; the host only requires an
# exact, non-empty substring, not any particular length.
_MAX_TEST_SUBSTRING = 200


def _fact_audit_payload(prompt):
    """Return the decoded payload iff it is exactly the focused-audit task."""
    try:
        payload = json.loads(prompt)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get('task') != FACT_AUDIT_TASK:
        return None
    return payload


def _nonempty_prefix(value):
    if not isinstance(value, str) or not value.strip():
        return ''
    return value[:_MAX_TEST_SUBSTRING]


def _first_nonempty_source_text(sources):
    for part in sources if isinstance(sources, list) else []:
        if isinstance(part, dict) and isinstance(part.get('text'), str) \
                and part['text'].strip():
            return part['text']
    return ''


def _synthetic_fact_audit_response(payload):
    """One structural ``supported`` check per audited create/update operation."""
    sources = payload.get('sources')
    fallback_quote = _first_nonempty_source_text(sources)
    checks = []
    for op in payload.get('operations') or []:
        if not isinstance(op, dict):
            continue
        # The host validates proposal_substring against title + '\n' + body.
        substring = (_nonempty_prefix(op.get('proposal_body'))
                     or _nonempty_prefix(op.get('proposal_title')))
        if not substring:
            continue
        # The old body is supplied verbatim as SOURCE evidence too; fall back to
        # any supplied source slice for a create with no old body.
        quote = _nonempty_prefix(op.get('old_body')) or _nonempty_prefix(fallback_quote)
        if not quote:
            continue
        checks.append({'op': op.get('op'),
                       'claim': 'synthetic structural check (not a judgement)',
                       'verdict': 'supported',
                       'proposal_substring': substring,
                       'source_quote': quote})
    return {'checks': checks, 'unsupported_count': 0, 'decision': 'pass'}


def with_fact_audit(responder):
    """Wrap an ``invoke_json`` responder for the focused-audit dispatch.

    ``responder(command, prompt, timeout, env=None)`` is the original synthetic
    transport. The returned callable answers only the exact focused-audit task
    itself (structurally) and delegates everything else unchanged.
    """
    def wrapped(command, prompt, timeout, env=None):
        payload = _fact_audit_payload(prompt)
        if payload is not None:
            return _synthetic_fact_audit_response(payload), {}
        return responder(command, prompt, timeout, env=env)
    return wrapped
