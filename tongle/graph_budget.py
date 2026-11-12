"""Bounded metadata estimate for optional graph discovery, not verification.

Known note/source/correction closures may reject a graph expansion before it
spends the public read's verification budget. No files or source bodies are
read, no identity is certified, and the existing shared hard limit remains
authoritative. Later raw-source discovery and changed metadata are not covered.
"""
import json
import sqlite3

_MAX_IDENTITIES = 256
_MAX_METADATA_ITEMS = 4096
_MAX_JSON_CHARS = 262144


class _Bound(Exception):
    pass


def preflight(engine, candidates):
    """Estimate current known candidate closure without consuming any budget.

    Overestimation is intentional: cited and current source versions are both
    counted even when an earlier validity check would reject the candidate.
    Only metadata counts/reasons are public; dependency identities stay private.
    """
    budget = getattr(engine, '_retrieval_verification_budget', None)
    maximum = min(_MAX_IDENTITIES, getattr(budget, 'maximum', _MAX_IDENTITIES))
    checked = set(getattr(budget, 'identities', ()))
    identities = set(checked)
    visited = set()
    source_versions = {}
    examined = 0
    db = engine.db

    def tick(count=1):
        nonlocal examined
        examined += count
        if examined > _MAX_METADATA_ITEMS:
            raise _Bound('metadata_scan_bound')

    def identity(kind, cid, version):
        if not isinstance(cid, str) or not cid or type(version) is not int or version < 1:
            raise ValueError('invalid identity metadata')
        identities.add((kind, cid, version))
        if len(identities) > maximum:
            raise _Bound('known_identity_budget')

    def decode(value, kind):
        if not isinstance(value, str) or len(value) > _MAX_JSON_CHARS:
            raise _Bound('metadata_json_bound')
        result = json.loads(value)
        if not isinstance(result, kind):
            raise ValueError('invalid metadata shape')
        return result

    def source(sid, cited=None):
        if not isinstance(sid, str) or not sid:
            raise ValueError('invalid source identity')
        if cited is not None:
            identity('source', sid, cited)
        if sid not in source_versions:
            tick()
            row = db.execute('SELECT current_version FROM sources WHERE id=?', (sid,)).fetchone()
            source_versions[sid] = row[0] if row else None
        if source_versions[sid] is not None:
            identity('source', sid, source_versions[sid])

    def evidence(encoded):
        for ref in decode(encoded, list):
            tick()
            if not isinstance(ref, dict):
                raise ValueError('invalid evidence metadata')
            source(ref['source_id'], ref['version'])

    try:
        if getattr(budget, 'exhausted', False) or len(identities) > maximum:
            raise _Bound('existing_budget_exhausted')
        tables = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
            "('knowledge_dependencies','organization_relations','source_memory_corrections')")}
        todo = []
        for card in candidates:
            tick()
            identity('card', card['id'], card['version'])
            todo.append(card['id'])
            for path in card.get('_graph_paths', []):
                tick()
                for key in ('source', 'target'):
                    identity('card', path[key + '_id'], path[key + '_version'])
                    todo.append(path[key + '_id'])
        while todo:
            cid = todo.pop()
            if cid in visited:
                continue
            tick()
            visited.add(cid)
            row = db.execute('SELECT id,version,project,evidence FROM knowledge_cards WHERE id=?', (cid,)).fetchone()
            if not row:
                continue  # Actual read guards reject missing dependencies.
            identity('card', cid, row['version'])
            evidence(row['evidence'])
            if 'knowledge_dependencies' in tables:
                refs = db.execute('SELECT basis_id FROM knowledge_dependencies WHERE card_id=? LIMIT ?',
                                  (cid, _MAX_IDENTITIES + 1)).fetchall()
                for ref in refs:
                    tick()
                    todo.append(ref[0])
                if len(refs) > _MAX_IDENTITIES:
                    raise _Bound('metadata_dependency_bound')
            if 'organization_relations' in tables:
                from .knowledge_organization import EVIDENCE_RELATIONS
                refs = db.execute('SELECT target_id,evidence FROM organization_relations WHERE owner_id=? AND relation IN (' +
                                  ','.join('?' for _ in EVIDENCE_RELATIONS) + ') LIMIT ?',
                                  (cid, *EVIDENCE_RELATIONS, _MAX_IDENTITIES + 1)).fetchall()
                for ref in refs:
                    tick()
                    todo.append(ref['target_id'])
                    evidence(ref['evidence'])
                if len(refs) > _MAX_IDENTITIES:
                    raise _Bound('metadata_dependency_bound')
            if 'source_memory_corrections' in tables:
                # Read-time consumers verify only the latest active head for a
                # target. Pending/withdrawn rows grant no effective correction.
                correction = db.execute(
                    "SELECT source_id,source_version,correction_type,matching_evidence "
                    "FROM source_memory_corrections WHERE project=? AND target_card_id=? AND status='active' "
                    "ORDER BY created_at DESC,event_key DESC LIMIT 1", (row['project'], cid)).fetchone()
                if correction:
                    tick()
                    source(correction['source_id'], correction['source_version'])
                    if correction['correction_type'] == 'natural':
                        match = decode(correction['matching_evidence'], dict)
                        source(match['source_id'], match['version'])
        accepted, reason = True, None
    except _Bound as exc:
        accepted, reason = False, str(exc)
    except (ValueError, KeyError, TypeError, sqlite3.Error):
        accepted, reason = False, 'metadata_unavailable'
    return {'accepted': accepted, 'scope': 'known_note_closure',
            'reason': reason, 'estimated_identity_count': len(identities),
            'already_checked_identity_count': len(checked), 'identity_limit': maximum,
            'metadata_items_examined': examined,
            'later_raw_and_metadata_changes': 'not_assessed'}
