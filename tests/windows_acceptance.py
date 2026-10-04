"""Independent binary acceptance using only synthetic data and loopback HTTP.
This verifies engineering behavior, not model intelligence or Windows 11 UI.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--binary', required=True)
    parser.add_argument('--report', required=True)
    args = parser.parse_args()
    binary = Path(args.binary).resolve()
    report_path = Path(args.report).resolve()
    report = {'passed': False, 'platform': platform.platform(),
              'material': 'synthetic', 'real_model': False,
              'windows_11_desktop_verified': False, 'checks': []}
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *unused): pass
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            payload = json.loads(request['messages'][-1]['content'])
            if 'proposal' in payload:
                answer = {'approved': True, 'findings': []}
            elif 'sources' in payload:
                relevant = [s for s in payload['sources'] if '青溪合成' in s.get('text', '')]
                if not relevant:
                    answer = {'operations': []}
                else:
                    source = relevant[-1]
                    old = payload['existing']['cards']
                    old = [c for c in old if c.get('title') == '青溪合成交付范围']
                    evidence = [{'source_id': source['source_id'], 'version': source['version'], 'quote': source['text']}]
                    operation = {'body': source['text'], 'evidence': evidence, 'reason': '合成验收中的明确资料或更正'}
                    if old:
                        operation.update(op='update', id=old[0]['id'], expected_version=old[0]['version'])
                    else:
                        operation.update(op='create', key='青溪交付范围', kind='memory', topic='合成项目', title='青溪合成交付范围')
                    answer = {'operations': [operation]}
            else:
                answer = {'operations': []}
            seen.append({'task': str(payload.get('task', ''))[:60], 'answer_keys': list(answer)})
            body = json.dumps({'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': json.dumps(answer, ensure_ascii=False)}}], 'usage': {'prompt_tokens': 100, 'completion_tokens': 50}}, ensure_ascii=False).encode('utf8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with tempfile.TemporaryDirectory(prefix='tongle-windows-') as temp:
        base = Path(temp).resolve() / '中文 空格 #验收'
        base.mkdir()
        bundle = base / '移动后的 工具'
        shutil.copytree(binary.parent, bundle, symlinks=True)
        exe = bundle / binary.name
        home, vault = base / '运行库', base / '知识库'
        env = dict(os.environ)
        for key in list(env):
            if key.startswith('TONGLE_') or key in ('PYTHONPATH', 'PYTHONHOME'):
                env.pop(key, None)
        env.update(TONGLE_SYNTHETIC_KEY='synthetic-not-a-real-api-key', PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
        env['PATH'] = str(Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32') if os.name == 'nt' else '/usr/bin:/bin'

        def call(argv, *, input=None, parse=True, timeout=90):
            proc = subprocess.run([str(exe)] + [str(x) for x in argv], input=input, capture_output=True, encoding='utf8', env=env, cwd=base, timeout=timeout)
            assert proc.returncode == 0, (argv, proc.returncode, proc.stdout[-2000:], proc.stderr[-1000:])
            assert 'synthetic-not-a-real-api-key' not in proc.stdout + proc.stderr
            return json.loads(proc.stdout) if parse else proc.stdout

        def product(command, *more, target=home):
            return call([command, '--home', target, *more])

        def check(label):
            report['checks'].append(label)
            print('PASS:', label, flush=True)

        def rows():
            with sqlite3.connect(home / 'state.sqlite3') as db:
                data = db.execute('select id,version,body from knowledge_cards').fetchall()
            db.close()
            return data

        try:
            manifest = json.loads((bundle / 'manifest.json').read_text(encoding='utf8'))
            for rel, digest in manifest['files'].items():
                assert hashlib.sha256((bundle / rel).read_bytes()).hexdigest() == digest, rel
            check('relocated bundle manifest verified')
            output = call([], parse=False)
            assert '请双击' in output and not output.lstrip().startswith('{')
            check('bare launch provides friendly guidance without hanging on noninteractive input')
            product('setup', '--vault', vault, '--project', '青溪合成', '--provider', 'deepseek', '--endpoint', 'https://example.invalid/chat/completions', '--model', 'synthetic', '--key-env', 'TONGLE_SYNTHETIC_KEY', '--write')
            check('fresh setup with Chinese, spaces and hash in paths; no Python or Pi on PATH')
            source = base / '合成资料.md'
            source.write_text('青溪合成本轮交付三张流程图。普通接口暂不纳入，支付回执是唯一例外。', encoding='utf8')
            product('add', '--path', source)
            with sqlite3.connect(home / 'state.sqlite3') as db:
                before = db.execute('select count(*) from sources').fetchone()[0]
            db.close()
            product('add', '--path', source)
            with sqlite3.connect(home / 'state.sqlite3') as db:
                assert db.execute('select count(*) from sources').fetchone()[0] == before
            db.close()
            check('duplicate file registration is idempotent')
            config_path = home / 'config.json'
            config = json.loads(config_path.read_text(encoding='utf8'))
            config.update(model_command=[str(exe), str(bundle / '_internal/scripts/http_model.py'), '--provider', 'deepseek', '--endpoint', 'http://127.0.0.1:%d/chat/completions' % server.server_port, '--model', 'synthetic', '--key-env', 'TONGLE_SYNTHETIC_KEY', '--allow-local-http'], knowledge_quiet_seconds=0, knowledge_daily_call_budget=30, knowledge_daily_token_budget=300000, model_timeout_seconds=20)
            config_path.write_text(json.dumps(config), encoding='utf8')
            product('once')
            cards = rows()
            assert len(cards) == 1 and '三张' in cards[0][2], cards
            first_id = cards[0][0]
            context = product('context', '--text', '青溪合成 交付', '--session', 'windows-consumer')
            assert context['results'] and '三张' in json.dumps(context, ensure_ascii=False)
            check('frozen HTTP runner compiles, verifies and publishes a card; fresh task retrieves it')
            call(['-m', 'tongle', '--home', home, 'capture', '--project', '青溪合成', '--session', 'windows-consumer', '--role', 'assistant', '--event-id', 'synthetic:answer', '--receipt-id', context['receipt_id']], input='青溪本轮交付三张流程图，普通接口不纳入，支付回执例外保留。')
            with sqlite3.connect(home / 'state.sqlite3') as db:
                row = db.execute('select binding,output_source from knowledge_consumption where id=?', (context['receipt_id'],)).fetchone()
            db.close()
            assert row[0] == 'receipt' and row[1]
            check('actual consumer output bound to exact receipt')
            source.write_text('青溪合成本轮更正为四张流程图。普通接口仍不纳入，支付回执例外保留。', encoding='utf8')
            product('once')
            cards = rows()
            assert len(cards) == 1 and cards[0][0] == first_id and cards[0][1] >= 2 and '四张' in cards[0][2], cards
            newer = product('context', '--text', '青溪合成 交付', '--session', 'windows-consumer-2')
            assert '四张' in json.dumps(newer, ensure_ascii=False)
            check('source revision updates same card and next task gets corrected version')
            backup = base / '备份.zip'
            product('backup', '--to', backup)
            product('restore', '--from', backup, '--vault', base / '恢复知识库', target=base / '恢复运行库')
            restored = product('context', '--project', '青溪合成', '--text', '青溪合成 交付', target=base / '恢复运行库')
            assert '四张' in json.dumps(restored, ensure_ascii=False)
            check('backup and independent restore preserve usable knowledge')
            product('start')
            status = product('status')
            assert status['maintenance']['running'], status
            product('start')
            product('stop')
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                if not product('status')['maintenance']['running']: break
                time.sleep(.2)
            else: raise AssertionError('background maintenance did not stop')
            check('background survives parent exit; repeated start safe; stop releases singleton lock')
            serialized = '\n'.join(p.read_text(encoding='utf8', errors='replace') for p in home.rglob('*.json'))
            assert 'synthetic-not-a-real-api-key' not in serialized
            check('API key absent from persisted configuration and status')
            report.update(passed=True, http_calls=len(seen), card_version=cards[0][1], executable_sha256=hashlib.sha256(exe.read_bytes()).hexdigest())
        except Exception as exc:
            report['error'] = type(exc).__name__ + ': ' + str(exc)
            raise
        finally:
            try: product('stop')
            except Exception: pass
            server.shutdown(); server.server_close(); thread.join(timeout=5)
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
