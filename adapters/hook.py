#!/usr/bin/env python3
"""tongle-next hook for Claude Code (CC) and Codex CLI.

Reads one bounded JSON event from stdin, calls the tongle core CLI via
argv + stdin (never a shell, never a model), and prints a bounded JSON
object on stdout. On any failure it warns truthfully (systemMessage /
stderr) and never claims a save or injection succeeded.

Supported events:
  UserPromptSubmit  capture user prompt, fetch project context, inject it
  SessionStart      fetch project context, inject it
  Stop              capture assistant output (CC: transcript uuids /
                    last_assistant_message; Codex: last_assistant_message
                    + turn_id)
  SessionEnd        CC only: final idempotent transcript capture
  PostToolUse       optional, only when .tongle-next.json declares
                    explicit ingest_roots; never arbitrary files

Durability rules:
  - The CC transcript cursor advances ONLY past complete newline-terminated
    lines that were successfully captured (or that are complete but
    unrelated/invalid and thus intentionally skipped). A partial trailing
    line or a failed capture leaves the cursor retryable.
  - State read -> handler -> state write runs under a per-home advisory
    file lock; state is written via unique temp file + fsync + rename.
  - Capture requires a valid JSON ack from the core; a bare zero exit code
    is NOT a save.

Identity rules:
  - Native ids win: CC prompt_id / transcript uuid, Codex turn_id.
  - Without a native id, fallback ids combine session + role + transcript
    byte boundary + text digest, so identical text in distinct actual turns
    (transcript grew in between) gets distinct ids, while an exact replay at
    the same boundary dedupes. Hosts with no identity at all may still
    double-store on boundary collisions; that conservative residual is
    documented in docs/adapters.md.
  - A user turn captured by the prompt hook is not captured again from the
    transcript: matching transcript user entries are reconciled against
    prompt-hook captures (multiset per session, consumed in order).

Codex schema per supervisor-verified official docs (UserPromptSubmit:
prompt/session_id/turn_id/cwd; Stop: last_assistant_message + turn_id;
transcript_path nullable). Native live behavior remains unverified until
the supervisor runs a real consumer.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common  # noqa: E402
from common import AdapterError  # noqa: E402

STATE_DIR_NAME = "adapter-state"
STATE_FILE_NAME = "transcripts.json"
LOCK_FILE_NAME = "hook.lock"

CC_TEXT_MAX = 1_000_000
MAX_RECONCILE_ENTRIES = 500


# ---------------------------------------------------------------- state ----

def _state_dir(cfg):
    return os.path.join(cfg.home, STATE_DIR_NAME)


def _state_path(cfg):
    return os.path.join(_state_dir(cfg), STATE_FILE_NAME)


def _load_state(cfg):
    try:
        with open(_state_path(cfg), "rb") as fh:
            raw = fh.read(4 * 1024 * 1024)
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(cfg, state):
    """Unique temp file + fsync + atomic rename. Raises OSError on failure;
    callers must surface a warning (preservation is never skipped silently)."""
    import tempfile
    state_dir = _state_dir(cfg)
    os.makedirs(state_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="state-", suffix=".json", dir=state_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, _state_path(cfg))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class StateLock:
    """Per-home advisory lock covering read-state/handler/write-state.

    Uses the real cross-platform portability lock so Windows is protected too
    instead of silently falling through to a no-op. Acquisition is fail
    closed: on failure the descriptor is closed and the lock is reported
    unavailable rather than proceeding unlocked. The package root is placed
    on sys.path before the helper import so scripts/tongle.py cannot shadow
    the tongle package.
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.fh = None
        self.error = None

    def __enter__(self):
        try:
            import sys as _sys
            _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            if _root not in _sys.path:
                _sys.path.insert(0, _root)
            from tongle.portability import lock_file
            os.makedirs(_state_dir(self.cfg), exist_ok=True)
            self.fh = open(os.path.join(_state_dir(self.cfg), LOCK_FILE_NAME), "a+b")
            try:
                lock_file(self.fh, blocking=True)
            except OSError:
                try:
                    self.fh.close()
                finally:
                    self.fh = None
                raise
        except OSError as exc:
            if self.fh is not None:
                try:
                    self.fh.close()
                finally:
                    self.fh = None
            self.error = "state lock unavailable: %s" % exc
        return self

    def __exit__(self, *exc):
        if self.fh is not None:
            try:
                from tongle.portability import unlock_file
                unlock_file(self.fh)
            finally:
                self.fh.close()
        return False


# ------------------------------------------------------- transcript (CC) ----

