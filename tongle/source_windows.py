"""Freeze host-verified source parts into stable evidence windows.

This module never claims provenance or semantic coverage; it only stabilizes
already-reconciled source parts into deterministic IDs and spans.

Hash values produced here are structural digests, NOT authentication. A
caller must independently confirm the live ledger, version, permission and
authorization state before trusting or acting on any window. Passing this
module's structural checks (including a successful rebuild) never constitutes
semantic approval, provenance verification, or an authorization decision.
"""

import hashlib
import json

PROTOCOL = "tongle-source-window-v1"
ROLES = ("user", "assistant", "document", "system", "tool")
RESPONSIBILITIES = ("owned", "reference")
DECISIONS = ("material", "covered", "nonmaterial", "unresolved")
REASON_MAX = 400

# The caller controls any dispatch/transport cap via ``max_chars``. The
# revalidation performed on rebuild does not impose an extra default cap, so a
# window frozen with a custom ``max_chars`` (e.g. > 32000) round-trips as long
# as the same cap is used.
PART_KEYS = (
    "source_id",
    "version",
    "part",
    "role",
    "kind",
    "text",
    "session",
    "ledger_captured_at",
    "ledger_version_at",
    "responsibility",
    "event_time",
    "source_bounds",
)
BOUNDS_KEYS = ("start", "end", "source_length", "text_start", "text_end", "partial")
DISPOSITION_KEYS = ("part_id", "decision", "span_ids", "reason")


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _is_str(v):
    return isinstance(v, str)


def _is_blank_str(v):
    return not _is_str(v) or v.strip() == ""


def _canonical_json(obj):
    """Serialize with a stable, canonical representation.

    ``sort_keys=True`` ignores JSON object key order; list order stays part of
    the contract; ``allow_nan=False`` rejects NaN/Infinity so distinct values can
    never collapse to the same payload. JSON ``false``/``0`` and ``true``/``1``
    serialize differently, so bool-vs-int confusion is rejected.
    """
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    )


def _canonical_hash(obj):
    return hashlib.sha256(_canonical_json(obj).encode("utf-8")).hexdigest()


def _text_sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _split_spans(text):
    """Split text on blank-line boundaries without losing or duplicating chars.

    A blank line is a maximal run of >=2 newline characters that may include
    spaces, tabs, and carriage returns between newlines (e.g. "\n\n",
    "\r\n\r\n", "\n  \n", "\n\r\n"). The blank-line content is kept as the trailing part of the preceding span. This guarantees that
    "".join(span texts) == original text.
    """
    if text == "":
        return [(0, 0, "")]

    spans = []
    n = len(text)
    i = 0
    seg_start = 0
    while i < n:
        ch = text[i]
        if ch in "\r\n":
            # Try to detect a blank-line boundary starting at i.
            j = i
            # consume first newline token (\r\n or \n or \r)
            if ch == "\r" and j + 1 < n and text[j + 1] == "\n":
                j += 2
            else:
                j += 1
            # consume optional whitespace then a second newline token -> blank line
            k = j
            while k < n and text[k] in " \t":
                k += 1
            if k < n and text[k] in "\r\n":
                # blank-line boundary found; consume through this second newline
                if text[k] == "\r" and k + 1 < n and text[k + 1] == "\n":
                    end = k + 2
                else:
                    end = k + 1
                spans.append((seg_start, end, text[seg_start:end]))
                seg_start = end
                i = end
                continue
            i = j
            continue
        i += 1
    if seg_start < n:
        spans.append((seg_start, n, text[seg_start:n]))
    if not spans:
        spans.append((0, n, text))
    return spans


def _opt_str(v):
    if v is None:
        return None
    if not _is_str(v):
        raise ValueError("optional metadata must be str or None")
    return v


def _normalize_event_time(v):
    """Event time is unknown in v1; capture timestamps are not event evidence."""
    if v is not None:
        raise ValueError("event_time requires a future grounded event-time protocol")
    return None


