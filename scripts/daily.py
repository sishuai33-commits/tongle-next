"""Local agent fallback and everyday status entry for the active installation."""
import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'adapters'))
from production import resolve, environment


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument('--cwd', default=str(Path.cwd()))
    p.add_argument('command', choices=['context', 'capture', 'status', 'stop', 'memory'])
    p.add_argument('--text', default='')
    p.add_argument('--text-stdin', action='store_true')
    p.add_argument('--read-only', action='store_true')
    p.add_argument('--session')
    p.add_argument('--current-source-id')
    p.add_argument('--memory-receipt-id')
    p.add_argument('--event-id')
    p.add_argument('--role', default='user', choices=['user', 'assistant'])
    p.add_argument('--action', choices=['help', 'search', 'get', 'source', 'remember', 'update', 'amend', 'drafts', 'resolve', 'finish', 'review'])
    a = p.parse_args(argv)
    if a.current_source_id and a.command != 'context':
        p.error('--current-source-id is only valid for context')
    if a.memory_receipt_id and a.command != 'capture':
        p.error('--memory-receipt-id is only valid for capture')
    if a.text_stdin and (a.command != 'context' or a.text):
        p.error('--text-stdin is only valid for context without --text')
    data = None
    if a.text_stdin:
        data = sys.stdin.buffer.read(800001)
        if len(data) > 800000:
            p.error('context text exceeds 800000 byte transport bound')
    if a.command == 'memory':
        if not a.session or not a.session.strip() or not a.action:
            p.error('memory requires --session and --action')
        raw = sys.stdin.read(64001)
        if len(raw) > 64000:
            p.error('memory payload exceeds 64000 characters')
        try:
            payload = json.loads(raw)
        except ValueError:
            p.error('memory payload must be JSON')
        if not isinstance(payload, dict):
            p.error('memory payload must be an object')
        if any(k in payload for k in ('project', 'home', 'session', 'event_key')):
            p.error('host identity cannot be supplied in memory payload')
        if a.action in ('remember', 'update', 'amend', 'resolve', 'finish') or (a.action == 'review' and payload.get('op') != 'status'):
            if not a.event_id or not a.event_id.strip():
                p.error('memory writes require stable --event-id; reuse it when retrying the same operation')
            # Structured encoding avoids colon-boundary aliases across sessions.
            payload['event_key'] = 'cli:' + json.dumps([a.session, a.event_id], separators=(',', ':'))
        elif a.event_id:
            p.error('--event-id is only used for memory writes')
        data = json.dumps(payload, ensure_ascii=False)
    if a.read_only and not (a.command == 'status' or (a.command == 'memory' and a.action == 'review' and payload.get('op') == 'status')):
        p.error('--read-only only supports status and memory review status')
    cfg = resolve(a.cwd)
    if a.command == 'status':
        command = [cfg['python'], '-m', 'tongle.console_report']
    elif a.command == 'stop':
        command = [cfg['python'], str(Path(cfg['root']) / 'scripts/maintain.py'), '--home', cfg['home'], 'stop']
    else:
        command = [cfg['python'], '-m', 'tongle', '--home', cfg['home'], a.command]
        if a.command == 'context':
            command += ['--project', cfg['project'], '--session', a.session or 'manual']
            command += ['--text-stdin'] if a.text_stdin else ['--text', a.text]
            if a.current_source_id:
                command += ['--current-source-id', a.current_source_id]
        if a.command == 'capture':
            if not a.event_id:
                p.error('capture requires --event-id for durable idempotence')
            command += ['--project', cfg['project'], '--session', a.session or 'manual', '--event-id', a.event_id, '--role', a.role]
            if a.memory_receipt_id:
                command += ['--memory-receipt-id', a.memory_receipt_id]
        if a.command == 'memory':
            command += ['--project', cfg['project'], '--session', a.session, '--action', a.action]
            if a.action == 'review' and payload.get('op') == 'status':
                command += ['--read-only']
    options = dict(cwd=cfg['root'], env=environment(cfg))
    if data is not None:
        options.update(input=data, text=isinstance(data, str))
    return subprocess.run(command, **options).returncode


if __name__ == '__main__':
    sys.exit(main())
