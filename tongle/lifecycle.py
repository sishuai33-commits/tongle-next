"""Scoped product lifecycle. Runtime data never participates in code rollback.

Only explicit host bindings are managed. Native trust/login/consumption remain
host evidence, not a flag an installer can manufacture.
"""
from __future__ import annotations

from contextlib import contextmanager, closing, nullcontext
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid

from .portability import IS_WINDOWS, lock_file

OWNER = 'tongle-next'
PORTABLE_BOOLEANS = {'knowledge_backup_include_vault', 'memory_trace_enabled',
                     'memory_automatic_context_enabled'}
PORTABLE_NUMBERS = {'knowledge_quiet_seconds', 'knowledge_daily_call_budget',
                    'knowledge_daily_token_budget', 'knowledge_max_output_tokens',
                    'knowledge_backup_interval_seconds','model_timeout_seconds'}
PORTABLE_SETTINGS = PORTABLE_BOOLEANS | PORTABLE_NUMBERS | {
    'knowledge_mode', 'knowledge_memory_projects', 'library_excluded_projects',
    'knowledge_project_topics', 'knowledge_project_names'}


def portable_settings(config):
    result={key:value for key,value in config.items() if key in PORTABLE_SETTINGS}
    validate_portable_settings(result)
    return result


def validate_portable_settings(settings):
    if not isinstance(settings,dict) or set(settings)-PORTABLE_SETTINGS:raise ValueError('unsafe backup settings')
    for key in PORTABLE_BOOLEANS & settings.keys():
        if type(settings[key]) is not bool:raise ValueError('invalid portable boolean setting')
    for key in PORTABLE_NUMBERS & settings.keys():
        value=settings[key]
        if type(value) not in (int,float) or not math.isfinite(value) or not 0<=value<=1_000_000_000:
            raise ValueError('invalid portable numeric setting')


def restore_metadata(home, registry=None, local_models=False, source_locations=None):
    """Preserve known sources without activating old machine paths."""
    home=Path(home);required=['host_adapters','credentials']
    if registry is not None:
        registry=json.loads(json.dumps(validate_registry(registry)))
        for source in registry['sources']:source.update(enabled=False,needs_rebind=True)
        atomic_json(home/'maintenance/sources.json',registry)
        if registry['sources']:required.insert(0,'source_paths')
    if local_models:required.append('local_models')
    if source_locations:
        atomic_json(home/'restore-source-locations.json',{'format':1,'policy':'historical_hints_only','sources':source_locations})
        if 'source_paths' not in required:required.insert(0,'source_paths')
    status={'required':required,'host_activation':'unverified','credentials':'not_restored',
            'source_bindings':'detached_archived_originals',
            'local_models':'lexical_fallback_until_rebound' if local_models else 'not_configured',
            'instructions':'Run setup, re-register source paths, install each host, and verify actual host events. Rebind model credentials/settings separately.'}
    atomic_json(home/'restore-rebind.json',status)
    return status


def safe_path(path):
    path=Path(path).absolute()
    # macOS exposes these OS-owned aliases to its temporary-file APIs. Resolve
    # only that exact alias, never an arbitrary symlink inside the chosen path.
    if sys.platform=='darwin':
        for alias,canonical in (('/var','/private/var'),('/tmp','/private/tmp'),('/etc','/private/etc')):
            prefix=Path(alias)
            if path.is_relative_to(prefix) and prefix.is_symlink() and prefix.resolve()==Path(canonical):
                path=Path(canonical)/path.relative_to(prefix)
                break
    if any(p.is_symlink() for p in (path,*path.parents)):
        raise ValueError('lifecycle path contains a symlink')
    return path


def read_json(path, default=None):
    path=safe_path(path)
    if not path.exists():return {} if default is None else default
    if path.stat().st_size>2_000_000:raise ValueError('lifecycle metadata exceeds bound')
    data=json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data,dict):raise ValueError('lifecycle metadata must be an object')
    return data


def atomic_json(path, data):
    atomic_bytes(path,(json.dumps(data,ensure_ascii=False,indent=2)+'\n').encode())


