#!/usr/bin/env python3
"""One-time scoped setup and a daily Pi launcher. No global installation."""
import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time
from maintain import atomic_json
ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser(description=__doc__,allow_abbrev=False)
    parser.add_argument('command',choices=['setup','pi','status','stop'])
    parser.add_argument('--home',required=True,type=Path)
    parser.add_argument('--vault',type=Path)
    parser.add_argument('--project')
    parser.add_argument('--enable-model',action='store_true')
    parser.add_argument('--provider');parser.add_argument('--model')
    parser.add_argument('--write',action='store_true')
    args,extra=parser.parse_known_args()
    home=args.home.expanduser().resolve()
    cfgpath=home/'launcher.json'
    if args.command=='setup':
        args.provider=args.provider or 'zhipu';args.model=args.model or 'glm-5.3-flash'
        if extra:raise ValueError('unrecognized setup arguments')
        if not args.vault or not args.project:raise ValueError('setup requires --vault and --project')
        vault=args.vault.expanduser().resolve()
        plan={'home':str(home),'vault':str(vault),'project':args.project,'model_enabled':args.enable_model,
              'writes':['state.sqlite3','config.json','launcher.json','bin/tongle-pi'],
              'global_config_changes':False,'dry_run':not args.write}
        if not args.write:print(json.dumps(plan,ensure_ascii=False,indent=2));return
        if cfgpath.is_symlink() or (home/'config.json').is_symlink():raise ValueError('configuration symlink rejected')
        for path in [cfgpath,home/'config.json']:
            if path.exists():shutil.copy2(path,path.with_name(path.name+'.before-'+str(time.time_ns())))
        run=subprocess.run([sys.executable,'-m','tongle','--home',str(home),'init','--vault',str(vault),'--evolving'],cwd=ROOT,text=True,capture_output=True)
        if run.returncode:raise RuntimeError(run.stdout)
        cfg={'project':args.project,'root':str(ROOT),'python':sys.executable}
        atomic_json(cfgpath,cfg)
        modelcfg={'knowledge_mode':'evolving','knowledge_quiet_seconds':300,'knowledge_daily_call_budget':40,'knowledge_daily_token_budget':300000,'knowledge_max_output_tokens':4096}
        if args.enable_model:
            pi=shutil.which('pi')
            if not pi:raise ValueError('Pi executable not found; no model installed')
            modelcfg.update(model_command=[sys.executable,str(ROOT/'scripts/pi_model.py'),'--pi',pi,'--provider',args.provider,'--model',args.model],
                            model_timeout_seconds=200,model_name=f'pi/{args.provider}/{args.model}')
        atomic_json(home/'config.json',modelcfg)
        # Build the initial Chinese navigation without starting model work.
        sys.path.insert(0,str(ROOT))
        from tongle.engine import Engine
        from tongle.evolution import Evolution
        from tongle.views import refresh
        initialized=Engine(home)
        try:Evolution(initialized);refresh(initialized)
        finally:initialized.store.close()
        binpath=home/'bin'
        if binpath.is_symlink():raise ValueError('launcher directory symlink rejected')
        binpath.mkdir(exist_ok=True)
        launch=binpath/'tongle-pi'
        if launch.is_symlink():raise ValueError('launcher symlink rejected')
        command=shlex.join([sys.executable,str(ROOT/'scripts/tongle.py'),'pi','--home',str(home)])
        if launch.exists():shutil.copy2(launch,launch.with_name(launch.name+'.before-'+str(time.time_ns())))
        temp=launch.with_name(launch.name+'.tmp-'+str(time.time_ns()))
        temp.write_text('#!/bin/sh\n# tongle-owned launcher\nexec '+command+' "$@"\n');temp.chmod(0o700);os.replace(temp,launch)
        plan['launch']=str(launch);plan['dry_run']=False;print(json.dumps(plan,ensure_ascii=False,indent=2));return
    if not cfgpath.is_file():raise ValueError('run setup first')
    cfg=json.loads(cfgpath.read_text())
    if args.command in ['status','stop']:
        result=subprocess.run([sys.executable,str(ROOT/'scripts/maintain.py'),'--home',str(home),args.command])
        if result.returncode:raise RuntimeError('maintenance command failed')
        if args.command=='status':subprocess.run([sys.executable,'-m','tongle','--home',str(home),'status'],cwd=ROOT,check=True)
        return
    if extra and extra[0]=='--':extra=extra[1:]
    if args.provider:extra=['--provider',args.provider,*extra]
    if args.model:extra=['--model',args.model,*extra]
    # Daemon uses core configuration; it never captures itself or loads host extensions.
    result=subprocess.run([sys.executable,str(ROOT/'scripts/maintain.py'),'--home',str(home),'start'],capture_output=True,text=True)
    if result.returncode:raise RuntimeError('maintenance startup failed: '+result.stdout)
    env=dict(os.environ,TONGLE_NEXT_HOME=str(home),TONGLE_NEXT_ROOT=str(ROOT),TONGLE_NEXT_PYTHON=sys.executable,
             TONGLE_NEXT_PROJECT=args.project or cfg['project'])
    pi=shutil.which('pi')
    if not pi:raise ValueError('Pi executable not found')
    os.execvpe(pi,[pi,'--no-extensions','-e',str(ROOT/'adapters/pi.ts'),*extra],env)


if __name__=='__main__':
    try:main()
    except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as exc:
        print(json.dumps({'error':str(exc)[:400]},ensure_ascii=False),file=sys.stderr);sys.exit(1)
