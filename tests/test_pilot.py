"""DS-authored boundary tests, integrated by ROOT against actual core APIs.
Synthetic tests only; no skip-on-failure or external model calls.
"""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from tongle import pilot as _pilot
from tongle import evaluate as _evaluate
import sqlite3
import hashlib
from tongle.engine import Engine
from tongle.evolution import Evolution
from unittest.mock import patch


# --------------------------------------------------------------------------- #
# Synthetic fixture data (mechanical, non-production)
# --------------------------------------------------------------------------- #

SRC_USER = (
    "需求：异步任务必须先写入持久化队列后才向调用方确认接收，失败可以重试，"
    "并且同一队列内保持处理顺序，不允许重复产生副作用。"
)

SRC_ASSISTANT = (
    "建议：消费端以幂等键去重，失败时指数退避重试，配合持久化队列实现至少一次"
    "投递语义，但处理顺序需要独立保证；幂等机制只能避免同一业务请求的重复副作用。"
)

# >= 50 characters, contains the facts asserted in sources/evidence.
BODY = (
    "异步任务采用先持久化入队、再向调用方确认接收的框架：消费者依据幂等键去重，"
    "失败时以指数退避重试；同一队列的处理顺序需要另外设计保障，"
    "并避免重复产生副作用。"
)

CANDIDATE_REASON = (
    "把可靠投递需求与幂等消费知识整合为一个有依据的框架候选，覆盖顺序与重试。"
)


def _digest(obj):
    return _evaluate.digest(obj)


def _framework_candidate():
    return {
        "key": "cand1",
        "kind": "framework",
        "topic": "delivery",
        "title": "至少一次投递框架",
        "body": BODY,
        "evidence": [{"source": "s2", "quote": SRC_ASSISTANT}],
        "basis": ["c1", "c2"],
        "reason": CANDIDATE_REASON,
    }


def _valid_case():
    """Build a minimal, contract-conformant synthetic case."""
    candidate = _framework_candidate()
    return {
        "schema": 1,
        "id": "pilot-case-001",
        "version": "v1",
        "project": "simple-project",
        "material": "synthetic",
        "sources": [
            {"key": "s1", "role": "user", "text": SRC_USER},
            {"key": "s2", "role": "assistant", "text": SRC_ASSISTANT},
        ],
        "cards": [
            {
                "key": "c1",
                "kind": "memory",
                "topic": "requirements",
                "title": "可靠投递需求",
                "body": SRC_USER,
                "evidence": [{"source": "s1", "quote": SRC_USER}],
                "basis": [],
            },
            {
                "key": "c2",
                "kind": "knowledge",
                "topic": "delivery",
                "title": "幂等消费知识",
                "body": SRC_ASSISTANT,
                "evidence": [{"source": "s2", "quote": SRC_ASSISTANT}],
                "basis": ["c1"],
            },
        ],
        "candidate": candidate,
        "review": {
            "approved": True,
            "findings": [],
            "proposal_hash": _digest(candidate),
            "reviewer": "root-reviewer",
        },
        "tasks": [
            {
                "id": "t1",
                "prompt": "失败重试时如何保持顺序且不重复？",
                "expect": {"slot_a": "persist_first"},
            },
            {
                "id": "t2",
                "prompt": "消费端如何安全去重？",
                "expect": {"slot_b": "idempotent"},
            },
        ],
    }


