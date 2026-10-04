import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tongle import guide
from tongle import guide_actions as A


def quote_path(p):
    """Quote a path for the guide input parser on the current platform."""
    text = str(p)
    if os.name == "nt":
        return '"' + text.replace('"', '""') + '"'
    return shlex.quote(text)


class GuideTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.home = self.root / "home"
        self.vault = self.root / "vault"

    def tearDown(self):
        self._tmp.cleanup()

    def make_inputs(self, answers):
        it = iter(answers)

        def input_fn(prompt):
            try:
                return next(it)
            except StopIteration:
                raise EOFError
        return input_fn

    def run_guide(self, answers, secrets=None, home=None, vault=None):
        out = io.StringIO()
        secrets = secrets or []
        sec_it = iter(secrets)

        def secret_fn(prompt):
            try:
                return next(sec_it)
            except StopIteration:
                return ""

        guide.run(
            home=home or self.home,
            vault=vault or self.vault,
            input_fn=self.make_inputs(answers),
            secret_fn=secret_fn,
            output=out,
        )
        return out.getvalue()


class TestFreshFlow(GuideTestBase):
    def test_01_offline_fresh_flow(self):
        src = self.root / "notes.txt"
        src.write_text("hello world data", encoding="utf8")
        quoted = quote_path(src)
        out = self.run_guide(["写一份总结", "口语化", quoted, "0", "y", "0"])
        self.assertTrue((self.home / "guide.json").exists(), out)
        self.assertTrue((self.vault).exists(), out)
        self.assertTrue((self.home / "product.json").exists(), out)
        self.assertIn("尚未生成卡片", out)
        state = A.load(self.home)
        self.assertFalse(state["completed"])
        self.assertNotIn("model_command", out.lower())

    def test_02_cancel_final_no_files(self):
        out = self.run_guide(["目标", "", "", "0", "n"])
        self.assertFalse(self.home.exists() and any(self.home.iterdir()))
        self.assertFalse(self.vault.exists())
        self.assertIn("已取消", out)

    def test_03_eof_before_consent_no_files(self):
        out = self.run_guide(["目标", "", "", "0"])
        self.assertFalse(self.home.exists())
        self.assertFalse(self.vault.exists())


class TestNonInteractive(GuideTestBase):
    def test_04_no_tty_no_callbacks(self):
        out = io.StringIO()
        old_stdin = sys.stdin
        try:
            sys.stdin = io.StringIO("")
            guide.run(home=self.home, vault=self.vault, output=out)
        finally:
            sys.stdin = old_stdin
        text = out.getvalue()
        expected_entry = "开始使用.cmd" if os.name == "nt" else "开始使用.command"
        self.assertIn(expected_entry, text)
        self.assertFalse(self.home.exists())


class TestPaths(GuideTestBase):
    def _write(self, name, content="data"):
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf8")
        return p

    def test_05_paths_unicode_spaces_quotes(self):
        p = self._write("我的 资料 说'明.txt")
        raw = quote_path(p)
        paths = A.parse_files(raw)
        self.assertEqual(len(paths), 1)
        self.assertEqual(paths[0], p.resolve())

    def test_05b_quote_path_platform_behavior(self):
        p = self._write("plain file.txt")
        quoted = quote_path(p)
        if os.name == "nt":
            self.assertEqual(quoted, '"' + str(p) + '"')
        else:
            self.assertEqual(quoted, shlex.quote(str(p)))
        self.assertEqual(A.parse_files(quoted), [p.resolve()])

    def test_05c_singlequote_literal_on_windows(self):
        if os.name != "nt":
            self.skipTest("single-quote literal filenames are Windows-specific")
        p = self._write("a'b.txt")
        self.assertEqual(quote_path(p), '"' + str(p) + '"')
        self.assertEqual(A.parse_files(quote_path(p)), [p.resolve()])

    def test_06_duplicate_file_once(self):
        p = self._write("a.txt")
        raw = str(p) + " " + str(p)
        paths = A.parse_files(raw)
        self.assertEqual(len(paths), 1)

    def test_07_unsupported_missing_symlink(self):
        missing = self.root / "nope.txt"
        with self.assertRaises(Exception):
            A.parse_files(str(missing))
        link = self.root / "link.txt"
        target = self._write("real.txt")
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("platform denies symlink creation")
        with self.assertRaises(Exception):
            A.parse_files(str(link))

    def test_08_picker_retry_to_valid(self):
        p = self._write("good.txt")
        out = self.run_guide([
            "目标", "", "this_file_does_not_exist_zzz.txt", str(p), "0", "y", "0",
        ])
        self.assertTrue((self.home / "guide.json").exists(), out)


