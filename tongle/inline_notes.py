"""Current-agent notes over the existing authoritative cards and publisher.
Host structural checks are not independent semantic review. No generative
writing calls; explicit search may use configured offline retrieval models.
"""
import hashlib
import json
import re
import time
import uuid
from contextlib import contextmanager
from .evolution import Evolution
from .knowledge_files import KnowledgeFiles
from .evidence_selection import terms
from .knowledge_organization import KnowledgeOrganization, ROLES

_AUDIT = 'inline_host_rules_not_independent_review'
_FIELDS = {'search_id','key','title','topic','statement','conditions','scope',
           'authority','status','evidence','id','expected_version','content_type',
           'revision_reason','correction_event_ids','topic_id'}
_PROTECTED = {'authorization','constraints','confirmed_decision','scope'}


def _digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def _text(value,name,limit):
    if not isinstance(value,str) or not value.strip() or len(value)>limit:
        raise ValueError(name+' must be a bounded nonempty string')
    return value


def _normal(value):
    return ' '.join(value.split()).casefold()


def _body(norm):
    body=norm['statement']+'\n\n适用条件：'+('；'.join(norm['conditions']) or '未附加条件')+'\n范围：'+norm['scope']+'\n性质：'+norm['authority']+' / '+norm['status']
    return body+('\n形式：wiki' if norm.get('content_type')=='wiki' else '')


def search_metadata(meta, state, *, effective_correction=False):
    """Expose applicable metadata, never revive an unavailable note's terms.

    ``state`` is the read lifecycle; ``status`` is the claim's declared nature.
    Matching-version metadata is not a truth score. Legacy/manual notes remain
    unverified rather than receiving an invented authority or empty conditions.
    """
    values = dict.fromkeys(('authority', 'status', 'conditions', 'scope'))
    if (state == 'active' or effective_correction) and isinstance(meta, dict):
        values.update({key: meta.get(key) for key in values})
        values['metadata_state'] = 'effective_correction' if effective_correction else 'current'
    else:
        values['metadata_state'] = 'unverified' if state == 'active' else 'unavailable'
    return values


