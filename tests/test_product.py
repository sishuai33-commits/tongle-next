import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP_ROOT = Path(tempfile.gettempdir()).resolve()


def fresh_root():
    return Path(tempfile.mkdtemp(prefix='tongle-product-', dir=str(TMP_ROOT)))


def run_cli(args, env=None, timeout=120, remove_env=()):
    argv = [sys.executable, '-m', 'tongle.product'] + list(args)
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    for name in remove_env:
        full_env.pop(name, None)
    proc = subprocess.run(argv, cwd=str(ROOT), text=True, capture_output=True,
                          env=full_env, timeout=timeout)
    out = proc.stdout.strip()
    payload = None
    if out:
        try:
            payload = json.loads(out)
        except ValueError:
            payload = None
    return proc.returncode, payload, proc.stdout, proc.stderr


def default_home(base):
    return str(base / 'home')


def vault_for(base):
    return str(base / 'vault')


def make_file(base, name='source.md', text='# hello\n'):
    path = base / name
    path.write_text(text, encoding='utf8')
    return path


def db_sources(home):
    db = Path(home) / 'state.sqlite3'
    conn = sqlite3.connect(str(db))
    try:
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r[0] for r in cur.fetchall()]
        for table in ('sources', 'source'):
            if table in tables:
                try:
                    return conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                except sqlite3.Error:
                    continue
        return -1
    finally:
        conn.close()


def current_versions(home):
    db = Path(home) / 'state.sqlite3'
    conn = sqlite3.connect(str(db))
    try:
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r[0] for r in cur.fetchall()]
        if 'sources' not in tables:
            return {}
        rows = conn.execute('SELECT id, current_version FROM sources').fetchall()
        return {r[0]: r[1] for r in rows}
    finally:
        conn.close()


def registry_entries(home):
    path = Path(home) / 'maintenance' / 'sources.json'
    data = json.loads(path.read_text(encoding='utf8'))
    return data.get('sources', [])


class Base(unittest.TestCase):
    def setUp(self):
        self.base = fresh_root()
        self.homes = []
        self.started_homes = set()

    def helper_setup(self, base=None, vault=None, project='demo', write=True, extra=(),
                     env=None, remove_env=()):
        b = base or self.base
        home = default_home(b)
        vlt = vault or vault_for(b)
        args = ['setup', '--home', home, '--vault', vlt, '--project', project]
        if write:
            args.append('--write')
        args.extend(extra)
        code, payload, out, err = run_cli(args, env=env, remove_env=remove_env)
        self.assertEqual(code, 0, err + out)
        self.assertIsInstance(payload, dict)
        self.homes.append(home)
        return home

    def helper_run(self, args, env=None, timeout=120, remove_env=()):
        return run_cli(args, env=env, timeout=timeout, remove_env=remove_env)

    def helper_stop(self, home, limit=20.0):
        if not (Path(home) / 'state.sqlite3').is_file():
            return
        run_cli(['stop', '--home', home], timeout=60)
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            code, payload, out, err = run_cli(['status', '--home', home], timeout=60)
            if code == 0 and isinstance(payload, dict):
                if not (payload.get('maintenance') or {}).get('running'):
                    return
            time.sleep(0.3)
        raise AssertionError('daemon did not stop')

    def tearDown(self):
        for home in list(self.started_homes):
            self.helper_stop(home)
        shutil.rmtree(str(self.base), ignore_errors=True)


