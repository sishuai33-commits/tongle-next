"""Offline asset inventory and curation-plan validation for Tongle's knowledge lifecycle.

Library API:
    inventory(vault: Path) -> dict
    validate_plan(vault: Path, manifest: dict, plan: dict) -> dict

CLI (never mutates the vault; outputs must live outside the vault):
    python3 -m tongle.curation inventory --vault PATH --output PATH
    python3 -m tongle.curation validate --vault PATH --manifest PATH --plan PATH --output PATH

Stdlib only. No network, no external model calls, no semantic scoring.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import posixpath
import re
import sys
import unicodedata
from pathlib import Path
from urllib.parse import unquote

SCHEMA_VERSION = 1

CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
FENCE_RE = re.compile(r"(```.*?```|~~~.*?~~~)", re.S)
INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
WIKILINK_RE = re.compile(r"\[\[([^\[\]]+)\]\]")
MDLINK_RE = re.compile(r"(?<!!)\[[^\[\]]*\]\(([^)]+)\)")
TONGLE_META_RE = re.compile(r"%%[ \t]*\r?\n[ \t]*tongle-meta:[ \t]*(\{.*?\})[ \t]*\r?\n[ \t]*%%", re.S)
FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n?", re.S)
H1_RE = re.compile(r"^#\s+(.+?)\s*#*\s*$", re.M)


# ---------------------------------------------------------------------------
# small helpers

def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_manifest_json(manifest: dict) -> str:
    return json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def manifest_digest(manifest: dict) -> str:
    return _sha256(canonical_manifest_json(manifest).encode("utf-8"))


def _rel_posix(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _is_hidden_name(name: str) -> bool:
    return name.startswith(".")


def _decode_target(raw: str) -> str:
    """Decode a markdown link destination (angle brackets, %XX, escaped spaces)."""
    t = raw.strip()
    if t.startswith("<") and t.endswith(">"):
        t = t[1:-1]
    t = unquote(t)  # percent-encoding is UTF-8 per RFC 3986
    t = t.replace("\\ ", " ")
    return t.strip()


def _split_wikilink(raw: str) -> str:
    """Original wikilink target, minus display alias."""
    return raw.split("|", 1)[0].strip()


def _link_path_part(target: str) -> str:
    """Path portion of a link target (strip heading ^block / #fragment)."""
    cut = len(target)
    for marker in ("#", "^"):
        i = target.find(marker)
        if i != -1:
            cut = min(cut, i)
    return target[:cut].strip()


def _strip_nonprose(markdown_text: str) -> str:
    """Remove fenced code, inline code and frontmatter so example links are not live."""
    text = FRONTMATTER_RE.sub("", markdown_text, count=1)
    text = FENCE_RE.sub(lambda m: "\n" * m.group(0).count("\n"), text)
    text = INLINE_CODE_RE.sub("", text)
    return text


def _parse_frontmatter(text: str) -> dict:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}
    out = {}
    for line in m.group(1).splitlines():
        if ":" not in line or line.startswith((" ", "\t", "-")):
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key in ("type", "created", "tongle_id") and value:
            out[key] = value
    return out


def _title(text: str, stem: str) -> str:
    body = FRONTMATTER_RE.sub("", text, count=1)
    m = H1_RE.search(body)
    if m:
        return m.group(1).strip()
    return stem


def _normalized_body(text: str) -> str:
    body = _strip_nonprose(text)
    body = re.sub(r"\s+", " ", body).strip().casefold()
    return body


# ---------------------------------------------------------------------------
# link resolution against a visible inventory

