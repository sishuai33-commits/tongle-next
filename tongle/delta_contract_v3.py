"""V3 compatibility wrapper over the frozen version-2 delta contract."""

import copy
import json
import re

from . import delta_contract as base
from . import delta_layout


_SEPARATION_ERROR = (
    "edit loses block separation: supply explicit newlines around the changed block"
)

_TASK_APPEND = (
    "\n"
    "注意：每个 target 在 changes 中只能出现一次；同一 target 的多个编辑必须放在同一个 update 的 edits 中；"
    "同一段落可以承接多个 insert_after（按给出顺序依次拼接）；replace 对每段至多一次，且不与同段的 insert_after 混用；"
    "每个 change 必须关联 owned material 的 disposition，不要单独处理 reference_context；"
    "examples 只表示单个 change 的形状，不是完整 response，也不是实际事实或建议的语义决策。"
)

_BLOCK_MARKER_RE = re.compile(
    r"(?m)^(?: {0,3}(?:```|~~~)| {4,}|\t| {0,3}<[/A-Za-z]| *[-+*][ \t]+| *\d+[.)][ \t]+| *>)"
)
_NEW_BLOCK_EXCLUDE_RE = re.compile(r"(?m)^ {0,3}(?:#{1,6}[ \t]+|(?:=+|-+|[_*]{3,})[ \t]*$)|\|")


def _has_container_markers(target_card, edits):
    for card in [target_card]:
        if not isinstance(card, dict):
            continue
        snapshot = card.get("snapshot")
        if isinstance(snapshot, dict):
            body = snapshot.get("body")
            if isinstance(body, str) and _BLOCK_MARKER_RE.search(body):
                return True

    for edit in edits:
        if not isinstance(edit, dict):
            continue
        text = edit.get("text")
        if isinstance(text, str):
            if _BLOCK_MARKER_RE.search(text) or _NEW_BLOCK_EXCLUDE_RE.search(text):
                return True
    return False


def _edit_by_segment(edits):
    """Map segment ref -> ordered list of edits on that segment.

    Returns None on anything unparseable. A pure insert_after chain on one
    segment is legal (card_delta applies the texts in order); any duplicate
    targeting that involves a replace is rejected later by card_delta, and is
    left as-is here so the separator normalization simply skips it.
    """
    mapping = {}
    for edit in edits:
        if not isinstance(edit, dict):
            return None
        op = edit.get("op")
        segment = edit.get("segment")
        text = edit.get("text")
        if op not in ("replace", "insert_after"):
            return None
        if not isinstance(segment, str) or not isinstance(text, str):
            return None
        bucket = mapping.setdefault(segment, [])
        if bucket and (op == "replace" or bucket[0]["op"] == "replace"):
            return None
        bucket.append(edit)
    return mapping


def _slot_text(slot):
    return slot["text"] if isinstance(slot, dict) else slot


def _normalize_change(ctx, card, change):
    edits = change.get("edits")
    if not isinstance(edits, list):
        return
    if _has_container_markers(card, edits):
        return

    edit_by_seg = _edit_by_segment(edits)
    if edit_by_seg is None:
        return

    segments = card.get("segments")
    if not isinstance(segments, list):
        return

    slots = []
    for item in segments:
        if not (isinstance(item, tuple) and len(item) == 2):
            return
        seg_id, seg = item
        if not isinstance(seg, dict):
            return
        seg_text = seg.get("text")
        if not isinstance(seg_id, str) or not isinstance(seg_text, str):
            return

        edits_for_seg = edit_by_seg.get(seg_id) or []
        replaces = [e for e in edits_for_seg if e["op"] == "replace"]
        inserts = [e for e in edits_for_seg if e["op"] == "insert_after"]
        if replaces:
            slots.append(replaces[0])
        else:
            slots.append(seg_text)
        # An insert_after chain on one segment lands in the given order, so
        # every inserted block takes part in boundary checks.
        slots.extend(inserts)

    for index, (left, right) in enumerate(zip(slots, slots[1:])):
        left_is_edit = isinstance(left, dict)
        right_is_edit = isinstance(right, dict)
        left_text = left["text"] if left_is_edit else left
        right_text = right["text"] if right_is_edit else right

        if not left_text or not right_text:
            continue
        if left_text[-1] in "\r\n" or right_text[0] in "\r\n":
            continue

        if (
            index == len(slots) - 2
            and right_is_edit
            and not left_is_edit
            and right["op"] == "insert_after"
            and right["segment"] == segments[-1][0]
            and "\n" not in right_text
            and "\r" not in right_text
            and not right_text.startswith(("    ", "\t"))
            and not delta_layout._BLOCK_RE.match(right_text)
            and delta_layout._ordinary_eof(
                "".join(_slot_text(slot) for slot in slots[: index + 1])
            )
        ):
            continue

        if left_is_edit:
            left["text"] = left_text + "\n\n"
        elif right_is_edit:
            right["text"] = "\n\n" + right_text


def _normalize_separators(ctx, response):
    changes = response.get("changes")
    if not isinstance(changes, list):
        return

    cards = ctx.get("cards")
    if not isinstance(cards, list):
        return

    card_by_alias = base._card_by_ref(ctx)

    for change in changes:
        if not isinstance(change, dict):
            continue
        if change.get("action") != "update":
            continue
        target = change.get("target")
        if not isinstance(target, str):
            continue
        card = card_by_alias.get(target)
        if card is None:
            continue
        _normalize_change(ctx, card, change)


def compile_plan(ctx, response):
    try:
        return base.compile_plan(ctx, copy.deepcopy(response))
    except ValueError as exc:
        if _SEPARATION_ERROR not in str(exc):
            raise

    work = copy.deepcopy(response)
    _normalize_separators(ctx, work)
    return base.compile_plan(ctx, work)


def plan_payload(ctx):
    payload = base.plan_payload(ctx)
    schema = json.loads(payload["response_schema"])

    samples = schema.pop("examples", [])
    owned = payload.get("owned_source_refs") or []
    if owned:
        items = schema.get("properties", {}).get("changes", {}).get("items")
        if isinstance(items, dict):
            corrected = []
            for sample in samples:
                if not isinstance(sample, dict):
                    continue
                corrected_sample = copy.deepcopy(sample)
                corrected_sample["source_refs"] = [owned[0]]
                if corrected_sample.get("action") == "update":
                    edits = corrected_sample.get("edits")
                    if isinstance(edits, list):
                        for edit in edits:
                            if isinstance(edit, dict):
                                edit["text"] = "\n\nAdditional detail.\n\n"
                corrected.append(corrected_sample)
            if corrected:
                items["examples"] = corrected

    payload["response_schema"] = json.dumps(
        schema, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )

    task = payload.get("task")
    if isinstance(task, str):
        payload["task"] = task + _TASK_APPEND
    else:
        payload["task"] = _TASK_APPEND
    return payload


def __getattr__(name):
    return getattr(base, name)
