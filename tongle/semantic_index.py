"""Optional local dense projection; source of truth stays SQLite + Markdown."""
import json
import sqlite3
import time
from .local_models import embed,model_key,ModelUnavailable,rerank


class SemanticIndex:
    def __init__(self, index, cfg):
        self.index=index; self.engine=index.engine; self.db=index.db; self.cfg=cfg
        self.model=cfg.get('embedding_model',{})
        self.key=model_key(self.model)
        with self.engine.locked(), self.engine.transaction():
            self.db.execute('CREATE TABLE IF NOT EXISTS knowledge_vectors(card_id TEXT,version INTEGER,model TEXT,chunk INTEGER,body TEXT,vector BLOB,PRIMARY KEY(card_id,version,model,chunk))')
            self.db.execute('CREATE INDEX IF NOT EXISTS kv_model ON knowledge_vectors(model,card_id,version)')

    def refresh(self, limit=128):
        rows=self.db.execute('SELECT k.card_id,k.version,k.content FROM knowledge_search k WHERE NOT EXISTS (SELECT 1 FROM knowledge_vectors v WHERE v.card_id=k.card_id AND v.version=k.version AND v.model=?) ORDER BY k.card_id LIMIT ?', (self.key,limit)).fetchall()
        entries=[]
        indexed_cards=0
        for row in rows:
            text=row['content']; title=text.split('\n',1)[0]
            chunks=max(1,(len(text)+599)//600)
            if entries and len(entries)+chunks>128:break
            indexed_cards+=1
            for n,start in enumerate(range(0,max(1,len(text)),600)):
                entries.append((row['card_id'],row['version'],self.key,n,title+'\n'+text[start:start+800]))
        if entries:
            vectors=embed(self.model,[e[-1] for e in entries])
            with self.engine.locked(),self.engine.transaction():
                for entry,vec in zip(entries,vectors):
                    self.db.execute('INSERT OR REPLACE INTO knowledge_vectors VALUES(?,?,?,?,?,?)',(*entry,vec.tobytes()))
                self.db.execute("INSERT INTO meta(key,value) VALUES('semantic_index_revision','1') ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1")
                self.db.execute('DELETE FROM knowledge_vectors WHERE model=? AND NOT EXISTS (SELECT 1 FROM knowledge_search k WHERE k.card_id=knowledge_vectors.card_id AND k.version=knowledge_vectors.version)', (self.key,))
        pending=self.db.execute('SELECT count(*) FROM knowledge_search k WHERE NOT EXISTS (SELECT 1 FROM knowledge_vectors v WHERE v.card_id=k.card_id AND v.version=k.version AND v.model=?)',(self.key,)).fetchone()[0]
        return {'indexed_cards':indexed_cards,'pending_cards':pending,'model':self.key}

    def search_many(self, project, queries, config, limit=64):
        """One scoped vector load and batched query embedding per operation.

        Offsets refer to the exact knowledge_search.content projection (title
        plus body). Each query independently chooses its best card passage.
        """
        import numpy as np
        from .retrieval_plan import query_specs
        specs = query_specs(queries)
        limit = min(64, max(1, limit))
        scope, params = self.index.scope_sql(project, config)
        rows = self.db.execute(
            "SELECT v.card_id,v.version,v.chunk,v.body,v.vector,length(k.content) AS content_length, "
            "length(k.content)-length(c.body) AS body_prefix_length,length(c.body) AS body_length, "
            "instr(k.content,char(0)) AS zero_position "
            "FROM knowledge_vectors v JOIN knowledge_search k ON k.card_id=v.card_id AND k.version=v.version "
            "JOIN knowledge_cards c ON c.id=k.card_id AND c.version=k.version AND c.state='active' "
            "WHERE v.model=? AND " + scope + " ORDER BY v.card_id,v.version,v.chunk", (self.key, *params)).fetchall()
        result = {q.id: [] for q in specs}
        if not rows:
            return result
        vectors = embed(self.model, [q.text for q in specs], query=True)
        if len(vectors) != len(specs):
            raise ValueError('query embedding count mismatch')
        matrix = np.stack([np.frombuffer(row['vector'], dtype='float32') for row in rows])
        scores = matrix @ np.asarray(vectors).T
        if not np.all(np.isfinite(scores)):
            raise ValueError('nonfinite semantic score')
        for column, q in enumerate(specs):
            seen = set()
            for i in np.argsort(-scores[:, column], kind='stable'):
                row = rows[int(i)]
                identity = (row['card_id'], row['version'])
                if identity in seen:
                    continue
                seen.add(identity)
                start = row['chunk'] * 600
                # SQLite length stops at NUL; such a source is still readable,
                # but its SQL-derived coordinate must not become a get hint.
                coordinates_known=not row['zero_position']
                result[q.id].append({'id': row['card_id'], 'version': row['version'],
                    'semantic_score': float(scores[i, column]), 'passage': row['body'],
                    'chunk': row['chunk'], 'start': start, 'end': min(start + 800, row['content_length']) if coordinates_known else None,
                    'body_start': max(0,min(row['body_length'],start-row['body_prefix_length'])) if coordinates_known else None,
                    'body_end': max(0,min(row['body_length'],start+800-row['body_prefix_length'])) if coordinates_known else None,
                    'passage_source': 'knowledge_search.content'})
                if len(result[q.id]) >= limit:
                    break
        return result

    def search(self, project, query, config, limit=64):
        from .retrieval_plan import QuerySpec
        return self.search_many(project, (QuerySpec('q0', query),), config, limit)['q0']


def _subquestions(query):
    """Compatibility wrapper for the existing conservative explicit splitter."""
    from .retrieval_plan import _explicit_subquestions
    return _explicit_subquestions(query)


def _identity(card):
    return card['id'], card.get('version')


def _fair_union(groups, limit):
    """Reserve one next candidate per question before spending another slot."""
    result = []
    seen = set()
    for n in range(max((len(rows) for rows in groups.values()), default=0)):
        for rows in groups.values():
            if n >= len(rows):
                continue
            card = rows[n]
            identity = _identity(card)
            if identity not in seen:
                result.append(card)
                seen.add(identity)
                if len(result) >= limit:
                    return result
    return result


def rerank_candidate_groups(groups, plan, cfg, *, rerank_budget=24, token_budget=12288):
    """Score fair, independent query pools within one shared pair/token bound.

    Ranking scores are not entailment. The caller still owns authoritative
    scope, freshness, corrections and automatic-admission checks.
    """
    import math
    from .retrieval_plan import query_specs
    specs = query_specs(plan)
    if type(rerank_budget) is not int or not 0 <= rerank_budget <= 24:
        raise ValueError('rerank_budget must be between zero and 24')
    if type(token_budget) is not int or not 0 <= token_budget <= 12288:
        raise ValueError('token_budget must be between zero and 12288')
    max_length = cfg.get('max_length', 384)
    if type(max_length) is not int or max_length < 1:
        raise ValueError('reranker max_length must be a positive integer')
    max_length = min(max_length, 512)
    rank_cfg = dict(cfg, max_length=max_length)
    capacity = min(rerank_budget, token_budget // max_length)
    pools = {q.id: [] for q in specs}
    rows = {q.id: list(dict((_identity(c), c) for c in groups.get(q.id, [])).values()) for q in specs}
    pairs = 0
    for n in range(max((len(items) for items in rows.values()), default=0)):
        for q in specs:
            if n < len(rows[q.id]) and pairs < capacity:
                pools[q.id].append(rows[q.id][n])
                pairs += 1
        if pairs >= capacity:
            break
    returned = {}
    counts = {}
    for q in specs:
        pool = pools[q.id]
        counts[q.id] = len(pool)
        if not pool:
            continue
        passages = [c.get('_semantic_query_matches', {}).get(q.id, {}).get('passage')
                    or c.get('title', '') + '\n' + c.get('body', '')[:1200] for c in pool]
        scores = rerank(rank_cfg, q.text, passages)
        if len(scores) != len(pool) or any(not math.isfinite(float(score)) for score in scores):
            raise ValueError('reranker returned invalid scores')
        for card, score in zip(pool, scores):
            score = float(score)
            card.setdefault('_semantic_query_matches', {}).setdefault(q.id, {})['rerank_score'] = score
            if q.id == 'q0':
                card['_rerank_score'] = score
            else:
                card.setdefault('_semantic_part_scores', {})[q.text] = score
                # Explicitly link the compatibility question key to its ID.
                # This is transient candidate state, not diagnostic logging.
                card.setdefault('_semantic_part_ids', {})[q.text] = q.id
            returned[_identity(card)] = card
    result = sorted(returned.values(), key=lambda c: -max(
        (m['rerank_score'] for m in c.get('_semantic_query_matches', {}).values() if 'rerank_score' in m),
        default=float('-inf')))
    return result, {'rerank_pairs': pairs, 'rerank_query_pairs': counts,
                    'input_tokens': None, 'input_tokens_upper_bound': pairs * max_length,
                    'input_tokens_measurement': 'estimated_upper_bound',
                    'rerank_budget': rerank_budget, 'token_budget': token_budget}


def _semantic_candidates(index, project, query, config, lexical, *, plan=None, rerank_budget=24, token_budget=12288):
    """Independently recall each question, then merge within a bounded budget.

    Uses only ready projections; indexing remains in existing maintenance.
    The lexical input may be q0's list or candidates_many's per-query mapping.
    In the list form, extra lexical reads use search_many without refreshing.
    """
    from .retrieval_plan import build_query_plan, query_specs
    plan = plan or build_query_plan(query)
    specs = query_specs(plan)
    lexical_groups = lexical if isinstance(lexical, dict) else {'q0': lexical}
    missing = [q for q in specs if q.id not in lexical_groups]
    lexical_started = time.perf_counter()
    if missing:
        lexical_groups = dict(lexical_groups, **index.search_many(project, missing, config))
    lexical_cards = {}
    for q in specs:
        for rank, source in enumerate(lexical_groups.get(q.id, []), 1):
            card = lexical_cards.setdefault(_identity(source), dict(source))
            card['_semantic_lexical_candidate'] = True
            card.setdefault('_semantic_query_matches', {}).setdefault(q.id, {})['lexical_rank'] = rank
    lexical_groups = {q.id: [lexical_cards[_identity(c)] for c in lexical_groups.get(q.id, [])] for q in specs}
    fallback = _fair_union(lexical_groups, 64)
    cfg = config.get('retrieval', {})
    strategy = cfg.get('strategy', 'lexical') if isinstance(cfg, dict) else 'lexical'
    report = {'requested_strategy': strategy, 'strategy': 'lexical', 'cache_state': 'bypass',
              'query_plan': plan.report(),
              'query_results': {q.id: {'hash': q.digest, 'candidate_found': bool(lexical_groups[q.id]),
                  'unresolved': not bool(lexical_groups[q.id]), 'candidates': len(lexical_groups[q.id]),
                  'scored': 0, 'candidate_ids': [c['id'] for c in lexical_groups[q.id]]} for q in specs}}
    if strategy not in ('hybrid', 'rerank', 'online_admission') or not query.strip():
        report.update(candidates=len(fallback), merged_candidates=len(fallback), subquestions=len(specs)-1,
                      stage_timings={'lexical': (time.perf_counter()-lexical_started)*1000})
        return fallback, report
    started = time.perf_counter()
    from .local_models import _load
    cache_before = _load.cache_info()
    timings = {}
    phase = 'semantic'; phase_started = started
    report['cache_state'] = 'unknown'
    try:
        semantic = SemanticIndex(index, cfg)
        dense_groups = semantic.search_many(project, specs, config)
        scope, params = index.scope_sql(project, config)
        pending = index.db.execute('SELECT count(*) FROM knowledge_search k WHERE ' + scope +
            ' AND NOT EXISTS (SELECT 1 FROM knowledge_vectors v WHERE v.card_id=k.card_id AND v.version=k.version AND v.model=?)',
            (*params, semantic.key)).fetchone()[0]
        report['semantic_pending'] = pending
        if pending:
            report['degraded'] = 'semantic_index_partial'
        if not any(dense_groups.values()):
            report['degraded'] = 'semantic_index_not_ready'
        timings[phase] = (time.perf_counter()-phase_started)*1000
        phase = 'rank'; phase_started = time.perf_counter()
        by_id = {}
        groups = {}
        for q in specs:
            route = {}
            for rank, old in enumerate(lexical_groups.get(q.id, []), 1):
                identity = _identity(old)
                card = by_id.setdefault(identity, dict(old, _semantic_query_matches={
                    qid: dict(match) for qid, match in old.get('_semantic_query_matches', {}).items()}))
                card['_semantic_lexical_candidate'] = True
                card.setdefault('_semantic_query_matches', {}).setdefault(q.id, {})['lexical_rank'] = rank
                route[identity] = card
            for rank, item in enumerate(dense_groups.get(q.id, []), 1):
                current = index.cards.get(item['id'])
                if not current or ('version' in item and item['version'] != current['version']):
                    continue
                identity = _identity(current)
                card = by_id.setdefault(identity, dict(current))
                match = card.setdefault('_semantic_query_matches', {}).setdefault(q.id, {})
                match.update({key: item[key] for key in ('semantic_score', 'passage', 'chunk', 'start', 'end', 'body_start', 'body_end', 'passage_source') if key in item})
                match['semantic_rank'] = rank
                card['_semantic_score'] = max(card.get('_semantic_score', -1), item['semantic_score'])
                if q.id == 'q0' or '_semantic_passage' not in card:
                    card['_semantic_passage'] = item['passage']
                route[identity] = card
            def rrf(card):
                match = card['_semantic_query_matches'][q.id]
                value = sum(1 / (60 + match[key]) for key in ('lexical_rank', 'semantic_rank') if key in match)
                match['rrf_score'] = value
                return -value, str(card['id']), card.get('version', 0)
            ranked = sorted(route.values(), key=rrf)
            # Reserve a lexical discovery slot in each independent pool while
            # new projections are pending. This still grants no admission.
            exact = lexical_groups.get(q.id, [])[:1]
            reserved = [by_id[_identity(c)] for c in exact if _identity(c) in route]
            groups[q.id] = reserved + [c for c in ranked if c not in reserved]
        merged = _fair_union(groups, 64)
        admitted_ids = {_identity(c) for c in merged}
        groups = {qid: [c for c in rows if _identity(c) in admitted_ids] for qid, rows in groups.items()}
        if strategy == 'rerank':
            fused, rank_report = rerank_candidate_groups(groups, plan, cfg.get('reranker', {}),
                rerank_budget=rerank_budget, token_budget=token_budget)
            report.update(rank_report)
        else:
            fused = merged
        report['query_results'] = {q.id: {
            'hash': q.digest, 'candidate_found': bool(groups[q.id]), 'unresolved': not bool(groups[q.id]),
            'candidates': len(groups[q.id]), 'scored': report.get('rerank_query_pairs', {}).get(q.id, 0),
            'candidate_ids': [c['id'] for c in groups[q.id]]} for q in specs}
        report.update(strategy=strategy, model=semantic.key, candidates=len(fused),
                      merged_candidates=len(merged), subquestions=len(specs)-1)
        return fused, report
    except (ImportError, ValueError, RuntimeError, OSError, sqlite3.Error) as exc:
        report.update(degraded=type(exc).__name__)
        return fallback, report
    finally:
        if phase not in timings:
            timings[phase] = (time.perf_counter()-phase_started)*1000
        report['stage_timings'] = timings
        report['elapsed_ms'] = round((time.perf_counter()-started)*1000, 3)
        cache_after = _load.cache_info()
        if cache_after.misses > cache_before.misses:
            report['cache_state'] = 'cold'
        elif cache_after.hits > cache_before.hits:
            report['cache_state'] = 'warm'
        report['cache_scope'] = 'process_local_model_loader'


def semantic_candidates(index, project, query, config, lexical, *, plan=None, rerank_budget=24, token_budget=12288):
    result, report = _semantic_candidates(index, project, query, config, lexical,
                                         plan=plan, rerank_budget=rerank_budget, token_budget=token_budget)
    retrieval = config.get('retrieval', {})
    graph = retrieval.get('graph', {}) if isinstance(retrieval, dict) else {}
    if not isinstance(graph, dict) or graph.get('enabled') is not True:
        return result, report
    from .graph_retrieval import augment
    from .retrieval_plan import build_query_plan
    return augment(index, project, plan or build_query_plan(query), config, result, report)


def select_semantic_cards(cards,query,project,config,lexical):
    """Conservative automatic admission after scope, freshness and corrections.

    Model scores rank usefulness only. They never override named identities,
    versions, withdrawals, project boundaries or the complete-block renderer.
    """
    import re
    from .context_retrieval import interpret_request,is_acknowledgement
    from .memory_relevance import named_query_terms,filter_applicable,execution_steering
    from .memory_selection import _raw_subject
    interp=interpret_request(query)
    if interp.no_history or interp.ack_only or is_acknowledgement(query) or execution_steering(query):return []
    cfg=config.get('retrieval',{})
    if not isinstance(cfg,dict):return lexical
    strategy=cfg.get('strategy','lexical')
    # Explicit search remains local hybrid discovery; online admission is only
    # called by automatic context after both cards and sources are protected.
    if strategy=='online_admission':strategy='hybrid'
    if strategy not in ('hybrid','rerank'):return lexical
    minimum=cfg.get('semantic_min_score',0.60)
    lexical_ids={c['id'] for c in lexical}
    eligible=[c for c in cards if c.get('_semantic_score',-1)>=minimum or c['id'] in lexical_ids]
    if not eligible:return lexical
    names=named_query_terms(query,project)
    noun=_raw_subject(query)
    def identity_ok(card):
        content=' '.join(str(card.get(k) or '') for k in ('title','body','key','topic')).lower()
        # Grammar position cannot establish an alias or remove identity.
        # A provider after a verb ("接入 NXCloud") remains a constraint.
        if names and not all(n.lower() in content for n in names):return False
        # With an ASCII subject, the remaining Chinese noun is its aspect,
        # not a second identity (DS 缓存规则 can have a 缓存验收 basis).
        if not names and noun and len(noun)<=4 and re.fullmatch(r'[\u3400-\u9fff]+',noun):
            return noun in content
        return True
    eligible=filter_applicable([c for c in eligible if identity_ok(c)],query,project)
    if not eligible:return []
    if strategy=='rerank':
        ranked=[c for c in eligible if '_rerank_score' in c or c.get('_semantic_part_scores')]
        if not ranked:return lexical
        def qualified(card, qid):
            matches=card.get('_semantic_query_matches')
            if not matches:return True  # Pre-plan candidates retain their old contract.
            return card['id'] in lexical_ids or matches.get(qid,{}).get('semantic_score',-1)>=minimum
        def threshold(scores):
            absolute=cfg.get('rerank_min_score',-6.0)
            window=cfg.get('rerank_score_window',0.75)
            return absolute if window is None else max(absolute,max(scores)-window)
        original=[c for c in ranked if '_rerank_score' in c and qualified(c,'q0')]
        selected=[]
        if original:
            floor=threshold([c['_rerank_score'] for c in original])
            selected=[c for c in original if c['_rerank_score']>=floor]
        # Each explicit part competes only among its own independently recalled
        # and scored candidates. Full original identity/applicability checks
        # above apply even to cards that were never scored for q0.
        for question in dict.fromkeys(q for c in ranked for q in c.get('_semantic_part_scores',{})):
            part_cards=[c for c in filter_applicable(ranked,question,project)
                        if question in c.get('_semantic_part_scores',{})
                        and qualified(c,c.get('_semantic_part_ids',{}).get(question,'q0'))]
            if not part_cards:continue
            part_floor=threshold([c['_semantic_part_scores'][question] for c in part_cards])
            present={_identity(c) for c in selected}
            selected.extend(c for c in part_cards if _identity(c) not in present and c['_semantic_part_scores'][question]>=part_floor)
        def best_score(card):
            return max([card.get('_rerank_score',float('-inf')),*card.get('_semantic_part_scores',{}).values()])
        # Similarity is not a dependency edge. A lower-ranked supplement still
        # needs a strict lexical qualification and inspectable current basis or
        # exact complete fact, including all conditions.
        admitted=list(selected);retained={_identity(c) for c in admitted}
        for card in ranked:
            if card['id'] not in lexical_ids or _identity(card) in retained or best_score(card)<cfg.get('rerank_min_score',-6.0):continue
            linked=any(any(ref.get('card_id')==card['id'] and ref.get('version')==card.get('version')
                           for ref in parent.get('basis_cards',[])) for parent in admitted)
            whole=any(parent.get('body','').strip() and card.get('body','').strip().startswith(parent['body'].strip())
                      for parent in admitted)
            if linked or whole:selected.append(card);retained.add(_identity(card))
        return sorted(selected,key=lambda c:-best_score(c))
    top=max((c.get('_semantic_score',-1) for c in eligible),default=-1)
    selected=[c for c in eligible if c['id'] in lexical_ids or c.get('_semantic_score',-1)>=top-cfg.get('semantic_score_window',0.035)]
    return sorted(selected,key=lambda c:-c.get('_semantic_score',-1))
