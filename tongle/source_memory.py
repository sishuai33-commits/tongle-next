"""Rebuildable exact-source search and persistent correction evidence.
DS initial contribution, integrated and repaired locally. No model calls.
Natural corrections use a conservative whole-message grammar and a stored
preceding assistant citation. No caller can supply an authorship override.
"""
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone

CHUNK_CHARS = 2000
SNIPPET_CHARS = 300


class _VerificationUnknown(Exception):
    """A read could not verify an identity; this is not invalid evidence."""


def _verify(engine, kind, identity, version):
    from .retrieval_budget import verify_identity
    if not verify_identity(engine, kind, identity, version):
        raise _VerificationUnknown('verification_budget_exceeded')


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def _text(value, name, maximum=2000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(name + ' must be a bounded non-empty string')
    return value


def _version(value):
    if type(value) is not int or value < 1:
        raise ValueError('version must be a positive integer')
    return value


def _tokens(text):
    out = set(re.findall(r'[a-z0-9_]+', text.lower()))
    for run in re.findall(r'[\u3400-\u9fff]+', text):
        out.update([run] if len(run) == 1 else (run[i:i+2] for i in range(len(run)-1)))
    return sorted(out)


def _chunks(text):
    start = 0
    while start < len(text):
        end = min(start + CHUNK_CHARS, len(text))
        if end < len(text):
            split = text.rfind('\n\n', start + CHUNK_CHARS // 2, end)
            if split >= 0:
                end = split + 2
        yield start, text[start:end]
        if end == len(text):
            break
        start = end - 8


DERIVED_TRIGGERS = {
    'sm_'+event.lower(): f'CREATE TRIGGER sm_{event.lower()} AFTER {event} ON versions BEGIN INSERT OR IGNORE INTO source_memory_dirty VALUES({"OLD" if event=="DELETE" else "NEW"}.source_id,{"OLD" if event=="DELETE" else "NEW"}.version); END'
    for event in ('INSERT','UPDATE','DELETE')
}


class SourceMemory:
    def __init__(self, engine):
        self.engine, self.db = engine, engine.db
        with engine.locked(), engine.transaction():
            missing_chunks = not self.db.execute("SELECT 1 FROM sqlite_master WHERE name='source_memory_chunks'").fetchone()
            self.db.execute('CREATE TABLE IF NOT EXISTS source_memory_chunks(rowid INTEGER PRIMARY KEY, source_id TEXT NOT NULL, version INTEGER NOT NULL, project TEXT NOT NULL, chunk_offset INTEGER NOT NULL, chunk_text TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(source_id,version,chunk_offset))')
            old = self.db.execute("SELECT sql FROM sqlite_master WHERE name='source_memory_fts'").fetchone()
            # Repair the unreleased DS candidate's derived contentless index only.
            reset_index = missing_chunks or not old or "content=''" in old[0]
            if reset_index:
                self.db.execute('DROP TABLE IF EXISTS source_memory_fts')
                self.db.execute('DELETE FROM source_memory_chunks')
                self.db.execute('DROP TABLE IF EXISTS source_memory_indexed')
            self.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS source_memory_fts USING fts5(chunk_terms,tokenize='unicode61')")
            self.db.execute('CREATE INDEX IF NOT EXISTS smc_project ON source_memory_chunks(project,source_id,version)')
            self.db.execute('CREATE TABLE IF NOT EXISTS source_memory_indexed(source_id TEXT, version INTEGER, text_hash TEXT, PRIMARY KEY(source_id,version))')
            self.db.execute('CREATE TABLE IF NOT EXISTS source_memory_corrections(event_key TEXT PRIMARY KEY, project TEXT NOT NULL, payload_hash TEXT NOT NULL, source_id TEXT NOT NULL, source_version INTEGER NOT NULL, quote TEXT NOT NULL, source_role TEXT NOT NULL, target_card_id TEXT, target_card_version INTEGER, replacement TEXT NOT NULL, candidates TEXT NOT NULL, status TEXT NOT NULL, reason TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)')
            fresh_dirty = not self.db.execute("SELECT 1 FROM sqlite_master WHERE name='source_memory_dirty'").fetchone()
            self.db.execute('CREATE TABLE IF NOT EXISTS source_memory_dirty(source_id TEXT,version INTEGER,PRIMARY KEY(source_id,version))')
            for sql in DERIVED_TRIGGERS.values():
                self.db.execute(sql.replace('CREATE TRIGGER ', 'CREATE TRIGGER IF NOT EXISTS ',1))
            if fresh_dirty or reset_index:
                self.db.execute('INSERT OR IGNORE INTO source_memory_dirty SELECT v.source_id,v.version FROM versions v LEFT JOIN source_memory_indexed i ON i.source_id=v.source_id AND i.version=v.version WHERE i.source_id IS NULL OR i.text_hash!=v.hash')
            columns = {row['name'] for row in self.db.execute('PRAGMA table_info(source_memory_corrections)')}
            if 'correction_type' not in columns:
                self.db.execute("ALTER TABLE source_memory_corrections ADD COLUMN correction_type TEXT NOT NULL DEFAULT 'structured'")
            if 'matching_evidence' not in columns:
                self.db.execute("ALTER TABLE source_memory_corrections ADD COLUMN matching_evidence TEXT NOT NULL DEFAULT '{}'")


    def index_sources(self, project, rebuild=False, limit=None):
        _text(project, 'project', 200)
        if limit is not None and (type(limit) is not int or not 1 <= limit <= 10000):
            raise ValueError('index limit must be between 1 and 10000')
        indexed = unchanged = 0
        errors = []
        with self.engine.locked(), self.engine.transaction():
            total = self.db.execute('SELECT count(*) FROM sources s JOIN versions v ON v.source_id=s.id WHERE s.project=?', (project,)).fetchone()[0]
            if rebuild:
                self.db.execute('INSERT OR IGNORE INTO source_memory_dirty SELECT v.source_id,v.version FROM versions v JOIN sources s ON s.id=v.source_id WHERE s.project=?',(project,))
            pending = self.db.execute('SELECT count(*) FROM source_memory_dirty d JOIN sources s ON s.id=d.source_id JOIN versions v ON v.source_id=d.source_id AND v.version=d.version WHERE s.project=?',(project,)).fetchone()[0]
            rows = self.db.execute('SELECT s.id,v.version,v.hash,v.created_at FROM source_memory_dirty d JOIN versions v ON v.source_id=d.source_id AND v.version=d.version JOIN sources s ON s.id=d.source_id WHERE s.project=? ORDER BY v.created_at DESC,s.id,v.version DESC' + (' LIMIT ?' if limit else ''), (project,limit) if limit else (project,)).fetchall()
            unchanged = total - pending
            for row in rows:
                digest = row['hash']
                prior = self.db.execute('SELECT text_hash FROM source_memory_indexed WHERE source_id=? AND version=?', (row['id'],row['version'])).fetchone()
                if not rebuild and prior and prior[0] == digest:
                    unchanged += 1
                    self.db.execute('DELETE FROM source_memory_dirty WHERE source_id=? AND version=?',(row['id'],row['version']))
                    continue
                self.db.execute('SAVEPOINT source_index')
                try:
                    old = self.db.execute('SELECT rowid FROM source_memory_chunks WHERE source_id=? AND version=?', (row['id'],row['version'])).fetchall()
                    for chunk in old:
                        self.db.execute('DELETE FROM source_memory_fts WHERE rowid=?', (chunk[0],))
                    self.db.execute('DELETE FROM source_memory_chunks WHERE source_id=? AND version=?', (row['id'],row['version']))
                    original = self.db.execute('SELECT text FROM versions WHERE source_id=? AND version=?',(row['id'],row['version'])).fetchone()[0]
                    for offset, text in _chunks(original):
                        cur = self.db.execute('INSERT INTO source_memory_chunks(source_id,version,project,chunk_offset,chunk_text,created_at) VALUES(?,?,?,?,?,?)', (row['id'],row['version'],project,offset,text,row['created_at']))
                        self.db.execute('INSERT INTO source_memory_fts(rowid,chunk_terms) VALUES(?,?)', (cur.lastrowid,' '.join(_tokens(text))))
                    self.db.execute('INSERT OR REPLACE INTO source_memory_indexed VALUES(?,?,?)', (row['id'],row['version'],digest))
                    self.db.execute('DELETE FROM source_memory_dirty WHERE source_id=? AND version=?',(row['id'],row['version']))
                    self.db.execute('RELEASE source_index')
                    indexed += 1
                except (sqlite3.Error, ValueError, TypeError) as exc:
                    self.db.execute('ROLLBACK TO source_index')
                    self.db.execute('RELEASE source_index')
                    errors.append({'source_id':row['id'],'version':row['version'],'error':type(exc).__name__})
            if indexed:
                self.db.execute("INSERT INTO meta(key,value) VALUES('source_index_revision','1') ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1")
            pending = self.db.execute('SELECT count(*) FROM source_memory_dirty d JOIN sources s ON s.id=d.source_id JOIN versions v ON v.source_id=d.source_id AND v.version=d.version WHERE s.project=?',(project,)).fetchone()[0]
        return {'project':project,'indexed_sources':indexed,'unchanged_versions':unchanged,'total_versions':total,'pending_versions':pending,'complete':pending==0 and not errors,'errors':errors}

    def search(self, project, text, limit=5, history=False, roles=None, query_tokens=None, exclude_source_id=None, candidate_limit=None):
        _text(project, 'project', 200)
        if not isinstance(text,str) or len(text)>4000:
            raise ValueError('query must be a string up to 4000 characters')
        if type(limit) is not int or limit<1 or type(history) is not bool:
            raise ValueError('invalid retrieval options')
        if roles is not None:
            if (not isinstance(roles,(tuple,list)) or not roles
                    or not all(isinstance(role,str) and role in ('user','assistant','document') for role in roles)):
                raise ValueError('roles must be a nonempty tuple/list of user, assistant, document')
            roles = tuple(dict.fromkeys(roles))
        if exclude_source_id is not None:
            _text(exclude_source_id,'excluded source id',200)
        tokens = _tokens(text)[:128] if query_tokens is None else sorted(query_tokens)[:128]
        if any(not isinstance(t,str) or not t or '"' in t for t in tokens):
            raise ValueError('invalid query tokens')
        if not tokens:
            return []
        match = ' OR '.join('"'+t+'"' for t in tokens)
        currency = '' if history else ' AND c.version=s.current_version'
        role_filter = '' if roles is None else ' AND s.role IN ('+','.join('?' for _ in roles)+')'
        if candidate_limit is not None and (type(candidate_limit) is not int or not 1 <= candidate_limit <= 64):
            raise ValueError('candidate limit must be between 1 and 64')
        params = (match,project,project)+(() if roles is None else roles)+(candidate_limit or min(limit,5),)
        if exclude_source_id: params=params[:-1]+(exclude_source_id,params[-1])
        rows = self.db.execute('SELECT c.*,s.role,s.current_version FROM source_memory_fts f JOIN source_memory_chunks c ON c.rowid=f.rowid JOIN sources s ON s.id=c.source_id JOIN versions v ON v.source_id=c.source_id AND v.version=c.version JOIN source_memory_indexed i ON i.source_id=v.source_id AND i.version=v.version AND i.text_hash=v.hash LEFT JOIN source_status st ON st.source_id=s.id WHERE source_memory_fts MATCH ? AND c.project=? AND s.project=? AND (st.state IS NULL OR st.state=\'available\')'+currency+role_filter+(' AND s.id != ?' if exclude_source_id else '')+' ORDER BY bm25(source_memory_fts),c.source_id,c.version DESC,c.chunk_offset LIMIT ?', params).fetchall()
        results = []
        for row in rows:
            chunk = row['chunk_text']
            positions = [chunk.lower().find(t) for t in tokens if t in chunk.lower()]
            start = max(0,min(positions or [0])-100)
            end = min(len(chunk), start+SNIPPET_CHARS)
            results.append({'source_id':row['source_id'],'version':row['version'],'current_version':row['current_version'],'role':row['role'],'created_at':row['created_at'],'offset':row['chunk_offset']+start,'length':end-start,'snippet':chunk[start:end],'historical':row['version']!=row['current_version']})
        return results

    def _card(self, project, cid, version=None):
        _text(cid,'card id',200)
        row = self.db.execute('SELECT project,version,state FROM knowledge_cards WHERE id=?',(cid,)).fetchone()
        if row:
            _verify(self.engine, 'card', cid, row['version'])
        if not row or row['project']!=project:
            raise ValueError('target/candidate is unavailable in project')
        if version is not None and row['version']!=_version(version):
            raise ValueError('target version changed')
        return row

    def _source(self, source_id, version=None):
        """Charge both current and cited versions before inspecting their text.

        No active public-read scope means the existing writer behavior is
        unchanged. Version identities already checked in this operation cost
        no additional slots, including a current-version citation.
        """
        if getattr(self.engine, '_retrieval_verification_budget', None) is not None:
            row = self.db.execute('SELECT current_version FROM sources WHERE id=?', (source_id,)).fetchone()
            if row:
                _verify(self.engine, 'source', source_id, row['current_version'])
            if version is not None:
                _verify(self.engine, 'source', source_id, version)
        source = self.engine.store.source(source_id, version)
        # A writer may advance the current version between the metadata read
        # and the joined source read. Charge the identities actually returned.
        _verify(self.engine, 'source', source_id, source['current_version'])
        _verify(self.engine, 'source', source_id, source['version'])
        return source

    @staticmethod
    def _natural_replacement(text):
        if not isinstance(text, str) or len(text) > 1200:
            return None
        # A quoted/forwarded passage or several paragraphs cannot grant authority.
        if any(c in text for c in ('\n', '\r', '"', "'", '“', '”', '‘', '’', '「', '」', '『', '』', '>', '`')):
            return None
        if re.search(r'转[贴述发]|反馈|(?:DS|GPT|assistant|deepseek)(?:说|的|：|:)|他说|她说|原话|引用', text, re.I):
            return None
        match = re.fullmatch(r'(?:这条不对[，,:：]|不对[，,:：]|我纠正一下[：:，,])\s*(\S(?:.*\S)?)', text.strip())
        return match.group(1) if match else None

    @staticmethod
    def _actual_citations(text):
        """Ignore literal code/quotes; only unquoted answer citations bind corrections."""
        text=re.sub(r'<(pre|code|blockquote)\b[^>]*>.*?</\1\s*>','',text,flags=re.I|re.S)
        text=re.sub(r'<(?:pre|code|blockquote)\b[^>]*>.*','',text,flags=re.I|re.S)
        visible=[];fence=None
        for line in text.splitlines():
            opening=re.match(r'^\s*(`{3,}|~{3,})',line)
            if fence:
                if re.match(r'^\s*'+re.escape(fence[0])+'{'+str(fence[1])+r',}\s*$',line):
                    fence=None
                continue
            if opening:
                fence=(opening.group(1)[0],len(opening.group(1)));continue
            if re.match(r'^\s*>|^(?: {4}|\t)',line):continue
            line=re.sub(r'(`+).*?\1','',line)
            if '`' in line:continue  # Unclosed inline code is not a trustworthy citation.
            for pattern in (r'“[^”]*”',r'‘[^’]*’',r'「[^」]*」',r'『[^』]*』',r'"[^"\n]*"',r"'[^'\n]*'"):
                line=re.sub(pattern,'',line)
            if any(mark in line for mark in ('"', "'", '“', '”', '‘', '’', '「', '」', '『', '』')):continue
            if re.search(r'(?:示例|例如|例子|原话|字面|引用格式|标签格式)',line):continue
            visible.append(line)
        return list(dict.fromkeys(re.findall(r'【记忆:([A-Za-z0-9_-]{1,200})@([1-9][0-9]*)】','\n'.join(visible))))

    def _natural_binding(self, src):
        """Resolve only the nearest preceding assistant in this exact session."""
        row = self.db.execute('SELECT rowid FROM sources WHERE id=?',(src['source_id'],)).fetchone()
        if not row or not src.get('session'):
            return {}, []
        previous = self.db.execute("SELECT id FROM sources WHERE project=? AND session=? AND role='assistant' AND rowid<? ORDER BY rowid DESC LIMIT 1",(src['project'],src['session'],row[0])).fetchone()
        if not previous:
            return {}, []
        assistant = self._source(previous['id'])
        refs = self._actual_citations(assistant['text'])
        # More than one actual citation is ambiguous, even if some are stale.
        candidates = []
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='knowledge_cards'").fetchone():
            for cid, version in refs:
                try:
                    if self._card(src['project'],cid)['state']=='active':
                        candidates.append(cid)
                except ValueError:
                    pass
        candidates = list(dict.fromkeys(candidates))[:5]
        if len(refs)!=1 or not self.engine.source_available(assistant['source_id']):
            return {}, candidates
        cid, version = refs[0]
        try:
            if not self.db.execute("SELECT 1 FROM sqlite_master WHERE name='knowledge_cards'").fetchone():
                return {}, []
            card = self._card(src['project'],cid,int(version))
            if card['state']!='active':
                return {}, candidates
        except ValueError:
            return {}, candidates
        return {'source_id':assistant['source_id'],'version':assistant['version'],
                'quote':f'【记忆:{cid}@{version}】','target_card_id':cid,
                'target_card_version':int(version)}, []

    def observe_user(self, source_id):
        """Observe a captured event; never synthesize a user event or authorization."""
        with self.engine.locked(), self.engine.transaction():
            src = self._source(source_id)
            if src['role']!='user':
                return {'status':'ignored','reason':'non_user_authorship'}
            event_key = 'natural:' + source_id + ':' + str(src['version'])
            prior = self.db.execute('SELECT status FROM source_memory_corrections WHERE event_key=?',(event_key,)).fetchone()
            if prior:
                return {'event_key':event_key,'status':prior['status'],'replay':True}
            text = src['text']
            replacement = self._natural_replacement(text)
            if replacement is None:
                # Preserve a short explicit but ambiguous correction for later review.
                candidate = re.match(r'^(?:这条不对[，,:：]|不对[，,:：]|我纠正一下[：:，,])\s*(.+)',text.strip(),re.S)
                if not candidate or len(text)>2000 or len(candidate.group(1))>1200:
                    return {'status':'ignored','reason':'not_unambiguous_correction'}
                return self.add_correction(src['project'],event_key,source_id,src['version'],text,candidate.group(1))
            binding, candidates = self._natural_binding(src)
            return self.add_correction(src['project'],event_key,source_id,src['version'],text,replacement,
                target_card_id=binding.get('target_card_id'),target_card_version=binding.get('target_card_version'),candidates=candidates)

    def _correction_evidence_valid(self, item, src):
        if item['correction_type']!='natural':
            return src['text']==f'/correct {item["target_card_id"]}\n{item["replacement"]}'
        if self._natural_replacement(src['text'])!=item['replacement']:
            return False
        match = item['matching_evidence']
        try:
            assistant = self._source(match['source_id'],match['version'])
            source_order = self.db.execute('SELECT rowid FROM sources WHERE id=?',(src['source_id'],)).fetchone()[0]
            assistant_order = self.db.execute('SELECT rowid FROM sources WHERE id=?',(assistant['source_id'],)).fetchone()[0]
            return (assistant['role']=='assistant' and assistant['project']==src['project']
                    and assistant['session']==src['session'] and assistant_order<source_order
                    and self.engine.source_available(assistant['source_id'])
                    and assistant['current_version']==match['version']
                    and match['target_card_id']==item['target_card_id']
                    and match['target_card_version']==item['target_card_version']
                    and match['quote']==f'【记忆:{item["target_card_id"]}@{item["target_card_version"]}】'
                    and self._actual_citations(assistant['text'])==[(item['target_card_id'],str(item['target_card_version']))])
        except (KeyError,TypeError,ValueError):
            return False

    def add_correction(self, project, event_key, source_id, source_version, quote, replacement, target_card_id=None, target_card_version=None, candidates=None):
        for name,value,bound in [('project',project,200),('event_key',event_key,200),('source_id',source_id,200),('quote',quote,2000),('replacement',replacement,1200)]:
            _text(value,name,bound)
        _version(source_version)
        if target_card_version is not None:
            _version(target_card_version)
        if target_card_id is None and target_card_version is not None:
            raise ValueError('version requires target')
        candidates = [] if candidates is None else candidates
        if not isinstance(candidates,list) or len(candidates)>5 or not all(isinstance(x,str) for x in candidates) or len(set(candidates))!=len(candidates):
            raise ValueError('candidates must be at most five distinct ids')
        payload = [project,event_key,source_id,source_version,quote,replacement,target_card_id,target_card_version,candidates]
        digest = hashlib.sha256(json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
        with self.engine.locked(),self.engine.transaction():
            prior = self.db.execute('SELECT payload_hash,status FROM source_memory_corrections WHERE event_key=?',(event_key,)).fetchone()
            if prior:
                if prior['payload_hash']!=digest:
                    raise ValueError('correction event payload conflict')
                return {'event_key':event_key,'status':prior['status'],'replay':True}
            src = self._source(source_id,source_version)
            if src['project']!=project or quote not in src['text']:
                raise ValueError('source scope or exact quote mismatch')
            card = self._card(project,target_card_id,target_card_version) if target_card_id is not None else None
            for candidate in candidates:
                self._card(project,candidate)
            natural = self._natural_replacement(src['text'])
            binding, _ = self._natural_binding(src) if natural is not None else ({}, [])
            natural_direct = (natural == replacement and binding.get('target_card_id') == target_card_id
                              and binding.get('target_card_version') == target_card_version)
            direct = (src['role']=='user' and src['current_version']==source_version and self.engine.source_available(source_id)
                      and card is not None and card['state']=='active' and target_card_version is not None and not candidates
                      and quote==src['text'] and (src['text']==f'/correct {target_card_id}\n{replacement}' or natural_direct))
            status,reason = ('active',None) if direct else ('pending','authorship_or_target_unresolved')
            if not self.engine.source_available(source_id):reason='source_withdrawn'
            elif src['role']!='user':reason='non_user_authorship'
            elif len(candidates)>1:reason='ambiguous_target'
            now = _now()
            correction_type = 'natural' if natural is not None else 'structured'
            matching = binding if natural_direct else {}
            self.db.execute('INSERT INTO source_memory_corrections(event_key,project,payload_hash,source_id,source_version,quote,source_role,target_card_id,target_card_version,replacement,candidates,status,reason,created_at,updated_at,correction_type,matching_evidence) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(event_key,project,digest,source_id,source_version,quote,src['role'],target_card_id,target_card_version,replacement,json.dumps(candidates),status,reason,now,now,correction_type,json.dumps(matching,ensure_ascii=False,sort_keys=True)))
        return {'event_key':event_key,'status':status,'reason':reason,'source_role':src['role'],'replay':False}

    def corrections(self, project, include_history=True, *, target_ids=None, source_ids=None):
        """Verify correction heads only for the requested target identities.

        Both filters omitted retains the full project/history contract. When
        present, target/source filters are ORed within the required project.
        Explicitly empty sets never request a project-wide fallback.
        """
        _text(project,'project',200)
        where = 'project=?'
        params = [project]
        if target_ids is not None or source_ids is not None:
            clauses = []
            for label, column, identities in (('target','target_card_id',target_ids),('source','source_id',source_ids)):
                if identities is None:
                    continue
                if not isinstance(identities, (list, tuple, set, frozenset)) or len(identities)>256:
                    raise ValueError('correction '+label+'s must contain at most 256 ids')
                for identity in identities:
                    _text(identity, 'correction '+label+' id', 200)
                selected = sorted(set(identities))
                if selected:
                    clauses.append(column+' IN ('+','.join('?' for _ in selected)+')')
                    params.extend(selected)
            if not clauses:
                return []
            where += ' AND ('+' OR '.join(clauses)+')'
        rows = self.db.execute('SELECT * FROM source_memory_corrections WHERE ' + where + ' ORDER BY created_at DESC,event_key DESC',params)
        output,heads = [],set()
        for row in rows:
            item = dict(row);item.pop('payload_hash');item['candidates']=json.loads(item['candidates']);item['matching_evidence']=json.loads(item['matching_evidence']);item['effective']=False
            target = item['target_card_id']
            if item['status']=='active':
                # A source-only lookup can omit a newer correction for the
                # same target. Filtering must never revive its older head.
                newer = source_ids is not None and self.db.execute(
                    "SELECT 1 FROM source_memory_corrections WHERE project=? AND target_card_id IS ? AND status='active' "
                    "AND (created_at>? OR (created_at=? AND event_key>?)) LIMIT 1",
                    (project,target,item['created_at'],item['created_at'],item['event_key'])).fetchone()
                if target in heads or newer:
                    item['reason']='later_correction';item['status']='superseded'
                else:
                    heads.add(target)
                    try:
                        src=self._source(item['source_id'],item['source_version'])
                        card=self._card(project,target)  # Later unrelated edits never undo a correction.
                        if (src['role']=='user' and src['project']==project and self.engine.source_available(item['source_id']) and src['current_version']==item['source_version']
                            and item['quote']==src['text'] and self._correction_evidence_valid(item,src) and card['state']=='active'):
                            item['effective']=True
                        else:
                            item['status']='pending';item['reason']='evidence_or_target_changed'
                    except _VerificationUnknown:
                        # Retain identities so consumers can suppress the old
                        # target and its sources. Neither the replacement nor
                        # the original body is verified by this partial read.
                        item.update(verification_unknown=True, status='unverified',
                            reason='verification_budget_exceeded', effective=False,
                            quote='', replacement='')
                        item['matching_evidence'] = {
                            key: value for key, value in item['matching_evidence'].items() if key != 'quote'}
                    except (KeyError,ValueError):
                        item['status']='pending';item['reason']='evidence_or_target_changed'
            if include_history or item['status'] not in ('superseded','withdrawn'):
                output.append(item)
            if item.get('verification_unknown'):
                break  # The whole read is incomplete; never revive older heads.
        return output
