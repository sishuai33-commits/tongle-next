"""tongle command line; machine-readable success/error replies."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
import time
from .engine import Engine
from .worker import process_jobs
from .archive import export_archive,restore_archive


def parser():
    p=argparse.ArgumentParser(prog='tongle')
    p.add_argument('--home',required=True,type=Path)
    sub=p.add_subparsers(dest='command',required=True)
    x=sub.add_parser('init');x.add_argument('--vault',required=True,type=Path);x.add_argument('--evolving',action='store_true')
    x=sub.add_parser('capture')
    for field in ['project','session','event-id']:x.add_argument('--'+field,required=True)
    x.add_argument('--role',choices=['user','assistant'],required=True)
    x.add_argument('--receipt-id',default=None)
    x=sub.add_parser('ingest');x.add_argument('--project',required=True);x.add_argument('--path',required=True);x.add_argument('--source-id')
    x=sub.add_parser('record')
    for field in ['project','key','source-id']:x.add_argument('--'+field,required=True)
    x.add_argument('--source-version',required=True,type=int);x.add_argument('--status',required=True,choices=['candidate','confirmed'])
    for cmd in ['query','context']:
        x=sub.add_parser(cmd);x.add_argument('--project',required=True);x.add_argument('--text',required=True);x.add_argument('--limit',type=int,default=8)
        if cmd=='context':x.add_argument('--session',default='')
    x=sub.add_parser('synthesize');x.add_argument('--project',required=True);x.add_argument('--text',default='整理共同主题、适用条件、限制与分歧')
    x=sub.add_parser('source-state');x.add_argument('--project',required=True);x.add_argument('--id',required=True);x.add_argument('--state',required=True,choices=['available','missing','revoked'])
    x=sub.add_parser('source');x.add_argument('--id',required=True);x.add_argument('--version',type=int)
    x=sub.add_parser('withdraw');x.add_argument('--project',required=True);x.add_argument('--id',required=True)
    sub.add_parser('sync');sub.add_parser('status');sub.add_parser('doctor')
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


def main():
    args=parser().parse_args()
    if args.command=='restore':return restore_archive(args.home,args.archive,args.vault)
    if args.command!='init' and not (args.home/'state.sqlite3').is_file():raise ValueError('tongle home not initialized')
    if (args.home/'state.sqlite3').is_symlink():raise ValueError('database symlink rejected')
    engine=Engine(args.home)
    try:
        from .evolution import enabled,Evolution
        evolving=enabled(engine)
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
        if args.command=='capture':
            text=sys.stdin.read(200001)
            with engine.locked():
                receipt=engine.store.capture(args.project,args.session,args.role,args.event_id,text)
                if evolving:
                    receipt['consumption']=Evolution(engine).captured(receipt['source_id'],args.role,args.session,receipt_id=args.receipt_id)
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
        if args.command=='context':return engine.context(args.project,args.text,args.limit,args.session)
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
            cfg=json.loads(cfgpath.read_text(encoding='utf-8')) if cfgpath.is_file() else {}
            state['model_configured']=isinstance(cfg.get('model_command'),list) and bool(cfg['model_command'])
            state['host_coverage']='not inferred; validate adapters separately'
            if evolving:state['knowledge']=Evolution(engine).status()
            if evolving:
                from .views import learning_snapshot
                state['learning']=learning_snapshot(engine)
            return state
        if args.command=='export':
            with engine.locked():return export_archive(engine,args.to)
        if args.command=='retry':
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


if __name__=='__main__':
    from .portability import configure_stdio
    configure_stdio()
    try:print(json.dumps(main(),ensure_ascii=False))
    except KeyboardInterrupt:sys.exit(130)
    except (ValueError,TypeError,KeyError,OSError,sqlite3.Error) as exc:
        print(json.dumps({'error':type(exc).__name__+': '+str(exc)[:500]},ensure_ascii=False));sys.exit(1)
