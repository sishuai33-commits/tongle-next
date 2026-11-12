"""Small integration boundary: processing policy, raw retrieval and corrections."""
import json
import hashlib
import sqlite3
from .memory_policy import read_home_config, processing_allowed


def allowed(engine, project):
    return processing_allowed(read_home_config(engine.home), project)


def source_allowed(engine, sid, version=None):
    row=engine.db.execute('SELECT project,current_version FROM sources WHERE id=?',(sid,)).fetchone()
    if not row or not allowed(engine,row['project']):
        return False
    try:
        return engine.store.admission(sid,row['current_version'] if version is None else version)
    except (KeyError,ValueError):
        return False


def register(engine):
    def work(project, inputs):
        if not allowed(engine,project):
            return 0
        try:
            parts=json.loads(inputs or '[]')
        except (TypeError,ValueError):
            return 1  # Existing malformed-work handling remains responsible.
        if not isinstance(parts,list):
            return 1
        return int(all(source_allowed(engine,p['source_id'],p.get('version')) for p in parts if isinstance(p,dict) and p.get('source_id')))
    engine.db.create_function('tongle_process',1,lambda p:int(allowed(engine,p)))
    engine.db.create_function('tongle_source_process',2,lambda s,v:int(source_allowed(engine,s,v)))
    engine.db.create_function('tongle_work_process',2,work)


def active_counts(engine):
    rows=engine.db.execute("SELECT project,state,COUNT(*) n FROM knowledge_work WHERE state NOT IN ('done','obsolete') GROUP BY project,state").fetchall()
    active,paused={},{}
    for r in rows:
        dest=active if allowed(engine,r['project']) else paused
        dest[r['state']]=dest.get(r['state'],0)+r['n']
    return {'active':active,'paused_history':paused}


