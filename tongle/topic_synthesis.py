"""Incremental, bounded topic content synthesis; utility remains independently measured.

A durable scan of published cards closes every compiler/publication recovery path.
Groups keep stable membership/target identities. No fabricated source events and no
new worker. Model output cannot choose a target or change the evidence graph.
"""
import json
import time
import uuid

from .evaluate import digest
from .evolution import BudgetWait, config
from .knowledge_files import KnowledgeFiles
from .learning import canonical, identity

PREFIX = 'topic-synthesis:'
RUNNABLE = {'propose', 'review', 'publication_pending'}
MAX_CARDS, MAX_SOURCES, MAX_CHARS = 12, 16, 48000


class CapacityError(ValueError):
    pass


def owned(card):
    return bool(card and card['key'].startswith(PREFIX))


def scopes(cfg):
    from .memory_policy import processing_allowed
    raw = cfg.get('knowledge_synthesis_topics', {})
    if not isinstance(raw, dict):
        return {}
    return {p: sorted(set(ts)) for p, ts in raw.items()
            if isinstance(p, str) and p and processing_allowed(cfg,p) and isinstance(ts, list) and ts
            and all(isinstance(t, str) and t.strip() for t in ts)}


def compact(card):
    if card is None:
        return None
    return {k: card[k] for k in ('id', 'key', 'project', 'kind', 'topic', 'title', 'body',
                                'version', 'state', 'manually_edited', 'body_sha256',
                                'basis_cards', 'evidence')}


