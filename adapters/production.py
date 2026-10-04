"""Shared local routing for installed hosts. No model calls or inferred global scope."""
import hashlib
import json
import os
from pathlib import Path
import sys

def resolve(cwd, path=None):
    config_path=Path(path or os.environ.get('TONGLE_NEXT_INSTALL',str(Path.home()/'.config/tongle-next/active.json')))
    cfg=json.loads(config_path.read_text())
    if not cfg.get('enabled'):raise ValueError('tongle-next installation is disabled')
    cwd=Path(cwd).resolve()
    project=None
    # Explicit local config wins, otherwise longest registered project root.
    for parent in [cwd,*cwd.parents]:
        local=parent/'.tongle-next.json'
        if local.is_file():
            project=json.loads(local.read_text()).get('project');break
    if not project:
        for root,name in sorted(cfg.get('projects',{}).items(),key=lambda x:len(x[0]),reverse=True):
            if cwd.is_relative_to(Path(root).resolve()):project=name;break
    if not project:
        root=next((p for p in [cwd,*cwd.parents] if (p/'.git').exists()),cwd)
        project='local-'+hashlib.sha256(str(root).encode()).hexdigest()[:16]
    if not isinstance(project,str) or not project:raise ValueError('invalid project')
    return dict(cfg,project=project,cwd=str(cwd))

def environment(cfg):
    return dict(os.environ,TONGLE_NEXT_HOME=cfg['home'],TONGLE_NEXT_ROOT=cfg['root'],
                TONGLE_NEXT_PYTHON=cfg['python'],TONGLE_NEXT_PROJECT=cfg['project'],TONGLE_VAULT=cfg['vault'])

if __name__=='__main__':
    print(json.dumps(resolve(sys.argv[1] if len(sys.argv)>1 else os.getcwd()),ensure_ascii=False))
