"""Bounded one-hop merge; DS-authored and locally reviewed. No truth inference."""

_RELATIONS = frozenset({'link', 'related', 'extends', 'contrasts', 'supports', 'refutes', 'supersedes'})
_DIRECTIONS = frozenset({'outgoing', 'incoming'})
_REQUIRED_KEYS = ('candidate_id', 'source_id', 'target_id', 'relation', 'direction')
_PROVENANCE_KEYS = _REQUIRED_KEYS + ('source_version', 'target_version', 'origin', 'anchor')


def _bounded_ids(values, limit):
    if len(values) > limit:
        raise ValueError('graph identity collection exceeds bound')
    if any(type(v) is not str or not v or len(v) > 512 for v in values):
        raise ValueError('invalid graph identity')


def _require_int(name, value, low, high):
    if type(value) is not int:
        raise TypeError(f'{name} must be an int')
    if not (low <= value <= high):
        raise ValueError(f'{name} must be in {low}..{high}')
    return value


def _require_allowed_ids(allowed_ids):
    if not isinstance(allowed_ids, (list, tuple, set, frozenset)):
        raise TypeError('allowed_ids must be a list, tuple, set or frozenset')
    if not all(type(item) is str for item in allowed_ids):
        raise TypeError('allowed_ids must contain only str')
    _bounded_ids(allowed_ids, 2048)
    return set(allowed_ids)


def expand_one_hop(seeds, adjacency, allowed_ids, *, max_seeds=3, per_seed_limit=4, total_limit=12, edge_limit=128):
    _require_int('max_seeds', max_seeds, 1, 8)
    _require_int('per_seed_limit', per_seed_limit, 1, 12)
    _require_int('total_limit', total_limit, 1, 24)
    _require_int('edge_limit', edge_limit, 1, 512)

    if not isinstance(seeds, list):
        raise TypeError('seeds must be a list')
    if not all(type(s) is str for s in seeds):
        raise TypeError('seeds must contain only str')
    _bounded_ids(seeds, 256)

    if not isinstance(adjacency, dict):
        raise TypeError('adjacency must be a dict')
    if not all(type(k) is str for k in adjacency):
        raise TypeError('adjacency keys must be str')
    _bounded_ids(adjacency, 256)
    if not all(type(v) is list for v in adjacency.values()):
        raise TypeError('adjacency values must be lists')

    allowed_set = _require_allowed_ids(allowed_ids)

    reduced_seeds = list(dict.fromkeys(seeds))
    selected_seeds = reduced_seeds[:max_seeds]
    seed_set = set(reduced_seeds)
    active_seeds = [s for s in selected_seeds if s in allowed_set and s in adjacency]

    candidates_by_id = {}
    candidate_order = []
    per_seed_contrib = {s: set() for s in active_seeds}
    indices = {s: 0 for s in active_seeds}
    seen_sigs = set()

    examined_edges = 0
    invalid_edges = 0
    restricted_edges = 0
    seed_truncated = len(reduced_seeds) > max_seeds
    edge_truncated = False
    candidate_truncated = False
    per_seed_truncated = False

    while examined_edges < edge_limit:
        made_progress = False
        for seed in active_seeds:
            idx = indices[seed]
            edge_list = adjacency[seed]
            if idx >= len(edge_list):
                continue
            if examined_edges >= edge_limit:
                break
            edge = edge_list[idx]
            indices[seed] = idx + 1
            examined_edges += 1
            made_progress = True

            if not isinstance(edge, dict):
                invalid_edges += 1
                continue
            if not all(key in edge for key in _REQUIRED_KEYS):
                invalid_edges += 1
                continue

            cand = edge['candidate_id']
            src = edge['source_id']
            tgt = edge['target_id']
            rel = edge['relation']
            direction = edge['direction']

            if not all(type(v) is str for v in (cand, src, tgt, rel, direction)):
                invalid_edges += 1
                continue
            if any(not v or len(v) > 512 for v in (cand, src, tgt)):
                invalid_edges += 1
                continue
            if rel not in _RELATIONS or direction not in _DIRECTIONS:
                invalid_edges += 1
                continue
            if direction == 'outgoing':
                if not (src == seed and tgt == cand):
                    invalid_edges += 1
                    continue
            else:
                if not (tgt == seed and src == cand):
                    invalid_edges += 1
                    continue

            if cand not in allowed_set or cand in seed_set:
                restricted_edges += 1
                continue

            sig = (cand, src, tgt, rel, direction)
            if sig in seen_sigs:
                continue
            seen_sigs.add(sig)

            if cand not in candidates_by_id:
                if len(candidate_order) >= total_limit:
                    candidate_truncated = True
                    continue
            if cand not in per_seed_contrib[seed]:
                if len(per_seed_contrib[seed]) >= per_seed_limit:
                    per_seed_truncated = True
                    continue
                if cand not in candidates_by_id:
                    candidates_by_id[cand] = {'id': cand, 'provenance': []}
                    candidate_order.append(cand)
                per_seed_contrib[seed].add(cand)

            if len(candidates_by_id[cand]['provenance']) < 3:
                # Never carry arbitrary caller payloads into retrieval output.
                provenance = {key: edge[key] for key in _PROVENANCE_KEYS if key in edge}
                for key in ('origin', 'anchor'):
                    if key in provenance:
                        provenance[key] = provenance[key][:200] if isinstance(provenance[key], str) else ''
                candidates_by_id[cand]['provenance'].append(provenance)

        if not made_progress:
            break

    if any(indices[s] < len(adjacency[s]) for s in active_seeds):
        edge_truncated = True

    return {
        'candidates': [candidates_by_id[cid] for cid in candidate_order],
        'diagnostics': {
            'examined_edges': examined_edges,
            'invalid_edges': invalid_edges,
            'restricted_edges': restricted_edges,
            'seed_truncated': seed_truncated,
            'edge_truncated': edge_truncated,
            'candidate_truncated': candidate_truncated,
            'per_seed_truncated': per_seed_truncated,
        },
    }
