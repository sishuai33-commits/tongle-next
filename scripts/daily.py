"""Local agent fallback and everyday status entry for the active installation."""
import argparse,json,subprocess,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'adapters'))
from production import resolve,environment
p=argparse.ArgumentParser();p.add_argument('--cwd',default=str(Path.cwd()));p.add_argument('command',choices=['context','capture','status','stop']);p.add_argument('--text',default='');p.add_argument('--session',default='manual');p.add_argument('--event-id');p.add_argument('--role',default='user',choices=['user','assistant']);a=p.parse_args();cfg=resolve(a.cwd)
if a.command=='stop':argv=[cfg['python'],str(Path(cfg['root'])/'scripts/maintain.py'),'--home',cfg['home'],'stop']
else:
 argv=[cfg['python'],'-m','tongle','--home',cfg['home'],a.command]
 if a.command=='context':argv+=['--project',cfg['project'],'--text',a.text,'--session',a.session]
 if a.command=='capture':
  if not a.event_id:p.error('capture requires --event-id for durable idempotence')
  argv+=['--project',cfg['project'],'--session',a.session,'--event-id',a.event_id,'--role',a.role]
sys.exit(subprocess.run(argv,cwd=cfg['root'],env=environment(cfg)).returncode)
