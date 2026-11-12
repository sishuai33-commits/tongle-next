"""Candidate context adapter for the v2 (isolated delta) pipeline.

This module pins exactly ONE complete source event per v2 job, freezes it
(together with an optional bounded set of read-only prior same-session
conversation events) into a real ``source_windows`` window, selects bounded
local candidate recall via the proven consumer ranking helper, and returns the
genuine ``delta_contract.build_context`` result: the frozen ``window``, the
selected ``cards`` and their ``binding`` hash.

Contract:
  * A v2 job owns exactly one (source_id, version) with ALL of its existing
    ``_source_parts`` (one real source may have several parts). A partial
    frozen input, an unowned part, or a part owned by another job cannot be
    processed: it raises CoverageWait.
  * The owned full event is frozen once (even when longer than 10000 chars).
    It is never truncated, trimmed or redacted here.
  * The frozen window (owned event plus read-only prior references) is bound
    by FULL_SOURCE_LIMIT total source characters. References are included
    whole or not at all -- never truncated.
  * Every frozen part carries the source's OWN real ``ledger_captured_at`` /
    ``ledger_version_at`` timestamps; ``event_time`` stays unknown. At most
    MAX_PRIOR_REFS prior events of the SAME project+session with a parseable
    capture time ordered strictly before the owned event are attached
    (chronologically, oldest first). If the owned capture time is unusable no
    prior order can be trusted, so NO reference is attached.
  * Recall is a bounded *hint* over ``_authorized_catalog``: at most
    RECALL_CARD_LIMIT whole card bodies within RECALL_CHAR_LIMIT characters.
    It is never a claim of global semantic uniqueness or completeness.
  * Reads only. No model calls. No network. No writes.
"""

from __future__ import annotations

import datetime as _dt
import json

from . import delta_contract, delta_migration, source_windows
from .evolution import CoverageWait, SelectionChanged
from .context_retrieval import (
    is_acknowledgement,
    is_continuation,
    is_referential_followup,
    rank_consumer,
    specific_terms,
)

# Frozen full-source character budget used by the v2 window identity.
FULL_SOURCE_LIMIT = 32000

# Bounded candidate recall budget.
RECALL_CHAR_LIMIT = 80000
RECALL_CARD_LIMIT = 6

# Total context hard cap for one frozen context, taken from the contract so it
# can never drift above what ``delta_contract.build_context`` will accept.
# ROOT-reviewed migration read hints are mandatory and counted first; ordinary
# bounded recall fills only the remaining count/character budget.
CONTEXT_CARD_CAP = delta_contract.CARD_COUNT_CAP
CONTEXT_CHAR_CAP = delta_contract.CARD_BODY_TOTAL_CAP

# Exact audit shape written by ``delta_migration.apply`` for a migrated child.
_CHILD_AUDIT_STATE = delta_migration.CHILD_AUDIT
_CHILD_AUDIT_FIELDS = (
    "grant_id", "plan_sha", "parent", "work_id", "project", "source_id",
    "version", "text_sha", "required_cards",
)
_CARD_HINT_FIELDS = ("id", "version", "body_sha")

# At most two read-only prior conversation references.
MAX_PRIOR_REFS = 2

# The owned source must be a real conversation event.
_OWNED_KINDS = ("event",)
_OWNED_ROLES = ("user", "assistant")


# ----------------------------------------------------------------- helpers

def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _aware(ts):
    """Parse an ISO timestamp to an aware datetime; None if unusable."""
    if not isinstance(ts, str) or not ts:
        return None
    try:
        dt = _dt.datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        return None
    return dt


def _ts(ts):
    dt = _aware(ts)
    if dt is None:
        return None
    return dt.timestamp()