def annotate_notes(engine, cards):
    """Read immutable, matching-version metadata without starting a writer.

    The hash only authenticates the stored projection, never the claim's truth.
    A manually edited/legacy note receives no manufactured authority label.
    """
    if not engine.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='inline_note_meta'").fetchone():
        return cards
    result=[]
    for card in cards:
        view=dict(card)
        row=engine.db.execute('SELECT payload FROM inline_note_meta WHERE card_id=? AND version=? AND project=?',
                             (card['id'],card['version'],card['project'])).fetchone()
        if row:
            try:
                meta=json.loads(row[0])
                digest=hashlib.sha256(json.dumps(card['body'],ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
                if isinstance(meta,dict) and meta.get('body_hash')==digest:
                    view['memory_metadata']=meta
            except (TypeError,ValueError):
                pass
        result.append(view)
    return result


def context_inputs(evolution, project, query, cards, focus=(), session=None,current_source_id=None,
                   effective_query=None,dependency_ids=(),query_plan=None,admission=None,
                   config=None,raw_pair_budget=6,online_deadline=None):
    # An empty admitted subject is abstention, not a chance for raw excerpts
    # to refill a card-free context. Keep the original query for event identity.
    if effective_query == '':
        return [], [], []
    retrieval_query=query if effective_query is None else effective_query
    from .retrieval_plan import build_context_plan
    from .raw_retrieval import discover_raw, score_raw, excerpt
    plan=query_plan or build_context_plan(retrieval_query)
    cfg=read_home_config(evolution.engine.home) if config is None else config
    from .source_memory import SourceMemory, _tokens
    from .memory_relevance import query_terms,relevant
    memory=SourceMemory(evolution.engine)
    notices=[]
    notice_guards=[]
    from .knowledge_learning import adopted_corrections_valid
    invalid_adoptions=[c for c in cards if not adopted_corrections_valid(evolution.engine,c)]
    if invalid_adoptions:
        notices.append('部分修订认识的纠正依据已变化，正文暂不作为有效结论；请回读当前原文，不能恢复旧口径。')
        cards=[c for c in cards if c not in invalid_adoptions]
    # Bound only the retrieval projection, never the saved source or corrections.
    try:
        report=memory.index_sources(project,limit=64)
        evolution._source_index_report=report
        if report.get('pending_versions'):
            notices.append('原文索引正在分批补建；当前检索可能不完整，可按来源编号回读。')
        if report['errors']:
            notices.append('原文索引尚有失败；搜索结果可能不完整，可按来源编号回读。')
        # Prior assistant answers are retained for explicit lookup, not fed back
        # automatically as fresh evidence for their own recommendations.
        # The host captures before recall. Re-injecting the current question as
        # a historical excerpt can make it look like the only source for a
        # note. The answer model already has this message in its current turn.
        current=None
        if current_source_id:
            current=evolution.db.execute("SELECT id FROM sources WHERE id=? AND project=? AND session=? AND role='user'",
                (current_source_id,project,session)).fetchone()
            if not current:raise ValueError('current source outside bound project/session')
        elif session:
            # Compatibility only: use the newest matching event, never every
            # historic message with identical text. New hosts supply identity.
            latest=evolution.db.execute("SELECT s.id,v.text FROM sources s JOIN versions v ON v.source_id=s.id AND v.version=s.current_version WHERE s.project=? AND s.session=? AND s.role='user' ORDER BY s.created_at DESC LIMIT 1",(project,session)).fetchone()
            q=query.strip()
            if latest and (latest['text'].strip()==q or (len(q)>=4000 and latest['text'].strip().startswith(q))):
                current=(latest['id'],)
        from .online_admission import requested as online_requested
        raw,groups=discover_raw(memory,project,plan,
            exclude_source_id=current[0] if current else None,
            candidate_limit=24 if online_requested(cfg) else 64,
            verification_limit=24 if online_requested(cfg) else None)
        raw,raw_report=score_raw(groups,plan,cfg,pair_budget=raw_pair_budget)
        if online_requested(cfg):
            raw_report['discovery_candidate_limit'] = 24
        evolution._raw_retrieval_report=raw_report
    except (ImportError,OSError,ValueError,RuntimeError,sqlite3.Error) as exc:
        raw=[]
        evolution._raw_retrieval_report={'degraded':type(exc).__name__,
            'rerank_pairs':None,'input_tokens_upper_bound':raw_pair_budget*512}
        notices.append('原文搜索暂时失败；以下已有笔记和纠正仍可用，不能据此断言没有原文。')
    tokens=set(_tokens(retrieval_query));by_id={c['id']:c for c in cards};suppressed=set()
    old_sources={(ref.get('source_id'),ref.get('version')) for card in invalid_adoptions for ref in card.get('evidence',[])}
    from .correction_scope import plan_correction_scope
    try:
        correction_scope=plan_correction_scope(evolution.engine,project,cards,raw,
            dependency_ids=dependency_ids,invalid_adoptions=invalid_adoptions)
        scope_report=correction_scope.report()
    except (ValueError,TypeError,sqlite3.Error):
        scope_report={'complete':False,'reason':'correction_scope_lookup_failed'}
    evolution._raw_retrieval_report['correction_scope']=scope_report
    if not scope_report['complete']:
        evolution._raw_retrieval_report['degraded']=scope_report['reason']
        return [],['本轮纠正关联范围未完成核验；已暂停历史资料投递，请按具体笔记或原文回读，不能恢复旧口径。'],[]
    scopes=correction_scope.scopes
    corrections=[c for scope in sorted(scopes) for c in memory.corrections(scope,include_history=False,
                 target_ids=correction_scope.target_ids,source_ids=correction_scope.source_ids)]
    if any(c.get('verification_unknown') for c in corrections):
        return [],['纠正依据核验达到本轮限额；当前历史资料暂不投递，请显式回读，不能恢复旧口径。'],[]
    corrections.sort(key=lambda c:not c['effective'])
    focus=set(focus)
    for c in corrections:
        affected={c['target_card_id']} if c['target_card_id'] else set()
        if c['effective']:
            from .knowledge_learning import correction_adopted
            target_card=by_id.get(c['target_card_id']) or evolution.cards.get(c['target_card_id'])
            adopted=bool(target_card and correction_adopted(evolution.engine,target_card,c))
            if c['target_card_id']:
                # This complete relation closure already includes every raw
                # owner and its true ancestors. Unrelated descendants cannot
                # consume this read's budget or restore an excluded raw view.
                affected.update(correction_scope.affected(c['target_card_id']))
            # Read-time exclusion only. Never schedule recursive maintenance.
            while True:
                descendants={card['id'] for card in cards if any(
                    ref.get('card_id') in affected for ref in card.get('basis_cards',[]))}
                newer=affected|descendants
                if newer==affected:break
                affected=newer
            # Adopting a correction updates this one note, not its descendants.
            suppressed.update(affected-({c['target_card_id']} if adopted else set()))
            for cid in affected:
                prior=by_id.get(cid) or evolution.cards.get(cid)
                if prior and prior.get('project') in scopes:
                    from .retrieval_budget import verify_identity
                    if not verify_identity(evolution.engine,'card',prior['id'],prior['version']):
                        return [],['纠正依据核验达到本轮限额；当前历史资料暂不投递，请显式回读，不能恢复旧口径。'],[]
                    old_sources.update((ref.get('source_id'),ref.get('version')) for ref in prior.get('evidence',[]))
        card=by_id.get(c['target_card_id'])
        text=' '.join([c['replacement'],c['quote'],(card or {}).get('title','')])
        matches = (relevant(retrieval_query,text,project,(card or {}).get('title',''))
                   if not allowed(evolution.engine,project) else bool(tokens.intersection(_tokens(text))))
        if not (affected.intersection(focus) or matches):
            continue
        label='有效纠正' if c['effective'] else '纠正候选，归属或目标尚未核定，不能当作用户决定'
        target = None
        if c['target_card_id']:
            target = by_id.get(c['target_card_id']) or evolution.cards.get(c['target_card_id'])
        if target and hasattr(evolution.engine,'db'):
            from .retrieval_freshness import card_visible
            if not card_visible(evolution.engine,project,target):
                notices.append('部分本项目知识的依据当前不可用或已变更；旧结论已暂停，请回读本项目知识。')
                continue
        known_target = (target and target.get('project') in scopes and isinstance(target.get('id'),str)
                        and type(target.get('version')) is int and target['version'] > 0)
        read_hint = ('get使用此笔记ID' if known_target and target['project'] == project
                     else '共享笔记；get可在获准共享范围内回读，不能从本项目改写')
        target_ref = (f"目标笔记【记忆:{target['id']}@{target['version']}】（{read_hint}）"
                      if known_target else '目标笔记未定或不可用，不生成笔记引用')
        notices.append(f"{label}：{c['replacement']}；{target_ref}；纠正原文 source={c['source_id']}@{c['source_version']}（仅用source读取，不是笔记ID）；事件 {c['event_key']}")
        notice_guards.append(c)
    raw=[c for c in raw if (c['_raw_source']['source_id'],c['version']) not in old_sources]
    current_cards=[c for c in cards if c['id'] not in suppressed]
    if admission is None:
        from .raw_retrieval import lexical_candidates
        from .semantic_index import select_semantic_cards
        def admission(candidates):
            lexical=lexical_candidates(candidates,retrieval_query,project)
            return select_semantic_cards(candidates,retrieval_query,project,cfg,lexical)
    # A source excerpt competes with notes under the same subject, applicability
    # and score gates. It cannot refill an empty result through a weaker route.
    from .online_admission import requested as online_requested, admit, local_candidates
    online_admitted = False
    if online_requested(cfg) and not allowed(evolution.engine,project):
        candidates=local_candidates(current_cards+raw,retrieval_query,project)
        admitted,online_report=admit(evolution,project,query,candidates,cfg,deadline=online_deadline,
                                     notice_guards=notice_guards)
        evolution._raw_retrieval_report['online_admission']=online_report
        if online_report.get('degraded'):
            evolution._raw_retrieval_report['degraded']=online_report['degraded']
            # Prior notices may themselves contain changed historical facts.
            return [],['本轮在线资料核对未完成，已暂停自动推荐；需要的资料仍可主动检索并核对原文。'],[]
        online_admitted = True
    else:
        admitted=admission(current_cards+raw)
    fallback=[excerpt(c) for c in admitted if '_raw_source' in c][:5]
    if online_admitted:
        from .memory_brief import _online_admitted_statement
        fallback = [_online_admitted_statement(item) for item in fallback]
    return [c for c in admitted if '_raw_source' not in c],notices,fallback


def note_view(engine, project, item):
    """Direct tools must honor the same correction overlay as automatic recall."""
    from .source_memory import SourceMemory
    view=dict(item)
    from .knowledge_learning import adopted_corrections_valid
    from .evolution import Evolution
    current=Evolution(engine).cards.get(view['id']) if view.get('id') else None
    scope=current['project'] if current else project
    corrections=SourceMemory(engine).corrections(scope,include_history=False,
        target_ids=[view['id']] if view.get('id') else [])
    from .retrieval_budget import verification_report
    verification=verification_report(engine)
    if any(c.get('verification_unknown') for c in corrections) or verification.get('verification_budget_exhausted'):
        view.update(statement=None,body='',complete=False,state='unavailable',
                    view_notice='本轮依据核验达到限额；纠正与旧正文均暂不投递，请重新按该笔记回读。')
        view['metadata']=None
        view.setdefault('retrieval',{}).update(verification)
        return view
    for correction in corrections:
        if correction['effective'] and correction['target_card_id']==view.get('id'):
            from .knowledge_learning import correction_adopted
            from .evolution import Evolution
            card=Evolution(engine).cards.get(view['id'])
            if card and correction_adopted(engine,card,correction):
                view['adopted_correction']=correction['event_key']
                continue
            view['statement']=correction['replacement']
            view['body']=correction['replacement']
            view['effective_correction']=correction['event_key']
            view['complete']=False
            view['projection_kind']='effective_correction'
            view['complete_scope']='original_note_full_text'
            view['knowledge_completeness']='not_assessed'
            view['view_notice']=('这是已生效纠正的投影；complete=false仅表示未返回原笔记全文，'
                                 '不表示材料或知识缺失，也不表示纠正尚未生效。'
                                 '接口状态不是用户事实，不要据此推断知识是否完整。')
            view['metadata']={'authority':'user','status':'corrected',
                'scope':project,'prior_note_version':item.get('version'),
                'evidence':[{'source_id':correction['source_id'],'version':correction['source_version'],'quote':correction['quote']}]}
            break
    if not view.get('effective_correction') and current and not adopted_corrections_valid(engine,current):
        view.update(statement=None,body='',complete=False,state='unavailable',
                    view_notice='已采纳纠正的依据发生变化；请回读原文，不恢复旧结论。')
    return view