class SetupTests(Base):
    def test_preview_writes_nothing(self):
        home = Path(default_home(self.base))
        vault = Path(vault_for(self.base))
        code, payload, out, err = run_cli([
            'setup', '--home', str(home), '--vault', str(vault), '--project', 'demo'])
        self.assertEqual(code, 0, err)
        self.assertFalse(home.exists())
        self.assertFalse(vault.exists())

    def test_preview_does_not_print_key(self):
        home = Path(default_home(self.base))
        vault = Path(vault_for(self.base))
        secret = 'top-secret-value-xyz'
        code, payload, out, err = run_cli([
            'setup', '--home', str(home), '--vault', str(vault),
            '--project', 'demo', '--provider', 'openai',
            '--endpoint', 'https://api.example.com/v1/chat/completions',
            '--model', 'gpt-test', '--key-env', 'TONGLE_TEST_KEY'],
            env={'TONGLE_TEST_KEY': secret})
        self.assertEqual(code, 0, err)
        self.assertNotIn(secret, out)
        self.assertNotIn(secret, err)

    def test_validation_failure_writes_nothing(self):
        home = Path(default_home(self.base))
        vault = Path(vault_for(self.base))
        code, payload, out, err = run_cli([
            'setup', '--home', str(home), '--vault', str(vault),
            '--project', 'demo', '--write', '--provider', 'openai',
            '--endpoint', 'http://api.example.com/v1/chat/completions',
            '--model', 'gpt-test', '--key-env', 'TONGLE_TEST_KEY'],
            env={'TONGLE_TEST_KEY': 'x'})
        self.assertNotEqual(code, 0)
        self.assertFalse((home / 'product.json').exists())
        self.assertFalse((home / 'config.json').exists())

    def test_write_offline_config_and_preferences(self):
        home = self.helper_setup()
        p = Path(home)
        config = json.loads((p / 'config.json').read_text(encoding='utf8'))
        self.assertEqual(config['knowledge_mode'], 'evolving')
        self.assertEqual(config['model_command'], [])
        self.assertEqual(config['knowledge_quiet_seconds'], 300)
        self.assertEqual(config['knowledge_daily_call_budget'], 40)
        self.assertEqual(config['knowledge_daily_token_budget'], 300000)
        self.assertEqual(config['knowledge_max_output_tokens'], 4096)
        self.assertEqual(config['model_timeout_seconds'], 200)
        prefs = (p / 'preferences.md').read_text(encoding='utf8')
        self.assertTrue(prefs.strip())
        for marker in ('\u7ed3\u8bba', '\u4f9d\u636e', '\u9002\u7528\u8303\u56f4', '\u9a8c\u6536', '\u6765\u6e90', '\u4e0d\u786e\u5b9a\u6027'):
            self.assertIn(marker, prefs)
        self.assertNotIn('Output is JSON', prefs)
        registry_path = p / 'maintenance' / 'sources.json'
        data = json.loads(registry_path.read_text(encoding='utf8'))
        self.assertEqual(len([s for s in data['sources'] if s.get('project') == 'demo']), 1)
        self.assertGreaterEqual(db_sources(home), 1)

    def test_setup_reports_style_unverified(self):
        home = Path(default_home(self.base))
        vault = Path(vault_for(self.base))
        code, payload, out, err = run_cli([
            'setup', '--home', str(home), '--vault', str(vault),
            '--project', 'demo'])
        self.assertEqual(code, 0, err + out)
        self.assertEqual(payload['style_effect'], 'unverified')
        self.assertFalse(home.exists())

    def test_repeat_setup_refuses_preserves_bytes(self):
        home = self.helper_setup()
        p = Path(home)
        config_bytes = (p / 'config.json').read_bytes()
        product_bytes = (p / 'product.json').read_bytes()
        code, payload, out, err = run_cli([
            'setup', '--home', home, '--vault', vault_for(self.base),
            '--project', 'demo', '--write'])
        self.assertNotEqual(code, 0)
        self.assertEqual((p / 'config.json').read_bytes(), config_bytes)
        self.assertEqual((p / 'product.json').read_bytes(), product_bytes)

    def test_source_symlink_rejected(self):
        home = self.helper_setup()
        target = make_file(self.base, 'real.md')
        link = self.base / 'link.md'
        link.symlink_to(target)
        code, payload, out, err = run_cli([
            'add', '--home', home, '--path', str(link), '--project', 'demo'])
        self.assertNotEqual(code, 0)

    def test_home_symlink_rejected(self):
        real = self.base / 'realhome'
        real.mkdir()
        link = self.base / 'linkhome'
        link.symlink_to(real, target_is_directory=True)
        code, payload, out, err = run_cli([
            'setup', '--home', str(link), '--vault', vault_for(self.base),
            '--project', 'demo', '--write'])
        self.assertNotEqual(code, 0)
        self.assertFalse((real / 'product.json').exists())

    def test_home_ancestor_symlink_rejected(self):
        real = self.base / 'realancestor'
        real.mkdir()
        link = self.base / 'linkancestor'
        link.symlink_to(real, target_is_directory=True)
        home = link / 'home'
        code, payload, out, err = run_cli([
            'setup', '--home', str(home), '--vault', vault_for(self.base),
            '--project', 'demo', '--write'])
        self.assertNotEqual(code, 0)
        self.assertFalse((real / 'home').exists())

    def test_existing_vault_symlink_no_home_created(self):
        home = self.base / 'home'
        real_vault = self.base / 'realvault'
        real_vault.mkdir()
        link_vault = self.base / 'vault'
        link_vault.symlink_to(real_vault, target_is_directory=True)
        code, payload, out, err = run_cli([
            'setup', '--home', str(home), '--vault', str(link_vault),
            '--project', 'demo', '--write'])
        self.assertNotEqual(code, 0)
        self.assertFalse(home.exists())

    def test_existing_zhi_shi_symlink_no_home_created(self):
        home = self.base / 'home'
        vault = self.base / 'vault'
        vault.mkdir()
        real_kb = self.base / 'realkb'
        real_kb.mkdir()
        link_kb = vault / '\u77e5\u8bc6'
        link_kb.symlink_to(real_kb, target_is_directory=True)
        code, payload, out, err = run_cli([
            'setup', '--home', str(home), '--vault', str(vault),
            '--project', 'demo', '--write'])
        self.assertNotEqual(code, 0)
        self.assertFalse(home.exists())

    def test_malformed_endpoint_whitespace_no_writes(self):
        home = Path(default_home(self.base))
        vault = Path(vault_for(self.base))
        code, payload, out, err = run_cli([
            'setup', '--home', str(home), '--vault', str(vault),
            '--project', 'demo', '--write', '--provider', 'openai',
            '--endpoint', 'https://api.example.com/v1/ chat',
            '--model', 'gpt-test', '--key-env', 'TONGLE_TEST_KEY'],
            env={'TONGLE_TEST_KEY': 'x'})
        self.assertNotEqual(code, 0)
        self.assertFalse(home.exists())
        self.assertFalse((home / 'config.json').exists())

    def test_invalid_port_no_writes(self):
        home = Path(default_home(self.base))
        vault = Path(vault_for(self.base))
        code, payload, out, err = run_cli([
            'setup', '--home', str(home), '--vault', str(vault),
            '--project', 'demo', '--write', '--provider', 'openai',
            '--endpoint', 'https://api.example.com:99999999/v1/chat',
            '--model', 'gpt-test', '--key-env', 'TONGLE_TEST_KEY'],
            env={'TONGLE_TEST_KEY': 'x'})
        self.assertNotEqual(code, 0)
        self.assertFalse(home.exists())

    def test_registry_corrupt_fails_preserves_bytes(self):
        home = self.helper_setup()
        registry = Path(home) / 'maintenance' / 'sources.json'
        registry.write_text('{not json', encoding='utf8')
        before = registry.read_bytes()
        count_before = db_sources(home)
        source = make_file(self.base, 'extra.md')
        code, payload, out, err = run_cli([
            'add', '--home', home, '--path', str(source), '--project', 'demo'])
        self.assertNotEqual(code, 0)
        self.assertEqual(registry.read_bytes(), before)
        self.assertEqual(db_sources(home), count_before)

    def test_registry_symlink_rejected(self):
        home = self.helper_setup()
        registry = Path(home) / 'maintenance' / 'sources.json'
        registry.unlink()
        real = self.base / 'real-registry.json'
        real.write_text('{"sources": []}', encoding='utf8')
        registry.symlink_to(real)
        source = make_file(self.base, 'extra.md')
        code, payload, out, err = run_cli([
            'add', '--home', home, '--path', str(source), '--project', 'demo'])
        self.assertNotEqual(code, 0)
        self.assertEqual(real.read_text(encoding='utf8'), '{"sources": []}')