def parse_cc_transcript_line(line):
    """Parse one complete CC transcript JSONL line.

    Returns (uuid, role, text) or None. Only real user text and assistant
    text are returned. Tool results quoted inside user-type entries never
    become user-role events; sidechain/meta entries are skipped.
    """
    try:
        entry = json.loads(line)
    except ValueError:
        return None
    if not isinstance(entry, dict):
        return None
    if entry.get("isMeta") or entry.get("isSidechain"):
        return None
    etype = entry.get("type")
    uuid = entry.get("uuid")
    if etype not in ("user", "assistant") or not isinstance(uuid, str) or not uuid:
        return None
    message = entry.get("message")
    if not isinstance(message, dict):
        return None
    if message.get("role", etype) != etype:
        return None
    content = message.get("content")
    texts = []
    if isinstance(content, str):
        texts.append(content)
    elif isinstance(content, list):
        saw_tool_result = False
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                texts.append(block["text"])
            elif block.get("type") == "tool_result":
                saw_tool_result = True
        if etype == "user" and saw_tool_result:
            return None  # tool output entry: not a user utterance
    else:
        return None
    text = "\n".join(t for t in texts if t.strip()).strip()
    if not text:
        return None
    return uuid, etype, text[:CC_TEXT_MAX]


def _prompt_digests(state, session_id):
    return state.setdefault("prompt_digests", {}).setdefault(session_id, [])


def _note_prompt_capture(state, session_id, text):
    digests = _prompt_digests(state, session_id)
    digests.append(hashlib.sha256(text.encode("utf-8")).hexdigest()[:24])
    del digests[:-MAX_RECONCILE_ENTRIES]


def _consume_prompt_match(state, session_id, text):
    """True when this transcript user entry reconciles with a prompt-hook
    capture (one occurrence consumed, in order)."""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]
    digests = _prompt_digests(state, session_id)
    if digest in digests:
        digests.remove(digest)
        return True
    return False


def _transcript_boundary(payload):
    """Current byte size of the transcript (0 when absent): a cheap, stable
    boundary marker for fallback ids. Distinct real turns grow the file."""
    path = payload.get("transcript_path")
    if isinstance(path, str) and path:
        try:
            return os.path.getsize(os.path.realpath(path))
        except OSError:
            pass
    return 0


def capture_cc_transcript(cfg, session_id, transcript_path, state, warnings):
    """Incrementally capture new user/assistant messages from a CC transcript.

    Cursor advances only past complete newline-terminated lines that were
    successfully captured, or past complete lines that parse as unrelated /
    invalid (intentionally skipped). A partial trailing line or a failed
    capture leaves the cursor retryable. Event ids derive from transcript
    uuids, so core-side idempotency dedupes any resend.
    """
    if not transcript_path or not isinstance(transcript_path, str):
        return
    path = os.path.normpath(transcript_path)
    if not os.path.isabs(path) or ".." in path.split(os.sep):
        warnings.append("transcript path rejected: %r" % transcript_path[:120])
        return
    try:
        real = os.path.realpath(path)
        size = os.path.getsize(real)
    except OSError:
        return  # transcript not written yet; not an error
    if size > common.MAX_TRANSCRIPT_BYTES:
        warnings.append("transcript exceeds size bound; skipped")
        return
    tstate = state.setdefault("transcripts", {}).setdefault(real, {"pos": 0, "ids": []})
    captured = set(tstate.get("ids", []))
    pos = int(tstate.get("pos", 0))
    if pos > size:
        pos = 0  # file truncated/rotated: rescan; core dedupes by uuid
    try:
        with open(real, "rb") as fh:
            fh.seek(pos)
            chunk = fh.read(common.MAX_TRANSCRIPT_BYTES + 1)
    except OSError as exc:
        warnings.append("transcript read failed: %s" % exc)
        return
    new_ids = []
    cursor = pos
    for raw_line in chunk.split(b"\n")[:-1]:  # last element is a partial line
        line_start = cursor
        cursor += len(raw_line) + 1  # complete lines only
        if not raw_line.strip():
            continue
        parsed = parse_cc_transcript_line(raw_line.decode("utf-8", "replace"))
        if not parsed:
            continue  # complete but unrelated/invalid: intentionally skipped
        uuid, role, text = parsed
        event_id = "cc:%s:%s:%s" % (session_id, uuid, role)
        if event_id in captured:
            continue
        if role == "user" and _consume_prompt_match(state, session_id, text):
            # Already captured by the prompt hook under a prompt_id/boundary
            # id; reconcile instead of double-storing the same turn.
            new_ids.append(event_id)
            continue
        ok, warn = common.capture_event(cfg, session_id, role, event_id, text)
        if not ok:
            if warn:
                warnings.append(warn)
            # Failed durable capture: keep cursor at this line for retry.
            cursor = line_start
            break
        new_ids.append(event_id)
    tstate["pos"] = cursor
    tstate["ids"] = (list(captured) + new_ids)[-20000:]


