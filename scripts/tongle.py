#!/usr/bin/env python3
"""One-time scoped setup and a daily Pi launcher. No global installation."""
import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
from maintain import atomic_json
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from tongle.portability import IS_WINDOWS


def pi_command():
    pi = shutil.which('pi')
    if not pi:
        raise ValueError('Pi executable not found')
    if not IS_WINDOWS or Path(pi).suffix.lower() == '.exe':
        return [pi]

    # npm on Windows exposes a cmd shim. Use Node and the installed package's
    # bin entry directly so that Pi arguments never pass through cmd.exe.
    prefix = Path(pi).parent
    node = prefix/'node.exe'
    if not node.is_file():
        node = shutil.which('node.exe')
    if not node:
        raise ValueError('Node.js executable not found for Pi')
    for package in ('@earendil-works/pi-coding-agent', '@mariozechner/pi-coding-agent'):
        root = (prefix/'node_modules'/package).resolve()
        manifest = root/'package.json'
        if not manifest.is_file():
            continue
        metadata = json.loads(manifest.read_text(encoding='utf-8'))
        bin_entries = metadata.get('bin')
        if metadata.get('name') != package or not isinstance(bin_entries, dict):
            continue
        entry = bin_entries.get('pi')
        if isinstance(entry, str) and entry:
            script = (root/entry).resolve()
            if script.is_relative_to(root) and script.suffix == '.js' and script.is_file():
                return [str(node), str(script)]
    raise ValueError('Pi npm package not found beside its launcher; install Pi with npm')