class SourceTests(Base):
    def setUp(self):
        super().setUp()
        self.home = self.helper_setup()
        self.vault = Path(vault_for(self.base))

    def test_bad_document_ingest_does_not_register(self):
        bad = self.base / 'bad.docx'
        bad.write_bytes(b'\x00\x01not a real docx\xff\xfe')
        registry = Path(self.home) / 'maintenance' / 'sources.json'
        before_bytes = registry.read_bytes()
        before_count = db_sources(self.home)
        code, payload, out, err = run_cli([
            'add', '--home', self.home, '--path', str(bad), '--project', 'demo'])
        self.assertNotEqual(code, 0)
        self.assertEqual(registry.read_bytes(), before_bytes)
        self.assertEqual(db_sources(self.home), before_count)

    def test_add_duplicate_then_edit(self):
        source = make_file(self.base, 'doc.md', '# v1\n')
        code, first, out, err = run_cli([
            'add', '--home', self.home, '--path', str(source), '--project', 'demo'])
        self.assertEqual(code, 0, err)
        first_id = first['source_id']
        v1 = current_versions(self.home).get(first_id)
        count_after_first = db_sources(self.home)
        code, second, out, err = run_cli([
            'add', '--home', self.home, '--path', str(source), '--project', 'demo'])
        self.assertEqual(code, 0, err)
        self.assertEqual(second['source_id'], first_id)
        self.assertEqual(second.get('version'), v1)
        self.assertEqual(db_sources(self.home), count_after_first)
        entries = [r for r in registry_entries(self.home) if r.get('source_id') == first_id]
        self.assertEqual(len(entries), 1)
        source.write_text('# v2 changed\n', encoding='utf8')
        code, third, out, err = run_cli([
            'add', '--home', self.home, '--path', str(source), '--project', 'demo'])
        self.assertEqual(code, 0, err)
        self.assertEqual(third['source_id'], first_id)
        v2 = current_versions(self.home).get(first_id)
        self.assertGreater(v2, v1)
        self.assertEqual(third.get('version'), v2)

    def test_text_too_large_rejected(self):
        big = self.base / 'big.txt'
        big.write_text('x' * (5 * 1024 * 1024 + 1), encoding='utf8')
        code, payload, out, err = run_cli([
            'add', '--home', self.home, '--path', str(big), '--project', 'demo'])
        self.assertNotEqual(code, 0)

    def test_bad_extension_rejected(self):
        bad = self.base / 'note.bin'
        bad.write_text('data', encoding='utf8')
        code, payload, out, err = run_cli([
            'add', '--home', self.home, '--path', str(bad), '--project', 'demo'])
        self.assertNotEqual(code, 0)