class TestReentry(GuideTestBase):
    def _fresh_offline(self):
        self.run_guide(["目标", "", "", "0", "y", "0"])

    def test_09_reentry_preserves_bytes_and_no_recreate(self):
        self._fresh_offline()
        state = A.load(self.home)
        snapshot = {}
        for p in self.home.rglob("*"):
            if p.is_file() and p.name != "guide.lock":
                snapshot[p] = p.read_bytes()

        created = []
        orig_create = A.create

        def spy_create(*a, **k):
            created.append(a)
            return orig_create(*a, **k)

        drafts = []
        orig_draft = A.draft

        def spy_draft(*a, **k):
            drafts.append(a)
            return orig_draft(*a, **k)

        A.create = spy_create
        A.draft = spy_draft
        try:
            self.run_guide(["0"])
            self.run_guide(["0"])
        finally:
            A.create = orig_create
            A.draft = orig_draft

        self.assertEqual(created, [])
        self.assertEqual(drafts, [])
        self.assertTrue(state["completed"] is False)
        for p, data in snapshot.items():
            if p.name == "guide.lock":
                continue
            self.assertEqual(p.read_bytes(), data, p)


class TestConfiguredFlow(GuideTestBase):
    def setUp(self):
        super().setUp()
        os.environ["TONGLE_GUIDE_API_KEY"] = "synthetic-not-real"

    def tearDown(self):
        os.environ.pop("TONGLE_GUIDE_API_KEY", None)
        super().tearDown()

    def test_10_configured_fresh_choice1(self):
        calls = {"maintain": 0, "draft": 0, "start": 0}
        draft_path = self.vault / "随笔" / "draft.md"

        orig_maintain = A.maintain
        orig_draft = A.draft
        orig_start = A.start
        orig_feedback = A.feedback

        def fake_maintain(home):
            calls["maintain"] += 1
            return {"ok": True}

        def fake_draft(home, goal):
            calls["draft"] += 1
            st = A.load(home)
            st["completed"] = True
            st["last_draft"] = str(draft_path)
            A.save(home, st)
            return {"text": "草稿内容", "path": str(draft_path),
                    "session": "sess-1", "cards": 1}

        def fake_start(home):
            calls["start"] += 1
            return True

        feedback_calls = []

        def fake_feedback(home, session, choice, correction=""):
            feedback_calls.append((choice, correction))
            return {"ok": True}

        A.maintain = fake_maintain
        A.draft = fake_draft
        A.start = fake_start
        A.feedback = fake_feedback
        try:
            out = self.run_guide(
                ["我的目标", "偏好A", "", "1", "y", "0"],
                secrets=["secret-value"],
            )
        finally:
            A.maintain = orig_maintain
            A.draft = orig_draft
            A.start = orig_start
            A.feedback = orig_feedback

        self.assertTrue((self.home / "guide.json").exists(), out)
        info = A.model_info(self.home)
        self.assertTrue(info["configured"])
        product_doc = json.loads((self.home / "product.json").read_text(encoding="utf8"))
        self.assertNotIn("secret-value", json.dumps(product_doc))
        state = A.load(self.home)
        self.assertTrue(state["completed"])
        self.assertEqual(calls["draft"], 1)
        self.assertIn("自动整理已启动", out)

    def test_11_missing_secret_goes_offline(self):
        draft_calls = []
        orig_draft = A.draft

        def spy_draft(*a, **k):
            draft_calls.append(a)
            return {"text": "", "path": None}

        A.draft = spy_draft
        try:
            out = self.run_guide(
                ["目标", "", "", "1", "y", "0"],
                secrets=[""],
            )
        finally:
            A.draft = orig_draft
        self.assertFalse((self.home / "guide.json").exists() if not self.home.exists()
                         else False)
        self.assertEqual(draft_calls, [])
        info = A.model_info(self.home) if (self.home / "product.json").exists() else None
        if info:
            self.assertFalse(info["configured"])


