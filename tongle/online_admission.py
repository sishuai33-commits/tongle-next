"""One optional online relevance decision, after local permission/freshness checks.

Selection is a retrieval hint, never evidence coverage or an authority upgrade.
No cache, retry, provider credentials, persistence or background work lives here.
"""
import hashlib
import json
import math
import re
import sqlite3
import time

STRATEGY = 'online_admission'
MAX_CANDIDATES = 24
MAX_CANDIDATE_CHARS = 1200
FINAL_VERIFICATION_RESERVE_SECONDS = .050
TRANSPORT_FAILURE_CODES = frozenset({
    'invalid_config', 'invalid_timeout', 'model_binding', 'invalid_input', 'input_limit',
    'transport_unavailable', 'unsupported_platform', 'timeout', 'process_exit', 'output_limit',
    'invalid_protocol', 'guard_rejected', 'rpc_rejected', 'unexpected_tool',
    'incomplete_response', 'invalid_response', 'missing_request_evidence',
    'cleanup_incomplete', 'transport_error',
})
GUARD_FAILURE_CODES = frozenset({
    'online_query_bound', 'online_input_configuration', 'online_input_bound',
    'online_dependency_budget', 'online_correction_scope', 'online_card_missing',
    'online_note_file_changed', 'online_note_unavailable', 'online_correction_unverified',
    'online_notice_changed', 'online_notice_scope_changed', 'online_new_correction',
    'online_raw_corrected', 'online_source_unavailable', 'online_raw_changed',
    'online_verification_budget',
})


def _transport_metadata(diagnostics):
    """Only numeric fields and fixed codes cross the observation boundary."""
    if not isinstance(diagnostics, dict):
        return {}
    safe = {}
    from .online_transport import RESPONSE_VALIDATION_CODES
    validation = diagnostics.get('response_validation_code')
    if isinstance(validation, str) and validation in RESPONSE_VALIDATION_CODES:
        safe['response_validation_code'] = validation
    for key in ('provider_requests', 'payload_bytes', 'material_bytes',
                'wire_bytes_upper_bound', 'scoring_input_byte_limit', 'proof_count',
                'proof_quote_chars'):
        value = diagnostics.get(key)
        if type(value) is int and value >= 0:
            safe[key] = value
    for key in ('elapsed_ms', 'first_text_ms', 'message_end_ms'):
        value = diagnostics.get(key)
        if (type(value) is int and value >= 0) or (type(value) is float and math.isfinite(value) and value >= 0):
            safe[key] = value
    usage = diagnostics.get('usage')
    if isinstance(usage, dict):
        safe['usage'] = {key: value for key, value in usage.items()
                         if key in ('input', 'output', 'cacheRead', 'cacheWrite', 'totalTokens')
                         and type(value) is int and value >= 0}
    return safe


def requested(config):
    return isinstance(config.get('retrieval'), dict) and config['retrieval'].get('strategy') == STRATEGY


def enabled(config):
    if not requested(config):
        return False
    options = config['retrieval'].get('online_admission', {})
    return isinstance(options, dict) and options.get('enabled', True) is True


def local_candidates(cards, query, project, prior_provider=None):
    """Only structural admission; no local similarity/reranker quality cutoff."""
    from .context_retrieval import interpret_request, is_acknowledgement
    from .memory_relevance import filter_applicable, execution_steering
    interp = interpret_request(query)
    if not query or interp.no_history or interp.ack_only or is_acknowledgement(query) or execution_steering(query):
        return []
    # Acronyms are not mandatory literal entity labels, and a compound request
    # can draw its evidence from separate notes. The online judge handles that
    # relevance decision; scoped discovery and snapshots still guard identity,
    # permission and freshness. Only explicit state conflicts are excluded here.
    return filter_applicable(cards, query, project)


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