class _Resolver:
    """Resolve link path parts against a set of visible relative POSIX paths."""

    def __init__(self, paths):
        self.paths = set(paths)
        self.stem_index = {}
        for p in paths:
            stem = posixpath.splitext(posixpath.basename(p))[0]
            self.stem_index.setdefault(stem.casefold(), []).append(p)
            self.stem_index.setdefault(posixpath.basename(p).casefold(), []).append(p)
        for k in self.stem_index:
            self.stem_index[k] = sorted(set(self.stem_index[k]))

    def _basename_match(self, name: str):
        matches = self.stem_index.get(name.casefold(), [])
        if len(matches) == 1:
            return "resolved", matches[0]
        if len(matches) > 1:
            return "ambiguous", None
        return "broken", None

    def resolve(self, source_rel: str, path_part: str, kind="wikilink"):
        """Return (status, resolved_rel|None)."""
        if not path_part:
            return "anchor", source_rel  # [[#heading]] / ^block refers to source file
        if kind == "markdown":
            # Markdown destinations are relative to the linking file; a bare
            # filename is not an Obsidian global-name lookup.
            if path_part.startswith("/"):
                return "broken", None
            joined = posixpath.normpath(posixpath.join(posixpath.dirname(source_rel), path_part))
            if joined in self.paths:
                return "resolved", joined
            return "broken", None
        candidates = []
        if "/" in path_part:
            joined = posixpath.normpath(posixpath.join(posixpath.dirname(source_rel), path_part))
            if not joined.startswith(".."):
                candidates.append(joined)
            rootrel = posixpath.normpath(path_part.lstrip("/"))
            if not rootrel.startswith(".."):
                candidates.append(rootrel)
        else:
            joined = posixpath.normpath(posixpath.join(posixpath.dirname(source_rel), path_part))
            if not joined.startswith(".."):
                candidates.append(joined)
        for c in candidates:
            if c in self.paths:
                return "resolved", c
            if not c.lower().endswith(".md"):
                withmd = c + ".md"
                if withmd in self.paths:
                    return "resolved", withmd
        if "/" in path_part:
            # explicit directory path that failed is broken; only bare stems
            # may use the unique-name fallback
            return "broken", None
        return self._basename_match(posixpath.splitext(path_part)[0])


# ---------------------------------------------------------------------------
# inventory

