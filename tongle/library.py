"""Read copied legacy Markdown without recompiling or silently confirming it."""
import hashlib,json,re
from pathlib import Path
from .presentation import folder

def search(home,text,limit=3,project=None):
    path=Path(home)/'config.json'
    try:cfg=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    except (OSError,ValueError):return []
    if project in cfg.get('library_excluded_projects',[]):return []
    if not cfg.get('library_vault') or not text.strip():return []
    root=(Path(cfg['library_vault'])/folder(cfg.get('library_dir','wiki'))).resolve()
    terms=set(re.findall(r'[a-z0-9_]{2,}',text.lower()))
    for run in re.findall(r'[\u4e00-\u9fff]+',text):terms.update(run[i:i+2] for i in range(len(run)-1))
    matches=[]
    for p in root.rglob('*.md'):
        if p.is_symlink() or not p.resolve().is_relative_to(root) or any(part.startswith('.') or part=='_archived' for part in p.relative_to(root).parts):continue
        try:
            if p.stat().st_size>1024*1024:continue
            body=p.read_text(encoding='utf-8',errors='replace')
        except OSError:continue
        lower=body.lower()
        hits=[term for term in terms if term in lower]
        if not hits:continue
        score=len(hits)+3*sum(term in p.stem.lower() for term in hits)
        pos=min(lower.find(term) for term in hits)
        snippet=body[max(0,pos-80):max(0,pos-80)+550]
        version=hashlib.sha256(body.encode()).hexdigest()[:16]
        matches.append((score,{'id':'legacy:'+str(p.relative_to(root)),'text':p.stem+'\n'+snippet+'\n完整卡片：'+str(p),
                             'status':'legacy_unverified','kind':'knowledge','source_role':'legacy_document',
                             'source_id':str(p),'source_version':version,'quote':snippet}))
    matches.sort(key=lambda x:(x[0],x[1]['id']),reverse=True)
    return [item for _,item in matches[:limit]]
