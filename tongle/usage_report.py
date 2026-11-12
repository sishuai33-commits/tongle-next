"""Read-only memory-consumption counters; no model calls or quality verdicts.

The report distinguishes prepared context, captured answers and observed
citations. Provider delivery, adoption, correctness and task benefit remain
unknown. Retention is bounded, so this is a recent-window diagnostic rather
than a complete historical ledger. Pi and Codex receipts enter the main rates;
CC and unknown hosts are counted separately rather than distorting them.
"""
import json
import time
from collections import Counter

from .evolution import config
from .memory_receipts import MAX_RECEIPTS


def _table(engine, name):
    return bool(engine.db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone())


def _rate(numerator, denominator):
    return round(numerator / denominator, 6) if denominator else None


def _active_cards(engine, project):
    """Same authorization universe as Evolution._authorized_catalog, read-only."""
    if not _table(engine, 'knowledge_cards'):
        return {}
    cfg = config(engine)
    scopes = [project] + ([] if project in cfg.get('library_excluded_projects', []) else ['shared'])
    placeholders = ','.join('?' for _ in scopes)
    rows = [dict(r) for r in engine.db.execute(
        'SELECT * FROM knowledge_cards WHERE project IN (' + placeholders + ')',
        scopes)]
    topic_map = cfg.get('knowledge_project_topics', {})
    allowed = topic_map.get(project) if isinstance(topic_map, dict) else None
    if isinstance(allowed, list) and all(isinstance(x, str) for x in allowed):
        rows = [r for r in rows if r['project'] == project or r['topic'] in allowed]
    return {r['id']: r for r in rows if r.get('state') == 'active'}