def _collect_key_hits(obj, target, hits):
    """Recursively record occurrences of dict key ``target`` inside ``obj``."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == target:
                hits.append(k)
            _collect_key_hits(v, target, hits)
    elif isinstance(obj, (list, tuple, set)):
        for item in obj:
            _collect_key_hits(item, target, hits)


class _RecordingConsumer:
    """Spy consumer: records every call, returns a neutral receipt."""

    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return {"ok": True}

    @property
    def call_count(self):
        return len(self.calls)


class _PilotTestBase(unittest.TestCase):
    def setUp(self):
        self.case = _valid_case()

    def _fresh(self):
        return copy.deepcopy(self.case)

    def _reject(self, case, label):
        """Assert the validator refuses ``case`` (boundary/negative path)."""
        with self.subTest(mutation=label):
            with self.assertRaises(ValueError):
                _pilot.validate_pilot_case(case)


# --------------------------------------------------------------------------- #
# validate_pilot_case: bad inputs
# --------------------------------------------------------------------------- #

class PilotValidateTests(_PilotTestBase):

    def test_validate_accepts_valid_case(self):
        result = _pilot.validate_pilot_case(self._fresh())
        self.assertIsInstance(result, dict)
        self.assertEqual(result.get("project"), "simple-project")

    def test_validate_rejects_schema_shape(self):
        c = self._fresh()
        c["schema"] = 2
        self._reject(c, "wrong schema")

        c = self._fresh()
        c["unexpected_top_key"] = True
        self._reject(c, "extra top-level key")

        c = self._fresh()
        del c["material"]
        self._reject(c, "missing required top-level key")

    def test_validate_rejects_non_synthetic_material(self):
        for bad in ("production", "real", "live"):
            c = self._fresh()
            c["material"] = bad
            self._reject(c, "material=%s" % bad)

    def test_validate_rejects_project_path_identifier(self):
        for bad in ("simple-project/../evil", "../etc", "/abs/home", "a/b"):
            c = self._fresh()
            c["project"] = bad
            self._reject(c, "project=%r" % bad)

    def test_validate_rejects_source_role_and_quote(self):
        c = self._fresh()
        c["sources"][0]["role"] = "system"
        self._reject(c, "unknown source role")

        c = self._fresh()
        c["cards"][0]["evidence"][0]["quote"] = "这段引文并不存在于来源正文之中"
        self._reject(c, "non-verbatim quote")

        c = self._fresh()
        c["cards"][0]["evidence"][0]["source"] = "sNOPE"
        self._reject(c, "evidence references missing source")

    def test_validate_rejects_basis_graph(self):
        c = self._fresh()
        c["cards"][1]["basis"] = ["cNOPE"]
        self._reject(c, "unknown basis reference")

        c = self._fresh()
        c["cards"][0]["basis"] = ["c2"]
        c["cards"][1]["basis"] = ["c1"]
        self._reject(c, "cyclic basis")

    def test_validate_rejects_duplicate_keys(self):
        c = self._fresh()
        c["sources"][1]["key"] = "s1"
        self._reject(c, "duplicate source key")

        c = self._fresh()
        c["cards"][1]["key"] = "c1"
        self._reject(c, "duplicate card key")

    def test_validate_rejects_candidate_contract(self):
        # Key colliding with a base card.
        c = self._fresh()
        c["candidate"]["key"] = "c1"
        c["review"]["proposal_hash"] = _digest(c["candidate"])
        self._reject(c, "candidate key collides with base")

        # Candidate must be a newly created framework object.
        c = self._fresh()
        c["candidate"]["kind"] = "knowledge"
        c["review"]["proposal_hash"] = _digest(c["candidate"])
        self._reject(c, "candidate kind not framework")

        # Candidate basis must be non-empty and only reference base cards.
        c = self._fresh()
        c["candidate"]["basis"] = []
        c["review"]["proposal_hash"] = _digest(c["candidate"])
        self._reject(c, "candidate empty basis")

        c = self._fresh()
        c["candidate"]["basis"] = ["cNOPE"]
        c["review"]["proposal_hash"] = _digest(c["candidate"])
        self._reject(c, "candidate basis references non-base key")

    def test_validate_rejects_review_contract(self):
        c = self._fresh()
        c["review"]["proposal_hash"] = "deadbeef"
        self._reject(c, "review proposal_hash mismatch")

        c = self._fresh()
        c["review"]["approved"] = False
        self._reject(c, "review not approved")

        c = self._fresh()
        c["review"]["reviewer"] = ""
        self._reject(c, "empty reviewer")

        c = self._fresh()
        c["review"]["findings"] = ["something wrong"]
        self._reject(c, "non-empty findings on approved review")

    def test_validate_rejects_task_and_limit_violations(self):
        c = self._fresh()
        c["tasks"][0]["expect"] = {"slot_a": {"nested": True}}
        self._reject(c, "expect value not a JSON scalar")

        c = self._fresh()
        c["tasks"][0]["expect"] = {}
        self._reject(c, "empty expect map")

        c = self._fresh()
        c["tasks"][0]["expect"] = ["persist_first"]
        self._reject(c, "expect not a mapping")

        # Over the task bound (<=6).
        c = self._fresh()
        c["tasks"] = [
            {"id": "x%d" % i, "prompt": "q%d" % i, "expect": {"slot": "v%d" % i}}
            for i in range(7)
        ]
        self._reject(c, "7 tasks exceeds bound")

        # Over the source bound (<=16).
        c = self._fresh()
        extra_sources = [
            {"key": "sx%d" % i, "role": "user", "text": SRC_USER}
            for i in range(16)
        ]
        c["sources"] = c["sources"] + extra_sources
        self._reject(c, "18 sources exceeds bound")


# --------------------------------------------------------------------------- #
# materialize / run_pilot / assess_run / publish_pilot: runtime boundaries
# --------------------------------------------------------------------------- #

def _native_fixture(root, case, arm="candidate"):
    """Fabricated host events + actual core; exercises checks, never proves gain."""
    _pilot.materialize(root, case, arm)
    task = case["tasks"][0]
    answer = json.dumps(task["expect"])
    engine = Engine(root / "home")
    try:
        ev = Evolution(engine)
        context = ev.context(case["project"], task["prompt"], session="synthetic-test")
        receipt_id = context["receipt_id"]
        out = engine.store.capture(case["project"], "synthetic-test", "assistant", "test-output", answer)
        ev.captured(out["source_id"], "assistant", "synthetic-test", receipt_id=receipt_id)
    finally:
        engine.store.close()
    events = [
        {"type": "message_end", "message": {"role": "user", "content": [{"type": "text", "text": task["prompt"]}]}},
        {"type": "message_end", "message": {"role": "custom", "customType": "tongle-next-context", "content": context["rendered_context"], "details": {"receiptId": receipt_id, "promptHash": hashlib.sha256(task["prompt"].encode()).hexdigest()}}},
        {"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": answer}], "provider": "fixture", "model": "fixture", "stopReason": "stop"}},
    ]
    (root / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return task, answer


class PilotRuntimeTests(_PilotTestBase):
    def test_materialize_real_dependencies_and_same_base_facts(self):
        with tempfile.TemporaryDirectory() as td:
            roots = {a: Path(td) / a for a in ("current", "candidate")}
            results = {a: _pilot.materialize(r, self.case, a) for a, r in roots.items()}
            facts = {}
            for arm, result in results.items():
                self.assertTrue(Path(result["home"]).exists())
                self.assertTrue(Path(result["vault"]).exists())
                self.assertEqual(set(result["cards"]), {"c1", "c2"} | ({"cand1"} if arm == "candidate" else set()))
                db = sqlite3.connect(Path(result["home"]) / "state.sqlite3")
                try:
                    rows = db.execute("SELECT id,key,body,version FROM knowledge_cards ORDER BY key").fetchall()
                    by_key = {key: (cid, body, ver) for cid, key, body, ver in rows}
                    facts[arm] = {k: (b, v) for k, (_, b, v) in by_key.items() if k != "cand1"}
                    deps = db.execute("SELECT basis_id,basis_version FROM knowledge_dependencies WHERE card_id=?", (by_key["c2"][0],)).fetchall()
                    self.assertEqual(deps, [(by_key["c1"][0], 1)])
                    if arm == "candidate":
                        deps = db.execute("SELECT basis_id,basis_version FROM knowledge_dependencies WHERE card_id=?", (by_key["cand1"][0],)).fetchall()
                        self.assertEqual(set(deps), {(by_key[k][0], 1) for k in ("c1", "c2")})
                finally:
                    db.close()
            self.assertEqual(facts["current"], facts["candidate"])

    def test_materialize_rejects_unknown_arm_and_existing_output(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                _pilot.materialize(Path(td) / "new", self.case, "both")
            with self.assertRaises((ValueError, FileExistsError)):
                _pilot.materialize(Path(td), self.case, "current")

    def test_run_pilot_rejects_insufficient_budget_before_output_or_consumer(self):
        consumer = _RecordingConsumer()
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "run"
            with self.assertRaises(ValueError):
                _pilot.run_pilot(self.case, out, "unused-pi", "mock", "synthetic-model",
                                 max_calls=1, consumer=consumer)
            self.assertFalse(out.exists())
        self.assertEqual(consumer.call_count, 0)

    def test_mock_payload_hides_expect_and_mock_cannot_publish(self):
        consumer = _RecordingConsumer()
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "run"
            _pilot.run_pilot(self.case, out, "unused-pi", "mock", "synthetic-model", consumer=consumer)
            self.assertEqual(consumer.call_count, 4)
            for args, kwargs in consumer.calls:
                for key in ("expect", "tasks", "sources", "cards"):
                    hits = []
                    _collect_key_hits((args, kwargs), key, hits)
                    self.assertEqual(hits, [])
            review = {"approved": True, "findings": [], "reviewer": "independent", "proof_hash": "0" * 64}
            with self.assertRaises(ValueError):
                _pilot.publish_pilot(self.case, out, review, Path(td) / "publication")
            self.assertFalse((Path(td) / "publication").exists())

    def test_assess_and_publish_refuse_missing_run(self):
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "missing"
            with self.assertRaises((ValueError, FileNotFoundError)):
                _pilot.assess_run(self.case, missing)
            review = {"approved": True, "findings": [], "reviewer": "independent", "proof_hash": "0" * 64}
            with self.assertRaises((ValueError, FileNotFoundError)):
                _pilot.publish_pilot(self.case, missing, review, Path(td) / "out")
            self.assertFalse((Path(td) / "out").exists())


    def test_inspect_accepts_full_delivery_and_rejects_tampered_basis(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "cell"
            task, answer = _native_fixture(root, self.case)
            proof = _pilot.inspect_arm(self.case, root, "candidate", task, answer)
            self.assertTrue(proof["candidate_delivered"])
            db = sqlite3.connect(root / "home/state.sqlite3")
            try:
                db.execute("UPDATE knowledge_dependencies SET basis_version=2")
                db.commit()
            finally:
                db.close()
            with self.assertRaises(ValueError):
                _pilot.inspect_arm(self.case, root, "candidate", task, answer)

    def test_receipt_alone_does_not_prove_delivered_body(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "cell"
            task, answer = _native_fixture(root, self.case)
            path = root / "events.jsonl"
            events = [json.loads(line) for line in path.read_text().splitlines()]
            events[1]["message"]["content"] = "Only a path, no actual framework body"
            path.write_text("\n".join(json.dumps(e) for e in events) + "\n")
            try:
                proof = _pilot.inspect_arm(self.case, root, "candidate", task, answer)
            except ValueError:
                return
            self.assertFalse(proof["candidate_delivered"])

    def test_inspect_rejects_modified_answer_and_source(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "cell"
            task, answer = _native_fixture(root, self.case)
            with self.assertRaises(ValueError):
                _pilot.inspect_arm(self.case, root, "candidate", task, "{}")
            db = sqlite3.connect(root / "home/state.sqlite3")
            try:
                db.execute("UPDATE versions SET text='changed source' WHERE source_id IN (SELECT id FROM sources WHERE session='fixture')")
                db.commit()
            finally:
                db.close()
            with self.assertRaises(ValueError):
                _pilot.inspect_arm(self.case, root, "candidate", task, answer)


class PilotGateTests(_PilotTestBase):
    def _run(self, out, gain=False, failure=False):
        """Deliberately fabricated native records for gate mechanics only."""
        tasks = {t["id"]: t for t in self.case["tasks"]}

        def fake(root, case_input, task_input, arm, pi, provider, model, timeout):
            if failure and task_input["id"] == "t1" and arm == "current":
                raise ValueError("synthetic infrastructure failure")
            task = tasks[task_input["id"]]
            answer = json.dumps({} if gain and task["id"] == "t1" and arm == "current" else task["expect"])
            engine = Engine(root / "home")
            try:
                ev = Evolution(engine)
                captured = engine.store.capture(self.case["project"], "fixture-consumer", "user", "u", task["prompt"])
                ev.captured(captured["source_id"], "user", "fixture-consumer")
                context = ev.context(self.case["project"], task["prompt"], session="fixture-consumer")
                body = context["rendered_context"]
                captured = engine.store.capture(self.case["project"], "fixture-consumer", "assistant", "a", answer)
                ev.captured(captured["source_id"], "assistant", "fixture-consumer", receipt_id=context["receipt_id"] if body else "")
            finally:
                engine.store.close()
            events = [{"type": "message_end", "message": {"role": "user", "content": [{"type": "text", "text": task["prompt"]}]}}]
            if body:
                events.append({"type": "message_end", "message": {"role": "custom", "customType": "tongle-next-context", "content": body, "details": {"receiptId": context["receipt_id"], "promptHash": hashlib.sha256(task["prompt"].encode()).hexdigest()}}})
            events.append({"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": answer}], "provider": "fixture", "model": "fixture", "stopReason": "stop"}})
            (root / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")
            return {"answer": answer, "provider": "fixture", "model": "fixture", "seconds": 0, "usage": None}

        # Only this synthetic unit test deliberately fabricates a native marker.
        with patch.object(_evaluate, "consume", fake):
            _pilot.run_pilot(self.case, out, "unused-pi", "fixture", "fixture", consumer=fake)
        return _pilot.assess_run(self.case, out)

    def test_no_gain_cannot_publish_and_gain_keeps_dependencies(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            proof = self._run(root / "tie")
            self.assertEqual(proof["disposition"], "no_gain")
            review = {"approved": True, "findings": [], "reviewer": "independent", "proof_hash": proof["proof_hash"]}
            with self.assertRaises(ValueError):
                _pilot.publish_pilot(self.case, root / "tie", review, root / "refused")
            self.assertFalse((root / "refused").exists())
            proof = self._run(root / "gain", gain=True)
            self.assertEqual(proof["disposition"], "validated_in_scope")
            with self.assertRaises(ValueError):
                _pilot.publish_pilot(self.case, root / "gain", review, root / "wrong-proof")
            review["proof_hash"] = proof["proof_hash"]
            result = _pilot.publish_pilot(self.case, root / "gain", review, root / "published")
            self.assertTrue(result["pilot_only"])
            self.assertEqual(len(result["candidate"]["basis"]), 2)

    def test_failed_cell_is_unknown_not_a_baseline_loss(self):
        with tempfile.TemporaryDirectory() as td:
            proof = self._run(Path(td) / "run", gain=True, failure=True)
            self.assertEqual(proof["disposition"], "needs_evidence")
            self.assertIsNone(proof["scores"]["t1"]["current"])
            self.assertEqual(proof["fixed"], [])
            self.assertEqual(proof["total_cells"], 4)

    def test_empty_knowledge_is_valid_absence_not_infra_failure(self):
        self.case["tasks"] = [{"id": "t1", "prompt": "6乘7等于多少？", "expect": {"result": 42}}]
        with tempfile.TemporaryDirectory() as td:
            proof = self._run(Path(td) / "run")
            self.assertEqual(proof["disposition"], "no_gain")
            self.assertFalse(proof["candidate_delivered"]["t1"])

    def test_changed_events_code_identity_and_missing_cell_are_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "run"
            self._run(root)
            mutations = [
                (root / "00-candidate/events.jsonl", lambda s: s.replace("fixture", "changed", 1)),
                (root / "manifest.json", lambda s: json.dumps(dict(json.loads(s), code_hash="changed"))),
                (root / "00-candidate.json", lambda s: json.dumps(dict(json.loads(s), identity={"provider": "changed", "model": "fixture"}))),
            ]
            for path, mutate in mutations:
                original = path.read_text()
                path.write_text(mutate(original))
                with self.assertRaises(ValueError):
                    _pilot.assess_run(self.case, root)
                path.write_text(original)
            (root / "00-current.json").unlink()
            with self.assertRaises((ValueError, FileNotFoundError)):
                _pilot.assess_run(self.case, root)

    def test_materialize_rejects_symlink_and_existing_instance_parent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "target").mkdir()
            (root / "alias").symlink_to(root / "target", target_is_directory=True)
            with self.assertRaises(ValueError):
                _pilot.materialize(root / "alias/escape", self.case, "current")
            (root / "target/state.sqlite3").touch()
            with self.assertRaises(ValueError):
                _pilot.materialize(root / "target/nested", self.case, "current")


if __name__ == "__main__":
    unittest.main()
