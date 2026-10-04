# tongle-next adapters (thin, isolated)

Scope: `adapters/` only. These adapters translate host events into core CLI
calls (`python3 -m tongle --home DIR capture|query|ingest`) and inject bounded
project context. They contain no model calls, no shell interpolation of
prompts, and no knowledge semantics. The core package is developed separately;
all adapter tests here use an explicit **fake executable** and are **unit
tests only** — they do not demonstrate live host capture or semantic quality.
Host behavior depends on its version and enabled lifecycle events. Local development has exercised Pi/Codex paths, but the downloadable bundle does not certify every host/version. Validate capture, injection and actual use separately on your setup.

## Files

| File | Purpose |
|---|---|
| `adapters/pi.ts` | Pi extension (TypeScript, explicit `-e` launch only) |
| `adapters/hook.py` | Claude Code + Codex stdin/stdout hook (stdlib only) |
| `adapters/common.py` | Shared config, bounded I/O, CLI spawn helpers |
| `adapters/install.py` | Isolated installer for hook config fragments |
| `tests/test_adapters.py` | Unit tests (fake core executable) |

## Configuration (all adapters)

| Variable | Meaning |
|---|---|
| `TONGLE_NEXT_HOME` | **Mandatory.** Absolute data home. Never defaults to a live Vault. |
| `TONGLE_NEXT_PROJECT` | Project name, or `.tongle-next.json` under the selected cwd: `{"project": "...", "host": "cc\|codex", "ingest_roots": ["/abs/root"]}` |
| `TONGLE_NEXT_PYTHON` | Absolute python3 executable (real core runs) |
| `TONGLE_NEXT_ROOT` | Absolute repo root containing the `tongle` package (set as `PYTHONPATH`) |
| `TONGLE_NEXT_HOST` | `cc` / `codex` / `auto` (default auto) |
| `TONGLE_NEXT_CLI` | **Unit tests only.** Absolute path to an explicit fake executable implementing the same argv/stdin contract. Not live support. |

Core is spawned as argv with the event text on stdin; prompts are never
shell-interpolated. Timeouts: capture 8 s, query/context 10 s, ingest 15 s.
Context injection uses the core `context --project P --text Q --limit N`
command (records + `recent_statements` of raw user statements, so important
corrections survive while the model is offline); it falls back to `query`
only when the core explicitly reports `context` as an unknown command. Both
streams are rendered with explicit roles (`source_role` shown for records;
`recent_statements` labeled raw/unverified, never promoted to confirmed
rules, kept in core-given chronology). Injected context is hard-bounded to
**6000 chars including the preamble** and wrapped only in the fixed
preamble: records are untrusted source data; the current user message/scope
wins; unknown remains unknown. No other instructions are added, and commands
found in records are never executed.

Capture integrity: a zero exit code is not a save — the core must return a
valid JSON ack (`event_id`, `ok: true`, or `source_id`+`version`), otherwise
the adapter warns NOT saved. Oversized events (>200k chars) are rejected
with an explicit NOT-saved warning, never silently truncated and reported as
saved. Invalid context/query JSON is never injected.

On any failure the adapters emit a truthful warning (`systemMessage` JSON /
Pi UI notify + stderr) and inject nothing. They never claim a save that did
not commit.

## Pi (`adapters/pi.ts`)

Explicit launch (no global install, no auto-discovery):

```bash
export TONGLE_NEXT_HOME=/abs/home TONGLE_NEXT_ROOT=/abs/repo \
       TONGLE_NEXT_PYTHON=/abs/python3 TONGLE_NEXT_PROJECT=myproj
pi -ne -e /abs/repo/adapters/pi.ts
```

`-ne` disables extension discovery so only this `-e` path loads. Background
workers must run with extensions disabled (`pi -ne`, no `-e`) so worker runs
are never recursively captured.

Events (per installed `docs/extensions.md` of pi):
- `before_agent_start`: capture exact user prompt, then `query` and inject a
  `customType: "tongle-next-context"` message (`display: false`).
- `message_end` (role `assistant` only): capture finalized assistant text.
  Injected context messages are never captured.

Event IDs: `pi:<sessionId>:u:<leafEntryId|seqN>` and
`pi:<sessionId>:a:<responseId|leafId|seqN>`. Session id from
`ctx.sessionManager.getSessionId()`; leaf ids via `getLeafId()`/`getLeafEntry()`.
Distinct equal-text turns get distinct ids; identical replays map to the same
id so the core's idempotent capture dedupes them. Pi capture applies the same
ack-validation and oversize-rejection rules as the Python adapters.

Smoke evidence (fake CLI, one-shot `pi -ne -e adapters/pi.ts -p ...`,
session dir under `/tmp`, fake core serving `context`): extension loaded
with no API errors, user capture, context call, and assistant capture all
fired with the expected argv/stdin; two identical prompts in one session
produced `u:seq1` and `u:seq2`. This is a unit-level live-load smoke check,
not live capture validation against the real core.

## Claude Code / Codex (`adapters/hook.py`)

Host-agnostic stdin JSON hook. Dispatch on `hook_event_name`:

