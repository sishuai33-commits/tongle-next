#!/usr/bin/env python3
"""tongle-next isolated adapter installer.

Generates or merges hook configuration ONLY into an explicitly supplied
test/project configuration directory. Never touches global defaults
(~/.claude, ~/.codex, ~/.pi). Dry-run by default; --write applies; a backup
is written before any mutation; uninstall removes only tongle-next-owned
entries and preserves all foreign hooks; repeat runs are idempotent.
Nothing is activated automatically: exact commands are printed for the
operator/supervisor.

Hosts:
  cc     merge into <config-dir>/settings.json (Claude Code hooks schema,
         exec form argv so paths with spaces need no shell quoting)
  codex  write <config-dir>/hooks.json (schema per supervisor-verified
         official docs; native activation/trust handled by the supervisor)
  pi     print explicit loading instructions only (no config file writes)
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sys
import tempfile
import time

MARKER = "tongle-next"
ADAPTERS_DIR = os.path.dirname(os.path.abspath(__file__))
HOOK_PY = os.path.join(ADAPTERS_DIR, "hook.py")
PI_TS = os.path.join(ADAPTERS_DIR, "pi.ts")

ENV_KEYS = ("TONGLE_NEXT_HOME", "TONGLE_NEXT_PYTHON", "TONGLE_NEXT_ROOT", "TONGLE_NEXT_PROJECT")

CC_EVENTS = {"SessionStart": 15, "UserPromptSubmit": 10, "Stop": 10, "SessionEnd": 5}
CODEX_EVENTS = {"SessionStart": 10, "UserPromptSubmit": 5, "Stop": 5}


class InstallError(Exception):
    pass


def _real_home_dirs():
    home = os.path.expanduser("~")
    return [os.path.join(home, d) for d in (".claude", ".codex", ".pi")]


def check_config_dir(path, allow_external):
    if not os.path.isabs(path):
        raise InstallError("--config-dir must be an absolute path")
    real = os.path.realpath(path)
    if real in (os.path.realpath(p) for p in _real_home_dirs()):
        raise InstallError("refusing to touch global live config dir: %s" % real)
    if allow_external:
        return real
    allowed_roots = [os.path.realpath(os.getcwd()), os.path.realpath(tempfile.gettempdir())]
    if any(real == root or real.startswith(root + os.sep) for root in allowed_roots):
        return real
    raise InstallError(
        "config dir %s is outside the current workspace and temp dirs; "
        "pass --allow-external if you intentionally target another location" % real
    )


ADAPTER_ID_ARG = "--adapter-id=tongle-next"


def _is_owned_handler(handler):
    """Exact ownership: our hook.py path plus the --adapter-id=tongle-next
    argument. A foreign command that merely mentions the product name (e.g.
    `echo check-tongle-next-availability`) is NOT ours and must survive
    uninstall."""
    if not isinstance(handler, dict) or handler.get("type") != "command":
        return False
    args = handler.get("args")
    if isinstance(args, list) and HOOK_PY in args and ADAPTER_ID_ARG in args:
        return True
    command = handler.get("command")
    if isinstance(command, str) and "args" not in handler:
        try:
            parts = shlex.split(command)
        except ValueError:
            return False
        return HOOK_PY in parts and ADAPTER_ID_ARG in parts
    return False


def _env_block(args):
    env = {
        "TONGLE_NEXT_HOME": args.home,
        "TONGLE_NEXT_PYTHON": args.python,
        "TONGLE_NEXT_ROOT": args.root,
    }
    if args.project:
        env["TONGLE_NEXT_PROJECT"] = args.project
    return env


# --------------------------------------------------------------- CC --------

def cc_handler(args, event, timeout):
    return {"type": "command", "command": args.python,
            "args": [HOOK_PY, ADAPTER_ID_ARG], "timeout": timeout}


def cc_fragment(args):
    events = dict(CC_EVENTS)
    if args.ingest_root:
        events["PostToolUse"] = 15
    hooks = {}
    for event, timeout in events.items():
        group = {"hooks": [cc_handler(args, event, timeout)]}
        if event == "PostToolUse":
            group["matcher"] = "Write|Edit|MultiEdit"
        hooks[event] = [group]
    return {"env": _env_block(args), "hooks": hooks}


def merge_cc(existing, fragment):
    merged = json.loads(json.dumps(existing)) if isinstance(existing, dict) else {}
    env = merged.setdefault("env", {})
    env.update(fragment["env"])
    hooks = merged.setdefault("hooks", {})
    for event, groups in fragment["hooks"].items():
        current = hooks.setdefault(event, [])
        # Replace only exact owned handler definitions (allows updated args
        # on repeated install); foreign handlers are never touched.
        replaced = False
        for group in current:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                continue
            for i, h in enumerate(group["hooks"]):
                if _is_owned_handler(h):
                    group["hooks"][i] = groups[0]["hooks"][0]
                    replaced = True
        if not replaced:
            current.extend(groups)
    return merged


def unmerge_cc(existing):
    merged = json.loads(json.dumps(existing)) if isinstance(existing, dict) else {}
    env = merged.get("env")
    if isinstance(env, dict):
        for key in ENV_KEYS:
            env.pop(key, None)
        if not env:
            merged.pop("env", None)
    hooks = merged.get("hooks")
    if isinstance(hooks, dict):
        for event in list(hooks.keys()):
            groups = hooks[event]
            if not isinstance(groups, list):
                continue
            kept = []
            for group in groups:
                if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                    kept.append(group)
                    continue
                foreign = [h for h in group["hooks"] if not _is_owned_handler(h)]
                if foreign:
                    group["hooks"] = foreign
                    kept.append(group)
            if kept:
                hooks[event] = kept
            else:
                hooks.pop(event, None)
        if not hooks:
            merged.pop("hooks", None)
    return merged


# ------------------------------------------------------------- Codex -------

def codex_fragment(args):
    command = "%s %s %s --host=codex" % (
        shlex.quote(args.python), shlex.quote(HOOK_PY), ADAPTER_ID_ARG)
    hooks = {}
    for event, timeout in CODEX_EVENTS.items():
        hooks[event] = [{
            "hooks": [{
                "type": "command",
                "command": command,
                "timeout": timeout,
                "statusMessage": "tongle-next",
            }],
        }]
    return {
        "description": "tongle-next adapter hooks (generated by adapters/install.py; "
                       "native trust/activation is handled separately by the operator)",
        "hooks": hooks,
    }


def merge_codex(existing, fragment):
    merged = json.loads(json.dumps(existing)) if isinstance(existing, dict) else {}
    # Preserve a foreign description; only set ours on a fresh/owned file.
    if not merged.get("description"):
        merged["description"] = fragment["description"]
    hooks = merged.setdefault("hooks", {})
    for event, groups in fragment["hooks"].items():
        current = hooks.setdefault(event, [])
        replaced = False
        for group in current:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                continue
            for i, h in enumerate(group["hooks"]):
                if _is_owned_handler(h):
                    group["hooks"][i] = groups[0]["hooks"][0]
                    replaced = True
        if not replaced:
            current.extend(groups)
    return merged


# Codex uninstall uses the same shape rules as CC.
unmerge_codex = unmerge_cc


# ---------------------------------------------------------------- Pi -------

def pi_instructions(args):
    lines = [
        "Pi uses explicit extension loading; no config files were written.",
        "Run tongle-next with Pi exactly like this (worker runs must keep -ne):",
        "",
        "  export TONGLE_NEXT_HOME=%s" % shlex.quote(args.home),
        "  export TONGLE_NEXT_PYTHON=%s" % shlex.quote(args.python),
        "  export TONGLE_NEXT_ROOT=%s" % shlex.quote(args.root),
    ]
    if args.project:
        lines.append("  export TONGLE_NEXT_PROJECT=%s  # or per-cwd .tongle-next.json"
                     % shlex.quote(args.project))
    lines += [
        "  pi -ne -e %s" % shlex.quote(PI_TS),
        "",
        "-ne disables extension discovery so only this explicit -e path loads.",
        "Never add this to global pi settings from the installer; activate it",
        "yourself per project after review.",
    ]
    return "\n".join(lines)


# -------------------------------------------------------------- driver -----

def plan_file(path, new_data):
    """Return (changed, new_text)."""
    new_text = json.dumps(new_data, indent=2, ensure_ascii=False) + "\n"
    try:
        with open(path, "r", encoding="utf-8") as fh:
            old_text = fh.read()
    except FileNotFoundError:
        return True, new_text
    if json.loads(old_text or "{}") == new_data:
        return False, new_text
    return True, new_text


def load_json_or_empty(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise InstallError("%s must contain a JSON object" % path)
        return data
    except FileNotFoundError:
        return {}
    except ValueError as exc:
        raise InstallError("%s is not valid JSON: %s" % (path, exc))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", required=True, choices=["cc", "codex", "pi"])
    parser.add_argument("--config-dir", help="explicit test/project config directory (cc/codex)")
    parser.add_argument("--home", required=True, help="absolute TONGLE_NEXT_HOME")
    parser.add_argument("--python", default=sys.executable, help="absolute python executable")
    parser.add_argument("--root", default=os.path.dirname(ADAPTERS_DIR),
                        help="absolute tongle-next repo root (contains the tongle package)")
    parser.add_argument("--project", help="optional default project (else per-cwd .tongle-next.json)")
    parser.add_argument("--ingest-root", action="append", default=[],
                        help="explicit absolute source root for optional PostToolUse ingestion (cc)")
    parser.add_argument("--write", action="store_true", help="apply changes (default: dry-run)")
    parser.add_argument("--uninstall", action="store_true", help="remove only tongle-next-owned entries")
    parser.add_argument("--allow-external", action="store_true",
                        help="permit config dirs outside the current workspace/temp dirs")
    args = parser.parse_args(argv)

    for name, value in (("home", args.home), ("python", args.python), ("root", args.root)):
        if not os.path.isabs(value):
            raise InstallError("--%s must be an absolute path" % name)
    for root in args.ingest_root:
        if not os.path.isabs(root):
            raise InstallError("--ingest-root must be absolute: %r" % root)

    if args.host == "pi":
        print(pi_instructions(args))
        return 0

    if not args.config_dir:
        raise InstallError("--config-dir is required for cc/codex")
    config_dir = check_config_dir(args.config_dir, args.allow_external)

    if args.host == "cc":
        target = os.path.join(config_dir, "settings.json")
        fragment = cc_fragment(args)
        merge, unmerge = merge_cc, unmerge_cc
    else:
        target = os.path.join(config_dir, "hooks.json")
        fragment = codex_fragment(args)
        merge, unmerge = merge_codex, unmerge_codex

    existing = load_json_or_empty(target)
    new_data = unmerge(existing) if args.uninstall else merge(existing, fragment)
    changed, new_text = plan_file(target, new_data)

    action = "uninstall" if args.uninstall else "install"
    if not changed:
        print("%s: %s already up to date (idempotent, nothing to do)" % (args.host, target))
        return 0

    if not args.write:
        print("DRY-RUN %s %s: would update %s" % (args.host, action, target))
        print("Planned content:")
        print(new_text)
        print("Apply with: %s --write" % " ".join(shlex.quote(a) for a in
              [args.python, os.path.abspath(__file__)] + (argv if argv is not None else sys.argv[1:])))
        return 0

    os.makedirs(config_dir, exist_ok=True)
    if os.path.exists(target):
        backup = "%s.bak-%s" % (target, time.strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(target, backup)
        print("backup written: %s" % backup)
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(new_text)
    os.replace(tmp, target)
    print("%s %s applied to %s" % (args.host, action, target))
    if not args.uninstall:
        print("Hooks are NOT active until you point a host at this directory.")
        print("  CC test launch:    claude --settings %s  (or copy into a test project's .claude/)" % target)
        print("  Codex test launch: place hooks.json in an explicit test .codex dir and trust it")
        print("Activation/trust on real hosts is the operator's/supervisor's step.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except InstallError as exc:
        print("install.py: %s" % exc, file=sys.stderr)
        sys.exit(2)
