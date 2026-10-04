"""Unit tests for tongle.evaluate (the on-demand consumption evaluation module).

These are UNIT tests of the harness only. A mocked ``consumer`` is a synthetic
harness: passing these tests does NOT demonstrate native Pi host quality or
production task benefit. Native validation must be run separately with a real
``pi`` binary.

Run: python3 -m unittest tests.test_evaluate -v
"""
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tongle import evaluate  # noqa: E402
from tongle.engine import Engine  # noqa: E402
from tongle.evolution import Evolution  # noqa: E402


def make_case(**overrides):
    case = {
        'schema': 1,
        'id': 'case-1',
        'version': 'v1',
        'project': 'proj-eval',
        'material': 'synthetic',
        'sources': [
            {'key': 's1', 'role': 'user', 'text': '项目目标：减少手工核对。'},
            {'key': 's2', 'role': 'user', 'text': '约束：不引入数据库服务。'},
        ],
        'cards': [
            {'key': 'goal', 'title': '目标', 'body': '减少手工核对。',
             'evidence': [{'source': 's1', 'quote': '项目目标：减少手工核对。'}]},
            {'key': 'constraint', 'title': '约束', 'body': '不引入数据库服务。',
             'evidence': [{'source': 's2', 'quote': '约束：不引入数据库服务。'}]},
        ],
        'tasks': [
            {'id': 't1', 'prompt': '回答目标是什么？', 'expect': {'goal': '减少手工核对'}},
            {'id': 't2', 'prompt': '回答约束是什么？', 'expect': {'constraint': '不引入数据库服务'}},
        ],
    }
    case.update(overrides)
    return case


class TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='tongle-eval-')
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()


# ------------------------------------------------------------------ validation

class TestValidateCase(TempCase):
    def test_accepts_minimal_valid_case(self):
        case = make_case()
        self.assertIs(evaluate.validate_case(case), case)

    def test_refuses_private_material_before_any_work(self):
        with self.assertRaises(ValueError) as ctx:
            evaluate.validate_case(make_case(material='private'))
        self.assertIn('synthetic', str(ctx.exception))

    def test_refuses_unknown_task_field(self):
        case = make_case()
        case['tasks'][0]['extra'] = 1
        with self.assertRaises(ValueError):
            evaluate.validate_case(case)

    def test_refuses_duplicate_task_id(self):
        case = make_case()
        case['tasks'][1]['id'] = 't1'
        with self.assertRaises(ValueError):
            evaluate.validate_case(case)

    def test_refuses_duplicate_source_and_card_keys(self):
        case = make_case()
        case['sources'][1]['key'] = 's1'
        with self.assertRaises(ValueError):
            evaluate.validate_case(case)
        case = make_case()
        case['cards'][1]['key'] = 'goal'
        with self.assertRaises(ValueError):
            evaluate.validate_case(case)

    def test_refuses_bad_role(self):
        case = make_case()
        case['sources'][0]['role'] = 'system'
        with self.assertRaises(ValueError):
            evaluate.validate_case(case)

    def test_refuses_unquoted_evidence_source(self):
        case = make_case()
        case['cards'][0]['evidence'] = [{'source': 'missing', 'quote': 'x'}]
        with self.assertRaises(ValueError):
            evaluate.validate_case(case)

    def test_refuses_absolute_card_path(self):
        case = make_case()
        case['cards'][0]['path'] = '/etc/passwd'
        with self.assertRaises(ValueError):
            evaluate.validate_case(case)

    def test_candidate_requires_existing_card_only(self):
        case = make_case(candidate=[{'key': 'new', 'body': 'b', 'reason': 'r',
                                     'evidence': [{'source': 's1', 'quote': '项目目标：减少手工核对。'}]}],
                         review={'approved': True, 'findings': [], 'proposal_hash': 'x',
                                 'reviewer': 'r'})
        with self.assertRaises(ValueError) as ctx:
            evaluate.validate_case(case)
        self.assertIn('existing card', str(ctx.exception))

    def test_candidate_rejects_duplicate_target_key(self):
        change = {'key': 'goal', 'body': 'b', 'reason': 'r',
                  'evidence': [{'source': 's1', 'quote': '项目目标：减少手工核对。'}]}
        candidate = [change, dict(change)]
        case = make_case(candidate=candidate,
                         review={'approved': True, 'findings': [],
                                 'proposal_hash': evaluate.digest(candidate),
                                 'reviewer': 'r'})
        with self.assertRaises(ValueError) as ctx:
            evaluate.validate_case(case)
        self.assertIn('duplicate candidate', str(ctx.exception))

    def test_review_hash_must_match_exact_candidate(self):
        candidate = [{'key': 'goal', 'body': '新目标', 'reason': 'r',
                      'evidence': [{'source': 's1', 'quote': '项目目标：减少手工核对。'}]}]
        case = make_case(candidate=candidate,
                         review={'approved': True, 'findings': [],
                                 'proposal_hash': 'deadbeef', 'reviewer': 'r'})
        with self.assertRaises(ValueError) as ctx:
            evaluate.validate_case(case)
        self.assertIn('proposal_hash', str(ctx.exception))

    def test_valid_candidate_passes(self):
        candidate = [{'key': 'goal', 'body': '新目标', 'reason': 'r',
                      'evidence': [{'source': 's1', 'quote': '项目目标：减少手工核对。'}]}]
        case = make_case(candidate=candidate,
                         review={'approved': True, 'findings': [],
                                 'proposal_hash': evaluate.digest(candidate),
                                 'reviewer': 'human'})
        evaluate.validate_case(case)