class TestBudget(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.home = self.root / "home"

    def tearDown(self):
        self._tmp.cleanup()

    def test_12_budget_preserved(self):
        from tongle import product
        self.home.mkdir()
        product._require = getattr(product, "_require_initialized", None)
        # initialize via cmd_setup
        ns = type("NS", (), {})()
        ns.home = self.home
        ns.vault = self.root / "vault"
        ns.project = "我的工作"
        ns.style = "product"
        ns.provider = None
        ns.endpoint = None
        ns.model = None
        ns.key_env = None
        ns.write = True
        product.cmd_setup(ns)

        custom = {
            "knowledge_mode": "quiet",
            "knowledge_quiet_seconds": 42,
            "knowledge_daily_call_budget": 7,
        }
        cfg_path = self.home / "config.json"
        existing = json.loads(cfg_path.read_text(encoding="utf8")) if cfg_path.exists() else {}
        existing.update(custom)
        cfg_path.write_text(json.dumps(existing), encoding="utf8")

        os.environ["TONGLE_GUIDE_API_KEY"] = "synthetic"
        try:
            A.configure(self.home, "deepseek",
                        "https://api.deepseek.com/chat/completions",
                        "deepseek-flash", "TONGLE_GUIDE_API_KEY")
        finally:
            os.environ.pop("TONGLE_GUIDE_API_KEY", None)

        cfg = json.loads(cfg_path.read_text(encoding="utf8"))
        for k, v in custom.items():
            self.assertEqual(cfg.get(k), v, k)
        self.assertNotIn("synthetic", json.dumps(cfg))
        for backup in self.home.glob("config-backup-*.json"):
            data = json.loads(backup.read_text(encoding="utf8"))
            self.assertNotIn("synthetic", json.dumps(data))


class TestValidation(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.home = self.root / "home"
        self.vault = self.root / "vault"

    def tearDown(self):
        self._tmp.cleanup()

    def _make_state(self):
        self.home.mkdir()
        state = {
            "schema": 1, "project": "我的工作", "goal": "g",
            "preference": "", "vault": str(self.vault),
            "completed": False, "last_draft": None,
        }
        return state

    def test_13_load_mismatched_vault(self):
        state = self._make_state()
        state["vault"] = str((self.root / "other").resolve())
        (self.home / "guide.json").write_text(json.dumps(state), encoding="utf8")
        with self.assertRaises(Exception):
            A.load(self.home)

    def test_14_load_symlink_guide(self):
        self.home.mkdir()
        real = self.root / "real-guide.json"
        real.write_text(json.dumps({"schema": 1}), encoding="utf8")
        try:
            (self.home / "guide.json").symlink_to(real)
        except (OSError, NotImplementedError):
            self.skipTest("platform denies symlink creation")
        with self.assertRaises(Exception):
            A.load(self.home)


class TestDraft(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.home = self.root / "home"
        self.vault = self.root / "vault"

    def tearDown(self):
        self._tmp.cleanup()

    def test_15_draft_passes_budget_to_call(self):
        from tongle import product
        self.home.mkdir()
        self.vault.mkdir()
        ns = type("NS", (), {})()
        ns.home = self.home
        ns.vault = self.vault
        ns.project = "我的工作"
        ns.style = "product"
        ns.provider = None
        ns.endpoint = None
        ns.model = None
        ns.key_env = None
        ns.write = True
        product.cmd_setup(ns)

        cfg = {
            "knowledge_daily_call_budget": 3,
            "knowledge_daily_token_budget": 12345,
            "model_command": "fake",
            "model_name": "fake",
            "model_timeout_seconds": 30,
        }
        (self.home / "config.json").write_text(json.dumps(cfg), encoding="utf8")

        import tongle.evolution as ev_mod
        from tongle.evolution import BudgetWait

        captured = {}

        class FakeEvolution:
            def __init__(self, engine):
                self._engine = engine

            def captured(self, *a, **k):
                return None

            def context(self, project, goal, session=None):
                return {
                    "results": [{
                        "id": "c1", "title": "T", "text": "body",
                        "complete": True, "source_version": 1,
                    }],
                    "pending_batches": 0, "notices": [],
                    "receipt_id": None,
                }

            def _call(self, session, kind, payload, cfg):
                captured["cfg"] = cfg
                raise BudgetWait("budget")

        orig_ev = A._load_evolution
        A._load_evolution = lambda engine: FakeEvolution(engine)
        try:
            from tongle import guide_actions
            guide_actions.draft  # noqa
            with self.assertRaises(Exception):
                A.draft(self.home, "目标")
        finally:
            A._load_evolution = orig_ev

    def test_16_no_complete_cards_no_call(self):
        from tongle import product
        self.home.mkdir()
        self.vault.mkdir()
        ns = type("NS", (), {})()
        ns.home = self.home
        ns.vault = self.vault
        ns.project = "我的工作"
        ns.style = "product"
        ns.provider = None
        ns.endpoint = None
        ns.model = None
        ns.key_env = None
        ns.write = True
        product.cmd_setup(ns)

        calls = {"_call": 0}

        class FakeEvolution:
            def __init__(self, engine):
                pass

            def captured(self, *a, **k):
                return None

            def context(self, *a, **k):
                return {"results": [], "pending_batches": 0, "notices": []}

            def _call(self, *a, **k):
                calls["_call"] += 1
                return {"markdown": "x"}

        orig_ev = A._load_evolution
        A._load_evolution = lambda engine: FakeEvolution(engine)
        try:
            with self.assertRaises(Exception):
                A.draft(self.home, "目标")
        finally:
            A._load_evolution = orig_ev
        self.assertEqual(calls["_call"], 0)


class TestFeedback(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.home = self.root / "home"
        self.vault = self.root / "vault"

    def tearDown(self):
        self._tmp.cleanup()

    def _setup(self):
        from tongle import product
        self.home.mkdir()
        self.vault.mkdir()
        ns = type("NS", (), {})()
        ns.home = self.home
        ns.vault = self.vault
        ns.project = "我的工作"
        ns.style = "product"
        ns.provider = None
        ns.endpoint = None
        ns.model = None
        ns.key_env = None
        ns.write = True
        product.cmd_setup(ns)

    def test_17_feedback_zero_no_write(self):
        self._setup()
        calls = []
        orig = A._load_engine
        A._load_engine = lambda home: calls.append(home)
        try:
            result = A.feedback(self.home, "sess", "0")
        finally:
            A._load_engine = orig
        self.assertEqual(result, {"ok": True})
        self.assertEqual(calls, [])

    def test_18_feedback_two_stores_exact_text(self):
        import sqlite3
        from tongle import product
        from tongle.engine import Engine
        from tongle.evolution import Evolution

        self.home.mkdir()
        self.vault.mkdir()
        A.create(self.home, self.vault, "目标", "", [])
        engine = Engine(self.home)
        ev = Evolution(engine)
        session = 'guide:test-feedback'
        original = '# 草稿\n\n  请改成要点  \n结束'
        receipt = engine.store.capture("我的工作", session, 'assistant',
                                      session + ':assistant', original)
        ev.captured(receipt['source_id'], 'assistant', session)
        engine.store.close()

        result = A.feedback(self.home, session, '2', '  请改成要点  ')
        self.assertTrue(result["ok"])

        engine = Engine(self.home)
        try:
            rows = engine.db.execute(
                "SELECT id, role FROM sources WHERE session=?",
                (session,)).fetchall()
            user_rows = [r for r in rows if r['role'] == 'user']
            assistant_rows = [r for r in rows if r['role'] == 'assistant']
            self.assertEqual(len(user_rows), 1)
            self.assertEqual(len(assistant_rows), 1)
            user_id = user_rows[0]['id']
            assistant_id = assistant_rows[0]['id']
            user_text = engine.store.source(user_id)['text']
            assistant_text = engine.store.source(assistant_id)['text']
        finally:
            engine.store.close()
        self.assertEqual(user_text, '  请改成要点  ')
        self.assertEqual(assistant_text, original)

        with self.assertRaises(Exception):
            A.feedback(self.home, 'not-a-guide-session', '1')

    def test_18b_load_fresh_correct_db(self):
        from tongle import product
        A.create(self.home, self.vault, "目标", "偏好", [])
        state = A.load(self.home)
        self.assertEqual(state['goal'], "目标")
        self.assertEqual(state['vault'], str(self.vault.resolve()))
        self.assertTrue((self.home / 'state.sqlite3').exists())
        self.assertFalse((self.vault / '.tongle').exists())


class TestMaintainMissing(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.home = self.root / "home"
        self.vault = self.root / "vault"

    def tearDown(self):
        self._tmp.cleanup()

    def test_18c_maintain_missing_process_false(self):
        A.create(self.home, self.vault, "目标", "", [])
        orig = A._product
        import tongle.product as product_mod

        orig_once = product_mod.cmd_once
        orig_status = product_mod.cmd_status

        product_mod.cmd_once = lambda ns: {'process': None}
        product_mod.cmd_status = lambda ns: {
            'maintenance': {'needs_attention': False},
            'work': {'knowledge': {'pending': 3}},
        }
        try:
            res = A.maintain(self.home)
        finally:
            product_mod.cmd_once = orig_once
            product_mod.cmd_status = orig_status
        self.assertFalse(res['ok'])
        self.assertIn('后台', res['reason'])

    def test_18d_maintain_skipped_idle_true(self):
        A.create(self.home, self.vault, "目标", "", [])
        import tongle.product as product_mod

        orig_once = product_mod.cmd_once
        orig_status = product_mod.cmd_status

        product_mod.cmd_once = lambda ns: {'process': None, 'skipped': True}
        product_mod.cmd_status = lambda ns: {
            'maintenance': {'needs_attention': False},
            'work': {'knowledge': {
                'pending': 0, 'running': 0, 'retry': 0,
                'budget_wait': 0, 'blocked': 0, 'failed': 0,
            }},
        }
        try:
            res = A.maintain(self.home)
        finally:
            product_mod.cmd_once = orig_once
            product_mod.cmd_status = orig_status
        self.assertTrue(res['ok'])


class TestMessage(unittest.TestCase):
    def test_19_message_no_secret_leak(self):
        secret = "SYNTHETIC-SECRET-TOKEN"
        for exc in (RuntimeError("network failure " + secret),
                    Exception("auth error " + secret)):
            msg = A.message(exc)
            self.assertNotIn(secret, msg)
            self.assertTrue(any("\u4e00" <= ch <= "\u9fff" for ch in msg), msg)


class TestRunHomeResolution(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()

    def tearDown(self):
        self._tmp.cleanup()

    def test_18e_run_default_home_calls_resolve_with_none(self):
        from tongle import product as product_mod
        seen = []
        orig = product_mod.resolve_home

        def spy(value=None):
            seen.append(value)
            return str(self.root / "home")

        product_mod.resolve_home = spy
        out = io.StringIO()
        old_stdin = sys.stdin
        try:
            sys.stdin = io.StringIO("")
            guide.run(input_fn=lambda p: (_ for _ in ()).throw(EOFError()),
                      output=out)
        finally:
            sys.stdin = old_stdin
            product_mod.resolve_home = orig
        self.assertTrue(seen)
        self.assertIsNone(seen[0])

    def test_18f_bad_guide_path_nontty_no_json(self):
        out = io.StringIO()
        old_stdin = sys.stdin
        try:
            sys.stdin = io.StringIO("")
            guide.run(home=self.root / "nope", vault=self.root / "v", output=out)
        finally:
            sys.stdin = old_stdin
        text = out.getvalue()
        self.assertNotIn('"schema"', text)


class TestFirstFlow(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.home = self.root / "home"
        self.vault = self.root / "vault"

    def tearDown(self):
        self._tmp.cleanup()

    def test_18g_first_flow_start_failure_no_false_claim(self):
        os.environ["TONGLE_GUIDE_API_KEY"] = "synthetic"
        try:
            state = A.create(self.home, self.vault, "目标", "", [])
        finally:
            os.environ.pop("TONGLE_GUIDE_API_KEY", None)
        state["completed"] = True
        A.save(self.home, state)

        out = io.StringIO()
        outfn = guide._out(out)
        orig_maintain = A.maintain
        orig_draft = A.draft
        orig_start = A.start
        orig_info = A.model_info
        A.maintain = lambda home: {'ok': True}
        A.draft = lambda home, goal: {'text': 'x', 'path': None}
        A.start = lambda home: False
        A.model_info = lambda home: {'configured': True, 'key_present': True}
        try:
            guide._first_flow(self.home, state, lambda p: "", None, outfn)
        finally:
            A.maintain = orig_maintain
            A.draft = orig_draft
            A.start = orig_start
            A.model_info = orig_info
        self.assertNotIn("自动整理已启动", out.getvalue())


class TestCLI(unittest.TestCase):
    def test_20_cli_help_and_noargs(self):
        cwd = Path(__file__).resolve().parents[1]
        help_proc = subprocess.run(
            [sys.executable, "-m", "tongle", "--help"],
            cwd=str(cwd), capture_output=True, text=True,
        )
        combined = (help_proc.stdout + help_proc.stderr)
        self.assertTrue(combined.strip(), combined)

        noargs = subprocess.run(
            [sys.executable, "-m", "tongle"],
            cwd=str(cwd), capture_output=True, text=True, input="",
        )
        text = noargs.stdout + noargs.stderr
        self.assertNotIn('"schema"', text)


if __name__ == "__main__":
    unittest.main()