"""Explicit retrospective checkpoints, using the existing work event ledger.

Checks establish identity, freshness and declared coverage, not understanding or
long-term benefit. No scheduler, command execution or automatic global edits.
"""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import time


SURFACES = ('knowledge', 'navigation', 'project', 'method', 'hosts', 'runtime', 'consumption')
STATUSES = ('changed', 'unchanged', 'not_applicable', 'pending')
MAX_FILE_BYTES = 16 * 1024 * 1024


def _text(value, label, maximum=600):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(label + ' must be a bounded nonempty string')
    return value


def _latest(engine, project, review_id=None, include_closed=False, limit=10):
    if not engine.db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_work_units'").fetchone():
        return []
    # Filter before LIMIT: ordinary finishes and newer closed batches cannot
    # hide an older open batch. rowid resolves equal wall-clock timestamps.
    sql = """SELECT w.result FROM memory_work_units w WHERE w.rowid IN (
        SELECT MAX(rowid) FROM memory_work_units WHERE project=?
        AND json_extract(payload,'$.kind')='review'
        GROUP BY json_extract(payload,'$.review_id'))"""
    params = [project]
    if review_id is not None:
        sql += " AND json_extract(w.payload,'$.review_id')=?"
        params.append(review_id)
    elif not include_closed:
        sql += " AND json_extract(w.result,'$.status')!='closed'"
    sql += ' ORDER BY w.rowid DESC LIMIT ?'
    params.append(limit)
    return [json.loads(row[0]) for row in engine.db.execute(sql, params)]


def pending_reviews(engine, project, limit=3):
    """Read-only, project-local summaries for existing context delivery."""
    if type(limit) is not int or not 1 <= limit <= 10:
        raise ValueError('review limit must be 1..10')
    return [{key: item[key] for key in ('review_id', 'scope', 'revision', 'status')}
            for item in _latest(engine, project, limit=limit)]