def _normalize_source_bounds(v):
    """Normalize explicit source bounds.

    ``None`` means "unknown bounds". Otherwise the value must be a dict with
    exactly ``start, end, source_length, text_start, text_end, partial``:
    true ints (not bool) for the offsets/lengths and a real bool for
    ``partial``, satisfying
    ``0 <= text_start <= start <= end <= text_end <= source_length``,
    ``len(text) == text_end - text_start``, and
    ``partial == (start > 0 or end < source_length)``.
    """
    if v is None:
        return None
    if not isinstance(v, dict):
        raise ValueError("source_bounds must be a dict or None")
    if set(v.keys()) != set(BOUNDS_KEYS):
        raise ValueError(
            "source_bounds must have exactly %s" % (BOUNDS_KEYS,)
        )
    for name in ("start", "end", "source_length", "text_start", "text_end"):
        if not _is_int(v[name]):
            raise ValueError("source_bounds.%s must be an int (not bool)" % (name,))
    if not isinstance(v["partial"], bool):
        raise ValueError("source_bounds.partial must be a bool")
    return {
        "start": v["start"],
        "end": v["end"],
        "source_length": v["source_length"],
        "text_start": v["text_start"],
        "text_end": v["text_end"],
        "partial": v["partial"],
    }


def _validate_source_bounds(bounds, text):
    if bounds is None:
        return
    if not (
        0 <= bounds["text_start"] <= bounds["start"] <= bounds["end"]
        <= bounds["text_end"] <= bounds["source_length"]
    ):
        raise ValueError("source_bounds ordering violated")
    if len(text) != bounds["text_end"] - bounds["text_start"]:
        raise ValueError("source_bounds text span does not match text length")
    expected_partial = bounds["start"] > 0 or bounds["end"] < bounds["source_length"]
    if bounds["partial"] != expected_partial:
        raise ValueError("source_bounds.partial inconsistent with offsets")


def _normalize_part(project, raw):
    if not isinstance(raw, dict):
        raise ValueError("part must be a dict")
    source_id = raw.get("source_id")
    if _is_blank_str(source_id):
        raise ValueError("source_id must be non-blank str")
    version = raw.get("version")
    if not _is_int(version) or version <= 0:
        raise ValueError("version must be positive int")
    part = raw.get("part")
    if not _is_int(part) or part < 0:
        raise ValueError("part must be non-negative int")
    role = raw.get("role")
    if role not in ROLES:
        raise ValueError("role must be one of %s" % (ROLES,))
    kind = raw.get("kind")
    if _is_blank_str(kind):
        raise ValueError("kind must be non-blank str")
    text = raw.get("text")
    if not _is_str(text):
        raise ValueError("text must be str")
    if "project" in raw and raw["project"] != project:
        raise ValueError("part project does not match window project")
    resp = raw.get("responsibility", "owned")
    if resp not in RESPONSIBILITIES:
        raise ValueError("responsibility must be owned/reference")
    bounds = _normalize_source_bounds(raw.get("source_bounds"))
    _validate_source_bounds(bounds, text)
    return {
        "source_id": source_id,
        "version": version,
        "part": part,
        "role": role,
        "kind": kind,
        "text": text,
        "session": _opt_str(raw.get("session")),
        "ledger_captured_at": _opt_str(raw.get("ledger_captured_at")),
        "ledger_version_at": _opt_str(raw.get("ledger_version_at")),
        "responsibility": resp,
        "event_time": _normalize_event_time(raw.get("event_time")),
        "source_bounds": bounds,
    }