def inventory(vault: Path) -> dict:
    vault = Path(vault)
    issues = []
    probe = vault
    while True:
        if probe.is_symlink():
            raise ValueError(f"vault path must not traverse a symlink: {probe}")
        if probe.parent == probe or not probe.exists():
            break
        probe = probe.parent
    if not vault.is_dir():
        raise ValueError(f"vault root must be an actual directory: {vault}")

    items = {}
    excluded = []

    def onerror(err):
        issues.append({"code": "walk_error", "path": getattr(err, "filename", str(err)),
                       "detail": str(err)})

    for dirpath, dirnames, filenames in os.walk(vault, followlinks=False, onerror=onerror):
        dirnames.sort()
        rel_dir = Path(dirpath).relative_to(vault)
        kept_dirs = []
        for d in dirnames:
            full = Path(dirpath) / d
            rel = (rel_dir / d).as_posix()
            if _is_hidden_name(d):
                excluded.append({"path": rel, "kind": "directory", "reason": "hidden"})
            elif full.is_symlink():
                excluded.append({"path": rel, "kind": "directory", "reason": "symlink"})
            else:
                kept_dirs.append(d)
        dirnames[:] = kept_dirs

        for fn in sorted(filenames):
            full = Path(dirpath) / fn
            rel = (rel_dir / fn).as_posix()
            if _is_hidden_name(fn):
                excluded.append({"path": rel, "kind": "file", "reason": "hidden"})
                continue
            if full.is_symlink():
                excluded.append({"path": rel, "kind": "file", "reason": "symlink"})
                continue
            if not full.is_file():
                excluded.append({"path": rel, "kind": "other", "reason": "not_a_regular_file"})
                continue
            data = full.read_bytes()
            item = {
                "path": rel,
                "sha256": _sha256(data),
                "byte_count": len(data),
                "markdown": fn.lower().endswith(".md"),
            }
            if item["markdown"]:
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError:
                    item["nontext"] = True
                    item["title"] = posixpath.splitext(fn)[0]
                else:
                    fm = _parse_frontmatter(text)
                    for k in ("type", "created", "tongle_id"):
                        if k in fm:
                            item[k] = fm[k]
                    mm = TONGLE_META_RE.search(text)
                    if mm:
                        try:
                            meta = json.loads(mm.group(1))
                        except ValueError:
                            meta = None
                        if isinstance(meta, dict) and \
                                re.fullmatch(r"[0-9a-fA-F]{32}", str(meta.get("id", ""))):
                            item["tongle_id"] = meta["id"]
                            item["managed"] = True
                            for k in ("project", "source_id", "source_version"):
                                if k in meta:
                                    item[k] = meta[k]
                    item["title"] = _title(text, posixpath.splitext(fn)[0])
                    item["body_norm"] = _normalized_body(text)
                    resolver = None  # filled after full pass
                    item["_text"] = text
            items[rel] = item

    paths = sorted(items)
    resolver = _Resolver(paths)

    # duplicate groups
    by_hash = {}
    by_body = {}
    by_stem = {}
    for p in paths:
        it = items[p]
        by_hash.setdefault(it["sha256"], []).append(p)
        if it.get("body_norm"):
            by_body.setdefault(it["body_norm"], []).append(p)
        by_stem.setdefault(posixpath.splitext(posixpath.basename(p))[0].casefold(), []).append(p)

    duplicate_hashes = sorted([sorted(v) for v in by_hash.values() if len(v) > 1])
    duplicate_bodies = sorted([sorted(v) for v in by_body.values() if len(v) > 1])
    duplicate_stems = sorted([sorted(v) for v in by_stem.values() if len(v) > 1])

    # per-file link extraction + resolution
    for p in paths:
        it = items[p]
        text = it.pop("_text", None)
        if text is None:
            it.pop("body_norm", None)
            continue
        prose = _strip_nonprose(text)
        links = []
        for m in WIKILINK_RE.finditer(prose):
            original = _split_wikilink(m.group(1))
            path_part = _link_path_part(original)
            status, resolved = resolver.resolve(p, path_part)
            links.append({"kind": "wikilink", "target": original, "raw_target": m.group(1),
                          "status": status, "resolved_path": resolved})
        for m in MDLINK_RE.finditer(prose):
            dest = _decode_target(m.group(1))
            if not dest or dest.startswith("#") or SCHEME_RE.match(dest):
                continue
            path_part = _link_path_part(dest)
            status, resolved = resolver.resolve(p, path_part, "markdown")
            links.append({"kind": "markdown", "target": dest, "raw_target": m.group(1),
                          "status": status, "resolved_path": resolved})
        if links:
            it["links"] = links
        it.pop("body_norm", None)

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "vault": str(vault),
        "items": {p: items[p] for p in paths},
        "excluded": sorted(excluded, key=lambda e: e["path"]),
        "duplicates": {
            "exact": duplicate_hashes,
            "normalized_body_hints": duplicate_bodies,
            "stems": duplicate_stems,
        },
        "issues": issues,
    }
    manifest["manifest_digest"] = manifest_digest(
        {k: v for k, v in manifest.items() if k != "manifest_digest"})
    return manifest


# ---------------------------------------------------------------------------
# plan validation

ACTIONS = {"keep", "move", "merge", "archive", "exclude_from_current"}
REVIEW_STATUSES = {"content_reviewed", "structure_only", "unresolved"}


def _issue(code, path=None, detail="", severity="blocker"):
    out = {"code": code, "severity": severity, "detail": detail}
    if path is not None:
        out["path"] = path
    return out


