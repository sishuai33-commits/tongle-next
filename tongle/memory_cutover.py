"""Explicit retirement of uncommitted work when a project enters memory mode.
No model calls, source changes, cache deletion, retry resets, or fake completion.
prepare is read-only; apply compares the entire project queue under the engine
lock and one transaction. Reclaim must honor memory_cutover_entries retention.
"""
import hashlib
import json
import time
from pathlib import Path
from .memory_runtime import allowed

_VERSION='memory-cutover-v1'
_SAFE={'pending','budget_wait','retry','failed','blocked','coverage_wait','ancestor_wait'}
_TERMINAL={'done','obsolete'}


def _hash(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def _text(value,name):
    if not isinstance(value,str) or not value.strip() or len(value)>200:
        raise ValueError(name+' must be a bounded nonempty string')
    return value


def _exists(engine,table):
    return bool(engine.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone())


def _select(engine,table,where,args=(),order='id'):
    if not _exists(engine,table):return []
    return [dict(r) for r in engine.db.execute('SELECT * FROM '+table+' WHERE '+where+' ORDER BY '+order,args)]


def _published_file(engine,record):
    """Read-only bounded file fingerprint, never emit note contents."""
    relative=record.get('note_path');expected=record.get('note_hash')
    result={'record_id':record['id'],'path':relative,'expected_hash':expected,'verified':False}
    if not isinstance(relative,str) or not relative or not isinstance(expected,str) or len(expected)!=64:
        return dict(result,state='missing_publication_binding')
    path=Path(relative)
    if path.is_absolute() or '..' in path.parts or '\\' in relative:
        return dict(result,state='unsafe_publication_binding')
    try:
        root=engine.vault();target=root/path
        if any(p.is_symlink() for p in [target,*target.parents]):
            return dict(result,state='symlink_publication_binding')
        if not target.exists():return dict(result,state='publication_file_missing')
        if not target.is_file():return dict(result,state='publication_file_not_regular')
        size=target.stat().st_size
        if size>2_000_000:return dict(result,state='publication_file_too_large',size=size)
        digest=hashlib.sha256(target.read_bytes()).hexdigest()
        return dict(result,state='published_file',actual_hash=digest,size=size,verified=digest==expected)
    except (OSError,ValueError):
        return dict(result,state='publication_file_unreadable')


def _related(engine,table,row):
    if table=='knowledge_work':
        return {
            'calls':_select(engine,'knowledge_calls','work_id=?',(row['id'],)),
            'audits':_select(engine,'knowledge_failures','work_id=?',(row['id'],),'rowid'),
            'seen':_select(engine,'knowledge_seen','work_id=?',(row['id'],),'source_id,version,part'),
            'batches':_select(engine,'knowledge_batches','batch_id=?',(row['id'],),'project'),
            'triage':_select(engine,'knowledge_triage','work_id=?',(row['id'],),'work_id')}
    records=_select(engine,'records','source_id=? AND source_version=?',(row['source_id'],row['version']))
    records=[r for r in records if r['key'].startswith('compile:'+row['id']+':')]
    seen=_select(engine,'knowledge_seen','source_id=? AND version=?',(row['source_id'],row['version']),'part')
    owners=[]
    for wid in sorted({r['work_id'] for r in seen if r['work_id'] is not None}):
        owner=engine.db.execute('SELECT id,project FROM knowledge_work WHERE id=?',(wid,)).fetchone()
        if owner:owners.append(dict(owner))
    source=engine.db.execute('SELECT s.id,s.project,v.version,v.hash FROM sources s JOIN versions v ON v.source_id=s.id WHERE s.id=? AND v.version=?',(row['source_id'],row['version'])).fetchone()
    return {'usage':_select(engine,'usage','job_id=?',(row['id'],)),
            'records':records,'record_files':[_published_file(engine,record) for record in records],
            'record_history':[{
                'id':record['id'],
                'revisions':_select(engine,'revisions','record_id=?',(record['id'],),'rowid'),
                'sources':_select(engine,'record_sources','record_id=?',(record['id'],),'source_id,version,quote'),
                'tombstones':_select(engine,'tombstones','record_id=?',(record['id'],),'project')
            } for record in records],
            'seen':seen,'owners':owners,'source':dict(source) if source else None,
            'published_owner_states':[{
                'id':owner['id'],
                'state':engine.db.execute('SELECT state FROM knowledge_work WHERE id=?',(owner['id'],)).fetchone()[0],
                'lease_until':engine.db.execute('SELECT lease_until FROM knowledge_work WHERE id=?',(owner['id'],)).fetchone()[0],
                'unsettled_calls':_select(engine,'knowledge_calls','work_id=? AND elapsed IS NULL',(owner['id'],))
            } for owner in owners] if records else []}


def _inventory(engine,project):
    jobs=[dict(r) for r in engine.db.execute('SELECT j.* FROM jobs j JOIN sources s ON s.id=j.source_id WHERE s.project=? ORDER BY j.id',(project,))]
    work=_select(engine,'knowledge_work','project=?',(project,))
    if len(jobs)+len(work)>20000:raise ValueError('project queue exceeds bounded retirement inventory')
    return [(table,row,_related(engine,table,row)) for table,rows in [('jobs',jobs),('knowledge_work',work)] for row in rows]


def _delegated(related):
    """A batched legacy job is inert history only with an intact ownership ledger.

    captured() marks batched before queueing, so the label alone proves nothing.
    NULL work_id is a baseline checkpoint, not proof of delegation to a work row.
    This does not claim that the work completed or all source parts were learned.
    """
    seen=related.get('seen',[]);source=related.get('source')
    if not seen or not source or any(not row['work_id'] or type(row['part']) is not int or row['part']<0 for row in seen):return False
    owners=related['owners'];ids={row['work_id'] for row in seen}
    return ({owner['id'] for owner in owners}==ids
            and all(owner['project']==source['project'] for owner in owners))


def _published_history(row,related):
    records=related.get('records',[])
    return (row['state']=='batched' and _delegated(related) and bool(records)
            and len(related['seen'])==1 and len(related['published_owner_states'])==1
            and all(owner['state']=='done' and (owner['lease_until'] or 0)<=time.time() and not owner['unsettled_calls'] for owner in related['published_owner_states'])
            and all(record.get('published')==1 and record['status']=='superseded' for record in records)
            and len(related['record_files'])==len(records)
            and all(file['verified'] for file in related['record_files']))


def _superseded_missing_history(row,related):
    """Retain a stopped historical responsibility, explicitly not verified publication."""
    records=related.get('records',[])
    return (row['state']=='batched' and _delegated(related) and len(related['seen'])==1
            and len(related['published_owner_states'])==1
            and all(owner['state']=='done' and (owner['lease_until'] or 0)<=time.time() and not owner['unsettled_calls'] for owner in related['published_owner_states']) and bool(records)
            and all(record.get('published')==1 and record['status']=='superseded' for record in records)
            and len(related['record_files'])==len(records)
            and any(file['state']=='publication_file_missing' for file in related['record_files'])
            and all(file['verified'] or file['state']=='publication_file_missing' for file in related['record_files']))


def _baseline_parts_ok(seen):
    """Every baseline checkpoint part is a valid nonnegative integer, no owner."""
    if not seen:return False
    for row in seen:
        if row.get('work_id') is not None:return False
        if type(row.get('part')) is not int or row['part']<0:return False
    return True


def _usage_settled(related):
    """Every usage row has a settled elapsed_seconds (non-NULL)."""
    return all(u.get('elapsed_seconds') is not None for u in related.get('usage',[]))


def _attempts_ok(row,related):
    """attempts must be a valid nonnegative integer equal to settled usage count."""
    attempts=row.get('attempts')
    return type(attempts) is int and attempts>=0 and attempts==len(related.get('usage',[])) and _usage_settled(related)


def _records_uniform_project(records,source):
    """Every retained record shares the source project and is superseded+published."""
    if not records:return False
    project=source.get('project') if source else None
    return all(record.get('project')==project and record.get('status')=='superseded' and record.get('published')==1 for record in records)


def _files_verified_or_missing(related,records):
    """File binding proves only verified matches or explicitly missing files."""
    files=related.get('record_files',[])
    if len(files)!=len(records):return False
    return all(file.get('verified') or file.get('state')=='publication_file_missing' for file in files)


def _no_live_lease(row,now):
    return (row.get('lease_until') or 0)<=now


def _retain_unprocessed_history(row,related,now):
    """Class1. Batched, source exists, no attempts/usage/records/seen/owners, no lease."""
    if row['state']!='batched':return False
    if not related.get('source'):return False
    if type(row.get('attempts')) is not int or row['attempts']!=0:return False
    if related.get('usage'):return False
    if related.get('records'):return False
    if related.get('seen'):return False
    if related.get('owners'):return False
    if not _no_live_lease(row,now):return False
    return True


def _retain_baseline_superseded_history(row,related,now):
    """Class2. Batched, missing/verified files only, baseline NULL seen, settled usage.

    Missing files do NOT prove publication; this is inert history, never completion.
    """
    if row['state']!='batched':return False
    source=related.get('source')
    if not source:return False
    if not _no_live_lease(row,now):return False
    seen=related.get('seen',[])
    if not _baseline_parts_ok(seen):return False
    if related.get('owners'):return False
    if not _attempts_ok(row,related):return False
    records=related.get('records',[])
    if not records:return False
    if not _records_uniform_project(records,source):return False
    if not _files_verified_or_missing(related,records):return False
    owners=related.get('published_owner_states',[])
    if owners:
        return False
    return True


def _blockers(row,related,table,now):
    reasons=[]
    if row['state']=='running':reasons.append('running_work')
    elif row['state'] not in _SAFE:
        justified=False
        if table=='jobs' and row['state']=='batched':
            justified=_delegated(related) or _retain_unprocessed_history(row,related,now) or _retain_baseline_superseded_history(row,related,now)
        if not justified:
            reasons.append('unverified_batched_delegation' if table=='jobs' and row['state']=='batched' else 'unknown_or_publication_state')
    if (row.get('lease_until') or 0)>now:reasons.append('live_lease')
    if table=='knowledge_work':
        if related['batches']:reasons.append('committed_batch_must_preserve_publication')
        if any(call['elapsed'] is None for call in related['calls']):reasons.append('unsettled_model_call')
        if any(a['state'] in ('completion','delta_complete_v1','delta_operator_intent_v1','delta_operator_complete_v1') for a in related['audits']):
            reasons.append('commit_or_completion_audit')
    else:
        if any(call['elapsed_seconds'] is None for call in related['usage']):reasons.append('unsettled_legacy_call')
        if row['attempts']>len(related['usage']):reasons.append('legacy_attempt_without_settlement')
        if related['records'] and not (_published_history(row,related) or _superseded_missing_history(row,related)):
            if not (_retain_baseline_superseded_history(row,related,now) or _retain_unprocessed_history(row,related,now)):
                reasons.append('committed_legacy_records')
    return reasons


def _plan(engine,project,operation_id,created_at):
    inventory=_inventory(engine,project);entries=[];blocked=[];ignored={'jobs':0,'knowledge_work':0}
    now=time.time()
    for table,row,related in inventory:
        if row['state'] in _TERMINAL:
            ignored[table]+=1;continue
        reasons=_blockers(row,related,table,now)
        if reasons:
            blocked.append({'table_name':table,'row_id':row['id'],'state':row['state'],'reasons':reasons});continue
        delegated=table=='jobs' and row['state']=='batched'
        after=dict(row) if delegated else dict(row,state='obsolete',lease_token=None,lease_until=0.0 if table=='knowledge_work' else 0)
        if delegated:
            if _retain_unprocessed_history(row,related,now):
                action='retain_unprocessed_history'
            elif _retain_baseline_superseded_history(row,related,now):
                action='retain_baseline_superseded_history'
            elif _superseded_missing_history(row,related):
                action='retain_superseded_history'
            elif _published_history(row,related):
                action='retain_published'
            else:
                action='retain_delegated'
        else:
            action='retire'
        entries.append({'action':action,'table_name':table,'row_id':row['id'],'prior_state':row['state'],
            'prior_lease_token':row.get('lease_token'),'prior_lease_until':row.get('lease_until',0),
            'before_hash':_hash(row),'after_hash':_hash(after),'related_hash':_hash(related),
            'historical_file_facts':[{'record_id':file['record_id'],'state':file['state'],'verified':file['verified']}
                for file in related.get('record_files',[])]})
    plan={'protocol':_VERSION,'operation_id':operation_id,'project':project,'created_at':created_at,
        'inventory_hash':_hash(inventory),'entries':entries,'blocked':blocked,'ignored':ignored,
        'meaning':'retired_without_completion; raw sources and work evidence retained'}
    plan['plan_hash']=_hash(plan)
    return plan


def prepare(engine,project,operation_id):
    """Return a body-free frozen plan; blocked rows prohibit the whole apply."""
    _text(project,'project');_text(operation_id,'operation_id')
    with engine.locked(),engine.transaction():
        return _plan(engine,project,operation_id,time.time())


def _schema(engine):
    engine.db.execute('CREATE TABLE IF NOT EXISTS memory_cutover_operations(operation_id TEXT PRIMARY KEY,project TEXT NOT NULL,plan_hash TEXT NOT NULL,result TEXT NOT NULL,applied_at REAL NOT NULL)')
    engine.db.execute("CREATE TABLE IF NOT EXISTS memory_cutover_entries(operation_id TEXT NOT NULL,table_name TEXT NOT NULL,row_id TEXT NOT NULL,prior_state TEXT NOT NULL,prior_lease_token TEXT,prior_lease_until REAL,before_hash TEXT NOT NULL,after_hash TEXT NOT NULL,related_hash TEXT NOT NULL,created_at REAL NOT NULL,action TEXT NOT NULL DEFAULT 'retire',PRIMARY KEY(table_name,row_id))")
    if 'action' not in {r['name'] for r in engine.db.execute('PRAGMA table_info(memory_cutover_entries)')}:
        engine.db.execute("ALTER TABLE memory_cutover_entries ADD COLUMN action TEXT NOT NULL DEFAULT 'retire'")


def apply(engine,plan):
    """CAS-retire safe work, or reject atomically. Idempotent by operation_id."""
    if not isinstance(plan,dict):raise ValueError('invalid cutover plan')
    required={'protocol','operation_id','project','created_at','inventory_hash','entries','blocked','ignored','meaning','plan_hash'}
    if set(plan)!=required or plan['protocol']!=_VERSION:raise ValueError('invalid cutover plan fields')
    project=_text(plan['project'],'project');operation_id=_text(plan['operation_id'],'operation_id')
    if _hash({k:v for k,v in plan.items() if k!='plan_hash'})!=plan['plan_hash']:raise ValueError('plan hash mismatch')
    with engine.locked(),engine.transaction():
        if allowed(engine,project):raise ValueError('retirement requires memory mode for the exact project')
        _schema(engine)
        previous=engine.db.execute('SELECT * FROM memory_cutover_operations WHERE operation_id=?',(operation_id,)).fetchone()
        if previous:
            if previous['project']!=project or previous['plan_hash']!=plan['plan_hash']:
                raise ValueError('operation_id reused with different plan')
            check=verify(engine,operation_id)
            if not check['ok']:raise ValueError('previous retirement no longer verifies')
            return dict(json.loads(previous['result']),replayed=True)
        if plan['blocked']:raise ValueError('plan has blocked work; resolve before retirement')
        current=_plan(engine,project,operation_id,plan['created_at'])
        if current['plan_hash']!=plan['plan_hash']:raise ValueError('project queue changed since prepare; prepare again')
        counts={'jobs':0,'knowledge_work':0};retained=0;published=0;history=0;missing_files=0
        unprocessed=0;baseline=0;baseline_missing_files=0;now=time.time()
        for item in plan['entries']:
            table=item['table_name']
            if table not in counts:raise ValueError('invalid retirement table')
            if item['action']=='retire':
                changed=engine.db.execute('UPDATE '+table+" SET state='obsolete',lease_token=NULL,lease_until=0 WHERE id=? AND state=?",(item['row_id'],item['prior_state'])).rowcount
                if changed!=1:raise ValueError('retirement row compare-and-swap failed')
            elif item['action'] not in ('retain_delegated','retain_published','retain_superseded_history','retain_unprocessed_history','retain_baseline_superseded_history') or table!='jobs':
                raise ValueError('invalid retirement action')
            row=dict(engine.db.execute('SELECT * FROM '+table+' WHERE id=?',(item['row_id'],)).fetchone())
            if _hash(row)!=item['after_hash']:raise ValueError('retirement changed protected work bytes')
            engine.db.execute('INSERT INTO memory_cutover_entries(operation_id,table_name,row_id,prior_state,prior_lease_token,prior_lease_until,before_hash,after_hash,related_hash,created_at,action) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (operation_id,table,item['row_id'],item['prior_state'],item['prior_lease_token'],item['prior_lease_until'],item['before_hash'],item['after_hash'],item['related_hash'],now,item['action']))
            if item['action']=='retire':counts[table]+=1
            elif item['action']=='retain_superseded_history':
                history+=1;missing_files+=sum(file['state']=='publication_file_missing' for file in item['historical_file_facts'])
            elif item['action']=='retain_published':published+=1
            elif item['action']=='retain_unprocessed_history':
                unprocessed+=1
            elif item['action']=='retain_baseline_superseded_history':
                baseline+=1;baseline_missing_files+=sum(file['state']=='publication_file_missing' for file in item['historical_file_facts'])
            else:retained+=1
        result={'operation_id':operation_id,'project':project,'status':'retired','counts':counts,
                'completed':0,'retained_delegated_jobs':retained,'retained_published_jobs':published,'retained_superseded_history_jobs':history,'historical_missing_files':missing_files,
                'retained_unprocessed_jobs':unprocessed,'retained_baseline_superseded_jobs':baseline,'baseline_missing_files':baseline_missing_files,
                'replayed':False,'plan_hash':plan['plan_hash']}
        engine.db.execute('INSERT INTO memory_cutover_operations VALUES(?,?,?,?,?)',(operation_id,project,plan['plan_hash'],json.dumps(result,ensure_ascii=False,sort_keys=True),now))
        report=verify(engine,operation_id)
        if not report['ok']:raise ValueError('retirement verification failed')
        return result


def verify(engine,operation_id):
    """Verify state, all retained bytes, calls and previous audits; no model calls."""
    _text(operation_id,'operation_id')
    with engine.locked(),engine.transaction():
        if not _exists(engine,'memory_cutover_operations'):
            return {'ok':False,'operation_id':operation_id,'problems':['operation_missing']}
        operation=engine.db.execute('SELECT * FROM memory_cutover_operations WHERE operation_id=?',(operation_id,)).fetchone()
        if not operation:return {'ok':False,'operation_id':operation_id,'problems':['operation_missing']}
        result=json.loads(operation['result']);problems=[];counts={'jobs':0,'knowledge_work':0};retained=0;published=0;history=0;missing_files=0
        unprocessed=0;baseline=0;baseline_missing_files=0
        entries=engine.db.execute('SELECT * FROM memory_cutover_entries WHERE operation_id=? ORDER BY table_name,row_id',(operation_id,)).fetchall()
        for entry in entries:
            table=entry['table_name']
            if table not in counts:problems.append('invalid_table');continue
            row=engine.db.execute('SELECT * FROM '+table+' WHERE id=?',(entry['row_id'],)).fetchone()
            if not row:problems.append(table+':'+entry['row_id']+':missing');continue
            row=dict(row)
            delegated=entry['action'] in ('retain_delegated','retain_published','retain_superseded_history','retain_unprocessed_history','retain_baseline_superseded_history')
            if entry['action']=='retain_superseded_history':history+=1
            elif entry['action']=='retain_published':published+=1
            elif entry['action']=='retain_unprocessed_history':unprocessed+=1
            elif entry['action']=='retain_baseline_superseded_history':baseline+=1
            elif delegated:retained+=1
            else:counts[table]+=1
            expected_state='batched' if delegated else 'obsolete'
            if _hash(row)!=entry['after_hash'] or row['state']!=expected_state:problems.append(table+':'+entry['row_id']+':work_changed')
            related=_related(engine,table,row)
            if entry['action']=='retain_superseded_history':missing_files+=sum(file['state']=='publication_file_missing' for file in related['record_files'])
            if entry['action']=='retain_baseline_superseded_history':baseline_missing_files+=sum(file['state']=='publication_file_missing' for file in related['record_files'])
            if _hash(related)!=entry['related_hash']:problems.append(table+':'+entry['row_id']+':evidence_changed')
        if (counts!=result['counts'] or retained!=result.get('retained_delegated_jobs',0)
                or published!=result.get('retained_published_jobs',0)
                or history!=result.get('retained_superseded_history_jobs',0)
                or missing_files!=result.get('historical_missing_files',0)
                or unprocessed!=result.get('retained_unprocessed_jobs',0)
                or baseline!=result.get('retained_baseline_superseded_jobs',0)
                or baseline_missing_files!=result.get('baseline_missing_files',0)):
            problems.append('entry_counts_changed')
        return {'ok':not problems,'operation_id':operation_id,'project':operation['project'],'counts':counts,'retained_delegated_jobs':retained,'retained_published_jobs':published,'retained_superseded_history_jobs':history,'historical_missing_files':missing_files,'retained_unprocessed_jobs':unprocessed,'retained_baseline_superseded_jobs':baseline,'baseline_missing_files':baseline_missing_files,'problems':problems}
