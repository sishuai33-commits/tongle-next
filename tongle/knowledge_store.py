import json
import re
import time
import unicodedata
import uuid
import hashlib
from .presentation import windows_reserved_name
import sqlite3
from .knowledge_relations import Relations

_SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge_cards (
    id TEXT PRIMARY KEY,
    project TEXT NOT NULL,
    "key" TEXT NOT NULL,
    kind TEXT NOT NULL,
    topic TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    path TEXT,
    evidence TEXT NOT NULL,
    state TEXT NOT NULL,
    version INTEGER NOT NULL,
    manually_edited INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(project, "key")
);
CREATE TABLE IF NOT EXISTS knowledge_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id TEXT NOT NULL,
    old_version INTEGER NOT NULL,
    snapshot TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at REAL NOT NULL,
    FOREIGN KEY(card_id) REFERENCES knowledge_cards(id)
);
CREATE TABLE IF NOT EXISTS knowledge_batches (
    batch_id TEXT NOT NULL,
    project TEXT NOT NULL,
    op_hash TEXT NOT NULL,
    review_hash TEXT NOT NULL,
    changed_ids TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY(batch_id, project)
);
"""

_KINDS = {"memory", "knowledge", "framework", "source", "index"}
_STATES = {"active", "conflict", "retired"}


def _collapse(value):
    """Whitespace-insensitive view used only to classify a quote mismatch."""
    return "".join(value.split())
_MAX_BODY = 60000
_MAX_OPS = 8
_MAX_EVID = 12
_SEG_RE = re.compile(r"^[^\x00\\/:*?\"<>|]+$", re.UNICODE)


def _norm_path(p):
    if not isinstance(p, str) or not p:
        raise ValueError("path must be a nonempty string")
    if "\\" in p:
        raise ValueError("path must use forward slashes")
    if p.startswith("/"):
        raise ValueError("path must be relative")
    segs = p.split("/")
    for s in segs:
        if not s or s in (".", "..") or s.startswith("."):
            raise ValueError("invalid path segment")
        if s.endswith((" ", ".")) or not _SEG_RE.match(s):
            raise ValueError("invalid path segment")
        if windows_reserved_name(s):
            raise ValueError("invalid path segment")
    if not p.lower().endswith(".md"):
        raise ValueError("path must have .md suffix")
    return p


def _norm_body(b):
    return " ".join(b.split()).casefold()


def _norm_path_key(p):
    return unicodedata.normalize("NFC", p).casefold()


def _path_conflict(db, pkey, exclude_id=None):
    cur = db.execute("SELECT id, path FROM knowledge_cards WHERE path IS NOT NULL")
    for r in cur.fetchall():
        if exclude_id is not None and r["id"] == exclude_id:
            continue
        if _norm_path_key(r["path"]) == pkey:
            return True
    return False


class KnowledgeStore:
    def __init__(self, engine):
        self.engine = engine
        with engine.locked(), engine.transaction():
            for stmt in _SCHEMA.split(";"):
                stmt = stmt.strip()
                if stmt:
                    engine.db.execute(stmt)
            self.relations=Relations(engine.db)

    # ---------- helpers ----------
    def _row_to_card(self, row):
        d = dict(row)
        d["evidence"] = json.loads(d["evidence"])
        d["manually_edited"] = bool(d["manually_edited"])
        d['body_sha256']=hashlib.sha256(d['body'].encode()).hexdigest()
        d["basis_cards"] = self.relations.list(d["id"])
        return d

    def _get_row(self, cid):
        cur = self.engine.db.execute(
            "SELECT * FROM knowledge_cards WHERE id = ?", (cid,)
        )
        return cur.fetchone()

    def _get_card(self, cid):
        row = self._get_row(cid)
        return self._row_to_card(row) if row else None

    def _get_by_key(self, project, key):
        cur = self.engine.db.execute(
            'SELECT * FROM knowledge_cards WHERE project = ? AND "key" = ?',
            (project, key),
        )
        row = cur.fetchone()
        return self._row_to_card(row) if row else None

    def _require_str(self, v, name, allow_none=False):
        if v is None and allow_none:
            return None
        if not isinstance(v, str) or not v or not v.strip():
            raise ValueError(name + " must be a nonempty string")
        return v

    def _require_version(self, v, name="expected_version"):
        if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
            raise ValueError(name + " must be a positive integer")
        return v

    def _evidence_entry(self, e, project):
        """Validate and normalize exactly one evidence entry against live state.

        This is the single authoritative per-entry check shared by create/retire
        (hard 12-entry incoming cap) and update (inherited set plus <=12 new
        entries): availability, project, current-version and exact-substring
        quote are always enforced, including for inherited tuples.
        """
        if not isinstance(e, dict):
            raise ValueError("evidence entry must be a dict")
        sid = e.get("source_id")
        ver = e.get("version")
        quote = e.get("quote")
        if not isinstance(sid, str) or not sid:
            raise ValueError("evidence source_id must be a nonempty string")
        if isinstance(ver, bool) or not isinstance(ver, int) or ver <= 0:
            raise ValueError("evidence version must be a positive integer")
        if not isinstance(quote, str) or not quote.strip():
            raise ValueError("evidence quote must be a nonempty string")
        if not self.engine.source_available(sid):
            raise ValueError("evidence source unavailable: " + sid)
        # source_available is fail-open for ids absent from source_status, so an
        # id that is not a real source (e.g. a consumption receipt id the model
        # mistook for a source id) reaches here and store.source raises a bare
        # KeyError that used to kill the whole batch as unclassified. Convert it
        # to the same validation-error family as the checks above.
        try:
            src = self.engine.store.source(sid)
        except KeyError:
            raise ValueError("evidence source unknown (not a source id): " + sid)
        if src.get("project") != project:
            raise ValueError("evidence source project mismatch")
        if src.get("current_version") != ver:
            raise ValueError("evidence source version mismatch")
        text = src.get("text")
        if not isinstance(text, str) or quote not in text:
            # Bounded diagnosis without copying the quote or the source: a
            # batch parked on this error is otherwise opaque, and four such
            # batches on 2026-09-12 could not be told apart from a model that
            # simply paraphrased. Whitespace-normalised match tells the two
            # apart next time.
            hint = "unknown" if not isinstance(text, str) else (
                "whitespace-only" if _collapse(quote) in _collapse(text) else "no")
            raise ValueError(
                "evidence quote must be an exact substring"
                " (len=%d; equal ignoring whitespace=%s)" % (len(quote), hint))
        return {"source_id": sid, "version": ver, "quote": quote}

    def _validate_evidence(self, ev, project):
        if not isinstance(ev, list) or not ev or len(ev) > _MAX_EVID:
            raise ValueError("evidence must be a nonempty list of at most 12 entries")
        seen = set()
        out = []
        for e in ev:
            entry = self._evidence_entry(e, project)
            ref = (entry["source_id"], entry["version"], entry["quote"])
            if ref not in seen:
                seen.add(ref)
                out.append(entry)
        return out

    def _validate_update_evidence(self, ev, project, card_id):
        """Evidence for an UPDATE of an actual existing own card.

        Two regimes, split purely on the incoming length so the legacy contract
        is preserved exactly:

        * ``len(ev) <= _MAX_EVID`` (the ordinary legacy case) delegates the
          unchanged ``_validate_evidence`` check: the incoming set is the whole
          provenance, so an independently reviewed correction may *replace* a
          stale source version ref. Nothing about the old row is consulted.
        * ``len(ev) > _MAX_EVID`` (the extended growth case) inherits the
          changed card's already-persisted provenance verbatim and adds only
          ``<= _MAX_EVID`` genuinely new unique entries. Here the whole old
          tuple set must be retained and every entry is still checked.

        Inheritance is derived from the real card row, never from a
        caller-supplied flag, and applies only on the extended path.
        """
        if not isinstance(ev, list):
            raise ValueError("evidence must be a nonempty list of at most 12 entries")
        if len(ev) <= _MAX_EVID:
            return self._validate_evidence(ev, project)
        row = self._get_row(card_id) if isinstance(card_id, str) and card_id else None
        inherited_keys = set()
        if row is not None:
            try:
                current = json.loads(row["evidence"])
            except (ValueError, TypeError):
                current = None
            if isinstance(current, list):
                for item in current:
                    if isinstance(item, dict):
                        inherited_keys.add(
                            (item.get("source_id"), item.get("version"), item.get("quote")))
        seen = set()
        new_count = 0
        out = []
        for e in ev:
            entry = self._evidence_entry(e, project)
            ref = (entry["source_id"], entry["version"], entry["quote"])
            if ref in seen:
                continue
            seen.add(ref)
            if ref not in inherited_keys:
                new_count += 1
            out.append(entry)
        if new_count > _MAX_EVID:
            raise ValueError("evidence new entries exceed 12")
        if inherited_keys and not inherited_keys.issubset(seen):
            raise ValueError("update must retain all existing evidence")
        return out

    def _validate_review(self, review, op_hash):
        if not isinstance(review, dict):
            raise ValueError("review must be a dict")
        if review.get("approved") is not True:
            raise ValueError("review not approved")
        findings = review.get("findings")
        if not isinstance(findings, list) or findings:
            raise ValueError("review findings must be an empty list")
        ph = review.get("proposal_hash")
        if not isinstance(ph, str) or not ph:
            raise ValueError("review proposal_hash must be a nonempty string")
        if ph != op_hash:
            raise ValueError("review proposal_hash mismatch")

    def _validate_ops(self, operations, project):
        if not isinstance(operations, list) or len(operations) > _MAX_OPS:
            raise ValueError("operations must be a list of at most 8 entries")
        norm = []
        touched_ids = set()
        touched_keys = set()
        batch_bodies = set()
        for i, op in enumerate(operations):
            if not isinstance(op, dict):
                raise ValueError("operation must be a dict")
            kind = op.get("op")
            if kind not in ("create", "update", "retire"):
                raise ValueError("unknown operation: " + repr(kind))
            if "path" in op and op["path"] is not None:
                raise ValueError("model operations cannot set path")
            allowed = {"op", "evidence", "reason", "basis_cards"}
            if kind == "create":
                allowed |= {"key", "kind", "topic", "title", "body", "state"}
            elif kind == "update":
                allowed |= {"id", "expected_version", "body", "title", "topic", "state"}
            else:
                allowed |= {"id", "expected_version"}
            for f in op:
                if f not in allowed:
                    raise ValueError("unexpected field for " + kind + ": " + f)
            refs=op.get('basis_cards',self.relations.list(op.get('id')) if isinstance(op.get('id'),str) else [])
            changing=[x['id'] for x in operations if isinstance(x,dict) and isinstance(x.get('id'),str)]
            basis=self.relations.validate(project,op.get('id'),refs,changing) if kind!='retire' else self.relations.list(op.get('id'))
            if kind=='retire' and refs!=basis:raise ValueError('retirement cannot rewrite dependency history')
            todo=list(basis);seen_bases=set()
            while todo and kind!='retire':
                ref=todo.pop();bid=ref['card_id']
                if bid in seen_bases:continue
                seen_bases.add(bid);parent=self._get_card(bid)
                for cited in parent['evidence']:
                    if not self.engine.source_available(cited['source_id']):raise ValueError('basis evidence source unavailable')
                    try:
                        source=self.engine.store.source(cited['source_id'])
                    except KeyError:
                        raise ValueError('basis evidence source missing: ' + cited['source_id'])
                    if source['current_version']!=cited['version'] or cited['quote'] not in source['text']:raise ValueError('basis evidence changed')
                todo.extend(parent['basis_cards'])
            ev=[] if basis and op.get('evidence',[])==[] else (
                self._validate_update_evidence(op.get('evidence'), project, op.get('id'))
                if kind=='update' else self._validate_evidence(op.get('evidence'),project))
            reason = op.get("reason")
            if not isinstance(reason, str) or not reason:
                raise ValueError("operation reason must be a nonempty string")
            o = {"op": kind, "evidence": ev, "reason": reason,"basis_cards":basis}
            if kind == "create":
                key = self._require_str(op.get("key"), "key")
                k = op.get("kind")
                if not isinstance(k, str) or k not in _KINDS:
                    raise ValueError("invalid kind")
                topic = self._require_str(op.get("topic"), "topic")
                title = self._require_str(op.get("title"), "title")
                body = self._require_str(op.get("body"), "body")
                if len(body) > _MAX_BODY:
                    raise ValueError("body too large")
                state = op.get("state", "active")
                if not isinstance(state, str) or state not in ("active", "conflict"):
                    raise ValueError("invalid create state")
                if key in touched_keys:
                    raise ValueError("duplicate new key in batch")
                touched_keys.add(key)
                if self._get_by_key(project, key) is not None:
                    raise ValueError("key already exists: " + key)
                nb = _norm_body(body)
                if nb in batch_bodies:
                    raise ValueError("duplicate active body in batch")
                batch_bodies.add(nb)
                cur = self.engine.db.execute(
                    "SELECT body FROM knowledge_cards WHERE project IN (?, 'shared') AND state = 'active'",
                    (project,),
                )
                for r in cur.fetchall():
                    if _norm_body(r["body"]) == nb:
                        raise ValueError("duplicate active body in scope")
                o.update(key=key, kind=k, topic=topic, title=title, body=body, state=state)
            elif kind == "update":
                cid = self._require_str(op.get("id"), "id")
                ver = self._require_version(op.get("expected_version"))
                body = self._require_str(op.get("body"), "body")
                if len(body) > _MAX_BODY:
                    raise ValueError("body too large")
                state = op.get("state")
                if state is not None and (not isinstance(state, str) or state not in ("active", "conflict")):
                    raise ValueError("invalid update state")
                title = op.get("title")
                if title is not None:
                    title = self._require_str(title, "title")
                topic = op.get("topic")
                if topic is not None:
                    topic = self._require_str(topic, "topic")
                if cid in touched_ids:
                    raise ValueError("duplicate target id in batch")
                touched_ids.add(cid)
                o.update(id=cid, expected_version=ver, body=body, state=state,
                         title=title, topic=topic)
            else:  # retire
                cid = self._require_str(op.get("id"), "id")
                ver = self._require_version(op.get("expected_version"))
                if cid in touched_ids:
                    raise ValueError("duplicate target id in batch")
                touched_ids.add(cid)
                o.update(id=cid, expected_version=ver)
            norm.append(o)
        return norm

    def _check_existing(self, o, project, review=None):
        """Validate against current DB state; raise ValueError on conflict."""
        self._check_target(o, project)
        if o["op"] == "create":
            return
        row = self._get_row(o["id"])
        if row["manually_edited"]:
            checks=(review or {}).get('human_checks',[])
            preserved=any(isinstance(c,dict) and c.get('id')==row['id'] and c.get('version')==row['version'] and c.get('body_sha256')==hashlib.sha256(row['body'].encode()).hexdigest() and c.get('preserved') is True and isinstance(c.get('reason'),str) and len(c['reason'].strip())>=10 for c in checks) if isinstance(checks,list) else False
            if o['op']!='update' or not preserved:raise ValueError("card was manually edited; fresh preservation review required")

    def _check_target(self, o, project):
        """Read-only target existence/state/version check (no human_checks yet)."""
        if o["op"] == "create":
            return
        row = self._get_row(o["id"])
        if row is None:
            raise ValueError("card not found: " + o["id"])
        if row["project"] != project:
            raise ValueError("card not found: " + o["id"])
        if row["state"] == "retired":
            raise ValueError("cannot modify retired card")
        if row["version"] != o["expected_version"]:
            raise ValueError("version changed for: " + o["id"])

    @staticmethod
    def _validation_field(message):
        """Map a bounded validation message to the offending operation field."""
        msg = message or ''
        if 'source_id' in msg or 'source unknown' in msg or 'source unavailable' in msg:
            return 'evidence.source_id'
        if 'evidence version' in msg:
            return 'evidence.version'
        if 'quote' in msg:
            return 'evidence.quote'
        if 'evidence' in msg:
            return 'evidence'
        if 'expected_version' in msg:
            return 'expected_version'
        if 'unexpected field' in msg:
            return 'field'
        if 'basis' in msg:
            return 'basis_cards'
        for field in ('reason', 'key', 'kind', 'topic', 'title', 'body', 'state'):
            if field in msg:
                return field
        if 'id' in msg:
            return 'id'
        return 'operation'

    def validate_operations(self, operations, project):
        """Read-only deterministic validation of a proposed operation batch.

        Runs the *same* authoritative checks ``apply`` runs (``_validate_ops``
        plus the target existence/state/version check) but returns bounded
        per-op findings instead of raising, so the compile stage can correct
        the exact index/field before an independent review is spent. There are
        no side effects, no coercion, no fuzzy quote substitution and no
        apply-then-rollback: a version/basis/field/quote error is reported as
        it is. Human-edit preservation is deliberately *not* decided here --
        that remains the independent verifier's job.
        """
        if not isinstance(operations, list):
            return {'ok': False, 'operations': None,
                    'problems': [{'op': None, 'field': 'operations',
                                  'error': 'operations must be a list'}]}
        problems = []
        for index, op in enumerate(operations):
            try:
                norm = self._validate_ops([op], project)[0]
                self._check_target(norm, project)
            except ValueError as exc:
                problems.append({'op': index,
                                 'field': self._validation_field(str(exc)),
                                 'error': ' '.join(str(exc).split())[:200]})
        # Whole-batch cross-op checks (duplicate keys/ids/bodies, basis in a
        # changing batch, batch budget). Localized per-op findings win if any.
        batch_norm = None
        try:
            batch_norm = self._validate_ops(operations, project)
        except ValueError as exc:
            if not problems:
                problems.append({'op': None,
                                 'field': self._validation_field(str(exc)),
                                 'error': ' '.join(str(exc).split())[:200]})
        return {'ok': not problems, 'problems': problems[:8],
                'operations': batch_norm if not problems else None}

    def _snapshot(self, row):
        return json.dumps(
            dict({k: row[k] for k in row.keys() if k != "updated_at"},basis_cards=self.relations.list(row["id"])),
            ensure_ascii=False, sort_keys=True,
        )

    def _add_revision(self, row, reason):
        self.engine.db.execute(
            "INSERT INTO knowledge_revisions (card_id, old_version, snapshot, reason, created_at)"
            " VALUES (?,?,?,?,?)",
            (row["id"], row["version"], self._snapshot(row), reason, time.time()),
        )

    def _apply_one(self, o, project):
        now = time.time()
        if o["op"] == "create":
            cid = uuid.uuid4().hex
            self.engine.db.execute(
                'INSERT INTO knowledge_cards (id, project, "key", kind, topic, title,'
                ' body, path, evidence, state, version, manually_edited, created_at, updated_at)'
                " VALUES (?,?,?,?,?,?,?,NULL,?,?,1,0,?,?)",
                (cid, project, o["key"], o["kind"], o["topic"], o["title"], o["body"],
                 json.dumps(o["evidence"], ensure_ascii=False), o["state"], now, now),
            )
            return cid
        row = self._get_row(o["id"])
        if o["op"] == "update":
            body = o["body"] if o["body"] is not None else row["body"]
            title = o["title"] if o["title"] is not None else row["title"]
            topic = o["topic"] if o["topic"] is not None else row["topic"]
            state = o["state"] if o["state"] is not None else row["state"]
            same = (row["body"] == body and row["title"] == title
                    and row["topic"] == topic and row["state"] == state
                    and json.loads(row["evidence"]) == o["evidence"]
                    and self.relations.list(row["id"]) == o["basis_cards"])
            if same:
                return None
            self._add_revision(row, o["reason"])
            self.engine.db.execute(
                'UPDATE knowledge_cards SET body=?, title=?, topic=?, state=?,'
                " evidence=?, version=version+1, updated_at=? WHERE id=?",
                (body, title, topic, state,
                 json.dumps(o["evidence"], ensure_ascii=False), now, o["id"]),
            )
            return o["id"]
        # retire
        if row["state"] == "retired":
            return None
        self._add_revision(row, o["reason"])
        self.engine.db.execute(
            "UPDATE knowledge_cards SET state='retired', version=version+1,"
            " evidence=?, updated_at=? WHERE id=?",
            (json.dumps(o["evidence"], ensure_ascii=False), now, o["id"]),
        )
        return o["id"]

    # ---------- public API ----------
    def get(self, card_id):
        if not isinstance(card_id, str) or not card_id:
            raise ValueError("card_id must be a nonempty string")
        with self.engine.locked(), self.engine.transaction():
            return self._get_card(card_id)

    def list(self, project=None, include_inactive=False):
        if project is not None and not isinstance(project, str):
            raise ValueError("project must be a string or None")
        sql = "SELECT * FROM knowledge_cards"
        args = []
        if project is not None:
            sql += " WHERE project = ?"
            args.append(project)
        if not include_inactive:
            sql += (" AND" if project is not None else " WHERE") + " state = 'active'"
        sql += ' ORDER BY "key"'
        with self.engine.locked(), self.engine.transaction():
            cur = self.engine.db.execute(sql, args)
            return [self._row_to_card(r) for r in cur.fetchall()]

    def seed(self, project, key, kind, topic, title, body, path=None,
             evidence=None, state="active"):
        project = self._require_str(project, "project")
        key = self._require_str(key, "key")
        if not isinstance(kind, str) or kind not in _KINDS:
            raise ValueError("invalid kind")
        topic = self._require_str(topic, "topic")
        title = self._require_str(title, "title")
        body = self._require_str(body, "body")
        if len(body) > _MAX_BODY:
            raise ValueError("body too large")
        if not isinstance(state, str) or state not in _STATES:
            raise ValueError("invalid state")
        if path is not None:
            path = _norm_path(path)
            pkey = _norm_path_key(path)
        if evidence is None:
            evidence = []
        if not isinstance(evidence, list):
            raise ValueError("evidence must be a list")
        for e in evidence:
            if not isinstance(e, dict):
                raise ValueError("evidence entry must be a dict")
            if not isinstance(e.get("source_id"), str) or not e.get("source_id"):
                raise ValueError("evidence source_id must be a nonempty string")
            v = e.get("version")
            if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
                raise ValueError("evidence version must be a positive integer")
            if not isinstance(e.get("quote"), str) or not e.get("quote"):
                raise ValueError("evidence quote must be a nonempty string")
        with self.engine.locked(), self.engine.transaction():
            existing = self._get_by_key(project, key)
            if existing is not None:
                same = (existing["kind"] == kind and existing["topic"] == topic
                        and existing["title"] == title and existing["body"] == body
                        and existing["path"] == path and existing["state"] == state
                        and existing["evidence"] == evidence)
                if same:
                    return existing
                raise ValueError("seed key already exists with different content")
            if path is not None and _path_conflict(self.engine.db, pkey):
                raise ValueError("path collision")
            cid = uuid.uuid4().hex
            now = time.time()
            self.engine.db.execute(
                'INSERT INTO knowledge_cards (id, project, "key", kind, topic, title,'
                ' body, path, evidence, state, version, manually_edited, created_at, updated_at)'
                " VALUES (?,?,?,?,?,?,?,?,?,?,1,0,?,?)",
                (cid, project, key, kind, topic, title, body, path,
                 json.dumps(evidence, ensure_ascii=False), state, now, now),
            )
            return self._get_card(cid)

    def apply(self, batch_id, project, operations, review):
        batch_id = self._require_str(batch_id, "batch_id")
        project = self._require_str(project, "project")
        op_hash = hashlib.sha256(
            json.dumps(operations, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")).encode()
        ).hexdigest()
        review_hash = hashlib.sha256(
            json.dumps(review, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")).encode()
        ).hexdigest()
        with self.engine.locked(), self.engine.transaction():
            self._validate_review(review, op_hash)
            cur = self.engine.db.execute(
                "SELECT * FROM knowledge_batches WHERE batch_id = ? AND project = ?",
                (batch_id, project),
            )
            prior = cur.fetchone()
            if prior is not None:
                if (prior["op_hash"] != op_hash or prior["review_hash"] != review_hash):
                    raise ValueError("batch id reused with different content")
                changed = json.loads(prior["changed_ids"])
                return {"batch_id": batch_id, "changed_ids": changed, "replayed": True}
            norm = self._validate_ops(operations, project)
            for o in norm:
                self._check_existing(o, project, review)
            changed = []
            for o in norm:
                cid = self._apply_one(o, project)
                if cid is not None:
                    self.relations.set(cid,o['basis_cards'])
                    changed.append(cid)
            self.engine.db.execute(
                "INSERT INTO knowledge_batches (batch_id, project, op_hash, review_hash,"
                " changed_ids, created_at) VALUES (?,?,?,?,?,?)",
                (batch_id, project, op_hash, review_hash,
                 json.dumps(changed, ensure_ascii=False), time.time()),
            )
            return {"batch_id": batch_id, "changed_ids": changed, "replayed": False}

    def manual_edit(self, card_id, expected_version, body):
        card_id = self._require_str(card_id, "card_id")
        expected_version = self._require_version(expected_version)
        body = self._require_str(body, "body")
        if len(body) > _MAX_BODY:
            raise ValueError("body too large")
        with self.engine.locked(), self.engine.transaction():
            row = self._get_row(card_id)
            if row is None:
                raise ValueError("card not found")
            if row["version"] != expected_version:
                raise ValueError("version changed")
            if row["body"] == body:
                return self._row_to_card(row)
            self._add_revision(row, "manual_edit")
            self.engine.db.execute(
                "UPDATE knowledge_cards SET body=?, version=version+1,"
                " manually_edited=1, updated_at=? WHERE id=?",
                (body, time.time(), card_id),
            )
            return self._get_card(card_id)

    def set_path(self, card_id, expected_version, path):
        card_id = self._require_str(card_id, "card_id")
        expected_version = self._require_version(expected_version)
        path = _norm_path(path)
        pkey = _norm_path_key(path)
        with self.engine.locked(), self.engine.transaction():
            row = self._get_row(card_id)
            if row is None:
                raise ValueError("card not found")
            if row["version"] != expected_version:
                raise ValueError("version changed")
            if _path_conflict(self.engine.db, pkey, exclude_id=card_id):
                raise ValueError("path collision")
            self.engine.db.execute(
                "UPDATE knowledge_cards SET path=?, updated_at=? WHERE id=?",
                (path, time.time(), card_id),
            )
            return self._get_card(card_id)

    def history(self, card_id):
        card_id = self._require_str(card_id, "card_id")
        with self.engine.locked(), self.engine.transaction():
            cur = self.engine.db.execute(
                "SELECT card_id, old_version, snapshot, reason, created_at"
                " FROM knowledge_revisions WHERE card_id = ? ORDER BY id",
                (card_id,),
            )
            out = []
            for r in cur.fetchall():
                snap = json.loads(r["snapshot"])
                snap["evidence"] = json.loads(snap["evidence"])
                out.append({
                    "card_id": r["card_id"],
                    "old_version": r["old_version"],
                    "snapshot": snap,
                    "reason": r["reason"],
                    "created_at": r["created_at"],
                })
            return out
