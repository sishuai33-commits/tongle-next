"""tongle command line; machine-readable success/error replies."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from .engine import Engine
from .worker import process_jobs
from .archive import export_archive,restore_archive


def parser():
    p=argparse.ArgumentParser(prog='tongle')
    p.add_argument('--home',type=Path)
    p.add_argument('--protocol-version')
    sub=p.add_subparsers(dest='command',required=True)
    sub.add_parser('capabilities',help='read-only versioned adapter contract')
    x=sub.add_parser('organize');x.add_argument('--project',required=True);x.add_argument('--action',required=True,choices=['topics','register-topic','rename-topic','map','relation','relations'])
    x=sub.add_parser('observe-import');x.add_argument('--project',required=True);x.add_argument('--host',required=True,choices=['pi','codex']);x.add_argument('--manifest',required=True,type=Path)
    sub.add_parser('index-refresh',help='rebuild bounded derived retrieval projections')
    x=sub.add_parser('observe');x.add_argument('--project',required=True);x.add_argument('--days',type=int,default=30)
    x=sub.add_parser('init');x.add_argument('--vault',required=True,type=Path);x.add_argument('--evolving',action='store_true')
    x=sub.add_parser('capture')
    for field in ['project','session','event-id']:x.add_argument('--'+field,required=True)
    x.add_argument('--role',choices=['user','assistant'],required=True)
    x.add_argument('--receipt-id',default=None)
    x.add_argument('--memory-receipt-id',default=None)
    x.add_argument('--reply-to-event-id',default=None)
    x=sub.add_parser('usage-report');x.add_argument('--project',required=True);x.add_argument('--days',type=int,default=30)
    x=sub.add_parser('ingest');x.add_argument('--project',required=True);x.add_argument('--path',required=True);x.add_argument('--source-id')
    x=sub.add_parser('record')
    for field in ['project','key','source-id']:x.add_argument('--'+field,required=True)
    x.add_argument('--source-version',required=True,type=int);x.add_argument('--status',required=True,choices=['candidate','confirmed'])
    for cmd in ['query','context']:
        x=sub.add_parser(cmd);x.add_argument('--project',required=True);x.add_argument('--limit',type=int,default=8)
        if cmd=='context':
            text=x.add_mutually_exclusive_group(required=True)
            text.add_argument('--text')
            text.add_argument('--text-stdin',action='store_true',help='read the full bounded user turn from UTF-8 stdin')
            x.add_argument('--session',default='')
            x.add_argument('--budget',type=int,default=6000)
            x.add_argument('--current-source-id')
        else:x.add_argument('--text',required=True)
    x=sub.add_parser('synthesize');x.add_argument('--project',required=True);x.add_argument('--text',default='整理共同主题、适用条件、限制与分歧')
    x=sub.add_parser('source-state');x.add_argument('--project',required=True);x.add_argument('--id',required=True);x.add_argument('--state',required=True,choices=['available','missing','revoked'])
    x=sub.add_parser('source');x.add_argument('--id',required=True);x.add_argument('--version',type=int)
    x=sub.add_parser('withdraw');x.add_argument('--project',required=True);x.add_argument('--id',required=True)
    x=sub.add_parser('source-index');x.add_argument('--project',required=True);x.add_argument('--rebuild',action='store_true')
    x=sub.add_parser('source-search');x.add_argument('--project',required=True);x.add_argument('--text',required=True);x.add_argument('--limit',type=int,default=5);x.add_argument('--history',action='store_true')
    x=sub.add_parser('corrections');x.add_argument('--project',required=True)
    x=sub.add_parser('correct');x.add_argument('--project',required=True)
    x=sub.add_parser('memory');x.add_argument('--project',required=True);x.add_argument('--session',required=True)
    x.add_argument('--read-only',action='store_true',help='require the read-only review status boundary')
    x.add_argument('--event-id',help='stable operation identity for direct CLI writes; same-session retries reuse it')
    x.add_argument('--action',required=True,choices=['help','search','get','source','remember','update','amend','drafts','resolve','finish','review'])
    sub.add_parser('sync');sub.add_parser('status');sub.add_parser('doctor')
    x=sub.add_parser('reclaim', help='bounded sweep of terminal stage caches plus file space reclamation')
    x.add_argument('--limit',type=int,default=None)
    x.add_argument('--vacuum-free-bytes',dest='vacuum_free_bytes',type=int,default=None)
    x=sub.add_parser('process');x.add_argument('--limit',type=int,default=5);x.add_argument('--force',action='store_true')
    x=sub.add_parser('worker');x.add_argument('--once',action='store_true');x.add_argument('--interval',type=float,default=15);x.add_argument('--limit',type=int,default=5)
    x=sub.add_parser('export');x.add_argument('--to',required=True)
    x=sub.add_parser('restore');x.add_argument('--from',dest='archive',required=True);x.add_argument('--vault',required=True)
    x=sub.add_parser('retry');x.add_argument('--job',required=True)
    x=sub.add_parser('learn-observe', help='register an existing source-backed task result')
    x.add_argument('--project',required=True);x.add_argument('--receipt',required=True);x.add_argument('--source',required=True)
    x=sub.add_parser('learn-cases', help='attach frozen new tasks and independent criteria')
    x.add_argument('--id',required=True);x.add_argument('--case',required=True,type=Path)
    x=sub.add_parser('learn-list', help='show learning outcomes without model calls');x.add_argument('--project')
    x=sub.add_parser('learn-process', help='advance at most two budgeted learning calls')
    x=sub.add_parser('learn-pause', help='stop using this exact learned framework')
    x.add_argument('--id',required=True);x.add_argument('--reason',required=True)
    sub.add_parser('topic-list', help='show bounded topic synthesis status without model calls')
    sub.add_parser('topic-process', help='advance at most two topic synthesis calls')
    return p


def _host_integration_disabled(args):
    # A pre-upgrade host may still have its old adapter loaded in memory.
    # Enforce the instance switch at the child-process boundary as well. The
    # ordinary explicit CLI (without host routing environment) stays usable.
    if args.command not in ('capture','context','ingest','memory') or args.home is None:
        return False
    bound=os.environ.get('TONGLE_NEXT_HOME')
    if not bound or Path(bound).expanduser().absolute()!=args.home.expanduser().absolute():
        return False
    path=args.home/'instance.json'
    if not path.exists():return False
    if path.is_symlink() or path.stat().st_size>65536:raise ValueError('invalid instance metadata')
    state=json.loads(path.read_text())
    if not isinstance(state,dict) or state.get('owner')!='tongle-next':raise ValueError('foreign instance metadata')
    return state.get('enabled') is False


def _main(args):
    from .protocol import require_compatible
    require_compatible(args.protocol_version)
    if args.command!='capabilities' and args.home is None:
        raise ValueError('--home is required for this command')
    if args.command=='capabilities':
        from .protocol import capabilities
        return capabilities()
    if args.command=='restore':return restore_archive(args.home,args.archive,args.vault)
    if args.command=='observe':
        from .observability import read_only_report
        return read_only_report(args.home,args.project,args.days)
    if args.command=='memory' and args.read_only:
        args.read_only_report=True
        if args.action!='review':raise ValueError('--read-only requires review status')
    if args.command=='memory' and args.action=='review':
        text=sys.stdin.read(64001)
        if len(text)>64000:raise ValueError('memory input too large')
        args.review_payload=json.loads(text)
        if not isinstance(args.review_payload,dict):raise ValueError('memory input must be an object')
        if args.read_only and args.review_payload.get('op')!='status':
            raise ValueError('--read-only requires review status')
        if args.review_payload.get('op')=='status':
            args.read_only_report=True
            if args.event_id is not None or 'event_key' in args.review_payload:
                raise ValueError('review status is read-only; no event identity')
            if _host_integration_disabled(args):
                return {'status':'disabled','disabled':True,'reviews':None,'read_only':True}
            from .console_report import read_review_status
            return read_review_status(args.home,args.project,args.session,args.review_payload)
    if args.command!='init' and not (args.home/'state.sqlite3').is_file():raise ValueError('tongle home not initialized')
    if (args.home/'state.sqlite3').is_symlink():raise ValueError('database symlink rejected')
    if _host_integration_disabled(args):
        return {'status':'disabled','disabled':True,'results':[],'recent_statements':[],
                'rendered_context':'','reason':'host integration disabled'}
    if args.command=='context':
        # Capture accepts at most 200,000 Unicode characters. A byte limit
        # bounds transport before decoding; the core separately bounds its
        # retrieval projection while keeping the complete request constraints.
        if args.text_stdin:
            raw=sys.stdin.buffer.read(800001)
            if len(raw)>800000:raise ValueError('context text exceeds 800000 byte transport bound')
            args.text=raw.decode('utf-8')
        if len(args.text)>200000:raise ValueError('context text exceeds 200000 character bound')
        if '\x00' in args.text:raise ValueError('context text contains NUL')
    if args.command=='memory':
        # Prompt hooks can precede Codex's native message persistence. Reconcile
        # only already-observed pending messages before opening this process's
        # Engine; nested capture must never wait on our own database lock.
        from adapters.codex_identity import reconcile_pending
        args.native_capture_reconciliation=reconcile_pending(
            args.home,args.project,args.session,Path(__file__).resolve().parents[1],sys.executable)
    engine=Engine(args.home)
    try:
        from .evolution import enabled,Evolution
        evolving=enabled(engine)
        if args.command=='observe-import':
            from .observability import import_native_manifest
            if args.manifest.is_symlink() or args.manifest.stat().st_size>2_000_000:
                raise ValueError('invalid native metadata manifest')
            return import_native_manifest(engine,args.host,args.project,json.loads(args.manifest.read_text()))
        if args.command=='organize':
            from .knowledge_organization import KnowledgeOrganization
            organization=KnowledgeOrganization(engine)
            if args.action=='topics':return {'topics':organization.topics(args.project)}
            if args.action=='map':return {'mapped':organization.map_existing(args.project),'files_moved':0}
            text=sys.stdin.read(64001)
            if len(text)>64000:raise ValueError('organization request too large')
            payload=json.loads(text or '{}')
            if not isinstance(payload,dict):raise ValueError('organization request must be object')
            if args.action=='register-topic':
                if set(payload)-{'name','aliases'}:raise ValueError('invalid topic fields')
                return organization.register_topic(args.project,payload.get('name'),payload.get('aliases',[]))
            if args.action=='rename-topic':
                if set(payload)-{'id','name','aliases'}:raise ValueError('invalid topic fields')
                return organization.rename_topic(args.project,payload.get('id'),payload.get('name'),payload.get('aliases',[]))
            if args.action=='relations':
                if set(payload)!={'id'}:raise ValueError('relation listing requires id')
                return {'relations':organization.list_relations(args.project,payload['id'])}
            if set(payload)-{'owner_id','expected_version','target_id','relation','target_version','evidence'}:
                raise ValueError('invalid relation fields')
            return organization.add_relation(args.project,**payload)
        if args.command=='index-refresh':
            from .knowledge_index import KnowledgeIndex
            from .evolution import config
            index=KnowledgeIndex(engine,Evolution(engine).cards)
            result=index.refresh(256)
            from .source_memory import SourceMemory
            memory=SourceMemory(engine)
            projects=[r[0] for r in engine.db.execute('SELECT DISTINCT s.project FROM source_memory_dirty d JOIN sources s ON s.id=d.source_id JOIN versions v ON v.source_id=d.source_id AND v.version=d.version ORDER BY s.project LIMIT 16')]
            result['sources']=[memory.index_sources(project,limit=64) for project in projects]
            settings=config(engine)
            cfg=settings.get('retrieval',{})
            if cfg.get('strategy') in ('hybrid','rerank','online_admission'):
                from .semantic_index import SemanticIndex
                result['semantic']=SemanticIndex(index,cfg).refresh()
            from .graph_retrieval import GraphIndex,options
            graph=options(settings)
            if graph:
                result['graph']=GraphIndex(index).refresh(limit=graph['refresh_limit'])
            return result
        if args.command=='init':
            result=engine.init(args.vault,evolving=args.evolving or evolving)
            if args.evolving and not evolving:
                from .evolution import config
                import os,uuid
                target=engine.home/'config.json'
                if target.is_symlink():raise ValueError('configuration symlink rejected')
                settings=config(engine);settings['knowledge_mode']='evolving'
                temporary=target.with_name('.config-'+uuid.uuid4().hex)
                with temporary.open('x') as f:json.dump(settings,f);f.flush();os.fsync(f.fileno())
                os.replace(temporary,target)
            return result
        if args.command in ('topic-list', 'topic-process'):
            if not evolving:raise ValueError('topic synthesis requires evolving mode')
            if args.command == 'topic-list':
                from .views import topic_snapshot
                return topic_snapshot(engine)
            from .topic_synthesis import TopicSynthesis
            from .evolution import config
            return TopicSynthesis(Evolution(engine)).cycle(config(engine))
        if args.command.startswith('learn-'):
            if not evolving:raise ValueError('learning requires evolving knowledge mode')
            if args.command=='learn-list' and not engine.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='knowledge_learning'").fetchone():
                return {'records':[], 'note':'learning has no records'}
            from .learning import Learning
            from .evolution import config
            learning=Learning(Evolution(engine))
            if args.command=='learn-observe':return learning.observe(args.project,args.receipt,args.source)
            if args.command=='learn-cases':return learning.set_cases(args.id,json.loads(args.case.read_text()))
            if args.command=='learn-list':
                return {'records':[dict({k:r[k] for k in ('id','project','receipt_id','state')},comparison=r['payload'].get('comparison')) for r in learning.list(args.project)],
                        'note':'controlled validation is not general user benefit'}
            if args.command=='learn-process':return learning.cycle(config(engine),max_calls=2)
            if args.command=='learn-pause':return learning.pause(args.id,args.reason)
        if args.command in ('source-index','source-search','correct','corrections'):
            from .source_memory import SourceMemory
            memory=SourceMemory(engine)
            if args.command=='source-index':return memory.index_sources(args.project,rebuild=args.rebuild)
            if args.command=='source-search':
                indexed=memory.index_sources(args.project,limit=64)
                return {'results':memory.search(args.project,args.text,args.limit,args.history),'index':indexed}
            if args.command=='corrections':return {'corrections':memory.corrections(args.project)}
            payload=json.loads(sys.stdin.read(16001))
            if not isinstance(payload,dict) or 'project' in payload:raise ValueError('invalid correction input')
            return memory.add_correction(args.project,**payload)
        if args.command=='memory':
            from .inline_notes import InlineNotes
            from .source_memory import SourceMemory
            if hasattr(args,'review_payload'):
                payload=args.review_payload
            else:
                text=sys.stdin.read(64001)
                if len(text)>64000:raise ValueError('memory input too large')
                payload=json.loads(text)
            if not isinstance(payload,dict):raise ValueError('memory input must be an object')
            if any(k in payload for k in ('project','home','session')):raise ValueError('host identity cannot be supplied in payload')
            if args.event_id is not None:
                if args.action not in ('remember','update','amend','resolve','finish','review') or (args.action=='review' and payload.get('op')=='status'):
                    raise ValueError('--event-id is only used for memory writes')
                if not args.event_id.strip() or len(args.event_id)>200:
                    raise ValueError('event-id must be a bounded nonempty string')
                if 'event_key' in payload:raise ValueError('event_key cannot accompany --event-id')
                payload['event_key']='cli:'+json.dumps([args.session,args.event_id],separators=(',',':'))
            if args.action=='help':
                if payload:raise ValueError('help takes an empty object')
                from .memory_help import memory_help
                return memory_help()
            notes=InlineNotes(engine)
            if args.action=='search':
                from .retrieval_budget import verification_scope, verification_report
                with verification_scope(engine):
                    query=payload.get('query','')
                    include_assistant=payload.get('include_assistant',False)
                    if type(include_assistant) is not bool:raise ValueError('include_assistant must be boolean')
                    include_history=payload.get('include_history',False)
                    if type(include_history) is not bool:raise ValueError('include_history must be boolean')
                    from .retrieval_plan import build_query_plan
                    plan=build_query_plan(query,subquestions=payload.get('subquestions'),intent=payload.get('intent'))
                    found=notes.search(args.project,query,include_history=include_history,
                        subquestions=payload.get('subquestions'),intent=payload.get('intent'))
                    from .memory_runtime import note_view
                    from .inline_notes import search_metadata
                    candidates=[]
                    for candidate in found['candidates']:
                        view=note_view(engine,args.project,candidate)
                        # A later correction/budget overlay may change the read
                        # projection. Never leave the old claim's metadata next
                        # to the replacement or suppressed body.
                        if view.get('effective_correction'):
                            view.update(search_metadata(view.get('metadata'),view['state'],
                                effective_correction=True))
                        elif view['state']!='active':
                            view.update(search_metadata(None,view['state']))
                        candidates.append(view)
                    found['candidates']=candidates
                    try:
                        memory=SourceMemory(engine);report=memory.index_sources(args.project,limit=64)
                        from .raw_retrieval import discover_raw,search_source_view,source_acquisition_view
                        raw,_=discover_raw(memory,args.project,plan,
                            roles=None if include_assistant else ('user','document'))
                        found['sources']=[search_source_view(c) for c in raw[:5]]
                        acquisitions={}
                        for source in found['sources']:
                            identity=(args.project,source['source_id'],source['version'])
                            if identity not in acquisitions:
                                acquisitions[identity]=source_acquisition_view(engine.db,*identity)
                            source['acquisition']=acquisitions[identity]
                        found['source_index_errors']=report['errors']
                        found['source_search_complete']=report['complete']
                        found['source_index_pending']=report['pending_versions']
                    except (OSError,ValueError,sqlite3.Error) as exc:
                        found['sources']=[]
                        found['source_index_errors']=[{'error':type(exc).__name__}]
                        found['source_search_complete']=False
                    found['retrieval'].update(verification_report(engine))
                    if found['retrieval'].get('verification_budget_exhausted'):
                        found['source_search_complete']=False
                    from .raw_retrieval import search_coverage
                    found['retrieval'].update(search_coverage(found,plan))
                    found['read_notice']=('优先看 retrieval.combined_query_results 核对每个子问的笔记与原文候选；'
                        'query_results 保留仅笔记的旧语义。candidate_found 只表示找到阅读线索，'
                        'unknown 表示索引或核验不完整，unresolved 仅表示返回候选中未找到；'
                        '均不证明问题已回答。按各自版本和位置读取全文、条件与例外，说明剩余未知。')
                    return found
            if args.action=='get':
                from .retrieval_budget import verification_scope, verification_report
                with verification_scope(engine):
                    from .memory_runtime import note_view
                    from .learning_work import LearningWork
                    cid=payload.get('id',payload.get('card_id'))
                    if 'card_id' in payload and payload.get('id',cid)!=payload['card_id']:
                        raise ValueError('conflicting note id and card_id')
                    result=note_view(engine,args.project,notes.get(args.project,cid,offset=payload.get('offset')))
                    try:result['signals']=LearningWork(engine).note_signals(args.project,cid)
                    except (OSError,sqlite3.Error) as exc:result['signals_error']=type(exc).__name__
                    result.setdefault('retrieval',{}).update(verification_report(engine))
                    return result
            if args.action=='source':
                sid=payload.get('id',payload.get('source_id'))
                if not isinstance(sid,str) or not sid:
                    raise ValueError('source requires id (the source_id returned by search, not a note id)')
                if 'source_id' in payload and payload.get('id',sid)!=payload['source_id']:
                    raise ValueError('conflicting source id and source_id')
                if 'version' in payload and (type(payload['version']) is not int or payload['version']<1):
                    raise ValueError('invalid source version')
                source=engine.store.source(sid,payload.get('version'))
                if source['project']!=args.project:
                    authorized=(source['project']=='shared' and any(
                        c['project']=='shared' and any(ref['source_id']==source['source_id'] and ref['version']==source['version'] for ref in c['evidence'])
                        for c in notes.evolution._authorized_catalog(args.project)))
                    if not authorized:raise ValueError('source outside project')
                if not engine.source_available(sid):raise ValueError('source unavailable')
                offset=payload.get('offset',0)
                if type(offset) is not int or offset<0:raise ValueError('invalid source offset')
                text=source['text']
                if offset>len(text) or (text and offset==len(text)):
                    raise ValueError('source offset outside text; start at 0 and follow next_offset (total_chars='+str(len(text))+')')
                end=min(len(text),offset+2000)
                from .learning_work import LearningWork
                read_observation={}
                try:
                    if source['project']==args.project:LearningWork(engine).read(args.project,args.session,source,offset,end)
                except (OSError,sqlite3.Error) as exc:
                    read_observation={'coverage_observation_error':type(exc).__name__}
                from .raw_retrieval import source_acquisition_view
                return {'source_id':source['source_id'],'version':source['version'],
                    'role':source['role'],'text':text[offset:end],'offset':offset,
                    'total_chars':len(text),'complete':offset==0 and end==len(text),
                    'next_offset':end if end<len(text) else None,
                    'acquisition':source_acquisition_view(engine.db,source['project'],
                        source['source_id'],source['version']),**read_observation}
            if args.action=='drafts':
                if set(payload)-{'limit','draft_id','offset','include_closed'}:raise ValueError('invalid draft listing fields')
                limit=payload.get('limit',20);draft_id=payload.get('draft_id')
                offset=payload.get('offset',0);include_closed=payload.get('include_closed',False)
                if type(limit) is not int or not 1<=limit<=50:raise ValueError('draft limit must be an integer from 1 to 50')
                if type(offset) is not int or not 0<=offset<=1000000:raise ValueError('draft offset must be an integer from 0 to 1000000')
                if type(include_closed) is not bool:raise ValueError('include_closed must be boolean')
                if draft_id is not None and (not isinstance(draft_id,str) or not draft_id.strip() or len(draft_id)>200):
                    raise ValueError('invalid draft id')
                from .inline_drafts import InlineDrafts
                return {'drafts':InlineDrafts(notes).list(args.project,limit=limit,draft_id=draft_id,
                    offset=offset,include_closed=include_closed)}
            if args.action=='review':
                from .review_work import ReviewWork
                event_key=payload.pop('event_key',None)
                if event_key:args.observation_event_key=event_key
                return ReviewWork(engine).run(args.project,args.session,event_key,payload)
            if args.action not in ('remember','update','amend','resolve','finish'):raise ValueError('unsupported memory action')
            event_key=payload.pop('event_key',None)
            if not isinstance(event_key,str) or not event_key:raise ValueError('stable event_key required')
            args.observation_event_key=event_key
            if args.action=='amend':
                from .note_amendments import amend
                return amend(engine,args.project,event_key,payload)
            if args.action=='finish':
                from .learning_work import LearningWork
                return LearningWork(engine).finish(args.project,args.session,event_key,payload)
            if args.action=='resolve':
                from .inline_drafts import InlineDrafts
                return InlineDrafts(notes).resolve(args.project,event_key,payload)
            return getattr(notes,args.action)(args.project,event_key,payload)
        if args.command=='usage-report':
            from .usage_report import usage_report
            return usage_report(engine,args.project,days=args.days)
        if args.command=='capture':
            if args.reply_to_event_id and (args.role!='assistant' or not args.memory_receipt_id):
                raise ValueError('native reply binding requires assistant and memory receipt')
            text=sys.stdin.read(200001)
            with engine.locked():
                receipt=engine.store.capture(args.project,args.session,args.role,args.event_id,text)
                if evolving:
                    receipt['consumption']=Evolution(engine).captured(receipt['source_id'],args.role,args.session,receipt_id=args.receipt_id)
                    from .memory_runtime import allowed
                    if args.role=='user' and not allowed(engine,args.project):
                        from .source_memory import SourceMemory
                        try:receipt['correction']=SourceMemory(engine).observe_user(receipt['source_id'])
                        except (ValueError,KeyError,sqlite3.Error) as exc:
                            receipt['correction_error']=type(exc).__name__
                    elif args.role=='assistant' and not allowed(engine,args.project):
                        from .learning_work import LearningWork
                        try:
                            receipt['observed_note_citations']=LearningWork(engine).observe_answer(receipt['source_id'],receipt['version'])
                        except (ValueError,KeyError,sqlite3.Error) as exc:
                            receipt['citation_observation_error']=type(exc).__name__
                    if args.memory_receipt_id:
                        from .memory_receipts import bind_answer,bind_user
                        binding_field='memory_receipt_binding' if args.role=='assistant' else 'memory_user_receipt_binding'
                        try:
                            bind=bind_answer if args.role=='assistant' else bind_user
                            binding_options={'reply_to_event_id':args.reply_to_event_id} if args.role=='assistant' else {}
                            receipt[binding_field]=bind(
                                engine,args.project,args.session,args.memory_receipt_id,
                                receipt['source_id'],receipt['version'],**binding_options)
                        except (ValueError,KeyError,sqlite3.Error) as exc:
                            receipt[binding_field+'_error']=type(exc).__name__

                return receipt
        if args.command=='ingest':
            with engine.locked():
                receipt=engine.store.ingest(args.project,args.path,args.source_id)
                if evolving:Evolution(engine).captured(receipt['source_id'],'document','')
                return receipt
        if args.command=='record':
            payload=json.loads(sys.stdin.read(100000))
            if not isinstance(payload,dict):raise ValueError('record input must be JSON object')
            return engine.record(args.project,args.key,args.status,args.source_id,args.source_version,payload)
        if args.command=='context':return engine.context(args.project,args.text,args.limit,args.session,budget=args.budget,current_source_id=args.current_source_id)
        if args.command=='query' and evolving:return Evolution(engine).context(args.project,args.text,args.limit)
        if args.command=='query':return engine.query(args.project,args.text,args.limit)
        if args.command=='synthesize':
            if evolving:return Evolution(engine).framework(args.project,args.text)
            from .synthesis import queue_synthesis
            return queue_synthesis(engine,args.project,args.text)
        if args.command=='source-state':return engine.source_state(args.project,args.id,args.state)
        if args.command=='source':return engine.store.source(args.id,args.version)
        if args.command=='withdraw':return engine.withdraw(args.project,args.id)
        if args.command=='sync':
            if evolving:
                from .knowledge_files import KnowledgeFiles
                return KnowledgeFiles(engine).sync()
            return engine.sync()
        if args.command in ['status','doctor']:
            if evolving:
                from .knowledge_files import KnowledgeFiles
                reconciliation=KnowledgeFiles(engine).sync()
            else:reconciliation=engine.sync()
            state=engine.store.status()
            state.update(adoption='unknown',reconciliation=reconciliation,cost=None,cost_note='subscription billing not observed')
            state['usage']=dict(engine.db.execute('SELECT COUNT(*) AS calls,SUM(input_tokens) AS input_tokens,SUM(output_tokens) AS output_tokens,SUM(elapsed_seconds) AS elapsed_seconds FROM usage').fetchone())
            state['retrieval_requests']=engine.db.execute('SELECT COUNT(*) FROM observations').fetchone()[0]
            state['delivery']='requires native host evidence; request count is not delivery or adoption'
            cfgpath=engine.home/'config.json'
            cfg=json.loads(cfgpath.read_text()) if cfgpath.is_file() else {}
            state['model_configured']=isinstance(cfg.get('model_command'),list) and bool(cfg['model_command'])
            state['host_coverage']='not inferred; validate adapters separately'
            if evolving:state['knowledge']=Evolution(engine).status()
            if evolving:
                from .views import learning_snapshot
                state['learning']=learning_snapshot(engine)
            return state
        if args.command=='reclaim':
            from . import reclaim as reclaim_module
            options={}
            if args.limit is not None:options['limit']=args.limit
            if args.vacuum_free_bytes is not None:options['vacuum_free_bytes']=args.vacuum_free_bytes
            return reclaim_module.reclaim(engine,**options)
        if args.command=='export':
            with engine.locked():return export_archive(engine,args.to)
        if args.command=='retry':
            from .memory_runtime import allowed,source_allowed
            table='knowledge_work' if evolving else 'jobs'
            row=engine.db.execute('SELECT * FROM '+table+' WHERE id=?',(args.job,)).fetchone()
            if row and (not allowed(engine,row['project']) if evolving else not source_allowed(engine,row['source_id'],row['version'])):
                raise ValueError('memory work is paused; retry cannot enable processing')
            with engine.transaction():
                if evolving:
                    n=engine.db.execute("UPDATE knowledge_work SET state='pending',attempts=0,next_at=0,error=NULL WHERE id=? AND state IN ('failed','retry','budget_wait')",(args.job,)).rowcount
                    if not n:raise ValueError('knowledge batch not retryable')
                    return {'requeued':args.job}
                n=engine.db.execute("UPDATE jobs SET state='pending',attempts=0,lease_until=0,error=NULL WHERE id=? AND state IN ('failed','retry')",(args.job,)).rowcount
            if not n:raise ValueError('job not retryable')
            return {'requeued':args.job}
        if args.command=='process':
            if evolving:return Evolution(engine).process(args.limit,args.force)
            return process_jobs(engine,args.limit)
        if args.command=='worker':
            if args.interval<.1:raise ValueError('worker interval must be >=0.1s')
            while True:
                if not evolving:engine.sync()
                result=process_jobs(engine,args.limit)
                if args.once:return result
                print(json.dumps(result),flush=True);time.sleep(args.interval)
    finally:engine.store.close()


def main():
    args=parser().parse_args()
    started=time.perf_counter()
    try:
        result=_main(args)
    except Exception as exc:
        if getattr(args,'read_only_report',False):raise
        from .operation_observation import observe_command
        observation=observe_command(args,error=exc,elapsed_ms=(time.perf_counter()-started)*1000)
        if isinstance(observation,dict) and observation.get('warning'):
            exc.tongle_observation_warning=observation['warning']
        raise
    if getattr(args,'read_only_report',False) or (isinstance(result,dict) and result.get('disabled') is True):
        return result
    reconciliation=getattr(args,'native_capture_reconciliation',None)
    if (isinstance(result,dict) and isinstance(reconciliation,dict)
            and reconciliation.get('status')!='no_pending'):
        result['native_capture_reconciliation']=reconciliation
    from .operation_observation import observe_command
    observation=observe_command(args,result=result,elapsed_ms=(time.perf_counter()-started)*1000)
    if isinstance(observation,dict) and observation.get('warning') and isinstance(result,dict):
        result['observation_warning']=observation['warning']
    return result


if __name__=='__main__':
    try:print(json.dumps(main(),ensure_ascii=False))
    except KeyboardInterrupt:sys.exit(130)
    except (ValueError,TypeError,KeyError,OSError,sqlite3.Error) as exc:
        output={'error':type(exc).__name__+': '+str(exc)[:500]}
        if getattr(exc,'tongle_observation_warning',None):
            output['observation_warning']=exc.tongle_observation_warning
        print(json.dumps(output,ensure_ascii=False));sys.exit(1)
