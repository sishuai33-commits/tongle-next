"""Opt-in, rebuildable one-hop discovery. Links grant no truth or permission.

Only card storage is projected; query-time work never scans the Vault. The
existing candidate synchronization and read guards remain authoritative.
"""
import copy
import json
import posixpath
import sqlite3
import time

from .curation import (WIKILINK_RE, MDLINK_RE, SCHEME_RE, _strip_nonprose,
                       _split_wikilink, _decode_target, _link_path_part)


def options(config):
    retrieval = config.get('retrieval', {})
    value = retrieval.get('graph', {}) if isinstance(retrieval, dict) else {}
    if not isinstance(value, dict) or value.get('enabled') is not True:
        return None
    result = {'max_seeds': 3, 'per_seed_limit': 4, 'total_limit': 12,
              'edge_limit': 128, 'refresh_limit': 128}
    bounds = {'max_seeds': 8, 'per_seed_limit': 12, 'total_limit': 24,
              'edge_limit': 512, 'refresh_limit': 256}
    for key, maximum in bounds.items():
        v = value.get(key, result[key])
        if type(v) is not int or not 1 <= v <= maximum:
            raise ValueError('invalid graph ' + key)
        result[key] = v
    return result


def _path(value):
    if not isinstance(value, str) or not value or '\x00' in value or '\\' in value:
        return None
    result = posixpath.normpath(value)
    if result.startswith('/') or result == '..' or result.startswith('../'):
        return None
    return result


def _keys(owner_path, target, kind):
    part = _link_path_part(target)
    if not part:
        return []
    # Explicit attachments must never fall back to a same-named Markdown card.
    suffix = posixpath.splitext(part)[1].casefold()
    if suffix in {'.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.csv',
                  '.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.heic', '.avif',
                  '.bmp', '.tif', '.tiff', '.mp3', '.mp4', '.wav', '.mov', '.zip',
                  '.7z', '.rar', '.html', '.htm', '.json', '.xml', '.txt'}:
        return []
    result = []
    relative = _path(posixpath.join(posixpath.dirname(owner_path), part))
    root = _path(part) if kind == 'wikilink' else None
    for p in (relative, root):
        if p:
            result.append('path:' + (p if p.lower().endswith('.md') else p + '.md').casefold())
    if kind == 'wikilink' and '/' not in part and not SCHEME_RE.match(part):
        result.append('stem:' + (part[:-3] if part.lower().endswith('.md') else part).casefold())
    return list(dict.fromkeys(result))


DERIVED_TABLES = ('knowledge_graph_nodes', 'knowledge_graph_links', 'knowledge_graph_targets',
                  'knowledge_graph_dirty', 'knowledge_graph_truncated')
DERIVED_TRIGGERS = {
    'kg_'+event.lower(): f'CREATE TRIGGER kg_{event.lower()} AFTER {event} ON knowledge_cards BEGIN INSERT OR IGNORE INTO knowledge_graph_dirty VALUES({"OLD" if event=="DELETE" else "NEW"}.id); END'
    for event in ('INSERT', 'UPDATE', 'DELETE')
}


