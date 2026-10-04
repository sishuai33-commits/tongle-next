"""Focused tests for scripts/http_model.py against synthetic local fixtures.

Every server binds 127.0.0.1 and is torn down by a contextmanager; no real
network or private configuration is touched. Credentials in the environment are
only ever synthetic sentinels used to prove they do not leak.
"""
import importlib.util
import json
import os
import ssl
import subprocess
import sys
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock


def _load_http_model():
    spec = importlib.util.spec_from_file_location(
        'http_model_under_test',
        Path(__file__).resolve().parent.parent / 'scripts' / 'http_model.py',
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / 'scripts' / 'http_model.py'

GOOD_CONTENT = json.dumps({'records': [{'title': 'x', 'text': 'y'}]})
SENTINEL_KEY = 'sk-synthetic-sentinel-value'
SENTINEL_PROMPT = 'PROMPT-SENTINEL-zzz'


class _Recorder:
    """Collects requests and the responding behavior of a fixture server."""

    def __init__(self, response_fn):
        self.response_fn = response_fn
        self.requests = []
        self.headers = []
        self.lock = threading.Lock()

    def record(self, method, path, headers, body):
        with self.lock:
            self.requests.append((method, path, body))
            self.headers.append(dict(headers))


@contextmanager
def _serve(response_fn):
    """Start a local ThreadingHTTPServer on an ephemeral port; clean it up."""
    recorder = _Recorder(response_fn)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            length = int(self.headers.get('Content-Length') or 0)
            body = self.rfile.read(length)
            recorder.record('POST', self.path, self.headers, body)
            try:
                status, payload, extra = response_fn(recorder, self)
            except Exception:
                status, payload, extra = 500, b'{}', {}
            self.send_response(status)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            if payload is not None:
                self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            if payload is not None:
                try:
                    self.wfile.write(payload)
                except BrokenPipeError:
                    pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, recorder
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _endpoint(server, path='/v1/chat/completions'):
    return 'http://127.0.0.1:%d%s' % (server.server_address[1], path)


def _json_response(content, finish='stop', usage=None, message_extra=None):
    message = {'role': 'assistant', 'content': content}
    if message_extra:
        message.update(message_extra)
    doc = {'choices': [{'message': message, 'finish_reason': finish}]}
    if usage is not None:
        doc['usage'] = usage
    return json.dumps(doc).encode('utf-8')


def _run(endpoint, provider='deepseek', model='m', prompt=GOOD_CONTENT,
         key_env='HTTP_MODEL_TEST_KEY', key=SENTINEL_KEY, extra=None,
         timeout='30', allow_local=True, env_overrides=None):
    args = [sys.executable, str(SCRIPT), '--endpoint', endpoint,
            '--provider', provider, '--model', model, '--key-env', key_env,
            '--timeout', timeout]
    if allow_local:
        args.append('--allow-local-http')
    if extra:
        args.extend(extra)
    env = dict(os.environ)
    env.pop('HTTP_PROXY', None)
    env.pop('HTTPS_PROXY', None)
    env.pop('ALL_PROXY', None)
    env.pop('TONGLE_MODEL_MAX_OUTPUT', None)
    if env_overrides:
        env.update(env_overrides)
    if key is not None:
        env[key_env] = key
    else:
        env.pop(key_env, None)
    try:
        proc = subprocess.run(args, input=prompt, capture_output=True, text=True,
                              timeout=60, env=env)
    except subprocess.TimeoutExpired:
        raise
    return proc


def _stderr_line(proc):
    for line in proc.stderr.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except ValueError:
            continue
    return None


def _stdout_json(proc):
    return json.loads(proc.stdout.strip())


def _ok(recorder, usage=None, content=GOOD_CONTENT, finish='stop',
        message_extra=None):
    payload = _json_response(content, finish=finish, usage=usage,
                             message_extra=message_extra)

    def responder(rec, handler):
        return 200, payload, {'Content-Type': 'application/json'}
    return responder


class HttpModelTests(unittest.TestCase):

    def test_deepseek_payload_shape(self):
        with _serve(_ok(None)) as (server, rec):
            proc = _run(_endpoint(server), provider='deepseek',
                        extra=['--max-output', '64'])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        method, path, body = rec.requests[0]
        self.assertEqual(method, 'POST')
        self.assertEqual(path, '/v1/chat/completions')
        sent = json.loads(body)
        self.assertEqual(sent['model'], 'm')
        self.assertEqual(sent['stream'], False)
        self.assertEqual(sent['max_tokens'], 64)
        self.assertEqual(sent['thinking'], {'type': 'disabled'})
        self.assertNotIn('max_completion_tokens', sent)
        self.assertEqual(sent['messages'][0]['role'], 'system')
        self.assertEqual(sent['messages'][1]['role'], 'user')
        self.assertEqual(sent['messages'][1]['content'], GOOD_CONTENT)

    def test_openai_payload_shape(self):
        with _serve(_ok(None)) as (server, rec):
            proc = _run(_endpoint(server), provider='openai',
                        extra=['--max-output', '128'])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        sent = json.loads(rec.requests[0][2])
        self.assertEqual(sent['max_completion_tokens'], 128)
        self.assertNotIn('max_tokens', sent)
        self.assertNotIn('thinking', sent)

    def test_bad_proxy_env_ignored(self):
        with _serve(_ok(None)) as (server, rec):
            proc = _run(_endpoint(server), env_overrides={
                'HTTP_PROXY': 'http://127.0.0.1:1/',
                'HTTPS_PROXY': 'http://127.0.0.1:1/',
                'ALL_PROXY': 'http://127.0.0.1:1/',
            })
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(rec.requests), 1)

    def test_authorization_reaches_fixture(self):
        with _serve(_ok(None)) as (server, rec):
            proc = _run(_endpoint(server))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(rec.headers[0].get('Authorization'), 'Bearer ' + SENTINEL_KEY)
        self.assertIn('application/json', rec.headers[0].get('Content-Type', ''))

    def test_success_usage_marker(self):
        usage = {'prompt_tokens': 11, 'completion_tokens': 5, 'total_tokens': 16}
        with _serve(_ok(None, usage=usage)) as (server, rec):
            proc = _run(_endpoint(server))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(_stdout_json(proc), {'records': [{'title': 'x', 'text': 'y'}]})
        line = _stderr_line(proc)
        self.assertEqual(line['runner'], 'http')
        self.assertEqual(line['usage']['input'], 11)
        self.assertTrue(line['usage']['inputIncludesCache'])
        self.assertEqual(line['usage']['output'], 5)
        self.assertEqual(line['usage']['totalTokens'], 16)
        self.assertNotIn('cacheRead', line['usage'])
        self.assertNotIn('cacheWrite', line['usage'])

    def test_partial_usage_stays_unknown(self):
        usage = {'prompt_tokens': None, 'completion_tokens': -3}
        with _serve(_ok(None, usage=usage)) as (server, rec):
            proc = _run(_endpoint(server))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        line = _stderr_line(proc)
        self.assertEqual(line['usage'], {})

    def test_length_keeps_usage(self):
        usage = {'prompt_tokens': 7, 'completion_tokens': 9}
        with _serve(_ok(None, usage=usage, content='{"a":1}', finish='length')) as (server, rec):
            proc = _run(_endpoint(server))
        self.assertNotEqual(proc.returncode, 0)
        line = _stderr_line(proc)
        self.assertEqual(line['category'], 'output_truncated')
        self.assertEqual(line['code'], 'truncated')
        self.assertEqual(line['usage']['input'], 7)
        self.assertEqual(line['usage']['output'], 9)

    def test_invalid_content_json(self):
        with _serve(_ok(None, content='not json')) as (server, rec):
            proc = _run(_endpoint(server))
        self.assertNotEqual(proc.returncode, 0)
        line = _stderr_line(proc)
        self.assertEqual(line['category'], 'invalid_json')
        self.assertEqual(line['code'], 'invalid_json')

    def test_array_content_json(self):
        with _serve(_ok(None, content='[1,2]')) as (server, rec):
            proc = _run(_endpoint(server))
        self.assertNotEqual(proc.returncode, 0)
        line = _stderr_line(proc)
        self.assertEqual(line['code'], 'invalid_json_type')

    def test_empty_model_content(self):
        with _serve(_ok(None, content='   ')) as (server, rec):
            proc = _run(_endpoint(server))
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(_stderr_line(proc)['code'], 'empty_output')

    def test_missing_choices_envelope(self):
        payload = json.dumps({'usage': {'prompt_tokens': 3}}).encode()

        def responder(rec, handler):
            return 200, payload, {'Content-Type': 'application/json'}
        with _serve(responder) as (server, rec):
            proc = _run(_endpoint(server))
        self.assertNotEqual(proc.returncode, 0)
        line = _stderr_line(proc)
        self.assertEqual(line['code'], 'empty_output')
        self.assertEqual(line['usage']['input'], 3)

    def test_tool_calls_refused(self):
        content = json.dumps({'records': []})
        with _serve(_ok(None, content=content, finish='stop',
                        message_extra={'tool_calls': [{'id': '1'}]})) as (server, rec):
            proc = _run(_endpoint(server))
        self.assertNotEqual(proc.returncode, 0)
        line = _stderr_line(proc)
        self.assertEqual(line['category'], 'provider_failure')
        self.assertEqual(line['stop_reason'], 'toolUse')

    def test_unknown_finish_reason(self):
        with _serve(_ok(None, content='{"a":1}', finish='weird')) as (server, rec):
            proc = _run(_endpoint(server))
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(_stderr_line(proc)['category'], 'provider_failure')

    def test_auth_error_no_leak(self):
        payload = ('{"error":"%s"}' % SENTINEL_KEY).encode()

        def responder(rec, handler):
            return 401, payload, {'X-Echo': SENTINEL_KEY}
        with _serve(responder) as (server, rec):
            proc = _run(_endpoint(server), prompt=SENTINEL_PROMPT)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), '')
        self.assertNotIn(SENTINEL_KEY, proc.stdout)
        self.assertNotIn(SENTINEL_KEY, proc.stderr)
        self.assertNotIn(SENTINEL_PROMPT, proc.stdout)
        self.assertNotIn(SENTINEL_PROMPT, proc.stderr)
        self.assertEqual(_stderr_line(proc)['code'], 'authentication_error')

    def test_rate_limited_status(self):
        def responder(rec, handler):
            return 429, b'{}', {}
        with _serve(responder) as (server, rec):
            proc = _run(_endpoint(server))
        self.assertNotEqual(proc.returncode, 0)
        line = _stderr_line(proc)
        self.assertEqual(line['code'], 'rate_limited')
        self.assertEqual(line['status'], 429)

    def test_network_connection_failure(self):
        # Port 1 on loopback refuses connections; no external network involved.
        proc = _run('http://127.0.0.1:1/v1/chat/completions')
        self.assertNotEqual(proc.returncode, 0)
        line = _stderr_line(proc)
        self.assertEqual(line['category'], 'provider_failure')
        self.assertIn(line['code'], ('network_error', 'provider_error'))

    def test_bounded_timeout(self):
        import time

        def slow(rec, handler):
            time.sleep(2)
            return 200, _json_response(GOOD_CONTENT), {}
        with _serve(slow) as (server, rec):
            proc = _run(_endpoint(server), timeout='0.5')
        self.assertNotEqual(proc.returncode, 0)
        line = _stderr_line(proc)
        self.assertEqual(line['category'], 'timeout')
        self.assertEqual(line['code'], 'timeout')

    def test_max_response_size(self):
        blob = (b'x' * (2 * 1024 * 1024 + 10))
        payload = b'{"choices":[],"junk":"' + blob + b'"}'

        def responder(rec, handler):
            return 200, payload, {'Content-Type': 'application/json'}
        with _serve(responder) as (server, rec):
            proc = _run(_endpoint(server))
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(_stderr_line(proc)['category'], 'output_truncated')

    def test_redirect_not_followed(self):
        with _serve(_ok(None)) as (target, target_rec):
            for code in (301, 302, 303, 307, 308):
                with self.subTest(code=code):
                    def redirector(rec, handler, _code=code):
                        return _code, None, {'Location': _endpoint(target)}
                    with _serve(redirector) as (first, first_rec):
                        proc = _run(_endpoint(first))
                    self.assertNotEqual(proc.returncode, 0)
                    self.assertEqual(len(first_rec.requests), 1)
                    self.assertEqual(len(target_rec.requests), 0)
                    self.assertEqual(_stderr_line(proc)['category'],
                                     'provider_failure')

    def test_invalid_endpoint_rejected_before_http(self):
        for endpoint in [
            'https:///v1/chat/completions',
            'https://example.com/other',
            'https://user:pass@example.com/v1/chat/completions',
            'https://example.com/v1/chat/completions?x=1',
            'https://example.com/v1/chat/completions#frag',
            'http://localhost/v1/chat/completions',
            'http://example.com/v1/chat/completions',
            'http://127.0.0.2/v1/chat/completions',
            'http://2130706433/v1/chat/completions',
            'http://0x7f000001/v1/chat/completions',
        ]:
            with self.subTest(endpoint=endpoint):
                proc = _run(endpoint, allow_local=True)
                self.assertNotEqual(proc.returncode, 0, endpoint)
                self.assertEqual(proc.stdout.strip(), '', endpoint)
                self.assertEqual(_stderr_line(proc)['code'], 'provider_error')

    def test_local_endpoint_rejected_without_allow_local(self):
        with _serve(_ok(None)) as (server, rec):
            proc = _run(_endpoint(server), allow_local=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), '')
        self.assertEqual(_stderr_line(proc)['code'], 'provider_error')
        self.assertEqual(len(rec.requests), 0)

    def test_missing_key_rejected(self):
        with _serve(_ok(None)) as (server, rec):
            proc = _run(_endpoint(server), key=None)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(len(rec.requests), 0)
        self.assertEqual(_stderr_line(proc)['code'], 'authentication_error')

    def test_bad_timeout_and_max_output_rejected(self):
        with _serve(_ok(None)) as (server, rec):
            proc = _run(_endpoint(server), timeout='0')
            self.assertNotEqual(proc.returncode, 0)
            proc2 = _run(_endpoint(server), extra=['--max-output', '0'])
            self.assertNotEqual(proc2.returncode, 0)
        self.assertEqual(len(rec.requests), 0)

    def test_malformed_cli_does_not_echo(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), '--endpoint',
             'https://example.com/v1/chat/completions', '--provider', SENTINEL_KEY],
            input='', capture_output=True, text=True, timeout=30)
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn(SENTINEL_KEY, proc.stdout)
        self.assertNotIn(SENTINEL_KEY, proc.stderr)

    def test_empty_stdin_rejected(self):
        with _serve(_ok(None)) as (server, rec):
            proc = _run(_endpoint(server), prompt='')
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(_stderr_line(proc)['category'], 'input_limited')
        self.assertEqual(len(rec.requests), 0)

    def test_malformed_chunked_http_is_bounded(self):
        canary = b'SYNTHETIC-BODY-SENTINEL\r\n'

        def responder(rec, handler):
            return 200, canary, {'Transfer-Encoding': 'chunked'}
        with _serve(responder) as (server, rec):
            proc = _run(_endpoint(server))
        self.assertNotEqual(proc.returncode, 0)
        line = _stderr_line(proc)
        self.assertEqual(line['code'], 'network_error')
        self.assertNotIn('Traceback', proc.stderr)
        self.assertNotIn('SYNTHETIC-BODY-SENTINEL', proc.stdout)
        self.assertNotIn('SYNTHETIC-BODY-SENTINEL', proc.stderr)
        self.assertEqual(len(rec.requests), 1)

    def test_import_from_unrelated_cwd(self):
        with _serve(_ok(None)) as (server, rec):
            env = dict(os.environ)
            env['HTTP_MODEL_TEST_KEY'] = SENTINEL_KEY
            proc = subprocess.run(
                [sys.executable, str(SCRIPT), '--endpoint', _endpoint(server),
                 '--provider', 'openai', '--model', 'm',
                 '--key-env', 'HTTP_MODEL_TEST_KEY', '--timeout', '30',
                 '--allow-local-http'],
                input=GOOD_CONTENT, capture_output=True, text=True,
                timeout=60, cwd='/', env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_invoke_json_integration_success_and_error(self):
        sys.path.insert(0, str(ROOT))
        from tongle.worker import invoke_json
        with _serve(_ok(None, usage={'prompt_tokens': 4})) as (server, rec):
            env = dict(os.environ)
            env['HTTP_MODEL_TEST_KEY'] = SENTINEL_KEY
            command = [sys.executable, str(SCRIPT), '--endpoint',
                       _endpoint(server), '--provider', 'deepseek', '--model', 'm',
                       '--key-env', 'HTTP_MODEL_TEST_KEY', '--timeout', '30',
                       '--allow-local-http']
            value, usage = invoke_json(command, GOOD_CONTENT, 30, env=env)
        self.assertEqual(value, {'records': [{'title': 'x', 'text': 'y'}]})
        self.assertEqual(usage['input'], 4)
        self.assertTrue(usage['inputIncludesCache'])
        with _serve(lambda rec, h: (429, b'{}', {})) as (server, rec):
            env = dict(os.environ)
            env['HTTP_MODEL_TEST_KEY'] = SENTINEL_KEY
            command = [sys.executable, str(SCRIPT), '--endpoint',
                       _endpoint(server), '--provider', 'deepseek', '--model', 'm',
                       '--key-env', 'HTTP_MODEL_TEST_KEY', '--timeout', '30',
                       '--allow-local-http']
            from tongle.model_errors import ModelError
            with self.assertRaises(ModelError) as ctx:
                invoke_json(command, GOOD_CONTENT, 30, env=env)
        self.assertEqual(ctx.exception.code, 'rate_limited')

    def test_usage_totals_includes_cache_marker(self):
        sys.path.insert(0, str(ROOT))
        from tongle.model_errors import usage_totals
        self.assertEqual(usage_totals({'input': 10, 'inputIncludesCache': True}),
                         (10, None))
        self.assertEqual(usage_totals({'input': 10, 'cacheRead': 2,
                                       'cacheWrite': 3,
                                       'inputIncludesCache': True}),
                         (10, None))

    def test_usage_totals_marker_activation(self):
        sys.path.insert(0, str(ROOT))
        from tongle.model_errors import usage_totals
        for marker in (False, 'true', 1):
            with self.subTest(marker=marker):
                self.assertEqual(
                    usage_totals({'input': 10, 'cacheRead': 1, 'cacheWrite': 1,
                                  'inputIncludesCache': marker}), (12, None))

    def test_usage_totals_missing_or_invalid_input(self):
        sys.path.insert(0, str(ROOT))
        from tongle.model_errors import normalize_usage, usage_totals
        for usage in ({'cacheRead': 1, 'cacheWrite': 1,
                       'inputIncludesCache': True},
                      {'input': None, 'cacheRead': 1, 'cacheWrite': 1,
                       'inputIncludesCache': True},
                      {'input': True, 'cacheRead': 1, 'cacheWrite': 1,
                       'inputIncludesCache': True},
                      {'input': -5, 'cacheRead': 1, 'cacheWrite': 1,
                       'inputIncludesCache': True}):
            with self.subTest(usage=usage):
                self.assertNotIn('inputIncludesCache', normalize_usage(usage))
                self.assertEqual(usage_totals(usage), (None, None))

    def test_usage_totals_legacy_fields(self):
        sys.path.insert(0, str(ROOT))
        from tongle.model_errors import usage_totals
        self.assertEqual(
            usage_totals({'input': 10, 'cacheRead': 2, 'cacheWrite': 3}),
            (15, None))
        self.assertEqual(usage_totals({'input': 10, 'cacheRead': 2}),
                         (None, None))
        self.assertEqual(usage_totals({'input': 10, 'totalTokens': 99}),
                         (None, None))

    def test_http_unknown_usage_fields(self):
        for usage in (None, {'prompt_tokens': None, 'completion_tokens': True,
                             'total_tokens': -1},
                      {'prompt_tokens': True, 'completion_tokens': True,
                       'total_tokens': -1}):
            with self.subTest(usage=usage):
                with _serve(_ok(None, usage=usage)) as (server, rec):
                    proc = _run(_endpoint(server))
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(_stderr_line(proc)['usage'], {})

    def test_http_partial_positive_usage_retained(self):
        usage = {'prompt_tokens': 5, 'completion_tokens': None}
        with _serve(_ok(None, usage=usage)) as (server, rec):
            proc = _run(_endpoint(server))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        line = _stderr_line(proc)
        self.assertEqual(line['usage']['input'], 5)
        self.assertTrue(line['usage']['inputIncludesCache'])
        self.assertNotIn('output', line['usage'])

    def test_env_max_output_lowers_both_providers(self):
        for provider, field in (('deepseek', 'max_tokens'),
                                ('openai', 'max_completion_tokens')):
            with self.subTest(provider=provider):
                with _serve(_ok(None)) as (server, rec):
                    proc = _run(_endpoint(server), provider=provider,
                                extra=['--max-output', '1024'],
                                env_overrides={'TONGLE_MODEL_MAX_OUTPUT': '64'})
                self.assertEqual(proc.returncode, 0, proc.stderr)
                sent = json.loads(rec.requests[0][2])
                self.assertEqual(sent[field], 64)

    def test_env_max_output_higher_never_raises_cli_cap(self):
        for env_value in ('99999', '524288'):
            with self.subTest(env_value=env_value):
                with _serve(_ok(None)) as (server, rec):
                    proc = _run(_endpoint(server),
                                extra=['--max-output', '256'],
                                env_overrides={
                                    'TONGLE_MODEL_MAX_OUTPUT': env_value})
                self.assertEqual(proc.returncode, 0, proc.stderr)
                sent = json.loads(rec.requests[0][2])
                self.assertEqual(sent['max_tokens'], 256)

    def test_env_max_output_absent_preserves_cli(self):
        with _serve(_ok(None)) as (server, rec):
            proc = _run(_endpoint(server), extra=['--max-output', '512'])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        sent = json.loads(rec.requests[0][2])
        self.assertEqual(sent['max_tokens'], 512)

    def test_env_max_output_invalid_sends_zero_requests(self):
        for env_value in ('', '0', '-1', '+4', ' 8', '8 ', '3.5', '1e3',
                          '0x10', 'true', 'None', '99_9', '٣'):
            with self.subTest(env_value=env_value):
                with _serve(_ok(None)) as (server, rec):
                    proc = _run(_endpoint(server),
                                env_overrides={
                                    'TONGLE_MODEL_MAX_OUTPUT': env_value})
                self.assertNotEqual(proc.returncode, 0)
                self.assertEqual(proc.stdout.strip(), '')
                line = _stderr_line(proc)
                self.assertEqual(line['category'], 'provider_failure')
                self.assertEqual(line['code'], 'provider_error')
                self.assertEqual(len(rec.requests), 0)


class FrozenMacOSCAFileTests(unittest.TestCase):
    """Cross-machine frozen macOS must use the OS CA file, not Python's."""

    def setUp(self):
        self.http_model = _load_http_model()
        self._env = mock.patch.dict(os.environ, {
            'SSL_CERT_FILE': '/nonexistent/synthetic/cert.pem',
            'SSL_CERT_DIR': '/nonexistent/synthetic/certs',
        })
        self._env.start()
        self.addCleanup(self._env.stop)

    def _fake_opener(self):
        fake_response = object()
        opener = mock.Mock()
        opener.open.return_value = fake_response
        return opener, fake_response

    @unittest.skipUnless(
        sys.platform == 'darwin' and os.path.exists('/etc/ssl/cert.pem'),
        'requires native macOS OS CA file /etc/ssl/cert.pem',
    )
    def test_frozen_darwin_uses_os_ca_file(self):
        opener, fake_response = self._fake_opener()
        real_create_default_context = ssl.create_default_context
        captured = {}

        def wrapping_create_default_context(*args, **kwargs):
            captured['args'] = args
            captured['kwargs'] = kwargs
            return real_create_default_context(*args, **kwargs)

        with mock.patch.object(sys, 'frozen', True, create=True), \
                mock.patch.object(sys, 'platform', 'darwin'), \
                mock.patch.object(self.http_model.ssl, 'create_default_context',
                                  side_effect=wrapping_create_default_context), \
                mock.patch.object(self.http_model.urllib.request,
                                  'build_opener', return_value=opener) as build_opener:
            result = self.http_model._open(
                'https://example.invalid/v1/chat/completions',
                b'{}', {'Content-Type': 'application/json'}, 5.0)

        self.assertIs(result, fake_response)
        self.assertEqual(captured['kwargs'].get('cafile'), '/etc/ssl/cert.pem')
        self.assertEqual(captured['args'], ())
        context = real_create_default_context(cafile='/etc/ssl/cert.pem')
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertGreater(context.cert_store_stats()['x509_ca'], 0)
        handlers = build_opener.call_args_list[0][0]
        self.assertEqual(len(build_opener.call_args_list), 1)
        https_handlers = [h for h in handlers
                          if isinstance(h, self.http_model.urllib.request.HTTPSHandler)]
        self.assertEqual(len(https_handlers), 1)
        self.assertIsInstance(https_handlers[0]._context, ssl.SSLContext)
        self.assertEqual(https_handlers[0]._context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(https_handlers[0]._context.check_hostname)
        proxy_handlers = [h for h in handlers
                          if isinstance(h, self.http_model.urllib.request.ProxyHandler)]
        self.assertEqual(len(proxy_handlers), 1)
        self.assertEqual(proxy_handlers[0].proxies, {})
        self.assertTrue(any(isinstance(h, self.http_model._NoRedirect)
                            for h in handlers))

    def test_frozen_darwin_missing_ca_fails_closed(self):
        opener, _ = self._fake_opener()
        with mock.patch.object(sys, 'frozen', True, create=True), \
                mock.patch.object(sys, 'platform', 'darwin'), \
                mock.patch.object(self.http_model.ssl, 'create_default_context',
                                  side_effect=FileNotFoundError), \
                mock.patch.object(self.http_model.urllib.request,
                                  'build_opener', return_value=opener) as build_opener:
            with self.assertRaises(FileNotFoundError):
                self.http_model._open(
                    'https://example.invalid/v1/chat/completions',
                    b'{}', {'Content-Type': 'application/json'}, 5.0)
        build_opener.assert_not_called()

    def test_source_darwin_preserves_default_opener(self):
        opener, fake_response = self._fake_opener()
        with mock.patch.object(sys, 'frozen', False, create=True), \
                mock.patch.object(sys, 'platform', 'darwin'), \
                mock.patch.object(self.http_model.ssl, 'create_default_context',
                                  side_effect=AssertionError('must not be called')), \
                mock.patch.object(self.http_model.urllib.request,
                                  'build_opener', return_value=opener) as build_opener:
            result = self.http_model._open(
                'https://example.invalid/v1/chat/completions',
                b'{}', {'Content-Type': 'application/json'}, 5.0)
        self.assertIs(result, fake_response)
        handlers = build_opener.call_args_list[0][0]
        self.assertFalse(any(isinstance(h, self.http_model.urllib.request.HTTPSHandler)
                             for h in handlers))
        self.assertEqual(len(handlers), 2)

    def test_frozen_non_darwin_preserves_default_opener(self):
        opener, fake_response = self._fake_opener()
        with mock.patch.object(sys, 'frozen', True, create=True), \
                mock.patch.object(sys, 'platform', 'linux'), \
                mock.patch.object(self.http_model.ssl, 'create_default_context',
                                  side_effect=AssertionError('must not be called')), \
                mock.patch.object(self.http_model.urllib.request,
                                  'build_opener', return_value=opener) as build_opener:
            result = self.http_model._open(
                'https://example.invalid/v1/chat/completions',
                b'{}', {'Content-Type': 'application/json'}, 5.0)
        self.assertIs(result, fake_response)
        handlers = build_opener.call_args_list[0][0]
        self.assertFalse(any(isinstance(h, self.http_model.urllib.request.HTTPSHandler)
                             for h in handlers))
        self.assertEqual(len(handlers), 2)


if __name__ == '__main__':
    unittest.main()
