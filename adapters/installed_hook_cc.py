"""CC (Claude Code) installed hook with project routing; delegates bounded protocol handling.

Mirrors installed_hook.py (Codex) but pins --host=cc. Reads active.json via
production.resolve so the project follows the caller's cwd instead of a fixed
env value. Never crashes the host: any failure exits 0 after a stderr note.
"""
import json,os,subprocess,sys
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from production import resolve,environment

def main():
    raw=sys.stdin.buffer.read(10*1024*1024+1)
    if len(raw)>10*1024*1024:raise ValueError('hook input too large')
    payload=json.loads(raw);cfg=resolve(payload.get('cwd') or os.getcwd())
    subprocess.Popen([cfg['python'],str(Path(cfg['root'])/'scripts/maintain.py'),'--home',cfg['home'],'start'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
    env=environment(cfg);env['TONGLE_NEXT_HOST']='cc'
    result=subprocess.run([cfg['python'],str(Path(cfg['root'])/'adapters/hook.py'),'--adapter-id=tongle-next','--host=cc'],input=raw,env=env,timeout=25)
    return result.returncode
if __name__=='__main__':
    try:sys.exit(main())
    except Exception as e:
        print('tongle-next cc hook failed: '+str(e)[:200],file=sys.stderr);sys.exit(0)