def atomic_bytes(path, data, mode=0o600):
    path=safe_path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        with temp.open('xb') as out:
            os.chmod(temp,mode);out.write(data);out.flush();os.fsync(out.fileno())
        os.replace(temp,path)
    finally:temp.unlink(missing_ok=True)


@contextmanager
def locked(home):
    home=safe_path(home)
    if not home.is_dir():raise ValueError('run setup first')
    with safe_path(home/'lifecycle.lock').open('a') as handle:
        lock_file(handle)
        yield


def _installer(root):
    spec=importlib.util.spec_from_file_location('tongle_scoped_install',Path(root)/'adapters/install.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


# Kept self-contained: this survives deletion of an obsolete code checkout.
DISPATCH = '''#!/usr/bin/env python3
# tongle-owned stable dispatcher v1
import hashlib, json, os, subprocess, sys, time
from pathlib import Path
home=Path(__file__).resolve().parents[1]
def ensure_maintenance(root, python):
 # Existing maintenance core only; asynchronous, singleton and throttled.
 # Windows Pi starts maintenance via its launcher; Windows Codex install is not enabled.
 if os.name=='nt':return
 import fcntl
 directory=home/'maintenance'
 try:
  if directory.is_symlink():return
  directory.mkdir(exist_ok=True)
  if (directory/'disabled.json').exists():return
  lockpath=directory/'ensure.lock';stamp=directory/'ensure-attempt.json'
  if lockpath.is_symlink() or stamp.is_symlink():return
  with lockpath.open('a') as lock:
   try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
   except BlockingIOError:return
   try:last=json.loads(stamp.read_text())
   except (OSError,ValueError):last={}
   code_hash=hashlib.sha256((root/'scripts/maintain.py').read_bytes()).hexdigest()
   same=last.get('root')==str(root) and last.get('code_hash')==code_hash
   if same and time.time()-last.get('at',0)<60:return
   attempts=last.get('attempts',0) if same else 0
   runtime_lock=directory/'lock'
   if runtime_lock.is_file() and not runtime_lock.is_symlink():
    with runtime_lock.open('rb') as active:
     try:fcntl.flock(active,fcntl.LOCK_EX|fcntl.LOCK_NB)
     except BlockingIOError:
      stamp.write_text(json.dumps({'at':time.time(),'root':str(root),'code_hash':code_hash,'attempts':0,'state':'active_lock_observed'}))
      return
   if not isinstance(attempts,int) or attempts>=3:return
   logfile=directory/'ensure.log'
   if logfile.is_symlink():return
   with logfile.open('wb') as log:
    subprocess.Popen([python,str(root/'scripts/maintain.py'),'--home',str(home),'start'],stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True,close_fds=True)
   temporary=stamp.with_name(stamp.name+'.'+str(os.getpid())+'.tmp')
   with temporary.open('x') as out:
    out.write(json.dumps({'at':time.time(),'root':str(root),'code_hash':code_hash,'attempts':attempts+1,'state':'attempted_not_verified'}))
   os.replace(temporary,stamp)
 except (OSError,ValueError,TypeError):pass
try:
 state=json.loads((home/'instance.json').read_text(encoding='utf-8'))
 if state.get('owner')!='tongle-next':raise ValueError('invalid instance owner')
 if not state.get('enabled',False):
  if len(sys.argv)>1 and sys.argv[1]=='hook':sys.exit(0)
  raise ValueError('tongle instance is disabled; run setup to enable it')
 cfg=json.loads((home/'launcher.json').read_text(encoding='utf-8'))
 root=Path(cfg['root']);python=cfg['python'];kind=sys.argv[1]
 if not root.is_absolute() or not Path(python).is_absolute():raise ValueError('invalid runtime binding')
 env=dict(os.environ,TONGLE_NEXT_HOME=str(home),TONGLE_NEXT_ROOT=str(root),TONGLE_NEXT_PYTHON=python,TONGLE_NEXT_DEFAULT_PROJECT=cfg['project'])
 if kind=='hook':
  ensure_maintenance(root,python)
  args=[python,str(root/'adapters/hook.py'),*sys.argv[2:]]
 elif kind=='pi':args=[python,str(root/'scripts/tongle.py'),'pi','--home',str(home),*sys.argv[2:]]
 else:raise ValueError('invalid entry mode')
 if os.name=='nt':
  env.update(PYTHONUTF8='1',PYTHONIOENCODING='utf-8')
  sys.exit(subprocess.run(args,env=env).returncode)
 os.execve(python,args,env)
except (OSError,ValueError,KeyError,IndexError) as exc:
 print(json.dumps({'error':str(exc)[:200]}),file=sys.stderr);sys.exit(1)
'''


def setup_ownership(home):
    home=Path(home);state=read_json(home/'instance.json')
    if state and state.get('owner')!=OWNER:raise ValueError('foreign instance metadata')
    for rel,expected in state.get('owned_files',{}).items():
        if rel not in ('bin/tongle-entry.py','bin/tongle-pi'):raise ValueError('unknown owned entry metadata')
        path=safe_path(home/rel)
        if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest()!=expected:
            raise ValueError('managed entry has local changes; reconcile before setup: '+rel)
    if not state:
        for rel,marker in (('bin/tongle-entry.py','# tongle-owned stable dispatcher v1'),
                           ('bin/tongle-pi','# tongle-owned launcher')):
            path=safe_path(home/rel)
            if path.is_file() and marker not in path.read_text():raise ValueError('foreign entry exists: '+rel)
    return state


def initialize_entry(home, already_locked=False):
    """Called after setup, preserving existing explicit host bindings."""
    home=Path(home)
    with (nullcontext() if already_locked else locked(home)):
        state=read_json(home/'instance.json')
        if state and state.get('owner')!=OWNER:raise ValueError('foreign instance metadata')
        state.update(owner=OWNER,format=1,enabled=True,uninstalled=False)
        state.setdefault('bindings',[])
        bootstrap=str(Path(getattr(sys,'_base_executable',sys.executable)).resolve())
        state['bootstrap_python']=bootstrap
        entry_paths = ['bin/tongle-entry.py']
        if not IS_WINDOWS:
            entry_paths.append('bin/tongle-pi')
        for rel in entry_paths:
            path=safe_path(home/rel)
            if path.is_file():atomic_bytes(path.with_name(path.name+'.before-'+str(time.time_ns())),path.read_bytes())
        atomic_bytes(home/'bin/tongle-entry.py',DISPATCH.encode(),0o700)
        state['owned_files']={'bin/tongle-entry.py':hashlib.sha256(DISPATCH.encode()).hexdigest()}
        if not IS_WINDOWS:
            launcher=('#!/bin/sh\n# tongle-owned launcher\nexec '+shlex.join(
                [bootstrap,str(home/'bin/tongle-entry.py'),'pi'])+' "$@"\n')
            atomic_bytes(home/'bin/tongle-pi',launcher.encode(),0o700)
            state['owned_files']['bin/tongle-pi']=hashlib.sha256(launcher.encode()).hexdigest()
        atomic_json(home/'instance.json',state)
        safe_path(home/'maintenance/disabled.json').unlink(missing_ok=True)
    return {'entry':str(home/'bin/tongle-entry.py'),
            'existing_host_bindings':'run install for each existing host configuration; unregistered connections are not migrated'}


def preflight(home, root, host=None, config_dir=None):
    """Read-only probes. A successful config inspection never proves native use."""
    home=safe_path(home);root=Path(root)
    launcher=read_json(home/'launcher.json')
    active_root=Path(launcher.get('root',str(root)))
    checks=[]
    checks.append({'name':'python','ok':sys.version_info>=(3,11),'version':sys.version.split()[0]})
    checks.append({'name':'runtime','ok':active_root.is_absolute() and all((active_root/p).is_file() for p in
                  ('scripts/tongle.py','scripts/maintain.py','adapters/hook.py','adapters/pi.ts')),
                  'path':str(active_root)})
    if launcher:
        checks.append({'name':'bound_python','ok':Path(launcher.get('python','')).is_file()})
    dbpath=home/'state.sqlite3';database_ok=False;vault=None
    if dbpath.is_file():
        try:
            wal=dbpath.with_name(dbpath.name+'-wal')
            suffix='?mode=ro' if wal.exists() and wal.stat().st_size else '?mode=ro&immutable=1'
            with closing(sqlite3.connect(dbpath.as_uri()+suffix,uri=True)) as db:
                database_ok=db.execute('PRAGMA quick_check').fetchone()[0]=='ok'
                row=db.execute("SELECT value FROM meta WHERE key='vault'").fetchone()
                vault=row[0] if row else None
        except sqlite3.Error:pass
    checks.append({'name':'database','ok':database_ok})
    checks.append({'name':'vault','ok':bool(vault and Path(vault).is_dir()),'path':vault})
    config=read_json(home/'config.json');state=read_json(home/'instance.json')
    from .memory_policy import uses_knowledge_store
    try:uses_knowledge_store(config);valid_config=True
    except (TypeError,ValueError):valid_config=False
    checks.append({'name':'config','ok':valid_config})
    if host:checks.append({'name':host+'_executable','ok':bool(shutil.which(host))})
    bindings=[]
    targets=list(state.get('bindings',[]))
    if host=='codex' and config_dir:
        target=str(safe_path(Path(config_dir)/'hooks.json'))
        if not any(b['target']==target for b in targets):targets.append({'host':'codex','target':target,'unregistered':True})
    for binding in targets:
        target=Path(binding['target']);current=read_json(target)
        installer=_installer(root)
        handlers=[h for groups in current.get('hooks',{}).values() if isinstance(groups,list)
                  for group in groups if isinstance(group,dict)
                  for h in group.get('hooks',[]) if isinstance(h,dict)]
        own=[h for h in handlers if installer._is_owned_handler(h,entrypoint=str(home/'bin/tongle-entry.py'))]
        bindings.append({'host':binding['host'],'target':str(target),'configured':bool(own),
                         'registered':not binding.get('unregistered',False),
                         'native_activation':'unverified','consumption':'unverified'})
    restore=read_json(home/'restore-rebind.json')
    return {'ok':all(c['ok'] for c in checks),'checks':checks,
            'instance':{'initialized':database_ok,'enabled':state.get('enabled') if state else None,
                        'active_runtime':launcher.get('root'),
                        'managed_bindings':bindings,'unmanaged_connections':'not_discovered'},
            'rebind':restore or {'required':[]},
            'maintenance_startup':read_json(home/'maintenance/ensure-attempt.json'),
            'native_activation':'unverified','credentials':'not_inspected',
            'verification':'local_preflight_only'}


def install_host(home, root, host, config_dir=None, write=False):
    from argparse import Namespace
    home=safe_path(home);root=Path(root)
    cfg=read_json(home/'launcher.json');state=read_json(home/'instance.json')
    if state.get('owner')!=OWNER or not (home/'bin/tongle-entry.py').is_file():
        raise ValueError('run setup to create the stable instance entry first')
    if host=='pi':
        result={'host':'pi','native_activation':'unverified','dry_run':not write}
        if IS_WINDOWS:
            result['launch_argv']=[state['bootstrap_python'],str(home/'bin/tongle-entry.py'),'pi']
        else:
            result['launch']=str(home/'bin/tongle-pi')
        return result
    if host!='codex' or not config_dir:raise ValueError('Codex install requires --config-dir')
    if IS_WINDOWS:raise ValueError('Codex hooks installation is not supported on Windows')
    installer=_installer(root)
    config_dir=Path(config_dir).expanduser()
    if not config_dir.is_absolute():raise ValueError('--config-dir must be absolute')
    config_dir=safe_path(config_dir)
    target=safe_path(config_dir/'hooks.json')
    args=Namespace(home=str(home),python=state.get('bootstrap_python',str(Path(getattr(sys,'_base_executable',sys.executable)).resolve())),root=cfg['root'],project=cfg['project'],
                   entrypoint=str(home/'bin/tongle-entry.py'))
    before=target.read_bytes() if target.exists() else None
    existing=read_json(target);fragment=installer.codex_fragment(args)
    merged=installer.merge_codex(existing,fragment,entrypoint=args.entrypoint)
    plan={'host':host,'target':str(target),'entrypoint':args.entrypoint,
          'changed':merged!=existing,'dry_run':not write,'native_activation':'unverified',
          'next':'Trust/enable this explicit configuration in Codex, then verify native user and assistant events.'}
    plan['unmanaged_legacy_handlers']=sum(1 for groups in merged.get('hooks',{}).values() if isinstance(groups,list)
        for group in groups if isinstance(group,dict) for h in group.get('hooks',[])
        if installer._is_owned_handler(h))
    if not write:return plan
    with locked(home):
        if (target.read_bytes() if target.exists() else None)!=before:raise ValueError('host configuration changed; retry')
        if before is not None and merged!=existing:
            atomic_bytes(target.with_name(target.name+'.before-'+str(time.time_ns())),before)
        if merged!=existing:atomic_json(target,merged)
        state=read_json(home/'instance.json')
        bindings=[b for b in state.get('bindings',[]) if b.get('target')!=str(target)]
        bindings.append({'host':host,'target':str(target),'entrypoint':args.entrypoint})
        state['bindings']=bindings;atomic_json(home/'instance.json',state)
    return plan


def registered_sources(home):
    return read_json(Path(home)/'maintenance/sources.json',{'sources':[]})


def validate_registry(registry):
    if not isinstance(registry,dict) or set(registry)-{'sources'} or not isinstance(registry.get('sources'),list) or len(registry['sources'])>10000:
        raise ValueError('invalid source registry')
    seen=set()
    for source in registry['sources']:
        if not isinstance(source,dict) or set(source)-{'project','source_id','path','enabled','needs_rebind'}:raise ValueError('invalid source registration')
        if not all(isinstance(source.get(k),str) and 0<len(source[k])<=4096 and '\x00' not in source[k] for k in ('project','source_id','path')):
            raise ValueError('invalid source registration fields')
        if not Path(source['path']).is_absolute():raise ValueError('source path must be absolute')
        if any(k in source and not isinstance(source[k],bool) for k in ('enabled','needs_rebind')):raise ValueError('invalid source flags')
        key=(source['project'],source['source_id'])
        if key in seen:raise ValueError('duplicate source identity')
        seen.add(key)
    return registry


def register_source(home, root, project, path, source_id, cwd=None, write=False):
    home=safe_path(home);path=safe_path(path)
    if not path.is_file() or path.suffix.lower() not in ('.txt','.md','.docx') or path.stat().st_size>5*1024*1024:
        raise ValueError('source must be an existing Markdown, text or DOCX file within 5MB')
    entry={'project':project,'source_id':source_id,'path':str(path),'enabled':True,'needs_rebind':False}
    validate_registry({'sources':[entry]})
    project_config=safe_path(Path(cwd)/'.tongle-next.json') if cwd else None
    existing_project=read_json(project_config) if project_config else {}
    if existing_project.get('project') not in (None,project):raise ValueError('cwd is already bound to another project')
    plan={'registered':entry,'project_config':str(project_config) if project_config else None,'dry_run':not write}
    if not write:return plan
    with locked(home):
        registry=validate_registry(registered_sources(home))
        with closing(sqlite3.connect((home/'state.sqlite3').as_uri()+'?mode=ro',uri=True)) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='source_acquisitions'").fetchone():
                if db.execute('SELECT 1 FROM source_acquisitions WHERE source_id=? LIMIT 1',(source_id,)).fetchone():
                    raise ValueError('acquired source requires acquire; mutable registration would bypass coverage protection')
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_status'").fetchone():
                row=db.execute('SELECT state FROM source_status WHERE source_id=?',(source_id,)).fetchone()
                if row and row[0]=='revoked':raise ValueError('revoked source cannot be re-registered; choose an explicitly new source identity')
        # Ingest first. A failed parse is never installed as an apparently working registration.
        env=dict(os.environ,PYTHONPATH=str(root))
        result=subprocess.run([sys.executable,'-m','tongle','--home',str(home),'ingest',
            '--project',project,'--path',str(path),'--source-id',source_id],cwd=root,env=env,
            capture_output=True,text=True,timeout=60)
        if result.returncode:raise ValueError('source ingestion failed; registry unchanged: '+result.stderr[:160])
        receipt=json.loads(result.stdout)
        if receipt.get('source_id')!=source_id:raise ValueError('source ingestion returned a different identity')
        available=subprocess.run([sys.executable,'-m','tongle','--home',str(home),'source-state',
            '--project',project,'--id',source_id,'--state','available'],cwd=root,env=env,
            capture_output=True,text=True,timeout=30)
        if available.returncode:raise ValueError('source imported but availability was not acknowledged; registry unchanged')
        registry['sources']=[s for s in registry['sources'] if (s['project'],s['source_id'])!=(project,source_id)]+[entry]
        atomic_json(home/'maintenance/sources.json',registry)
        retries=read_json(home/'maintenance/retries.json')
        retry_key='source:'+hashlib.sha256(json.dumps([project,source_id],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        if retry_key in retries:
            retries.pop(retry_key);atomic_json(home/'maintenance/retries.json',retries)
        if project_config:
            current=read_json(project_config)
            if current!=existing_project:raise ValueError('project configuration changed; registration retained, retry cwd binding')
            if current!=dict(current,project=project):
                if project_config.exists():atomic_bytes(project_config.with_name(project_config.name+'.before-'+str(time.time_ns())),project_config.read_bytes())
                atomic_json(project_config,dict(current,project=project))
        plan['ingest']={key:receipt[key] for key in ('source_id','version') if key in receipt}
        restore=read_json(home/'restore-rebind.json')
        if restore and all(not s.get('needs_rebind') for s in registry['sources']):
            restore['required']=[x for x in restore.get('required',[]) if x!='source_paths']
            atomic_json(home/'restore-rebind.json',restore)
    return plan


def _runtime_probe(root, home, python=None):
    """Candidate opens a database snapshot, never the live instance or its Vault."""
    root=safe_path(root)
    # The stable dispatcher lives in the instance. A previous release does not
    # need the newer lifecycle manager to serve capture/context or run Pi.
    required=('tongle/__main__.py','tongle/engine.py','tongle/memory_policy.py',
              'scripts/tongle.py','scripts/maintain.py','adapters/common.py','adapters/hook.py','adapters/pi.ts')
    if not all((root/p).is_file() for p in required):raise ValueError('candidate is not a compatible product runtime')
    with tempfile.TemporaryDirectory(prefix='tongle-upgrade-check-') as temp:
        copy=Path(temp).resolve()/'home';copy.mkdir()
        with closing(sqlite3.connect((Path(home)/'state.sqlite3').as_uri()+'?mode=ro',uri=True)) as source:
            with closing(sqlite3.connect(copy/'state.sqlite3')) as dest:
                source.backup(dest)
                dest.execute("UPDATE meta SET value=? WHERE key='vault'",(str(Path(temp)/'vault'),))
                dest.commit()
        config=read_json(Path(home)/'config.json');config.pop('model_command',None)
        atomic_json(copy/'config.json',config)
        script="from tongle.engine import Engine;from tongle.memory_policy import read_home_config,uses_knowledge_store;import sys;e=Engine(sys.argv[1]);uses_knowledge_store(read_home_config(e.home));assert e.db.execute('PRAGMA quick_check').fetchone()[0]=='ok';e.store.close()"
        result=subprocess.run([python or sys.executable,'-c',script,str(copy)],cwd=root,
                              env=dict(os.environ,PYTHONPATH=str(root)),capture_output=True,text=True,timeout=30)
        if result.returncode:raise ValueError('candidate cannot open the current database snapshot: '+result.stderr[-180:])
        cli=subprocess.run([python or sys.executable,'-m','tongle','--help'],cwd=root,
                           env=dict(os.environ,PYTHONPATH=str(root)),capture_output=True,text=True,timeout=15)
        if cli.returncode or any(command not in cli.stdout for command in ('capture','context','ingest')):
            raise ValueError('candidate core CLI is incompatible with stable adapters')
    return {'database_snapshot':'compatible','core_cli':'capture_context_ingest_available',
            'lifecycle_manager':'present' if (root/'tongle/lifecycle.py').is_file() else 'managed_by_current_release',
            'native_host':'unverified',
            'legacy_limits':[] if (root/'tongle/lifecycle.py').is_file() else [
                'Use current release scripts/tongle.py for subsequent lifecycle operations.',
                'Legacy adapters require explicit TONGLE_NEXT_PROJECT or cwd .tongle-next.json; DEFAULT_PROJECT is not supported.',
                'Legacy manual maintenance commands require explicit --python and --root.']}


def switch_runtime(home, root=None, rollback=False, write=False):
    home=safe_path(home)
    with locked(home):
        state=read_json(home/'instance.json');cfg=read_json(home/'launcher.json')
        if state.get('owner')!=OWNER or not cfg:raise ValueError('run setup before managed upgrades')
        history=cfg.get('runtime_history',state.get('runtime_history',[]))
        if rollback:
            if not history:raise ValueError('no previous runtime to roll back to')
            target=dict(history[-1]['launcher'],python=cfg['python']);root=Path(target['root'])
        else:
            if root is None:raise ValueError('upgrade requires --candidate-root')
            root=safe_path(root);target=dict(cfg,root=str(root))
        proof=_runtime_probe(root,home,target['python'])
        plan={'action':'rollback' if rollback else 'upgrade','from':cfg['root'],'to':str(root),
              'dry_run':not write,'database_preserved':True,'probe':proof,
              'scope':'managed stable entries only; unregistered host connections require install'}
        if not write or target==cfg:return plan
        # Snapshot code binding/config as evidence; deliberately never restore database bytes.
        stamp=str(time.time_ns());backup=home/'lifecycle-backups'/stamp
        backup.mkdir(parents=True)
        atomic_json(backup/'launcher.json',cfg)
        atomic_json(backup/'config.json',read_json(home/'config.json'))
        previous={k:v for k,v in cfg.items() if k not in ('runtime_history','last_switch')}
        target['runtime_history']=history[:-1] if rollback else history+[{'launcher':previous,'backup':str(backup)}]
        target['last_switch']={'from':cfg['root'],'to':str(root),'backup':str(backup),'at':time.time()}
        # Pointer AND rollback stack share one atomic publication. A crash cannot
        # consume a rollback entry while leaving the previous pointer active.
        atomic_json(home/'launcher.json',target)
        plan['backup']=str(backup)
        plan['restart_required']=['active Pi sessions','existing maintenance loop']
        # An old run-loop is asked to stop after its bounded cycle; the next Pi start uses the new runtime.
        maintenance=read_json(home/'maintenance/state.json')
        if maintenance.get('running') and maintenance.get('token'):
            atomic_json(home/'maintenance/stop.json',{'token':maintenance['token']})
        return plan


def disable(home, root, uninstall=False, write=False):
    home=safe_path(home);state=read_json(home/'instance.json')
    if state.get('owner')!=OWNER:raise ValueError('run setup before managing this instance')
    changes=[];installer=_installer(root)
    for binding in state.get('bindings',[]):
        target=safe_path(binding['target']);old=read_json(target)
        updated=installer.unmerge_codex(old,entrypoint=str(home/'bin/tongle-entry.py'))
        changes.append((target,old,updated))
    plan={'disabled':True,'uninstalled':uninstall,'dry_run':not write,
          'preserved':['database','vault','sources','config','credentials','local_models','rollback_history'],
          'host_configs':[str(x[0]) for x in changes],
          'external_schedulers':'disabled cycles become no-ops; unregistered scheduler configuration is preserved'}
    if not write:return plan
    with locked(home):
        # Fail closed before removing hooks; a concurrent already-loaded hook uses this same dispatcher.
        state=read_json(home/'instance.json');state.update(enabled=False,uninstalled=uninstall)
        atomic_json(home/'instance.json',state)
        atomic_json(home/'maintenance/disabled.json',{'owner':OWNER,'disabled_at':time.time()})
        for target,old,updated in changes:
            if read_json(target)!=old:raise ValueError('host configuration changed; instance disabled, retry cleanup')
            if updated!=old:
                atomic_bytes(target.with_name(target.name+'.before-'+str(time.time_ns())),target.read_bytes())
                atomic_json(target,updated)
        if uninstall:
            launch=safe_path(home/'bin/tongle-pi')
            if launch.is_file():
                expected=state.get('owned_files',{}).get('bin/tongle-pi')
                if expected and hashlib.sha256(launch.read_bytes()).hexdigest()==expected:launch.unlink()
                else:plan['preserved_modified_launcher']=str(launch)
        # Dispatcher is a disabled tombstone for stale host configuration, not removed underneath it.
    return plan
