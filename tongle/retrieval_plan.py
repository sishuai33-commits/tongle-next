"""Bounded discovery questions. A plan is not a claim of evidence coverage.

Original text remains in memory for constraint checks. Diagnostic serialization
contains hashes and identifiers only; it never makes another raw-query log.
"""
from dataclasses import dataclass
import hashlib
import re


@dataclass(frozen=True)
class QuerySpec:
    id: str
    text: str
    kind: str = 'original'

    @property
    def digest(self):
        return hashlib.sha256(self.text.encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class QueryPlan:
    original: str
    queries: tuple
    partial: bool = False
    reasons: tuple = ()
    intent: str | None = None

    def report(self):
        return {'query_count': len(self.queries), 'partial': self.partial,
                'reasons': list(self.reasons),
                'queries': [{'id': q.id, 'hash': q.digest, 'kind': q.kind} for q in self.queries]}


def query_specs(queries):
    """Validate a bounded plan/spec list without silently dropping a question."""
    values = queries.queries if isinstance(queries, QueryPlan) else tuple(queries)
    if not 1 <= len(values) <= 4:
        raise ValueError('retrieval requires between one and four queries')
    if any(not isinstance(q, QuerySpec) or not q.id or not isinstance(q.text, str) for q in values):
        raise ValueError('queries must be QuerySpec values')
    if len({q.id for q in values}) != len(values):
        raise ValueError('query IDs must be unique')
    return values


def build_query_plan(query, *, subquestions=None, intent=None):
    if not isinstance(query, str) or len(query) > 4000:
        raise ValueError('query must be text of at most 4000 characters')
    if intent is not None and (not isinstance(intent, str) or not intent.strip() or len(intent) > 400):
        raise ValueError('intent must be nonempty text of at most 400 characters')
    if subquestions is not None:
        if not isinstance(subquestions, (list, tuple)) or len(subquestions) > 3:
            raise ValueError('at most three explicit subquestions are accepted')
        if any(not isinstance(q, str) or not q.strip() or len(q) > 1000 for q in subquestions):
            raise ValueError('subquestions must be nonempty text of at most 1000 characters')
        parts = list(dict.fromkeys(q.strip() for q in subquestions))
        # Caller-specified focus is retrieval guidance only, never authority to
        # drop constraints or change the original request's identity/scope.
        parts = [part + '\n完整请求（所有约束仍适用）：' + query for part in parts]
        partial = len(parts) != len(subquestions)
        reasons = ('duplicate_subquestions',) if partial else ()
        kind = 'explicit'
    else:
        parts = _explicit_subquestions(query)
        # Rule-based splitting cannot certify a complete semantic decomposition.
        ambiguous = bool(re.search(r'分别|以及|同时|两(?:项|段|方面|部分|条)', query))
        partial = bool(parts) or ambiguous
        reasons = ('heuristic_decomposition',) if parts else (('unsplit_coordination',) if ambiguous else ())
        kind = 'derived'
    suffix = '\n检索意图（不改变原请求约束）：' + intent if intent else ''
    specs = [QuerySpec('q0', query + suffix)]
    specs.extend(QuerySpec('q' + str(i), part + suffix, kind) for i, part in enumerate(parts, 1))
    return QueryPlan(query, tuple(specs), partial, reasons, intent)


def build_context_plan(query):
    """Bound automatic discovery without rejecting a long captured user turn.

    The original remains intact for identity, scope and correction admission.
    This discovery projection is explicitly partial, never a complete plan.
    Active search retains its existing strict 4000-character input contract.
    """
    if not isinstance(query,str):
        raise ValueError('query must be text')
    if len(query)<=4000:
        return build_query_plan(query)
    projected=build_query_plan(query[:4000])
    return QueryPlan(query,projected.queries,True,
        ('bounded_query_projection',*projected.reasons))


def _explicit_subquestions(query):
    """At most two explicit question components, retaining verbatim context.

    Punctuation alone does not request multiple answers. Split only two
    interrogative clauses, or an explicitly coordinated request (分别/两项/
    以及). Do not rewrite negation, conditions, or invent synonym expansions.
    """
    import re
    from .context_retrieval import interpret_request,_frame_residual
    from .memory_relevance import execution_steering,query_terms,_REQUEST_PREFIX
    from .memory_selection import _raw_subject
    interp=interpret_request(query)
    if interp.no_history or interp.ack_only or execution_steering(query):return []
    text=interp.text
    clauses=[s.strip() for s in re.split(r'[，,、。；;？！?!：:]+',text) if s.strip()]
    interrogative=re.compile(r'谁|哪|什么|怎么|怎样|如何|多少|多久|为何|为什么|是否|能否|有没有|(?:能|可以|会).*[吗么]')
    asked=[(i,s) for i,s in enumerate(clauses) if interrogative.search(s)]
    explicit=bool(re.search(r'分别|两(?:项|段|方面|部分|条)|以及|同时[^。？！!?]*(?:和|与|及)',text))
    if len(asked)>=2:
        first=asked[0][0];last=asked[1][0]
        # Conditions often precede their question across a comma. Keep the
        # whole intervening span ("若有异议，即使期限已满也会关吗") intact.
        parts=[asked[0][1], '；'.join(clauses[first+1:asked[1][0]+1])]
    elif explicit:
        first=None;parts=[]
        for i,clause in enumerate(clauses):
            if not(interrogative.search(clause) or re.search(r'需要知道|核对|给出|查清|以及',clause)):continue
            joins=list(re.finditer(r'以及',clause)) or list(re.finditer(r'和|与',clause))
            if not joins:continue
            join=joins[-1]
            pair=[clause[:join.start()],clause[join.end():]]
            if all(_frame_residual(_REQUEST_PREFIX.sub('',s.strip())) and query_terms(s) for s in pair):
                first=i;last=i;parts=pair;break
        if first is None:return []
    else:return []
    # Earlier declarative clauses introduce the object and operating scope.
    # When the first clause is already a question, its unchanged noun/prefix
    # carries that context into a later question about an exception or owner.
    context='；'.join(clauses[:first])
    if not context:
        marker=interrogative.search(parts[0])
        context=parts[0][:marker.start()].strip() if marker else ''
    if not context:
        context=_raw_subject(text)
    if not context:return []
    # A trailing restriction applies to the whole request, not just the last
    # question. Preserve it verbatim in every component; otherwise a card
    # rejected for the original scope can be revived by a narrower rerank.
    tail='；'.join(clauses[last+1:])
    result=[]
    for part in parts:
        part=part.strip()
        if context and context not in part:part=context+'；'+part
        if tail:part+='；'+tail
        if part and part not in result:result.append(part)
    return result if len(result)==2 else []
