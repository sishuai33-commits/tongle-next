"""tongle stage-1 store: SQLite persistence core (Python stdlib only).

Owns the authoritative local store at <home>/state.sqlite3.
Conversation capture and document ingestion land here as immutable
sources + versions + events + pending jobs. No model calls, no Markdown.
"""

from __future__ import annotations

import hashlib
import io
import re
import os
import sqlite3
import stat as stat_mod
import threading
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

MAX_TEXT_CHARS = 200_000          # capture() text bound
MAX_ORIGINAL_BYTES = 5 * 1024 * 1024   # ingested original file size
MAX_DOCX_TEXT_CHARS = 10 * 1024 * 1024  # expanded docx text bound
MAX_DOCX_XML_BYTES = 64 * 1024 * 1024   # decompressed document.xml bound
BUSY_TIMEOUT_MS = 15_000
TEXT_EXTENSIONS = {".md", ".markdown", ".txt"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    "key"  TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sources (
    id              TEXT PRIMARY KEY,
    project         TEXT NOT NULL,
    kind            TEXT NOT NULL,
    role            TEXT NOT NULL,
    session         TEXT,
    current_version INTEGER NOT NULL,
    original_path   TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sources_project ON sources(project);
CREATE TABLE IF NOT EXISTS versions (
    source_id  TEXT NOT NULL REFERENCES sources(id),
    version    INTEGER NOT NULL,
    text       TEXT NOT NULL,
    original   BLOB NOT NULL,
    hash       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (source_id, version)
);
CREATE TABLE IF NOT EXISTS events (
    project      TEXT NOT NULL,
    event_id     TEXT NOT NULL,
    source_id    TEXT NOT NULL REFERENCES sources(id),
    payload_hash TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (project, event_id)
);
CREATE INDEX IF NOT EXISTS idx_events_source ON events(source_id);
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    source_id   TEXT NOT NULL REFERENCES sources(id),
    version     INTEGER NOT NULL,
    state       TEXT NOT NULL DEFAULT 'pending',
    attempts    INTEGER NOT NULL DEFAULT 0,
    lease_until INTEGER NOT NULL DEFAULT 0,
    lease_token TEXT,
    error       TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);
CREATE INDEX IF NOT EXISTS idx_jobs_source ON jobs(source_id, version);
CREATE TABLE IF NOT EXISTS records (
    id              TEXT PRIMARY KEY,
    project         TEXT NOT NULL,
    "key"           TEXT NOT NULL,
    text            TEXT NOT NULL,
    quote           TEXT,
    kind            TEXT NOT NULL,
    status          TEXT NOT NULL,
    source_id       TEXT REFERENCES sources(id),
    source_version  INTEGER,
    note_path       TEXT,
    note_hash       TEXT,
    manually_edited INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    UNIQUE (project, "key", source_id, source_version)
);
CREATE INDEX IF NOT EXISTS idx_records_project_key ON records(project, "key");
CREATE INDEX IF NOT EXISTS idx_records_source ON records(source_id, source_version);
CREATE TABLE IF NOT EXISTS revisions (
    record_id  TEXT NOT NULL REFERENCES records(id),
    text       TEXT NOT NULL,
    status     TEXT NOT NULL,
    reason     TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_revisions_record ON revisions(record_id);
CREATE TABLE IF NOT EXISTS tombstones (
    project    TEXT NOT NULL,
    record_id  TEXT NOT NULL REFERENCES records(id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (project, record_id)
);
CREATE TABLE IF NOT EXISTS usage (
    id              TEXT PRIMARY KEY,
    job_id          TEXT REFERENCES jobs(id),
    model           TEXT,
    elapsed_seconds REAL,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    error           TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_job ON usage(job_id);
CREATE TABLE IF NOT EXISTS redactions (
    source_id  TEXT NOT NULL,
    version    INTEGER NOT NULL,
    role       TEXT NOT NULL,
    rules      TEXT NOT NULL,
    hits       INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (source_id, version)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# Credential *literals* are stripped at the capture boundary so a pasted key
# never reaches the ledger, the model provider, or the Vault. Applied to
# conversation streams only (not file ingest): documents may legitimately
# contain such strings, chat paste-in is where secrets leak by accident.
#
# Scope rule (2026-09-12): only shapes that are actually credential-shaped are
# redacted. Bare high-frequency words (``token``, ``secret``, ``password``) are
# deliberately NOT rules: this knowledge base is about LLM engineering, where
# "token: the model bills per token" and "本次调用 token=128000" are ordinary
# prose. An earlier revision matched those and rewrote the user's own words
# before they ever reached the ledger; that is data loss, not protection.
# A ``$VAR`` / ``${VAR}`` reference is a variable *name*, not a secret, so
# values beginning with ``$`` are left intact (``Authorization: Bearer $TOKEN``).
# Long bare random strings are also not redacted on their own: sha256 digests,
# card ids and git hashes in this corpus are long and random-looking too.
_REDACT_RULES = (
    ("sk_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}")),
    ("aws_akia", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("google_api", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    ("bearer_literal", re.compile(r"(?i)\bbearer\s+(?!\$)[A-Za-z0-9._\-]{20,}")),
    # A credential-named assignment whose value is a literal (not a $VAR) of at
    # least 20 credential-ish characters that mixes letters and digits. The
    # letter+digit requirement keeps timestamps and pure-numeric values out.
    ("keyed_literal", re.compile(
        r"(?i)\b\w*(?:key|token|secret|passwd|password)\s*[=:]\s*[\"']?"
        r"(?!\$)(?=[A-Za-z0-9._\-]*[A-Za-z])(?=[A-Za-z0-9._\-]*[0-9])[A-Za-z0-9._\-]{20,}")),
)
_REDACTED = "[REDACTED]"


def _redact(text: str):
    """Return ``(redacted_text, {rule: hits})``; never stores what was removed."""
    hits = {}
    for name, pat in _REDACT_RULES:
        text, count = pat.subn(_REDACTED, text)
        if count:
            hits[name] = hits.get(name, 0) + count
    return text, hits


def _payload_hash(*parts: str) -> str:
    """Length-prefixed SHA-256 over ordered string parts (collision-safe)."""
    h = hashlib.sha256()
    for part in parts:
        raw = part.encode("utf-8")
        h.update(len(raw).to_bytes(8, "big"))
        h.update(raw)
    return h.hexdigest()


def _require_str(value, name: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be str, got {type(value).__name__}")
    if not allow_empty and not value.strip():
        raise ValueError(f"{name} must be non-empty")
    return value


def _decode_text(data: bytes) -> str:
    """Decode .md/.txt bytes: UTF-8 (with BOM), then GBK, then latin-1 fallback."""
    for encoding in ("utf-8-sig", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")  # never fails


class _DocxTextTarget:
    """Streaming ElementTree target for word/document.xml (w:t text only).

    Tracks expanded text size and aborts beyond MAX_DOCX_TEXT_CHARS, so
    entity-expansion bombs cannot grow output unbounded.
    """

    def __init__(self, max_chars: int):
        self._parts: list[str] = []
        self._chars = 0
        self._max = max_chars
        self._in_text = False

    def _emit(self, s: str) -> None:
        self._chars += len(s)
        if self._chars > self._max:
            raise ValueError("docx expanded text exceeds 10MB limit")
        self._parts.append(s)

    def start(self, tag, attrib):  # noqa: ANN001
        local = tag.rsplit("}", 1)[-1]
        if local == "p":
            self._emit("\n")
        elif local == "t":
            self._in_text = True
        elif local in ("tab", "br", "cr"):
            self._emit("\n" if local != "tab" else "\t")

    def end(self, tag):  # noqa: ANN001
        if tag.rsplit("}", 1)[-1] == "t":
            self._in_text = False

    def data(self, data):  # noqa: ANN001
        if self._in_text:
            self._emit(data)

    def close(self):  # noqa: ANN102
        return "".join(self._parts)


def _docx_text(data: bytes) -> str:
    """Extract bounded text from .docx (zip + word/document.xml via ElementTree)."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            if "word/document.xml" not in zf.namelist():
                raise ValueError("docx is missing word/document.xml")
            info = zf.getinfo("word/document.xml")
            if info.file_size > MAX_DOCX_XML_BYTES:
                raise ValueError("docx document.xml exceeds size limit")
            with zf.open(info) as fh:
                xml_bytes = fh.read(MAX_DOCX_XML_BYTES + 1)
            if len(xml_bytes) > MAX_DOCX_XML_BYTES:
                raise ValueError("docx document.xml exceeds size limit")
    except zipfile.BadZipFile as exc:
        raise ValueError(f"invalid docx file: {exc}") from exc
    target = _DocxTextTarget(MAX_DOCX_TEXT_CHARS)
    parser = ET.XMLParser(target=target)
    try:
        parser.feed(xml_bytes)
        parser.close()
    except ET.ParseError as exc:
        raise ValueError(f"unparsable docx XML: {exc}") from exc
    return target.close()


class Store:
    """Authoritative SQLite store. One instance per process is expected."""

    def __init__(self, home):
        self.home = os.path.abspath(os.fspath(home))
        os.makedirs(self.home, mode=0o700, exist_ok=True)
        self._write_lock = threading.RLock()  # serializes store-managed txns
        self.db = sqlite3.connect(
            os.path.join(self.home, "state.sqlite3"),
            timeout=BUSY_TIMEOUT_MS / 1000.0,
            isolation_level=None,  # explicit transaction control
            check_same_thread=False,  # cross-thread use serialized via _write_lock
        )
        self.db.row_factory = sqlite3.Row
        self.db.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(_SCHEMA)
        self.db.execute(
            'INSERT OR IGNORE INTO meta("key", value) VALUES(?, ?)',
            ("schema_version", "1"),
        )

    @contextmanager
    def transaction(self, immediate: bool = True):
        """Transaction context; BEGIN IMMEDIATE by default (contended writes).

        Serialized per Store instance; use one Store per thread/process for
        real concurrency (busy_timeout resolves cross-connection contention).
        """
        with self._write_lock:
            self.db.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield self.db
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            self.db.execute("COMMIT")

    # ------------------------------------------------------------- capture

    def capture(self, project, session, role, event_id, text):
        """Persist one conversation event idempotently; returns ack dict."""
        _require_str(project, "project")
        _require_str(session, "session")
        _require_str(role, "role")
        _require_str(event_id, "event_id")
        _require_str(text, "text")
        if role not in ("user", "assistant"):
            raise ValueError(f"role must be 'user' or 'assistant', got {role!r}")
        # Credential literals never enter the ledger (capture-boundary choke
        # point; see _redact above). Applied before the idempotence hash so
        # replays of the same original still dedup identically. The redaction
        # itself is recorded below: the ledger must never claim byte fidelity
        # for a version whose text was rewritten.
        text, redactions = _redact(text)
        if len(text) > MAX_TEXT_CHARS:
            raise ValueError(f"text exceeds {MAX_TEXT_CHARS} chars")
        payload_hash = _payload_hash(project, session, role, text)
        now = _now()
        with self.transaction() as db:
            row = db.execute(
                "SELECT source_id, payload_hash FROM events"
                " WHERE project=? AND event_id=?",
                (project, event_id),
            ).fetchone()
            if row is not None:
                if row["payload_hash"] != payload_hash:
                    raise ValueError(
                        f"event {event_id!r} in project {project!r}"
                        " already captured with different payload"
                    )
                job = db.execute(
                    "SELECT id FROM jobs WHERE source_id=? AND version=1"
                    " ORDER BY created_at, id LIMIT 1",
                    (row["source_id"],),
                ).fetchone()
                return {"source_id": row["source_id"], "version": 1,
                        "event_id": event_id,
                        "job_id": job["id"] if job else None}
            sid = uuid.uuid4().hex
            original = text.encode("utf-8")
            db.execute(
                "INSERT INTO sources(id, project, kind, role, session,"
                " current_version, original_path, created_at)"
                " VALUES(?,?,?,?,?,1,NULL,?)",
                (sid, project, "event", role, session, now),
            )
            db.execute(
                "INSERT INTO versions(source_id, version, text, original,"
                " hash, created_at) VALUES(?,?,?,?,?,?)",
                (sid, 1, text, original, _sha256_hex(original), now),
            )
            db.execute(
                "INSERT INTO events(project, event_id, source_id,"
                " payload_hash, created_at) VALUES(?,?,?,?,?)",
                (project, event_id, sid, payload_hash, now),
            )
            if redactions:
                # Audit row without the removed value: rule names and counts
                # only, so the record of the rewrite cannot itself leak.
                db.execute(
                    "INSERT OR REPLACE INTO redactions(source_id, version,"
                    " role, rules, hits, created_at) VALUES(?,1,?,?,?,?)",
                    (sid, role, ",".join(sorted(redactions)),
                     sum(redactions.values()), now),
                )
            jid = uuid.uuid4().hex
            db.execute(
                "INSERT INTO jobs(id, source_id, version, created_at)"
                " VALUES(?,?,1,?)",
                (jid, sid, now),
            )
        return {"source_id": sid, "version": 1, "event_id": event_id,
                "job_id": jid}

    # -------------------------------------------------------------- ingest

    def ingest(self, project, path, source_id=None):
        """Ingest a document (.md/.txt/.docx); versioned, idempotent by bytes."""
        _require_str(project, "project")
        if not os.path.islink(path):
            resolved = os.path.realpath(os.fspath(path))
        else:
            raise ValueError(f"symlink not allowed: {path}")
        st = os.stat(resolved)  # FileNotFoundError etc. propagate
        if not stat_mod.S_ISREG(st.st_mode):
            raise ValueError(f"not a regular file: {resolved}")
        if st.st_size > MAX_ORIGINAL_BYTES:
            raise ValueError(
                f"file exceeds {MAX_ORIGINAL_BYTES} byte limit: {resolved}")
        with open(resolved, "rb") as fh:
            original = fh.read(MAX_ORIGINAL_BYTES + 1)
        if len(original) > MAX_ORIGINAL_BYTES:
            raise ValueError(
                f"file exceeds {MAX_ORIGINAL_BYTES} byte limit: {resolved}")
        ext = os.path.splitext(resolved)[1].lower()
        if ext in TEXT_EXTENSIONS:
            text = _decode_text(original)
        elif ext == ".docx":
            text = _docx_text(original)
        else:
            raise ValueError(f"unsupported file type: {ext!r} ({resolved})")
        if source_id is None:
            sid = _payload_hash("tongle-document-v1", project, resolved)[:32]
        else:
            sid = _require_str(source_id, "source_id")
        digest = _sha256_hex(original)
        now = _now()
        with self.transaction() as db:
            row = db.execute(
                "SELECT s.project, s.kind, s.current_version, v.hash"
                " FROM sources s JOIN versions v"
                "   ON v.source_id=s.id AND v.version=s.current_version"
                " WHERE s.id=?",
                (sid,),
            ).fetchone()
            if row is not None:
                if row["project"] != project or row["kind"] != "document":
                    raise ValueError(
                        f"source id {sid!r} collision: belongs to"
                        f" project={row['project']!r} kind={row['kind']!r}"
                    )
                if row["hash"] == digest:  # identical bytes: idempotent
                    job = db.execute(
                        "SELECT id FROM jobs WHERE source_id=? AND version=?"
                        " ORDER BY created_at, id LIMIT 1",
                        (sid, row["current_version"]),
                    ).fetchone()
                    return {"source_id": sid,
                            "version": row["current_version"],
                            "job_id": job["id"] if job else None}
                version = row["current_version"] + 1
                db.execute(
                    "UPDATE sources SET current_version=? WHERE id=?",
                    (version, sid),
                )
            else:
                version = 1
                db.execute(
                    "INSERT INTO sources(id, project, kind, role, session,"
                    " current_version, original_path, created_at)"
                    " VALUES(?,?,?,?,NULL,?,?,?)",
                    (sid, project, "document", "document", version,
                     resolved, now),
                )
            db.execute(
                "INSERT INTO versions(source_id, version, text, original,"
                " hash, created_at) VALUES(?,?,?,?,?,?)",
                (sid, version, text, original, digest, now),
            )
            jid = uuid.uuid4().hex
            db.execute(
                "INSERT INTO jobs(id, source_id, version, created_at)"
                " VALUES(?,?,?,?)",
                (jid, sid, version, now),
            )
            if version > 1:  # stale-mark prior-version records, keep withdrawn/superseded
                db.execute(
                    "UPDATE records SET status='stale'"
                    " WHERE source_id=? AND source_version<?"
                    "   AND status NOT IN ('withdrawn','superseded')",
                    (sid, version),
                )
        return {"source_id": sid, "version": version, "job_id": jid}

    # -------------------------------------------------------------- access

    def source(self, sid, version=None):
        """Joined source/version dict, or KeyError if unknown."""
        _require_str(sid, "sid")
        sql = (
            "SELECT s.id AS source_id, s.project, s.kind, s.role, s.session,"
            " s.current_version, s.original_path, s.created_at,"
            " v.version, v.text, v.hash, v.created_at AS version_created_at"
            " FROM sources s JOIN versions v"
            "   ON v.source_id=s.id AND {vcond}"
            " WHERE s.id=?"
        )
        if version is None:
            query, params = sql.format(vcond="v.version=s.current_version"), (sid,)
        else:
            if not isinstance(version, int) or version < 1:
                raise ValueError("version must be a positive integer")
            query, params = sql.format(vcond="v.version=?"), (version, sid)
        row = self.db.execute(query, params).fetchone()
        if row is None:
            raise KeyError(f"unknown source: {sid!r}"
                           + (f" version {version}" if version else ""))
        return dict(row)

    def status(self):
        """Aggregate counters for observability."""
        events = self.db.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
        jobs = {r["state"]: r["n"] for r in self.db.execute(
            "SELECT state, COUNT(*) AS n FROM jobs GROUP BY state")}
        records = {r["status"]: r["n"] for r in self.db.execute(
            "SELECT status, COUNT(*) AS n FROM records GROUP BY status")}
        return {"events": events, "jobs": jobs, "records": records}

    def close(self):
        self.db.close()