class InlineNotes:
    def __init__(self,engine):
        self.engine=engine; self.db=engine.db
        self.evolution=Evolution(engine);self.cards=self.evolution.cards
        self.files=KnowledgeFiles(engine,self.cards)
        self.organization=KnowledgeOrganization(engine,self.cards)
        with engine.locked(),engine.transaction():
            self.db.execute('CREATE TABLE IF NOT EXISTS inline_note_meta(card_id TEXT NOT NULL,version INTEGER NOT NULL,project TEXT NOT NULL,payload TEXT NOT NULL,audit TEXT NOT NULL,created_at REAL NOT NULL,PRIMARY KEY(card_id,version))')
            self.db.execute('CREATE TABLE IF NOT EXISTS inline_note_events(event_key TEXT NOT NULL,project TEXT NOT NULL,payload_hash TEXT NOT NULL,result TEXT NOT NULL,card_id TEXT,card_version INTEGER,search_id TEXT,created_at REAL NOT NULL,PRIMARY KEY(event_key,project))')
            self.db.execute('CREATE TABLE IF NOT EXISTS inline_note_searches(search_id TEXT NOT NULL,project TEXT NOT NULL,query TEXT NOT NULL,snapshot TEXT NOT NULL,catalog_hash TEXT NOT NULL,created_at REAL NOT NULL,PRIMARY KEY(search_id,project))')
            self.db.execute('CREATE TABLE IF NOT EXISTS inline_note_drafts(draft_id TEXT NOT NULL,project TEXT NOT NULL,event_key TEXT NOT NULL,payload TEXT NOT NULL,payload_hash TEXT NOT NULL,reason TEXT NOT NULL,created_at REAL NOT NULL,PRIMARY KEY(draft_id,project))')
            self.db.execute('CREATE TABLE IF NOT EXISTS inline_note_publish(card_id TEXT NOT NULL,project TEXT NOT NULL,version INTEGER NOT NULL,created_at REAL NOT NULL,PRIMARY KEY(card_id,project,version))')

    @contextmanager
    def _read_scope(self):
        absent = object()
        cache = getattr(self.evolution, '_valid_cache', absent)
        source_cache = getattr(self.engine, '_retrieval_source_checks', absent)
        self.evolution._valid_cache = {}
        self.engine._retrieval_source_checks = {}
        try:
            from .retrieval_budget import verification_scope
            with verification_scope(self.engine):
                yield
        finally:
            if cache is absent:
                del self.evolution._valid_cache
            else:
                self.evolution._valid_cache = cache
            if source_cache is absent:
                del self.engine._retrieval_source_checks
            else:
                self.engine._retrieval_source_checks = source_cache

    def _catalog(self,project):
        return self.cards.list(project,include_inactive=True)

    def _catalog_hash(self,project):
        # Body edits, state changes and human synchronisation update version or
        # updated_at. Fingerprint authoritative metadata without loading bodies.
        # Earlier receipts deliberately become stale and require a fresh search.
        return 'metadata-v2:' + _digest([tuple(row) for row in self.db.execute(
            'SELECT id,version,state,updated_at,path,title,topic FROM knowledge_cards WHERE project=? ORDER BY "key",id', (project,))])

    def _scope(self, project):
        from .evolution import config
        from .knowledge_index import KnowledgeIndex
        cfg = config(self.engine)
        sql, params = KnowledgeIndex.scope_sql(project, cfg)
        return cfg, sql, params

    def _authorized_card(self, project, cid):
        _cfg, scope, params = self._scope(project)
        row = self.db.execute('SELECT k.id FROM knowledge_cards k WHERE k.id=? AND ' + scope, [cid, *params]).fetchone()
        return self.cards.get(row[0]) if row else None

    def _extra_candidates(self, project, query, include_history):
        # Explicit history includes retired assets; the automatic index must
        # never resurrect them. Filter scope and text in SQL before a fixed limit.
        _cfg, scope, params = self._scope(project)
        tokens = sorted(terms(query))[:128]
        if not tokens:
            return []
        state = "((k.state='conflict' OR k.kind IN ('source','index')) AND k.state!='retired')"
        if include_history:
            state = "(k.state!='active' OR k.kind IN ('source','index'))"
        text = "lower(k.title || ' ' || k.\"key\" || ' ' || k.topic || ' ' || k.body)"
        matches = ' OR '.join('instr(' + text + ',?)>0' for _ in tokens)
        rows = self.db.execute('SELECT k.id FROM knowledge_cards k WHERE ' + scope + ' AND ' + state +
            ' AND (' + matches + ') ORDER BY k.id LIMIT 64', [*params, *tokens]).fetchall()
        return [self.cards.get(row[0]) for row in rows]

    def _sync_candidates(self, candidates, recover_missing=False):
        from .retrieval_freshness import candidate_dependencies
        ids, complete = candidate_dependencies(self.cards, candidates)
        started = time.perf_counter()
        sync = self.files.sync(ids)
        recovery = bool(recover_missing and sync.get('missing'))
        if recovery:
            # An explicit identity read retains the established moved-asset
            # recovery contract. Ordinary search never enters this branch.
            sync = self.files.sync()
        if sync.get('changed'):
            self.files.publish(ids)
        return complete, {'dependency_complete': complete, 'checked_cards': len(ids),
            'discovery_recovery': recovery, 'freshness_ms': round((time.perf_counter()-started)*1000, 3)}

    def _meta(self,card):
        row=self.db.execute('SELECT payload FROM inline_note_meta WHERE card_id=? AND version=?',(card['id'],card['version'])).fetchone()
        if not row:return None
        try:meta=json.loads(row[0])
        except (TypeError,ValueError):return None
        if not isinstance(meta,dict):return None
        if meta.get('body_hash')!=_digest(card['body']):return None
        count=meta.get('statement_chars')
        if type(count) is not int or count<1:return None
        statement=card['body'][:count]
        if _digest(statement)!=meta.get('statement_hash'):return None
        # Reconstruct the public projection; no second authoritative body is stored.
        return dict(meta,statement=statement,evidence=card['evidence'],
                    content_type=meta.get('content_type','insight'))

    def _correction(self,project,cid,include_adopted=False):
        from .source_memory import SourceMemory
        from .knowledge_learning import correction_adopted
        # Read-time suppression includes every cited ancestor, without jobs or writes.
        todo=[cid];seen=set();scopes={project}
        while todo:
            current=todo.pop()
            if current in seen:continue
            seen.add(current);card=self.cards.get(current)
            if not card or card['project'] not in (project,'shared'):continue
            from .retrieval_budget import verify_identity
            if not verify_identity(self.engine,'card',card['id'],card['version']):
                return [{'target_card_id':cid,'effective':False,'adopted':False,
                    'verification_unknown':True,'status':'unverified',
                    'reason':'verification_budget_exceeded','quote':'','replacement':''}]
            scopes.add(card['project'])
            todo.extend(ref['card_id'] for ref in card['basis_cards'])
            from .knowledge_organization import EVIDENCE_RELATIONS
            todo.extend(row[0] for row in self.db.execute('SELECT target_id FROM organization_relations WHERE owner_id=? AND relation IN (' + ','.join('?' for _ in EVIDENCE_RELATIONS) + ')', (current, *EVIDENCE_RELATIONS)))
        memory=SourceMemory(self.engine)
        target=self.cards.get(cid)
        rows=[dict(row,adopted=correction_adopted(self.engine,target,row))
              for scope in sorted(scopes) for row in memory.corrections(scope,False,target_ids=seen)
              if row.get('verification_unknown') or
                  (row['target_card_id'] in seen and (row['effective'] or include_adopted))]
        return rows if include_adopted else [row for row in rows if row.get('verification_unknown') or (row['effective'] and not row['adopted'])]

    def _read_state(self,card,corrections):
        from .knowledge_learning import adopted_corrections_valid
        from .retrieval_budget import verification_report
        if any(row.get('verification_unknown') for row in corrections) or verification_report(self.engine).get('verification_budget_exhausted'):
            return 'unavailable'
        if any(row['effective'] and not row.get('adopted',False) for row in corrections):return 'corrected'
        if not adopted_corrections_valid(self.engine,card):return 'unavailable'
        try:
            if self.evolution._valid_card(card):return 'active'
        except (KeyError,ValueError):
            pass
        return card['state'] if card['state']!='active' else 'unavailable'

    def _public_corrections(self, project, rows):
        # Ancestors can invalidate a note without granting permission to read
        # their correction text or identities. All public read/write feedback
        # must use this projection; internal validity checks keep full rows.
        from .retrieval_freshness import card_visible
        return [row if card_visible(self.engine,project,self.cards.get(row.get('target_card_id')))
            else {'effective':row['effective'],'adopted':row.get('adopted',False),
                  'scope':'restricted_dependency','notice':'依据当前不可用或已变更；旧结论已暂停。'}
            for row in rows]

    def search(self,project,query,include_history=False,*,subquestions=None,intent=None):
        _text(project,'project',200);_text(query,'query',4000)
        if type(include_history) is not bool:raise ValueError('include_history must be boolean')
        from .knowledge_index import KnowledgeIndex
        from .semantic_index import semantic_candidates
        from .retrieval_plan import build_query_plan
        plan=build_query_plan(query,subquestions=subquestions,intent=intent)
        with self.engine.locked(), self._read_scope():
            cfg, scope, params = self._scope(project)
            index = KnowledgeIndex(self.engine, self.cards)
            catalog, retrieval = index.candidates_many(project, plan, cfg)
            catalog, semantic = semantic_candidates(index, project, query, cfg, catalog,plan=plan)
            retrieval.update(semantic)
            seen = {card['id'] for card in catalog}
            for card in self._extra_candidates(project, query, include_history):
                if card['id'] not in seen:
                    catalog.append(card); seen.add(card['id'])
            complete, freshness = self._sync_candidates(catalog)
            from .graph_retrieval import filter_graph_candidates
            catalog=filter_graph_candidates(self.engine,project,catalog)
            retrieval.update(freshness)
            if not complete:
                retrieval['degraded'] = 'dependency_budget_exceeded'
            count = self.db.execute('SELECT count(*) FROM knowledge_cards k WHERE ' + scope, params).fetchone()[0]
            with self.engine.transaction():
                rows=[];tokens=terms(query)
                for candidate in catalog:
                    card = self.cards.get(candidate['id'])
                    if not card or (card['state']=='retired' and not include_history):continue
                    if candidate['version']!=card['version']:
                        candidate=card  # Old-body ranking cannot describe a new version.
                    aliases = candidate.get('_topic_aliases', [])
                    alias_text = aliases if isinstance(aliases, str) else ' '.join(a for a in aliases if isinstance(a, str)) if isinstance(aliases, list) else ''
                    score=len(tokens&terms(card['title']+' '+card['key']+' '+card['body']+' '+alias_text))
                    if not score and '_semantic_score' not in candidate and not candidate.get('_semantic_query_matches'):continue
                    meta=self._meta(card);corrected=self._correction(project,card['id'],True) if complete else []
                    state=self._read_state(card,corrected) if complete else 'unavailable'
                    rows.append({'id':card['id'],'version':card['version'],'key':card['key'],
                        'title':card['title'],'state':state,'project':card['project'],
                        'read_only':card['project']!=project,'historical':card['state']=='retired',
                        'statement':None if state!='active' else (meta['statement'] if meta else card['body'])[:300],
                        'complete':False,'excerpt':True,
                        **search_metadata(meta,state),
                        'organization':self.organization.describe(card),
                        'corrections':self._public_corrections(project,corrected),'score':score,
                        **({'graph_paths':candidate['_graph_paths']} if candidate.get('_graph_paths') else {}),
                        **{key:candidate[key] for key in ('_semantic_score','_rerank_score') if key in candidate},
                        'query_matches':[{ 'query_id':qid,
                            'status':'candidate_found','read_offset':match.get('body_start'),
                            'body_excerpt_end':match.get('body_end'),
                            'rerank_score':match.get('rerank_score'),
                            'semantic_rank':match.get('semantic_rank'),
                            'lexical_rank':match.get('lexical_rank'),
                            **({'discovery':'graph','graph_rank':match['graph_rank'],
                                'provenance':[path for path in candidate.get('_graph_paths',[])
                                    if path.get('query_id')==qid]} if 'graph_rank' in match else {})}
                            for qid,match in candidate.get('_semantic_query_matches',{}).items()]})
                # Explicit search offers candidates for the user's next read. It
                # does not apply the stricter automatic-injection admission gate.
                rows.sort(key=lambda r:(-max([r.get('_rerank_score',-1000),
                    *(m['rerank_score'] for m in r['query_matches'] if m['rerank_score'] is not None)]),
                    -r.get('_semantic_score', -1), -r['score'],r['id']))
                ordered=[]; retained=set()
                def question_rank(row,qid):
                    match=next(m for m in row['query_matches'] if m['query_id']==qid)
                    score=match['rerank_score']
                    return (score is None,-score if score is not None else 0,
                        match['semantic_rank'] or 100000,match['lexical_rank'] or 100000,row['id'])
                by_question={q.id:sorted(
                    [r for r in rows if any(m['query_id']==q.id for m in r['query_matches'])],
                    key=lambda r:question_rank(r,q.id)) for q in plan.queries}
                # Graph discovery has no invented lexical/dense score. Give a
                # graph-only route a bounded turn alongside each question's
                # ordinary route, without changing the eight-note budget or
                # the order when no graph-only candidate exists.
                routes=[]
                for q in plan.queries:
                    def graph_only(row):
                        match=next(m for m in row['query_matches'] if m['query_id']==q.id)
                        return ('graph_rank' in match and match['lexical_rank'] is None
                                and match['semantic_rank'] is None)
                    ordinary=[row for row in by_question[q.id] if not graph_only(row)]
                    graph=sorted((row for row in by_question[q.id] if graph_only(row)),
                        key=lambda row:(next(m['graph_rank'] for m in row['query_matches']
                            if m['query_id']==q.id),row['id']))
                    routes.append(ordinary)
                    if graph:routes.append(graph)
                for position in range(8):
                    for choices in routes:
                        if position<len(choices) and choices[position]['id'] not in retained:
                            ordered.append(choices[position]); retained.add(choices[position]['id'])
                            if len(ordered)==8:break
                    if len(ordered)==8:break
                rows=(ordered+[r for r in rows if r['id'] not in retained])[:8]
                retrieval['query_results']=[dict(query_id=q.id,
                    status='candidate_found' if any(m['query_id']==q.id for r in rows for m in r['query_matches']) else 'unresolved',
                    candidate_ids=[r['id'] for r in rows if any(m['query_id']==q.id for m in r['query_matches'])])
                    for q in plan.queries]
                retrieval['query_results_scope']='notes'
                retrieval['answer_coverage']='not_assessed'
                from .retrieval_budget import verification_report
                retrieval.update(verification_report(self.engine))
                snapshot={r['id']:r['version'] for r in rows};sid=uuid.uuid4().hex
                cat_hash=self._catalog_hash(project)
                self.db.execute('INSERT INTO inline_note_searches VALUES(?,?,?,?,?,?)',(sid,project,query,json.dumps(snapshot),cat_hash,time.time()))
                return {'search_id':sid,'project':project,'candidates':rows,'catalog_hash':cat_hash,
                    'snapshot':snapshot,'available_note_count':count,'retrieval':retrieval,
                    'read_notice':'候选与排名不代表问题已回答。逐项核对所问事项，读取必要全文和条件；未找到依据的部分明确说明未知。'}

    def get(self,project,cid,offset=None):
        _text(project,'project',200);_text(cid,'id',200)
        if offset is not None and (type(offset) is not int or offset<0):raise ValueError('invalid note offset')
        with self.engine.locked(), self._read_scope():
            card=self._authorized_card(project,cid)
            if not card:raise ValueError('card not found in project')
            complete_dependencies, freshness = self._sync_candidates([card], recover_missing=True)
            card = self.cards.get(cid)
            corrections=self._correction(project,cid,True) if complete_dependencies else []
            state=self._read_state(card,corrections) if complete_dependencies else 'unavailable'
            relations=self.organization.list_relations(card['project'],cid)
            if state in ('corrected','unavailable'):
                relations=[dict(relation,state=state,requires_review=True,
                    evidence=[{key:ref[key] for key in ('source_id','version') if key in ref} for ref in relation['evidence']],
                    owner_read_state=state) if relation['category']=='evidence' else relation for relation in relations]
            readable=state=='active' and (offset is not None or len(card['body'])<=12000)
            # Explicit pagination may inspect a retired asset, never revive it.
            if offset is not None and state=='retired':
                # Historical inspection uses a transient active view. Do not
                # reuse/cache that result under the real retired card identity.
                previous_cache = self.evolution._valid_cache
                self.evolution._valid_cache = {}
                try:readable=self.evolution._valid_card(dict(card,state='active'))
                except (KeyError,ValueError):readable=False
                finally:self.evolution._valid_cache = previous_cache
            body=card['body'] if readable else '';meta=self._meta(card) if readable else None
            public_corrections=self._public_corrections(project,corrections)
            result={'id':cid,'project':card['project'],'requested_project':project,
                'read_only':card['project']!=project,'version':card['version'],'title':card['title'],
                'key':card['key'],'topic':card['topic'],
                'state':state,'historical':card['state']=='retired','body':body,'complete':readable,
                'path':str(self.files.root/card['path']) if card.get('path') else None,
                'metadata':meta,'corrections':public_corrections,
                'organization':self.organization.describe(card),
                'relations':relations,'retrieval':freshness,
                'evidence':card['evidence'] if readable else [
                    {k:ref[k] for k in ('source_id','version')} for ref in card['evidence']]}
            from .retrieval_budget import verification_report
            result['retrieval'].update(verification_report(self.engine))
            if offset is not None:
                if readable and (offset>len(body) or (body and offset==len(body))):
                    raise ValueError('note offset outside text; start at 0 and follow next_offset')
                end=min(len(body),offset+4000) if readable else offset
                result.update(body=body[offset:end] if readable else '',offset=offset,
                    total_chars=len(body) if readable else None,
                    next_offset=end if readable and end<len(body) else None,
                    complete=readable and offset==0 and end==len(body),complete_scope='whole_note')
                # Do not defeat pagination by echoing the complete statement.
                if meta:result['metadata']={key:value for key,value in meta.items() if key!='statement'}
            return result

    def _validate(self,payload,kind):
        if not isinstance(payload,dict) or set(payload)-_FIELDS:raise ValueError('invalid payload fields')
        norm={key:_text(payload.get(key),key,bound) for key,bound in
              [('key',200),('title',400),('topic',200),('statement',6000),('scope',400)]}
        conditions=payload.get('conditions',[])
        if not isinstance(conditions,list) or len(conditions)>8:raise ValueError('invalid conditions')
        norm['conditions']=[_text(c,'condition',400) for c in conditions]
        authority=payload.get('authority');status=payload.get('status')
        if authority not in ('user','external','assistant_inference') or status not in ('confirmed','reported','hypothesis'):
            raise ValueError('invalid authority or status')
        if authority=='assistant_inference' and status!='hypothesis':raise ValueError('assistant inference must remain hypothesis')
        if authority=='external' and status!='reported':raise ValueError('external claims must remain reported')
        norm.update(authority=authority,status=status)
        # Omitted additions stay out of the event digest, preserving old retries.
        if 'content_type' in payload:
            if payload['content_type'] not in ROLES:raise ValueError('invalid content_type')
            norm['content_type']=payload['content_type']
        if 'topic_id' in payload:
            norm['topic_id']=_text(payload['topic_id'],'topic_id',200)
        if 'revision_reason' in payload:
            if kind!='update':raise ValueError('revision_reason requires an update')
            norm['revision_reason']=_text(payload['revision_reason'],'revision_reason',800)
        if 'correction_event_ids' in payload:
            ids=payload['correction_event_ids']
            if kind!='update' or not isinstance(ids,list) or not 1<=len(ids)<=12:
                raise ValueError('invalid correction_event_ids')
            ids=[_text(value,'correction_event_id',200) for value in ids]
            if len(set(ids))!=len(ids):raise ValueError('duplicate correction_event_ids')
            norm['correction_event_ids']=ids
        evidence=payload.get('evidence')
        if not isinstance(evidence,list) or not 1<=len(evidence)<=12:raise ValueError('real evidence is required (1 to 12 entries)')
        norm['evidence']=[]
        for ref in evidence:
            if not isinstance(ref,dict) or set(ref)!={'source_id','version','quote'}:raise ValueError('invalid evidence fields')
            if type(ref['version']) is not int or ref['version']<1:raise ValueError('invalid evidence version')
            norm['evidence'].append({'source_id':_text(ref['source_id'],'source_id',200),
                'version':ref['version'],'quote':_text(ref['quote'],'quote',2000)})
        if kind=='update':
            norm['id']=_text(payload.get('id'),'id',200)
            ver=payload.get('expected_version')
            if type(ver) is not int or ver<1:raise ValueError('invalid expected_version')
            norm['expected_version']=ver
        elif 'id' in payload or 'expected_version' in payload:
            raise ValueError('remember cannot include an update target')
        return norm

    def _evidence(self,project,norm):
        originals=[]
        for ref in norm['evidence']:
            self.cards._evidence_entry(ref,project)
            source=self.engine.store.source(ref['source_id'],ref['version']);originals.append(source)
            if norm['authority']=='user' and source['role']!='user':raise ValueError('user authority requires user evidence')
            if norm['authority']=='external' and source['role']!='document':raise ValueError('external authority requires document evidence')
        return originals

    def _revision_guard(self,card,old,norm):
        """A narrow evidence-backed exception, never a semantic approval."""
        if (old['authority'],old['status'],norm['authority'],norm['status']) != (
                'assistant_inference','hypothesis','assistant_inference','hypothesis'):
            return 'revision_requires_assistant_hypothesis'
        if not norm.get('revision_reason'):return 'revision_reason_required'
        used={(ref['source_id'],ref['version']) for ref in card['evidence']}
        for revision in self.cards.history(card['id']):
            used.update((ref['source_id'],ref['version']) for ref in revision['snapshot']['evidence'])
        # _evidence has already checked live availability, version and exact quote.
        if not any((ref['source_id'],ref['version']) not in used for ref in norm['evidence']):
            return 'revision_requires_new_source_version'
        return None

    def _adoption_guard(self,card,norm,corrections):
        ids=norm.get('correction_event_ids',[])
        if not corrections or any(row['target_card_id']!=card['id'] for row in corrections):
            return 'correction_adoption_target_unresolved'
        if set(ids)!={row['event_key'] for row in corrections}:
            return 'correction_adoption_requires_all_effective_events'
        for row in corrections:
            ref={'source_id':row['source_id'],'version':row['source_version'],'quote':row['quote']}
            if ref not in norm['evidence']:return 'correction_adoption_requires_exact_evidence'
            if row['replacement'] not in norm['statement']:return 'correction_adoption_requires_verbatim_replacement'
        return None

    def _confirm_ok(self,norm,originals):
        if len(originals)!=1:return False
        source=originals[0];text=source['text']
        return (source['role']=='user' and norm['statement']==norm['evidence'][0]['quote']==text
            and len(text)<=400 and '\n' not in text and '\r' not in text
            and text.startswith(('我决定','我采用','以后按','就按这个','我确定','就这么办','我要求'))
            and not re.search(r'转[述贴发]|他说|她说|反馈|据说|听说|引用|原文|[>「」“”‘’\'"`]',text)
            and (norm['scope'] in (source['project'],'project','当前项目','本项目') or norm['scope'] in text)
            and all(condition in text for condition in norm['conditions']))

    def _search_check(self,project,search_id,norm,kind):
        _text(search_id,'search_id',200)
        row=self.db.execute('SELECT * FROM inline_note_searches WHERE project=? AND search_id=?',(project,search_id)).fetchone()
        if not row:raise ValueError('unknown project-bound search_id; search first')
        snapshot=json.loads(row['snapshot'])
        if kind=='update' and norm['id'] not in snapshot:raise ValueError('update target not in search candidates')
        return row

    def _duplicates(self,project,norm):
        hits=[]
        for card in self._catalog(project):
            meta=self._meta(card);statement=meta['statement'] if meta else card['body']
            if any(_normal(a)==_normal(b) for a,b in
                   [(card['key'],norm['key']),(card['title'],norm['title']),(statement,norm['statement'])]):
                hits.append({'id':card['id'],'version':card['version'],'title':card['title'],'state':card['state']})
        return hits[:20]

    def _record(self,project,event_key,digest,result,search_id,cid=None,version=None):
        self.db.execute('INSERT INTO inline_note_events VALUES(?,?,?,?,?,?,?,?)',
            (event_key,project,digest,json.dumps(result,ensure_ascii=False),cid,version,search_id,time.time()))

    def _draft_feedback(self,project,norm,result):
        # A storage guard is not a request to repeat a decision in magic words.
        # Resolution still uses the original write guards; drafts are not recall.
        result=dict(result,note_update_status='draft',draft_state='open',
            repeat_confirmation_required=False,
            next_action={'action':'drafts','draft_id':result['draft_id']},
            notice='本次笔记写入尚未应用。按需要查看草稿原因并修正提案；不要仅为改变记忆标签要求用户复述。')
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='inline_draft_states'").fetchone():
            state=self.db.execute('SELECT status FROM inline_draft_states WHERE project=? AND draft_id=?',
                (project,result['draft_id'])).fetchone()
            if state:
                result['draft_state']=state['status']
                if state['status']!='open':
                    result['notice']='这是原草稿事件的回放；草稿当前状态为'+state['status']+'，用drafts查看后续处理结果。'
        if norm.get('id'):
            if result.get('reason')=='scope_or_conditions_change_is_risky':
                card=self.cards.get(norm['id'])
                current=self._meta(card) if card and card['project']==project else None
                if current and current['scope']!=norm['scope']:
                    result.update(current_scope=current['scope'],proposed_scope=norm['scope'],
                        mismatched_fields=['scope'],
                        retry_hint='范围与当前笔记不一致。沿用 get 的 metadata.scope；适用条件的修订写入 conditions，不要改写 scope。修正完整提案后用新的 search_id 和事件再次 update；原草稿可 dismiss，resolve retry 不能修改原草稿的 scope。条件修订仍须 revision_reason 和新的有效来源证据。')
            corrections=[row for row in self._correction(project,norm['id'],True) if row['effective']]
            if corrections:
                result=dict(result,correction_status='effective',effective_corrections=self._public_corrections(project,corrections),
                    note_update_status='draft',user_action_required=False,
                    notice='纠正已经独立生效，无需再次确认；笔记更新草稿尚未发布，不要将草稿状态表述为纠正未生效。')
        return result

    def _draft(self,project,event_key,norm,digest,reason,search_id):
        did='draft:'+_digest([project,event_key,digest])[:40]
        result=self._draft_feedback(project,norm,{'status':'draft','draft_id':did,'reason':reason,'replayed':False})
        self.db.execute('INSERT INTO inline_note_drafts VALUES(?,?,?,?,?,?,?)',
            (did,project,event_key,json.dumps(norm,ensure_ascii=False),digest,reason,time.time()))
        self._record(project,event_key,digest,result,search_id)
        return result

    def _publish(self,project,event_key,result):
        cid=result['id'];version=result['version'];error=None
        # Never enter this method from a DB transaction: publication commits its intent.
        if self.db.in_transaction:raise RuntimeError('publication must follow commit')
        try:
            report=self.files.publish([cid]);current=self.cards.get(cid)
            receipt=self.files._tracking(cid)
            verified=(not report['conflicts'] and not report['missing'] and current is not None
                and receipt is not None and receipt['version']==current['version'] and receipt['state']=='ok')
            superseded=bool(verified and current['version']>version)
            ok=bool(verified and current['version']==version)
            if not ok and not superseded:error='publication_not_verified'
        except (OSError,ValueError) as exc:
            ok=False;superseded=False;error=type(exc).__name__
        result=dict(result,publication='superseded' if superseded else ('ok' if ok else 'publication_pending'))
        if superseded:result['published_current_version']=current['version']
        if error:result['publication_error']=error
        else:result.pop('publication_error',None)
        with self.engine.locked(),self.engine.transaction():
            if ok or superseded:self.db.execute('DELETE FROM inline_note_publish WHERE card_id=? AND project=? AND version=?',(cid,project,version))
            self.db.execute('UPDATE inline_note_events SET result=? WHERE project=? AND event_key=?',
                (json.dumps(result,ensure_ascii=False),project,event_key))
        return result

    def _apply(self,project,event_key,payload,kind):
        _text(project,'project',200);_text(event_key,'event_key',200)
        from .memory_runtime import allowed
        if allowed(self.engine,project):raise ValueError('inline writes require memory mode for this project')
        norm=self._validate(payload,kind);digest=_digest([kind,norm]);search_id=payload.get('search_id')
        with self.engine.locked():
            prior=self.db.execute('SELECT * FROM inline_note_events WHERE project=? AND event_key=?',(project,event_key)).fetchone()
            if prior:
                if prior['payload_hash']!=digest:raise ValueError('event_key reused with different action or content')
                result=dict(json.loads(prior['result']),replayed=True)
                if result.get('status')=='draft':result=self._draft_feedback(project,norm,result)
                return self._publish(project,event_key,result) if result.get('publication')=='publication_pending' else result
            self.files.sync()
            with self.engine.transaction():
                search=self._search_check(project,search_id,norm,kind)
                old=None;card=None;corrections=[]
                if kind=='update':
                    card=self.cards.get(norm['id'])
                    if card and card['project']=='shared' and project!='shared':
                        raise ValueError('shared cards are read-only')
                    if not card or card['project']!=project:raise ValueError('card not found in project')
                    if card['state']=='retired':raise ValueError('cannot modify retired card')
                    old=self._meta(card)
                    if card['version']!=norm['expected_version']:
                        return {'status':'conflict','current_version':card['version'],
                            'diff':{'current_body':card['body'],'proposed_body':_body(norm)},'replayed':False}
                    if card['key']!=norm['key']:raise ValueError('update cannot change key')
                    if old and 'content_type' not in norm:norm['content_type']=old.get('content_type','insight')
                if search['catalog_hash']!=self._catalog_hash(project):raise ValueError('catalog changed; search again')
                if 'topic_id' in norm:
                    topic=self.organization.resolve_topic(project,norm['topic_id'])
                    named=self.organization.resolve_topic(project,norm['topic'])
                    if not topic or topic['id']!=norm['topic_id'] or not named or named['id']!=topic['id']:
                        raise ValueError('topic_id and topic must identify the same registered topic in project')
                    if card:
                        previous=self.organization.describe(card)
                        if previous['topic_id'] and previous['topic_id']!=topic['id']:
                            raise ValueError('topic identity change requires explicit review')
                if card:
                    previous=self.organization.describe(card)
                    named=self.organization.resolve_topic(project,norm['topic'])
                    if previous['topic_id'] and (not named or named['id']!=previous['topic_id']):
                        raise ValueError('topic identity change requires explicit review')
                originals=self._evidence(project,norm)
                reason=None
                if norm['authority']=='user' and norm['status']=='confirmed' and not self._confirm_ok(norm,originals):
                    reason='user_confirmation_not_verbatim_decision'
                if kind=='update':
                    if old is None:reason='legacy_or_untracked_card'
                    elif card['manually_edited']:reason='manually_edited_card'
                    elif old['status']=='confirmed' or card['key'] in _PROTECTED:reason='confirmed_decision_protected'
                    elif old['authority']!=norm['authority']:reason='source_authority_change_requires_review'
                    elif old['scope']!=norm['scope']:reason='scope_or_conditions_change_is_risky'
                    elif old.get('content_type','insight')!=norm.get('content_type','insight'):reason='content_type_change_requires_review'
                    else:
                        corrections=[row for row in self._correction(project,card['id'],True) if row['effective']]
                        revising=(old['conditions']!=norm['conditions'] or 'revision_reason' in norm
                                  or 'correction_event_ids' in norm)
                        if revising:reason=self._revision_guard(card,old,norm)
                        if not reason and old.get('adopted_corrections') and not corrections:
                            reason='adopted_correction_no_longer_effective'
                        if not reason and corrections:
                            reason=(self._adoption_guard(card,norm,corrections) if 'correction_event_ids' in norm
                                    else 'corrected_card_requires_explicit_resolution')
                        elif not reason and 'correction_event_ids' in norm:
                            reason='correction_adoption_target_unresolved'
                elif norm['key'] in _PROTECTED and not (norm['authority']=='user' and norm['status']=='confirmed' and not reason):
                    reason='protected_semantics_require_user_decision'
                if reason:return self._draft(project,event_key,norm,digest,reason,search_id)
                if kind=='create':
                    duplicates=self._duplicates(project,norm)
                    if duplicates:
                        result={'status':'needs_update','cards':duplicates,'replayed':False}
                        self._record(project,event_key,digest,result,search_id);return result
                op={'op':kind,'body':_body(norm),'title':norm['title'],'topic':norm['topic'],
                    'evidence':norm['evidence'],'basis_cards':[],'reason':_AUDIT}
                if kind=='create':op.update(key=norm['key'],kind='knowledge')
                else:op.update(id=card['id'],expected_version=norm['expected_version'])
                ops=[op];review={'approved':True,'findings':[],'proposal_hash':_digest(ops),'validation':_AUDIT}
                receipt=self.cards.apply('inline:'+_digest([project,event_key]),project,ops,review)
                cid=receipt['changed_ids'][0] if receipt['changed_ids'] else card['id']
                saved=self.cards.get(cid);version=saved['version']
                organization=self.organization.bind_card(saved,role=norm.get('content_type','insight'),topic_id=norm.get('topic_id'))
                meta={k:norm[k] for k in ('conditions','scope','authority','status','key')}
                meta.update(content_type=norm.get('content_type','insight'),revision_reason=norm.get('revision_reason'))
                meta['topic_id']=organization['topic_id']
                meta.update(statement_chars=len(norm['statement']),statement_hash=_digest(norm['statement']),
                    body_hash=_digest(saved['body']),as_of=time.time(),last_source_checked=time.time(),semantic_verified=False)
                if corrections:
                    from .knowledge_learning import adoption_record
                    meta['adopted_corrections']=[adoption_record(saved,row) for row in corrections]
                if not self.db.execute('SELECT 1 FROM inline_note_meta WHERE card_id=? AND version=?',(cid,version)).fetchone():
                    self.db.execute('INSERT INTO inline_note_meta VALUES(?,?,?,?,?,?)',(cid,version,project,json.dumps(meta,ensure_ascii=False),_AUDIT,time.time()))
                result={'status':'applied','id':cid,'version':version,'key':saved['key'],'replayed':False,
                    'authority':norm['authority'],'note_status':norm['status'],'validation':_AUDIT,'publication':'publication_pending',
                    'repeat_confirmation_required':False}
                self.db.execute('INSERT OR IGNORE INTO inline_note_publish VALUES(?,?,?,?)',(cid,project,version,time.time()))
                self._record(project,event_key,digest,result,search_id,cid,version)
            return self._publish(project,event_key,result)

    def remember(self,project,event_key,payload):
        return self._apply(project,event_key,payload,'create')

    def update(self,project,event_key,payload):
        return self._apply(project,event_key,payload,'update')