class GraphIndex:
    def __init__(self, index):
        self.index, self.engine, self.db = index, index.engine, index.db
        self.cards = index.cards
        with self.engine.locked(), self.engine.transaction():
            fresh = not self.db.execute("SELECT 1 FROM sqlite_master WHERE name='knowledge_graph_nodes'").fetchone()
            self.db.execute('CREATE TABLE IF NOT EXISTS knowledge_graph_nodes(card_id TEXT PRIMARY KEY,version INTEGER NOT NULL,project TEXT,topic TEXT,path TEXT,path_key TEXT,stem_key TEXT)')
            self.db.execute('CREATE INDEX IF NOT EXISTS kg_path ON knowledge_graph_nodes(path_key)')
            self.db.execute('CREATE INDEX IF NOT EXISTS kg_stem ON knowledge_graph_nodes(stem_key)')
            self.db.execute('CREATE TABLE IF NOT EXISTS knowledge_graph_links(owner_id TEXT,owner_version INTEGER,ordinal INTEGER,kind TEXT,target TEXT,keys_json TEXT,PRIMARY KEY(owner_id,ordinal))')
            self.db.execute('CREATE TABLE IF NOT EXISTS knowledge_graph_targets(owner_id TEXT,ordinal INTEGER,target_key TEXT,PRIMARY KEY(owner_id,ordinal,target_key))')
            self.db.execute('CREATE INDEX IF NOT EXISTS kg_target ON knowledge_graph_targets(target_key)')
            self.db.execute('CREATE TABLE IF NOT EXISTS knowledge_graph_dirty(card_id TEXT PRIMARY KEY)')
            self.db.execute('CREATE TABLE IF NOT EXISTS knowledge_graph_truncated(card_id TEXT PRIMARY KEY)')
            for sql in DERIVED_TRIGGERS.values():
                self.db.execute(sql.replace('CREATE TRIGGER ', 'CREATE TRIGGER IF NOT EXISTS ', 1))
            if fresh:
                self.db.execute('INSERT OR IGNORE INTO knowledge_graph_dirty SELECT id FROM knowledge_cards')

    def refresh(self, limit=128):
        import re
        updated = truncated = 0
        with self.engine.locked(), self.engine.transaction():
            for row in self.db.execute('SELECT card_id FROM knowledge_graph_dirty ORDER BY card_id LIMIT ?', (limit,)).fetchall():
                cid = row[0]
                for table, key in (('knowledge_graph_nodes', 'card_id'), ('knowledge_graph_links', 'owner_id'), ('knowledge_graph_targets', 'owner_id'), ('knowledge_graph_truncated', 'card_id')):
                    self.db.execute(f'DELETE FROM {table} WHERE {key}=?', (cid,))
                card = self.cards.get(cid)
                path = _path(card.get('path')) if card else None
                if card and card['state'] == 'active' and path and card['kind'] not in ('source', 'index'):
                    self.db.execute('INSERT INTO knowledge_graph_nodes VALUES(?,?,?,?,?,?,?)',
                                    (cid, card['version'], card['project'], card['topic'], path,
                                     'path:' + path.casefold(), 'stem:' + posixpath.splitext(posixpath.basename(path))[0].casefold()))
                    body = re.sub(r'<!--.*?-->|%%.*?%%', '', _strip_nonprose(card['body']), flags=re.S)
                    links = [(m.start(), 'wikilink', _split_wikilink(m.group(1))) for m in WIKILINK_RE.finditer(body)]
                    links += [(m.start(), 'markdown', _decode_target(m.group(1))) for m in MDLINK_RE.finditer(body)]
                    links.sort(key=lambda item: item[0])
                    if len(links) > 256:
                        truncated += 1
                        self.db.execute('INSERT INTO knowledge_graph_truncated VALUES(?)', (cid,))
                    for ordinal, (_, kind, target) in enumerate(links[:256]):
                        if not target or target.startswith('#') or SCHEME_RE.match(target):
                            continue
                        keys = _keys(path, target, kind)
                        if not keys:
                            continue
                        self.db.execute('INSERT INTO knowledge_graph_links VALUES(?,?,?,?,?,?)',
                                        (cid, card['version'], ordinal, kind, target, json.dumps(keys)))
                        self.db.executemany('INSERT INTO knowledge_graph_targets VALUES(?,?,?)', [(cid, ordinal, k) for k in keys])
                self.db.execute('DELETE FROM knowledge_graph_dirty WHERE card_id=?', (cid,))
                updated += 1
        return {'updated': updated, 'pending': self.db.execute('SELECT count(*) FROM knowledge_graph_dirty').fetchone()[0],
                'link_truncated_cards': self.db.execute('SELECT count(*) FROM knowledge_graph_truncated').fetchone()[0]}

    def _node(self, cid):
        row = self.db.execute("SELECT n.* FROM knowledge_graph_nodes n JOIN knowledge_cards c ON c.id=n.card_id AND c.version=n.version AND c.state='active' WHERE n.card_id=? AND NOT EXISTS(SELECT 1 FROM knowledge_graph_dirty d WHERE d.card_id=n.card_id)", (cid,)).fetchone()
        return dict(row) if row else None

    def _resolve(self, link):
        # A partial projection cannot establish unique names or absence of a
        # nearer path. Never resolve navigation against incomplete metadata.
        if self.db.execute('SELECT 1 FROM knowledge_graph_dirty LIMIT 1').fetchone():
            return None
        for key in json.loads(link['keys_json']):
            column = 'path_key' if key.startswith('path:') else 'stem_key'
            rows = self.db.execute(f"SELECT n.* FROM knowledge_graph_nodes n JOIN knowledge_cards c ON c.id=n.card_id AND c.version=n.version AND c.state='active' WHERE n.{column}=? AND NOT EXISTS(SELECT 1 FROM knowledge_graph_dirty d WHERE d.card_id=n.card_id) ORDER BY n.card_id LIMIT 2", (key,)).fetchall()
            if len(rows) > 1:
                return None
            if rows:
                return dict(rows[0])
        return None

    def adjacency(self, seeds, project, cfg, limit):
        from .retrieval_freshness import card_visible
        result, allowed = {}, set()
        checked = {}
        def visible(node):
            if not node:
                return False
            cid = node['card_id']
            if cid not in checked:
                checked[cid] = card_visible(self.engine, project, self.cards.get(cid))
            if checked[cid]:
                allowed.add(cid)
            return checked[cid]
        examined = 0
        truncated = False
        pools = {}
        for seed in seeds:
            node = self._node(seed)
            result[seed] = []
            if not visible(node):
                continue
            outgoing = self.db.execute('SELECT * FROM knowledge_graph_links WHERE owner_id=? ORDER BY ordinal LIMIT ?', (seed, limit+1)).fetchall()
            incoming = self.db.execute('SELECT DISTINCT l.* FROM knowledge_graph_targets t JOIN knowledge_graph_links l ON l.owner_id=t.owner_id AND l.ordinal=t.ordinal WHERE t.target_key IN (?,?) ORDER BY l.owner_id,l.ordinal LIMIT ?', (node['path_key'], node['stem_key'], limit+1)).fetchall()
            typed = []
            if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='organization_relations'").fetchone():
                typed = self.db.execute('SELECT * FROM organization_relations WHERE owner_id=? OR target_id=? ORDER BY owner_id,target_id,relation LIMIT ?', (seed, seed, limit+1)).fetchall()
            pools[seed] = (outgoing, incoming, typed)
        # Round-robin both seeds and edge routes. A hub cannot spend the entire
        # index-resolution budget before the next query seed gets considered.
        maximum = max((len(route) for routes in pools.values() for route in routes), default=0)
        work = ((seed, route_no, route[n]) for n in range(maximum)
                for seed, routes in pools.items() for route_no, route in enumerate(routes) if n < len(route))
        for seed, route_no, row in work:
            if examined >= limit:
                truncated = True
                break
            examined += 1
            if route_no < 2:
                owner = self._node(row['owner_id'])
                target = self._resolve(row)
                if not owner or owner['version'] != row['owner_version'] or not visible(owner) or not visible(target):
                    continue
                if route_no == 1 and target['card_id'] != seed:
                    continue
                relation, origin = 'link', row['kind']
                anchor = row['target'].partition('#')[2][:200]
            else:
                owner, target = self._node(row['owner_id']), self._node(row['target_id'])
                if not visible(owner) or not visible(target) or owner['version'] != row['owner_version'] or target['version'] != row['target_version']:
                    continue
                relation, origin, anchor = row['relation'], 'organization', ''
            direction = 'outgoing' if owner['card_id'] == seed else 'incoming'
            candidate = target if direction == 'outgoing' else owner
            result[seed].append({'candidate_id': candidate['card_id'], 'source_id': owner['card_id'],
                                 'source_version': owner['version'], 'target_id': target['card_id'],
                                 'target_version': target['version'], 'relation': relation, 'direction': direction,
                                 'origin': origin, 'anchor': anchor})

        return result, allowed, {'index_edges_examined': examined, 'index_edge_truncated': truncated}