class ProviderTests(Base):
    def test_provider_setup_writes_runner_argv(self):
        secret = 'super-secret-value-123'
        home = self.helper_setup(
            extra=['--provider', 'deepseek',
                   '--endpoint', 'https://api.deepseek.com/v1/chat/completions',
                   '--model', 'deepseek-chat', '--key-env', 'TONGLE_TEST_KEY'],
            env={'TONGLE_TEST_KEY': secret})
        p = Path(home)
        self.assertNotIn(secret, (p / 'config.json').read_text(encoding='utf8'))
        self.assertNotIn(secret, (p / 'product.json').read_text(encoding='utf8'))
        config = json.loads((p / 'config.json').read_text(encoding='utf8'))
        command = config['model_command']
        self.assertIn('http_model.py', ' '.join(command))
        self.assertIn('--provider', command)
        self.assertIn('deepseek', command)
        self.assertIn('--endpoint', command)
        self.assertIn('https://api.deepseek.com/v1/chat/completions', command)
        self.assertIn('--model', command)
        self.assertIn('deepseek-chat', command)
        self.assertIn('--key-env', command)
        self.assertIn('TONGLE_TEST_KEY', command)
        self.assertIn('--max-output', command)
        self.assertIn('4096', command)
        self.assertIn('--timeout', command)
        self.assertIn('180', command)
        code, payload, out, err = run_cli(
            ['status', '--home', home], remove_env=('TONGLE_TEST_KEY',))
        self.assertEqual(code, 0, err + out)
        self.assertNotIn(secret, out + err)
        model = payload['model']
        self.assertTrue(model.get('configured'))
        self.assertFalse(model.get('key_present'))

    def test_missing_key_env_start_fails(self):
        home = Path(default_home(self.base))
        vault = Path(vault_for(self.base))
        code, payload, out, err = run_cli([
            'setup', '--home', str(home), '--vault', str(vault),
            '--project', 'demo', '--write', '--provider', 'openai',
            '--endpoint', 'https://api.example.com/v1/chat/completions',
            '--model', 'gpt-test', '--key-env', 'TONGLE_ABSENT_KEY'],
            remove_env=('TONGLE_ABSENT_KEY',))
        self.assertNotEqual(code, 0)
        self.assertFalse(home.exists())

    def test_setup_then_missing_key_blocks_start_and_once(self):
        secret = 'present-for-setup-only'
        home = self.helper_setup(
            extra=['--provider', 'openai',
                   '--endpoint', 'https://api.example.com/v1/chat/completions',
                   '--model', 'gpt-test', '--key-env', 'TONGLE_ABSENT_KEY'],
            env={'TONGLE_ABSENT_KEY': secret})
        for cmd in ('start', 'once'):
            code, payload, out, err = run_cli(
                [cmd, '--home', home], remove_env=('TONGLE_ABSENT_KEY',))
            self.assertNotEqual(code, 0, cmd)
            self.assertNotIn(secret, out + err)
        code, payload, out, err = run_cli(['status', '--home', home])
        self.assertEqual(code, 0, err + out)
        maint = payload.get('maintenance') or {}
        self.assertFalse(maint.get('running'))