def _fair(candidates):
    groups = {}
    for card in candidates:
        for qid in card.get('_semantic_query_matches', {}) or {'q0': {}}:
            # Separate discovery routes so many notes cannot consume every
            # slot before a raw source, or vice versa, reaches the judge.
            match = card.get('_semantic_query_matches', {}).get(qid, {})
            graph_only = 'graph_rank' in match and not any(key in match for key in ('lexical_rank', 'semantic_rank'))
            groups.setdefault((qid, '_raw_source' in card, graph_only), []).append(card)
    found = set()
    for position in range(max(map(len, groups.values()), default=0)):
        for group in groups.values():
            if position < len(group):
                card = group[position]
                identity = (card['id'], card['version'])
                if identity not in found:
                    found.add(identity)
                    yield card


def verification_candidates(candidates):
    """Reserve a bounded fair discovery pool before expensive dependency checks.

    This is not admission: each retained identity and its complete correction
    closure must still pass the existing guards before any provider receives it.
    """
    ordered = list(_fair(candidates))
    return ordered[:MAX_CANDIDATES], max(0, len(ordered)-MAX_CANDIDATES)


def _long_note_excerpt(card):
    """Use current-body coordinates to offer whole paragraphs as a partial lead.

    Never trust cached passage text or trim a sentence to the character limit.
    Markdown fenced blocks stay together. No valid bounded passage means the
    existing title-only locator, not an invented or truncated explanation.
    """
    body = card.get('body', '')
    # Lists/quotes/indented or embedded fence tokens need a full Markdown
    # parser. Keep the location-only fallback instead of guessing boundaries.
    if any(re.search(r'`{3,}|~{3,}', line) and not re.match(r'^ {0,3}(`{3,}|~{3,})', line)
           for line in body.splitlines()):
        return ''
    blocks, start, offset, fence = [], 0, 0, None
    for line in body.splitlines(keepends=True):
        opening = re.match(r'^ {0,3}(`{3,}|~{3,})', line)
        if fence:
            if re.fullmatch(r' {0,3}'+re.escape(fence[0])+'{'+str(fence[1])+r',}[ \t]*(?:\r?\n)?', line):
                fence = None
        elif opening:
            fence = (opening.group(1)[0], len(opening.group(1)))
        elif not line.strip():
            if body[start:offset].strip():
                blocks.append((start, offset))
            start = offset + len(line)
        offset += len(line)
    if fence is None and body[start:].strip():
        blocks.append((start, len(body)))
    for match in card.get('_semantic_query_matches', {}).values():
        left, right = match.get('body_start'), match.get('body_end')
        if (match.get('passage_source') != 'knowledge_search.content'
                or type(left) is not int or type(right) is not int or not 0 <= left < right <= len(body)):
            continue
        ranked = sorted(enumerate(blocks), key=lambda pair: -max(0, min(right, pair[1][1])-max(left, pair[1][0])))
        for index, (first, last) in ranked:
            if min(right, last) <= max(left, first) or last-first > MAX_CANDIDATE_CHARS:
                continue
            lo = hi = index
            while lo > 0 and last-blocks[lo-1][0] <= MAX_CANDIDATE_CHARS:
                lo -= 1; first = blocks[lo][0]
            while hi+1 < len(blocks) and blocks[hi+1][1]-first <= MAX_CANDIDATE_CHARS:
                hi += 1; last = blocks[hi][1]
            return body[first:last]
    return ''