def _part(source, part, responsibility):
    """One freeze_window part carrying REAL ledger timestamps.

    ``ledger_captured_at``/``ledger_version_at`` are the source's own stored
    capture/version timestamps -- the only ones the host owns. They are never
    an invented or caller-supplied ordering time, and ``event_time`` stays
    unknown (None) because no grounded event time exists in v1.
    """
    return {
        "source_id": part["source_id"],
        "version": part["version"],
        "part": part["part"],
        "role": part.get("role", source.get("role")),
        "kind": part.get("kind", source.get("kind")),
        "text": part["text"],
        "source_bounds": {
            "start": 0, "end": len(part["text"]),
            "text_start": 0, "text_end": len(part["text"]),
            "source_length": len(part["text"]), "partial": False,
        },
        "session": source.get("session"),
        "ledger_captured_at": source.get("created_at"),
        "ledger_version_at": source.get("version_created_at"),
        "responsibility": responsibility,
    }


def _owned_parts(source, live):
    # Legacy chunks prove ownership only. Their overlapping 300-character
    # context must never become repeated responsibility in the new window.
    return [_part(source, {
        "source_id": source["source_id"], "version": source["version"],
        "part": 0, "text": source["text"],
    }, "owned")]


# ------------------------------------------------------------------- inputs

def _classify_inputs_error(exc):
    """Translate a raw ``_inputs`` ValueError into the v2 failure family.

    A superseded/changed source is a selection change (bounded rebuild); an
    unavailable/revoked/missing source is a coverage wait. Any other message
    is preserved verbatim rather than silently narrowed.
    """
    message = str(exc)
    if "superseded" in message or "version" in message:
        return SelectionChanged(message or "owned source changed version")
    if ("unavailable" in message or "revoked" in message
            or "missing" in message):
        return CoverageWait(message or "owned source unavailable")
    return exc


def _resolve_owned_part(evo, job):
    """Validate raw inputs and resolve the single owned source+version.

    Returns ``(source, provided_parts, live_parts)``. ``provided_parts`` is
    the frozen input list; ``live_parts`` is the complete recomputed source
    decomposition. Raises CoverageWait/SelectionChanged for v2 violations.
    """
    try:
        provided = evo._inputs(job)
    except ValueError as exc:
        raise _classify_inputs_error(exc)
    except KeyError:
        raise CoverageWait("owned source unavailable")

    if not isinstance(provided, list) or not provided:
        raise CoverageWait("v2 requires a complete single source event")

    seen = set()
    distinct = set()
    for entry in provided:
        if not isinstance(entry, dict):
            raise CoverageWait("malformed owned part entry")
        sid = entry.get("source_id")
        ver = entry.get("version")
        if not isinstance(sid, str) or not sid or not _is_int(ver) or ver < 1:
            raise CoverageWait("malformed owned part entry")
        key = (sid, ver, entry.get("part"))
        if key in seen:
            raise CoverageWait("duplicate owned part entry")
        seen.add(key)
        resp = entry.get("responsibility")
        if resp not in (None, "owned"):
            # references are acquired independently below
            raise CoverageWait("v2 requires a complete single source event")
        distinct.add((sid, ver))

    if len(distinct) != 1:
        raise CoverageWait("v2 requires a complete single source event")

    sid, ver = next(iter(distinct))
    try:
        source = evo.engine.store.source(sid, ver)
    except (KeyError, ValueError):
        raise CoverageWait("owned source unavailable")

    if not evo.engine.source_available(sid):
        raise CoverageWait("owned source unavailable")

    if source.get("kind") not in _OWNED_KINDS:
        raise CoverageWait("unsupported owned source kind")
    if source.get("role") not in _OWNED_ROLES:
        raise CoverageWait("unsupported owned source role")
    if not isinstance(source.get("project"), str) or not source["project"]:
        raise CoverageWait("owned source missing project")
    if source.get("session") is not None and not isinstance(source["session"], str):
        raise CoverageWait("owned source session malformed")

    # Permission/scope: the job's project/session must match the owned source.
    job_project = job.get("project")
    job_session = job.get("session")
    if job_project is not None and source.get("project") != job_project:
        raise SelectionChanged("owned source changed project")
    if job_session is not None and source.get("session") != job_session:
        raise SelectionChanged("owned source changed session")

    if _is_int(source.get("current_version")) and source["current_version"] != ver:
        raise SelectionChanged("owned source changed version")

    live = list(evo._source_parts(source))
    if not live:
        raise CoverageWait("owned source has no parts")
    return source, provided, live


