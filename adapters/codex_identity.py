"""Codex prompt identities: native messages, never active-turn/content IDs.

UserPromptSubmit can precede the native rollout message. In that case only
bounded prompt digests are queued; a later hook reads persisted native messages
in the same session/turn. Digests select observed text, never identify events.
No daemon, no transcript copy, no guessed replay or latest-message selection.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import time

MAX_TURNS = 128
MAX_PROMPTS = 64
MAX_SCAN_BYTES = 8 * 1024 * 1024
MAX_LINE_BYTES = 1024 * 1024
MAX_MESSAGES = 500
MAX_CAPTURE_PER_PASS = 16
MAINTENANCE_SECONDS = 5
MAINTENANCE_BYTES = 8 * 1024 * 1024
MAINTENANCE_GROUP_BYTES = 2 * 1024 * 1024
MAINTENANCE_GROUPS = 8
MAINTENANCE_MESSAGES = 128


class _BudgetExhausted(Exception):
    pass


class _MaintenanceBudget:
    """One shared allowance, not one fresh allowance for every pending turn."""
    def __init__(self):
        self.started = time.monotonic()
        self.deadline = self.started + MAINTENANCE_SECONDS
        self.bytes = 0
        self.messages = 0
        self.captures = 0

    def remaining_seconds(self):
        return max(0, self.deadline - time.monotonic())

    def read(self, stream, limit):
        allowance = min(limit, MAINTENANCE_BYTES - self.bytes)
        if allowance <= 0 or not self.remaining_seconds():
            raise _BudgetExhausted()
        raw = stream.readline(allowance)
        self.bytes += len(raw)
        return raw

    def capture(self):
        if self.captures >= MAX_CAPTURE_PER_PASS or not self.remaining_seconds():
            raise _BudgetExhausted()
        self.captures += 1
        return self.remaining_seconds()


def _id(value):
    return isinstance(value, str) and bool(re.fullmatch(r'[A-Za-z0-9_-]{1,200}', value))


def _hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def event_id(session, turn, message):
    return 'codex:' + session + ':' + turn + ':' + message + ':user'


def _key(project, session, turn):
    return _hash(json.dumps([project, session, turn], separators=(',', ':')))


def _legacy(cfg, session, turn, timeout=.2):
    """Read the original legacy event only; never migrate or edit the ledger."""
    path = Path(cfg.home) / 'state.sqlite3'
    if not path.is_file():
        return None
    with sqlite3.connect(path.absolute().as_uri() + '?mode=ro', uri=True, timeout=timeout) as db:
        db.row_factory = sqlite3.Row
        row = db.execute('''SELECT e.event_id,e.source_id,v.text,
            EXISTS(SELECT 1 FROM redactions r WHERE r.source_id=s.id AND r.version=1) redacted
            FROM events e JOIN sources s ON s.id=e.source_id
            JOIN versions v ON v.source_id=s.id AND v.version=1
            WHERE e.project=? AND e.event_id=? AND s.project=? AND s.session=? AND s.role='user' ''',
            (cfg.project, 'codex:'+session+':'+turn+':user', cfg.project, session)).fetchone()
    if row is None:
        return None
    return {'event_id': row['event_id'], 'source_id': row['source_id'],
            'version': 1, 'sha256': _hash(row['text']), 'redacted': bool(row['redacted'])}


def _observed_native_boundary(path, session):
    """Validate the current session and snapshot its complete-line EOF only."""
    with os.fdopen(os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)), 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('native transcript is not a regular file')
        header = stream.readline(MAX_LINE_BYTES + 1)
        if len(header) > MAX_LINE_BYTES or not header.endswith(b'\n'):
            raise ValueError('native session header unavailable')
        metadata = json.loads(header)
        if metadata.get('type') != 'session_meta' or metadata.get('payload', {}).get('id') != session:
            raise ValueError('native transcript session mismatch')
        info = os.fstat(stream.fileno())
        stream.seek(info.st_size - 1)
        if stream.read(1) != b'\n':
            raise ValueError('native observation boundary incomplete')
        return info.st_size, [info.st_dev, info.st_ino]


def _messages(path, session, turn, cursor=None, end=None, budget=None):
    """Read one bounded forward segment; return a resumable complete-line cursor.

    A pending node must never fall out of a moving tail window. The caller only
    commits this cursor after all relevant nodes in the segment were saved.
    """
    if not isinstance(path, str) or not os.path.isabs(path):
        raise ValueError('native transcript path unavailable')
    with os.fdopen(os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)), 'rb') as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('native transcript is not a regular file')
        header = budget.read(source, MAX_LINE_BYTES + 1) if budget else source.readline(MAX_LINE_BYTES + 1)
        if budget and not header.endswith(b'\n') and len(header) <= MAX_LINE_BYTES:
            raise _BudgetExhausted()
        if len(header) > MAX_LINE_BYTES:
            raise ValueError('native transcript header exceeds bound')
        metadata = json.loads(header)
        if metadata.get('type') != 'session_meta' or metadata.get('payload', {}).get('id') != session:
            raise ValueError('native transcript session mismatch')
        header_end = source.tell()
        size = info.st_size
        if end is not None:
            if type(end) is not int or not header_end <= end <= size:
                raise ValueError('native scan endpoint unavailable')
            size = end
        start = header_end if cursor is None else max(header_end, cursor)
        if start > size:
            raise ValueError('native transcript shrank below committed boundary')
        complete = start == header_end
        source.seek(start)
        if start > header_end:
            source.seek(start - 1)
            if (budget.read(source, 1) if budget else source.read(1)) != b'\n':
                raise ValueError('native transcript boundary is not a complete line')
        entries = {}; ambiguous = set()
        next_cursor = start
        scan_bound = MAINTENANCE_GROUP_BYTES if budget else MAX_SCAN_BYTES
        while source.tell() < size and source.tell() - start < scan_bound:
            offset = source.tell()
            if budget and (not budget.remaining_seconds() or budget.bytes >= MAINTENANCE_BYTES
                           or budget.messages >= MAINTENANCE_MESSAGES):
                break
            raw = (budget.read(source, min(MAX_LINE_BYTES + 1, scan_bound - (offset-start)))
                   if budget else source.readline(MAX_LINE_BYTES + 1))
            if len(raw) > MAX_LINE_BYTES:
                raise ValueError('native transcript line exceeds bound')
            if source.tell() > size:
                raise ValueError('native scan endpoint is not a complete line')
            if not raw.endswith(b'\n'):
                break
            next_cursor = source.tell()
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            item = row.get('payload', {})
            if row.get('type') != 'response_item' or not isinstance(item, dict):
                continue
            meta = item.get('internal_chat_message_metadata_passthrough', {})
            if (item.get('type') != 'message' or item.get('role') != 'user'
                    or not isinstance(meta, dict) or meta.get('turn_id') != turn
                    or meta.get('provenance') not in (None, 'user')
                    or not _id(item.get('id'))):
                continue
            content = item.get('content')
            provenance = meta.get('content_item_metadata', [])
            if isinstance(provenance, list) and any(
                    isinstance(part, dict) and isinstance(part.get('provenance'), dict)
                    and part['provenance'].get('type') != 'user' for part in provenance):
                continue
            if not isinstance(content, list) or not content or any(
                    not isinstance(part, dict) or part.get('type') not in ('input_text', 'text')
                    or not isinstance(part.get('text'), str) for part in content):
                continue
            text = '\n'.join(part['text'] for part in content)
            value = {'message': item['id'], 'text': text, 'sha256': _hash(text), 'offset': offset}
            if budget:
                budget.messages += 1
            prior = entries.get(item['id'])
            if prior and prior['sha256'] != value['sha256']:
                ambiguous.add(item['id'])
            else:
                entries.setdefault(item['id'], value)
            if len(entries) > MAX_MESSAGES:
                raise ValueError('native transcript messages exceed bound')
        at_end = next_cursor == size
        return list(entries.values()), complete and at_end, ambiguous, size, next_cursor, at_end


def _capture_native(cfg, state, session, turn, message, text, capture, messages=None, complete=False, budget=None):
    identity = event_id(session, turn, message)
    aliases = state.setdefault('codex_legacy_aliases', {})
    alias_key = _key(cfg.project, session, identity)
    legacy = _legacy(cfg, session, turn, timeout=min(.2, budget.remaining_seconds()) if budget else .2)
    if legacy and legacy['redacted']:
        return False, 'legacy redacted correspondence unknown; NOT saved', {}
    if legacy and legacy['sha256'] == _hash(text):
        alias = aliases.get(alias_key)
        expected_alias = {'project': cfg.project, 'legacy_event_id': legacy['event_id']}
        if alias == expected_alias:
            return True, None, {'source_id': legacy['source_id'], 'version': legacy['version'], 'event_id': legacy['event_id']}
        # An already established alias identifies which equal-text message was
        # the legacy event. Further distinct native IDs are new actual messages.
        established = [k for k, v in aliases.items() if v == expected_alias]
        matches = [m for m in (messages or []) if m['sha256'] == legacy['sha256']]
        if not established:
            if not complete or len(matches) != 1 or matches[0]['message'] != message:
                return False, 'legacy native correspondence ambiguous; NOT saved', {}
            aliases[alias_key] = expected_alias
            while len(aliases) > 1000:
                del aliases[next(iter(aliases))]
            return True, None, {'source_id': legacy['source_id'], 'version': legacy['version'], 'event_id': legacy['event_id']}
    ok, warning, ack = capture(cfg, session, identity, text)
    return ok, warning, dict(ack, event_id=identity) if ok else ack


def reply_target(cfg, state, session, payload):
    """Resolve the last real user by native file order, never ledger row order.

    The independent bounded cursor can be resumed after long tool output or a
    temporarily unavailable log. No text is retained, and partial/ambiguous
    scans cannot certify a final reply target.
    """
    turn = payload.get('turn_id')
    if not _id(session) or not _id(turn):
        raise ValueError('native reply scope unavailable')
    orders = state.setdefault('codex_turn_order', {})
    key = _key(cfg.project, session, turn)
    prior = orders.get(key, {})
    path = payload.get('transcript_path') or prior.get('path')
    if not path:
        return None
    if key not in orders:
        if len(orders) >= MAX_TURNS:
            del orders[next(iter(orders))]
        orders[key] = {'project': cfg.project, 'session': session, 'turn': turn, 'path': path, 'seen': {}}
    order = orders[key]
    if any(order.get(k) != v for k, v in (('project', cfg.project), ('session', session), ('turn', turn))):
        raise ValueError('native order scope mismatch')
    info = os.stat(path, follow_symlinks=False)
    identity = [info.st_dev, info.st_ino]
    if order.get('file_identity') not in (None, identity) or order.get('path') != path:
        # Replacement requires a complete new bounded scan, not an old cursor.
        order.clear(); order.update(project=cfg.project, session=session, turn=turn, path=path, seen={})
    entries, _, ambiguous, size, cursor, at_end = _messages(path, session, turn, order.get('cursor'), order.get('stop_boundary'))
    if ambiguous:
        raise ValueError('native reply message identity ambiguous')
    seen = order['seen']
    for entry in entries:
        previous = seen.get(entry['message'])
        if previous is not None and previous != entry['sha256']:
            raise ValueError('native reply identity changed')
        if previous is None:
            if len(seen) >= MAX_PROMPTS:
                raise ValueError('native order metadata bound exceeded')
            seen[entry['message']] = entry['sha256']
            order['last'] = {k: entry[k] for k in ('message', 'sha256', 'offset')}
    order.update(cursor=cursor, file_identity=identity, partial_scan=not at_end, scanned_size=size)
    if not at_end:
        raise ValueError('native reply order scan incomplete')
    last = order.get('last')
    if not last:
        raise ValueError('native reply user message unavailable')
    native_event = event_id(session, turn, last['message'])
    with sqlite3.connect((Path(cfg.home)/'state.sqlite3').absolute().as_uri()+'?mode=ro', uri=True, timeout=.2) as db:
        db.row_factory = sqlite3.Row
        row = db.execute('SELECT e.source_id,v.text FROM events e JOIN sources s ON s.id=e.source_id '
            'JOIN versions v ON v.source_id=s.id AND v.version=1 '
            "WHERE e.project=? AND e.event_id=? AND s.project=? AND s.session=? AND s.role='user'",
            (cfg.project, native_event, cfg.project, session)).fetchone()
    if row and _hash(row['text']) == last['sha256']:
        return {'source_id': row['source_id'], 'event_id': native_event, 'native_order_verified': True}
    legacy = _legacy(cfg, session, turn)
    alias = state.get('codex_legacy_aliases', {}).get(_key(cfg.project, session, native_event))
    if (legacy and not legacy['redacted'] and legacy['sha256'] == last['sha256']
            and alias == {'project': cfg.project, 'legacy_event_id': legacy['event_id']}):
        # Exact source alias is proven, but core does not grant a native-message
        # chronology override to a turn-only legacy event identity.
        return {'source_id': legacy['source_id'], 'event_id': legacy['event_id'], 'native_order_verified': False}
    raise ValueError('last native user source unavailable or alias unproven')


def observe_stop_boundary(cfg, state, session, payload):
    """Freeze the completed native-file extent for this observed Stop.

    Later turns may append arbitrarily much data; they must not keep an older
    answer's bounded reconciliation perpetually chasing a moving file end.
    """
    turn=payload.get('turn_id')
    if not _id(turn) or not _id(session):return
    key=_key(cfg.project,session,turn)
    order=state.get('codex_turn_order',{}).get(key)
    if not order:return
    path=payload.get('transcript_path') or order.get('path')
    try:
        boundary,file_identity=_observed_native_boundary(path,session)
        if order.get('file_identity') not in (None,file_identity):return
        order.setdefault('stop_boundary',boundary)
        group=state.get('codex_pending_prompts',{}).get(key)
        if group:group.setdefault('scan_end',order['stop_boundary'])
    except (OSError,ValueError,TypeError):
        pass  # Unknown endpoint does not become a fabricated completion.


def register_delivery(cfg, state, session, turn, receipt):
    """Keep only digest/boundary/receipt metadata for a deferred preparation."""
    group = state.get('codex_pending_prompts', {}).get(_key(cfg.project, session, turn))
    if not group or not group.get('last_observation'):
        return
    value = {'receipt_id': receipt.get('id', ''), 'memory_receipt_id': receipt.get('memory_id', '')}
    if not value['memory_receipt_id']:
        return
    deliveries = group.setdefault('deliveries', [])
    if group['last_observation'].get('after_byte') is None:
        group['delivery_overflow'] = True
        return
    if len(deliveries) >= MAX_PROMPTS:
        group['delivery_overflow'] = True
        return
    deliveries.append(dict(group['last_observation'], **value))


def _bind_deliveries(cfg, state, group, session, turn, capture, deadline, budget=None):
    """Only a uniquely matched persisted native node can fill an empty receipt.

    Text is read from the already saved source solely for an idempotent capture
    retry; no second copy is retained in adapter state or observations.
    """
    def check_budget():
        if budget and not budget.remaining_seconds():
            raise _BudgetExhausted()

    captured = group.get('captured', {})
    attempts = 0
    for delivery in group.get('deliveries', []):
        check_budget()
        if time.time() >= deadline or attempts >= MAX_CAPTURE_PER_PASS:
            break
        if delivery.get('bound'):
            continue
        matches = []
        for mid, info in captured.items():
            check_budget()
            if (info['sha256'] == delivery['sha256']
                    and info.get('offset', -1) >= delivery['after_byte']
                    and (delivery.get('before_byte') is None or info.get('offset', -1) < delivery['before_byte'])):
                matches.append((mid, info))
                if len(matches) > 1:
                    break  # Two matches already disprove uniqueness.
        competing = []
        for candidate in group.get('deliveries', []):
            check_budget()
            if candidate['sha256'] == delivery['sha256'] and candidate['after_byte'] == delivery['after_byte']:
                competing.append(candidate)
                if len(competing) > 1:
                    break
        if len(matches) != 1 or len(competing) != 1:
            delivery['status'] = 'ambiguous_or_pending'
            continue
        check_budget()
        mid, info = matches[0]; ack = info['ack']
        with sqlite3.connect((Path(cfg.home)/'state.sqlite3').absolute().as_uri()+'?mode=ro', uri=True,
                             timeout=min(.2, budget.remaining_seconds()) if budget else 5) as db:
            row = db.execute('SELECT v.text FROM versions v JOIN sources s ON s.id=v.source_id '
                "WHERE s.id=? AND v.version=? AND s.project=? AND s.session=? AND s.role='user'",
                (ack['source_id'], ack['version'], cfg.project, session)).fetchone()
        if not row or _hash(row[0]) != info['sha256']:
            raise ValueError('saved native source unavailable for receipt association')
        attempts += 1
        ok, warning, _ = capture(cfg, session, ack['event_id'], row[0],
                                memory_receipt_id=delivery['memory_receipt_id'])
        if not ok:
            raise ValueError(warning or 'user saved; receipt association unavailable')
        delivery.update(bound=True, status='user_bound', native_message_id=mid)
        key = _hash('\0'.join(['codex', cfg.project, session, turn]))
        binding = state.setdefault('turn_receipts', {}).setdefault(key, {'messages': {}})
        values = binding.setdefault('messages', {}).setdefault(ack['source_id'], [])
        value = {k: delivery[k] for k in ('receipt_id', 'memory_receipt_id')}
        if value not in values:
            values.append(value)


def reconcile(cfg, state, session, payload, capture, warnings, closing=False, budget=None):
    turn = payload.get('turn_id')
    if not _id(session) or not _id(turn):
        return []
    group = state.get('codex_pending_prompts', {}).get(_key(cfg.project, session, turn))
    if not isinstance(group, dict) or any(group.get(k) != v for k, v in (
            ('project', cfg.project), ('session', session), ('turn', turn))):
        return []
    now = time.time()
    if now < group.get('retry_at', 0) and not closing:
        return []
    saved_sources = []
    try:
        path = group.get('path')
        if not path and isinstance(payload.get('transcript_path'), str):
            path = payload['transcript_path']; group['path'] = path
        known = group['prompts']
        boundaries = [o['after_byte'] for o in known.values() if o.get('after_byte') is not None]
        if not boundaries or any(o.get('after_byte') is None for o in known.values()):
            raise ValueError('original native transcript boundary unavailable')
        cursor = group.get('scan_cursor', min(boundaries))
        entries, complete, ambiguous, size, next_cursor, at_end = _messages(
            path, session, turn, cursor, group.get('scan_end'), budget=budget)
        matched = 0; new_captures = 0; matched_hashes = set(); limited = False
        captured = group.setdefault('captured', {})
        for entry in entries:
            observed = known.get(entry['sha256'])
            if not observed:
                continue
            if observed['after_byte'] is None:
                raise ValueError('original native transcript boundary unavailable')
            if entry['offset'] < observed['after_byte']:
                continue
            if entry['message'] in ambiguous:
                raise ValueError('native message identity is ambiguous')
            if new_captures >= MAX_CAPTURE_PER_PASS or time.time() - now > 5 or (
                    budget and not budget.remaining_seconds()):
                limited = True; break
            prior_capture = captured.get(entry['message'])
            if prior_capture and prior_capture.get('sha256') != entry['sha256']:
                raise ValueError('captured native message content changed')
            _ack = prior_capture.get('ack') if prior_capture else None
            if not _ack:
                if len(captured) >= 64:
                    raise ValueError('native capture metadata bound exceeded')
                ok, warning, _ack = _capture_native(cfg, state, session, turn, entry['message'],
                    entry['text'], capture, entries, complete, budget=budget)
                if not ok:
                    raise ValueError(warning or 'native capture unavailable')
                captured[entry['message']] = {'ack': _ack, 'sha256': entry['sha256'], 'offset': entry['offset']}
                new_captures += 1
            matched += 1
            matched_hashes.add(entry['sha256'])
            # Every observed boundary is metadata, not an event identity. A
            # later equal-text hook may be a retry or another real message; do
            # not discard an earlier uncommitted boundary to guess which.
            observed['boundaries'] = [b for b in observed.get('boundaries', [observed['after_byte']])
                                      if b > entry['offset']]
            saved_sources.append(dict(_ack, native_message_id=entry['message'], turn_id=turn))
        if not limited:
            group['scan_cursor'] = next_cursor
        if at_end and not limited:
            _bind_deliveries(cfg, state, group, session, turn, capture, now + 5, budget=budget)
        receipt_pending = sum(not d.get('bound') for d in group.get('deliveries', [])) + int(bool(group.get('delivery_overflow')))
        settled = all(not o.get('boundaries', [o['after_byte']]) for o in known.values())
        group.update(status='native_messages_captured' if settled and not limited and at_end else 'awaiting_native_message',
                     last_scan_bytes=size, matched_messages=matched, partial_scan=not at_end,
                     limited=limited, failures=0, retry_at=0, receipt_pending=receipt_pending)
        if closing and (not settled or limited or not at_end):
            warnings.append('Codex observed user capture still pending at Stop; native reconciliation incomplete, NOT all saved')
        if closing and receipt_pending:
            warnings.append('Codex prepared receipt association unresolved; capture and preparation are not proven paired')
        if closing and not limited and settled and at_end and not receipt_pending:
            state['codex_pending_prompts'].pop(_key(cfg.project, session, turn), None)
    except _BudgetExhausted:
        # Keep uncommitted cursor/boundaries and do not turn an ordinary budget
        # yield into a capture error or exponential backoff.
        group.update(limited=True, status='awaiting_native_message')
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        failures = min(group.get('failures', 0) + 1, 6)
        group.update(status='identity_unresolved', failures=failures,
                     retry_at=now + min(30, 2 ** (failures - 1)))
        warnings.append('Codex pending capture unresolved: ' + type(exc).__name__ + '; no native identity guessed')
    return saved_sources


def capture_prompt(cfg, state, session, payload, prompt, capture, warnings):
    turn = payload.get('turn_id')
    if not _id(session) or session == 'unknown-session' or not _id(turn):
        return False, 'Codex native session/turn unavailable; NOT saved', {}
    path = payload.get('transcript_path')
    if isinstance(path, str) and os.path.isabs(path):
        orders = state.setdefault('codex_turn_order', {})
        order_key = _key(cfg.project, session, turn)
        if order_key not in orders:
            if len(orders) >= MAX_TURNS:
                del orders[next(iter(orders))]
            order = {'project': cfg.project, 'session': session, 'turn': turn, 'path': path, 'seen': {},
                     'seed_state': 'unavailable'}
            try:
                boundary, file_identity = _observed_native_boundary(path, session)
                order.update(cursor=boundary, start_boundary=boundary, file_identity=file_identity,
                             seed_state='first_prompt_observation')
            except (OSError, ValueError, TypeError):
                pass
            # This observation precedes persistence of the current prompt. A
            # retry must never replace it with a later EOF and lose that node.
            orders[order_key] = order
    reconcile(cfg, state, session, payload, capture, warnings)
    message = payload.get('message_id') or payload.get('prompt_id')
    if message is not None:
        if not _id(message):
            return False, 'Codex native message identity invalid; NOT saved', {}
        entries, complete = None, False
        if _legacy(cfg, session, turn):
            try:
                entries, complete, ambiguous, _, _, _ = _messages(payload.get('transcript_path'), session, turn)
                if message in ambiguous:
                    return False, 'Codex native message identity ambiguous; NOT saved', {}
            except (OSError, ValueError, TypeError):
                pass
        return _capture_native(cfg, state, session, turn, message, prompt, capture, entries, complete)
    groups = state.setdefault('codex_pending_prompts', {})
    if not isinstance(payload.get('transcript_path'), str) or not os.path.isabs(payload['transcript_path']):
        return False, 'Codex native message ID and persistent transcript unavailable; capture unsupported for this event (including ephemeral CLI); NOT saved', {}
    key = _key(cfg.project, session, turn)
    if key not in groups:
        if len(groups) >= MAX_TURNS:
            return False, 'Codex pending identity queue full; NOT saved', {}
        groups[key] = {'project': cfg.project, 'session': session, 'turn': turn, 'prompts': {},
                       'path': payload.get('transcript_path'), 'status': 'awaiting_native_message'}
    group = groups[key]
    group['observed_at'] = time.time()
    digest = _hash(prompt)
    try:
        boundary = os.path.getsize(payload.get('transcript_path'))
    except (OSError, TypeError):
        boundary = None
    if boundary is not None:
        for delivery in group.get('deliveries', []):
            if not delivery.get('bound') and delivery.get('before_byte') is None and boundary > delivery['after_byte']:
                delivery['before_byte'] = boundary
    group['last_observation'] = {'sha256': digest, 'after_byte': boundary}
    if digest not in group['prompts']:
        if len(group['prompts']) >= MAX_PROMPTS:
            return False, 'Codex pending prompt bound exceeded; NOT saved', {}
        # The current node can be appended only after this hook returns. The
        # boundary prevents an older identical prompt from being substituted.
        group['prompts'][digest] = {'after_byte': boundary, 'boundaries': [boundary]}
    else:
        # A repeated hook without a message identity could be a retry OR a new
        # equal-text steering message. Do not substitute the preceding node.
        try:
            boundary = os.path.getsize(payload.get('transcript_path'))
            observed = group['prompts'][digest]
            pending = observed.setdefault('boundaries', [observed['after_byte']])
            if boundary not in pending:
                if len(pending) >= MAX_PROMPTS:
                    return False, 'Codex pending observations exceed bound; NOT saved', {}
                pending.append(boundary)
        except (OSError, TypeError):
            pass
    group['status'] = 'awaiting_native_message'
    return False, 'Codex capture pending native message identity; not saved yet; knowledge writes require a saved source', {}


def reconcile_pending(home, project, session, root, python):
    """Core memory preflight, BEFORE Engine/engine.lock: metadata result only.

    Adapter-state lock is nonblocking. Each group and pass are bounded; callers
    must keep unresolved identity visible and never call this while holding an
    Engine lock (the capture child needs it). No home/database is initialized.
    """
    answer = {'status': 'no_pending', 'sources': [], 'pending_turns': 0}
    # Windows does not install the Codex hooks that produce these queues.
    if sys.platform == 'win32':
        return answer
    import fcntl
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import common
    import hook
    home = Path(home).expanduser().absolute()
    try:
        if not common.instance_enabled(str(home)):
            return dict(answer, status='disabled')
        if not _id(session) or not isinstance(project, str) or not project:
            return dict(answer, status='pending', warning='native reconciliation scope unavailable')
        state_path = home / hook.STATE_DIR_NAME / hook.STATE_FILE_NAME
        if not state_path.is_file():
            return answer
        if state_path.is_symlink() or state_path.parent.is_symlink():
            raise ValueError('unsafe pending state path')
        cfg = common.Config(str(home), project, str(python), str(root), host='codex')
        lockpath = state_path.parent / hook.LOCK_FILE_NAME
        with os.fdopen(os.open(lockpath, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600), 'a+b') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            state = hook._load_state(cfg)
            groups = state.get('codex_pending_prompts', {})
            selected = [g for g in groups.values() if isinstance(g, dict)
                        and g.get('project') == project and g.get('session') == session]
            selected.sort(key=lambda g: (g.get('status') == 'native_messages_captured', -g.get('observed_at', 0)))
            pending_replies = [g for g in state.get('codex_reply_pending', {}).values()
                               if isinstance(g,dict) and g.get('project')==project and g.get('session')==session]
            if not selected and not pending_replies:
                return answer
            warnings = []; started = time.time()
            for group in selected[:8]:
                if time.time() - started > 5:
                    break
                # A new rollout node may now exist even if the previous hook
                # hit backoff. This is an explicit scoped operation, not a loop.
                group['retry_at'] = 0
                answer['sources'].extend(reconcile(cfg, state, session,
                    {'turn_id': group['turn'], 'transcript_path': group.get('path')},
                    hook._capture_prompt_ack, warnings))
            reply_result=hook._resume_codex_replies(cfg,state,session,warnings,reconcile_users=False)
            hook._save_state(cfg, state)
            unresolved = [g for g in selected if g.get('status') != 'native_messages_captured'
                          or g.get('limited') or g.get('partial_scan') or g.get('receipt_pending')]
            unresolved_turns={g['turn'] for g in unresolved}
            unresolved_turns.update(g['turn'] for g in state.get('codex_reply_pending',{}).values()
                                    if isinstance(g,dict) and g.get('project')==project and g.get('session')==session)
            answer.update(status='pending' if unresolved_turns else 'captured', pending_turns=len(unresolved_turns), **reply_result)
            if warnings:
                answer['warning'] = '; '.join(warnings[:3])
            return answer
    except (OSError, ValueError, TypeError, KeyError, common.AdapterError) as exc:
        return dict(answer, status='pending', warning='native reconciliation unavailable: '+type(exc).__name__)


def _validate_maintenance_metadata(group):
    """Reject invalid persisted collections before scanning or capturing.

    The byte bound alone cannot bound quadratic matching work. These are the
    same collection limits enforced while recording ordinary hook metadata.
    """
    def digest(value):
        return isinstance(value, str) and bool(re.fullmatch('[0-9a-f]{64}', value))

    def boundary(value):
        return type(value) is int and value >= 0

    prompts = group.get('prompts')
    captured = group.get('captured', {})
    deliveries = group.get('deliveries', [])
    if (not isinstance(prompts, dict) or not 1 <= len(prompts) <= MAX_PROMPTS
            or not isinstance(captured, dict) or len(captured) > MAX_PROMPTS
            or not isinstance(deliveries, list) or len(deliveries) > MAX_PROMPTS):
        raise ValueError('invalid pending metadata bounds')
    for key, observed in prompts.items():
        if not digest(key) or not isinstance(observed, dict):
            raise ValueError('invalid pending prompt metadata')
        after = observed.get('after_byte')
        boundaries = observed.get('boundaries', [after])
        if ((after is not None and not boundary(after)) or not isinstance(boundaries, list)
                or len(boundaries) > MAX_PROMPTS
                or any(item is not None and not boundary(item) for item in boundaries)):
            raise ValueError('invalid pending observation boundary')
    for message, info in captured.items():
        if not _id(message) or not isinstance(info, dict):
            raise ValueError('invalid captured metadata')
        ack = info.get('ack')
        if (not digest(info.get('sha256')) or not boundary(info.get('offset')) or not isinstance(ack, dict)
                or not isinstance(ack.get('source_id'), str) or not 1 <= len(ack['source_id']) <= 200
                or type(ack.get('version')) is not int or ack['version'] < 1
                or not isinstance(ack.get('event_id'), str) or not 1 <= len(ack['event_id']) <= 1000):
            raise ValueError('invalid captured source acknowledgement')
    for delivery in deliveries:
        if (not isinstance(delivery, dict) or not digest(delivery.get('sha256'))
                or not boundary(delivery.get('after_byte'))
                or (delivery.get('before_byte') is not None and not boundary(delivery['before_byte']))
                or not isinstance(delivery.get('receipt_id'), str) or len(delivery['receipt_id']) > 200
                or not isinstance(delivery.get('memory_receipt_id'), str)
                or not 1 <= len(delivery['memory_receipt_id']) <= 200):
            raise ValueError('invalid pending delivery metadata')


def reconcile_maintenance(home, root, python):
    """Resume this home's observed user queue without a later host operation.

    One shared budget and a persisted round-robin cursor cover all groups. This
    runs outside Engine locks, uses the adapter lock nonblockingly, and never
    discovers chats or manufactures an assistant/Stop event. Existing answer
    work and native-order/receipt metadata remain for the normal reply protocol.
    """
    answer = {'status': 'no_pending', 'groups_examined': 0, 'groups_reclaimed': 0,
              'capture_attempts': 0, 'native_messages_read': 0, 'bytes_read': 0,
              'pending_groups': 0, 'partial': False, 'errors': 0}
    # Windows does not install the Codex hooks that produce these queues.
    if sys.platform == 'win32':
        return answer
    import fcntl
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import common
    import hook
    home = Path(home).expanduser().absolute()
    budget = _MaintenanceBudget()
    try:
        if (home/'maintenance/disabled.json').exists() or not common.instance_enabled(str(home)):
            return dict(answer, status='disabled')
        path = home / hook.STATE_DIR_NAME / hook.STATE_FILE_NAME
        if not path.exists():
            return answer
        if home.is_symlink() or path.is_symlink() or path.parent.is_symlink():
            raise ValueError('unsafe adapter state')
        lockpath = path.parent / hook.LOCK_FILE_NAME
        with os.fdopen(os.open(lockpath, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600), 'a+b') as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return dict(answer, status='busy', partial=True)
            # Do not use the hook's best-effort loader: maintenance must never
            # replace corrupt/oversized state with an apparently empty queue.
            with os.fdopen(os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)), 'rb') as source:
                raw = source.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise ValueError('adapter state exceeds bound')
            state = json.loads(raw)
            if not isinstance(state, dict):
                raise ValueError('invalid adapter state')
            groups = state.get('codex_pending_prompts', {})
            replies = state.get('codex_reply_pending', {})
            if (not isinstance(groups, dict) or len(groups) > MAX_TURNS
                    or not isinstance(replies, dict) or len(replies) > MAX_TURNS):
                raise ValueError('invalid pending queues')
            keys = list(groups)
            if not keys:
                return answer
            previous = state.get('codex_maintenance_cursor')
            if previous in keys:
                offset = keys.index(previous) + 1
                keys = keys[offset:] + keys[:offset]
            warnings = []
            for key in keys[:MAINTENANCE_GROUPS]:
                if not budget.remaining_seconds() or budget.bytes >= MAINTENANCE_BYTES:
                    break
                # Cursor rotation includes backoff/corrupt entries, so one bad
                # or very long session cannot permanently starve later groups.
                state['codex_maintenance_cursor'] = key
                answer['groups_examined'] += 1
                group = groups[key]
                try:
                    if not isinstance(group, dict):
                        raise ValueError('invalid pending group')
                    project, session, turn = (group.get(k) for k in ('project', 'session', 'turn'))
                    if (not isinstance(project, str) or not 1 <= len(project) <= 200 or '\0' in project
                            or not _id(session) or not _id(turn) or key != _key(project, session, turn)):
                        raise ValueError('pending scope mismatch')
                    _validate_maintenance_metadata(group)
                    if time.time() < group.get('retry_at', 0):
                        continue
                    if (home/'maintenance/disabled.json').exists() or not common.instance_enabled(str(home)):
                        answer['status'] = 'disabled'
                        break
                    cfg = common.Config(str(home), project, str(python), str(root), host='codex')
                    native_path = group.get('path')
                    if not isinstance(native_path, str) or not os.path.isabs(native_path):
                        raise ValueError('pending native path unavailable')
                    order = state.get('codex_turn_order', {}).get(key, {})
                    info = os.stat(native_path, follow_symlinks=False)
                    if (not stat.S_ISREG(info.st_mode) or (order.get('file_identity') is not None
                            and order['file_identity'] != [info.st_dev, info.st_ino])
                            or order.get('path', native_path) != native_path):
                        raise ValueError('pending native file identity changed')

                    def capture(config, sid, event, text, memory_receipt_id=None):
                        if (home/'maintenance/disabled.json').exists() or not common.instance_enabled(str(home)):
                            raise _BudgetExhausted()
                        timeout = min(common.CAPTURE_TIMEOUT_SEC, budget.capture())
                        return hook._capture_prompt_ack(config, sid, event, text,
                            memory_receipt_id=memory_receipt_id, timeout=timeout)

                    before = len(warnings)
                    reconcile(cfg, state, session, {'turn_id': turn}, capture, warnings, budget=budget)
                    answer['errors'] += int(len(warnings) > before)
                    settled = (group.get('status') == 'native_messages_captured'
                               and not group.get('limited') and not group.get('partial_scan')
                               and not group.get('receipt_pending'))
                    pending_answer = any(isinstance(value, dict) and value.get('project') == project
                        and value.get('session') == session and value.get('turn') == turn for value in replies.values())
                    if settled and not pending_answer:
                        # No answer is invented. Preserve codex_turn_order and
                        # turn_receipts so a later real Stop can still bind the
                        # last real user by native order, not capture insertion.
                        groups.pop(key)
                        answer['groups_reclaimed'] += 1
                except (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error):
                    answer['errors'] += 1
                    if isinstance(group, dict):
                        prior_failures = group.get('failures', 0)
                        failures = min(prior_failures + 1, 6) if type(prior_failures) is int and prior_failures >= 0 else 1
                        group.update(status='identity_unresolved', failures=failures,
                                     retry_at=time.time() + min(30, 2 ** (failures-1)))
            hook._save_state(common.Config(str(home), '', str(python), str(root)), state)
            answer.update(pending_groups=len(groups), capture_attempts=budget.captures,
                          native_messages_read=budget.messages, bytes_read=budget.bytes,
                          partial=bool(groups), elapsed_ms=round((time.monotonic()-budget.started)*1000, 3))
            if answer['status'] != 'disabled':
                answer['status'] = 'pending' if groups else 'captured'
            if answer['errors']:
                answer['warning'] = 'native_capture_identity_unresolved'
            return answer
    except (OSError, ValueError, TypeError, KeyError, AttributeError, common.AdapterError):
        return dict(answer, status='unavailable', partial=True, errors=1,
                    warning='native_capture_maintenance_unavailable')
