"""A parked batch must say *why* its quote failed, without copying the quote.

Four batches stalled on 2026-09-12 with the same opaque message, and it was
impossible to tell "the model paraphrased" from "the text differs only in
whitespace". The error now carries a bounded, content-free classification.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tongle.engine import Engine
from tongle.evolution import Evolution


class QuoteDiagnosisTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.e = Engine(self.root / "state")
        self.e.init(self.root / "vault")
        self.addCleanup(self.e.store.close)
        (self.e.home / "config.json").write_text(json.dumps(
            {"knowledge_mode": "evolving", "model_command": ["synthetic-stub"]}))
        self.ev = Evolution(self.e)
        rec = self.e.store.capture("project", "session", "user", "one", "原文含有 甲 乙 丙。")
        self.sid = rec["source_id"]

    def _reject(self, quote):
        with self.assertRaises(ValueError) as ctx:
            self.ev.cards._validate_evidence(
                [{"source_id": self.sid, "version": 1, "quote": quote}], "project")
        return str(ctx.exception)

    def test_exact_quote_passes(self):
        self.ev.cards._validate_evidence(
            [{"source_id": self.sid, "version": 1, "quote": "原文含有 甲 乙 丙。"}], "project")

    def test_whitespace_only_difference_is_classified(self):
        message = self._reject("原文含有甲乙丙。")
        self.assertIn("exact substring", message)
        self.assertIn("equal ignoring whitespace=whitespace-only", message)

    def test_unrelated_quote_is_classified_without_copying_content(self):
        message = self._reject("完全不同的句子。")
        self.assertIn("equal ignoring whitespace=no", message)
        for leaked in ("完全不同的句子", "甲", "乙", "原文含有"):
            self.assertNotIn(leaked, message)


if __name__ == "__main__":
    unittest.main()