def _match_owned_parts(source, provided, live):
    """Prove the frozen inputs cover ALL recomputed parts exactly.

    Missing/extra parts are a partial (structural) violation; a same-part
    field or text change is a selection change.
    """
    expected_by_part = {}
    for p in live:
        expected_by_part[p["part"]] = {
            "source_id": source["source_id"],
            "version": source["version"],
            "part": p["part"],
            "start": p["start"],
            "end": p["end"],
            "source_length": p["source_length"],
            "text": p["text"],
            "partial": p["partial"],
            "role": p.get("role", source.get("role")),
            "kind": p.get("kind", source.get("kind")),
        }

    provided_by_part = {}
    for entry in provided:
        part = entry.get("part")
        if not _is_int(part):
            raise SelectionChanged("owned source changed")
        if part in provided_by_part:
            raise CoverageWait("duplicate owned part entry")
        provided_by_part[part] = entry

    if set(provided_by_part) != set(expected_by_part):
        raise CoverageWait("v2 requires a complete single source event")

    for part, exp in expected_by_part.items():
        got = provided_by_part[part]
        for field in ("source_id", "version"):
            gv = got.get(field)
            if gv != exp[field] or isinstance(gv, bool):
                raise SelectionChanged("owned source changed")
        for field in ("part", "start", "end", "source_length"):
            gv = got.get(field)
            if not _is_int(gv) or gv != exp[field]:
                raise SelectionChanged("owned source changed")
        gv = got.get("partial")
        if not isinstance(gv, bool) or gv != exp["partial"]:
            raise SelectionChanged("owned source changed")
        if got.get("text") != exp["text"]:
            raise SelectionChanged("owned source changed")
        if got.get("role") != exp["role"]:
            raise SelectionChanged("owned source changed")
        if got.get("kind") != exp["kind"]:
            raise SelectionChanged("owned source changed")


def _verify_owner(evo, job, source, live):
    """Every part of the owned source must be booked to this job."""
    sid = source["source_id"]
    ver = source["version"]
    rows = evo.db.execute(
        "SELECT part, work_id FROM knowledge_seen WHERE source_id=? AND version=?",
        (sid, ver),
    ).fetchall()
    owners = {row["part"]: row["work_id"] for row in rows}
    job_id = job.get("id")
    if not isinstance(job_id, str) or not job_id:
        raise CoverageWait("v2 job has no identity")
    for part in live:
        owner = owners.get(part["part"], "__absent__")
        if owner == "__absent__":
            raise CoverageWait("owned source part not yet booked")
        if owner != job_id:
            raise CoverageWait("owned source part owned by another job")


# ------------------------------------------------------------- references