def augment(index, project, plan, cfg, candidates, report):
    """Append graph candidates, without invented lexical or semantic scores."""
    try:
        opt = options(cfg)
    except ValueError:
        report['graph'] = {'enabled': False, 'reason': 'invalid_configuration'}
        return candidates, report
    if opt is None:
        return candidates, report
    started = time.perf_counter()
    try:
        from .bounded_graph import expand_one_hop
        graph = GraphIndex(index)
        status = graph.refresh(opt['refresh_limit'])
        groups = {q.id: [c for c in candidates if q.id in c.get('_semantic_query_matches', {})] for q in plan.queries}
        from .semantic_index import _fair_union
        seed_count = len({c['id'] for group in groups.values() for c in group})
        seeds = _fair_union(groups, opt['max_seeds'])
        adjacency, allowed, scan = graph.adjacency([c['id'] for c in seeds], project, cfg, opt['edge_limit'])
        selected = expand_one_hop([c['id'] for c in seeds], adjacency, allowed,
                                  **{k: v for k, v in opt.items() if k != 'refresh_limit'})
        # A failed optional projection must not mutate baseline qualifications.
        result = copy.deepcopy(candidates)
        by_id = {c['id']: c for c in result}
        selected['diagnostics']['seed_truncated'] = seed_count > len(seeds)
        selected['diagnostics']['seeds_considered'] = len(seeds)
        selected['diagnostics']['seeds_omitted'] = seed_count - len(seeds)
        for rank, item in enumerate(selected['candidates'], 1):
            current = index.cards.get(item['id'])
            if not current:
                continue
            candidate = by_id.get(item['id'])
            if candidate is None:
                candidate = dict(current, _semantic_query_matches={})
                by_id[item['id']] = candidate
                result.append(candidate)
            for edge in item['provenance']:
                seed_id = edge['source_id'] if edge['direction'] == 'outgoing' else edge['target_id']
                seed = next(c for c in seeds if c['id'] == seed_id)
                for qid in seed.get('_semantic_query_matches', {}):
                    path = dict(edge, query_id=qid, seed_id=seed_id, seed_version=seed['version'])
                    paths = candidate.setdefault('_graph_paths', [])
                    if path not in paths and len(paths) < 12:
                        paths.append(path)
                    candidate.setdefault('_semantic_query_matches', {}).setdefault(qid, {})['graph_rank'] = rank
        preflight = None
        discovered = len(result) - len(candidates)
        if any(c.get('_graph_paths') for c in result):
            from .graph_budget import preflight as graph_preflight
            preflight = graph_preflight(index.engine, result)
            if not preflight['accepted']:
                result = candidates
        report['graph'] = dict(enabled=True, **status, **scan, **selected['diagnostics'], added_candidates=len(result)-len(candidates),
                               elapsed_ms=round((time.perf_counter()-started)*1000, 3),
                               authority='discovery_only', automatic_admission='existing_gate')
        if preflight is not None:
            report['graph']['verification_preflight'] = preflight
            report['graph']['discovered_candidates'] = discovered
        report['candidates'] = len(result)
        return result, report
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error, ImportError, StopIteration) as exc:
        report['graph'] = {'enabled': True, 'unavailable': type(exc).__name__, 'elapsed_ms': round((time.perf_counter()-started)*1000, 3)}
        return candidates, report


