"""Bounded use-time checks; discovery of moved/new files stays in maintenance."""
import hashlib
from pathlib import Path
from .store import MAX_ORIGINAL_BYTES


def source_current(engine, sid):
    row = engine.db.execute('SELECT s.kind,s.original_path,s.current_version,v.hash FROM sources s JOIN versions v ON v.source_id=s.id AND v.version=s.current_version WHERE s.id=?', (sid,)).fetchone()
    if not row:
        return False
    from .retrieval_budget import verify_identity
    if not verify_identity(engine,'source',sid,row['current_version']):
        return False
    if row['kind'] != 'document' or not row['original_path']:
        return True  # immutable captured/archived original, no mutable binding
    path = Path(row['original_path'])
    try:
        if any(p.is_symlink() for p in (path, *path.parents)):
            return False
        before=path.stat()
        if before.st_size > MAX_ORIGINAL_BYTES:
            return False
        key=(sid,row['hash'],str(path),before.st_mtime_ns,before.st_size,before.st_ino)
        cache=getattr(engine,'_retrieval_source_checks',None)
        if cache is not None and key in cache:return cache[key]
        data=path.read_bytes();after=path.stat()
        valid=(before.st_mtime_ns,before.st_size,before.st_ino)==(after.st_mtime_ns,after.st_size,after.st_ino) and hashlib.sha256(data).hexdigest()==row['hash']
        if cache is not None:cache[key]=valid
        return valid
    except (OSError, ValueError):
        return False


def candidate_dependencies(cards, candidates, maximum=256):
    ids, queue = set(), list(candidates)
    while queue:
        card = queue.pop()
        if not card or card['id'] in ids:
            continue
        if len(ids)>=maximum:return ids,False
        from .retrieval_budget import verify_identity
        if not verify_identity(cards.engine,'card',card['id'],card['version']):return ids,False
        ids.add(card['id'])
        queue.extend(cards.get(r['card_id']) for r in card.get('basis_cards', []))
        # Navigation is not evidence. Its endpoints still need use-time sync so
        # an edited/withdrawn seed cannot keep granting stale graph discovery.
        graph_ids = {path[key] for path in card.get('_graph_paths', [])
                     for key in ('source_id', 'target_id') if isinstance(path.get(key), str)}
        queue.extend(cards.get(cid) for cid in sorted(graph_ids))
        if cards.engine.db.execute("SELECT 1 FROM sqlite_master WHERE name='organization_relations'").fetchone():
            from .knowledge_organization import EVIDENCE_RELATIONS
            queue.extend(cards.get(r[0]) for r in cards.engine.db.execute('SELECT target_id FROM organization_relations WHERE owner_id=? AND relation IN ('+','.join('?' for _ in EVIDENCE_RELATIONS)+')',(card['id'],*EVIDENCE_RELATIONS)))
    return ids, not queue


def correction_descendants(engine,card_id,scopes,maximum=256):
    """Expand only the indexed reverse dependency set of a real correction."""
    db=engine.db
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='knowledge_dependencies'").fetchone():
        return {card_id},True
    edges='SELECT card_id AS owner,basis_id AS target FROM knowledge_dependencies'
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='organization_relations'").fetchone():
        edges+=" UNION SELECT owner_id,target_id FROM organization_relations WHERE relation IN ('supports','refutes','supersedes')"
    rows=db.execute('WITH RECURSIVE edges(owner,target) AS ('+edges+'), affected(id) AS (SELECT ? UNION SELECT e.owner FROM edges e JOIN affected a ON e.target=a.id JOIN knowledge_cards k ON k.id=e.owner WHERE k.project IN ('+','.join('?' for _ in scopes)+')) SELECT id FROM affected LIMIT ?', (card_id,*scopes,maximum+1)).fetchall()
    return {r[0] for r in rows[:maximum]},len(rows)<=maximum


def card_visible(engine,project,card):
    """Delivery permission is distinct from internal dependency invalidation."""
    if not card:return False
    if card['project']==project:return True
    if card['project']!='shared':return False
    from .evolution import config
    cfg=config(engine)
    if project in cfg.get('library_excluded_projects',[]):return False
    topics=cfg.get('knowledge_project_topics',{})
    allowed=topics.get(project) if isinstance(topics,dict) else None
    return not (isinstance(allowed,list) and all(isinstance(t,str) for t in allowed)) or card['topic'] in allowed