class ReviewWork:
    def __init__(self, engine):
        self.engine = engine
        self.db = engine.db

    def pending(self, project, limit=3):
        return pending_reviews(self.engine, project, limit)

    def status(self, project, review_id=None, limit=10, include_closed=False):
        if review_id is not None:
            _text(review_id, 'review_id', 160)
        if type(limit) is not int or not 1 <= limit <= 10 or type(include_closed) is not bool:
            raise ValueError('invalid review status options')
        return {'reviews': _latest(self.engine, project, review_id, include_closed, limit),
                'evidence_freshness': 'recorded_checkpoint_only; use check to revalidate',
                'benefit': 'unknown', 'queued_jobs': 0}

    def run(self, project, session, event_key, payload):
        if not isinstance(payload, dict):
            raise ValueError('review input must be an object')
        op = payload.get('op')
        allowed = {
            'start': {'op', 'review_id', 'scope', 'reason'},
            'status': {'op', 'review_id', 'limit', 'include_closed'},
            'check': {'op', 'review_id', 'expected_revision', 'assessments', 'resolutions'},
            'close': {'op', 'review_id', 'expected_revision', 'assessments', 'resolutions'},
        }
        if not isinstance(op, str) or op not in allowed or set(payload) - allowed[op]:
            raise ValueError('invalid review operation or fields')
        if op == 'status':
            if event_key is not None:
                raise ValueError('review status is read-only; no event identity')
            return self.status(project, payload.get('review_id'), payload.get('limit', 10),
                               payload.get('include_closed', False))
        _text(session, 'host session', 400)
        _text(event_key, 'stable event_key', 1000)
        rid = _text(payload.get('review_id'), 'review_id', 160)
        canonical = json.dumps(dict(payload, kind='review'), ensure_ascii=False,
                               sort_keys=True, separators=(',', ':'))
        if len(canonical) > 64000:
            raise ValueError('review input too large')
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        from .learning_work import LearningWork
        LearningWork(self.engine)  # reuse the work ledger; status never creates it
        with self.engine.locked(), self.engine.transaction():
            prior = self.db.execute('SELECT session,payload_hash,result FROM memory_work_units WHERE project=? AND event_key=?',
                                    (project, event_key)).fetchone()
            if prior:
                if prior['session'] != session or prior['payload_hash'] != digest:
                    raise ValueError('review event reused with different content or session')
                return dict(json.loads(prior['result']), replayed=True)
            rows = _latest(self.engine, project, rid)
            current = rows[0] if rows else None
            now = time.time()
            if op == 'start':
                if current:
                    raise ValueError('review_id already exists; resume its latest revision')
                result = {'review_id': rid, 'scope': _text(payload.get('scope'), 'scope', 400),
                          'reason': _text(payload.get('reason'), 'reason'),
                          'producer_session': session, 'started_at': now,
                          'status': 'open', 'revision': 1, 'closed_at': None,
                          'assessments': {s: {'status': 'pending', 'reason': 'not assessed', 'evidence': []} for s in SURFACES},
                          'failures': [], 'resolutions': [],
                          'blockers': [{'surface': s, 'reason': 'not assessed'} for s in SURFACES],
                          'mechanical_status': 'not_checked', 'semantic_quality': 'not_assessed'}
            else:
                if not current:
                    raise ValueError('review must be started before check or close')
                revision = payload.get('expected_revision')
                if type(revision) is not int or revision != current['revision']:
                    raise ValueError('review revision changed; read status and reconcile')
                if op == 'check' and 'assessments' not in payload:
                    raise ValueError('review check requires assessments')
                assessments = payload.get('assessments', current['assessments'])
                result = self._check(project, current, assessments, payload.get('resolutions', []))
                result.update(revision=revision + 1, status='open', closed_at=None)
                if not result['blockers'] and op == 'close':
                    result.update(status='closed', closed_at=now)
            result.update(operation=op, checked_at=now, replayed=False, benefit='unknown', queued_jobs=0,
                          evidence_freshness='at_checkpoint_only',
                          scope_assessment='declared; completeness and relevance require review')
            self.db.execute('INSERT INTO memory_work_units VALUES(?,?,?,?,?,?,?)',
                            (project, session, event_key, digest, canonical,
                             json.dumps(result, ensure_ascii=False), now))
            return result

    def _check(self, project, current, assessments, resolutions):
        if not isinstance(assessments, dict) or set(assessments) - set(SURFACES):
            raise ValueError('invalid review assessment surfaces')
        if not isinstance(resolutions, list) or len(resolutions) > 12:
            raise ValueError('review resolutions must be a list of at most 12')
        blockers, checked, observed_failures = [], [], []
        for surface in SURFACES:
            item = assessments.get(surface)
            if item is None:
                blockers.append({'surface': surface, 'reason': 'missing assessment'})
                continue
            if (not isinstance(item, dict) or set(item) != {'status', 'reason', 'evidence'} or
                    item['status'] not in STATUSES):
                raise ValueError('invalid assessment for ' + surface)
            _text(item['reason'], 'assessment reason')
            evidence = item['evidence']
            if not isinstance(evidence, list) or len(evidence) > 12:
                raise ValueError('assessment evidence must be a list of at most 12')
            if item['status'] == 'pending':
                blockers.append({'surface': surface, 'reason': 'pending assessment'})
            if item['status'] == 'changed' and not evidence:
                blockers.append({'surface': surface, 'reason': 'changed surface needs evidence'})
            if surface == 'consumption' and (item['status'] != 'changed' or
                    not any(isinstance(e, dict) and e.get('kind') in ('consumption', 'consumer_artifact') for e in evidence)):
                blockers.append({'surface': surface, 'reason': 'fresh-session consumption evidence required'})
            for ref in evidence:
                try:
                    proof = self._evidence(project, current, ref)
                    checked.append(dict(proof, surface=surface))
                    if proof.get('verdict') == 'failed':
                        observed_failures.append({'failure_id': proof['failure_id'],
                                                  'evidence': ref})
                except (ValueError, KeyError, OSError, UnicodeError) as exc:
                    # A repair may already have changed the consumed files. Keep
                    # an authenticated failed assessment even when its reads
                    # are no longer current; stale reads can never pass closure.
                    try:
                        failed = self._evidence(project, current, ref, verify_reads=False)
                        if failed.get('verdict') == 'failed':
                            observed_failures.append({'failure_id': failed['failure_id'], 'evidence': ref})
                    except (ValueError, KeyError, OSError, UnicodeError):
                        pass
                    # No raw file contents or exception payloads are returned.
                    blockers.append({'surface': surface, 'reason': str(exc) if isinstance(exc, ValueError)
                                     else 'evidence unavailable (' + type(exc).__name__ + ')'})
        failures = {f['failure_id']: f for f in current['failures']}
        for failure in observed_failures:
            failures.setdefault(failure['failure_id'], failure)
        resolved = {r['failure_id']: r for r in current['resolutions']}
        # Retained resolutions are rechecked too; a changed assessment or
        # withdrawn answer cannot silently keep an old failed task resolved.
        for resolution in resolutions:
            if not isinstance(resolution, dict) or set(resolution) != {'failure_id', 'reason', 'evidence'}:
                raise ValueError('invalid review resolution')
            fid = _text(resolution['failure_id'], 'failure_id', 240)
            _text(resolution['reason'], 'resolution reason')
            if fid not in failures:
                raise ValueError('resolution references unknown failure')
            # Replacing a stale resolution is explicit, retained in the
            # append-only earlier event, and revalidated below.
            resolved[fid] = resolution
        for fid, resolution in resolved.items():
            try:
                proof = self._evidence(project, current, resolution['evidence'])
                if proof.get('kind') not in ('consumption', 'consumer_artifact') or proof.get('verdict') != 'passed':
                    raise ValueError('resolution needs passed consumption evidence')
                checked.append(dict(proof, resolves=fid))
            except (ValueError, KeyError, OSError, UnicodeError):
                blockers.append({'surface': 'consumption', 'reason': 'resolution evidence unavailable or not passed',
                                 'failure_id': fid})
        for fid in failures:
            if fid not in resolved:
                blockers.append({'surface': 'consumption', 'reason': 'recorded consumption failure is unresolved',
                                 'failure_id': fid})
        # A currently failed test remains a blocker even when its earlier
        # failure was resolved: replace that surface with the repaired result.
        for failure in observed_failures:
            blockers.append({'surface': 'consumption', 'reason': 'current consumption assessment failed',
                             'failure_id': failure['failure_id']})
        result = dict(current)
        result.update(assessments=assessments, failures=list(failures.values()), resolutions=list(resolved.values()),
                      blockers=blockers, checks=checked,
                      mechanical_status='verified' if not blockers else 'incomplete',
                      semantic_quality='declared_review' if any(c.get('kind') in ('consumption', 'consumer_artifact') for c in checked)
                      else 'not_assessed')
        return result

    def _file(self, ref, *, assessment=False):
        if (not isinstance(ref, dict) or set(ref) != {'path', 'sha256'} or
                not isinstance(ref['path'], str) or len(ref['path']) > 4096 or
                not Path(ref['path']).is_absolute() or
                not isinstance(ref['sha256'], str) or not re.fullmatch('[a-f0-9]{64}', ref['sha256'])):
            raise ValueError('file evidence requires absolute path and SHA256')
        path = Path(ref['path'])
        before = path.stat()
        limit = 64000 if assessment else MAX_FILE_BYTES
        if not path.is_file() or before.st_size > limit:
            raise ValueError('file evidence is not a bounded regular file')
        with path.open('rb') as handle:
            data = handle.read(limit + 1)
        after = path.stat()
        if (len(data) > limit or
                (before.st_mtime_ns, before.st_size, before.st_ino) !=
                (after.st_mtime_ns, after.st_size, after.st_ino) or
                hashlib.sha256(data).hexdigest() != ref['sha256']):
            raise ValueError('file evidence changed or hash mismatches')
        return data

    def _source(self, project, ref):
        from .learning_work import LearningWork
        from .retrieval_freshness import source_current
        LearningWork._ref(ref)
        source = self.engine.store.source(ref['id'], ref['version'])
        if (source['project'] != project or source['current_version'] != ref['version'] or
                not self.engine.source_available(ref['id']) or not source_current(self.engine, ref['id'])):
            raise ValueError('source evidence unavailable, stale or outside project')
        return source

    def _evidence(self, project, current, ref, *, verify_reads=True):
        if not isinstance(ref, dict):
            raise ValueError('invalid review evidence')
        kind = ref.get('kind')
        if kind == 'file' and set(ref) == {'kind', 'path', 'sha256'}:
            self._file({k: v for k, v in ref.items() if k != 'kind'})
        elif kind == 'source' and set(ref) == {'kind', 'id', 'version'}:
            self._source(project, {k: v for k, v in ref.items() if k != 'kind'})
        elif kind == 'note' and set(ref) == {'kind', 'id', 'version'}:
            from .learning_work import LearningWork
            from .inline_notes import InlineNotes
            from .knowledge_learning import adopted_corrections_valid
            LearningWork._ref({k: v for k, v in ref.items() if k != 'kind'})
            catalog = InlineNotes(self.engine)
            card = catalog.cards.get(ref['id'])
            if (not card or card['project'] != project or card['version'] != ref['version'] or
                    card['state'] != 'active' or not catalog.evolution._valid_card(card) or
                    not adopted_corrections_valid(self.engine, card) or catalog._correction(project, card['id'])):
                raise ValueError('note evidence unavailable, stale or outside project')
            tracking = catalog.files._tracking(ref['id'])
            self._file({'path': str(catalog.files._safe_target(tracking['path'])), 'sha256': tracking['file_hash']})
        elif kind == 'consumption' and set(ref) == {'kind', 'question', 'answer', 'trace', 'assessment'}:
            question = self._source(project, ref['question'])
            answer = self._source(project, ref['answer'])
            if (question['kind'] != 'event' or answer['kind'] != 'event' or
                    question['role'] != 'user' or answer['role'] != 'assistant' or
                    not question['session'] or question['session'] != answer['session'] or
                    question['session'] == current['producer_session']):
                raise ValueError('consumption needs captured question and answer in one fresh session')
            qtime = datetime.fromisoformat(question['created_at']).timestamp()
            atime = datetime.fromisoformat(answer['created_at']).timestamp()
            if qtime < current['started_at'] or atime < qtime:
                raise ValueError('consumption must occur after batch start in question-answer order')
            self._file(ref['trace'])
            try:
                assessment = json.loads(self._file(ref['assessment'], assessment=True))
            except (json.JSONDecodeError, UnicodeError):
                raise ValueError('consumption assessment must be JSON') from None
            if (not isinstance(assessment, dict) or assessment.get('verdict') not in ('passed', 'failed') or
                    assessment.get('question_id') != question['source_id'] or
                    assessment.get('answer_id') != answer['source_id']):
                raise ValueError('consumption assessment must bind the question, answer and verdict')
            _text(assessment.get('scope'), 'consumption assessment scope', 2000)
            reads = assessment.get('reads')
            if not isinstance(reads, list) or not 1 <= len(reads) <= 12:
                raise ValueError('consumption assessment needs 1..12 actual read versions')
            for read in reads:
                if not isinstance(read, dict) or read.get('kind') not in ('file', 'note', 'source'):
                    raise ValueError('consumption reads must reference files, notes or sources')
                if verify_reads:self._evidence(project, current, read)
            return {'kind': kind, 'question': ref['question'], 'answer': ref['answer'],
                    'verdict': assessment['verdict'], 'failure_id': 'consumption:' + question['source_id'],
                    'identity': 'host_captured', 'semantic_quality': 'declared_review',
                    'meaning': 'captured identities and evidence hashes verified; assessment meaning not machine-proven'}
        elif kind == 'consumer_artifact' and set(ref) == {'kind', 'record', 'assessment'}:
            try:
                record = json.loads(self._file(ref['record'], assessment=True))
                assessment = json.loads(self._file(ref['assessment'], assessment=True))
            except (json.JSONDecodeError, UnicodeError):
                raise ValueError('consumer artifact and assessment must be JSON') from None
            keys = {'review_id', 'project', 'consumer_id', 'created_at', 'question', 'answer', 'reads', 'trace'}
            if not isinstance(record, dict) or set(record) != keys:
                raise ValueError('invalid consumer artifact fields')
            if record['review_id'] != current['review_id'] or record['project'] != project:
                raise ValueError('consumer artifact outside review/project')
            consumer = _text(record['consumer_id'], 'consumer identity', 400)
            if consumer == current['producer_session']:
                raise ValueError('consumer must differ from the review producer')
            _text(record['question'], 'consumer question', 8000)
            _text(record['answer'], 'consumer answer', 24000)
            try:
                stamp = datetime.fromisoformat(record['created_at'])
                if stamp.tzinfo is None or not current['started_at'] <= stamp.timestamp() <= time.time() + 60:
                    raise ValueError
            except (ValueError, TypeError):
                raise ValueError('consumer artifact needs a timezone-aware time after review start') from None
            if not isinstance(record['reads'], list) or not 1 <= len(record['reads']) <= 12:
                raise ValueError('consumer artifact needs 1..12 actual file-read references')
            for read in record['reads']:
                if verify_reads:self._file(read)
            self._file(record['trace'])
            if (not isinstance(assessment, dict) or assessment.get('verdict') not in ('passed', 'failed') or
                    assessment.get('review_id') != current['review_id'] or
                    assessment.get('consumer_id') != consumer or
                    assessment.get('record_sha256') != ref['record']['sha256']):
                raise ValueError('consumer assessment must bind review, consumer, record and verdict')
            reviewer = _text(assessment.get('reviewer_id'), 'reviewer identity', 400)
            if reviewer == consumer:
                raise ValueError('consumer self-assessment cannot close a review')
            _text(assessment.get('scope'), 'consumption assessment scope', 2000)
            return {'kind': kind, 'verdict': assessment['verdict'],
                    'failure_id': 'consumer_artifact:' + ref['record']['sha256'],
                    'identity': 'declared_artifact', 'semantic_quality': 'declared_review',
                    'meaning': 'file hashes, current read versions and declared review binding verified; native identity, actual reading and assessment meaning require independent review'}
        else:
            raise ValueError('invalid review evidence kind or fields')
        return {'kind': kind, 'verified': True}