def filter_graph_candidates(engine, project, candidates):
    """After sync, remove graph qualification whose path is no longer current."""
    if not any(c.get('_graph_paths') for c in candidates):
        return candidates
    from .inline_notes import InlineNotes
    from .retrieval_freshness import card_visible
    notes = InlineNotes(engine)
    cache = {}
    def valid(cid, version):
        key = (cid, version)
        if key not in cache:
            card = notes.cards.get(cid)
            cache[key] = bool(card and card['version'] == version and card_visible(engine, project, card)
                              and notes._read_state(card, notes._correction(project, cid, True)) == 'active')
        return cache[key]
    result = []
    for old in candidates:
        if not old.get('_graph_paths'):
            result.append(old)
            continue
        card = dict(old)
        paths = [p for p in old['_graph_paths'] if valid(p['source_id'], p['source_version']) and valid(p['target_id'], p['target_version'])]
        card['_graph_paths'] = paths
        matches = {qid: dict(m) for qid, m in old.get('_semantic_query_matches', {}).items()}
        for qid in list(matches):
            if not any(p['query_id'] == qid for p in paths):
                matches[qid].pop('graph_rank', None)
                if not any(k in matches[qid] for k in ('lexical_rank', 'semantic_rank')):
                    del matches[qid]
        card['_semantic_query_matches'] = matches
        if matches or old.get('_semantic_lexical_candidate') or '_semantic_score' in old:
            result.append(card)
    return result
