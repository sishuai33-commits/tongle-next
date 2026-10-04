"""Codex installed hook with project routing; delegates bounded protocol handling."""
import json,os,subprocess,sys
from pathlib import Path
from production import resolve,environment

def main():
    raw=sys.stdin.buffer.read(10*1024*1024+1)
    if len(raw)>10*1024*1024:raise ValueError('hook input too large')
    payload=json.loads(raw);cfg=resolve(payload.get('cwd') or os.getcwd())
    subprocess.Popen([cfg['python'],str(Path(cfg['root'])/'scripts/maintain.py'),'--home',cfg['home'],'start'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
    env=environment(cfg);env['TONGLE_NEXT_HOST']='codex'
    result=subprocess.run([cfg['python'],str(Path(cfg['root'])/'adapters/hook.py'),'--adapter-id=tongle-next','--host=codex'],input=raw,env=env,timeout=25)
    return result.returncode
if __name__=='__main__':
    try:sys.exit(main())
    except Exception as e:
        print('tongle-next hook failed: '+str(e)[:200],file=sys.stderr);sys.exit(0)
