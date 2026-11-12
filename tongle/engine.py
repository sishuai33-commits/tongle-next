"""Editable Markdown reconciliation and evidence-gated records."""
import hashlib
import contextlib
import functools
import json
import os
from pathlib import Path
import re
import tempfile
import time
from .portability import fsync_directory, lock_file
from .store import Store
from .presentation import label,folder,title,hidden_metadata

BEGIN='%% tongle:body %%\n'
END='\n%% /tongle:body %%'
OLD_BEGIN='<!-- tongle:body -->\n'
OLD_END='\n<!-- /tongle:body -->'


def digest(data):
    return hashlib.sha256(data if isinstance(data,bytes) else data.encode()).hexdigest()


def atomic_create(path, data):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd, tmp=tempfile.mkstemp(prefix='.tongle-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as f:
            f.write(data);f.flush();os.fsync(f.fileno())
        os.link(tmp,path)  # exclusive target creation, no incomplete note visible
        fsync_directory(path.parent)
    finally:
        os.unlink(tmp)


def synchronized(method):
    @functools.wraps(method)
    def wrapped(self,*args,**kwargs):
        with self.locked():return method(self,*args,**kwargs)
    return wrapped


class SourceUnavailable(ValueError):
    pass


class Engine:
    @contextlib.contextmanager
    def locked(self):
        if getattr(self,'_lock_depth',0):
            yield;return
        with (self.home/'engine.lock').open('a') as f:
            lock_file(f)
            self._lock_depth=1
            try:yield
            finally:self._lock_depth=0
    def __init__(self,home):
        self.home=Path(home).resolve()
        self.store=Store(self.home)
        self.db=self.store.db
        from .memory_runtime import register
        register(self)
        with self.locked():
            cols={r[1] for r in self.db.execute('PRAGMA table_info(records)')}
            if 'note_title' not in cols:
                self.db.execute('ALTER TABLE records ADD COLUMN note_title TEXT');self.db.commit()
            if 'published' not in cols:
                self.db.execute('ALTER TABLE records ADD COLUMN published INTEGER NOT NULL DEFAULT 0');self.db.commit()
            if 'prior_status' not in cols:
                self.db.execute('ALTER TABLE records ADD COLUMN prior_status TEXT');self.db.commit()
            self.db.execute("CREATE TABLE IF NOT EXISTS record_sources(record_id TEXT,source_id TEXT,version INTEGER,quote TEXT,PRIMARY KEY(record_id,source_id,version))")
            self.db.execute("CREATE TABLE IF NOT EXISTS source_status(source_id TEXT PRIMARY KEY,state TEXT NOT NULL,updated_at REAL NOT NULL)")
            self.db.execute('CREATE TABLE IF NOT EXISTS observations(id INTEGER PRIMARY KEY, project TEXT, query TEXT, offered_ids TEXT, created_at REAL)')
            self.db.commit()


    @contextlib.contextmanager
    def transaction(self):
        if self.db.in_transaction:
            yield self.db
        else:
            with self.store.transaction() as db:yield db

    def vault(self):
        row=self.db.execute("SELECT value FROM meta WHERE key='vault'").fetchone()
        if not row:raise ValueError('initialize with init --vault first')
        name=self.db.execute("SELECT value FROM meta WHERE key='managed_dir'").fetchone()
        path=Path(row[0])/folder(name[0] if name else 'tongle')
        if path.is_symlink():raise ValueError('managed vault root became a symlink')
        return path

    @synchronized
    def init(self,vault,evolving=False):
        vault=Path(vault).expanduser().resolve()
        prior=self.db.execute("SELECT value FROM meta WHERE key='vault'").fetchone()
        if prior and Path(prior[0])!=vault:raise ValueError('home already bound to another vault')
        managed=self.db.execute("SELECT value FROM meta WHERE key='managed_dir'").fetchone()
        target=vault/('知识' if evolving else folder(managed[0] if managed else 'tongle'))
        if target.is_symlink():raise ValueError('managed vault is symlink')
        target.mkdir(parents=True,exist_ok=True)
        with self.transaction():self.db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('vault',?)",(str(vault),))
        return {'home':str(self.home),'vault':str(vault),'initialized':True}

    def source_available(self,sid):
        row=self.db.execute('SELECT state FROM source_status WHERE source_id=?',(sid,)).fetchone()
        return not row or row['state']=='available'

    def job_source_ids(self, job):
        job = dict(job)
        refs = json.loads(job.get('arguments') or '{}').get('sources', [])
        return {job['source_id']} | {ref['source_id'] for ref in refs}

    def job_has_revoked_source(self, job):
        return any(self.db.execute("SELECT 1 FROM source_status WHERE source_id=? AND state='revoked'", (sid,)).fetchone()
                   for sid in self.job_source_ids(job))

    @synchronized
    def source_state(self,project,sid,state):
        source=self.store.source(sid)
        if source['project']!=project:raise ValueError('source project mismatch')
        if state not in ('available','missing','revoked'):raise ValueError('invalid source state')
        prior=self.db.execute('SELECT state FROM source_status WHERE source_id=?',(sid,)).fetchone()
        if prior and prior['state']=='revoked' and state!='revoked':raise ValueError('explicitly revoked source cannot automatically return')
        with self.transaction():
            if not prior or prior['state']!=state:
                self.db.execute('INSERT OR REPLACE INTO source_status(source_id,state,updated_at) VALUES(?,?,?)',(sid,state,time.time()))
            for job in self.db.execute("SELECT * FROM jobs WHERE state IN ('blocked','pending','retry')").fetchall():
                from .memory_runtime import source_allowed
                if not source_allowed(self,job['source_id'],job['version']):continue
                if sid not in self.job_source_ids(job):continue
                if state=='revoked':
                    self.db.execute("UPDATE jobs SET state='obsolete',lease_until=0 WHERE id=?",(job['id'],))
                elif state=='available' and job['state']=='blocked':
                    if all(self.source_available(ref) for ref in self.job_source_ids(job)):
                        self.db.execute("UPDATE jobs SET state='pending',attempts=0,lease_until=0 WHERE id=?",(job['id'],))
        return {'source_id':sid,'state':state}

    def _render(self,rec):
        created=time.strftime('%Y-%m-%d',time.localtime(float(rec['created_at'])))
        header='---\ntype: '+('memory' if rec['kind']=='memory' else 'concept')+'\ncreated: '+created+'\n---\n\n'
        refs=self.db.execute('SELECT source_id,version,quote FROM record_sources WHERE record_id=?',(rec['id'],)).fetchall()
        quotes=list(dict.fromkeys([rec['quote']]+[r['quote'] for r in refs]))
        evidence='\n\n'.join('> '+q.replace('\n','\n> ') for q in quotes)
        return header+hidden_metadata(rec)+'# '+title(rec)+'\n\n'+BEGIN+rec['text']+END+'\n\n> [!quote]- 原始依据（可展开）\n'+''.join('> '+line+'\n' for line in evidence.splitlines())

    def _parse(self,path):
        if path.is_symlink() or path.stat().st_size>2_000_000:raise ValueError('invalid note')
        raw=path.read_bytes();text=raw.decode('utf8')
        begin,end=(BEGIN,END) if BEGIN in text else (OLD_BEGIN,OLD_END)
        if not text.startswith('---\n') or text.count(begin)!=1 or text.count(end)!=1:raise ValueError('ambiguous note format')
        front=text.split('---\n',2)[1]
        metadata={}
        for line in front.splitlines():
            if ':' in line:
                k,v=line.split(':',1)
                if k in metadata:raise ValueError('duplicate metadata key')
                try:metadata[k]=json.loads(v)
                except ValueError:metadata[k]=v.strip().strip('"\'')
        hidden=re.findall(r'^%%\ntongle-meta: (.+)\n%%$',text,re.M)
        if 'tongle-meta:' in text and len(hidden)!=1:raise ValueError('invalid hidden identity wrapper')
        if hidden:
            if len(hidden)!=1:raise ValueError('duplicate hidden identity')
            if 'tongle_id' in metadata:raise ValueError('duplicate identity representation')
            identity=json.loads(hidden[0])
            if not isinstance(identity,dict) or set(identity)!={'id','project','source_id','source_version'}:
                raise ValueError('invalid hidden identity fields')
            if not all(isinstance(identity[k],str) and identity[k] for k in ('id','project','source_id')) or type(identity['source_version']) is not int:
                raise ValueError('invalid hidden identity values')
            identity['tongle_id']=identity.pop('id')
            metadata.update(identity)
        body=text.split(begin,1)[1].split(end,1)[0]
        if not body.strip():raise ValueError('empty body')
        return metadata,body,raw

    def _publish(self,rec):
        if not isinstance(rec['id'],str) or not re.fullmatch(r'[0-9a-f]{32}',rec['id']):raise ValueError('invalid managed record identity')
        names=self.db.execute("SELECT value FROM meta WHERE key='project_labels'").fetchone()
        labels=json.loads(names[0]) if names else {}
        if not isinstance(labels,dict):raise ValueError('invalid project labels')
        if rec['project'].startswith('local-') and rec['project'] not in labels:
            number=1
            while '未命名项目 '+str(number) in labels.values():number+=1
            labels[rec['project']]='未命名项目 '+str(number)
            with self.transaction():self.db.execute("INSERT OR REPLACE INTO meta VALUES('project_labels',?)",(json.dumps(labels,ensure_ascii=False),))
        root=self.vault();directory=root/label(labels.get(rec['project'],rec['project']),'其他项目',60)
        if directory.is_symlink():raise ValueError('project note directory is a symlink')
        text=self._render(rec);data=text.encode()
        if rec.get('note_path'):
            relative=Path(rec['note_path'])
            if relative.is_absolute() or '..' in relative.parts or '\\' in str(relative):raise ValueError('unsafe note binding')
            path=root/relative
            if any(p.is_symlink() for p in [path,*list(path.parents)[:len(relative.parts)-1]]):raise ValueError('symlink note binding')
        else:
            name=title(rec);path=directory/(name+'.md');n=1
            while path.exists() or path.is_symlink():
                if not path.is_symlink() and path.read_bytes()==data:break
                n+=1;path=directory/(name+'（'+str(n)+'）.md')
            with self.transaction():self.db.execute('UPDATE records SET note_path=? WHERE id=?',(str(path.relative_to(root)),rec['id']))
        if path.exists() or path.is_symlink():
            if path.is_symlink() or path.read_bytes()!=data:
                with self.transaction():self.db.execute("UPDATE records SET status='conflicted' WHERE id=?",(rec['id'],))
                raise ValueError('note collision; existing content preserved')
        else:atomic_create(path,data)
        with self.transaction():self.db.execute('UPDATE records SET note_path=?,note_hash=?,published=1 WHERE id=?',
                                     (str(path.relative_to(root)),digest(data),rec['id']))

    @synchronized
    def record(self,project,key,status,source_id,source_version,payload,model=False):
        source=self.store.source(source_id,source_version)
        if not self.source_available(source_id):raise SourceUnavailable('source unavailable')
        if source['project']!=project:raise ValueError('source project mismatch')
        if int(source['current_version'])!=int(source_version):raise ValueError('stale source version')
        if payload.get('title') is not None and not isinstance(payload['title'],str):raise ValueError('invalid note title')
        text=payload.get('text');quote=payload.get('quote');kind=payload.get('kind','memory')
        if not isinstance(text,str) or not text.strip() or len(text)>20000:raise ValueError('invalid record text')
        if not isinstance(quote,str) or not quote.strip() or quote not in source['text']:raise ValueError('quote absent from exact source')
        if not isinstance(key,str) or not key or len(key)>300:raise ValueError('invalid record key')
        if status not in ['confirmed','candidate']:raise ValueError('invalid status')
        if status=='confirmed' and (model or source['role']!='user' or text!=quote):
            raise ValueError('confirmation requires exact user statement and explicit record operation')
        if any(mark.strip() in text for mark in [BEGIN,END,OLD_BEGIN,OLD_END]) or 'tongle-meta:' in text:raise ValueError('reserved note delimiter in body')
        refs=payload.get('evidence',[])
        if not isinstance(refs,list):raise ValueError('evidence must be array')
        if kind not in ['memory','knowledge','synthesis']:raise ValueError('invalid knowledge kind')
        if kind=='synthesis' and len({r.get('source_id') for r in refs if isinstance(r,dict)})<2:raise ValueError('synthesis requires two distinct cited sources')
        for ref in refs:
            evidence_source=self.store.source(ref['source_id'],ref['version'])
            if evidence_source['project']!=project or evidence_source['current_version']!=ref['version']:raise ValueError('stale or foreign supporting source')
            if not self.source_available(ref['source_id']):raise SourceUnavailable('source unavailable')
            if not isinstance(ref.get('quote'),str) or not ref['quote'].strip() or ref['quote'] not in evidence_source['text']:raise ValueError('supporting quote absent')
        rid=digest(json.dumps([project,key,source_id,int(source_version)]))[:32]
        with self.transaction():
            if self.db.execute('SELECT 1 FROM tombstones WHERE project=? AND record_id=?',(project,rid)).fetchone():
                return {'id':rid,'status':'withdrawn','replayed':True}
            old=self.db.execute('SELECT * FROM records WHERE id=?',(rid,)).fetchone()
            if old:
                if old['text']!=text and not old['manually_edited']:raise ValueError('same record identity with changed content')
                if not old['published']:self._publish(dict(old))
                diverged=old['text']!=text and bool(old['manually_edited'])
                if diverged and not self.db.execute("SELECT 1 FROM revisions WHERE record_id=? AND reason='replay preserved manual edit'",(rid,)).fetchone():
                    self.db.execute('INSERT INTO revisions(record_id,text,status,reason,created_at) VALUES(?,?,?,?,?)',(rid,text,'candidate','replay preserved manual edit',time.time()))
                if not diverged and status=='confirmed' and old['status']=='candidate':
                    self.db.execute("UPDATE records SET status='confirmed' WHERE id=?",(rid,))
                return {'id':rid,'status':self.db.execute('SELECT status FROM records WHERE id=?',(rid,)).fetchone()[0],'replayed':True,'diverged':diverged}
            now=time.time()
            if status=='confirmed':
                for prior in self.db.execute('SELECT * FROM records WHERE project=? AND key=? AND status=\'confirmed\'',(project,key)).fetchall():
                    self.db.execute('INSERT INTO revisions(record_id,text,status,reason,created_at) VALUES(?,?,?,?,?)',
                                    (prior['id'],prior['text'],prior['status'],'superseded by '+rid,now))
                    self.db.execute("UPDATE records SET status='superseded' WHERE id=?",(prior['id'],))
            self.db.execute('INSERT INTO records(id,project,key,text,quote,kind,status,source_id,source_version,note_path,note_hash,manually_edited,created_at,published) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,0)',
                            (rid,project,key,text,quote,kind,status,source_id,int(source_version),None,None,0,now))
            for ref in refs:self.db.execute('INSERT OR IGNORE INTO record_sources(record_id,source_id,version,quote) VALUES(?,?,?,?)',(rid,ref['source_id'],ref['version'],ref['quote']))
        if payload.get('title'):
            if not isinstance(payload['title'],str):raise ValueError('invalid note title')
            with self.transaction():self.db.execute('UPDATE records SET note_title=? WHERE id=?',(label(payload['title']),rid))
        rec=dict(self.db.execute('SELECT * FROM records WHERE id=?',(rid,)).fetchone())
        self._publish(rec)
        return {'id':rid,'status':status}

    @synchronized
    def sync(self):
        root=self.vault();found={};invalid=[]
        with self.transaction():
            self.db.execute("UPDATE records SET status='stale' WHERE status NOT IN ('withdrawn','superseded','stale') AND EXISTS(SELECT 1 FROM record_sources rs JOIN sources s ON s.id=rs.source_id WHERE rs.record_id=records.id AND rs.version!=s.current_version)")
        for path in root.rglob('*.md'):
            if any(p.is_symlink() for p in [path,*list(path.parents)[:len(path.relative_to(root).parts)-1]]):
                invalid.append(str(path.relative_to(root)));continue
            try:meta,body,raw=self._parse(path)
            except (ValueError,OSError,UnicodeError):
                invalid.append(str(path.relative_to(root)));continue
            rid=meta.get('tongle_id')
            if isinstance(rid,str):found.setdefault(rid,[]).append((path,meta,body,raw))
        changed=missing=conflicts=0
        for row in self.db.execute('SELECT * FROM records').fetchall():
            rec=dict(row);rid=rec['id'];matches=found.get(rid,[])
            if rec['status'] in ['withdrawn','superseded','stale'] or not self.source_available(rec['source_id']):continue
            if not rec['published']:
                if not matches:
                    try:self._publish(rec)
                    except (OSError,ValueError):conflicts+=1
                    continue
                if len(matches)==1 and matches[0][2]==rec['text'] and all(matches[0][1].get(k)==rec[k] for k in ['project','source_id','source_version']):
                    with self.transaction():self.db.execute('UPDATE records SET published=1 WHERE id=?',(rid,))
            if rec['status'] in ['withdrawn','superseded','stale']:continue
            if len(matches)!=1:
                status='conflicted' if len(matches)>1 else 'missing'
                with self.transaction():self.db.execute("UPDATE records SET status=?,prior_status=CASE WHEN status IN ('confirmed','candidate') THEN status ELSE prior_status END WHERE id=?",(status,rid))
                if matches:conflicts+=1
                else:missing+=1
                continue
            path,meta,body,raw=matches[0]
            if meta.get('project')!=rec['project'] or meta.get('source_id')!=rec['source_id'] or meta.get('source_version')!=rec['source_version']:
                with self.transaction():self.db.execute("UPDATE records SET status='conflicted' WHERE id=?",(rid,))
                conflicts+=1;continue
            modified=body!=rec['text']
            state='candidate' if modified else rec['status']
            if state in ['missing','conflicted']:
                state=rec.get('prior_status') if digest(raw)==rec['note_hash'] and rec.get('prior_status') in ['confirmed','candidate'] else 'candidate'
            if not modified and state==rec['status'] and digest(raw)==rec['note_hash'] and str(path.relative_to(root))==rec['note_path'] and rec['published']:
                continue
            with self.transaction():
                if modified:
                    self.db.execute('INSERT INTO revisions(record_id,text,status,reason,created_at) VALUES(?,?,?,?,?)',
                                    (rid,rec['text'],rec['status'],'manual edit',time.time()));changed+=1
                self.db.execute('UPDATE records SET text=?,status=?,note_path=?,note_hash=?,manually_edited=?,published=1 WHERE id=?',
                                (body,state,str(path.relative_to(root)),digest(raw),int(modified or rec['manually_edited']),rid))
        return {'changed':changed,'missing':missing,'conflicts':conflicts,'invalid_notes':invalid}

    @synchronized
    def withdraw(self,project,rid):
        row=self.db.execute('SELECT * FROM records WHERE id=? AND project=?',(rid,project)).fetchone()
        if not row:raise ValueError('record not in project')
        with self.transaction():
            self.db.execute('INSERT OR IGNORE INTO tombstones(project,record_id,created_at) VALUES(?,?,?)',(project,rid,time.time()))
            self.db.execute("UPDATE records SET status='withdrawn' WHERE id=?",(rid,))
        return {'id':rid,'status':'withdrawn'}

    @synchronized
    def query(self,project,text,limit=8):
        self.sync()
        # CJK bigrams preserve Chinese matchability without an external tokenizer.
        terms=set(re.findall(r'[a-z0-9_]+',text.lower()))
        for run in re.findall(r'[\u3400-\u9fff]+',text):
            terms.update(run[i:i+2] for i in range(len(run)-1))
            if len(run)==1:terms.add(run)
        rows=self.db.execute("SELECT r.*,s.role AS source_role FROM records r JOIN sources s ON r.source_id=s.id WHERE r.project=? AND r.published=1 AND r.status IN ('confirmed','candidate') AND r.source_version=s.current_version AND NOT EXISTS(SELECT 1 FROM record_sources rs JOIN sources sx ON sx.id=rs.source_id LEFT JOIN source_status ssx ON ssx.source_id=sx.id WHERE rs.record_id=r.id AND (rs.version!=sx.current_version OR (ssx.state IS NOT NULL AND ssx.state!='available'))) AND NOT EXISTS(SELECT 1 FROM source_status ss WHERE ss.source_id=s.id AND ss.state!='available') AND NOT EXISTS(SELECT 1 FROM tombstones t WHERE t.project=r.project AND t.record_id=r.id)",(project,)).fetchall()
        ranked=[]
        for row in rows:
            item=dict(row);score=sum(1 for term in terms if term in item['text'].lower())
            if score or not text.strip():ranked.append((score,item['created_at'],item))
        ranked.sort(key=lambda x:(x[0],x[1]),reverse=True)
        results=[]
        for score,_,item in ranked[:max(1,min(limit,50))]:
            results.append({k:item[k] for k in ['id','text','status','kind','source_id','source_version','source_role','quote','created_at']})
        with self.transaction():self.db.execute('INSERT INTO observations(project,query,offered_ids,created_at) VALUES(?,?,?,?)',
                                     (project,text[:4000],json.dumps([r['id'] for r in results]),time.time()))
        for item in results:item['evidence']=[dict(r) for r in self.db.execute('SELECT source_id,version,quote FROM record_sources WHERE record_id=?',(item['id'],))]
        return {'results':results,'adoption':'unknown','offered':len(results)}

    @synchronized
    def context(self,project,text,limit=8,session='',budget=6000,current_source_id=None):
        if type(budget) is not int or not 1 <= budget <= 6000:
            raise ValueError('context budget must be between 1 and 6000 UTF-16 units')
        from .evolution import enabled,Evolution
        if enabled(self):return Evolution(self).context(project,text,limit,session,budget=budget,current_source_id=current_source_id)
        result=self.query(project,text,limit)
        from .library import search
        library=search(self.home,text,project=project)
        result['results'].extend(library)
        result['library_offered']=len(library)
        # Raw recent user statements remain available before model compilation.
        # They are evidence, NOT confirmed knowledge and never imply execution.
        recent=self.db.execute("SELECT s.id,s.role,s.created_at,v.text FROM sources s JOIN versions v ON v.source_id=s.id AND v.version=s.current_version WHERE s.project=? AND s.kind='event' AND s.role='user' AND NOT EXISTS(SELECT 1 FROM source_status ss WHERE ss.source_id=s.id AND ss.state!='available') AND NOT EXISTS(SELECT 1 FROM records r JOIN tombstones t ON t.record_id=r.id AND t.project=r.project WHERE r.source_id=s.id) ORDER BY s.created_at DESC LIMIT 12",(project,)).fetchall()
        result['recent_statements']=[{'source_id':r['id'],'role':r['role'],'text':r['text'][:3000],
                                      'status':'user_statement','created_at':r['created_at']} for r in reversed(recent)]
        return result