def _target_path_errors(target: str):
    """Return a list of structural error codes for a target string."""
    codes = []
    if not isinstance(target, str) or not target:
        return ["empty_target"]
    if "\x00" in target:
        codes.append("nul_in_target")
    if "\\" in target:
        codes.append("backslash_in_target")
    if WINDOWS_DRIVE_RE.match(target):
        codes.append("windows_drive_in_target")
    if target.startswith("/"):
        codes.append("absolute_target")
    norm = posixpath.normpath(target)
    if ".." in target.split("/"):
        codes.append("traversal_in_target")
    if any(not part or part == "." or part.startswith(".") for part in target.split("/")):
        codes.append("hidden_or_noncanonical_target")
    if target.endswith("/"):
        codes.append("empty_target")
    return codes


def _has_cjk(s: str) -> bool:
    return bool(CJK_RE.search(s))


def validate_plan(vault: Path, manifest: dict, plan: dict) -> dict:
    """Validate untrusted plan input; malformed fields cannot produce readiness."""
    try:
        return _validate_plan(vault, manifest, plan)
    except (TypeError, KeyError, ValueError, OSError, AttributeError) as exc:
        return _report([_issue("invalid_input", detail=f"{type(exc).__name__}: {exc}")], plan)


def _validate_plan(vault: Path, manifest: dict, plan: dict) -> dict:
    issues = []
    vault = Path(vault)

    # ---- fail-closed structural validation of inputs -----------------------
    if not isinstance(manifest, dict) or not isinstance(manifest.get("items"), dict):
        issues.append(_issue("malformed_manifest", detail="manifest must be a dict with an items mapping"))
        return _report(issues, plan)
    if not isinstance(plan, dict):
        issues.append(_issue("malformed_plan", detail="plan must be a dict"))
        return _report(issues, plan)
    if plan.get("schema_version") != SCHEMA_VERSION:
        issues.append(_issue("unsupported_schema_version",
                             detail=f"expected schema_version {SCHEMA_VERSION}"))
        return _report(issues, plan)
    decisions = plan.get("decisions")
    if not isinstance(decisions, list):
        issues.append(_issue("malformed_plan", detail="plan.decisions must be a list"))
        return _report(issues, plan)

    manifest_items = manifest["items"]
    if manifest.get("schema_version") != SCHEMA_VERSION or manifest.get("issues"):
        return _report([_issue("malformed_or_incomplete_manifest")], plan)
    for path, item in manifest_items.items():
        if (not isinstance(path, str) or _target_path_errors(path)
                or not isinstance(item, dict)
                or not isinstance(item.get("sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
                or not isinstance(item.get("links", []), list)):
            return _report([_issue("malformed_manifest", detail="invalid item path, hash or links")], plan)
    digest_now = manifest_digest({k: v for k, v in manifest.items() if k != "manifest_digest"})
    declared = plan.get("manifest_digest")
    if not isinstance(declared, str) or declared != digest_now:
        issues.append(_issue("manifest_digest_mismatch",
                             detail="plan.manifest_digest does not match the provided manifest snapshot"))

    # ---- per-decision field validation ------------------------------------
    seen_sources = {}
    dest_map = {}           # old rel path -> intended destination rel path
    merge_edges = {}        # source -> merge_into
    merge_archive = {}      # source -> archive target
    action_by_source = {}
    keep_targets = set()    # files that survive (keep/move destinations, merge_into)
    moved_sources = set()   # sources whose links may need updating

    for i, d in enumerate(decisions):
        if not isinstance(d, dict) or not isinstance(d.get("path"), str):
            issues.append(_issue("malformed_decision", detail=f"decisions[{i}] lacks a string path"))
            continue
        src = d["path"]
        if src in seen_sources:
            issues.append(_issue("duplicate_decision_source", path=src,
                                 detail=f"also decided at index {seen_sources[src]}"))
        seen_sources[src] = i
        if src in manifest_items and d.get("expected_sha256") != manifest_items[src]["sha256"]:
            issues.append(_issue("expected_hash_mismatch", path=src,
                                 detail="decision must identify the exact source bytes it reviewed"))
        action = d.get("action")
        if not isinstance(action, str) or action not in ACTIONS:
            issues.append(_issue("invalid_action", path=src,
                                 detail=f"action must be one of {sorted(ACTIONS)}"))
            continue
        action_by_source[src] = action
        reason = d.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            issues.append(_issue("empty_reason", path=src,
                                 detail="reason must be a non-empty string"))
        review = d.get("review_status")
        if not isinstance(review, str) or review not in REVIEW_STATUSES:
            issues.append(_issue("invalid_review_status", path=src,
                                 detail=f"review_status must be one of {sorted(REVIEW_STATUSES)}"))
        target = d.get("target")
        if _target_path_errors(target if target is not None else ""):
            for code in _target_path_errors(target if target is not None else ""):
                issues.append(_issue(code, path=src, detail=f"target={target!r}"))
            continue
        target = unicodedata.normalize("NFC", target)
        if action in ("keep", "exclude_from_current"):
            if target != src:
                issues.append(_issue("invalid_target", path=src,
                                     detail=f"{action} target must equal the source path"))
            dest_map[src] = src
            keep_targets.add(src)
            if action == "keep":
                moved_sources.add(src)  # no-op move; links unaffected
        elif action == "move":
            dest_map[src] = target
            keep_targets.add(target)
            moved_sources.add(src)
        elif action == "archive":
            dest_map[src] = target
            moved_sources.add(src)
        elif action == "merge":
            keep_targets.add(src)  # placeholder; refined below
            merge_archive[src] = target
            moved_sources.add(src)

    # unknown sources
    for src in seen_sources:
        if src not in manifest_items:
            issues.append(_issue("unknown_decision_source", path=src,
                                 detail="path is not in the manifest snapshot"))

    # missing decisions (coverage)
    missing = sorted(set(manifest_items) - set(seen_sources))
    for src in missing:
        issues.append(_issue("missing_decision", path=src,
                             detail="manifest item has no decision"))

    # ---- current vault vs snapshot ----------------------------------------
    try:
        current = inventory(vault)
    except ValueError as exc:
        issues.append(_issue("vault_error", detail=str(exc)))
        return _report(issues, plan)
    cur_items = current["items"]
    if current.get("issues"):
        issues.append(_issue("incomplete_current_inventory", detail="inventory reported read/walk errors"))
    for p in sorted(set(manifest_items) - set(cur_items)):
        issues.append(_issue("removed_file", path=p,
                             detail="file present in snapshot is gone from the vault"))
    for p in sorted(set(cur_items) - set(manifest_items)):
        issues.append(_issue("added_file", path=p,
                             detail="visible file exists in vault but not in the snapshot"))
    for p in sorted(set(manifest_items) & set(cur_items)):
        if manifest_items[p]["sha256"] != cur_items[p]["sha256"]:
            issues.append(_issue("changed_file", path=p,
                                 detail="file bytes differ from the snapshot; refresh the manifest"))

    # ---- merge graph validation -------------------------------------------
    for src, action in action_by_source.items():
        if action != "merge":
            continue
        d = next(x for x in decisions if isinstance(x, dict) and x.get("path") == src)
        evidence = d.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            issues.append(_issue("missing_evidence", path=src,
                                 detail="merge decisions require evidence references"))
        into = d.get("merge_into")
        into_resolved = None
        if not isinstance(into, str) or not into:
            issues.append(_issue("invalid_merge_target", path=src,
                                 detail="merge_into must name a surviving item or its target"))
        else:
            if into == src:
                issues.append(_issue("merge_self", path=src, detail="cannot merge into itself"))
            if into in manifest_items:
                into_resolved = into
            elif into in keep_targets:
                into_resolved = into
            else:
                issues.append(_issue("invalid_merge_target", path=src,
                                     detail="merge_into is neither a manifest item nor a surviving target"))
            into_original = next((p for p, target in dest_map.items() if target == into_resolved), into_resolved)
            if into_resolved and action_by_source.get(into_original) not in ("keep", "move"):
                issues.append(_issue("invalid_merge_target", path=src,
                                     detail="merge_into must be a surviving canonical file, not a merged source"))
        if into_resolved:
            merge_edges[src] = into_resolved
        keep_targets.discard(src)
        archive_t = unicodedata.normalize("NFC", d.get("target", ""))
        if archive_t == (into_resolved or ""):
            issues.append(_issue("invalid_target", path=src,
                                 detail="merge archive target must differ from merge_into"))
        if archive_t:
            dest_map[src] = archive_t

    # merge cycles
    for start in merge_edges:
        seen, node = set(), start
        while node in merge_edges:
            if node in seen:
                issues.append(_issue("merge_cycle", path=start,
                                     detail="merge_into chain contains a cycle"))
                break
            seen.add(node)
            node = merge_edges[node]

    # ---- destination collision checks -------------------------------------
    def existing_untouched(p: str) -> bool:
        return p in cur_items and p not in dest_map and p not in keep_targets

    file_dests = {}
    dir_dests = set()
    archive_dests = set()
    for d in decisions:
        if not isinstance(d, dict) or not isinstance(d.get("path"), str):
            continue
        src = d["path"]
        action = d.get("action")
        target = d.get("target")
        if not isinstance(target, str) or _target_path_errors(target):
            continue
        target = unicodedata.normalize("NFC", target)
        key = target.casefold()
        # overwrite of an existing file that no decision rehomes
        if target in cur_items and src != target and existing_untouched(target):
            issues.append(_issue("target_overwrites_existing", path=src,
                                 detail=f"target {target!r} exists and is not part of the plan"))
        if key in file_dests and file_dests[key] != src:
            issues.append(_issue("destination_collision", path=src,
                                 detail=f"target {target!r} collides with decision for {file_dests[key]!r} "
                                        "(Unicode NFC/casefold)"))
        file_dests[key] = src
        parent = posixpath.dirname(target)
        if parent:
            dir_dests.add(parent.casefold())
        if action in ("archive", "merge"):
            archive_dests.add(key)
    # archive destinations must never be shared or overwrite existing files
    cur_fold = {unicodedata.normalize("NFC", p).casefold() for p in cur_items}
    for key in archive_dests:
        if key in cur_fold:
            issues.append(_issue("archive_overwrites_existing",
                                 detail=f"archive destination {key!r} already exists in the vault"))
    # file/folder overlap: one file target used as a directory prefix of another
    fkeys = list(file_dests)
    for f in fkeys:
        for g in fkeys:
            if f != g and g.startswith(f + "/"):
                issues.append(_issue("file_folder_overlap", path=file_dests[f],
                                     detail=f"file target {f!r} overlaps target {g!r}"))

    # proposed directory naming: new segments must contain CJK or match existing dirs
    existing_dirs = set()
    for p in cur_items:
        parts = p.split("/")[:-1]
        acc = []
        for seg in parts:
            acc.append(seg)
            existing_dirs.add("/".join(acc).casefold())
    for key in dir_dests:
        parts = key.split("/")
        acc = []
        for seg in parts:
            acc.append(seg)
            joined = "/".join(acc)
            if joined.casefold() not in existing_dirs and not _has_cjk(seg):
                issues.append(_issue("invalid_directory_name", detail=(
                    f"new proposed directory segment {seg!r} must contain Chinese characters "
                    "or match an existing unchanged directory")))

    # symlink parent escape (defensive: visible vault has no symlinks, but reject anyway)
    for d in decisions:
        if not isinstance(d, dict) or not isinstance(d.get("target"), str):
            continue
        probe = vault
        for seg in d["target"].split("/"):
            probe = probe / seg
            if probe.is_symlink():
                issues.append(_issue("symlink_parent_escape", path=d["path"],
                                     detail=f"target directory {seg!r} is a symlink"))
                break

    # ---- link impact --------------------------------------------------------
    link_updates = plan.get("link_updates", [])
    if not isinstance(link_updates, list):
        issues.append(_issue("malformed_plan", detail="plan.link_updates must be a list"))
        link_updates = []
    historical = plan.get("preserve_historical", [])
    if not isinstance(historical, list):
        historical = []

    update_by_key = {}
    hist_covered = set()
    known_links = {(p, link["target"]) for p, it in manifest_items.items()
                   for link in it.get("links", [])}
    for update in link_updates:
        if (not isinstance(update, dict) or any(not isinstance(update.get(k), str)
                for k in ("from_path", "old_target", "new_target"))):
            issues.append(_issue("malformed_link_update")); continue
        key = (update["from_path"], update["old_target"])
        if key not in known_links or key in update_by_key:
            issues.append(_issue("unknown_or_duplicate_link_update", path=key[0]))
        update_by_key[key] = update["new_target"]
    for entry in historical:
        if (not isinstance(entry, dict) or not isinstance(entry.get("from_path"), str)
                or not isinstance(entry.get("old_target"), str)):
            issues.append(_issue("malformed_historical_exception")); continue
        key = (entry["from_path"], entry["old_target"])
        if (entry.get("preserve_historical") is not True
                or not isinstance(entry.get("reason"), str) or not entry["reason"].strip()
                or not entry["from_path"].startswith(("归档/", "随笔/")) or key not in known_links):
            issues.append(_issue("invalid_historical_exception", path=key[0])); continue
        if key in update_by_key:
            issues.append(_issue("conflicting_link_disposition", path=key[0]))
        hist_covered.add(key)

    post_paths = {dest_map.get(p, p) for p in cur_items}
    post_resolver = _Resolver(post_paths)
    link_report = []
    for from_path, it in sorted(manifest_items.items()):
        post_from = dest_map.get(from_path, from_path)
        for link in it.get("links", []):
            src = link.get("resolved_path")
            key = (from_path, link["target"])
            destination = dest_map.get(src, src)
            allowed = {destination} if destination else set()
            if src in merge_edges:
                canonical = merge_edges[src]
                allowed.add(dest_map.get(canonical, canonical))
            changed_endpoint = src in moved_sources and src != destination
            if key in hist_covered:
                link_report.append({"from_path": from_path, "old_target": link["target"],
                                    "status": "preserved_historical", "file": src})
                continue
            new_target = update_by_key.get(key, link["target"])
            part = _link_path_part(_decode_target(_split_wikilink(new_target)))
            status, resolved = post_resolver.resolve(post_from, part, link["kind"])
            wrong = status not in ("resolved", "anchor") or (allowed and resolved not in allowed)
            needs_update = changed_endpoint or (bool(src) and wrong)
            if key not in update_by_key and not needs_update:
                # Existing broken/ambiguous links remain explicit debt, never
                # silently described as repaired by this plan.
                if link.get("status") in ("broken", "ambiguous"):
                    issues.append(_issue("existing_unresolved_link", path=from_path,
                                         detail=link["target"], severity="warning"))
                continue
            entry = {"from_path": from_path, "old_target": link["target"],
                     "file": src, "destination": destination}
            if key in update_by_key:
                entry.update(new_target=new_target, status="invalid" if wrong else "addressed")
                if wrong:
                    issues.append(_issue("invalid_link_update", path=from_path,
                                         detail=f"{new_target!r} resolves to {resolved!r} in the final layout"))
            else:
                entry["status"] = "unaddressed"
                issues.append(_issue("unaddressed_incoming_link", path=from_path,
                                     detail=f"link {link['target']!r} needs a disposition after relocation"))
            link_report.append(entry)

    # ---- readiness ----------------------------------------------------------
    blockers = [i for i in issues if i["severity"] == "blocker"]
    structurally_valid = not blockers
    coverage_complete = not missing and len(seen_sources) == len(manifest_items)
    content_review_complete = all(
        isinstance(d, dict) and d.get("review_status") == "content_reviewed" for d in decisions) \
        and coverage_complete
    managed = [p for p, item in manifest_items.items() if item.get("tongle_id")]
    draft = plan.get("status") == "draft"
    ready = (structurally_valid and content_review_complete and coverage_complete
             and not draft and not managed)
    if managed:
        issues.append(_issue("db_sync_required",
                             detail="managed tongle_id files require database synchronization; "
                                    "ready_for_application is always false for them",
                             severity="info"))
    if draft:
        issues.append(_issue("draft_plan", detail="plan is marked draft and cannot be ready",
                             severity="info"))

    summary = {
        "decisions": len(decisions),
        "manifest_items": len(manifest_items),
        "blockers": len(blockers),
        "warnings": sum(1 for i in issues if i["severity"] == "warning"),
        "unaddressed_links": sum(1 for e in link_report if e["status"] == "unaddressed"),
    }
    return {
        "ok": structurally_valid,
        "issues": issues,
        "summary": summary,
        "readiness": {
            "structurally_valid": structurally_valid,
            "content_review_complete": content_review_complete,
            "coverage_complete": coverage_complete,
            "ready_for_application": ready,
        },
        "link_report": link_report,
        "note": "A clean plan is structural validation only; it is not proof of knowledge truth.",
    }


def _report(issues, plan):
    blockers = [i for i in issues if i["severity"] == "blocker"]
    return {
        "ok": not blockers,
        "issues": issues,
        "summary": {"decisions": 0, "manifest_items": 0,
                    "blockers": len(blockers),
                    "warnings": sum(1 for i in issues if i["severity"] == "warning"),
                    "unaddressed_links": 0},
        "readiness": {"structurally_valid": False, "content_review_complete": False,
                      "coverage_complete": False, "ready_for_application": False},
        "link_report": [],
        "note": "A clean plan is structural validation only; it is not proof of knowledge truth.",
    }


# ---------------------------------------------------------------------------
# CLI

def _load_json(path: Path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _write_output(output: Path, payload: dict, vault: Path) -> int:
    vault_resolved = Path(vault).resolve()
    out_resolved = Path(output).resolve()
    if out_resolved == vault_resolved or vault_resolved in out_resolved.parents:
        print(f"error: output path must not be inside the vault: {output}", file=sys.stderr)
        return 2
    out_resolved.parent.mkdir(parents=True, exist_ok=True)
    with open(out_resolved, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m tongle.curation")
    sub = parser.add_subparsers(dest="command", required=True)

    p_inv = sub.add_parser("inventory", help="build an offline inventory of a vault")
    p_inv.add_argument("--vault", required=True, type=Path)
    p_inv.add_argument("--output", required=True, type=Path)

    p_val = sub.add_parser("validate", help="validate a curation plan against a vault snapshot")
    p_val.add_argument("--vault", required=True, type=Path)
    p_val.add_argument("--manifest", required=True, type=Path)
    p_val.add_argument("--plan", required=True, type=Path)
    p_val.add_argument("--output", required=True, type=Path)

    args = parser.parse_args(argv)

    if args.command == "inventory":
        try:
            manifest = inventory(args.vault)
        except (ValueError, OSError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        return _write_output(args.output, manifest, args.vault)

    try:
        manifest = _load_json(args.manifest)
        plan = _load_json(args.plan)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: cannot read manifest/plan: {exc}", file=sys.stderr)
        return 2
    result = validate_plan(args.vault, manifest, plan)
    rc = _write_output(args.output, result, args.vault)
    if rc != 0:
        return rc
    return 0 if result["readiness"]["structurally_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