def prepare_candidates(query, candidates, *, max_input_bytes=30000, response_mode='set'):
    if not isinstance(query, str) or len(query) > 4000 or '\x00' in query:
        raise ValueError('online_query_bound')
    if type(max_input_bytes) is not int or not 512 <= max_input_bytes <= 30000:
        raise ValueError('online_input_configuration')
    from .online_transport import judge_input_budget
    used = judge_input_budget(query, [], max_input_bytes, response_mode=response_mode)
    if not used['fits']:
        raise ValueError('online_input_bound')
    payload, mapping, omitted = [], {}, 0
    for card in _fair(candidates):
        raw = '_raw_source' in card
        body = card.get('body', '')
        # Long-note evidence uses a whole-paragraph current-body passage only
        # as a location lead. The renderer still applies its whole-note rules.
        complete = not raw and len(body) <= MAX_CANDIDATE_CHARS
        text = body if len(body) <= MAX_CANDIDATE_CHARS else ('' if raw else _long_note_excerpt(card))
        title = card.get('title', '')
        if len(title) > 400:
            omitted += 1
            continue
        # Request-local aliases reduce generated output without exposing durable
        # identity. Only admitted items advance the sequence; omitted items can
        # never overwrite an existing mapping. Full objects stay in memory for
        # the same identity deduplication and before/after freshness guards.
        cid = f'c{len(payload)}'
        item = {'id': cid, 'kind': 'source' if raw else 'note', 'title': title,
                'text': text, 'complete': complete}
        cost = judge_input_budget(query, payload + [item], max_input_bytes, response_mode=response_mode)
        if (not text and not title) or len(payload) >= MAX_CANDIDATES or not cost['fits']:
            omitted += 1
            continue
        used = cost
        payload.append(item)
        mapping[cid] = card
    return payload, mapping, {'candidates_sent': len(payload), 'candidates_omitted': omitted,
                             'input_material_bytes': used['material_bytes'], 'input_tokens': None,
                             'input_wire_bytes_upper_bound': used['wire_bytes_upper_bound'],
                             'scoring_input_byte_limit': used['scoring_input_byte_limit'],
                             'input_token_bound_basis': 'complete UTF-8 wire estimate plus bounded framing reserve; actual wire capped independently; not a DS tokenizer measurement',
                             'candidate_partial': bool(omitted or any(not c['complete'] for c in payload))}


