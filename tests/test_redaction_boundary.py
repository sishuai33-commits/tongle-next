"""Capture-boundary redaction: precise credential shapes, audited, no word damage.

Pins two properties that an earlier revision got wrong:
  1. real credential literals are stripped, and a $VAR reference is not a secret;
  2. ordinary prose of this domain (token counts, digests, timestamps, the words
     token/secret/password used as nouns) is never rewritten.
Everything here is synthetic; no live credential is used.
"""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tongle.engine import Engine
from tongle.store import _redact

SK = "sk-" + "abcdefghijklmnopqrstuvwx"
AWS = "AKIA" + "ABCDEFGHIJKLMNOP"
GITHUB = "ghp_" + "a" * 40
BEARER = "Bearer AbCdEf0123456789AbCdEf0123456789"
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.dQ8WlPqZ3sT0vXy1aBcDeF"
VOLC = "AbCd1234EfGh5678IjKl9012MnOp"
TOKEN = "token"
SECRET = "secret"
PASSWORD = "password"


class RedactionRuleTests(unittest.TestCase):
    def test_real_credential_literals_are_stripped(self):
        for label, text, rule in [
            ("sk", "key is " + SK, "sk_key"),
            ("aws", "see " + AWS + " here", "aws_akia"),
            ("github", "use " + GITHUB + " now", "github_token"),
            ("bearer", "Authorization: " + BEARER, "bearer_literal"),
            ("jwt", "jwt " + JWT, "jwt"),
            ("key=value", "export VOLC_HS_KEY=" + VOLC, "keyed_literal"),
            ("suffix key", "DEEPSEEK_API_KEY=abcdef1234567890ABCDE", "keyed_literal"),
        ]:
            with self.subTest(label):
                out, hits = _redact(text)
                self.assertNotEqual(out, text)
                self.assertIn("[REDACTED]", out)
                self.assertEqual(hits, {rule: 1})

    def test_variable_references_are_not_secrets(self):
        for text in (
            "Authorization: Bearer $ANTHROPIC_AUTH_" + TOKEN.upper(),
            "apiKey: $DEEPSEEK_API_KEY -> !cat ~/.pi/agent/secrets/deepseek.key",
            "export TOKEN=${MY_" + TOKEN.upper() + "}",
        ):
            with self.subTest(text[:30]):
                out, hits = _redact(text)
                self.assertEqual(out, text)
                self.assertEqual(hits, {})

    def test_ordinary_domain_prose_is_never_rewritten(self):
        for text in (
            TOKEN + ": 该模型按 " + TOKEN + " 计费",
            "本次调用 " + TOKEN + "=128000，成本约 0.3 元",
            SECRET + ": 这个词在文档里很常见",
            PASSWORD + ": 不要写在配置文件里",
            "body_sha256: " + "a1b2c3d4" * 8,
            "created_key=2026-09-12T15:31:30.123456",
            "card_id=7e25d2d58b724780a8918dd48fbd85ef",
            "把密钥放到环境变量里，不要写进仓库",
        ):
            with self.subTest(text[:30]):
                out, hits = _redact(text)
                self.assertEqual(out, text)
                self.assertEqual(hits, {})

    def test_redaction_is_deterministic_for_dedup(self):
        self.assertEqual(_redact("k " + SK), _redact("k " + SK))


class RedactionAuditTests(unittest.TestCase):
    """The ledger must record that a version's text was rewritten."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.engine = Engine(self.root / "state")
        self.engine.init(self.root / "vault")
        self.addCleanup(self.engine.store.close)

    def test_audit_row_recorded_without_the_removed_value(self):
        rec = self.engine.store.capture("p", "s", "user", "e1", "key " + SK)
        row = self.engine.db.execute(
            "SELECT source_id, version, role, rules, hits FROM redactions"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["source_id"], rec["source_id"])
        self.assertEqual((row["version"], row["role"]), (1, "user"))
        self.assertEqual(row["rules"], "sk_key")
        self.assertEqual(row["hits"], 1)
        stored = self.engine.db.execute(
            "SELECT text FROM versions WHERE source_id=?", (rec["source_id"],)
        ).fetchone()["text"]
        self.assertNotIn(SK, stored)
        self.assertIn("[REDACTED]", stored)

    def test_clean_capture_writes_no_audit_row(self):
        self.engine.store.capture("p", "s", "user", "e2", "普通原话，无凭证")
        self.assertEqual(
            self.engine.db.execute("SELECT COUNT(*) AS n FROM redactions").fetchone()["n"], 0
        )

    def test_redaction_applies_only_to_conversation_capture(self):
        # Document ingest deliberately keeps its own bytes: the file on disk is
        # the original and the ledger stores it verbatim.
        doc = self.root / "doc.md"
        doc.write_text("配置示例：api_key=" + SK, encoding="utf8")
        rec = self.engine.store.ingest("p", str(doc))
        text = self.engine.db.execute(
            "SELECT text FROM versions WHERE source_id=?", (rec["source_id"],)
        ).fetchone()["text"]
        self.assertIn(SK, text)


if __name__ == "__main__":
    unittest.main()