def freeze_window(project, parts, max_chars=32000):
    if _is_blank_str(project):
        raise ValueError("project must be non-blank str")
    if not isinstance(parts, list) or not parts:
        raise ValueError("parts must be a non-empty list")
    if not _is_int(max_chars) or max_chars <= 0:
        raise ValueError("max_chars must be a positive int")

    normalized = []
    seen = set()
    total = 0
    for raw in parts:
        np = _normalize_part(project, raw)
        key = (np["source_id"], np["version"], np["part"])
        if key in seen:
            raise ValueError("duplicate (source_id,version,part): %r" % (key,))
        seen.add(key)
        total += len(np["text"])
        normalized.append(np)
    if total > max_chars:
        raise ValueError(
            "total_chars %d exceeds max_chars %d" % (total, max_chars)
        )

    # Hash every normalized metadata field by hashing the normalized parts
    # directly (no hand-picked subset).
    window_id = _canonical_hash({"project": project, "parts": normalized})

    parts_out = []
    spans_out = []
    for idx, p in enumerate(normalized):
        part_id = _canonical_hash({"window_id": window_id, "index": idx})
        text = p["text"]
        tsha = _text_sha256(text)
        span_ids = []
        for (start, end, chunk) in _split_spans(text):
            span_id = _canonical_hash(
                {
                    "window_id": window_id,
                    "part_id": part_id,
                    "start": start,
                    "end": end,
                    "text_sha256": _text_sha256(chunk),
                }
            )
            span_ids.append(span_id)
            spans_out.append(
                {
                    "id": span_id,
                    "part_id": part_id,
                    "start": start,
                    "end": end,
                    "text": chunk,
                }
            )
        part_out = {k: p[k] for k in PART_KEYS}
        part_out["part_id"] = part_id
        part_out["text_sha256"] = tsha
        part_out["span_ids"] = span_ids
        parts_out.append(part_out)

    return {
        "protocol": PROTOCOL,
        "project": project,
        "window_id": window_id,
        "parts": parts_out,
        "spans": spans_out,
    }


def _window_from_supplied(window):
    """Extract only the identity/metadata fields a caller is allowed to feed
    back into ``freeze_window``. Unknown legacy input keys are ignored here;
    the full-shape comparison in ``_rebuild_window`` is what enforces that the
    supplied window has no extra or missing keys.
    """
    project = window.get("project")
    raw_parts = window.get("parts")
    if _is_blank_str(project):
        raise ValueError("window.project invalid")
    if not isinstance(raw_parts, list) or not raw_parts:
        raise ValueError("window.parts invalid")
    stripped = []
    for p in raw_parts:
        if not isinstance(p, dict):
            raise ValueError("window parts must be dicts")
        stripped.append(
            {
                "source_id": p.get("source_id"),
                "version": p.get("version"),
                "part": p.get("part"),
                "role": p.get("role"),
                "kind": p.get("kind"),
                "text": p.get("text"),
                "session": p.get("session"),
                "ledger_captured_at": p.get("ledger_captured_at"),
                "ledger_version_at": p.get("ledger_version_at"),
                "responsibility": p.get("responsibility", "owned"),
                "event_time": p.get("event_time"),
                "source_bounds": p.get("source_bounds"),
            }
        )
    return project, stripped


def _rebuild_window(window):
    """Rebuild a window from scratch and require an exact canonical match.

    The caller-supplied window is re-frozen from its project + part identity
    fields + text, then compared to the supplied object via canonical JSON
    (order- and type-sensitive, ``allow_nan=False``). This rejects extra,
    omitted, reordered, duplicated or retyped fields anywhere in the window,
    its parts and its spans; in particular a supplied ``false`` never passes
    for a rebuilt ``0``. The window is rebuilt with the part-identity-derived
    ``max_chars`` implied by its own text, so no extra dispatch cap is applied
    during revalidation (the caller controls any cap at freeze time).
    """
    if not isinstance(window, dict):
        raise ValueError("window must be a dict")
    project, stripped = _window_from_supplied(window)

    # No revalidation-time cap: allow the window's own total length.
    total = sum(len(p["text"]) for p in stripped if _is_str(p["text"]))
    rebuilt = freeze_window(project, stripped, max_chars=max(total, 1))

    try:
        supplied_json = _canonical_json(window)
        rebuilt_json = _canonical_json(rebuilt)
    except (TypeError, ValueError) as exc:
        raise ValueError("window is not JSON-canonicalizable: %s" % (exc,))
    if supplied_json != rebuilt_json:
        raise ValueError(
            "window does not exactly match its rebuilt canonical form"
        )
    return rebuilt