def _snapshot(evolution, project, cards, notice_guards=()):
    """Recheck only these candidates and their bounded source/correction closure.

    No global data_version rejection: unrelated writes do not invalidate a read.
    The same 256-identity scope is shared with the outer context operation.
    """
    from .correction_scope import plan_correction_scope
    from .retrieval_freshness import card_visible, source_current, candidate_dependencies
    from .source_memory import SourceMemory
    from .knowledge_files import KnowledgeFiles, _MAX_SCAN
    from .knowledge_learning import adopted_corrections_valid
    from .memory_policy import read_home_config
    from .retrieval_budget import verification_report
    engine = evolution.engine
    notes = [c for c in cards if '_raw_source' not in c]
    raw = [c for c in cards if '_raw_source' in c]
    deps, complete = candidate_dependencies(evolution.cards, notes)
    if not complete:
        raise ValueError('online_dependency_budget')
    plan = plan_correction_scope(engine, project, notes, raw, dependency_ids=deps)
    if not plan.complete:
        raise ValueError('online_correction_scope')
    # Cached validity from before the network operation cannot certify current
    # sources/manual files. Reuse identity accounting, not validity results.
    evolution._valid_cache = {}
    engine._retrieval_source_checks = {}
    files = KnowledgeFiles(engine, evolution.cards)
    current_cards, file_state, source_ids = [], [], set(plan.source_ids)
    for cid in sorted(plan.target_ids):
        current = evolution.cards.get(cid)
        if current is None:
            raise ValueError('online_card_missing')
        current_cards.append(current)
        source_ids.update(r['source_id'] for r in current.get('evidence', []))
        tracked = engine.db.execute('SELECT * FROM knowledge_files WHERE card_id=?', (cid,)).fetchone()
        if tracked:
            tracking = dict(tracked)
            if tracking.get('state') == 'ok':
                path = files._safe_target(tracking['path'])
                with path.open('rb') as stream:
                    body = stream.read(_MAX_SCAN + 1)
                if len(body) > _MAX_SCAN or hashlib.sha256(body).hexdigest() != tracking['file_hash']:
                    raise ValueError('online_note_file_changed')
            file_state.append(tracking)
    for old in notes:
        current = evolution.cards.get(old['id'])
        if (not current or current['version'] != old['version'] or current['body'] != old['body']
                or not card_visible(engine, project, current) or not evolution._valid_card(current)
                or not adopted_corrections_valid(engine, current)):
            raise ValueError('online_note_unavailable')
    memory = SourceMemory(engine)
    corrections = [item for scope in sorted(plan.scopes) for item in memory.corrections(
        scope, include_history=False, target_ids=plan.target_ids, source_ids=plan.source_ids)]
    if any(c.get('verification_unknown') for c in corrections):
        raise ValueError('online_correction_unverified')
    # Effective/pending correction notices are delivered independently of the
    # selected cards. Bind their exact original identities too, but never send
    # these guards to the remote judge or scan unrelated project corrections.
    notice_state = []
    notice_fields = ('project', 'event_key', 'source_id', 'source_version', 'target_card_id',
                     'target_card_version', 'replacement', 'quote', 'status', 'effective')
    for old in notice_guards:
        rows = memory.corrections(old['project'], include_history=False,
            target_ids=[], source_ids=[old['source_id']])
        current = next((c for c in rows if c['event_key'] == old['event_key']), None)
        if (current is None or current.get('verification_unknown')
                or any(current.get(k) != old.get(k) for k in notice_fields)):
            raise ValueError('online_notice_changed')
        if current.get('target_card_id'):
            target = evolution.cards.get(current['target_card_id'])
            if target and not card_visible(engine, project, target):
                raise ValueError('online_notice_scope_changed')
        source_ids.add(current['source_id'])
        notice_state.append({k: current.get(k) for k in notice_fields})
    from .knowledge_learning import correction_adopted
    note_ids = {c['id'] for c in notes}
    raw_pairs = {(c['_raw_source']['source_id'], c['version']) for c in raw}
    by_id = {c['id']: c for c in current_cards}
    for correction in corrections:
        if not correction['effective']:
            continue
        target_id = correction['target_card_id']
        affected = plan.affected(target_id)
        target = by_id.get(target_id)
        adopted = bool(target and correction_adopted(engine, target, correction))
        if note_ids.intersection(affected - ({target_id} if adopted else set())):
            raise ValueError('online_new_correction')
        old_sources = {(r['source_id'], r['version']) for cid in affected
                       for r in by_id.get(cid, {}).get('evidence', [])}
        if raw_pairs.intersection(old_sources):
            raise ValueError('online_raw_corrected')
    source_ids.update(c['source_id'] for c in corrections)
    sources = []
    for sid in sorted(source_ids):
        if not engine.source_available(sid) or not source_current(engine, sid):
            raise ValueError('online_source_unavailable')
        src = engine.store.source(sid)
        sources.append({k: v for k, v in src.items() if k != 'text'})
        sources[-1]['text_digest'] = hashlib.sha256(src['text'].encode()).hexdigest()
    for card in raw:
        row = card['_raw_source']
        src = engine.store.source(row['source_id'])
        if (src['project'] != project or src['current_version'] != row['version']
                or src['role'] not in ('user', 'document')
                or src['text'][row['offset']:row['offset'] + row['length']] != row['snippet']):
            raise ValueError('online_raw_changed')
    if verification_report(engine).get('verification_budget_exhausted'):
        raise ValueError('online_verification_budget')
    relations = []
    if engine.db.execute("SELECT 1 FROM sqlite_master WHERE name='organization_relations'").fetchone():
        for cid in sorted(plan.target_ids):
            relations.extend(dict(r) for r in engine.db.execute(
                'SELECT * FROM organization_relations WHERE owner_id=?', (cid,)))
    return _digest({'cards': current_cards, 'files': file_state, 'sources': sources, 'relations': relations,
                    'corrections': corrections, 'notice_state': notice_state,
                    'parents': {k: sorted(v) for k, v in plan.parents.items()},
                    'config': read_home_config(engine.home)})


