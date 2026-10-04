import hashlib
import json
import os
import re
import tempfile
import time
import unicodedata
from pathlib import Path

from .portability import fsync_directory

_H1_RE = re.compile(r"^#\s+", re.MULTILINE)
_META_RE = re.compile(r"\A---(?:\r?\n).*?(?:\r?\n)---(?:\r?\n)", re.DOTALL)
_ID_RE = re.compile(r"tongle-card: \{.*\}")
_MAX_SCAN = 2 * 1024 * 1024


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_WRAPPER = re.compile(r'(?:\r?\n){2}%%\r?\ntongle-card: (\{[^\r\n]*\})\r?\n%%(?:\r?\n)?\Z')


def _parse_wrapper(text):
    m = _WRAPPER.search(text)
    if not m:return None
    try:meta=json.loads(m.group(1))
    except ValueError:return None
    if not isinstance(meta,dict) or set(meta)!={'id','version'}:return None
    if not isinstance(meta['id'],str) or type(meta['version']) is not int or meta['version']<1:return None
    return meta['id'],meta['version']


def _strip_wrapper(text):
    if _parse_wrapper(text) is None:return text
    m=_WRAPPER.search(text)
    return text[:m.start()] if m else text


class KnowledgeFiles:
    """Local publisher/reconciler for the knowledge-card Markdown vault."""

    def __init__(self, engine, cards=None):
        self.engine = engine
        self.cards = cards if cards is not None else KnowledgeStore(engine)
        from .presentation import label
        self.label = label
        row = engine.db.execute(
            "SELECT value FROM meta WHERE key='vault'").fetchone()
        if row is None:
            raise ValueError("vault meta missing")
        self.root = Path(row[0])
        with engine.locked(),engine.transaction():
            engine.db.execute(
                "CREATE TABLE IF NOT EXISTS knowledge_files ("
                " card_id TEXT PRIMARY KEY, file_hash TEXT, version INTEGER,"
                " path TEXT, state TEXT)")
            engine.db.execute(
                "CREATE TABLE IF NOT EXISTS knowledge_publications ("
                " card_id TEXT PRIMARY KEY, path TEXT, expected_hash TEXT,"
                " payload TEXT, target_hash TEXT, version INTEGER)")

    # ---------- helpers ----------

    def _tracking(self, cid):
        row = self.engine.db.execute(
            "SELECT file_hash, version, path, state FROM knowledge_files"
            " WHERE card_id = ?", (cid,)).fetchone()
        if row is None:
            return None
        return {"file_hash": row[0], "version": row[1],
                "path": row[2], "state": row[3]}

    def _intent(self, cid):
        row = self.engine.db.execute(
            "SELECT path, expected_hash, payload, target_hash, version"
            " FROM knowledge_publications WHERE card_id = ?", (cid,)).fetchone()
        if row is None:
            return None
        return {"path": row[0], "expected_hash": row[1], "payload": row[2],
                "target_hash": row[3], "version": row[4]}

    def _validate_rel(self, rel):
        if not rel or "\x00" in rel or "\\" in rel:
            raise ValueError("unsafe path")
        p = Path(rel)
        if p.is_absolute():
            raise ValueError("absolute path")
        parts = p.parts
        if any(part in ("", ".", "..") or part.startswith(".")
               for part in parts):
            raise ValueError("unsafe path segment")
        if p.suffix != ".md" or len(parts) < 2:
            raise ValueError("need namespaced .md path")
        folded = unicodedata.normalize("NFC", str(p)).casefold()
        if folded != str(p).casefold():
            raise ValueError("path must be NFC normalized")
        return parts

    def _check_symlinks(self, parts):
        current = self.root
        if current.is_symlink():
            raise ValueError("vault root is a symlink")
        probe = current.parent
        while True:
            if probe.is_symlink():
                raise ValueError("symlink in vault ancestors")
            if probe.parent == probe:
                break
            probe = probe.parent
        for part in parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("symlink in path")

    def _safe_target(self, rel):
        parts = self._validate_rel(rel)
        self._check_symlinks(parts)
        return self.root.joinpath(*parts)

    def _default_path(self, card, taken):
        title = self.label(card.get("key") or card.get("title") or card["id"],
                           card["id"])
        if card.get("kind") == "memory":
            proj = card.get("project") or ""
            prior={Path(c['path']).parts[1] for c in self.cards.list(proj,include_inactive=True) if c.get('path') and c['path'].startswith('项目/') and len(Path(c['path']).parts)>2}
            cfgpath=self.engine.home/'config.json'
            cfg=json.loads(cfgpath.read_text(encoding='utf-8')) if cfgpath.is_file() else {}
            names=cfg.get('knowledge_project_names',{})
            name=names.get(proj,proj) if isinstance(names,dict) else proj
            folder=next(iter(prior)) if len(prior)==1 else self.label(name if re.search(r"[\u4e00-\u9fff]",str(name)) else '项目·'+str(name))
            base = "项目/{}/{}.md".format(folder, title)
        else:
            topic = card.get("topic") or ""
            folder = self.label(topic) if re.search(r"[\u4e00-\u9fff]", topic) else "其他主题"
            base = "知识/{}/{}.md".format(folder, title)
        candidate = base
        n = 1
        while candidate.casefold() in taken:
            n += 1
            candidate = base[:-3] + str(n) + ".md"
        taken.add(candidate.casefold())
        return candidate

    def _human(self, card):
        body = card.get("body") or ""
        state = card.get("state")
        if state in ("conflict", "retired"):
            note = ("> [!warning] 存在待核分歧\n"
                    if state == "conflict"
                    else "> [!info] 已退出当前使用\n")
            if note in body:  # banner already captured into body (e.g. by a
                return body   # previous manual edit); never stack a second one
            m = _META_RE.match(body)
            if m:
                body = body[:m.end()] + note + body[m.end():]
            else:
                body = note + body
        return body

    def render(self, card):
        body = self._human(card)
        if not _H1_RE.search(body):
            title = self.label(card.get("title") or card.get("key")
                               or card["id"], card["id"])
            h = "# {}\n\n".format(title)
            fm=_META_RE.match(body)
            body = body[:fm.end()]+h+body[fm.end():] if fm else h+body
        meta = json.dumps({"id": card["id"], "version": card["version"]},
                          ensure_ascii=False, separators=(",", ":"))
        return body + "\n\n%%\ntongle-card: " + meta + "\n%%\n"

    def _write_atomic(self, target, payload, exclusive, expected_hash=None):
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmpname = tempfile.mkstemp(dir=str(target.parent),
                                       prefix=".tongle-tmp-")
        tmp = Path(tmpname)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            self._safe_target(target.relative_to(self.root).as_posix())
            latest=self._read(target)
            if (_sha(latest) if latest is not None else None) != expected_hash:
                raise FileExistsError('file changed during publication')
            if exclusive:
                os.link(tmp, target)
                os.unlink(tmp)
            else:
                os.replace(tmp, target)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        fsync_directory(target.parent)

    def _read(self, target):
        try:
            data = target.read_bytes()
        except FileNotFoundError:
            return None
        if len(data) > _MAX_SCAN:
            raise ValueError('file exceeds safe read bound')
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError('file is not UTF-8')

    def _commit_receipt(self, card, rel, target_hash, version):
        self.engine.db.execute(
            "INSERT INTO knowledge_files (card_id, file_hash, version, path,"
            " state) VALUES (?,?,?,?, 'ok')"
            " ON CONFLICT(card_id) DO UPDATE SET file_hash=excluded.file_hash,"
            " version=excluded.version, path=excluded.path, state='ok'",
            (card["id"], target_hash, version, rel))
        self.engine.db.execute(
            "DELETE FROM knowledge_publications WHERE card_id = ?",
            (card["id"],))
        self.engine.db.commit()

    # ---------- recovery ----------

    def _flag(self,cid,state):
        with self.engine.transaction():
            self.engine.db.execute("UPDATE knowledge_files SET state=? WHERE card_id=?",(state,cid))

    def _publication_valid(self, card, seen=None):
        if not card:
            return False
        # Retired/conflict notes remain publishable as withdrawal information.
        if card['state'] != 'active':
            return True
        seen = set() if seen is None else seen
        if card['id'] in seen or len(seen) > 32:
            return False
        for ref in card.get('evidence', []):
            if not self.engine.source_available(ref['source_id']):
                return False
            try:
                source = self.engine.store.source(ref['source_id'])
            except (KeyError, ValueError):
                return False
            if source['current_version'] != ref['version'] or ref['quote'] not in source['text']:
                return False
        for ref in card.get('basis_cards', []):
            basis = self.cards.get(ref['card_id'])
            if not basis or basis['state'] != 'active' or basis['version'] != ref['version'] or not self._publication_valid(basis, seen | {card['id']}):
                return False
        return True

    def recover(self, card_ids=None):
        conflicts=[]
        with self.engine.locked():
            rows=self.engine.db.execute('SELECT * FROM knowledge_publications').fetchall()
            for row in rows:
                intent=dict(row);cid=intent['card_id']
                if card_ids is not None and cid not in card_ids:
                    continue
                try:
                    card=self.cards.get(cid);target=self._safe_target(intent['path'])
                    text=self._read(target);actual=_sha(text) if text is not None else None
                    if card['version']!=intent['version']:
                        if actual==intent['expected_hash']:
                            with self.engine.transaction():self.engine.db.execute('DELETE FROM knowledge_publications WHERE card_id=?',(cid,))
                            continue
                        raise ValueError('obsolete publication conflicts with disk')
                    if not self._publication_valid(card) or _sha(self.render(card)) != intent['target_hash']:
                        raise ValueError('publication evidence or payload changed')
                    if actual==intent['target_hash']:
                        self._commit_receipt(card,intent['path'],actual,intent['version']);continue
                    if actual!=intent['expected_hash']:raise ValueError('publication bytes changed')
                    self._write_atomic(target,intent['payload'],intent['expected_hash'] is None,intent['expected_hash'])
                    self._commit_receipt(card,intent['path'],intent['target_hash'],intent['version'])
                except (ValueError,FileExistsError):
                    conflicts.append(cid);self._flag(cid,'conflict')
        return conflicts

    # ---------- publish ----------

    def publish(self, card_ids=None):
        result = {"published": 0, "unchanged": 0,
                  "conflicts": [], "missing": []}
        with self.engine.locked():
            blocked=set(self.recover(card_ids))
            if card_ids is None:
                cards = self.cards.list(include_inactive=True)
            else:
                cards = []
                for cid in card_ids:
                    try:
                        found=self.cards.get(cid)
                        if found:cards.append(found)
                    except ValueError:
                        continue
            taken = set()
            for row in self.engine.db.execute("SELECT path FROM knowledge_files"):
                if row[0]:
                    taken.add(row[0].casefold())
            for base in ("知识", "项目", "归档"):
                d = self.root / base
                if d.is_dir() and not d.is_symlink():
                    for p in d.rglob("*.md"):
                        if p.is_file() and not p.is_symlink():
                            taken.add(p.relative_to(self.root)
                                      .as_posix().casefold())
            for card in cards:
                cid = card["id"]
                tr = self._tracking(cid)
                if cid in blocked or (tr and tr['state']=='conflict'):
                    result['conflicts'].append(cid);continue
                if tr and tr["state"] == "missing":
                    result["missing"].append(cid)
                    continue
                rel = card.get("path") or (tr and tr["path"])
                if not rel:
                    rel = self._default_path(card, taken)
                    try:
                        self.cards.set_path(cid, card["version"], rel)
                    except ValueError:
                        result["conflicts"].append(cid)
                        continue
                    card = dict(card)
                    card["path"] = rel
                try:
                    target = self._safe_target(rel)
                except ValueError:
                    result["conflicts"].append(cid)
                    continue
                if not self._publication_valid(card):
                    result["conflicts"].append(cid)
                    continue
                payload = self.render(card)
                target_hash = _sha(payload)
                if tr and tr["file_hash"] == target_hash:
                    text = self._read(target)
                    if text is not None and _sha(text) == target_hash:
                        result["unchanged"] += 1
                        continue
                    result["conflicts"].append(cid)
                    continue
                text = self._read(target)
                on_disk = _sha(text) if text is not None else None
                if tr and tr["path"] == rel:
                    if on_disk is None:
                        result["missing"].append(cid)
                        self.engine.db.execute(
                            "UPDATE knowledge_files SET state='missing'"
                            " WHERE card_id = ?", (cid,))
                        self.engine.db.commit()
                        continue
                    if on_disk != tr["file_hash"]:
                        result["conflicts"].append(cid)
                        continue
                    expected = tr["file_hash"]
                    exclusive = False
                else:
                    if on_disk is not None:
                        result["conflicts"].append(cid)
                        continue
                    expected = None
                    exclusive = True
                self.engine.db.execute(
                    "INSERT INTO knowledge_publications (card_id, path,"
                    " expected_hash, payload, target_hash, version)"
                    " VALUES (?,?,?,?,?,?)"
                    " ON CONFLICT(card_id) DO UPDATE SET path=excluded.path,"
                    " expected_hash=excluded.expected_hash,"
                    " payload=excluded.payload, target_hash=excluded.target_hash,"
                    " version=excluded.version",
                    (cid, rel, expected, payload, target_hash, card["version"]))
                self.engine.db.commit()
                again = self._read(target)
                now_hash = _sha(again) if again is not None else None
                if now_hash != on_disk:
                    result["conflicts"].append(cid)
                    continue
                try:
                    self._write_atomic(target, payload, exclusive, expected)
                except FileExistsError:
                    result["conflicts"].append(cid)
                    continue
                self._commit_receipt(card, rel, target_hash, card["version"])
                result["published"] += 1
        for cid in result['conflicts']:self._flag(cid,'conflict')
        for cid in result['missing']:self._flag(cid,'missing')
        return result

    # ---------- adopt ----------

    def adopt(self, card_id, path, expected_hash):
        rel = path.as_posix() if isinstance(path, Path) else str(path)
        with self.engine.locked():
            target = self._safe_target(rel)
            text = self._read(target)
            if text is None or _sha(text) != expected_hash:
                raise ValueError("file missing or hash mismatch")
            if 'tongle-card:' in text:
                raise ValueError("file already carries a tongle-card identity")
            row = self.engine.db.execute(
                "SELECT card_id FROM knowledge_files WHERE path = ?"
                " AND card_id != ?", (rel, card_id)).fetchone()
            if row:
                raise ValueError("path bound to another card")
            self.cards.set_path(card_id, self.cards.get(card_id)['version'], rel)
            self.engine.db.execute(
                "INSERT INTO knowledge_files (card_id, file_hash, version,"
                " path, state) VALUES (?,?,0,?,'ok')"
                " ON CONFLICT(card_id) DO UPDATE SET file_hash=excluded.file_hash,"
                " version=0, path=excluded.path, state='ok'",
                (card_id, expected_hash, rel))
            self.engine.db.commit()

    # ---------- sync ----------

    def _scan(self):
        found = {}
        for base in ("知识", "项目", "归档"):
            d = self.root / base
            if not d.is_dir() or d.is_symlink():
                continue
            for p in d.rglob("*.md"):
                try:
                    if p.is_symlink() or not p.is_file():
                        continue
                    rel = p.relative_to(self.root).as_posix()
                    self._safe_target(rel)
                except ValueError:
                    continue
                try:text = self._read(p)
                except (ValueError,OSError):continue
                if text is None:
                    continue
                meta = _parse_wrapper(text)
                if meta is None:
                    continue
                cid, _ver = meta
                found.setdefault(cid, []).append(rel)
        return found

    def _repair_moved_links(self, result):
        """Version path-only edits; publication still uses the existing hash CAS.

        Retain move receipts so a skipped human edit can be reconciled on a later
        sync. An occupied former path or ambiguous filename is never guessed.
        """
        moves = self.engine.db.execute(
            'SELECT card_id,old_path FROM knowledge_link_moves').fetchall()
        if not moves:
            return
        norm = lambda value: unicodedata.normalize('NFC', value).casefold()
        cards = self.cards.list(include_inactive=True)
        bindings = {norm(c['path']): c['id'] for c in cards if c['path']}
        names = {}
        occupied = set()
        for p in self.root.rglob('*.md'):
            rel = p.relative_to(self.root).as_posix()
            if any(part.startswith('.') for part in p.relative_to(self.root).parts):
                continue
            # Include even unsupported/symlink names in ambiguity detection.
            occupied.add(norm(rel))
            names.setdefault(norm(p.stem), set()).add(bindings.get(norm(rel), rel))
        replacements = {}
        bare = {}
        for cid, old in moves:
            card = next((c for c in cards if c['id'] == cid), None)
            if not card or not card['path'] or card['path'] == old:
                continue
            if norm(old) in occupied or norm(old) in bindings:
                continue
            dest = card['path']
            tracking = self._tracking(cid)
            if not tracking or tracking['path'] != dest or tracking['state'] != 'ok':
                continue
            try:
                target_text = self._read(self._safe_target(dest))
            except (ValueError, OSError):
                continue
            if target_text is None or _parse_wrapper(target_text) != (cid, tracking['version']):
                continue
            replacements[norm(old)] = dest
            key = norm(Path(old).stem)
            names.setdefault(key, set()).add(cid)
            bare.setdefault(key, set()).add((cid, dest))
        def rewrite(body):
            # Markdown code fences (including unclosed fences), inline code,
            # indented examples, YAML and comments are opaque to link repair.
            protected = re.compile(
                r'(?ms:^ {0,3}(`{3,}|~{3,})[^\n]*\n.*?(?:^ {0,3}\1[ \t]*$|\Z))'
                r'|(?m:^(?: {4}|\t| {0,3}>)[^\n]*(?:\n|$))'
                r'|(?P<ticks>`+)(?:(?!(?P=ticks))[\s\S])*?(?P=ticks)'
                r'|(?s:<!--.*?(?:-->|\Z)|%%.*?(?:%%|\Z))'
                r'|(?s:\A---\n.*?\n---(?:\n|$))')
            def links(part):
                def replace(match):
                    raw = match.group(1)
                    target, sep, alias = raw.partition('|')
                    name, anchor_sep, anchor = target.partition('#')
                    key = norm(name)
                    dest = replacements.get(key if key.endswith('.md') else key + '.md')
                    if '/' not in name and dest is None:
                        stem = norm(name[:-3] if name.endswith('.md') else name)
                        candidates = list(bare.get(stem, ()))
                        if len(candidates) == 1 and names.get(stem) == {candidates[0][0]}:
                            dest = candidates[0][1]
                    if dest is None:
                        return match.group(0)
                    if not name.endswith('.md'):
                        dest = dest[:-3]
                    return '[[' + dest + anchor_sep + anchor + sep + alias + ']]'
                return re.sub(r'(?<!\\)\[\[([^\]\n]+)\]\]', replace, part)
            out=[];end=0
            for match in protected.finditer(body):
                out.extend((links(body[end:match.start()]), match.group(0)));end=match.end()
            out.append(links(body[end:]));return ''.join(out)
        for card in cards:
            rel = card['path']
            if card['id'] in result['conflicts'] or card['id'] in result['missing']:
                continue
            if card['state'] == 'retired' or not rel or not rel.startswith(('知识/', '项目/')):
                continue
            tr = self._tracking(card['id'])
            if not tr or tr['state'] != 'ok' or tr['version'] != card['version']:
                continue
            body = rewrite(card['body'])
            if body == card['body']:
                continue
            from .knowledge_store import _MAX_BODY
            if len(body) > _MAX_BODY:
                result.setdefault('link_conflicts', []).append(card['id']);continue
            try:
                target = self._safe_target(rel)
                text = self._read(target)
                if text is None or _sha(text) != tr['file_hash']:
                    result.setdefault('link_conflicts', []).append(card['id']);continue
                with self.engine.transaction():
                    row = self.cards._get_row(card['id'])
                    # Recheck immediately before versioning; a later disk edit
                    # is preserved by publish's existing expected-hash guard.
                    if row['version'] != card['version'] or self._read(target) != text:
                        result.setdefault('link_conflicts', []).append(card['id']);continue
                    self.cards._add_revision(row, 'link_path_repair')
                    self.engine.db.execute(
                        'UPDATE knowledge_cards SET body=?,version=version+1,updated_at=? WHERE id=?',
                        (body, time.time(), card['id']))
                result['changed'] += 1
                result.setdefault('links_repaired', []).append(card['id'])
            except (ValueError, OSError):
                result.setdefault('link_conflicts', []).append(card['id'])

    def sync(self):
        result = {"changed": 0, "moved": 0, "missing": [], "conflicts": []}
        with self.engine.locked():
            self.recover()
            with self.engine.transaction():
                self.engine.db.execute('CREATE TABLE IF NOT EXISTS knowledge_link_moves '
                                       '(card_id TEXT,old_path TEXT,PRIMARY KEY(card_id,old_path))')
            found = self._scan()
            rows = self.engine.db.execute(
                "SELECT card_id, file_hash, version, path, state"
                " FROM knowledge_files").fetchall()
            for row in rows:
                cid, fhash, ver, rel, state = row
                try:
                    card = self.cards.get(cid)
                except ValueError:
                    continue
                paths = found.get(cid, [])
                if len(paths) > 1:
                    result["conflicts"].append(cid)
                    continue
                if len(paths)==1:
                    candidate=self._read(self._safe_target(paths[0]))
                    if _parse_wrapper(candidate)!=(cid,ver):
                        result['conflicts'].append(cid);continue
                if len(paths) == 1 and paths[0] != rel:
                    other = self.engine.db.execute(
                        "SELECT card_id FROM knowledge_files WHERE path = ?"
                        " AND card_id != ?", (paths[0], cid)).fetchone()
                    if other:
                        result["conflicts"].append(cid)
                        continue
                    try:
                        self._safe_target(paths[0])
                        self.cards.set_path(cid, card["version"], paths[0])
                        self.engine.db.execute(
                            "UPDATE knowledge_files SET path = ?"
                            " WHERE card_id = ?", (paths[0], cid))
                        self.engine.db.execute(
                            'INSERT OR IGNORE INTO knowledge_link_moves VALUES(?,?)', (cid, rel))
                        self.engine.db.commit()
                        rel = paths[0]
                        result["moved"] += 1
                    except ValueError:
                        result["conflicts"].append(cid)
                        continue
                try:
                    target = self._safe_target(rel)
                except ValueError:
                    result["conflicts"].append(cid)
                    continue
                try:text = self._read(target)
                except (ValueError,OSError):
                    result['conflicts'].append(cid);continue
                if text is None:
                    if state != "missing":
                        self.engine.db.execute(
                            "UPDATE knowledge_files SET state='missing'"
                            " WHERE card_id = ?", (cid,))
                        self.engine.db.commit()
                    result["missing"].append(cid)
                    continue
                meta = _parse_wrapper(text)
                if meta is None or meta[0] != cid:
                    result["conflicts"].append(cid)
                    continue
                if _sha(text) == fhash:
                    if state != "ok":
                        self.engine.db.execute(
                            "UPDATE knowledge_files SET state='ok'"
                            " WHERE card_id = ?", (cid,))
                        self.engine.db.commit()
                    continue
                if meta[1] != ver:
                    result["conflicts"].append(cid)
                    continue
                human = _strip_wrapper(text)
                expected_human = _strip_wrapper(self.render(card))
                if human.replace('\r\n','\n') == expected_human.replace('\r\n','\n'):
                    self.engine.db.execute(
                        "UPDATE knowledge_files SET file_hash = ?,"
                        " version = ?, state='ok' WHERE card_id = ?",
                        (_sha(text), ver, cid))
                    self.engine.db.commit()
                    continue
                try:
                    self.cards.manual_edit(cid, card["version"], human)
                except ValueError:
                    result["conflicts"].append(cid)
                    continue
                self.engine.db.execute(
                    "UPDATE knowledge_files SET file_hash = ?,"
                    " version = ?, state='ok' WHERE card_id = ?",
                    (_sha(text), ver, cid))
                self.engine.db.commit()
                result["changed"] += 1
            self._repair_moved_links(result)
        for cid in result['conflicts']:self._flag(cid,'conflict')
        for cid in result['missing']:self._flag(cid,'missing')
        return result


from .knowledge_store import KnowledgeStore  # noqa: E402
