"""Transient source candidates share the note query plan and admission gate.

These views never create notes or upgrade the authority of a source excerpt.
The caller combines them with notes before automatic admission. Explicit reads
remain possible even when no excerpt is suitable for automatic delivery.
"""
import sqlite3

from .memory_relevance import query_terms
from .retrieval_freshness import source_current


def lexical_candidates(cards,query,project,prior_provider=None):
    """Use note-heading rules for notes and exact-source rules for excerpts.

    Raw speech has no authored title/key. Manufacturing one from the question
    would incorrectly lend it the question's identity. Both routes still feed
    the same final applicability and (when configured) model admission gate.
    """
    from .memory_selection import select_memory_topic
    from .memory_relevance import relevant,filter_applicable
    notes=[c for c in cards if '_raw_source' not in c]
    raw=[c for c in cards if '_raw_source' in c and relevant(query,c['body'],project)]
    return select_memory_topic(notes,query,project,prior_provider)+filter_applicable(raw,query,project)


def discover_raw(memory, project, plan, *, exclude_source_id=None, roles=('user', 'document'), candidate_limit=64, verification_limit=None):
    if type(candidate_limit) is not int or not 1 <= candidate_limit <= 64:
        raise ValueError('raw candidate limit must be between 1 and 64')
    if verification_limit is not None and (type(verification_limit) is not int or not 1 <= verification_limit <= 64):
        raise ValueError('raw verification limit must be between 1 and 64')
    groups = {}
    for question in plan.queries:
        tokens = query_terms(question.text, project)
        groups[question.id] = memory.search(
            project, question.text[:4000], roles=roles, query_tokens=tokens,
            exclude_source_id=exclude_source_id, candidate_limit=candidate_limit) if tokens else []
    # Merge only after each question has its own recall route. Round-robin
    # keeps a large first question from consuming every candidate slot.
    merged = {}; per_query = {q.id: [] for q in plan.queries}
    checked_sources = {}
    for position in range(candidate_limit):
        for question in plan.queries:
            rows = groups[question.id]
            if position >= len(rows):
                continue
            row = rows[position]
            key = (row['source_id'], row['version'], row['offset'])
            if key not in merged:
                if len(merged) >= candidate_limit:
                    continue
                source_key = (row['source_id'], row['version'])
                if source_key not in checked_sources:
                    # Invalid/moved sources also consume discovery capacity;
                    # extra subquestions cannot spend every verification slot
                    # by continually replacing rejected raw candidates.
                    if verification_limit is not None and len(checked_sources) >= verification_limit:
                        continue
                    checked_sources[source_key] = source_current(memory.engine, row['source_id'])
                if not checked_sources[source_key]:
                    continue
                cid = f"raw:{row['source_id']}@{row['version']}:{row['offset']}"
                merged[key] = {
                    'id': cid, 'version': row['version'], 'project': project,
                    'kind': 'memory', 'state': 'active', 'title': '', 'key': '',
                    'topic': '', 'body': row['snippet'], 'evidence': [],
                    'basis_cards': [], 'updated_at': 0, '_raw_source': row,
                    '_semantic_query_matches': {},
                }
            card = merged[key]
            card['_semantic_query_matches'][question.id] = {
                'lexical_rank': position, 'passage': row['snippet'],
                'start': row['offset'], 'end': row['offset'] + row['length'],
            }
            per_query[question.id].append(card)
    return list(merged.values()), per_query


def score_raw(groups, plan, config, *, pair_budget=6):
    cfg = config.get('retrieval', {})
    if not isinstance(cfg, dict) or cfg.get('strategy') != 'rerank':
        cards = {c['id']: c for rows in groups.values() for c in rows}
        return list(cards.values()), {'rerank_pairs': 0, 'input_tokens_upper_bound': 0}
    from .semantic_index import rerank_candidate_groups
    return rerank_candidate_groups(groups, plan, cfg.get('reranker',{}),
        rerank_budget=pair_budget, token_budget=pair_budget * 512)


def excerpt(card):
    row = card['_raw_source']
    return dict(source_id=row['source_id'], version=row['version'], role=row['role'],
                created_at=row['created_at'], text=row['snippet'], offset=row['offset'],
                status='raw_evidence', complete=False)


def search_source_view(card):
    """Keep source identity and per-question discovery in the public search.

    Do not return the internal synthetic note or its raw passage a second time.
    These offsets are source offsets for ``source``, not note offsets for get.
    """
    row = card['_raw_source']
    return dict(row, candidate_id=card['id'], query_matches=[{
        'query_id': qid, 'status': 'candidate_found',
        'read_offset': match['start'], 'excerpt_end': match['end'],
        'lexical_rank': match.get('lexical_rank'),
    } for qid, match in card.get('_semantic_query_matches', {}).items()])


def source_acquisition_view(db, project, source_id, version):
    """Ancillary acquisition declarations never make an available body vanish.

    The caller must already have authorized the exact source and version. This
    does not initialize a schema or certify a caller's coverage declaration.
    """
    from .source_acquisition import acquisition_provenance
    try:
        return acquisition_provenance(db, project, source_id, version)
    except (OSError, ValueError, sqlite3.Error) as exc:
        return dict(state='unavailable', coverage_assessment='unknown',
                    error=type(exc).__name__)


def search_coverage(found, plan):
    """Describe only returned candidates; index/verification gaps stay unknown.

    Keep the legacy notes-only query_results intact. Neither finding a candidate
    nor an available index proves that an answer has been read or is complete.
    """
    retrieval = found['retrieval']
    graph = retrieval.get('graph', {})
    graph_partial = isinstance(graph, dict) and bool(
        graph.get('unavailable') or graph.get('pending') or
        graph.get('index_edge_truncated') or graph.get('link_truncated_cards') or
        graph.get('reason') == 'invalid_configuration')
    notes_partial = bool(retrieval.get('degraded') or
        retrieval.get('dependency_complete') is False or
        retrieval.get('verification_budget_exhausted') or graph_partial)
    sources_partial = bool(found.get('source_search_complete') is not True or
        found.get('source_index_errors') or
        retrieval.get('verification_budget_exhausted'))
    source_results = []
    combined = []
    for question in plan.queries:
        qid = question.id
        notes = [dict(id=row['id'], version=row['version'], state=row['state'],
                      historical=row.get('historical', False))
                 for row in found['candidates']
                 if any(m['query_id'] == qid for m in row.get('query_matches', []))]
        sources = [dict(source_id=row['source_id'], version=row['version'],
                        offset=row['offset'], length=row['length'])
                   for row in found.get('sources', [])
                   if any(m['query_id'] == qid for m in row.get('query_matches', []))]
        source_results.append(dict(query_id=qid, sources=sources,
            status='candidate_found' if sources else ('unknown' if sources_partial else 'unresolved'),
            discovery_state='partial' if sources_partial else 'available'))
        combined.append(dict(query_id=qid, notes=notes, sources=sources,
            status='candidate_found' if notes or sources else (
                'unknown' if notes_partial or sources_partial else 'unresolved'),
            discovery_state='partial' if notes_partial or sources_partial else 'available',
            answer_coverage='not_assessed'))
    return dict(query_results_scope='notes', coverage_schema_version=1,
        combined_query_results_scope='returned_note_and_source_candidates',
        source_query_results=source_results, combined_query_results=combined,
        answer_coverage='not_assessed')