def main():
    parser=argparse.ArgumentParser(description=__doc__,allow_abbrev=False)
    parser.add_argument('command',choices=['setup','pi','status','stop','preflight','install','register','sources','acquire','coverage','upgrade','rollback','disable','uninstall'])
    parser.add_argument('--home',required=True,type=Path)
    parser.add_argument('--vault',type=Path)
    parser.add_argument('--project')
    parser.add_argument('--enable-model',action='store_true')
    parser.add_argument('--evolving',action='store_true',help='opt a NEW instance into the historical background compiler')
    parser.add_argument('--provider');parser.add_argument('--model')
    parser.add_argument('--write',action='store_true')
    parser.add_argument('--host',choices=['pi','codex'])
    parser.add_argument('--config-dir',type=Path)
    parser.add_argument('--path',type=Path)
    parser.add_argument('--source-id')
    parser.add_argument('--cwd',type=Path)
    parser.add_argument('--candidate-root',type=Path)
    parser.add_argument('--acquisition',type=Path,help='acquire: bounded JSON reading receipt')
    parser.add_argument('--paths-file',type=Path,help='coverage: JSON list of explicit absolute paths')
    args,extra=parser.parse_known_args()
    home=args.home.expanduser().resolve()
    cfgpath=home/'launcher.json'
    if args.command not in ('setup','pi','status','stop'):
        if extra:raise ValueError('unrecognized lifecycle arguments')
        from tongle import lifecycle
        if args.command=='preflight':result=lifecycle.preflight(home,ROOT,args.host,args.config_dir)
        elif args.command=='install':
            if not args.host:raise ValueError('install requires --host pi or codex')
            result=lifecycle.install_host(home,ROOT,args.host,args.config_dir,args.write)
        elif args.command=='register':
            if not args.project or not args.path or not args.source_id:raise ValueError('register requires --project, --path and --source-id')
            result=lifecycle.register_source(home,ROOT,args.project,args.path,args.source_id,args.cwd,args.write)
        elif args.command=='sources':result=lifecycle.registered_sources(home)
        elif args.command=='acquire':
            if not args.project or not args.path or not args.source_id or not args.acquisition:
                raise ValueError('acquire requires --project, --path, --source-id and --acquisition')
            from tongle.source_acquisition import acquire
            metadata=lifecycle.read_json(args.acquisition)
            result=acquire(home,args.project,args.path,args.source_id,metadata,args.write)
        elif args.command=='coverage':
            if not args.project or not args.paths_file:raise ValueError('coverage requires --project and --paths-file')
            if args.write:raise ValueError('coverage is read-only')
            from tongle.asset_coverage import inspect_assets
            manifest=lifecycle.safe_path(args.paths_file)
            if manifest.stat().st_size>300000:raise ValueError('paths manifest exceeds bound')
            paths=json.loads(manifest.read_text())
            if not isinstance(paths,list) or any(not isinstance(p,str) or not Path(p).is_absolute() for p in paths):
                raise ValueError('paths manifest must be an array of absolute paths')
            result=inspect_assets(home,args.project,paths)
        elif args.command in ('upgrade','rollback'):
            result=lifecycle.switch_runtime(home,args.candidate_root,args.command=='rollback',args.write)
        else:result=lifecycle.disable(home,ROOT,args.command=='uninstall',args.write)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        if args.command=='preflight' and not result['ok']:sys.exit(1)
        return
    if args.command=='setup':
        if extra:raise ValueError('unrecognized setup arguments')
        if not args.vault or not args.project:raise ValueError('setup requires --vault and --project')
        vault=args.vault.expanduser().resolve()
        if not args.project.strip():raise ValueError('project must be non-empty')
        from tongle.lifecycle import setup_ownership, initialize_entry, locked
        prior_instance=setup_ownership(home)
        configpath=home/'config.json'
        for path in (cfgpath,configpath,home/'state.sqlite3',home/'bin',home/'bin/tongle-pi'):
            if path.is_symlink():raise ValueError('setup target symlink rejected')
        previous=configpath.read_bytes() if configpath.exists() else None
        previous_launcher=cfgpath.read_bytes() if cfgpath.exists() else None
        settings=json.loads(previous) if previous is not None else {}
        sys.path.insert(0,str(ROOT))
        from tongle.memory_policy import uses_knowledge_store
        uses_knowledge_store(settings)  # Validate before changing the installation.
        existing=(home/'state.sqlite3').exists() or previous is not None
        mode=settings.get('knowledge_mode','legacy') if existing else ('evolving' if args.evolving else 'memory')
        if existing and args.evolving and mode!='evolving':
            raise ValueError('setup preserves the existing mode; use the reviewed migration workflow to change it')
        if args.enable_model and mode!='evolving':
            raise ValueError('--enable-model requires --evolving on a new instance, or an existing evolving instance')
        modelcfg=dict(settings)
        if not existing:
            modelcfg.update(knowledge_mode=mode,knowledge_quiet_seconds=300,knowledge_daily_call_budget=40,
                            knowledge_daily_token_budget=300000,knowledge_max_output_tokens=4096)
        if args.enable_model:
            pi=shutil.which('pi')
            if not pi:raise ValueError('Pi executable not found; no installation changed')
            provider=args.provider or 'zhipu';model=args.model or 'glm-5.3-flash'
            modelcfg.update(model_command=[sys.executable,str(ROOT/'scripts/pi_model.py'),'--pi',pi,'--provider',provider,'--model',model],
                            model_timeout_seconds=200,model_name=f'pi/{provider}/{model}')
        plan={'home':str(home),'vault':str(vault),'project':args.project,'knowledge_mode':mode,
              'model_enabled':bool(modelcfg.get('model_command')),
              'writes':['state.sqlite3','config.json','launcher.json','instance.json','bin/tongle-entry.py']+([] if IS_WINDOWS else ['bin/tongle-pi']),
              'global_config_changes':False,'dry_run':not args.write}
        if not args.write:print(json.dumps(plan,ensure_ascii=False,indent=2));return
        # Build the initial Chinese navigation without starting model work.
        from tongle.engine import Engine
        from tongle.evolution import Evolution
        from tongle.views import refresh
        home.mkdir(parents=True,exist_ok=True)
        with locked(home):
            if setup_ownership(home)!=prior_instance:raise ValueError('instance metadata changed during setup; retry')
            current=configpath.read_bytes() if configpath.exists() else None
            if current!=previous:raise ValueError('configuration changed during setup; retry with the current settings')
            if (cfgpath.read_bytes() if cfgpath.exists() else None)!=previous_launcher:
                raise ValueError('runtime binding changed during setup; retry with the current settings')
            initialized=Engine(home)
            try:
                with initialized.locked():
                    initialized.init(vault,evolving=mode in ('memory','evolving'))
                    for path in (cfgpath,configpath):
                        if path.exists():shutil.copy2(path,path.with_name(path.name+'.before-'+str(time.time_ns())))
                    if modelcfg!=settings or previous is None:atomic_json(configpath,modelcfg)
                    launch_settings=json.loads(previous_launcher) if previous_launcher is not None else {}
                    launch_settings.update(project=args.project)
                    launch_settings.setdefault('root',str(ROOT));launch_settings.setdefault('python',sys.executable)
                    atomic_json(cfgpath,launch_settings)
                    if mode in ('memory','evolving'):Evolution(initialized);refresh(initialized)
            finally:initialized.store.close()
            plan.update(initialize_entry(home,already_locked=True))
        if IS_WINDOWS:
            plan['launch_argv']=[sys.executable,str(home/'bin/tongle-entry.py'),'pi']
        else:
            plan['launch']=str(home/'bin/tongle-pi')
        plan['dry_run']=False;print(json.dumps(plan,ensure_ascii=False,indent=2));return
    if args.command in ['status','stop']:
        result=subprocess.run([sys.executable,str(ROOT/'scripts/maintain.py'),'--home',str(home),args.command])
        if result.returncode:raise RuntimeError('maintenance command failed')
        if args.command=='status':
            diagnosis=subprocess.run([sys.executable,str(ROOT/'scripts/doctor.py'),'--home',str(home),'--json'])
            if diagnosis.returncode:sys.exit(diagnosis.returncode)
        return
    if not cfgpath.is_file():raise ValueError('run setup first')
    cfg=json.loads(cfgpath.read_text(encoding='utf-8'))
    from tongle.lifecycle import read_json
    if read_json(home/'instance.json').get('enabled') is False:raise ValueError('instance is disabled; run setup to enable it')
    if extra and extra[0]=='--':extra=extra[1:]
    if args.provider:extra=['--provider',args.provider,*extra]
    if args.model:extra=['--model',args.model,*extra]
    pi=pi_command()
    # Daemon uses core configuration; it never captures itself or loads host extensions.
    try:
        result=subprocess.run([sys.executable,str(ROOT/'scripts/maintain.py'),'--home',str(home),'start'],capture_output=True,text=True,timeout=12)
        maintenance=json.loads(result.stdout) if result.returncode==0 else {'degraded':True,'reason':'maintenance_start_failed'}
    except (OSError,ValueError,subprocess.TimeoutExpired):
        maintenance={'degraded':True,'reason':'maintenance_start_unavailable'}
    if maintenance.get('degraded') or maintenance.get('disabled'):
        # Capture/retrieval remain available. A scheduler failure must not block the host;
        # it is visible and bounded, never silently labelled healthy.
        print(json.dumps({'maintenance':maintenance,'pi':'continuing_without_confirmed_maintenance'}),file=sys.stderr)
    env=dict(os.environ,TONGLE_NEXT_HOME=str(home),TONGLE_NEXT_ROOT=str(ROOT),TONGLE_NEXT_PYTHON=sys.executable,
             TONGLE_NEXT_DEFAULT_PROJECT=cfg['project'])
    if args.project:env['TONGLE_NEXT_PROJECT']=args.project
    argv=[*pi,'--no-extensions','-e',str(ROOT/'adapters/pi.ts'),*extra]
    if IS_WINDOWS:
        env.update(PYTHONUTF8='1',PYTHONIOENCODING='utf-8')
        sys.exit(subprocess.run(argv,env=env).returncode)
    os.execvpe(pi[0],argv,env)


if __name__=='__main__':
    try:main()
    except (OSError,ValueError,KeyError,TypeError,RuntimeError,sqlite3.Error,subprocess.SubprocessError) as exc:
        print(json.dumps({'error':str(exc)[:400]},ensure_ascii=False),file=sys.stderr);sys.exit(1)