def _prior_references(evo, owned, budget):
    """At most two read-only prior same-session conversation events.

    ``budget`` is the remaining source-char allowance after the owned event.
    Each admitted reference is whole (never truncated); a reference that does
    not fit the remaining allowance is skipped entirely. Each returned
    reference is a freeze_window part (single part 0) with the reference's own
    real ledger timestamps.
    """
    session = owned.get("session")
    project = owned.get("project")
    if not isinstance(session, str) or not session:
        return []
    if not isinstance(project, str) or not project:
        return []

    owned_ts = _ts(owned.get("created_at"))
    if owned_ts is None:
        # The owned source's capture time is unusable, so no reliable prior
        # order exists. Do not guess: exclude every reference.
        return []

    rows = evo.db.execute(
        "SELECT id, project, session, kind, role, created_at FROM sources"
        " WHERE project=? AND session=?",
        (project, session),
    ).fetchall()

    candidates = []
    for row in rows:
        sid = row["id"]
        if sid == owned["source_id"]:
            continue
        if row["project"] != project or row["session"] != session:
            continue
        if row["kind"] != "event":
            continue
        if row["role"] not in _OWNED_ROLES:
            continue
        if not evo.engine.source_available(sid):
            continue
        ts = _ts(row["created_at"])
        if ts is None or ts >= owned_ts:
            # Unusable time cannot be placed chronologically, and a later or
            # simultaneous event is not a prior reference: exclude both rather
            # than guess an ordering.
            continue
        candidates.append((ts, sid))

    # Newest first, tie-break by id; admit in that order until two whole
    # references fit the remaining budget. A reference that cannot fit is
    # skipped whole (never truncated) and the next candidate is considered.
    candidates.sort(key=lambda t: (-t[0], t[1]))

    refs = []
    for ref_ts, sid in candidates:
        if len(refs) >= MAX_PRIOR_REFS:
            break
        try:
            src = evo.engine.store.source(sid)
        except (KeyError, ValueError):
            continue
        text = src.get("text")
        if not isinstance(text, str):
            continue
        if src.get("role") not in _OWNED_ROLES:
            continue
        if _ts(src.get("created_at")) is None:
            continue
        if budget - len(text) < 0:
            # Would exceed the frozen window budget if included whole; skip
            # rather than truncate.
            continue
        budget -= len(text)
        refs.append((ref_ts, _part(src, {
            "source_id": src["source_id"],
            "version": src["version"],
            "part": 0,
            "role": src.get("role"),
            "kind": src.get("kind"),
            "text": text,
        }, "reference")))

    # Chronological (oldest first), owned appended last by the caller.
    refs.sort(key=lambda t: (t[0], t[1]["source_id"]))
    return [part for _t, part in refs]


# --------------------------------------------------- migration read hints

def _child_audit(evo, job):
    """Read the single ``delta_migration_child_v1`` audit for this job, if any.

    A job with no such row (an ordinary, non-migrated v2 child) returns ``None``
    and takes the completely unchanged ordinary path. More than one row, or a
    row that is not exactly the expected body-free audit object, is rejected as
    a coverage wait: a duplicate/ambiguous or malformed provenance record can
    never silently downgrade a migrated child to ordinary recall.
    """
    job_id = job.get("id")
    if not isinstance(job_id, str) or not job_id:
        raise CoverageWait("v2 job has no identity")
    rows = evo.db.execute(
        "SELECT error FROM knowledge_failures WHERE work_id=? AND state=?",
        (job_id, _CHILD_AUDIT_STATE),
    ).fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        raise CoverageWait("ambiguous migration child audit")
    raw = rows[0]["error"]
    try:
        audit = json.loads(raw)
    except (ValueError, TypeError):
        raise CoverageWait("migration child audit malformed")
    if not isinstance(audit, dict) or set(audit.keys()) != set(_CHILD_AUDIT_FIELDS):
        raise CoverageWait("migration child audit malformed")
    return audit