class TopicSynthesis:
    def __init__(self, evolution):
        self.ev = evolution
        self.engine, self.db, self.cards = evolution.engine, evolution.db, evolution.cards
        with self.engine.locked(), self.engine.transaction():
            self.db.execute('CREATE TABLE IF NOT EXISTS knowledge_topics ('
                            'id TEXT PRIMARY KEY, project TEXT, topic TEXT, partition INTEGER, '
                            'members TEXT, target_id TEXT, state TEXT, payload TEXT, '
                            'lease_token TEXT, lease_until REAL DEFAULT 0, updated_at REAL)')

    def list(self):
        return [self._record(r) for r in self.db.execute(
            'SELECT * FROM knowledge_topics ORDER BY project,topic,partition')]

    @staticmethod
    def _record(row):
        r = dict(row)
        for k in ('members', 'payload'):
            r[k] = json.loads(r[k])
        return r

    def get(self, rid):
        row = self.db.execute('SELECT * FROM knowledge_topics WHERE id=?', (rid,)).fetchone()
        if row is None:
            raise KeyError(rid)
        return self._record(row)

    def _save(self, rec, token=None):
        sql = ('UPDATE knowledge_topics SET members=?,target_id=?,state=?,payload=?,updated_at=? '
               'WHERE id=?')
        args = [canonical(rec['members']), rec['target_id'], rec['state'],
                canonical(rec['payload']), time.time(), rec['id']]
        if token is not None:
            sql += ' AND lease_token=?'
            args.append(token)
        if self.db.execute(sql, args).rowcount != 1:
            raise ValueError('topic lease lost')

    def _scope(self, project, topic, cfg):
        if topic not in scopes(cfg).get(project, []):
            raise ValueError('topic no longer enabled')
        return digest([self.ev._scope_fingerprint(project, cfg), project, topic])

    def _derived(self, card, seen=None):
        if not card or owned(card):
            return True
        seen = set() if seen is None else seen
        if card['id'] in seen or len(seen) > 32:
            return True
        return any(self._derived(self.cards.get(ref['card_id']), seen | {card['id']})
                   for ref in card['basis_cards'])

    def _closure(self, card, catalog):
        cards, sources = {}, {}
        todo = [card]
        while todo:
            c = todo.pop()
            if not c or c['id'] not in catalog or owned(c):
                raise ValueError('unauthorized or synthesis-derived basis')
            if c['id'] in cards:
                continue
            if not self.ev._valid_card(c):
                raise ValueError('basis unavailable or unpublished')
            cards[c['id']] = compact(c)
            if len(cards) > MAX_CARDS:
                raise CapacityError('basis closure exceeds card bound')
            for ref in c['evidence']:
                src = self.engine.store.source(ref['source_id'])
                if (src['current_version'] != ref['version'] or ref['quote'] not in src['text']
                        or not self.engine.source_available(src['source_id'])):
                    raise ValueError('source changed or revoked')
                if src['session'] in ('knowledge-dependency', 'framework-maintenance'):
                    raise ValueError('system request is not original evidence')
                sources[src['source_id']] = dict(
                    {k: src[k] for k in ('project', 'version', 'text', 'role', 'kind', 'hash')},
                    id=src['source_id'])
            todo.extend(self.cards.get(ref['card_id']) for ref in c['basis_cards'])
        if not sources:
            raise ValueError('no original evidence')
        return cards, sources

    def _catalog_state(self, project, topic, catalog):
        result = {}
        for c in catalog.values():
            if (c['project'] != project or c['topic'] != topic or c['kind'] not in ('memory', 'knowledge', 'framework')
                    or self._derived(c) or not self.ev._published(c['id'])):
                continue
            evidence = []
            todo = [c]; seen = set()
            while todo:
                basis = todo.pop()
                if not basis or basis['id'] in seen:
                    continue
                seen.add(basis['id'])
                evidence.append([basis['id'], basis['version'], basis['state'], self.ev._published(basis['id'])])
                for ref in basis['evidence']:
                    try:
                        source = self.engine.store.source(ref['source_id'])
                        evidence.append([ref['source_id'], source['current_version'], source['hash'],
                                         self.engine.source_available(ref['source_id'])])
                    except KeyError:
                        evidence.append([ref['source_id'], 'missing'])
                todo.extend(self.cards.get(ref['card_id']) for ref in basis['basis_cards'])
            result[c['id']] = digest([compact(c), evidence])
        return result

    def consumable(self, card):
        row = self.db.execute('SELECT * FROM knowledge_topics WHERE target_id=?', (card['id'],)).fetchone()
        if not row:
            return False
        rec = self._record(row); cfg = config(self.engine); p = rec['payload']
        if rec['state'] != 'published' or digest(compact(card)) != p.get('target_hash'):
            return False
        try:
            self._fresh(rec, cfg, target=False)
            if (self._input_hash(p['snapshot']) != p['input_hash']
                    or digest(p['snapshot']['previous']) != p['base_target_hash']):
                return False
            review = self._proof(rec, committed=True)
            self._committed(rec, review)
            scope = self._scope(rec['project'], rec['topic'], cfg)
            catalog = {c['id']: c for c in self.ev._authorized_catalog(rec['project'])}
            current = self._catalog_state(rec['project'], rec['topic'], catalog)
            return scope == p['snapshot']['scope'] and current == p['snapshot']['catalog_state']
        except (KeyError, TypeError, ValueError):
            return False

    def _snapshot(self, rec, cfg, catalog=None):
        catalog = catalog or {c['id']: c for c in self.ev._authorized_catalog(rec['project'])}
        cards, sources, covered, excluded = {}, {}, [], []
        state = self._catalog_state(rec['project'], rec['topic'], catalog)
        cross = set(state) - set(rec['members'])
        overflow = []
        for cid in rec['members'] + sorted(cross):
            c = catalog.get(cid)
            try:
                if not c or c['topic'] != rec['topic'] or c['kind'] not in ('memory', 'knowledge', 'framework'):
                    raise ValueError('basis removed from topic or scope')
                cs, ss = self._closure(c, catalog)
                cards.update(cs); sources.update(ss); covered.append(cid)
            except CapacityError as exc:
                overflow.append({'id': cid, 'reason': str(exc)})
            except (KeyError, ValueError) as exc:
                excluded.append({'id': cid, 'version': c['version'] if c else None, 'reason': str(exc)})
        previous = compact(self.cards.get(rec['target_id'])) if rec['target_id'] else None
        from .topic_prompts import proposal_packet, review_packet
        protocol = digest([proposal_packet({}), review_packet({}, {}, 'hash')])
        snap = {'protocol': protocol, 'project': rec['project'], 'topic': rec['topic'], 'partition': rec['partition'],
                'members': covered, 'cross_members': sorted(cross), 'catalog_state': state, 'cards': sorted(cards.values(), key=lambda c: c['id']),
                'sources': sorted(sources.values(), key=lambda s: s['id']), 'previous': previous,
                'scope': self._scope(rec['project'], rec['topic'], cfg),
                'coverage': {'kind': 'enabled_project_topic', 'covered': covered, 'excluded': excluded, 'overflow': overflow}}
        return snap

    @staticmethod
    def _input_hash(snap):
        return digest({k: v for k, v in snap.items() if k != 'previous'})

    @staticmethod
    def _bounded(snap):
        return (not snap['coverage'].get('overflow') and len(snap['members']) <= MAX_CARDS and len(snap['cards']) <= MAX_CARDS and len(snap['sources']) <= MAX_SOURCES
                and len(canonical(snap)) <= MAX_CHARS)

    def discover(self, cfg):
        """Reconcile full durable published state, never advance a lossy watermark."""
        with self.engine.locked():
            KnowledgeFiles(self.engine).sync()
        with self.engine.locked(), self.engine.transaction():
            rows = self.list()
            for project, topics in scopes(cfg).items():
                catalog = {c['id']: c for c in self.ev._authorized_catalog(project)}
                for topic in topics:
                    groups = [r for r in rows if r['project'] == project and r['topic'] == topic]
                    assigned = {cid for r in groups for cid in r['members']}
                    candidates = [c for c in catalog.values() if c['project'] == project and c['topic'] == topic
                                  and c['kind'] in ('memory', 'knowledge', 'framework')
                                  and c['state'] == 'active' and not self._derived(c) and c['id'] not in assigned]
                    for card in sorted(candidates, key=lambda c: (c['created_at'], c['id'])):
                        # Wait for actual publication; a future scan recovers it.
                        if not self.ev._published(card['id']):
                            continue
                        chosen = groups[0] if groups else None
                        if chosen and (chosen['lease_until'] > time.time() or chosen['state'] == 'publication_pending'):
                            continue
                        if chosen is None:
                            part = max([r['partition'] for r in groups], default=0) + 1
                            rid = digest([project, topic, part])[:32]
                            self.db.execute('INSERT INTO knowledge_topics VALUES(?,?,?,?,?,NULL,?,?,NULL,0,?)',
                                            (rid, project, topic, part, '[]', 'waiting', '{}', time.time()))
                            chosen = self.get(rid); groups.append(chosen); rows.append(chosen)
                        chosen['members'].append(card['id']); self._save(chosen)
            for rec in self.list():
                if rec['topic'] not in scopes(cfg).get(rec['project'], []) or rec['lease_until'] > time.time():
                    continue
                if rec['state'] == 'publication_pending':
                    continue
                snap = self._snapshot(rec, cfg)
                ih, th = self._input_hash(snap), digest(snap['previous'])
                p = rec['payload']
                if p.get('input_hash') == ih and p.get('target_hash') == th:
                    continue
                rec['payload'] = {'snapshot': snap, 'input_hash': ih, 'target_hash': th, 'base_target_hash': th,
                                  'run': uuid.uuid4().hex, 'attempts': {}, 'responses': [], 'calls': {}}
                if not self._bounded(snap):
                    rec['state'] = 'attention'; rec['payload']['reason'] = 'full topic exceeds bound; explicit topic subdivision required, no truncation'
                elif not snap['members']:
                    rec['state'] = 'waiting'; rec['payload']['reason'] = 'no currently usable original basis'
                elif snap['previous'] and snap['previous']['state'] == 'retired':
                    rec['state'] = 'attention'; rec['payload']['reason'] = 'target deliberately retired'
                else:
                    rec['state'] = 'propose'
                self._save(rec)
        return self.status()

    def _fresh(self, rec, cfg, target=True):
        p = rec['payload']
        if self._input_hash(p['snapshot']) != p['input_hash'] or digest(p['snapshot']['previous']) != p['base_target_hash']:
            raise ValueError('frozen snapshot changed')
        live = config(self.engine)
        if identity(live) != identity(cfg):
            raise ValueError('model configuration changed')
        snap = self._snapshot(rec, live)
        if self._input_hash(snap) != rec['payload']['input_hash']:
            raise ValueError('topic input changed')
        if target and digest(snap['previous']) != rec['payload']['target_hash']:
            raise ValueError('target changed')
        return snap

    def _operation(self, rec, answer):
        if (not isinstance(answer, dict) or set(answer) != {'body', 'reason'}
                or not isinstance(answer['body'], str) or not 1 <= len(answer['body'].strip()) <= 2400
                or not isinstance(answer['reason'], str) or not answer['reason'].strip()):
            raise ValueError('proposal must contain body (1..2400 chars) and reason')
        snap = rec['payload']['snapshot']
        byid = {c['id']: c for c in snap['cards']}
        refs = [{'card_id': cid, 'version': byid[cid]['version']} for cid in snap['members']]
        op = {'body': answer['body'].strip() + '\n\n覆盖范围：本主题分区 ' + str(rec['partition'])
              + '；完整核对本项目此主题的当前有效基础卡及其原始来源；不代表整个知识库。',
              'evidence': [], 'basis_cards': refs, 'reason': answer['reason'], 'state': 'active'}
        if snap['previous']:
            op.update(op='update', id=rec['target_id'], expected_version=snap['previous']['version'])
        else:
            op.update(op='create', key=PREFIX + rec['id'], kind='framework', topic=rec['topic'],
                      title=rec['topic'] + '·综合 ' + str(rec['partition']))
        return op

    def _packet(self, rec):
        from .topic_prompts import proposal_packet, review_packet
        p = rec['payload']
        if rec['state'] == 'propose':
            return proposal_packet(p['snapshot'], p.get('feedback'))
        return review_packet(p['snapshot'], p['operation'], digest([p['operation']]))

    def _audit(self, rec, stage, proof):
        call_id = proof['call_id']
        row = self.db.execute('SELECT * FROM knowledge_calls WHERE id=?', (call_id,)).fetchone()
        if (not row or row['work_id'] != PREFIX + rec['payload']['run'] or row['stage'] != 'topic_' + stage
                or row['error'] is not None or row['elapsed'] is None):
            raise ValueError('successful independent model audit required')
        binding = {'call_id': call_id, 'input_hash': digest(proof['packet']),
                   'answer_hash': digest(proof['answer'])}
        row = self.db.execute('SELECT 1 FROM knowledge_failures WHERE work_id=? AND state=? AND error=?',
                              (PREFIX + rec['payload']['run'], 'topic_' + stage + '_receipt', canonical(binding))).fetchone()
        if not row:
            raise ValueError('response not bound to independent host receipt')

    def _review(self, rec, answer, check_operations=True):
        if (not isinstance(answer, dict) or set(answer) != {'approved', 'findings', 'proposal_hash', 'human_checks'}
                or type(answer['approved']) is not bool or not isinstance(answer['findings'], list)
                or not all(isinstance(f, str) and f.strip() for f in answer['findings'])
                or bool(answer['findings']) == answer['approved']
                or answer['proposal_hash'] != digest([rec['payload']['operation']])
                or not isinstance(answer['human_checks'], list)):
            raise ValueError('review schema or proposal hash mismatch')
        if answer['approved'] and check_operations:
            norm = self.cards._validate_ops([rec['payload']['operation']], rec['project'])[0]
            self.cards._check_existing(norm, rec['project'], answer)
        return answer['approved']

    def _step(self, rid, token, cfg):
        with self.engine.locked():
            KnowledgeFiles(self.engine).sync()
        with self.engine.locked(), self.engine.transaction():
            rec = self.get(rid); self._fresh(rec, cfg)
            p = rec['payload']; stage = rec['state']
            if p['attempts'].get(stage, 0) >= 2:
                rec['state'] = 'attention'; p['reason'] = 'stage attempt limit'; self._save(rec, token); return
            packet = self._packet(rec)
            p['attempts'][stage] = p['attempts'].get(stage, 0) + 1
            self._save(rec, token)
        try:
            self._cycle_calls += 1
            answer = self.ev._call({'id': PREFIX + p['run']}, 'topic_' + stage, packet, cfg)
            call_id = self.ev._last_call_id
        except BudgetWait:
            self._cycle_calls -= 1
            with self.engine.locked(), self.engine.transaction():
                rec = self.get(rid); rec['payload']['attempts'][stage] -= 1; self._save(rec, token)
            raise
        except (RuntimeError, OSError, ValueError) as exc:
            with self.engine.locked(), self.engine.transaction():
                rec = self.get(rid)
                rec['payload']['reason'] = str(exc)
                if rec['payload']['attempts'][stage] >= 2:
                    rec['state'] = 'attention'
                self._save(rec, token)
            return
        with self.engine.locked():
            KnowledgeFiles(self.engine).sync()
        with self.engine.locked(), self.engine.transaction():
            rec = self.get(rid); p = rec['payload']
            proof = {'stage': stage, 'call_id': call_id, 'packet': packet, 'input_hash': digest(packet),
                     'answer': answer, 'answer_hash': digest(answer)}
            if rec['lease_token'] != token:
                raise ValueError('topic lease lost before receipt')
            p['responses'].append(proof)
            self.db.execute('INSERT INTO knowledge_failures(work_id,state,error,created_at) VALUES(?,?,?,?)',
                            (PREFIX + p['run'], 'topic_' + stage + '_receipt',
                             canonical({'call_id': call_id, 'input_hash': digest(packet),
                                        'answer_hash': digest(answer)}), time.time()))
            try:
                self._fresh(rec, cfg); self._audit(rec, stage, proof)
                if stage == 'propose':
                    op = self._operation(rec, answer)
                    check = self.cards.validate_operations([op], rec['project'])
                    if not check['ok']:
                        raise ValueError(canonical(check['problems']))
                    p['operation'] = op; p['calls']['propose'] = proof; rec['state'] = 'review'
                else:
                    p['calls']['review'] = proof
                    if self._review(rec, answer):
                        rec['state'] = 'publication_pending'
                    else:
                        p['feedback'] = answer['findings']
                        rec['state'] = 'propose' if p['attempts'].get('propose', 0) < 2 else 'attention'
            except (KeyError, TypeError, ValueError) as exc:
                p['feedback'] = [str(exc)]; p['reason'] = str(exc)
                if p['attempts'][stage] >= 2:
                    rec['state'] = 'attention'
            self._save(rec, token)

    def _proof(self, rec, committed=False):
        p = rec['payload']
        for stage in ('propose', 'review'):
            proof = p['calls'][stage]; self._audit(rec, stage, proof)
            if digest(proof['packet']) != proof['input_hash'] or digest(proof['answer']) != proof['answer_hash']:
                raise ValueError('stage proof changed')
        a, b = p['calls']['propose'], p['calls']['review']
        if a['call_id'] == b['call_id'] or self._operation(rec, a['answer']) != p['operation']:
            raise ValueError('proposal proof mismatch')
        from .topic_prompts import proposal_packet, review_packet
        if a['packet'] != proposal_packet(p['snapshot'], a['packet'].get('feedback')):
            raise ValueError('proposal packet mismatch')
        if b['packet'] != review_packet(p['snapshot'], p['operation'], digest([p['operation']])):
            raise ValueError('review packet mismatch')
        if not self._review(rec, b['answer'], check_operations=not committed):
            raise ValueError('review rejected')
        return b['answer']

    def _committed(self, rec, review):
        p = rec['payload']; rid = rec['id']
        batch = self.db.execute('SELECT * FROM knowledge_batches WHERE batch_id=? AND project=?',
                                (PREFIX + p['run'], rec['project'])).fetchone()
        if (not batch or batch['op_hash'] != digest([p['operation']])
                or batch['review_hash'] != digest(review)
                or any(cid != rec['target_id'] for cid in json.loads(batch['changed_ids']))):
            raise ValueError('committed batch proof mismatch')
        card = self.cards.get(rec['target_id'])
        if (not card or card['key'] != PREFIX + rid or card['project'] != rec['project']
                or p['committed']['card_id'] != card['id'] or p['committed']['version'] != card['version']
                or digest(compact(card)) != p['committed']['hash']):
            raise ValueError('committed card changed; preserve manual edit')
        return card

    def publish(self, rid, cfg, token=None):
        with self.engine.locked():
            KnowledgeFiles(self.engine).sync()
            rec = self.get(rid); p = rec['payload']
            if rec['state'] != 'publication_pending':
                raise ValueError('not publication pending')
            committed = bool(p.get('committed'))
            self._fresh(rec, cfg, target=not committed)
            review = self._proof(rec, committed=committed)
            if not committed:
                with self.engine.transaction():
                    self.cards.apply(PREFIX + p['run'], rec['project'], [p['operation']], review)
                    card = self.cards._get_by_key(rec['project'], PREFIX + rid)
                    if not card:
                        raise ValueError('target missing after apply')
                    rec['target_id'] = card['id']
                    p['committed'] = {'card_id': card['id'], 'version': card['version'], 'hash': digest(compact(card))}
                    self._save(rec, token)
            # A cached committed marker is not authority: require the actual
            # atomic core batch and the original independently audited verdict.
            card = self._committed(rec, review)
            KnowledgeFiles(self.engine).publish([card['id']])
            if not self.ev._published(card['id']):
                raise OSError('topic publication readback failed')
            with self.engine.transaction():
                rec['state'] = 'published'; p['target_hash'] = digest(compact(card)); p.pop('reason', None); self._save(rec, token)
        return rec

    def cycle(self, cfg, max_calls=2):
        if type(max_calls) is not int or not 0 <= max_calls <= 2:
            raise ValueError('at most two calls per cycle')
        if not scopes(cfg) or not cfg.get('model_command') or self.ev.breaker_open():
            return {'calls': 0, 'disabled': True}
        if scopes(config(self.engine)) != scopes(cfg) or identity(config(self.engine)) != identity(cfg):
            return {'calls': 0, 'disabled': True}
        self.discover(cfg); self._cycle_calls = 0; failed = set()
        while True:
            with self.engine.locked(), self.engine.transaction():
                rec = next((r for r in sorted(self.list(), key=lambda r: (r['state'] != 'publication_pending', r['updated_at']))
                            if r['id'] not in failed and r['state'] in RUNNABLE
                            and r['topic'] in scopes(cfg).get(r['project'], []) and r['lease_until'] < time.time()
                            and (r['state'] == 'publication_pending' or self._cycle_calls < max_calls)), None)
                if rec is None:
                    break
                rid, token = rec['id'], uuid.uuid4().hex
                self.db.execute('UPDATE knowledge_topics SET lease_token=?,lease_until=? WHERE id=?',
                                (token, time.time() + 330, rid))
            try:
                if rec['state'] == 'publication_pending':
                    self.publish(rid, cfg, token)
                else:
                    self._step(rid, token, cfg)
            except BudgetWait:
                break
            except (KeyError, TypeError, ValueError, RuntimeError, OSError) as exc:
                failed.add(rid)
                with self.engine.locked(), self.engine.transaction():
                    rec = self.get(rid); rec['payload']['reason'] = str(exc)
                    # Only filesystem failure retains publication recovery. Stale
                    # input becomes visible and discover rebuilds on the next cycle.
                    if not isinstance(exc, OSError):
                        rec['state'] = 'attention'
                    self._save(rec, token)
            finally:
                with self.engine.locked(), self.engine.transaction():
                    self.db.execute('UPDATE knowledge_topics SET lease_token=NULL,lease_until=0 WHERE id=? AND lease_token=?', (rid, token))
        return {'calls': self._cycle_calls, **self.status()}

    def status(self):
        states = dict(self.db.execute('SELECT state,COUNT(*) FROM knowledge_topics GROUP BY state'))
        return {'states': states, 'partitions': len(self.list()), 'utility': 'unknown',
                'note': '内容复核与实际效用分开；综合只覆盖列明的分区。'}