# ------------------------------------------------------------ event ids ----

def detect_host(cfg, payload):
    if cfg.host != "auto":
        return cfg.host
    # Codex supplies turn_id; CC supplies prompt_id / permission_mode.
    if payload.get("turn_id") and not payload.get("prompt_id"):
        return "codex"
    return "cc"


def _fallback_event_id(host, session_id, role, text, boundary):
    """Fallback id when the host provides no native identity: session + role
    + transcript byte boundary + text digest. Identical text in distinct
    actual turns (boundary moved) differs; exact replay at the same boundary
    dedupes. Never content-only."""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return "%s:%s:%s:b%d:%s" % (host, session_id, role, boundary, digest)


def user_event_id(cfg, state, host, session_id, payload, prompt):
    native = payload.get("prompt_id") or payload.get("turn_id")
    if isinstance(native, str) and native:
        return "%s:%s:%s:user" % (host, session_id, native)
    return _fallback_event_id(host, session_id, "user", prompt, _transcript_boundary(payload))


def assistant_event_id(cfg, state, host, session_id, payload, text):
    native = payload.get("turn_id")
    if host == "codex" and isinstance(native, str) and native:
        return "codex:%s:%s:assistant" % (session_id, native)
    return _fallback_event_id(host, session_id, "assistant", text, _transcript_boundary(payload))


# ------------------------------------------------------------- handlers ----

def handle_session_start(cfg, payload, warnings):
    context, warn = common.fetch_context(
        cfg, "项目接续")
    if warn:
        warnings.append(warn)
    out = {}
    if context:
        out["hookSpecificOutput"] = {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    return out


def handle_user_prompt_submit(cfg, state, host, session_id, payload, warnings):
    prompt_raw = payload.get("prompt")
    if isinstance(prompt_raw, str) and len(prompt_raw) > common.MAX_EVENT_TEXT_CHARS:
        warnings.append("user prompt exceeds %d char adapter bound; NOT saved"
                        % common.MAX_EVENT_TEXT_CHARS)
        prompt = None
    else:
        try:
            prompt = common.bounded_str(prompt_raw, common.MAX_EVENT_TEXT_CHARS, "prompt")
        except AdapterError as exc:
            warnings.append(str(exc))
            return {}
    if prompt is None:
        if not isinstance(prompt_raw, str) or len(prompt_raw or "") <= common.MAX_EVENT_TEXT_CHARS:
            warnings.append("UserPromptSubmit without prompt text; nothing captured")
        return {}
    event_id = user_event_id(cfg, state, host, session_id, payload, prompt)
    ok, warn = common.capture_event(cfg, session_id, "user", event_id, prompt)
    if ok:
        _note_prompt_capture(state, session_id, prompt)
    elif warn:
        warnings.append("user prompt NOT saved: %s" % warn)
    receipt = {}
    if host == 'codex':
        context, qwarn = common.fetch_context(cfg, prompt, session_id=session_id, receipt=receipt)
        turn = payload.get('turn_id')
        if isinstance(turn, str) and turn:
            key = hashlib.sha256((host+'\0'+session_id+'\0'+turn).encode()).hexdigest()
            bindings = state.setdefault('turn_receipts', {})
            # Duplicate hooks with different receipts are ambiguous, never latest-wins.
            value = receipt.get('id', '') if context else ''
            bindings[key] = value if key not in bindings or bindings[key] == value else ''
            while len(bindings) > 1000:
                del bindings[next(iter(bindings))]
    else:
        context, qwarn = common.fetch_context(cfg, prompt, session_id=session_id)
    if qwarn:
        warnings.append("context unavailable: %s" % qwarn)
    out = {}
    if context:
        out["hookSpecificOutput"] = {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        }
    return out


def handle_stop(cfg, state, host, session_id, payload, warnings):
    transcript_seen = False
    if host == "cc":
        before = len(warnings)
        capture_cc_transcript(cfg, session_id, payload.get("transcript_path"), state, warnings)
        transcript_seen = len(warnings) == before and bool(
            payload.get("transcript_path"))
    last = payload.get("last_assistant_message")
    if isinstance(last, str) and last.strip():
        # Codex: primary capture path (turn_id identity). CC: only when no
        # transcript was available (transcript uuids are the stable ids).
        if host == "codex" or not transcript_seen:
            event_id = assistant_event_id(cfg, state, host, session_id, payload, last)
            if host == 'codex':
                turn = payload.get('turn_id')
                key = hashlib.sha256((host+'\0'+session_id+'\0'+turn).encode()).hexdigest() if isinstance(turn,str) and turn else None
                receipt = state.get('turn_receipts', {}).get(key, '')
                ok, warn = common.capture_event(cfg, session_id, "assistant", event_id, last, receipt_id=receipt)
            else:
                ok, warn = common.capture_event(cfg, session_id, "assistant", event_id, last)
            if not ok and warn:
                warnings.append("assistant message NOT saved: %s" % warn)
    return {}  # per contract: success output is {}; never inject on Stop


def handle_session_end(cfg, state, host, session_id, payload, warnings):
    if host == "cc":
        capture_cc_transcript(cfg, session_id, payload.get("transcript_path"), state, warnings)
    return {}


def handle_post_tool_use(cfg, state, host, session_id, payload, warnings):
    """Optional registered-source ingestion. Disabled unless explicit
    ingest_roots are configured; never ingests arbitrary global files."""
    if not cfg.ingest_roots:
        return {}
    if payload.get("tool_name") not in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        return {}
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return {}
    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str) or not os.path.isabs(file_path):
        return {}
    real = os.path.realpath(file_path)
    if not os.path.splitext(real)[1].lower() in (".md", ".txt"):
        return {}
    if not any(real == root or real.startswith(root + os.sep) for root in cfg.ingest_roots):
        return {}
    result = common.run_cli(
        cfg, ["ingest", "--project", cfg.project, "--path", real],
        timeout=common.INGEST_TIMEOUT_SEC,
    )
    if not result["ok"]:
        warnings.append("ingest NOT completed for %s: %s" % (real, result["error"]))
    return {}


