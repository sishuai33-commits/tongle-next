"""Read-only correction scope from exact source and persisted dependency edges.

No word matching or new index grants a relation. Returned identities/edges are
complete only within the bounded, relevant closure; incomplete plans suppress
all historical delivery for the operation.
"""
from dataclasses import dataclass, field

from .retrieval_budget import verify_identity


@dataclass
class CorrectionScope:
    target_ids: set = field(default_factory=set)
    source_ids: set = field(default_factory=set)
    scopes: set = field(default_factory=set)
    parents: dict = field(default_factory=dict)
    complete: bool = True
    reason: str | None = None

    def report(self):
        return {'complete': self.complete, 'target_count': len(self.target_ids),
                'source_count': len(self.source_ids), 'reason': self.reason}

    def affected(self, target_id):
        """Relevant descendants only; never treat an incomplete plan as safe."""
        if not self.complete:
            raise ValueError('correction scope is incomplete')
        found = {target_id} & self.target_ids
        while True:
            additions = {owner for owner, parents in self.parents.items() if parents & found}
            if additions <= found:
                return found
            found.update(additions)


def plan_correction_scope(engine, project, cards, raw, *, dependency_ids=(), invalid_adoptions=()):
    """Cover candidates, raw owners, correction sources and true ancestors.

    Raw ownership matches source AND version in a current active card's exact
    evidence JSON. Shared identities may invalidate a local descendant, but
    the consumer must still apply card_visible before revealing their text.
    """
    db=engine.db
    allowed=tuple(dict.fromkeys((project,'shared')))
    placeholders=','.join('?' for _ in allowed)
    plan=CorrectionScope(scopes={project})
    maximum=256
    def incomplete(reason='correction_scope_budget_exceeded'):
        plan.complete=False;plan.reason=reason
        return plan
    budget=getattr(engine,'_retrieval_verification_budget',None)
    if budget is not None and budget.exhausted:
        return incomplete('verification_budget_exceeded')
    raw_pairs={(card['_raw_source']['source_id'],card['_raw_source']['version']) for card in raw}
    plan.source_ids={sid for sid,_ in raw_pairs}
    if len(plan.source_ids)>maximum:
        return incomplete()
    roots={c['id'] for c in [*cards,*invalid_adoptions]} | set(dependency_ids)
    if len(roots)>maximum:
        return incomplete()
    if raw_pairs:
        pairs=sorted(raw_pairs)
        values=','.join('(?,?)' for _ in pairs)
        owners=db.execute(
            'WITH requested(source_id,version) AS (VALUES '+values+') '
            'SELECT DISTINCT k.id FROM knowledge_cards k JOIN json_each(k.evidence) e '
            "JOIN requested r ON json_extract(e.value,'$.source_id')=r.source_id "
            "AND json_extract(e.value,'$.version')=r.version "
            "WHERE k.project IN ("+placeholders+") AND k.state='active' LIMIT ?",
            [value for pair in pairs for value in pair]+list(allowed)+[maximum+1])
        for row in owners:
            roots.add(row['id'])
            if len(roots)>maximum:
                return incomplete()
        sources=sorted(plan.source_ids)
        targets=db.execute('SELECT DISTINCT target_card_id FROM source_memory_corrections WHERE project IN ('+
            placeholders+') AND source_id IN ('+','.join('?' for _ in sources)+') AND target_card_id IS NOT NULL LIMIT ?',
            [*allowed,*sources,maximum+1])
        for row in targets:
            roots.add(row['target_card_id'])
            if len(roots)>maximum:
                return incomplete()
    tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('knowledge_dependencies','organization_relations')")}
    todo=list(sorted(roots));seen=set()
    while todo:
        cid=todo.pop()
        if cid in seen:
            continue
        if len(seen)>=maximum:
            return incomplete()
        seen.add(cid)
        row=db.execute('SELECT id,project,version FROM knowledge_cards WHERE id=? AND project IN ('+placeholders+')',
            (cid,*allowed)).fetchone()
        if not row:
            continue
        if not verify_identity(engine,'card',cid,row['version']):
            return incomplete('verification_budget_exceeded')
        plan.target_ids.add(cid);plan.scopes.add(row['project'])
        parents=set()
        if 'knowledge_dependencies' in tables:
            for ref in db.execute('SELECT basis_id FROM knowledge_dependencies WHERE card_id=? LIMIT ?', (cid,maximum+1)):
                parents.add(ref[0])
        if 'organization_relations' in tables:
            from .knowledge_organization import EVIDENCE_RELATIONS
            for ref in db.execute('SELECT target_id FROM organization_relations WHERE owner_id=? AND relation IN ('+
                ','.join('?' for _ in EVIDENCE_RELATIONS)+') LIMIT ?', (cid,*EVIDENCE_RELATIONS,maximum+1)):
                parents.add(ref[0])
        if len(seen | set(todo) | parents)>maximum:
            return incomplete()
        plan.parents[cid]=parents
        todo.extend(sorted(parents-seen))
    return plan
