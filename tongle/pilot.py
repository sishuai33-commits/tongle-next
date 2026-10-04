"""Isolated synthetic pilot for exactly one new framework candidate.

A frozen, self-contained synthetic case materializes two brand-new instances
(current base cards vs. the same base plus one new framework card), a native
Pi consumer answers held-out tasks in fresh isolated sessions, and a
recomputing assessor decides from raw evidence whether the candidate earned a
published pilot-only instance.

Boundaries: this module never reads a production home, never snapshots or
promotes real material, accepts only ``material: "synthetic"`` cases with a
plain project identifier (no home/origin/authorization path fields), and never
edits evaluate/evaluation_lifecycle/core. All outputs are brand-new
directories via ``evaluate._check_out``; per-cell raw events and rows are
preserved without overwrite; assess/publish recompute everything from raw
events and the ledger and distrust report.json. A mocked consumer exercises
the harness in tests only and can never pass the native-consumer gate. A
synthetic gain is selected-case evidence, never proof of real user benefit.
The whole immutable case is the frozen closure: initial cards are logically
version 1 and no live production source update is supported or simulated.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
import uuid

from . import evaluate
from .context_delivery import CARD_FULL_FORMAT
from .evaluation_lifecycle import connect as _connect
from .evaluation_lifecycle import file_digest as _file_digest
from .evaluation_lifecycle import read as _read
from .evaluation_lifecycle import source as _source

SCHEMA = 1
SYNTHETIC = 'synthetic'
ARMS = ('current', 'candidate')
CARD_KINDS = ('memory', 'knowledge', 'framework')
ROLES = ('user', 'assistant')
MAX_SOURCES = 16
MAX_CARDS = 16
MAX_TASKS = 6
MAX_BODY = 60000
MAX_SOURCE_TEXT = 60000
MAX_CASE_BYTES = 200000
MAX_REFS = 12  # matches KnowledgeStore._MAX_EVID and Relations.validate caps
_PROJECT_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$')

PILOT_LIMITATIONS = (
    'Synthetic material in disposable instances; results are non-blind '
    'selected-case evidence, never proof of real user benefit.',
    'Hard criteria are exact JSON scalars only; no open-ended answer quality '
    'is scored, and substring matching is never used as a semantic judge.',
    'The case author supplied the candidate; the run says nothing about how a '
    'real producer/compiler would generate one.',
    'No production snapshot, promotion, routing, daemon or new table is '
    'performed here; the published product is a new pilot-only instance.',
    'candidate_delivered requires the full candidate body inside the injected '
    'hidden context; a locator/path hint never counts as delivery.',
    'a mocked consumer exercises the harness only and can never reach '
    'assess/publish; unknowns are reported, never forged.',
)

_fail = evaluate._fail
_exact_keys = evaluate._exact_keys
_text = evaluate._text


# ------------------------------------------------------------------ validation

def _pilot_evidence(refs, label, source_keys, allow_empty):
    if not isinstance(refs, list) or len(refs) > MAX_REFS or (not refs and not allow_empty):
        _fail(label + (' must be a list of at most %d entries' % MAX_REFS if allow_empty
                       else ' must be a nonempty list of at most %d entries' % MAX_REFS))
    seen = set()
    for ref in refs:
        _exact_keys(ref, ('source', 'quote'), label + ' entry', required=('source', 'quote'))
        key = _text(ref['source'], label + ' source')
        quote = _text(ref['quote'], label + ' quote')
        if key not in source_keys:
            _fail(label + ' cites unknown source key: ' + key)
        if quote not in source_keys[key]:
            _fail(label + ' quote absent from source')
        if (key, quote) in seen:
            _fail(label + ' has a duplicate evidence reference')
        seen.add((key, quote))


def _pilot_basis(refs, label, card_keys, owner):
    if not isinstance(refs, list) or len(refs) > MAX_REFS:
        _fail(label + ' must be a list of at most %d card keys' % MAX_REFS)
    seen = set()
    for ref in refs:
        if not isinstance(ref, str) or not ref.strip():
            _fail(label + ' entries must be nonempty card keys')
        if ref == owner:
            _fail(label + ' cannot reference its own card')
        if ref not in card_keys:
            _fail(label + ' references a card outside this case: ' + ref)
        if ref in seen:
            _fail(label + ' has a duplicate entry: ' + ref)
        seen.add(ref)


def _dependency_order(cards):
    """Deterministic topological order (dependencies first); rejects cycles."""
    by_key = {c['key']: c for c in cards}
    order, state = [], {}

    def visit(key, stack):
        mark = state.get(key)
        if mark == 'done':
            return
        if mark == 'open':
            _fail('card basis cycle: ' + ' -> '.join(stack + [key]))
        state[key] = 'open'
        for ref in by_key[key]['basis']:
            visit(ref, stack + [key])
        state[key] = 'done'
        order.append(key)

    for card in cards:
        visit(card['key'], [])
    return order


def validate_pilot_case(case):
    """Full structural validation before any directory, instance or model call.

    Synthetic-only, self-contained: every evidence quote is checked verbatim
    against its in-case source, every basis reference must name a base card in
    the same case, duplicates and cycles are rejected, and the candidate is a
    single new framework card whose nonempty basis references only base cards.
    """
    _exact_keys(case, ('schema', 'id', 'version', 'project', 'material', 'sources',
                       'cards', 'candidate', 'review', 'tasks'), 'pilot case',
                required=('schema', 'id', 'version', 'project', 'material',
                          'sources', 'cards', 'candidate', 'review', 'tasks'))
    if len(json.dumps(case, allow_nan=False)) > MAX_CASE_BYTES:
        _fail('case too large')
    if type(case['schema']) is not int or case['schema'] != SCHEMA:
        _fail('case schema must be the integer %d' % SCHEMA)
    for field in ('id', 'version'):
        _text(case[field], 'case ' + field)
    project = _text(case['project'], 'case project')
    if not _PROJECT_RE.match(project) or '..' in project:
        _fail('case project must be a plain identifier, never a path')
    if case['material'] != SYNTHETIC:
        _fail('pilot accepts synthetic material only')
    sources = case['sources']
    if not isinstance(sources, list) or not 1 <= len(sources) <= MAX_SOURCES:
        _fail('case requires 1..%d sources' % MAX_SOURCES)
    source_keys = {}
    for source in sources:
        _exact_keys(source, ('key', 'role', 'text'), 'source',
                    required=('key', 'role', 'text'))
        key = _text(source['key'], 'source key')
        if key in source_keys:
            _fail('duplicate source key: ' + key)
        if source['role'] not in ROLES:
            _fail('source role must be user or assistant')
        if len(_text(source['text'], 'source text')) > MAX_SOURCE_TEXT:
            _fail('source text too large')
        source_keys[key] = source['text']
    cards = case['cards']
    if not isinstance(cards, list) or not 1 <= len(cards) <= MAX_CARDS:
        _fail('case requires 1..%d base cards' % MAX_CARDS)
    card_keys = set()
    bodies = {}
    for card in cards:
        _exact_keys(card, ('key', 'kind', 'topic', 'title', 'body', 'evidence', 'basis'),
                    'card', required=('key', 'kind', 'topic', 'title', 'body',
                                      'evidence', 'basis'))
        key = _text(card['key'], 'card key')
        if key in card_keys:
            _fail('duplicate card key: ' + key)
        card_keys.add(key)
        if card['kind'] not in CARD_KINDS:
            _fail('card kind must be memory, knowledge or framework')
        _text(card['topic'], 'card topic')
        _text(card['title'], 'card title')
        if len(_text(card['body'], 'card body')) > MAX_BODY:
            _fail('card body too large')
    for card in cards:
        key = card['key']
        _pilot_basis(card['basis'], 'card basis', card_keys, key)
        # A basis-free card must ground itself in evidence; a basis-bearing
        # card may leave evidence empty, matching core apply semantics.
        _pilot_evidence(card['evidence'], 'card evidence', source_keys,
                        allow_empty=bool(card['basis']))
        norm = ' '.join(card['body'].split()).casefold()
        if norm in bodies:
            _fail('duplicate active body in case: ' + key)
        bodies[norm] = key
    _dependency_order(cards)  # cycle check over the frozen basis graph
    candidate = case['candidate']
    _exact_keys(candidate, ('key', 'kind', 'topic', 'title', 'body', 'evidence',
                            'basis', 'reason'), 'candidate',
                required=('key', 'kind', 'topic', 'title', 'body', 'evidence',
                          'basis', 'reason'))
    ckey = _text(candidate['key'], 'candidate key')
    if ckey in card_keys:
        _fail('candidate key duplicates a base card: ' + ckey)
    if candidate['kind'] != 'framework':
        _fail('candidate kind must be framework')
    _text(candidate['topic'], 'candidate topic')
    _text(candidate['title'], 'candidate title')
    if len(_text(candidate['body'], 'candidate body')) > MAX_BODY:
        _fail('candidate body too large')
    _text(candidate['reason'], 'candidate reason')
    if not isinstance(candidate['basis'], list) or not candidate['basis']:
        _fail('candidate basis must be a nonempty list of base card keys')
    _pilot_basis(candidate['basis'], 'candidate basis', card_keys, ckey)
    _pilot_evidence(candidate['evidence'], 'candidate evidence', source_keys,
                    allow_empty=True)
    if ' '.join(candidate['body'].split()).casefold() in bodies:
        _fail('candidate body duplicates a base card body')
    review = case['review']
    _exact_keys(review, ('approved', 'findings', 'proposal_hash', 'reviewer'), 'review',
                required=('approved', 'findings', 'proposal_hash', 'reviewer'))
    if review['approved'] is not True or review['findings'] != []:
        _fail('candidate requires an approved review with empty findings')
    if review['proposal_hash'] != evaluate.digest(candidate):
        _fail('review proposal_hash must match the exact candidate')
    _text(review['reviewer'], 'review reviewer')
    tasks = case['tasks']
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= MAX_TASKS:
        _fail('case requires 1..%d tasks' % MAX_TASKS)
    task_ids = set()
    for task in tasks:
        _exact_keys(task, ('id', 'prompt', 'expect'), 'task',
                    required=('id', 'prompt', 'expect'))
        tid = _text(task['id'], 'task id')
        if tid in task_ids:
            _fail('duplicate task id: ' + tid)
        task_ids.add(tid)
        _text(task['prompt'], 'task prompt')
        expect = task['expect']
        if not isinstance(expect, dict) or not expect:
            _fail('task expect must be a nonempty object')
        for ekey, value in expect.items():
            if not isinstance(ekey, str) or not ekey:
                _fail('task expect keys must be nonempty strings')
            if not isinstance(value, (str, int, float, bool, type(None))):
                _fail('task expect values must be JSON scalars')
    return case


# ------------------------------------------------------------- materialization

def materialize(root, case, arm):
    """Build a fresh isolated instance through real core APIs.

    Base cards are created in topological order through the real
    ``KnowledgeStore``: basis-free cards via ``seed`` (as ``evaluate.prepare``
    does), basis-bearing cards via a per-card ``apply(create)`` so dependency
    validation and ``relations.set`` stay inside core instead of being
    bypassed. The candidate framework is created via ``apply`` with a review
    bound to the exact translated operation. Publication goes through
    ``KnowledgeFiles`` and every card is read back. Both arms carry identical
    base facts; the only difference is the candidate card.
    """
    from .engine import Engine
    from .evolution import Evolution
    from .knowledge_files import KnowledgeFiles
    validate_pilot_case(case)
    if arm not in ARMS:
        _fail('arm must be one of: ' + ', '.join(ARMS))
    root = Path(root).absolute()
    evaluate._check_out(root)
    root.mkdir(parents=True, exist_ok=False)
    engine = Engine(root / 'home')
    try:
        engine.init(root / 'vault', evolving=True)
        evaluate.save(engine.home / 'config.json',
                      {'knowledge_mode': 'evolving', 'model_command': None})
        evolution = Evolution(engine)
        source_ids = {}
        for source in case['sources']:
            receipt = engine.store.capture(case['project'], 'fixture', source['role'],
                                           source['key'], source['text'])
            evolution.captured(receipt['source_id'], source['role'], 'fixture')
            source_ids[source['key']] = receipt['source_id']

        def evidence(refs):
            return [{'source_id': source_ids[r['source']], 'version': 1,
                     'quote': r['quote']} for r in refs]

        by_key = {c['key']: c for c in case['cards']}
        cards = {}
        for key in _dependency_order(case['cards']):
            card = by_key[key]
            refs = evidence(card['evidence'])
            if card['basis']:
                op = {'op': 'create', 'key': key, 'kind': card['kind'],
                      'topic': card['topic'], 'title': card['title'],
                      'body': card['body'], 'evidence': refs,
                      'reason': 'pilot base card with declared basis',
                      'basis_cards': [{'card_id': cards[k]['id'],
                                       'version': cards[k]['version']}
                                      for k in card['basis']]}
                evolution.cards.apply('pilot-base:' + key, case['project'], [op],
                                      {'approved': True, 'findings': [],
                                       'proposal_hash': evaluate.digest([op])})
                created = evolution.cards._get_by_key(case['project'], key)
            else:
                evolution.cards._validate_evidence(refs, case['project'])
                created = evolution.cards.seed(case['project'], key, card['kind'],
                                               card['topic'], card['title'],
                                               card['body'], evidence=refs)
            cards[key] = created
        evolution.baseline()  # the synthetic fixture is already curated
        result = {'arm': arm, 'material': SYNTHETIC, 'pilot_only': True,
                  'sources': dict(source_ids)}
        if arm == 'candidate':
            candidate = case['candidate']
            op = {'op': 'create', 'key': candidate['key'], 'kind': 'framework',
                  'topic': candidate['topic'], 'title': candidate['title'],
                  'body': candidate['body'], 'evidence': evidence(candidate['evidence']),
                  'reason': candidate['reason'],
                  'basis_cards': [{'card_id': cards[k]['id'],
                                   'version': cards[k]['version']}
                                  for k in candidate['basis']]}
            core_review = {'approved': True, 'findings': [],
                           'proposal_hash': evaluate.digest([op]),
                           'reviewer': case['review']['reviewer']}
            evolution.cards.apply('pilot-candidate:' + evaluate.digest(candidate),
                                  case['project'], [op], core_review)
            cards[candidate['key']] = evolution.cards._get_by_key(case['project'],
                                                                  candidate['key'])
            result['operation_hash'] = evaluate.digest([op])
            result['case_proposal_hash'] = case['review']['proposal_hash']
        files = KnowledgeFiles(engine)
        publication = files.publish()
        if publication['conflicts'] or publication['missing']:
            raise ValueError('pilot publication incomplete')
        for card in evolution.cards.list(case['project']):
            if not evolution._published(card['id']):
                raise ValueError('published card readback failed')
        if files.root.resolve() != (root / 'vault').resolve():
            raise ValueError('isolated vault binding mismatch')
        result.update(home=str(engine.home), vault=str(files.root.resolve()),
                      publication=publication,
                      cards={c['key']: {'id': c['id'], 'version': c['version'],
                                        'body_sha256': c['body_sha256'],
                                        'basis': c['basis_cards']}
                             for c in evolution.cards.list(case['project'])})
        return result
    finally:
        engine.store.close()


# ------------------------------------------------------------------ inspection

def inspect_arm(case, root, arm, task, answer):
    """Re-verify one cell from the ledger, published files and raw Pi events.

    Rechecks the complete initial sources, the exact card set (base plus the
    candidate on the candidate arm), body/evidence/basis/version/project/
    state, file tracking and hashes; then rechecks the native task, the
    injected hidden context, the answer and the resolved model identity from
    raw events and the consumption receipt. Framework cards and dependencies
    are verified, never rejected. The candidate counts as delivered only when
    its full body appears in the injected context; a locator never counts.
    Unknowns raise; they are never forged.
    """
    if arm not in ARMS:
        _fail('arm must be one of: ' + ', '.join(ARMS))
    if not isinstance(answer, str):
        raise ValueError('answer artifact missing')
    root = Path(root)
    db = _connect(root / 'home')
    try:
        vault = db.execute("SELECT value FROM meta WHERE key='vault'").fetchone()
        if not vault or Path(vault[0]).resolve() != (root / 'vault').resolve():
            raise ValueError('materialized vault binding differs')
        sources = {}
        for s in case['sources']:
            matches = db.execute(
                "SELECT s.id FROM sources s JOIN events e ON e.source_id=s.id "
                "JOIN versions v ON v.source_id=s.id AND v.version=s.current_version "
                "WHERE s.project=? AND e.event_id=? AND s.session='fixture' "
                "AND s.role=? AND v.text=?",
                (case['project'], s['key'], s['role'], s['text'])).fetchall()
            if len(matches) != 1:
                raise ValueError('materialized source differs from case')
            sid = matches[0][0]
            frozen_source = _source(db, sid)
            if frozen_source['current_version'] != 1:
                raise ValueError('materialized source version differs')
            sources[s['key']] = sid
        fixture_ids = {r[0] for r in db.execute("SELECT id FROM sources WHERE session='fixture'")}
        if fixture_ids != set(sources.values()):
            raise ValueError('materialized source set differs')
        rows = db.execute('SELECT * FROM knowledge_cards ORDER BY id').fetchall()
        ids_by_key = {dict(r)['key']: dict(r)['id'] for r in rows}
        by_key = {c['key']: c for c in case['cards']}
        expected_keys = set(by_key)
        if arm == 'candidate':
            expected_keys.add(case['candidate']['key'])
        if len(rows) != len(expected_keys) or set(ids_by_key) != expected_keys:
            raise ValueError('materialized card set differs from case')
        actual = {}
        for row in rows:
            c = dict(row)
            spec = (case['candidate'] if arm == 'candidate'
                    and c['key'] == case['candidate']['key'] else by_key[c['key']])
            if (c['project'] != case['project'] or c['state'] != 'active'
                    or c['version'] != 1 or c['manually_edited']):
                raise ValueError('materialized card scope or version differs')
            if (c['kind'] != spec['kind'] or c['topic'] != spec['topic']
                    or c['title'] != spec['title'] or c['body'] != spec['body']):
                raise ValueError('materialized card content differs from case')
            refs = [{'source_id': sources[r['source']], 'version': 1,
                     'quote': r['quote']} for r in spec['evidence']]
            if json.loads(c['evidence']) != refs:
                raise ValueError('materialized evidence differs from case')
            basis_rows = db.execute(
                'SELECT basis_id, basis_version FROM knowledge_dependencies '
                'WHERE card_id=? ORDER BY basis_id', (c['id'],)).fetchall()
            expected_basis = sorted(ids_by_key[k] for k in spec['basis'])
            if ([r['basis_id'] for r in basis_rows] != expected_basis
                    or any(r['basis_version'] != 1 for r in basis_rows)):
                raise ValueError('materialized basis differs from case')
            tr = db.execute('SELECT * FROM knowledge_files WHERE card_id=?',
                            (c['id'],)).fetchone()
            if (not tr or tr['state'] != 'ok' or tr['version'] != 1
                    or tr['file_hash'] != _file_digest(db, c)):
                raise ValueError('materialized file changed')
            actual[c['id']] = c
        receipts = db.execute('SELECT * FROM knowledge_consumption').fetchall()
        events = [json.loads(line)
                  for line in (root / 'events.jsonl').read_text(
                      encoding='utf-8', errors='replace').splitlines() if line.strip()]
        contexts = [e['message'] for e in events
                    if e.get('type') == 'message_end'
                    and isinstance(e.get('message'), dict)
                    and e['message'].get('customType') == 'tongle-next-context']
        users = [e['message'] for e in events
                 if e.get('type') == 'message_end'
                 and isinstance(e.get('message'), dict)
                 and e['message'].get('role') == 'user']
        if (len(users) != 1
                or users[0].get('content') != [{'type': 'text', 'text': task['prompt']}]):
            raise ValueError('native user task differs')
        messages = [e['message'] for e in events
                    if e.get('type') == 'message_end'
                    and isinstance(e.get('message'), dict)
                    and e['message'].get('role') == 'assistant']
        if len(messages) != 1 or messages[0].get('stopReason') != 'stop':
            raise ValueError('invalid raw native answer')
        message = messages[0]
        text = ''.join(b.get('text', '') for b in message.get('content', [])
                       if isinstance(b, dict) and b.get('type') == 'text')
        if text != answer:
            raise ValueError('answer differs from raw events')
        if not message.get('provider') or not message.get('model'):
            raise ValueError('resolved model identity unavailable')
        if len(receipts) != 1:
            raise ValueError('ambiguous experiment receipt')
        receipt = dict(receipts[0])
        if receipt['project'] != case['project'] or receipt['query'] != task['prompt'][:4000]:
            raise ValueError('receipt task mismatch')
        # A successful query may offer no knowledge. Pi correctly omits the
        # empty context and therefore never binds this receipt to the answer.
        # Verify both captured turns in its session; do not invent delivery.
        if not contexts and json.loads(receipt['offered']) == []:
            if receipt['output_source'] or receipt['state'] != 'offered' or receipt['binding'] != 'legacy_recent':
                raise ValueError('empty-context receipt unexpectedly bound')
            turns = db.execute(
                'SELECT s.role,v.text FROM sources s JOIN versions v '
                'ON v.source_id=s.id AND v.version=s.current_version '
                'WHERE s.project=? AND s.session=?',
                (case['project'], receipt['session'])).fetchall()
            if sorted((r['role'], r['text']) for r in turns) != sorted([
                    ('user', task['prompt']), ('assistant', answer)]):
                raise ValueError('empty-context native captures differ')
            return {'cards': evaluate.digest(actual), 'sources': evaluate.digest(sources),
                    'receipt': evaluate.digest(receipt), 'context_absent': True,
                    'identity': {'provider': message['provider'], 'model': message['model']},
                    'delivered': [], 'candidate_delivered': False}
        if (receipt['binding'] != 'receipt' or not receipt['output_source']
                or receipt['project'] != case['project']
                or receipt['query'] != task['prompt'][:4000]
                or _source(db, receipt['output_source'])['text'] != answer):
            raise ValueError('receipt task/output mismatch')
        if (len(contexts) != 1
                or contexts[0].get('details', {}).get('receiptId') != receipt['id']
                or contexts[0].get('details', {}).get('promptHash')
                != hashlib.sha256(task['prompt'].encode()).hexdigest()):
            raise ValueError('native injected context/receipt differs')
        injected = contexts[0].get('content')
        if not isinstance(injected, str):
            raise ValueError('native injected context absent')
        offered = json.loads(receipt['offered'])
        seen, delivered = set(), set()
        for item in offered:
            card = actual.get(item.get('id'))
            if (not card or item['id'] in seen or item.get('version') != card['version']
                    or item.get('title') != card['title']
                    or item.get('delivery') not in ('full', 'locator')
                    or item.get('body') != (card['body'] if item.get('delivery') == 'full' else '')):
                raise ValueError('offered knowledge differs from case')
            seen.add(item['id'])
            block = CARD_FULL_FORMAT % (card['title'], card['body'])
            if item['delivery'] == 'full' and ('\n' + block + '\n') in ('\n' + injected + '\n'):
                delivered.add(item['id'])
        candidate_id = ids_by_key.get(case['candidate']['key']) if arm == 'candidate' else None
        return {'cards': evaluate.digest(actual),
                'sources': evaluate.digest(sources),
                'receipt': evaluate.digest({'receipt': receipt, 'context': contexts[0]}),
                'identity': {'provider': message['provider'], 'model': message['model']},
                'delivered': sorted(delivered),
                'candidate_delivered': bool(candidate_id and candidate_id in delivered)}
    finally:
        db.close()


# ---------------------------------------------------------------------- running

def run_pilot(case, out, pi, provider, model, max_calls=12, timeout=90,
              consumer=evaluate.consume):
    """Run the full 2-arm paired pilot once, per task in rotated arm order.

    The whole budget (tasks x 2 arms, at most 12 calls) is validated before
    any directory or model call. The consumer receives only ``project`` and
    ``id/prompt`` -- never ``expect`` or the case artifact -- in a fresh
    instance and fresh session per cell. Every cell's raw events, materialized
    mapping and row are preserved without overwrite; infra failures are
    recorded with their reason and never hidden from the denominator. A mock
    consumer marks the manifest non-native and can never pass assess/publish.
    """
    validate_pilot_case(case)
    arms = list(ARMS)
    total_calls = len(case['tasks']) * len(arms)
    if not isinstance(max_calls, int) or isinstance(max_calls, bool) \
            or not 1 <= max_calls <= 12:
        raise ValueError('max_calls must be an integer 1..12')
    if total_calls > max_calls:
        raise ValueError('full paired pilot requires %d calls, budget is %d'
                         % (total_calls, max_calls))
    if not isinstance(timeout, int) or isinstance(timeout, bool) \
            or not 1 <= timeout <= 180:
        raise ValueError('timeout must be an integer 1..180 seconds')
    out = Path(out).expanduser().absolute()
    evaluate._check_out(out)
    out.mkdir(parents=True, exist_ok=False)
    run_id = uuid.uuid4().hex
    evaluate.save(out / 'case.json', case)
    evaluate.save(out / 'manifest.json',
                  {'schema': 1, 'run_id': run_id, 'case_hash': evaluate.digest(case),
                   'provider': provider, 'model': model, 'arms': arms,
                   'timeout': timeout, 'total_calls': total_calls,
                   'answer_budget_tokens': evaluate.ANSWER_BUDGET_TOKENS,
                   'created_at': time.time(), 'code_hash': evaluate.code_hash(),
                   'consumer_blind': False, 'trial': 'first_pair',
                   'native_consumer': consumer is evaluate.consume,
                   'consumer': 'native' if consumer is evaluate.consume else 'synthetic_mock',
                   'material': SYNTHETIC, 'pilot_only': True,
                   'limitations': list(PILOT_LIMITATIONS),
                   'scope': 'non-blind selected-case synthetic pilot, native consumer'})
    rows = []
    for index, task in enumerate(case['tasks']):
        order = arms if index % 2 == 0 else arms[::-1]  # rotate arm order per task
        for arm in order:
            root = out / ('%02d-%s' % (index, arm))
            row = {'task': task['id'], 'arm': arm, 'status': 'infra_error',
                   'seconds': None, 'identity': None}
            try:
                row['materialized'] = materialize(root, case, arm)
                result = consumer(root, {'project': case['project']},
                                  {'id': task['id'], 'prompt': task['prompt']},
                                  arm, pi, provider, model, timeout)
                evaluate.record_identity(row, result)
                row.update(result)
                row['status'] = 'completed'
                row['assessment'] = evaluate.grade(result['answer'], task['expect'])
                row['consumption'] = evaluate.summary(root / 'home', case['project'])
                if consumer is evaluate.consume:
                    row['inspection'] = inspect_arm(case, root, arm, task, result['answer'])
                    row['raw_events_hash'] = hashlib.sha256((root / 'events.jsonl').read_bytes()).hexdigest()
            except KeyboardInterrupt:
                row.update(status='cancelled', reason='interrupted')
                evaluate.save(out / ('%02d-%s.json' % (index, arm)), row)
                raise
            except (ValueError, KeyError, TypeError, OSError, sqlite3.Error,
                    subprocess.SubprocessError) as exc:
                row['status'] = 'infra_error'
                row['reason'] = type(exc).__name__ + ': ' + str(exc)[:400]
            rows.append(row)
            evaluate.save(out / ('%02d-%s.json' % (index, arm)), row)  # never overwritten
    report = evaluate.compare(rows, arms)
    report.update(run_id=run_id, case_hash=evaluate.digest(case),
                  material=SYNTHETIC, pilot_only=True,
                  native_consumer=consumer is evaluate.consume,
                  promotion='manual_review_only', generalization='not established',
                  consumer_blind=False, limitations=list(PILOT_LIMITATIONS),
                  scope='non-blind selected-case synthetic pilot, native consumer')
    evaluate.save(out / 'report.json', report)
    return report


# ------------------------------------------------------------------- assessment

def assess_run(case, run):
    """Recompute the full matrix from frozen case, raw events and ledgers.

    Missing or extra cells, failed cells, tampered answers/material/identity/
    context and non-native consumers are all rejected; report.json is never
    trusted. ``disposition`` is:

    * ``validated_in_scope`` -- every candidate hard criterion passes, at
      least one current failure is fixed, and the candidate was actually
      delivered (full body in the hidden context) on such a fixed task;
    * ``regression`` -- any task the current arm passed now fails;
    * ``no_gain`` -- every task ties (including both-pass and both-fail);
    * ``needs_evidence`` -- anything else: an undelivered fix, or a fix beside
      still-failing ties; errors and unknowns stay separate, never averaged.

    The returned proof carries ``proof_hash`` = ``evaluate.digest`` of the
    proof without that field, so any party can recompute the binding.
    """
    validate_pilot_case(case)
    run = Path(run)
    manifest = _read(run / 'manifest.json')
    if (_read(run / 'case.json') != case
            or manifest.get('case_hash') != evaluate.digest(case)
            or manifest.get('native_consumer') is not True
            or manifest.get('material') != SYNTHETIC
            or manifest.get('pilot_only') is not True
            or manifest.get('arms') != list(ARMS)
            or manifest.get('code_hash') != evaluate.code_hash()
            or manifest.get('total_calls') != len(case['tasks']) * 2):
        raise ValueError('run is not an exact native two-arm synthetic pilot')
    scores, delivered, evidence_hashes, unknown_usage, failed = {}, {}, {}, [], []
    for i, task in enumerate(case['tasks']):
        scores[task['id']] = {}
        for arm in ARMS:
            name = '%02d-%s' % (i, arm)
            row = _read(run / (name + '.json'))
            root = run / name
            if row.get('task') != task['id'] or row.get('arm') != arm:
                raise ValueError('pilot cell identity differs: ' + name)
            if row.get('status') in ('infra_error', 'cancelled'):
                scores[task['id']][arm] = None
                failed.append({'cell': name, 'status': row['status'], 'reason': row.get('reason')})
                evidence_hashes[name] = {'hash': evaluate.digest(row), 'candidate_delivered': False}
                continue
            if row.get('status') != 'completed':
                raise ValueError('invalid pilot cell status: ' + name)
            if row.get('raw_events_hash') != hashlib.sha256((root / 'events.jsonl').read_bytes()).hexdigest():
                raise ValueError('raw native events changed: ' + name)
            inspected = inspect_arm(case, root, arm, task, row.get('answer'))
            identity = inspected['identity']
            if row.get('identity') != identity or (row.get('provider'), row.get('model')) != (identity['provider'], identity['model']):
                raise ValueError('row identity differs from native events: ' + name)
            if ((identity['provider'], identity['model'])
                    != (manifest.get('provider'), manifest.get('model'))):
                raise ValueError('model identity mismatch in cell ' + name)
            scores[task['id']][arm] = evaluate.grade(row['answer'], task['expect'])['pass']
            evidence_hashes[name] = {
                'hash': evaluate.digest({'row': row, 'inspection': inspected}),
                'candidate_delivered': inspected['candidate_delivered']}
            if not row.get('usage'):
                unknown_usage.append(name)
        delivered[task['id']] = evidence_hashes['%02d-candidate' % i]['candidate_delivered']
    expected = {'%02d-%s.json' % (i, a)
                for i in range(len(case['tasks'])) for a in ARMS}
    if {p.name for p in run.glob('[0-9][0-9]-*.json')} != expected:
        raise ValueError('duplicate or unexpected pilot cell')
    fixed = [t['id'] for t in case['tasks']
             if scores[t['id']]['current'] is False and scores[t['id']]['candidate'] is True]
    broken = [t['id'] for t in case['tasks']
              if scores[t['id']]['current'] is True and scores[t['id']]['candidate'] is False]
    all_candidate = all(scores[t['id']]['candidate'] for t in case['tasks'])
    fixed_delivered = [tid for tid in fixed if delivered[tid]]
    if failed:
        disposition = 'needs_evidence'
    elif broken:
        disposition = 'regression'
    elif all_candidate and fixed_delivered:
        disposition = 'validated_in_scope'
    elif not fixed:
        disposition = 'no_gain'
    else:
        disposition = 'needs_evidence'
    proof = {'schema': 1, 'case_hash': evaluate.digest(case),
             'manifest_hash': evaluate.digest(manifest),
             'evidence_hash': evaluate.digest(evidence_hashes),
             'arms': list(ARMS), 'total_tasks': len(case['tasks']),
             'scores': scores, 'candidate_delivered': delivered,
             'fixed': fixed, 'fixed_and_delivered': fixed_delivered,
             'regressed': broken,
             'unknown_usage_cells': sorted(unknown_usage),
             'failed_cells': failed, 'total_cells': len(case['tasks']) * 2,
             'disposition': disposition,
             'identity': {'provider': manifest.get('provider'),
                          'model': manifest.get('model')},
             'material': SYNTHETIC, 'pilot_only': True,
             'scope': 'synthetic isolated pilot only; not evidence of real user benefit'}
    proof['proof_hash'] = evaluate.digest(
        {k: v for k, v in proof.items() if k != 'proof_hash'})
    return proof


# ------------------------------------------------------------------ publishing

def publish_pilot(case, run, review, out):
    """Publish a validated pilot into a brand-new pilot-only instance.

    Requires an assess_run proof of ``validated_in_scope`` and an independent
    review binding the exact recomputed proof hash. The instance is built only
    inside the new ``out`` directory through core apply/publish/readback, so
    the framework candidate keeps its dependency basis; a ``pilot.json``
    marker records synthetic/pilot_only. There is no production path
    parameter and ``no_gain`` can never publish. If instance creation fails,
    the partial directory is kept as evidence; generic recovery is out of
    scope for this round. New-session consumption is performed by ROOT reusing
    the published instance home with ``evaluate.consume``; this module never
    regenerates or modifies the candidate afterwards.
    """
    validate_pilot_case(case)
    proof = assess_run(case, run)
    if proof['disposition'] != 'validated_in_scope':
        raise ValueError('pilot publish requires validated_in_scope, got '
                         + proof['disposition'])
    if (not isinstance(review, dict) or review.get('approved') is not True
            or review.get('findings') != []
            or not isinstance(review.get('reviewer'), str)
            or not review['reviewer'].strip()
            or review.get('proof_hash') != proof['proof_hash']):
        raise ValueError('independent review must bind the exact recomputed proof')
    out = Path(out).expanduser().absolute()
    evaluate._check_out(out)
    out.mkdir(parents=True, exist_ok=False)
    materialized = materialize(out / 'instance', case, 'candidate')
    candidate_info = materialized['cards'].get(case['candidate']['key'])
    if not candidate_info or not candidate_info['basis']:
        raise ValueError('published candidate lost its framework basis')
    marker = {'material': SYNTHETIC, 'pilot_only': True,
              'case_hash': proof['case_hash'], 'proof_hash': proof['proof_hash'],
              'reviewer': review['reviewer'],
              'note': 'synthetic pilot-only instance; never a production home; '
                      'new-session consumption is performed by ROOT'}
    evaluate.save(Path(materialized['home']) / 'pilot.json', marker)
    result = {'state': 'published', 'material': SYNTHETIC, 'pilot_only': True,
              'case_hash': proof['case_hash'], 'proof_hash': proof['proof_hash'],
              'disposition': proof['disposition'],
              'instance': {'home': materialized['home'], 'vault': materialized['vault']},
              'cards': materialized['cards'], 'candidate': candidate_info,
              'operation_hash': materialized.get('operation_hash'),
              'case_proposal_hash': materialized.get('case_proposal_hash'),
              'publication': materialized['publication'],
              'consumption': ('ROOT may reuse this instance home with '
                              'evaluate.consume in a new session; this module '
                              'never regenerates or modifies the candidate'),
              'limitations': list(PILOT_LIMITATIONS)}
    evaluate.save(out / 'publish-result.json', result)
    return result


# ------------------------------------------------------------------------- CLI

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('validate', help='structural validation only; no external calls')
    p.add_argument('--case', required=True)
    p = sub.add_parser('run', help='run the paired native-consumer pilot')
    p.add_argument('--case', required=True)
    p.add_argument('--out', required=True)
    p.add_argument('--pi', required=True)
    p.add_argument('--provider', required=True)
    p.add_argument('--model', required=True)
    p.add_argument('--max-calls', type=int, default=12)
    p.add_argument('--timeout', type=int, default=90)
    p = sub.add_parser('assess', help='recompute the proof from raw evidence; no external calls')
    p.add_argument('--case', required=True)
    p.add_argument('--run', required=True)
    p = sub.add_parser('publish', help='publish a validated pilot into a new pilot-only instance')
    p.add_argument('--case', required=True)
    p.add_argument('--run', required=True)
    p.add_argument('--review', required=True)
    p.add_argument('--out', required=True)
    args = parser.parse_args()
    case = json.loads(Path(args.case).read_text(encoding='utf-8'))
    if args.command == 'validate':
        validate_pilot_case(case)
        return {'valid': True, 'case_hash': evaluate.digest(case),
                    'material': SYNTHETIC}
    if args.command == 'run':
        return run_pilot(case, args.out, args.pi, args.provider, args.model,
                         args.max_calls, args.timeout)
    if args.command == 'assess':
        return assess_run(case, args.run)
    return publish_pilot(case, args.run, _read(args.review), args.out)


if __name__ == '__main__':
    try:
        print(json.dumps(main(), ensure_ascii=False))
    except (ValueError, OSError, KeyError, TypeError, sqlite3.Error) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False))
        sys.exit(1)