def resolve_evidence(window, span_ids):
    rebuilt = _rebuild_window(window)
    if not isinstance(span_ids, list) or not span_ids:
        raise ValueError("span_ids must be a non-empty list")
    seen = set()
    span_map = {s["id"]: s for s in rebuilt["spans"]}
    part_map = {p["part_id"]: p for p in rebuilt["parts"]}
    out = []
    for sid in span_ids:
        if not _is_str(sid) or sid == "":
            raise ValueError("span id must be non-empty str")
        if sid in seen:
            raise ValueError("duplicate span id: %s" % (sid,))
        seen.add(sid)
        if sid not in span_map:
            raise ValueError("span id not in window: %s" % (sid,))
        s = span_map[sid]
        if s["text"] == "" or s["text"].strip() == "":
            raise ValueError("blank/whitespace-only span cannot serve as evidence")
        p = part_map[s["part_id"]]
        out.append(
            {
                "source_id": p["source_id"],
                "version": p["version"],
                "quote": s["text"],
            }
        )
    return out


def validate_dispositions(window, dispositions):
    rebuilt = _rebuild_window(window)
    if not isinstance(dispositions, list):
        raise ValueError("dispositions must be a list")
    parts = rebuilt["parts"]
    part_map = {p["part_id"]: p for p in parts}
    span_map = {s["id"]: s for s in rebuilt["spans"]}
    if len(dispositions) != len(parts):
        raise ValueError("every part must have exactly one disposition")
    normalized = []
    seen_parts = set()
    for d in dispositions:
        if not isinstance(d, dict):
            raise ValueError("disposition must be a dict")
        if set(d.keys()) != set(DISPOSITION_KEYS):
            raise ValueError(
                "disposition must have exactly %s" % (DISPOSITION_KEYS,)
            )
        pid = d.get("part_id")
        if not _is_str(pid) or pid == "":
            raise ValueError("disposition.part_id must be non-empty str")
        if pid not in part_map:
            raise ValueError("unknown part_id: %s" % (pid,))
        if pid in seen_parts:
            raise ValueError("duplicate disposition for part: %s" % (pid,))
        seen_parts.add(pid)
        decision = d.get("decision")
        if decision not in DECISIONS:
            raise ValueError("invalid decision: %r" % (decision,))
        reason = d.get("reason")
        if _is_blank_str(reason):
            raise ValueError("reason must be non-blank str")
        if len(reason) > REASON_MAX:
            raise ValueError("reason exceeds %d chars" % (REASON_MAX,))
        dsids = d.get("span_ids")
        if not isinstance(dsids, list):
            raise ValueError("disposition.span_ids must be a list")
        seen_s = set()
        for sid in dsids:
            if not _is_str(sid) or sid == "":
                raise ValueError("disposition span id must be non-empty str")
            if sid in seen_s:
                raise ValueError("duplicate span id in disposition")
            seen_s.add(sid)
            if sid not in span_map:
                raise ValueError("span id not in window: %s" % (sid,))
            if span_map[sid]["part_id"] != pid:
                raise ValueError("span does not belong to part")
        p = part_map[pid]
        nonblank = p["text"] != "" and p["text"].strip() != ""
        if nonblank:
            if not dsids:
                raise ValueError("non-blank part requires at least one span")
            if all(span_map[s]["text"].strip() == "" for s in dsids):
                raise ValueError("whitespace-only spans are not evidence")
        # empty/whitespace-only part may use []
        normalized.append(
            {
                "part_id": pid,
                "decision": decision,
                "span_ids": list(dsids),
                "reason": reason,
            }
        )
    return {"valid": True, "resolved": False, "parts": normalized}