def usage_report(engine, project, days=30):
    if type(days) is not int or not 1 <= days <= 3650:
        raise ValueError('usage report days must be between 1 and 3650')
    since = time.time() - days * 86400
    active_cards = _active_cards(engine, project)
    base = {
        'project': project,
        'window_days': days,
        'window_start': since,
        'active_cards': len(active_cards),
        'host_coverage': 'Pi and Codex memory receipts; CC and unknown hosts are excluded from main rates',
        'host_delivery': 'unknown',
        'adoption': 'unknown',
        'benefit': 'unknown',
        'outcome_rate': None,
        'outcome_note': 'no outcome ledger; judgment results remain unknown',
        'history_note': 'bounded to retained receipts (up to 1000), not full history',
        'retention_limit': MAX_RECEIPTS,
        'capture_completeness': 'unknown: receipts do not enumerate all native events',
        'host_metrics': {host: {'prepared_turns': 0, 'answer_captured_turns': 0}
            for host in ('pi', 'codex')},
        'host_denominators': {host: {'expected_native_events': None, 'capture_rate': None}
            for host in ('pi', 'codex')},
        'version_groups': [],
    }
    if not _table(engine, 'memory_context_receipts'):
        return dict(base, trace_available=False, prepared_turns=0,
                    turn_bound_turns=0, answer_captured_turns=0,
                    prepared_card_occurrences=0, cited_prepared_card_occurrences=0,
                    prepared_card_citation_rate=None, answered_card_citation_rate=None,
                    coverage_rate=None,
                    never_prepared_active_card_count=len(active_cards),
                    never_prepared_active_cards=sorted(active_cards)[:50],
                    never_prepared_active_cards_truncated=len(active_cards) > 50,
                    uncited_preparation_candidates=[])
    # dict(row) plus .get supports the original receipt table before the
    # additive binding/host columns have been migrated by a new trace write.
    all_rows = [dict(r) for r in engine.db.execute(
        'SELECT * FROM memory_context_receipts '
        'WHERE project=? AND prepared_at>=? ORDER BY prepared_at',
        (project, since))]
    rows = [r for r in all_rows if r.get('host', 'unknown') in ('pi', 'codex')]
    excluded_hosts = Counter(r.get('host', 'unknown') or 'unknown'
                             for r in all_rows if r.get('host') not in ('pi', 'codex'))
    host_metrics = {
        host: {
            'prepared_turns': sum(r.get('host') == host for r in all_rows),
            'answer_captured_turns': sum(
                r.get('host') == host and bool(r.get('answer_source_id'))
                for r in all_rows),
        } for host in ('pi', 'codex')
    }
    retained_total = engine.db.execute(
        'SELECT COUNT(*) FROM memory_context_receipts').fetchone()[0]
    citations = {}
    if _table(engine, 'memory_note_uses'):
        for row in engine.db.execute(
                'SELECT source_id,source_version,card_id,card_version '
                'FROM memory_note_uses WHERE project=?', (project,)):
            citations.setdefault((row[0], row[1]), set()).add((row[2], row[3]))

    prepared_counts = Counter()
    cited_counts = Counter()
    prepared_occurrences = 0
    answered_occurrences = 0
    cited_occurrences = 0
    answers_with_citations = 0
    for row in rows:
        try:
            cards = json.loads(row['rendered_cards'])
        except (TypeError, json.JSONDecodeError):
            # A malformed disposable trace must not break read-only diagnosis.
            continue
        if not isinstance(cards, list):
            continue
        answer_key = (row.get('answer_source_id'), row.get('answer_source_version'))
        answer_citations = citations.get(answer_key, set())
        if row.get('answer_source_id') and answer_citations:
            answers_with_citations += 1
        for card in cards:
            if (not isinstance(card, dict) or not isinstance(card.get('id'), str)
                    or type(card.get('version')) is not int):
                continue
            identity = (card['id'], card['version'])
            prepared_counts[identity] += 1
            prepared_occurrences += 1
            if row.get('answer_source_id'):
                answered_occurrences += 1
            if identity in answer_citations:
                cited_counts[identity] += 1
                cited_occurrences += 1
    prepared_active = {cid for cid, _version in prepared_counts if cid in active_cards}
    never_prepared = sorted(set(active_cards) - prepared_active)
    candidates = []
    for (cid, version), count in prepared_counts.items():
        if count >= 5 and not cited_counts[(cid, version)] and cid in active_cards:
            candidates.append({
                'id': cid, 'version': version,
                'title': active_cards[cid].get('title'),
                'prepared_count': count,
                'reason': 'prepared at least five times with no exact-version citation in this window; candidate only',
            })
    candidates.sort(key=lambda item: (-item['prepared_count'], item['id'], item['version']))
    return dict(
        base,
        trace_available=True,
        retained_receipts_all_projects=retained_total,
        host_metrics=host_metrics,
        version_groups=[{'fingerprints': version, 'receipts': count}
                        for version, count in _version_groups(all_rows)],
        excluded_host_prepared_turns=dict(excluded_hosts),
        prepared_turns=len(rows),
        turn_bound_turns=sum(bool(r.get('user_source_id')) for r in rows),
        answer_captured_turns=sum(bool(r.get('answer_source_id')) for r in rows),
        answers_with_citations=answers_with_citations,
        prepared_card_occurrences=prepared_occurrences,
        answered_card_occurrences=answered_occurrences,
        cited_prepared_card_occurrences=cited_occurrences,
        prepared_card_citation_rate=_rate(cited_occurrences, prepared_occurrences),
        answered_card_citation_rate=_rate(cited_occurrences, answered_occurrences),
        citation_rate_note='prepared rate includes unanswered rows; answered rate uses only captured answers; neither proves adoption or benefit',
        distinct_prepared_active_cards=len(prepared_active),
        coverage_rate=_rate(len(prepared_active), len(active_cards)),
        never_prepared_active_card_count=len(never_prepared),
        never_prepared_active_cards=never_prepared[:50],
        never_prepared_active_cards_truncated=len(never_prepared) > 50,
        uncited_preparation_candidates=candidates[:20],
        candidate_note='low or zero citation is not proof of irrelevance; citation format and task need must be reviewed',
    )


def _version_groups(rows):
    """Old records stay unknown; do not relabel history with today's code."""
    groups = Counter()
    for row in rows:
        try:
            value = json.loads(row.get('version_fingerprints') or 'null')
        except (ValueError, TypeError):
            value = None
        groups[json.dumps(value if isinstance(value, dict) else {'state': 'unknown'}, sort_keys=True)] += 1
    return [(json.loads(value), count) for value, count in groups.items()]