HANDLERS = {
    "SessionStart": lambda cfg, state, host, sid, p, w: handle_session_start(cfg, p, w),
    "UserPromptSubmit": handle_user_prompt_submit,
    "Stop": handle_stop,
    "SessionEnd": handle_session_end,
    "PostToolUse": handle_post_tool_use,
}


def main(argv, stdin=None, stdout=None, env=None):
    stdout = stdout if stdout is not None else sys.stdout
    warnings = []

    # Benign adapter flags (e.g. --host=codex, --adapter-id=tongle-next) are
    # accepted and mostly ignored; config comes from env / .tongle-next.json.
    cli_host = None
    for arg in argv:
        if arg.startswith("--host="):
            cli_host = arg.split("=", 1)[1]

    try:
        payload = common.read_stdin_bounded(stdin)
    except AdapterError as exc:
        json.dump({"systemMessage": "tongle-next: hook input rejected: %s" % exc}, stdout)
        stdout.write("\n")
        return 0
    if not isinstance(payload, dict):
        json.dump({"systemMessage": "tongle-next: hook input must be a JSON object"}, stdout)
        stdout.write("\n")
        return 0

    event = payload.get("hook_event_name")
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else os.getcwd()

    try:
        cfg = common.load_config(cwd, env=env)
    except AdapterError as exc:
        # Truthful warning; no pretend save, no injected context.
        json.dump({"systemMessage": "tongle-next: %s" % exc}, stdout)
        stdout.write("\n")
        return 0
    if cli_host in ("cc", "codex"):
        cfg.host = cli_host

    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        session_id = "unknown-session"
    session_id = session_id[:200]

    handler = HANDLERS.get(event)
    out = {}
    if handler is None:
        warnings.append("unsupported event %r; no-op" % (event,))
    else:
        host = detect_host(cfg, payload)
        with StateLock(cfg) as lock:
            if lock.error:
                warnings.append(lock.error + "; running without state coordination")
            state = _load_state(cfg)
            try:
                out = handler(cfg, state, host, session_id, payload, warnings) or {}
            except Exception as exc:  # never crash the host session
                warnings.append("hook handler failed: %s: %s" % (type(exc).__name__, exc))
                out = {}
            try:
                _save_state(cfg, state)
            except OSError as exc:
                warnings.append("adapter state NOT persisted (dedup/cursor state lost): %s" % exc)

    if warnings:
        msg = "; ".join(w[:200] for w in warnings[:5])
        out.setdefault("systemMessage", "tongle-next: " + msg)
        print("tongle-next hook warning: " + msg, file=sys.stderr)
    json.dump(out, stdout, ensure_ascii=False)
    stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