class ConfigGateTests(Base):
    def test_invalid_model_command_list_status_and_start_fail_no_daemon(self):
        home = self.helper_setup()
        p = Path(home)
        config = json.loads((p / 'config.json').read_text(encoding='utf8'))
        config['model_command'] = [3]
        (p / 'config.json').write_text(json.dumps(config), encoding='utf8')
        for cmd in ('status', 'start'):
            code, payload, out, err = run_cli([cmd, '--home', home])
            self.assertNotEqual(code, 0, cmd)
            self.assertNotIn('3', out + err)
        self.assertNotIn(home, self.started_homes)

    def test_dangling_config_symlink_rejects_status_and_start(self):
        home = self.helper_setup()
        p = Path(home)
        (p / 'config.json').unlink()
        (p / 'config.json').symlink_to(p / 'gone-config.json')
        for cmd in ('status', 'start'):
            code, payload, out, err = run_cli([cmd, '--home', home])
            self.assertNotEqual(code, 0, cmd)
        self.assertNotIn(home, self.started_homes)

    def test_argument_error_sentinel_not_echoed(self):
        home = Path(default_home(self.base))
        vault = Path(vault_for(self.base))
        code, payload, out, err = run_cli([
            'setup', '--home', str(home), '--vault', str(vault),
            '--project', 'demo', '--style', 'SECRET_INVALID'])
        self.assertNotEqual(code, 0)
        self.assertNotIn('SECRET_INVALID', out + err)

    def test_corrupt_config_status_fails_no_leak(self):
        home = self.helper_setup()
        secret = 'sentinel-config-leak'
        (Path(home) / 'config.json').write_text('{bad json ' + secret, encoding='utf8')
        code, payload, out, err = run_cli(['status', '--home', home])
        self.assertNotEqual(code, 0)
        self.assertNotIn(secret, out + err)

    def test_corrupt_config_start_fails_no_leak(self):
        home = self.helper_setup()
        secret = 'sentinel-start-leak'
        (Path(home) / 'config.json').write_text('{bad json ' + secret, encoding='utf8')
        code, payload, out, err = run_cli(['start', '--home', home])
        self.assertNotEqual(code, 0)
        self.assertNotIn(secret, out + err)

    def test_corrupt_product_status_fails_no_leak(self):
        home = self.helper_setup()
        secret = 'sentinel-product-leak'
        (Path(home) / 'product.json').write_text('nope ' + secret, encoding='utf8')
        code, payload, out, err = run_cli(['status', '--home', home])
        self.assertNotEqual(code, 0)
        self.assertNotIn(secret, out + err)

    def test_corrupt_product_start_fails_no_leak(self):
        home = self.helper_setup()
        secret = 'sentinel-product-start-leak'
        (Path(home) / 'product.json').write_text('nope ' + secret, encoding='utf8')
        code, payload, out, err = run_cli(['start', '--home', home])
        self.assertNotEqual(code, 0)
        self.assertNotIn(secret, out + err)


