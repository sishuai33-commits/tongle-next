"""Card delta protocol v1: freeze a card into segments and apply local edits.

Pure functions, stdlib only. No DB/network/model access.

Structural digests and rebuilds here are NOT authentication. A caller must
independently confirm the live ledger, version, permission and authorization
state before trusting any snapshot or applying any delta. Passing a rebuild or
a hash check never means the edit is semantically approved or authorized.
"""

import hashlib
import json
import re

PROTOCOL = "tongle-card-delta-v1"
MAX_BODY_LEN = 60000
MAX_EDITS = 8
VALID_STATES = ("active", "conflict", "retired")
_WS_ONLY = re.compile(r"^\s*$")
EDIT_KEYS = ("op", "segment_id", "text")
SEGMENT_KEYS = ("id", "start", "end", "text")


def _canonical_json(obj):
    """Canonical JSON preserving list order; rejects NaN/Infinity and never lets
    JSON ``false``/``true`` collapse onto ``0``/``1``."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    )


class _CardError(ValueError):
    """ValueError subclass for all contract violations."""

    pass


def _sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_digest(*parts):
    raw = "\x1f".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _require_nonempty_str(value, name):
    if not isinstance(value, str) or not value or _WS_ONLY.match(value):
        raise _CardError(f"{name} must be a non-empty non-whitespace string")
    return value


def _require_opt_str_or_null(value, name):
    if value is None:
        return None
    if not isinstance(value, str) or not value or _WS_ONLY.match(value):
        raise _CardError(f"{name} must be null or a non-empty non-whitespace string")
    return value


def _split_segments(body):
    """Split on blank lines; every char covered exactly once, no trim."""
    segments = []
    pos = 0
    start = 0
    while pos < len(body):
        if body[pos] == "\n":
            nl_end = pos + 1
            if body[pos:pos + 2] == "\r\n":
                nl_end = pos + 2
            p = nl_end
            while p < len(body) and body[p] in " \t\r":
                p += 1
            blank = False
            if p >= len(body):
                blank = True
                sep_end = len(body)
            elif body[p] == "\n":
                blank = True
                sep_end = p + 1
                if body[p:p + 2] == "\r\n":
                    sep_end = p + 2
            else:
                sep_end = nl_end
            if blank:
                segments.append((start, sep_end))
                start = sep_end
                pos = sep_end
                continue
            pos = nl_end
            continue
        pos += 1
    segments.append((start, len(body)))
    return segments


def _build_snapshot(card_id, project, version, body_sha256, state,
                    manually_edited, title, topic, basis_cards, body):
    display_body = body
    raw_segments = _split_segments(display_body)
    segments = []
    for start, end in raw_segments:
        seg_id = _canonical_digest(
            card_id, project, version, body_sha256, start, end
        )
        segments.append({
            "id": seg_id,
            "start": start,
            "end": end,
            "text": display_body[start:end],
        })
    return {
        "protocol": PROTOCOL,
        "id": card_id,
        "project": project,
        "version": version,
        "body_sha256": body_sha256,
        "state": state,
        "manually_edited": manually_edited,
        "title": title,
        "topic": topic,
        "basis_cards": basis_cards,
        "body": body,
        "segments": segments,
    }


def freeze_card(card):
    """Validate a card dict and produce a frozen snapshot with segments."""
    if not isinstance(card, dict):
        raise _CardError("card must be a dict")

    card_id = _require_nonempty_str(card.get("id"), "id")
    project = _require_nonempty_str(card.get("project"), "project")

    version = card.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
        raise _CardError("version must be a positive int (not bool)")

    body = card.get("body")
    if not isinstance(body, str) or not body or _WS_ONLY.match(body):
        raise _CardError("body must be a non-empty non-whitespace string")

    state = card.get("state")
    if state not in VALID_STATES:
        raise _CardError("state must be one of active/conflict/retired")

    manually_edited = card.get("manually_edited", False)
    if not isinstance(manually_edited, bool):
        raise _CardError("manually_edited must be a bool")

    title = _require_opt_str_or_null(card.get("title"), "title")
    topic = _require_opt_str_or_null(card.get("topic"), "topic")

    basis_cards = card.get("basis_cards", [])
    if not isinstance(basis_cards, list):
        raise _CardError("basis_cards must be a list")
    import copy
    basis_cards = copy.deepcopy(basis_cards)

    body_sha256 = _sha256_text(body)
    supplied = card.get("body_sha256")
    if supplied is not None:
        if not isinstance(supplied, str) or supplied != body_sha256:
            raise _CardError("body_sha256 does not match utf8 body hash")

    return _build_snapshot(card_id, project, version, body_sha256, state,
                           manually_edited, title, topic, basis_cards, body)


def _validate_snapshot(snapshot):
    if not isinstance(snapshot, dict):
        raise _CardError("snapshot must be a dict")
    if snapshot.get("protocol") != PROTOCOL:
        raise _CardError("unknown protocol")

    rebuilt = freeze_card({
        "id": snapshot.get("id"),
        "project": snapshot.get("project"),
        "version": snapshot.get("version"),
        "body": snapshot.get("body"),
        "state": snapshot.get("state"),
        "manually_edited": snapshot.get("manually_edited", False),
        "title": snapshot.get("title"),
        "topic": snapshot.get("topic"),
        "basis_cards": snapshot.get("basis_cards", []),
        "body_sha256": snapshot.get("body_sha256"),
    })

    # Require the whole snapshot (top-level keys and types, plus every
    # segment and nested field) to exactly match the rebuilt canonical form.
    # This rejects selected-key comparison, extra/missing keys or reordered segments,
    # bool-for-int offsets, and any extra nested fields.
    try:
        supplied_json = _canonical_json(snapshot)
        rebuilt_json = _canonical_json(rebuilt)
    except (TypeError, ValueError) as exc:
        raise _CardError(f"snapshot is not JSON-canonicalizable: {exc}")
    if supplied_json != rebuilt_json:
        raise _CardError(
            "snapshot does not exactly match its rebuilt canonical form"
        )
    return rebuilt


def _normalize_edits(edits):
    if not isinstance(edits, list) or not edits:
        raise _CardError("edits must be a non-empty list")
    if len(edits) > MAX_EDITS:
        raise _CardError(f"edits must not exceed {MAX_EDITS} items")

    cleaned = []
    seen_targets = {}
    for edit in edits:
        if not isinstance(edit, dict):
            raise _CardError("each edit must be a dict")
        if set(edit.keys()) != set(EDIT_KEYS):
            raise _CardError(f"edit must have exactly {EDIT_KEYS}")
        op = edit.get("op")
        if op not in ("replace", "insert_after"):
            raise _CardError("edit op must be replace or insert_after")
        seg_id = edit.get("segment_id")
        if not isinstance(seg_id, str) or not seg_id:
            raise _CardError("edit segment_id must be a non-empty string")
        text = _require_nonempty_str(edit.get("text"), "edit text")
        if seg_id in seen_targets:
            raise _CardError("each segment may be targeted at most once")
        seen_targets[seg_id] = op
        cleaned.append((seg_id, op, text))
    return cleaned


def apply_delta(snapshot, edits):
    """Apply local segment edits to a frozen card snapshot."""
    card = _validate_snapshot(snapshot)

    if card["state"] == "retired":
        raise _CardError("retired cards cannot be modified")

    cleaned = _normalize_edits(edits)

    segments = card["segments"]
    by_id = {s["id"]: s for s in segments}

    changes = {}
    inserts = {}
    changed_ids = set()
    for seg_id, op, text in cleaned:
        if seg_id not in by_id:
            raise _CardError("unknown segment_id")
        if op == "replace":
            if by_id[seg_id]["text"] == text:
                raise _CardError("replace must change segment text")
            changes[seg_id] = text
            changed_ids.add(seg_id)
        else:
            inserts.setdefault(seg_id, []).append(text)
            changed_ids.add(seg_id)

    parts = []
    for seg in segments:
        sid = seg["id"]
        if sid in changes:
            parts.append(changes[sid])
        else:
            parts.append(seg["text"])
        if sid in inserts:
            parts.extend(inserts[sid])

    new_body = "".join(parts)
    if new_body == card["body"]:
        raise _CardError("delta produced no change to body")
    if not new_body or _WS_ONLY.match(new_body):
        raise _CardError("resulting body must be non-empty non-whitespace")
    if len(new_body) > MAX_BODY_LEN:
        raise _CardError(f"resulting body exceeds {MAX_BODY_LEN} characters")

    ordered_changed = [s["id"] for s in segments if s["id"] in changed_ids]

    return {
        "id": card["id"],
        "project": card["project"],
        "expected_version": card["version"],
        "expected_body_sha256": card["body_sha256"],
        "body": new_body,
        "body_sha256": _sha256_text(new_body),
        "changed_segment_ids": ordered_changed,
        "requires_semantic_review": True,
        "requires_human_review": card["manually_edited"],
    }
