"""Bounded work checkpoints and observed reads/citations, never learning jobs.

An observation proves a tool returned text or an answer cited a note, not benefit.
No background work, automatic retries, card scoring or deletion is introduced.
"""
import hashlib
import json
import time


class LearningWork:
    def __init__(self, engine):
        self.engine = engine
        self.db = engine.db
        with engine.locked(), engine.transaction():
            self.db.execute('CREATE TABLE IF NOT EXISTS memory_source_reads(project TEXT NOT NULL,session TEXT NOT NULL,source_id TEXT NOT NULL,version INTEGER NOT NULL,start INTEGER NOT NULL,end INTEGER NOT NULL,PRIMARY KEY(project,session,source_id,version,start,end))')
            self.db.execute('CREATE TABLE IF NOT EXISTS memory_work_units(project TEXT NOT NULL,session TEXT NOT NULL,event_key TEXT NOT NULL,payload_hash TEXT NOT NULL,payload TEXT NOT NULL,result TEXT NOT NULL,created_at REAL NOT NULL,PRIMARY KEY(project,event_key))')
            self.db.execute('CREATE TABLE IF NOT EXISTS memory_note_uses(project TEXT NOT NULL,source_id TEXT NOT NULL,source_version INTEGER NOT NULL,card_id TEXT NOT NULL,card_version INTEGER NOT NULL,kind TEXT NOT NULL CHECK(kind=\'cited\'),PRIMARY KEY(project,source_id,source_version,card_id,card_version))')

    def read(self, project, session, source, start, end):
        if source['project'] != project:
            raise ValueError('source outside project')
        if not session or type(start) is not int or type(end) is not int or not 0 <= start <= end <= len(source['text']):
            raise ValueError('invalid source read range')
        with self.engine.locked(), self.engine.transaction():
            self.db.execute('INSERT OR IGNORE INTO memory_source_reads VALUES(?,?,?,?,?,?)',
                            (project, session, source['source_id'], source['version'], start, end))

    def _coverage(self, project, session, source):
        spans = [list(r) for r in self.db.execute('SELECT start,end FROM memory_source_reads WHERE project=? AND session=? AND source_id=? AND version=? ORDER BY start,end',
                 (project, session, source['source_id'], source['version']))]
        # A host-captured user message was itself delivered to the current agent.
        if source['role'] == 'user' and source.get('session') == session:
            spans.append([0, len(source['text'])])
        end = 0
        for a, b in sorted(spans):
            if a > end:
                break
            end = max(end, b)
        return {'source_id': source['source_id'], 'version': source['version'],
                'returned_ranges': spans, 'full_text_returned': end >= len(source['text']),
                'is_current_version': source['version'] == source['current_version']}

    def finish(self, project, session, event_key, payload):
        if not isinstance(payload, dict) or set(payload) - {'outcome', 'sources', 'notes', 'reason'}:
            raise ValueError('invalid finish fields')
        outcome = payload.get('outcome')
        if outcome not in ('learned', 'no_change', 'incomplete'):
            raise ValueError('outcome must be learned, no_change or incomplete')
        reason = payload.get('reason', '')
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 600:
            raise ValueError('finish requires a short reason')
        sources, notes = payload.get('sources', []), payload.get('notes', [])
        if not isinstance(sources, list) or not 1 <= len(sources) <= 12 or not isinstance(notes, list) or len(notes) > 8:
            raise ValueError('finish expects 1..12 sources and at most 8 notes')
        if not event_key or not session:
            raise ValueError('host session and event identity required')
        if (outcome == 'learned' and not notes) or (outcome == 'no_change' and notes):
            raise ValueError('finish outcome and notes disagree')
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        from .inline_notes import InlineNotes
        catalog = InlineNotes(self.engine)
        with self.engine.locked(), self.engine.transaction():
            prior = self.db.execute('SELECT session,payload_hash,result FROM memory_work_units WHERE project=? AND event_key=?', (project, event_key)).fetchone()
            if prior:
                if prior['session'] != session or prior['payload_hash'] != digest:
                    raise ValueError('finish event reused with different content')
                return dict(json.loads(prior['result']), replayed=True)
            coverage = []
            seen = set()
            for ref in sources:
                self._ref(ref)
                identity = (ref['id'], ref['version'])
                if identity in seen:
                    raise ValueError('duplicate finish source')
                seen.add(identity)
                source = self.engine.store.source(*identity)
                if (source['project'] != project or not self.engine.source_available(ref['id']) or
                        (outcome == 'learned' and source['current_version'] != ref['version'])):
                    raise ValueError('finish source unavailable, stale or outside project')
                coverage.append(self._coverage(project, session, source))
            returned = all(c['full_text_returned'] for c in coverage)
            if outcome != 'incomplete' and not returned:
                raise ValueError('source not fully returned in this session; use incomplete or read remaining ranges')
            for ref in notes:
                self._ref(ref)
                card = catalog.cards.get(ref['id'])
                if not card or card['project'] != project or card['version'] != ref['version'] or card['state'] != 'active':
                    raise ValueError('finish note unavailable, stale or outside project')
                if not any((e['source_id'], e['version']) in seen for e in card['evidence']):
                    raise ValueError('finish note must cite this work sources')
                writes = self.db.execute('SELECT event_key FROM inline_note_events WHERE project=? AND card_id=? AND card_version=?',
                                         (project, ref['id'], ref['version'])).fetchall()
                if not any(self._same_session(r['event_key'], session) for r in writes):
                    raise ValueError('learned note must have been written by this session')
                if not catalog.evolution._valid_card(card):
                    raise ValueError('finish note has unavailable evidence')
                from .knowledge_learning import adopted_corrections_valid
                if not adopted_corrections_valid(self.engine,card):
                    raise ValueError('finish note has invalid correction adoption')
                if catalog._correction(project, card['id']):
                    raise ValueError('finish note has an unresolved effective correction')
            result = {'status': 'recorded', 'outcome': outcome, 'coverage': coverage,
                      'notes': notes, 'replayed': False,
                      'semantic_quality': 'not_assessed', 'benefit': 'unknown', 'queued_jobs': 0}
            self.db.execute('INSERT INTO memory_work_units VALUES(?,?,?,?,?,?,?)',
                            (project, session, event_key, digest, canonical, json.dumps(result, ensure_ascii=False), time.time()))
            return result

    @staticmethod
    def _ref(ref):
        if (not isinstance(ref, dict) or set(ref) != {'id', 'version'} or
                not isinstance(ref['id'], str) or not ref['id'] or len(ref['id']) > 200 or
                type(ref['version']) is not int or ref['version'] < 1):
            raise ValueError('reference requires id and integer version')

    @staticmethod
    def _same_session(event_key, session):
        if event_key.startswith('pi:' + session + ':tool:'):
            return True
        if event_key.startswith('cli:'):
            try:
                identity = json.loads(event_key[4:])
                return isinstance(identity, list) and len(identity) == 2 and identity[0] == session
            except ValueError:
                pass
        return False

    def observe_answer(self, source_id, version):
        from .source_memory import SourceMemory
        from .evolution import Evolution
        source = self.engine.store.source(source_id, version)
        if source['role'] != 'assistant':
            return 0
        refs=SourceMemory._actual_citations(source['text'])[:20]
        if not refs:
            return 0
        evolution = Evolution(self.engine)
        cards = {c['id']: c for c in evolution._authorized_catalog(source['project'])}
        count = 0
        with self.engine.locked(), self.engine.transaction():
            for cid, raw_version in refs:
                v = int(raw_version)
                card = cards.get(cid)
                if not card or v > card['version']:
                    continue
                self.db.execute('INSERT OR IGNORE INTO memory_note_uses VALUES(?,?,?,?,?,?)',
                                (source['project'], source_id, version, cid, v, 'cited'))
                count += 1
        return count

    def note_signals(self, project, card_id):
        """Observed events, not a quality score; shared use counts stay project-local."""
        from .evolution import Evolution
        from .source_memory import SourceMemory
        card=next((c for c in Evolution(self.engine)._authorized_catalog(project) if c['id']==card_id),None)
        if not card:raise ValueError('note outside authorized catalog')
        corrections=[c for c in SourceMemory(self.engine).corrections(card['project']) if c['target_card_id']==card_id]
        used=self.db.execute('SELECT COUNT(*) FROM memory_note_uses WHERE project=? AND card_id=?',(project,card_id)).fetchone()[0]
        revisions=self.db.execute('SELECT COUNT(DISTINCT old_version) FROM knowledge_revisions WHERE card_id=?',(card_id,)).fetchone()[0]
        return {'used':used,'corrected':len(corrections),'superseded':revisions,
                'effective_corrections':sum(bool(c['effective']) for c in corrections),
                'meaning':'used=observed answer citations in this project; corrected=recorded correction events; superseded=earlier versions; none proves benefit',
                'benefit':'unknown'}