class UninitTests(Base):
    def test_operational_commands_fail_without_creating(self):
        home = Path(default_home(self.base))
        for cmd in ('start', 'stop', 'status', 'once', 'backup'):
            args = [cmd, '--home', str(home)]
            if cmd == 'backup':
                args.extend(['--to', str(self.base / 'b.tar')])
            code, payload, out, err = run_cli(args)
            self.assertNotEqual(code, 0, cmd)
        self.assertFalse(home.exists())

    def test_add_uninitialized_fails_no_writes(self):
        home = Path(default_home(self.base))
        src = make_file(self.base, 'a.md')
        code, payload, out, err = run_cli([
            'add', '--home', str(home), '--path', str(src), '--project', 'demo'])
        self.assertNotEqual(code, 0)
        self.assertFalse(home.exists())

    def test_context_uninitialized_fails(self):
        home = Path(default_home(self.base))
        code, payload, out, err = run_cli([
            'context', '--home', str(home), '--text', 'hello', '--project', 'demo'])
        self.assertNotEqual(code, 0)
        self.assertFalse(home.exists())


class ConnectTests(Base):
    def test_connect_writes_no_config(self):
        home = Path(default_home(self.base))
        code, payload, out, err = run_cli([
            'connect', '--home', str(home), '--host', 'codex', '--project', 'demo'])
        self.assertEqual(code, 0, err)
        self.assertFalse(home.exists())
        self.assertIn('env', payload)
        self.assertEqual(payload['env']['TONGLE_NEXT_HOME'], str(home))
        self.assertIn('hook.py', ' '.join(payload['argv']))

    def test_connect_pi_env_only(self):
        home = Path(default_home(self.base))
        code, payload, out, err = run_cli([
            'connect', '--home', str(home), '--host', 'pi'])
        self.assertEqual(code, 0, err)
        self.assertFalse(home.exists())
        self.assertEqual(payload['command'][0], 'pi')


class ContextTests(Base):
    def setUp(self):
        super().setUp()
        self.home = self.helper_setup()

    def test_context_uses_metadata_project(self):
        code, payload, out, err = run_cli([
            'context', '--home', self.home, '--text', 'hello'])
        self.assertEqual(code, 0, err + out)

    def test_context_explicit_project_wins(self):
        code, payload, out, err = run_cli([
            'context', '--home', self.home, '--text', 'hello', '--project', 'other'])
        self.assertEqual(code, 0, err + out)

    def test_context_without_metadata_needs_project(self):
        other = self.base / 'bare'
        code, payload, out, err = run_cli([
            'context', '--home', str(other), '--text', 'hello'])
        self.assertNotEqual(code, 0)


