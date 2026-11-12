"""Explicit organization metadata; never moves assets or grants read permission.

Topics have identities independent of labels. Navigation edges carry no truth
claim; evidence edges retain exact source versions and surface review status.
Neither kind silently rewrites or retires a card.
"""
import hashlib
import json
import re
import time
import unicodedata
import uuid

ROLES = ('project', 'insight', 'wiki', 'material', 'reflection')
NAVIGATION_RELATIONS = ('related', 'extends', 'contrasts')
EVIDENCE_RELATIONS = ('supports', 'refutes', 'supersedes')


def _label(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ValueError('topic must be a bounded nonempty string')
    value = unicodedata.normalize('NFKC', ' '.join(value.split()))
    if any(ord(c) < 32 for c in value) or value in ('.', '..'):
        raise ValueError('invalid topic label')
    return value


def _key(value):
    return _label(value).casefold()


def _segment(value):
    value = re.sub(r'[\x00-\x1f\\/:*?"<>|]', '-', value).strip(' .')
    return value[:120] or '其他主题'


class KnowledgeOrganization:
    def __init__(self, engine, cards=None):
        from .knowledge_store import KnowledgeStore
        self.engine, self.db = engine, engine.db
        self.cards = cards or KnowledgeStore(engine)
        with engine.locked(), engine.transaction():
            self.db.execute('CREATE TABLE IF NOT EXISTS organization_topics(id TEXT PRIMARY KEY,project TEXT NOT NULL,name TEXT NOT NULL,revision INTEGER NOT NULL,created_at REAL NOT NULL)')
            self.db.execute('CREATE TABLE IF NOT EXISTS organization_aliases(project TEXT NOT NULL,alias TEXT NOT NULL,label TEXT NOT NULL,topic_id TEXT NOT NULL,PRIMARY KEY(project,alias))')
            self.db.execute('CREATE TABLE IF NOT EXISTS organization_cards(card_id TEXT PRIMARY KEY,role TEXT NOT NULL,topic_id TEXT NOT NULL,origin TEXT NOT NULL,created_at REAL NOT NULL)')
            self.db.execute('CREATE TABLE IF NOT EXISTS organization_relations(id TEXT PRIMARY KEY,project TEXT NOT NULL,owner_id TEXT NOT NULL,owner_version INTEGER NOT NULL,target_id TEXT NOT NULL,target_version INTEGER NOT NULL,relation TEXT NOT NULL,evidence TEXT NOT NULL,revision INTEGER NOT NULL,created_at REAL NOT NULL)')
            self.db.execute('CREATE INDEX IF NOT EXISTS organization_relation_owner ON organization_relations(project,owner_id)')

    def resolve_topic(self, project, name_or_id):
        _label(project); _label(name_or_id)
        row = self.db.execute('SELECT * FROM organization_topics WHERE project=? AND id=?', (project, name_or_id)).fetchone()
        if not row:
            row = self.db.execute('SELECT t.* FROM organization_aliases a JOIN organization_topics t ON t.id=a.topic_id WHERE a.project=? AND a.alias=?', (project, _key(name_or_id))).fetchone()
        if not row:
            return None
        value = dict(row)
        value['aliases'] = [r[0] for r in self.db.execute('SELECT label FROM organization_aliases WHERE project=? AND topic_id=? ORDER BY alias', (project, value['id']))]
        return value

    def _aliases(self, project, topic_id, labels):
        for label in labels:
            row = self.db.execute('SELECT topic_id FROM organization_aliases WHERE project=? AND alias=?', (project, _key(label))).fetchone()
            if row and row[0] != topic_id:
                raise ValueError('topic alias already belongs to another topic')
        for label in labels:
            self.db.execute('INSERT OR IGNORE INTO organization_aliases VALUES(?,?,?,?)', (project, _key(label), label, topic_id))
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='knowledge_search_dirty'").fetchone():
            self.db.execute('INSERT OR IGNORE INTO knowledge_search_dirty SELECT card_id FROM organization_cards WHERE topic_id=?',(topic_id,))

    @staticmethod
    def _labels(name, aliases):
        if not isinstance(aliases, (list, tuple)) or len(aliases) > 20:
            raise ValueError('topic aliases must contain at most 20 labels')
        return [_label(name)] + [_label(a) for a in aliases]

    def register_topic(self, project, name, aliases=()):
        _label(project); labels = self._labels(name, aliases)
        with self.engine.locked(), self.engine.transaction():
            topic = self.resolve_topic(project, labels[0])
            tid = topic['id'] if topic else uuid.uuid4().hex
            # Validate collisions before adding a topic, including nested callers.
            for label in labels:
                existing = self.resolve_topic(project, label)
                if existing and existing['id'] != tid:
                    raise ValueError('topic alias already belongs to another topic')
            if not topic:
                self.db.execute('INSERT INTO organization_topics VALUES(?,?,?,?,?)', (tid, project, labels[0], 1, time.time()))
            self._aliases(project, tid, labels)
            return self.resolve_topic(project, tid)

    def rename_topic(self, project, topic_id, name, aliases=()):
        labels = self._labels(name, aliases)
        with self.engine.locked(), self.engine.transaction():
            topic = self.resolve_topic(project, topic_id)
            if not topic or topic['id'] != topic_id:
                raise ValueError('topic not found in project')
            self._aliases(project, topic_id, labels)
            if topic['name'] != labels[0]:
                self.db.execute('UPDATE organization_topics SET name=?,revision=revision+1 WHERE id=?', (labels[0], topic_id))
            return self.resolve_topic(project, topic_id)

    def topics(self, project):
        return [self.resolve_topic(project, r[0]) for r in self.db.execute('SELECT id FROM organization_topics WHERE project=? ORDER BY name,id', (project,))]

    def _legacy_role(self, card):
        # Existing content metadata outranks folder appearance; neither grants authority.
        table = self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='inline_note_meta'").fetchone()
        row = self.db.execute('SELECT payload FROM inline_note_meta WHERE card_id=? AND version=?', (card['id'], card['version'])).fetchone() if table else None
        if row:
            try:
                role = json.loads(row[0]).get('content_type')
                if role in ROLES:
                    return role
            except (ValueError, AttributeError):
                pass
        first = (card.get('path') or '').split('/')[0]
        return {'资料': 'material', '随笔': 'reflection', '项目': 'project'}.get(first, {'memory': 'project', 'framework': 'wiki', 'index': 'wiki', 'source': 'material'}.get(card['kind'], 'insight'))

    def describe(self, card):
        row = self.db.execute('SELECT * FROM organization_cards WHERE card_id=?', (card['id'],)).fetchone()
        topic = self.resolve_topic(card['project'], row['topic_id'] if row else card['topic'])
        return {'role': row['role'] if row else self._legacy_role(card),
                'topic_id': topic['id'] if topic else None,
                'topic': topic['name'] if topic else card['topic'],
                'aliases': topic['aliases'] if topic else [], 'path': card.get('path'),
                'legacy_mapped': not row or row['origin'] == 'legacy'}

    def bind_card(self, card, role=None, topic_id=None):
        if role is not None and role not in ROLES:
            raise ValueError('invalid content role')
        with self.engine.locked(), self.engine.transaction():
            existing = self.db.execute('SELECT * FROM organization_cards WHERE card_id=?', (card['id'],)).fetchone()
            if existing:
                if (role and role != existing['role']) or (topic_id and topic_id != existing['topic_id']):
                    raise ValueError('organization identity change requires explicit review')
                return self.describe(card)
            topic = self.resolve_topic(card['project'], topic_id or card['topic'])
            if topic_id and (not topic or topic['id'] != topic_id):
                raise ValueError('topic_id not found in project')
            topic = topic or self.register_topic(card['project'], card['topic'])
            self.db.execute('INSERT INTO organization_cards VALUES(?,?,?,?,?)', (card['id'], role or self._legacy_role(card), topic['id'], 'explicit' if role or topic_id else 'legacy', time.time()))
            return self.describe(card)

    def directory(self, card, project_label=None):
        value = self.describe(card)
        role = value['role']
        root = {'project': '项目', 'material': '资料', 'reflection': '随笔'}.get(role, '知识')
        label = (project_label or card['project']) if role == 'project' else value['topic']
        return root + '/' + _segment(label)

    def map_existing(self, project):
        """Explicit metadata-only migration of tracked cards; no file scans/moves."""
        with self.engine.locked(), self.engine.transaction():
            return [dict(id=card['id'], **self.bind_card(card))
                    for card in self.cards.list(project, include_inactive=True)]

    def evidence_current(self, card):
        return all(row['state'] == 'current' for row in self.list_relations(card['project'], card['id'])
                   if row['category'] == 'evidence')

    def _card(self, project, cid):
        card = self.cards.get(cid)
        if not card or card['project'] != project:
            raise ValueError('relation endpoint not found in project')
        return card

    def _check_cycle(self, project, owner_id, target_id):
        todo = [(target_id, 0)]; seen = set()
        while todo:
            cid, depth = todo.pop()
            if cid == owner_id or depth > 32:
                raise ValueError('evidence relation cycle or depth limit')
            if cid in seen:
                continue
            seen.add(cid)
            todo.extend((r[0], depth + 1) for r in self.db.execute(
                'SELECT target_id FROM organization_relations WHERE project=? AND owner_id=? AND relation IN (?,?,?)',
                (project, cid, *EVIDENCE_RELATIONS)))

    def add_relation(self, project, owner_id, expected_version, target_id, relation, target_version=None, evidence=None):
        if relation not in NAVIGATION_RELATIONS + EVIDENCE_RELATIONS:
            raise ValueError('invalid relation kind')
        if owner_id == target_id:
            raise ValueError('self relation is not allowed')
        if type(expected_version) is not int or expected_version < 1:
            raise ValueError('invalid expected_version')
        if target_version is not None and (type(target_version) is not int or target_version < 1):
            raise ValueError('invalid target_version')
        with self.engine.locked(), self.engine.transaction():
            owner, target = self._card(project, owner_id), self._card(project, target_id)
            meta_table = self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='inline_note_meta'").fetchone()
            meta_row = self.db.execute('SELECT payload FROM inline_note_meta WHERE card_id=? AND version=?', (owner_id, owner['version'])).fetchone() if meta_table else None
            try:
                confirmed = bool(meta_row and json.loads(meta_row[0]).get('status') == 'confirmed')
            except (ValueError, AttributeError):
                confirmed = True
            if owner['manually_edited'] or owner['state'] != 'active' or confirmed or owner['key'] in ('authorization', 'constraints', 'confirmed_decision', 'scope'):
                raise ValueError('protected or inactive relation owner')
            if owner['version'] != expected_version or (target_version is not None and target['version'] != target_version):
                raise ValueError('relation version conflict')
            if target['state'] != 'active':
                raise ValueError('inactive relation target')
            refs = evidence if evidence is not None else []
            if not isinstance(refs, list) or len(refs) > 12:
                raise ValueError('invalid relation evidence')
            if relation in EVIDENCE_RELATIONS:
                if not refs or target_version is None:
                    raise ValueError('evidence relation requires exact source and target versions')
                self._check_cycle(project, owner_id, target_id)
                refs = [self.cards._evidence_entry(ref, project) for ref in refs]
            elif refs:
                raise ValueError('navigation does not accept evidence claims')
            rid = hashlib.sha256(json.dumps([project, owner_id, relation, target_id]).encode()).hexdigest()[:32]
            old = self.db.execute('SELECT * FROM organization_relations WHERE id=?', (rid,)).fetchone()
            encoded = json.dumps(refs, ensure_ascii=False, sort_keys=True)
            changed = not old or (old['owner_version'], old['target_version'], old['evidence']) != (expected_version, target['version'], encoded)
            if changed:
                self.db.execute('INSERT OR REPLACE INTO organization_relations VALUES(?,?,?,?,?,?,?,?,?,?)', (rid, project, owner_id, expected_version, target_id, target['version'], relation, encoded, old['revision'] + 1 if old else 1, time.time()))
            return next(r for r in self.list_relations(project, owner_id) if r['id'] == rid)

    def list_relations(self, project, card_id):
        owner = self._card(project, card_id)
        results = []
        for row in self.db.execute('SELECT * FROM organization_relations WHERE project=? AND owner_id=? ORDER BY relation,target_id', (project, card_id)):
            value = dict(row); value['evidence'] = json.loads(value['evidence'])
            target = self.cards.get(value['target_id'])
            valid = bool(target and target['project'] == project and target['state'] == 'active' and owner['state'] == 'active')
            category = 'navigation' if value['relation'] in NAVIGATION_RELATIONS else 'evidence'
            state = 'current' if valid else 'unavailable'
            if valid and category == 'evidence':
                if (owner['version'], target['version']) != (value['owner_version'], value['target_version']):
                    state = 'stale'
                try:
                    for ref in value['evidence']:
                        self.cards._evidence_entry(ref, project)
                except (KeyError, ValueError):
                    state = 'unavailable'
            value.update(category=category, state=state, requires_review=state != 'current',
                         affects_body=False, affects_validity=category == 'evidence', semantic_verified=False)
            # Unavailable evidence must not leak withdrawn quotes through metadata.
            if state == 'unavailable':
                value['evidence'] = [{k: ref[k] for k in ('source_id', 'version')} for ref in value['evidence']]
            results.append(value)
        return results