def _migration_required(evo, job, source):
    """Resolve ROOT-reviewed mandatory full-card read hints for one child.

    The child's body-free audit is cross-validated against the authoritative
    ``delta_migration_v1:<grant_id>`` grant receipt: the retained body-free plan
    must re-validate, the plan's SHA must agree with the audit, the receipt's
    request must equal the plan's request, and exactly one plan child must name
    this job with a source/project/session/version identical to the current
    owned event. The expected audit fields are DERIVED from that authoritative
    child (never trusted from the child audit or its self-hash alone) and must
    match exactly in canonical form.

    Required card IDs are then resolved against the CURRENT authorized catalog
    for this project each build. A required card may legitimately have advanced
    version/body since migration (a sibling migrated process may have updated
    it): the migration version/body digest is lineage, not a permanent pin, so
    the *current* card body is what is frozen here. A required target that is
    missing, revoked or out of scope is an explicit CoverageWait -- it is never
    silently dropped. The returned list preserves the deterministic audit order
    and contains complete card bodies only.
    """
    audit = _child_audit(evo, job)
    if audit is None:
        return []

    grant_id = audit.get("grant_id")
    if not isinstance(grant_id, str) or not grant_id:
        raise CoverageWait("migration child audit malformed")

    key = delta_migration.MIGRATION_META_PREFIX + grant_id
    row = evo.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    if row is None:
        raise CoverageWait("migration grant receipt missing")
    try:
        receipt = json.loads(row["value"])
    except (ValueError, TypeError):
        raise CoverageWait("migration grant receipt malformed")
    if not isinstance(receipt, dict):
        raise CoverageWait("migration grant receipt malformed")
    if receipt.get("grant_id") != grant_id:
        raise CoverageWait("migration grant receipt mismatch")

    plan = receipt.get("plan")
    try:
        delta_migration._validate_plan_schema(plan)
    except (ValueError, TypeError):
        raise CoverageWait("migration grant plan invalid")
    if receipt.get("plan_sha") != plan.get("plan_sha"):
        raise CoverageWait("migration grant plan mismatch")
    if receipt.get("plan_sha") != audit.get("plan_sha"):
        raise CoverageWait("migration child audit plan mismatch")
    if (delta_migration._canon(receipt.get("request"))
            != delta_migration._canon(plan.get("request"))):
        raise CoverageWait("migration grant request mismatch")

    job_id = job.get("id")
    matches = [c for c in (plan.get("children") or [])
               if isinstance(c, dict) and c.get("child_id") == job_id]
    if len(matches) != 1:
        raise CoverageWait("migration child not uniquely identified in plan")
    child = matches[0]
    if child.get("source_id") != source.get("source_id"):
        raise CoverageWait("migration child source mismatch")
    if child.get("project") != source.get("project"):
        raise CoverageWait("migration child project mismatch")
    if child.get("session") != source.get("session"):
        raise CoverageWait("migration child session mismatch")
    if child.get("version") != source.get("version"):
        raise CoverageWait("migration child version mismatch")

    # Expected audit fields are DERIVED from the authoritative plan child; the
    # stored audit must equal them exactly (canonical form, every key).
    expected = {
        "grant_id": receipt.get("grant_id"),
        "plan_sha": plan.get("plan_sha"),
        "parent": child.get("parent"),
        "source_id": child.get("source_id"),
        "version": child.get("version"),
        "text_sha": child.get("text_sha"),
        "work_id": child.get("child_id"),
        "project": child.get("project"),
        "required_cards": child.get("required_cards"),
    }
    if delta_migration._canon(audit) != delta_migration._canon(expected):
        raise CoverageWait("migration child audit mismatch")

    hints = child.get("required_cards")
    if not isinstance(hints, list):
        raise CoverageWait("migration child audit malformed")

    catalog = evo._authorized_catalog(job.get("project")) if job.get("project") is not None else []
    by_id = {}
    for card in catalog:
        cid = card.get("id")
        if isinstance(cid, str) and cid not in by_id:
            by_id[cid] = card

    required = []
    seen = set()
    chars = 0
    for hint in hints:
        if not isinstance(hint, dict) or set(hint.keys()) != set(_CARD_HINT_FIELDS):
            raise CoverageWait("migration child audit malformed")
        cid = hint.get("id")
        if not isinstance(cid, str) or not cid:
            raise CoverageWait("migration child audit malformed")
        if cid in seen:
            continue
        card = by_id.get(cid)
        if card is None:
            # Missing, revoked or out-of-scope: an explicit wait, never a silent
            # drop of a mandatory read hint.
            raise CoverageWait("required card not authorized: %s" % cid)
        body = card.get("body")
        if not isinstance(body, str):
            raise CoverageWait("required card body unavailable: %s" % cid)
        seen.add(cid)
        required.append(card)
        chars += len(body)
        if len(required) > CONTEXT_CARD_CAP:
            raise CoverageWait("required cards exceed context cap")
        if chars > CONTEXT_CHAR_CAP:
            raise CoverageWait("required card bodies exceed context cap")
    return required


# ---------------------------------------------------------------- recall