def admit(evolution, project, query, candidates, config, *, deadline=None, notice_guards=()):
    report = {'strategy': STRATEGY, 'provider_requests': 0, 'unresolved': True,
              'coverage': 'not_assessed'}
    started = time.perf_counter()
    def failed(reason):
        report.update(degraded=reason, elapsed_ms=round((time.perf_counter()-started)*1000, 3))
        return [], report
    try:
        from .memory_policy import read_home_config, processing_allowed
        current_config = read_home_config(evolution.engine.home)
        if (not enabled(config) or not enabled(current_config)
                or processing_allowed(current_config, project)
                or current_config.get('retrieval') != config.get('retrieval')):
            return failed('online_configuration_changed_or_disabled')
        payload, mapping, limits = prepare_candidates(query, candidates,
            max_input_bytes=config['retrieval'].get('online_admission', {}).get('max_input_bytes', 30000),
            response_mode=config['retrieval'].get('online_admission', {}).get('response_mode', 'set'))
        report.update(limits)
        if not payload:
            return [], report
        bound = list(mapping.values())
        before = _snapshot(evolution, project, bound, notice_guards)
        from .online_transport import judge_candidates
        # The outer context deadline includes current-state revalidation and
        # rendering, not only provider wait. Transport still respects an
        # explicitly shorter configured timeout; no retry consumes this reserve.
        remaining = (None if deadline is None else
                     deadline - time.perf_counter() - FINAL_VERIFICATION_RESERVE_SECONDS)
        if remaining is not None and remaining <= 0:
            return failed('online_deadline_exhausted')
        result = judge_candidates(query, payload, config['retrieval'].get('online_admission', {}),
                                  timeout_seconds=remaining)
        metadata = _transport_metadata(result.get('diagnostics', {}))
        report['provider_requests'] = metadata.get('provider_requests')
        report['transport'] = metadata
        if result.get('ok') is not True:
            error = result.get('error')
            report['failure_code'] = error if isinstance(error, str) and error in TRANSPORT_FAILURE_CODES else 'unclassified_transport_failure'
            return failed('online_transport_failed')
        ids = result.get('selected_ids')
        if (not isinstance(ids, list) or any(not isinstance(x, str) or x not in mapping for x in ids)
                or len(ids) != len(set(ids)) or type(result.get('unresolved')) is not bool):
            return failed('online_invalid_selection')
        if config['retrieval'].get('online_admission', {}).get('response_mode','set')=='grounded':
            from .online_transport import validate_grounded_selection, grounded_proof_metadata, ResponseValidationError
            proofs=result.get('selected_proofs')
            try:
                if validate_grounded_selection(proofs,payload)!=ids:
                    return failed('online_invalid_selection')
            except ResponseValidationError as exc:
                report['response_validation_code']=exc.code
                return failed('online_invalid_selection')
            report.update(grounded_proof_metadata(proofs))
        if _snapshot(evolution, project, bound, notice_guards) != before:
            return failed('online_state_changed')
        if deadline is not None and time.perf_counter() >= deadline:
            return failed('online_deadline_exhausted')
        report.update(unresolved=result['unresolved'], selected_count=len(ids),
                      elapsed_ms=round((time.perf_counter()-started)*1000, 3))
        return [mapping[cid] for cid in ids], report
    except (ImportError, OSError, ValueError, KeyError, TypeError, RuntimeError, sqlite3.Error) as exc:
        # Preserve only known guard labels; exception text may contain private data.
        code = exc.args[0] if exc.args else None
        if isinstance(exc, ValueError) and isinstance(code, str) and code in GUARD_FAILURE_CODES:
            report['failure_code'] = code
        return failed('online_guard_or_configuration_failed')