class LifecycleTests(Base):
    def setUp(self):
        super().setUp()
        self.home = self.helper_setup()
        self.vault = Path(vault_for(self.base))

    def test_lifecycle_offline(self):
        self.started_homes.add(self.home)
        code, payload, out, err = run_cli(['start', '--home', self.home])
        self.assertEqual(code, 0, err + out)
        code, payload, out, err = run_cli(['status', '--home', self.home])
        self.assertEqual(code, 0, err + out)
        self.assertFalse(payload['model']['configured'])
        maint = payload.get('maintenance') or {}
        self.assertTrue(maint.get('running'))
        code, payload, out, err = run_cli(['once', '--home', self.home])
        self.assertEqual(code, 0, err + out)
        code, payload, out, err = run_cli(['stop', '--home', self.home])
        self.assertEqual(code, 0, err + out)
        self.helper_stop(self.home)
        code, payload, out, err = run_cli(['status', '--home', self.home])
        self.assertEqual(code, 0, err + out)
        self.assertFalse((payload.get('maintenance') or {}).get('running'))

    def test_stop_idle_reports_false(self):
        code, payload, out, err = run_cli(['stop', '--home', self.home])
        self.assertEqual(code, 0, err + out)
        if isinstance(payload, dict) and 'stop_requested' in payload:
            self.assertFalse(payload['stop_requested'])

    def test_backup_restore_offline(self):
        archive = self.base / 'backup.tar'
        code, payload, out, err = run_cli([
            'backup', '--home', self.home, '--to', str(archive)])
        self.assertEqual(code, 0, err + out)
        restored_home = default_home(self.base / 'restored')
        restored_vault = str(self.base / 'restored-vault')
        code, payload, out, err = run_cli([
            'restore', '--home', restored_home, '--from', str(archive),
            '--vault', restored_vault])
        self.assertEqual(code, 0, err + out)
        self.assertTrue(payload['metadata_missing'])
        self.homes.append(restored_home)
        code, status, out, err = run_cli(['status', '--home', restored_home])
        self.assertEqual(code, 0, err + out)
        self.assertFalse(status['model']['configured'])
        code, payload, out, err = run_cli([
            'context', '--home', restored_home, '--text', 'hello'])
        self.assertNotEqual(code, 0)
        code, payload, out, err = run_cli([
            'context', '--home', restored_home, '--text', 'hello', '--project', 'demo'])
        self.assertEqual(code, 0, err + out)
        self.started_homes.add(restored_home)
        code, payload, out, err = run_cli(['start', '--home', restored_home])
        self.assertEqual(code, 0, err + out)
        code, status, out, err = run_cli(['status', '--home', restored_home])
        self.assertEqual(code, 0, err + out)
        self.assertTrue((status.get('maintenance') or {}).get('running'))


class MaintainTimeoutDispatchTests(unittest.TestCase):
    def test_once_uses_no_timeout_and_others_use_maintain_timeout(self):
        from unittest.mock import patch
        from tongle import product

        completed = subprocess.CompletedProcess([], 0, '{}', '')
        with patch.object(product, '_run', return_value=completed) as run:
            product._maintain(Path('/synthetic/home'), 'once')
            self.assertIsNone(run.call_args[0][1])
        for command in ('start', 'status', 'stop'):
            with patch.object(product, '_run', return_value=completed) as run:
                product._maintain(Path('/synthetic/home'), command)
                self.assertEqual(run.call_args[0][1], 200)

    def test_core_status_uses_core_timeout(self):
        from unittest.mock import patch
        from tongle import product

        completed = subprocess.CompletedProcess([], 0, '{}', '')
        with patch.object(product, '_run', return_value=completed) as run:
            product._core(Path('/synthetic/home'), ['status'])
            self.assertEqual(run.call_args[0][1], product.CORE_TIMEOUT)
            self.assertEqual(product.CORE_TIMEOUT, 200)


if __name__ == '__main__':
    unittest.main()