# --------------------------------------------------------------------- summary

class TestSummary(TempCase):
    def _home(self):
        home = self.root / 'home'
        engine = Engine(home)
        engine.store.close()
        return home

    def test_old_schema_absence_supported(self):
        home = self._home()
        result = evaluate.summary(home)
        self.assertEqual(result['receipts'], 0)
        self.assertEqual(result['task_benefit'], 'unknown')
        self.assertEqual(result['bindings'], {})
        # Old schema must not be created by a read-only summary.
        db = sqlite3.connect(home / 'state.sqlite3')
        try:
            tables = {r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            db.close()
        self.assertNotIn('knowledge_consumption', tables)

    def test_binding_absent_is_legacy_recent_and_no_output_is_unknown(self):
        home = self.root / 'home2'
        engine = Engine(home)
        try:
            # A pre-``binding`` schema: the column does not exist at all.
            engine.db.execute(
                'CREATE TABLE knowledge_consumption'
                '(id TEXT PRIMARY KEY, project TEXT, session TEXT, query TEXT,'
                ' offered TEXT, output_source TEXT, checks TEXT, state TEXT,'
                ' created_at REAL)')
            engine.db.execute(
                "INSERT INTO knowledge_consumption VALUES('r1','p','s','q','[]',NULL,NULL,'offered',1.0)")
            engine.db.execute(
                "INSERT INTO knowledge_consumption VALUES('r2','p','s','q','[]','src',NULL,'awaiting_check',1.0)")
            engine.db.commit()
        finally:
            engine.store.close()
        result = evaluate.summary(home, project='p')
        self.assertEqual(result['bindings'].get('unknown'), 1)
        self.assertEqual(result['bindings'].get('legacy_recent'), 1)
        self.assertEqual(result['receipts'], 2)

    def test_malformed_evidence_counted_not_inflated(self):
        home = self.root / 'home3'
        engine = Engine(home)
        try:
            Evolution(engine)
            good = json.dumps([{'id': 'c1', 'delivery': 'full'}])
            engine.db.execute(
                "INSERT INTO knowledge_consumption(id,project,session,query,offered,output_source,binding,state,created_at)"
                " VALUES('g','p','s','q',?,'src','receipt','checked',1.0)", (good,))
            engine.db.execute(
                "INSERT INTO knowledge_consumption(id,project,session,query,offered,output_source,binding,state,created_at)"
                " VALUES('b','p','s','q','{not json','src','receipt','checked',1.0)")
            engine.db.commit()
        finally:
            engine.store.close()
        result = evaluate.summary(home, project='p')
        self.assertEqual(result['receipts'], 2)
        self.assertEqual(result['malformed_records'], 1)
        self.assertEqual(result['distinct_offered_cards'], 1)

    def test_summary_does_not_modify_database(self):
        home = self.root / 'home4'
        engine = Engine(home)
        try:
            Evolution(engine)
        finally:
            engine.store.close()
        db_path = home / 'state.sqlite3'
        before = db_path.stat().st_mtime_ns, db_path.stat().st_size
        evaluate.summary(home)
        after = db_path.stat().st_mtime_ns, db_path.stat().st_size
        self.assertEqual(before, after)

    def test_receipt_assessments_distinct_from_legacy(self):
        home = self.root / 'home6'
        engine = Engine(home)
        try:
            Evolution(engine)
            checks = json.dumps({'evidence': [
                {'card_id': 'c1', 'verdict': 'applied'}]})
            engine.db.execute(
                "INSERT INTO knowledge_consumption(id,project,session,query,offered,output_source,binding,checks,state,created_at)"
                " VALUES('r','p','s','q','[{\"id\":\"c1\",\"delivery\":\"full\"}]','src','receipt',?,'checked',1.0)",
                (checks,))
            engine.db.execute(
                "INSERT INTO knowledge_consumption(id,project,session,query,offered,output_source,binding,checks,state,created_at)"
                " VALUES('l','p','s','q','[{\"id\":\"c1\",\"delivery\":\"full\"}]','src','legacy_recent',?,'checked',1.0)",
                (checks,))
            engine.db.commit()
        finally:
            engine.store.close()
        result = evaluate.summary(home, project='p')
        self.assertEqual(result['model_assessments'].get('receipt:applied'), 1)
        self.assertEqual(result['model_assessments'].get('legacy_recent:applied'), 1)
        self.assertEqual(result['distinct_receipt_assessed_applied_cards'], 1)
        self.assertEqual(result['task_benefit'], 'unknown')

    def test_symlink_database_refused(self):
        home = self.root / 'home5'
        home.mkdir()
        real = self.root / 'real.sqlite3'
        real.write_bytes(b'')
        (home / 'state.sqlite3').symlink_to(real)
        with self.assertRaises(ValueError):
            evaluate.summary(home)


# ------------------------------------------------------------ environment/isolation

class TestEnvironment(TempCase):
    def test_clean_env_removes_inherited_secrets(self):
        old = dict(os.environ)
        try:
            os.environ.update(TONGLE_NEXT_HOME='/x', TONGLE_MODEL_PROVIDER='zhipu',
                              PYTHONPATH='/y', PI_SESSION_DIR='/z',
                              PI_CONFIG='/w', PI_API_KEY='keep-me')
            env = evaluate._clean_env(self.root, REPO_ROOT, sys.executable)
        finally:
            os.environ.clear()
            os.environ.update(old)
        self.assertFalse('TONGLE_MODEL_PROVIDER' in env)
        self.assertFalse('PYTHONPATH' in env)
        self.assertFalse('PI_SESSION_DIR' in env)
        self.assertFalse('PI_CONFIG' in env)
        # Credential/identity variables must pass through or native auth breaks.
        self.assertEqual(env.get('PI_API_KEY'), 'keep-me')
        self.assertEqual(env['TONGLE_NEXT_HOME'], str(self.root / 'home'))

    def test_check_out_rejects_existing_and_symlink_and_instance(self):
        out = self.root / 'out'
        out.mkdir()
        with self.assertRaises(ValueError):
            evaluate._check_out(out)
        target = self.root / 'real'
        target.mkdir()
        link = self.root / 'link'
        link.symlink_to(target)
        with self.assertRaises(ValueError):
            evaluate._check_out(link / 'new')
        inst = self.root / 'instance'
        inst.mkdir()
        (inst / 'state.sqlite3').write_text('')
        with self.assertRaises(ValueError):
            evaluate._check_out(inst / 'new')
        vault = self.root / 'vault'
        (vault / '.obsidian').mkdir(parents=True)
        with self.assertRaises(ValueError):
            evaluate._check_out(vault / 'new')


FAKE_PI = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, os, sys, time
    from pathlib import Path
    argv = sys.argv[1:]
    report = Path.cwd() / "pi_report.json"
    report.write_text(json.dumps({
        "argv": argv,
        "env": {k: v for k, v in os.environ.items() if k.startswith(("TONGLE_", "PI_")) or k == "PYTHONPATH"},
    }))
    if "--fail" in argv:
        sys.exit(3)
    if "--sleep" in argv:
        time.sleep(30)
    message = {"role": "assistant", "stopReason": "stop", "provider": "fake-provider",
               "model": "fake-model", "usage": {"input": 5, "output": 7},
               "content": [{"type": "text", "text": "{\\"goal\\": \\"减少手工核对\\"}"}]}
    print(json.dumps({"type": "message_end", "message": message}), flush=True)
''')


class TestConsumeNative(TempCase):
    def _fake_pi(self):
        path = self.root / 'fake-pi'
        path.write_text(FAKE_PI)
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        return str(path)

    def test_native_invocation_flags_identity_and_env(self):
        root = self.root / 'run'; root.mkdir()
        case = make_case()
        result = evaluate.consume(root, case, case['tasks'][0], 'current',
                                  self._fake_pi(), 'zhipu', 'glm', timeout=20)
        self.assertEqual(result['provider'], 'fake-provider')
        self.assertEqual(result['model'], 'fake-model')
        self.assertTrue(result['answer'].startswith('{'))
        report = json.loads((root / 'workspace' / 'pi_report.json').read_text())
        argv = report['argv']
        for flag in ('--no-tools', '--no-extensions', '--no-skills',
                     '--no-context-files', '--no-prompt-templates', '--no-themes',
                     '--offline', '--no-session'):
            self.assertIn(flag, argv)
        self.assertIn(str(REPO_ROOT / 'adapters' / 'pi.ts'), argv)
        self.assertIn(str(root / 'budget.ts'), argv)
        # Every Tongle path is explicit; nothing inherited.
        self.assertEqual(report['env'].get('TONGLE_NEXT_HOME'), str(root / 'home'))
        self.assertEqual(report['env'].get('TONGLE_NEXT_PROJECT'), case['project'])
        self.assertNotIn('PYTHONPATH', report['env'])
        # The budget extension caps output and provider requests.
        guard = (root / 'budget.ts').read_text()
        self.assertIn('cap=1200', guard)
        self.assertIn('max_completion_tokens', guard)
        self.assertIn('provider-request budget exceeded', guard)

    def test_off_arm_omits_adapter(self):
        root = self.root / 'off'; root.mkdir()
        case = make_case()
        evaluate.consume(root, case, case['tasks'][0], 'off', self._fake_pi(),
                         'zhipu', 'glm', timeout=20)
        report = json.loads((root / 'workspace' / 'pi_report.json').read_text())
        self.assertNotIn(str(REPO_ROOT / 'adapters' / 'pi.ts'), report['argv'])

    def test_timeout_raises_and_retains_raw_evidence(self):
        root = self.root / 'to'; root.mkdir()
        case = make_case()
        task = dict(case['tasks'][0], prompt='--sleep')
        with self.assertRaises(subprocess.SubprocessError):
            evaluate.consume(root, case, task, 'off', self._fake_pi(), 'p', 'm', timeout=1)
        self.assertTrue((root / 'events.jsonl').exists())

    def test_nonzero_exit_is_error(self):
        root = self.root / 'fail'; root.mkdir()
        case = make_case()
        task = dict(case['tasks'][0], prompt='--fail')
        with self.assertRaises(ValueError):
            evaluate.consume(root, case, task, 'off', self._fake_pi(), 'p', 'm', timeout=20)


# ------------------------------------------------------------------ preparation

class TestPrepare(TempCase):
    def test_prepare_materializes_and_publishes(self):
        root = self.root / 'prep'
        info = evaluate.prepare(root, make_case())
        self.assertTrue(Path(info['home']).is_dir())
        self.assertEqual(info['publication']['conflicts'], [])
        self.assertEqual(len(info['cards']), 2)
        self.assertTrue(all(c['body_sha256'] for c in info['cards'].values()))

    def test_prepare_applies_candidate_updates(self):
        candidate = [{'key': 'goal', 'body': '新目标', 'reason': 'r',
                      'evidence': [{'source': 's1', 'quote': '项目目标：减少手工核对。'}]}]
        case = make_case(candidate=candidate,
                         review={'approved': True, 'findings': [],
                                 'proposal_hash': evaluate.digest(candidate),
                                 'reviewer': 'human'})
        base = self.root / 'base'; with_cand = self.root / 'cand'
        before = evaluate.prepare(base, make_case())
        after = evaluate.prepare(with_cand, case, candidate=True)
        self.assertNotEqual(before['cards']['goal']['body_sha256'],
                            after['cards']['goal']['body_sha256'])
        self.assertEqual(before['cards']['goal']['version'], 1)
        self.assertEqual(after['cards']['goal']['version'], 2)

    def test_prepare_rejects_quote_absent_from_source(self):
        candidate = [{'key': 'goal', 'body': '新目标', 'reason': 'r',
                      'evidence': [{'source': 's1', 'quote': '不存在的引文'}]}]
        case = make_case(candidate=candidate,
                         review={'approved': True, 'findings': [],
                                 'proposal_hash': evaluate.digest(candidate),
                                 'reviewer': 'human'})
        with self.assertRaises(ValueError):
            evaluate.prepare(self.root / 'bad', case, candidate=True)

    def test_apply_requires_matching_review_hash(self):
        root = self.root / 'review'
        evaluate.prepare(root, make_case())
        engine = Engine(root / 'home')
        try:
            cards = Evolution(engine).cards.list('proj-eval')
            target = next(c for c in cards if c['key'] == 'goal')
            operations = [{'op': 'update', 'id': target['id'],
                           'expected_version': target['version'], 'body': '新',
                           'evidence': target['evidence'], 'reason': 'r'}]
            with self.assertRaises(ValueError):
                Evolution(engine).cards.apply('b', 'proj-eval', operations,
                                              {'approved': True, 'findings': [],
                                               'proposal_hash': 'wrong'})
        finally:
            engine.store.close()


# ------------------------------------------------------------------------- run

class TestRunCase(TempCase):
    def _consumer(self, fail_arms=()):
        def consumer(root, case, task, arm, pi, provider, model, timeout):
            # A synthetic harness answer; never proof of native host quality.
            if arm in fail_arms:
                raise ValueError('synthetic failure for arm ' + arm)
            return {'answer': json.dumps(next(t['expect'] for t in make_case()['tasks'] if t['id'] == task['id'])), 'usage': {'input': 1, 'output': 1},
                    'seconds': 0.0, 'provider': 'fake', 'model': 'fake', 'cost': None}
        return consumer

    def test_preflight_budget_refused_before_any_call(self):
        case = make_case()
        with self.assertRaises(ValueError):
            evaluate.run_case(case, self.root / 'o1', 'pi', 'p', 'm',
                              max_calls=2, timeout=10, consumer=self._consumer())
        self.assertFalse((self.root / 'o1').exists())

    def test_infra_errors_excluded_from_effective_denominator(self):
        case = make_case()
        report = evaluate.run_case(case, self.root / 'o2', 'pi', 'p', 'm',
                                   max_calls=9, timeout=10,
                                   consumer=self._consumer(fail_arms=('current',)))
        # 'current' rows are infra; only 'off' rows complete.
        self.assertEqual(report['total_tasks'], 2)
        self.assertEqual(report['infra_or_missing_pairs'], 2)
        self.assertEqual(report['arms']['off']['effective_tasks'], 0)
        self.assertGreaterEqual(report['infra_rows'], 2)

    def test_immutability_rerun_same_path_refused(self):
        case = make_case()
        evaluate.run_case(case, self.root / 'o3', 'pi', 'p', 'm', max_calls=9,
                          timeout=10, consumer=self._consumer())
        with self.assertRaises(ValueError):
            evaluate.run_case(case, self.root / 'o3', 'pi', 'p', 'm', max_calls=9,
                              timeout=10, consumer=self._consumer())

    def test_non_off_arm_needs_native_receipt_proof(self):
        # The mock returns a passing answer but writes no receipt, so the
        # non-off arm must be rejected as infra rather than counted as a win.
        out = self.root / 'o8'
        evaluate.run_case(make_case(), out, 'pi', 'p', 'm', max_calls=9,
                          timeout=10, consumer=self._consumer())
        current = json.loads((out / '00-current.json').read_text())
        self.assertEqual(current['status'], 'infra_error')
        self.assertIn('receipt', current['reason'])
        off = json.loads((out / '00-off.json').read_text())
        self.assertEqual(off['status'], 'completed')

    def test_partial_progress_rows_preserved(self):
        case = make_case()
        out = self.root / 'o4'
        evaluate.run_case(case, out, 'pi', 'p', 'm', max_calls=9, timeout=10,
                          consumer=self._consumer(fail_arms=('current',)))
        self.assertTrue((out / 'report.json').exists())
        self.assertTrue((out / 'manifest.json').exists())
        self.assertTrue((out / '00-off.json').exists())

    def test_case_json_not_in_workspace_and_prompt_only(self):
        seen = {}

        def consumer(root, case, task, arm, pi, provider, model, timeout):
            seen[arm] = {'task_keys': set(task), 'workspace': [p.name for p in
                                                               (Path(root) / 'workspace').iterdir()] if (Path(root) / 'workspace').is_dir() else []}
            return {'answer': '{}', 'usage': {}, 'seconds': 0.0,
                    'provider': 'fake', 'model': 'fake', 'cost': None}

        case = make_case()
        evaluate.run_case(case, self.root / 'o5', 'pi', 'p', 'm', max_calls=9,
                          timeout=10, consumer=consumer)
        self.assertEqual(set(seen), {'off', 'current'})
        for arm, info in seen.items():
            self.assertEqual(info['task_keys'], {'id', 'prompt'})
            self.assertNotIn('case.json', info['workspace'])

    def test_report_declares_non_blind_synthetic_scope(self):
        case = make_case()
        report = evaluate.run_case(case, self.root / 'o7', 'pi', 'p', 'm',
                                   max_calls=9, timeout=10, consumer=self._consumer())
        self.assertFalse(report['consumer_blind'])
        self.assertIn('non-blind', report['scope'])
        self.assertTrue(any('do not prove broad production benefit' in item
                            for item in report['limitations']))
        # A mock consumer must be marked as a synthetic harness.
        self.assertFalse(report['native_consumer'])
        manifest = json.loads((self.root / 'o7' / 'manifest.json').read_text())
        self.assertEqual(manifest['case_hash'], evaluate.digest(case))
        self.assertFalse(manifest['native_consumer'])
        self.assertTrue(manifest['code_hash'])

    def test_arm_order_is_deterministic_balanced(self):
        order = []

        def consumer(root, case, task, arm, pi, provider, model, timeout):
            order.append((task['id'], arm))
            return {'answer': '{}', 'usage': {}, 'seconds': 0.0,
                    'provider': 'fake', 'model': 'fake', 'cost': None}

        evaluate.run_case(make_case(), self.root / 'o6', 'pi', 'p', 'm',
                          max_calls=9, timeout=10, consumer=consumer)
        self.assertEqual(order, [('t1', 'off'), ('t1', 'current'),
                                 ('t2', 'current'), ('t2', 'off')])


# --------------------------------------------------------------------- compare

class TestCompare(unittest.TestCase):
    def _row(self, task, arm, passed, status='completed', identity=None, usage=None):
        return {'task': task, 'arm': arm, 'status': status,
                'assessment': {'pass': passed},
                'identity': identity or {'provider': 'p', 'model': 'm'},
                'usage': usage if usage is not None else {'input': 1, 'output': 1}}

    def test_equal_task_weighting_and_effective_denominator(self):
        rows = [self._row('t1', 'off', False), self._row('t1', 'current', True),
                self._row('t2', 'off', True), self._row('t2', 'current', True),
                self._row('t3', 'off', True), self._row('t3', 'current', False, 'infra_error')]
        report = evaluate.compare(rows, ['off', 'current'])
        self.assertEqual(report['total_tasks'], 3)
        self.assertEqual(report['effective_pairs'], 2)
        self.assertEqual(report['arms']['off']['effective_tasks'], 2)
        self.assertEqual(report['arms']['off']['passed'], 1)
        self.assertEqual(report['arms']['off']['pass_rate'], 0.5)
        self.assertEqual(report['off_to_current']['improved'], 1)
        self.assertEqual(report['off_to_current']['regressed'], 0)

    def test_unknown_usage_counted(self):
        rows = [self._row('t1', 'off', True, usage={}),
                self._row('t1', 'current', True)]
        report = evaluate.compare(rows, ['off', 'current'])
        self.assertEqual(report['unknown_usage_rows'], 1)

    def test_identity_mismatch_invalidates_comparison(self):
        rows = [self._row('t1', 'off', True, identity={'provider': 'p', 'model': 'a'}),
                self._row('t1', 'current', True, identity={'provider': 'p', 'model': 'b'})]
        report = evaluate.compare(rows, ['off', 'current'])
        self.assertFalse(report['identity_consistent'])
        self.assertFalse(report['comparison_valid'])

    def test_identity_match_keeps_comparison_valid(self):
        rows = [self._row('t1', 'off', True), self._row('t1', 'current', True)]
        report = evaluate.compare(rows, ['off', 'current'])
        self.assertTrue(report['comparison_valid'])


# --------------------------------------------------------------- code hashing

class TestCodeHash(TempCase):
    def test_code_hash_is_stable_and_hex(self):
        first = evaluate.code_hash()
        self.assertEqual(first, evaluate.code_hash())
        self.assertRegex(first, r'^[0-9a-f]{64}$')


if __name__ == '__main__':
    unittest.main()