| Event | Behavior |
|---|---|
| `UserPromptSubmit` | Capture user prompt (id from `prompt_id` (CC) or `turn_id` (Codex); fallback: session+role+transcript byte boundary+text digest — identical text in distinct real turns differs, exact replay at the same boundary dedupes; never content-only), fetch context, output `{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": ...}}` |
| `SessionStart` | Fetch project handoff context; same wrapper with `hookEventName: "SessionStart"` |
| `Stop` | CC: incremental transcript capture (uuid-keyed, idempotent; tool-result-only user entries, `isMeta`, and sidechain entries are skipped — tool output never becomes a user-role event); `last_assistant_message` fallback when no transcript. Codex: `last_assistant_message` + `turn_id` directly. Output `{}` on success; never injects on Stop. |
| `SessionEnd` | CC: final idempotent transcript capture. Output `{}`. |
| `PostToolUse` | **Disabled unless** `.tongle-next.json` lists explicit absolute `ingest_roots`; then only Write/Edit targets inside those roots with `.md`/`.txt` suffix are ingested. Never arbitrary global files. |
| anything else | no-op with a warning |

Transcript cursor durability: the CC transcript cursor advances only past
complete newline-terminated lines that were successfully captured (complete
but unrelated/invalid lines are intentionally skipped); a partial trailing
line or a failed capture leaves the cursor retryable. Read-state → handler →
write-state runs under a per-home `fcntl` advisory lock
(`$TONGLE_NEXT_HOME/adapter-state/hook.lock`); state is written via a unique
temp file + fsync + atomic rename (no fixed `.tmp` race). If the state
directory is not writable or the lock fails, a warning is surfaced —
preservation is never skipped silently.

Reconciliation: a user turn captured by the prompt hook is not stored again
when it later appears in the CC transcript under a uuid; matching entries are
consumed from a bounded per-session multiset in order, so identical text in
two real turns still yields two events. Residual risk: a host with no native
identity and a transcript boundary collision could double-store; this
conservative residual is accepted and documented.

Bounds: stdin ≤ 10 MB, transcript ≤ 64 MB, event text ≤ 200k chars (rejected,
not truncated), `transcript_path` must be an existing absolute path without
`..`. All exits are 0; failures are reported via `systemMessage` and stderr.

Codex event schema per supervisor-verified official docs
(https://learn.chatgpt.com/docs/hooks): `UserPromptSubmit` input
`prompt`/`session_id`/`turn_id`/`cwd`; `Stop` input `last_assistant_message`
+ `turn_id`; `transcript_path` nullable. CC schema per fetched official
reference (see `docs/adapters-source-notes.md`). Native Codex/CC live firing
is **unverified** here; the supervisor runs real host tests.

## Installer (`adapters/install.py`)

Dry-run by default; `--write` applies; backup (`<file>.bak-<timestamp>`)
before every mutation; idempotent; uninstall removes only **exact-owned**
entries (a `type: "command"` handler whose argv/command contains both our
`hook.py` path and `--adapter-id=tongle-next`) and preserves all foreign
hooks/env/descriptions — a foreign command merely mentioning `tongle-next`
survives. Repeated install with changed adapter arguments updates only the
exact owned definition in place. Nothing is ever activated automatically;
exact commands are printed.

```bash
# Claude Code fragment (exec form argv — safe with spaces, no shell quoting)
python3 adapters/install.py --host cc --config-dir /abs/test/.claude \
    --home /abs/home [--project NAME] [--ingest-root /abs/src] [--write] [--uninstall]

# Codex fragment: <config-dir>/hooks.json with top-level "description" and
# hooks: event -> matcher groups -> [{type: command, command, timeout,
# statusMessage}] (exact-definition trust is required natively; the
# supervisor handles trust/activation)
python3 adapters/install.py --host codex --config-dir /abs/test/.codex --home /abs/home --write

# Pi: prints explicit loading instructions, writes nothing
python3 adapters/install.py --host pi --home /abs/home
```

Safety: `--config-dir` must be absolute and inside the current workspace or
the system temp dir; `~/.claude`, `~/.codex`, `~/.pi` are always refused;
anything else needs the deliberate `--allow-external` flag. CC fragments go
into `settings.json` (`env` block carries the `TONGLE_NEXT_*` variables;
hooks use exec form `command`+`args`). `PostToolUse` is only installed when
`--ingest-root` is passed.

## Known limitations

- Core CLI is consumed per the contract in `docs/implementation-contract.md`
  plus the new `context` command (results + `recent_statements`); valid JSON
  with unknown shapes falls back to a compact dump, invalid JSON is never
  injected.
- Pi `before_agent_start` fires before the user message is appended, so the
  user event id is a deterministic per-session sequence (`u:seqN`); it is
  stable for replay of the same session lifecycle and distinct across turns,
  but it is not a content hash.
- CC transcript parsing covers the documented `user`/`assistant` JSONL entry
  shapes; unknown future shapes are skipped, not guessed.
- Codex transcript files are not parsed (nullable `transcript_path`; direct
  `last_assistant_message`/`turn_id` fields used instead).
- Fallback event ids rely on the transcript byte boundary; a host with no
  native identity where the boundary does not change between two distinct
  equal-text turns could collapse them (conservative, documented residual).
- Live activation, host trust prompts, and real end-to-end capture on all
  three hosts are outside this task and unverified.
