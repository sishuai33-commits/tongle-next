"""Rebuildable scoped card candidates. SQLite triggers invalidate within the write.

Only dirty cards are projected. Read-time validity remains the core's authority;
this index cannot grant access, accept corrections or overwrite a human note.
"""
import json
import time
from .source_memory import _tokens


DERIVED_TRIGGERS = {
    'ks_'+event.lower(): f'CREATE TRIGGER ks_{event.lower()} AFTER {event} ON knowledge_cards BEGIN INSERT OR IGNORE INTO knowledge_search_dirty VALUES({"OLD" if event=="DELETE" else "NEW"}.id); END'
    for event in ('INSERT','UPDATE','DELETE')
}


class KnowledgeIndex:
    def __init__(self, engine, cards):
        self.engine, self.db, self.cards = engine, engine.db, cards
        with engine.locked(), engine.transaction():
            fresh = (not self.db.execute("SELECT 1 FROM sqlite_master WHERE name='knowledge_search'").fetchone()
                     or not self.db.execute("SELECT 1 FROM sqlite_master WHERE name='knowledge_search_fts'").fetchone())
            if fresh:
                self.db.execute('DROP TABLE IF EXISTS knowledge_search_fts')
                self.db.execute('DROP TABLE IF EXISTS knowledge_search')
            self.db.execute('CREATE TABLE IF NOT EXISTS knowledge_search(rowid INTEGER PRIMARY KEY, card_id TEXT UNIQUE, version INTEGER, project TEXT, topic TEXT, content TEXT)')
            self.db.execute('CREATE INDEX IF NOT EXISTS ks_scope ON knowledge_search(project,topic)')
            self.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_search_fts USING fts5(title_terms,body_terms,tokenize='unicode61')")
            self.db.execute('CREATE TABLE IF NOT EXISTS knowledge_search_dirty(card_id TEXT PRIMARY KEY)')
            for event in ('INSERT', 'UPDATE', 'DELETE'):
                obj = 'OLD' if event == 'DELETE' else 'NEW'
                self.db.execute(f'CREATE TRIGGER IF NOT EXISTS ks_{event.lower()} AFTER {event} ON knowledge_cards BEGIN INSERT OR IGNORE INTO knowledge_search_dirty VALUES({obj}.id); END')
            if fresh:
                self.db.execute('INSERT OR IGNORE INTO knowledge_search_dirty SELECT id FROM knowledge_cards')
        self.bootstrap = fresh

    def refresh(self, limit=256):
        updated = 0
        with self.engine.locked(), self.engine.transaction():
            rows = self.db.execute('SELECT card_id FROM knowledge_search_dirty ORDER BY card_id LIMIT ?', (limit,)).fetchall()
            for row in rows:
                cid = row[0]
                old = self.db.execute('SELECT rowid FROM knowledge_search WHERE card_id=?', (cid,)).fetchone()
                if old:
                    self.db.execute('DELETE FROM knowledge_search_fts WHERE rowid=?', (old[0],))
                    self.db.execute('DELETE FROM knowledge_search WHERE rowid=?', (old[0],))
                if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='knowledge_vectors'").fetchone():
                    self.db.execute('DELETE FROM knowledge_vectors WHERE card_id=?',(cid,))
                card = self.cards.get(cid)
                if card and card['state'] == 'active' and card['kind'] not in ('source', 'index'):
                    # Store an exact projection, not a generated summary. Full body
                    # stays in authoritative card storage; semantic chunks are derived.
                    title = ' '.join((card['title'], card['key'], card['topic'],*self.topic_aliases(card['id'])))
                    cur = self.db.execute('INSERT INTO knowledge_search(card_id,version,project,topic,content) VALUES(?,?,?,?,?)',
                        (cid, card['version'], card['project'], card['topic'], title+'\n'+card['body']))
                    self.db.execute('INSERT INTO knowledge_search_fts(rowid,title_terms,body_terms) VALUES(?,?,?)',
                        (cur.lastrowid, ' '.join(_tokens(title)), ' '.join(_tokens(card['body']))))
                self.db.execute('DELETE FROM knowledge_search_dirty WHERE card_id=?', (cid,))
                updated += 1
            if updated:
                self.db.execute("INSERT INTO meta(key,value) VALUES('knowledge_index_revision','1') ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1")
        return {'updated': updated, 'pending': self.db.execute('SELECT count(*) FROM knowledge_search_dirty').fetchone()[0]}

    @staticmethod
    def scope_sql(project, cfg):
        scopes = [project] if project == 'shared' or project in cfg.get('library_excluded_projects', []) else [project, 'shared']
        sql = ' k.project IN ('+','.join('?' for _ in scopes)+')'
        params = list(scopes)
        topics = cfg.get('knowledge_project_topics', {}).get(project) if isinstance(cfg.get('knowledge_project_topics'), dict) else None
        if isinstance(topics, list) and all(isinstance(t,str) for t in topics):
            sql += ' AND (k.project=? OR k.topic IN ('+','.join('?' for _ in topics)+'))'
            params += [project, *topics]
        return sql, params

    def search(self, project, query, cfg, limit=64):
        tokens = _tokens(query)[:128]
        if not tokens:
            return []
        scope, params = self.scope_sql(project, cfg)
        rows = self.db.execute('SELECT k.card_id FROM knowledge_search_fts f JOIN knowledge_search k ON k.rowid=f.rowid JOIN knowledge_cards c ON c.id=k.card_id AND c.version=k.version AND c.state=\'active\' WHERE knowledge_search_fts MATCH ? AND '+scope+' ORDER BY bm25(knowledge_search_fts,4.0,1.0),k.card_id LIMIT ?',
            [' OR '.join('"'+t+'"' for t in tokens), *params, min(64,max(1,limit))]).fetchall()
        return [dict(self.cards.get(row[0]),_topic_aliases=self.topic_aliases(row[0])) for row in rows]

    def topic_aliases(self,card_id):
        if not self.db.execute("SELECT 1 FROM sqlite_master WHERE name='organization_aliases'").fetchone():return []
        return [r[0] for r in self.db.execute('SELECT a.label FROM organization_aliases a JOIN organization_cards c ON c.topic_id=a.topic_id WHERE c.card_id=? ORDER BY a.alias',(card_id,))]

    def search_many(self, project, queries, cfg, prior=()):
        """Read independent lexical lists after a caller's single refresh.

        Session anchors are discovery only and are attached to q0, not treated
        as evidence that every explicit subquestion has candidates.
        """
        from .retrieval_plan import query_specs
        specs = query_specs(queries)
        result = {q.id: self.search(project, q.text, cfg) for q in specs}
        if 'q0' in result:
            chosen = result['q0']
            ids = {(c['id'], c['version']) for c in chosen}
            for text, *_ in list(prior)[-6:]:
                for card in self.search(project, text, cfg, limit=24):
                    identity = (card['id'], card['version'])
                    if identity not in ids and len(chosen) < 64:
                        chosen.append(card)
                        ids.add(identity)
        return result

    def candidates_many(self, project, queries, cfg, prior=()):
        from .retrieval_plan import query_specs
        specs = query_specs(queries)
        started = time.perf_counter()
        report = self.refresh(256)
        timings = {'index': (time.perf_counter()-started)*1000}
        lexical_started = time.perf_counter()
        chosen = self.search_many(project, specs, cfg, prior)
        if report['pending']:
            report['degraded'] = 'lexical_index_partial'
        timings['lexical'] = (time.perf_counter()-lexical_started)*1000
        report['stage_timings'] = timings
        report.update(candidates=len({(c['id'], c['version']) for rows in chosen.values() for c in rows}),
                      elapsed_ms=round((time.perf_counter()-started)*1000, 3), strategy='lexical',
                      query_results={q.id: {'hash': q.digest, 'candidates': len(chosen[q.id]),
                                           'candidate_found': bool(chosen[q.id]),
                                           'unresolved': not bool(chosen[q.id])} for q in specs})
        return chosen, report

    def candidates(self, project, query, cfg, prior=()):
        from .retrieval_plan import QuerySpec
        chosen, report = self.candidates_many(project, (QuerySpec('q0', query),), cfg, prior)
        return chosen['q0'], report
