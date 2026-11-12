"""Durable resolution of InlineNotes drafts without weakening note guards.

Draft proposals are immutable.  Retries are ordinary InlineNotes writes with new,
project-bound searches; this module only adds resolution audit and terminal state.
"""
import json
import time
from .inline_notes import _digest, _text


class InlineDrafts:
    def __init__(self, notes):
        self.notes = notes
        self.engine = notes.engine
        self.db = notes.db
        with self.engine.locked(), self.engine.transaction():
            # Existing inline_note_drafts rows are deliberately neither altered nor
            # rewritten.  Absence of a state row means the historical draft is open.
            self.db.execute(
                'CREATE TABLE IF NOT EXISTS inline_draft_states('
                'draft_id TEXT NOT NULL,project TEXT NOT NULL,status TEXT NOT NULL,'
                'result TEXT,latest_event_key TEXT,updated_at REAL NOT NULL,'
                'PRIMARY KEY(draft_id,project))'
            )
            self.db.execute(
                'CREATE TABLE IF NOT EXISTS inline_draft_resolution_events('
                'event_key TEXT NOT NULL,project TEXT NOT NULL,draft_id TEXT NOT NULL,'
                'payload_hash TEXT NOT NULL,decision TEXT NOT NULL,proposal TEXT,'
                'result TEXT NOT NULL,retry_event_key TEXT,search_id TEXT,'
                'created_at REAL NOT NULL,PRIMARY KEY(event_key,project))'
            )
            self.db.execute(
                'CREATE INDEX IF NOT EXISTS inline_draft_resolution_by_draft '
                'ON inline_draft_resolution_events(project,draft_id,created_at)'
            )

    def _mode(self, project):
        from .memory_runtime import allowed
        if allowed(self.engine, project):
            raise ValueError('inline drafts require memory mode for this project')

    def _draft(self, project, draft_id):
        row = self.db.execute(
            'SELECT * FROM inline_note_drafts WHERE project=? AND draft_id=?',
            (project, draft_id),
        ).fetchone()
        if not row:
            raise ValueError('draft not found in project')
        return row

    def list(self, project, limit=20, draft_id=None, offset=0, include_closed=False):
        _text(project, 'project', 200)
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError('limit must be between 1 and 50')
        if draft_id is not None:
            _text(draft_id, 'draft_id', 200)
        if type(offset) is not int or not 0 <= offset <= 1000000:
            raise ValueError('offset must be between 0 and 1000000')
        if type(include_closed) is not bool:
            raise ValueError('include_closed must be boolean')
        self._mode(project)
        with self.engine.locked():
            sql = (
                'SELECT d.*,s.status AS resolution_status,s.result AS resolution_result,'
                's.latest_event_key FROM inline_note_drafts d '
                'LEFT JOIN inline_draft_states s ON s.draft_id=d.draft_id '
                'AND s.project=d.project WHERE d.project=?'
            )
            args = [project]
            if draft_id is not None:
                sql += ' AND d.draft_id=?'
                args.append(draft_id)
            elif not include_closed:
                sql += (" AND (s.status IS NULL OR s.status IN ('open','resolving') "
                        "OR (s.status='resolved' AND json_extract(s.result,'$.publication')='publication_pending'))")
            sql += ' ORDER BY d.created_at DESC,d.draft_id LIMIT ? OFFSET ?'
            args.extend([limit,offset])
            rows = self.db.execute(sql, args).fetchall()
            records = []
            for row in rows:
                status = row['resolution_status'] or 'open'
                result = json.loads(row['resolution_result']) if row['resolution_result'] else None
                proposal = json.loads(row['payload'])
                if status == 'resolving':
                    next_action = 'resume'
                elif status == 'open':
                    next_action = 'retry_or_dismiss'
                elif result and result.get('publication') == 'publication_pending':
                    # Repeating the identical resolution event is the safe publication
                    # retry; it cannot create a second note or resolution.
                    next_action = 'resume'
                else:
                    next_action = None
                record = {
                    'draft_id': row['draft_id'],
                    'project': project,
                    'kind': 'update' if proposal.get('id') else 'create',
                    'proposal': proposal,
                    'reason': row['reason'],
                    'status': status,
                    'created_at': row['created_at'],
                    'next_action': next_action,
                }
                if row['latest_event_key']:
                    record['latest_attempt'] = {
                        'event_key': row['latest_event_key'],
                        'result': result,
                    }
                    if next_action == 'resume':
                        attempt = self.db.execute(
                            'SELECT decision,proposal FROM inline_draft_resolution_events '
                            'WHERE project=? AND event_key=?',
                            (project,row['latest_event_key']),
                        ).fetchone()
                        retry_payload = {'draft_id':row['draft_id'],'decision':attempt['decision']}
                        if attempt['proposal']:
                            retry_payload['proposal'] = json.loads(attempt['proposal'])
                        record['latest_attempt']['payload'] = retry_payload
                records.append(record)
            return records

    def _resume(self,project,event_key,draft_id,payload_hash):
        """A fresh host tool call can resume ONLY the persisted original proposal.

        The outer event is an audit alias, not a second attempt or authorization.
        It never replaces the draft's latest original attempt identity.
        """
        with self.engine.locked():
            self._draft(project,draft_id)
            alias = self.db.execute('SELECT * FROM inline_draft_resolution_events '
                'WHERE project=? AND event_key=?',(project,event_key)).fetchone()
            if alias:
                if alias['payload_hash'] != payload_hash or alias['decision'] != 'resume':
                    raise ValueError('resolution event_key reused with different content')
                original_key = alias['retry_event_key']
            else:
                state = self.db.execute('SELECT * FROM inline_draft_states '
                    'WHERE project=? AND draft_id=?',(project,draft_id)).fetchone()
                if not state or state['status'] not in ('resolving','resolved'):
                    raise ValueError('no committed or pending resolution to resume')
                original_key = state['latest_event_key']
            original = self.db.execute('SELECT * FROM inline_draft_resolution_events '
                'WHERE project=? AND event_key=?',(project,original_key)).fetchone()
            if not original or original['decision'] != 'retry' or original['draft_id'] != draft_id:
                raise ValueError('invalid original resolution')
            if not alias:
                with self.engine.transaction():
                    self.db.execute('INSERT INTO inline_draft_resolution_events VALUES(?,?,?,?,?,?,?,?,?,?)',
                        (event_key,project,draft_id,payload_hash,'resume',None,
                         json.dumps({'status':'resolving'}),original_key,None,time.time()))
            result = self.resolve(project,original_key,{'draft_id':draft_id,
                'decision':'retry','proposal':json.loads(original['proposal'])})
            result = dict(result,resumed_event_key=original_key,replayed=bool(alias))
            with self.engine.transaction():
                self.db.execute('UPDATE inline_draft_resolution_events SET result=? '
                    'WHERE project=? AND event_key=?',
                    (json.dumps(result,ensure_ascii=False),project,event_key))
            return result

    def _retry_event_key(self, project, event_key):
        # Keep this namespace separate from caller-controlled InlineNotes event keys.
        return 'inline-draft-resolution:' + _digest(['resolve', project, event_key])[:48]

    def _check_retry(self, original, proposal):
        if not isinstance(proposal, dict):
            raise ValueError('retry proposal must be an object')
        try:
            encoded = json.dumps(proposal, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            raise ValueError('retry proposal must be JSON serializable')
        if len(encoded) > 50000:
            raise ValueError('retry proposal is too large')
        updating = bool(original.get('id'))
        if updating:
            if proposal.get('id') != original['id']:
                raise ValueError('retry cannot change the original update target')
            if 'expected_version' not in proposal:
                raise ValueError('retry must retain update validation')
        elif 'id' in proposal or 'expected_version' in proposal:
            raise ValueError('retry cannot change a create into an update')
        if proposal.get('key') != original['key']:
            raise ValueError('retry cannot change the original key')
        if proposal.get('scope') != original['scope']:
            raise ValueError('retry cannot move scope')
        _text(proposal.get('search_id'), 'search_id', 200)
        return 'update' if updating else 'create'

    def _fresh_search(self, project, draft, proposal):
        search_id = proposal['search_id']
        original = self.db.execute(
            'SELECT search_id FROM inline_note_events WHERE project=? AND event_key=?',
            (project, draft['event_key']),
        ).fetchone()
        used = {original['search_id']} if original else set()
        used.update(
            row[0] for row in self.db.execute(
                'SELECT search_id FROM inline_draft_resolution_events '
                'WHERE project=? AND draft_id=? AND search_id IS NOT NULL',
                (project, draft['draft_id']),
            ).fetchall()
        )
        if search_id in used:
            raise ValueError('retry requires a fresh search_id')

    def _run_retry(self, kind, project, retry_event_key, proposal):
        # This is the sole write path.  In particular, do not reproduce or relax any
        # of InlineNotes' confirmation, protection, evidence, CAS, or scope checks.
        if kind == 'update':
            return self.notes.update(project, retry_event_key, proposal)
        return self.notes.remember(project, retry_event_key, proposal)

    def _retry_result(self, draft_id, result):
        applied = result.get('status') == 'applied'
        value = dict(result, draft_id=draft_id, decision='retry', applied=applied)
        if applied:
            value['resolution'] = 'resolved'
            value['user_action_required'] = False
        else:
            value['resolution'] = 'open'
            value['resolution_action_required'] = True
            value['repeat_confirmation_required'] = False
            value['notice'] = (
                'Retry was not applied; the original draft remains open. '
                'Inspect latest_attempt before choosing another safe retry.'
            )
            if result.get('status') == 'draft' and result.get('draft_id'):
                value['attempt_draft_id'] = result['draft_id']
        return value

    def _reserve(self, project, event_key, draft_id, payload_hash, decision,
              proposal, result, retry_event_key, search_id, status):
        # Persist intent BEFORE applying a note. A process crash cannot let another
        # event dismiss/rewrite a draft whose previous attempt already committed.
        with self.engine.transaction():
            self.db.execute(
                'INSERT INTO inline_draft_resolution_events VALUES(?,?,?,?,?,?,?,?,?,?)',
                (event_key, project, draft_id, payload_hash, decision,
                 None if proposal is None else json.dumps(proposal, ensure_ascii=False),
                 json.dumps(result, ensure_ascii=False), retry_event_key, search_id,
                 time.time()),
            )
            self.db.execute(
                'INSERT OR REPLACE INTO inline_draft_states VALUES(?,?,?,?,?,?)',
                (draft_id, project, status, json.dumps(result, ensure_ascii=False),
                 event_key, time.time()),
            )

    def _save(self, project, event_key, draft_id, payload_hash, decision,
              proposal, result, retry_event_key, search_id, status):
        self._update_result(project,event_key,draft_id,status,result)

    def _update_result(self, project, event_key, draft_id, status, result):
        with self.engine.transaction():
            self.db.execute(
                'UPDATE inline_draft_resolution_events SET result=? '
                'WHERE project=? AND event_key=?',
                (json.dumps(result, ensure_ascii=False), project, event_key),
            )
            self.db.execute(
                'UPDATE inline_draft_states SET result=?,status=?,updated_at=? '
                'WHERE project=? AND draft_id=?',
                (json.dumps(result, ensure_ascii=False), status, time.time(),
                 project, draft_id),
            )

    def resolve(self, project, event_key, payload):
        _text(project, 'project', 200)
        _text(event_key, 'event_key', 200)
        if not isinstance(payload, dict) or set(payload) - {'draft_id', 'decision', 'proposal'}:
            raise ValueError('invalid resolution payload fields')
        draft_id = _text(payload.get('draft_id'), 'draft_id', 200)
        decision = payload.get('decision')
        if decision not in ('dismiss', 'retry', 'resume'):
            raise ValueError('decision must be dismiss, retry or resume')
        proposal = payload.get('proposal')
        if decision in ('dismiss','resume'):
            if 'proposal' in payload:
                raise ValueError(decision+' cannot include a proposal')
            proposal = None
        payload_hash = _digest([draft_id, decision, proposal])
        self._mode(project)
        if decision == 'resume':
            return self._resume(project,event_key,draft_id,payload_hash)

        # The outer engine lock serializes all resolutions for this installation.
        # InlineNotes re-enters it safely.  Thus two different events cannot both pass
        # the open-state check and apply the same draft.
        with self.engine.locked():
            draft = self._draft(project, draft_id)
            original = json.loads(draft['payload'])
            prior = self.db.execute(
                'SELECT * FROM inline_draft_resolution_events '
                'WHERE project=? AND event_key=?', (project, event_key)
            ).fetchone()
            resuming = False
            if prior:
                if (prior['draft_id'] != draft_id or
                        prior['payload_hash'] != payload_hash):
                    raise ValueError('resolution event_key reused with different content')
                result = json.loads(prior['result'])
                if result.get('status') == 'resolving':
                    resuming = True
                elif result.get('status') == 'error':
                    raise ValueError('previous resolution failed; use a new event and fresh search')
                if (result.get('status') == 'applied' and
                        result.get('publication') == 'publication_pending'):
                    kind = 'update' if original.get('id') else 'create'
                    retried = self._run_retry(
                        kind, project, prior['retry_event_key'],
                        json.loads(prior['proposal']),
                    )
                    if retried.get('status') != 'applied':
                        raise RuntimeError('committed resolution replay changed its result')
                    result = self._retry_result(draft_id, retried)
                    self._update_result(project, event_key, draft_id, 'resolved', result)
                if not resuming:
                    return dict(result, replayed=True)

            state = self.db.execute(
                'SELECT status FROM inline_draft_states WHERE project=? AND draft_id=?',
                (project, draft_id),
            ).fetchone()
            if state and state['status'] != 'open' and not resuming:
                raise ValueError('draft has a terminal resolution or pending attempt; inspect drafts')

            if decision == 'dismiss':
                result = {
                    'status': 'dismissed', 'draft_id': draft_id,
                    'decision': 'dismiss', 'resolution': 'dismissed',
                    'applied': False, 'replayed': False,
                    'user_action_required': False,
                }
                self._reserve(project, event_key, draft_id, payload_hash, decision,
                           None, result, None, None, 'dismissed')
                return result

            kind = self._check_retry(original, proposal)
            retry_event_key = self._retry_event_key(project, event_key)
            if not resuming:
                self._fresh_search(project, draft, proposal)
                self._reserve(project,event_key,draft_id,payload_hash,decision,proposal,
                    {'status':'resolving','resolution':'resolving','applied':False},
                    retry_event_key,proposal['search_id'],'resolving')
            try:
                outcome = self._run_retry(kind, project, retry_event_key, proposal)
            except (ValueError,KeyError) as exc:
                committed = self.db.execute('SELECT 1 FROM inline_note_events '
                    'WHERE project=? AND event_key=?',(project,retry_event_key)).fetchone()
                if not committed:
                    # Invalid evidence/search is a failed attempt, not an endless lock.
                    self._update_result(project,event_key,draft_id,'open',
                        {'status':'error','resolution':'open','applied':False,
                         'error':type(exc).__name__,'next_action':'new_event_and_fresh_search'})
                raise
            result = self._retry_result(draft_id, outcome)
            terminal = outcome.get('status') == 'applied'
            self._save(project, event_key, draft_id, payload_hash, decision,
                       proposal, result, retry_event_key, proposal['search_id'],
                       'resolved' if terminal else 'open')
            return result