def _recall(evo, job, owned_text, reference_texts, required=None):
    """Bounded local candidate recall; complete card bodies only.

    ``required`` are the deterministic, mandatory migration read hints (in
    audit order); they are always included whole and consume the count/character
    budget first. Ordinary ranked recall then fills only the remaining budget
    (at most :data:`RECALL_CARD_LIMIT` optional cards, total context at most
    :data:`CONTEXT_CARD_CAP` cards / :data:`CONTEXT_CHAR_CAP` characters),
    skipping any card already present as a required hint.

    This is a bounded *hint* over the authorized catalog, never a claim of
    global semantic uniqueness or completeness.
    """
    query = owned_text
    if (is_referential_followup(owned_text)
            or is_continuation(owned_text)
            or is_acknowledgement(owned_text)):
        for ref_text in reference_texts:
            query = query + "\n" + ref_text

    specific = specific_terms(query)
    project = job.get("project")
    catalog = evo._authorized_catalog(project) if project is not None else []

    def is_own_lookup(card):
        return card.get("project") == project

    ranked = rank_consumer(
        catalog,
        query,
        specific,
        is_own_lookup,
        identity=None,
    )

    selected = list(required) if required else []
    seen = {card.get("id") for card in selected}
    total = sum(len(card["body"]) for card in selected)
    optional = 0
    for card in ranked:
        if optional >= RECALL_CARD_LIMIT:
            break
        if len(selected) >= CONTEXT_CARD_CAP:
            break
        if card.get("id") in seen:
            continue
        body = card.get("body")
        if not isinstance(body, str):
            continue
        if total + len(body) > RECALL_CHAR_LIMIT:
            continue
        selected.append(card)
        seen.add(card.get("id"))
        total += len(body)
        optional += 1
    return selected


def build_live_context(evo, job):
    """Build the real ``delta_contract`` context for one v2 job.

    Pins the complete owned source event, optionally attaches bounded
    read-only prior same-session conversation references, runs bounded recall,
    then freezes the window (``source_windows.freeze_window``) and validates it
    into the bound contract context (``delta_contract.build_context``). The
    returned object is the actual contract context: ``protocol``, ``window``,
    ``cards`` and a ``binding`` hash over them -- never an intermediate raw
    window dict.
    """
    if not isinstance(job, dict):
        raise CoverageWait("v2 job must be a mapping")
    source, provided, live = _resolve_owned_part(evo, job)
    _match_owned_parts(source, provided, live)
    _verify_owner(evo, job, source, live)

    if not isinstance(source.get("text"), str):
        raise CoverageWait("owned source text unavailable")

    # ROOT-reviewed mandatory read hints for a migrated child, resolved against
    # the current owned event and current catalog BEFORE any selection. An
    # ordinary job returns an empty list and is completely unchanged.
    required = _migration_required(evo, job, source)

    owned_parts = _owned_parts(source, live)
    owned_total = sum(len(p["text"]) for p in owned_parts)
    if owned_total > FULL_SOURCE_LIMIT:
        raise CoverageWait("owned source exceeds full-source limit")

    references = _prior_references(evo, source, FULL_SOURCE_LIMIT - owned_total)
    parts = references + owned_parts
    window = source_windows.freeze_window(
        source["project"], parts, max_chars=FULL_SOURCE_LIMIT)

    selected = _recall(
        evo, job, source["text"], [r["text"] for r in references], required)
    # Revoked/unavailable sources can no longer ground anything new. A card
    # selected for context may still carry provenance tuples that cite such a
    # source; mark those entries so the contract keeps them frozen in the
    # snapshot (bytes and binding stay honest) but never inherits them into a
    # fresh operation's evidence. The marker list is part of the frozen
    # context, so compile, review-recompile and publication all observe the
    # exact same exclusion set.
    for card in selected:
        if not isinstance(card, dict):
            continue
        evidence = card.get("evidence")
        if not isinstance(evidence, list):
            continue
        unavailable = [
            e for e in evidence
            if isinstance(e, dict)
            and isinstance(e.get("source_id"), str)
            and not evo.engine.source_available(e["source_id"])
        ]
        if unavailable:
            card["unavailable_evidence"] = unavailable
    return delta_contract.build_context(window, selected)
