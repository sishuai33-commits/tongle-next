"""Focused tests for the v2 delta context adapter.

Uses the real ``Engine``/``Evolution`` APIs and the real ``knowledge_work`` /
``knowledge_seen`` tables. No synthetic fallbacks: a captured conversation
event is queued exactly as ``Evolution.queue`` would queue it (canonical
``_source_parts`` in ``knowledge_work.inputs`` plus per-part ``knowledge_seen``
ownership).

``build_live_context`` returns the *real* ``delta_contract`` context
(``protocol`` / ``window`` / ``cards`` / ``binding``), so every assertion here
is made against that genuine shape and re-checked through
``delta_contract.revalidate_context``.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from tongle import delta_contract
from tongle.engine import Engine
from tongle.evolution import CoverageWait, SelectionChanged, Evolution

from tongle.delta_context import (
    FULL_SOURCE_LIMIT,
    build_live_context,
)


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.realpath(self.tmp.name)
        engine = Engine(os.path.join(self.root, "state"))
        engine.init(os.path.join(self.root, "vault"))
        self.addCleanup(engine.store.close)
        (engine.home / "config.json").write_text(json.dumps({}))
        self.engine = engine
        self.evo = Evolution(engine)

    # --- helpers -----------------------------------------------------

    def _capture(self, project, session, role, event_id, text):
        # The real capture API lives on the Store reached through the engine.
        return self.engine.store.capture(project, session, role, event_id, text)

    def _make_owned_job(self, source, parts=None):
        """Queue one source exactly like Evolution.queue does.

        Freezes the canonical ``_source_parts`` decomposition into a
        ``knowledge_work`` row and books every part to that work id in
        ``knowledge_seen``. ``parts`` may override the frozen decomposition to
        simulate a partial/stale input.
        """
        if parts is None:
            parts = list(self.evo._source_parts(source))
        jid = "work-" + source["source_id"]
        with self.engine.transaction() as db:
            db.execute(
                "INSERT INTO knowledge_work(id, project, session, inputs,"
                " state, created_at) VALUES(?,?,?,?,'pending',?)",
                (jid, source["project"], source.get("session"),
                 _canonical(parts), 0.0),
            )
            for part in list(self.evo._source_parts(source)):
                db.execute(
                    "INSERT OR REPLACE INTO knowledge_seen"
                    "(source_id, version, part, work_id) VALUES(?,?,?,?)",
                    (source["source_id"], source["version"], part["part"], jid),
                )
        return {
            "id": jid,
            "project": source["project"],
            "session": source.get("session"),
            "inputs": _canonical(parts),
        }

    # --- contract inspection helpers ---------------------------------

    @staticmethod
    def _parts(ctx):
        return ctx["window"]["parts"]

    def _owned(self, ctx):
        return [p for p in self._parts(ctx) if p["responsibility"] == "owned"]

    def _refs(self, ctx):
        return [p for p in self._parts(ctx)
                if p["responsibility"] == "reference"]

    def _card_bodies(self, ctx):
        return [c["snapshot"]["body"] for c in ctx["cards"]]

    def _assert_contract(self, ctx):
        # Revalidates the binding hash over the exact frozen window and cards.
        delta_contract.revalidate_context(ctx)
        self.assertEqual(ctx["protocol"], delta_contract.PROTOCOL)
        self.assertIsInstance(ctx["binding"], str)
        self.assertEqual(len(ctx["binding"]), 64)


class FullSourceTests(_Base):
    def test_long_source_owned_all_parts_coalesces_once(self):
        marker = "late-exception-marker-0123456789"
        text = ("x" * 11190) + marker + ("y" * 800)
        self.assertGreater(len(text), 10000)
        rec = self._capture("p", "s", "user", "e1", text)
        source = self.engine.store.source(rec["source_id"], rec["version"])
        job = self._make_owned_job(source)
        ctx = build_live_context(self.evo, job)
        self._assert_contract(ctx)
        self.assertEqual(ctx["window"]["protocol"], "tongle-source-window-v1")
        self.assertEqual(ctx["window"]["project"], "p")

        owned = self._owned(ctx)
        self.assertEqual(len(owned), 1)
        self.assertEqual(owned[0]["text"], text)
        self.assertEqual(owned[0]["source_bounds"]["source_length"], len(text))
        for part in owned:
            self.assertEqual(part["source_id"], rec["source_id"])
            self.assertEqual(part["session"], "s")
        # Every owned part is frozen in full: nothing trimmed or redacted.
        # The late marker (past the 10000-char split) must survive intact.
        self.assertTrue(any(marker in p["text"] for p in owned))
        self.assertEqual(self._refs(ctx), [])
        total = sum(len(p["text"]) for p in self._parts(ctx))
        self.assertEqual(total, len(text))
        # The owned source keeps exactly one responsibility owner.
        self.assertTrue(all(p["responsibility"] == "owned"
                            for p in self._parts(ctx)))

    def test_missing_owned_chunk_rejects(self):
        text = "a" * 11000
        rec = self._capture("p", "s", "user", "e1", text)
        source = self.engine.store.source(rec["source_id"], rec["version"])
        full = list(self.evo._source_parts(source))
        self.assertGreater(len(full), 1)  # a partial input is even possible
        job = self._make_owned_job(source, parts=full[:1])
        with self.assertRaises(CoverageWait):
            build_live_context(self.evo, job)

    def test_same_version_text_mutation_rejects(self):
        rec = self._capture("p", "s", "user", "e1", "hello world")
        source = self.engine.store.source(rec["source_id"], rec["version"])
        parts = list(self.evo._source_parts(source))
        mutated = dict(parts[0])
        mutated["text"] = "HELLO WORLD CHANGED"
        job = self._make_owned_job(source, parts=[mutated])
        with self.assertRaises(SelectionChanged):
            build_live_context(self.evo, job)

    def test_current_version_change_refuses(self):
        rec = self._capture("p", "s", "user", "e1", "hello world")
        source = self.engine.store.source(rec["source_id"], rec["version"])
        job = self._make_owned_job(source)
        with self.engine.transaction() as db:
            db.execute("UPDATE sources SET current_version=current_version+1"
                       " WHERE id=?", (rec["source_id"],))
        with self.assertRaises((SelectionChanged, CoverageWait)):
            build_live_context(self.evo, job)

    def test_wrong_seen_owner_rejects(self):
        rec = self._capture("p", "s", "user", "e1", "hello world")
        source = self.engine.store.source(rec["source_id"], rec["version"])
        job = self._make_owned_job(source)
        with self.engine.transaction() as db:
            db.execute(
                "UPDATE knowledge_seen SET work_id='other-job'"
                " WHERE source_id=? AND version=?",
                (rec["source_id"], rec["version"]),
            )
        with self.assertRaises(CoverageWait):
            build_live_context(self.evo, job)

    def test_unbooked_owned_part_rejects(self):
        rec = self._capture("p", "s", "user", "e1", "hello world")
        source = self.engine.store.source(rec["source_id"], rec["version"])
        job = self._make_owned_job(source)
        with self.engine.transaction() as db:
            db.execute("DELETE FROM knowledge_seen WHERE source_id=?",
                       (rec["source_id"],))
        with self.assertRaises(CoverageWait):
            build_live_context(self.evo, job)

    def test_owned_job_project_mismatch_rejects(self):
        rec = self._capture("p", "s", "user", "e1", "hello world")
        source = self.engine.store.source(rec["source_id"], rec["version"])
        job = self._make_owned_job(source)
        job["project"] = "other-project"
        with self.assertRaises(SelectionChanged):
            build_live_context(self.evo, job)

    def test_owned_job_session_mismatch_rejects(self):
        rec = self._capture("p", "s", "user", "e1", "hello world")
        source = self.engine.store.source(rec["source_id"], rec["version"])
        job = self._make_owned_job(source)
        job["session"] = "other-session"
        with self.assertRaises(SelectionChanged):
            build_live_context(self.evo, job)


class ReferenceTests(_Base):
    def test_previous_same_session_is_reference_only(self):
        prev = self._capture("p", "s", "user", "prev1", "earlier topic about cats")
        rec = self._capture("p", "s", "user", "e1", "now about dogs")
        source = self.engine.store.source(rec["source_id"], rec["version"])
        job = self._make_owned_job(source)
        ctx = build_live_context(self.evo, job)
        self._assert_contract(ctx)

        owned = self._owned(ctx)
        refs = self._refs(ctx)
        self.assertEqual([p["source_id"] for p in owned], [rec["source_id"]])
        self.assertEqual([p["source_id"] for p in refs], [prev["source_id"]])
        for ref in refs:
            self.assertEqual(ref["part"], 0)
            stored = self.engine.store.source(ref["source_id"])
            self.assertEqual(ref["text"], stored["text"])

    def test_reference_times_are_real_source_timestamps(self):
        prev = self._capture("p", "s", "user", "prev1", "earlier topic")
        rec = self._capture("p", "s", "user", "e1", "now")
        source = self.engine.store.source(rec["source_id"], rec["version"])
        job = self._make_owned_job(source)
        ctx = build_live_context(self.evo, job)
        self._assert_contract(ctx)

        for part in self._parts(ctx):
            stored = self.engine.store.source(part["source_id"])
            self.assertEqual(part["ledger_captured_at"], stored["created_at"])
            self.assertEqual(part["ledger_version_at"],
                             stored["version_created_at"])
            self.assertIsNotNone(part["ledger_captured_at"])
            self.assertIsNotNone(part["ledger_version_at"])
        # The reference earlier in the same session is ordered before the owned.
        refs = self._refs(ctx)
        owned = self._owned(ctx)
        self.assertEqual([p["source_id"] for p in refs], [prev["source_id"]])
        self.assertEqual([p["source_id"] for p in owned], [rec["source_id"]])

    def test_unparseable_owned_time_excludes_references(self):
        self._capture("p", "s", "user", "prev1", "earlier topic")
        rec = self._capture("p", "s", "user", "e1", "mine")
        source = self.engine.store.source(rec["source_id"], rec["version"])
        job = self._make_owned_job(source)
        with self.engine.transaction() as db:
            db.execute("UPDATE sources SET created_at='not-a-timestamp'"
                       " WHERE id=?", (rec["source_id"],))
        ctx = build_live_context(self.evo, job)
        self._assert_contract(ctx)
        self.assertEqual(self._refs(ctx), [])
        self.assertEqual(len(self._owned(ctx)), 1)

    def test_foreign_session_excluded(self):
        self._capture("p", "other", "user", "prev1", "foreign session topic")
        rec = self._capture("p", "s", "user", "e1", "mine")
        source = self.engine.store.source(rec["source_id"], rec["version"])
        job = self._make_owned_job(source)
        ctx = build_live_context(self.evo, job)
        self._assert_contract(ctx)
        self.assertEqual(self._refs(ctx), [])
        self.assertEqual([p["source_id"] for p in self._owned(ctx)],
                         [rec["source_id"]])

    def test_foreign_project_excluded(self):
        self._capture("other", "s", "user", "prev1", "other project topic")
        rec = self._capture("p", "s", "user", "e1", "mine")
        source = self.engine.store.source(rec["source_id"], rec["version"])
        job = self._make_owned_job(source)
        ctx = build_live_context(self.evo, job)
        self._assert_contract(ctx)
        self.assertEqual(self._refs(ctx), [])

    def test_later_same_session_event_is_not_a_reference(self):
        rec = self._capture("p", "s", "user", "e1", "the owned event")
        later = self._capture("p", "s", "user", "e2", "a later event")
        source = self.engine.store.source(rec["source_id"], rec["version"])
        job = self._make_owned_job(source)
        ctx = build_live_context(self.evo, job)
        self._assert_contract(ctx)
        ids = [p["source_id"] for p in self._parts(ctx)]
        self.assertNotIn(later["source_id"], ids)

    def test_oversize_reference_never_truncated(self):
        big = "z" * 25000
        prev = self._capture("p", "s", "user", "prev1", big)
        rec = self._capture("p", "s", "user", "e1", "small")
        source = self.engine.store.source(rec["source_id"], rec["version"])
        job = self._make_owned_job(source)
        ctx = build_live_context(self.evo, job)
        self._assert_contract(ctx)
        refs = {r["source_id"]: r for r in self._refs(ctx)}
        self.assertIn(prev["source_id"], refs)
        self.assertEqual(refs[prev["source_id"]]["text"], big)
        total = sum(len(p["text"]) for p in self._parts(ctx))
        self.assertLessEqual(total, FULL_SOURCE_LIMIT)

    def test_at_most_two_prior_references(self):
        self._capture("p", "s", "user", "p1", "first prior turn")
        self._capture("p", "s", "user", "p2", "second prior turn")
        self._capture("p", "s", "user", "p3", "third prior turn")
        rec = self._capture("p", "s", "user", "e1", "the owned turn")
        source = self.engine.store.source(rec["source_id"], rec["version"])
        job = self._make_owned_job(source)
        ctx = build_live_context(self.evo, job)
        self._assert_contract(ctx)
        self.assertLessEqual(len(self._refs(ctx)), 2)

    def test_window_budget_excludes_reference_that_cannot_fit(self):
        # Owned event plus a prior event that alone would exceed the budget.
        big = "z" * 25000
        self._capture("p", "s", "user", "prev1", big)
        rec = self._capture("p", "s", "user", "e1", "q" * 29000)
        source = self.engine.store.source(rec["source_id"], rec["version"])
        job = self._make_owned_job(source)
        ctx = build_live_context(self.evo, job)
        self._assert_contract(ctx)
        self.assertEqual(self._refs(ctx), [])
        total = sum(len(p["text"]) for p in self._parts(ctx))
        self.assertLessEqual(total, FULL_SOURCE_LIMIT)


class RecallTests(_Base):
    def test_relevant_card_retrieved_with_entire_body(self):
        body = "aardvark marsupial taxonomy " + ("q" * 500)
        self.evo.cards.seed("p", "k1", "memory", "zoology", "Marsupials", body,
                            evidence=[])
        rec = self._capture("p", "s", "user", "e1", "aardvark marsupial")
        source = self.engine.store.source(rec["source_id"], rec["version"])
        job = self._make_owned_job(source)
        ctx = build_live_context(self.evo, job)
        self._assert_contract(ctx)
        bodies = self._card_bodies(ctx)
        self.assertIn(body, bodies)
        self.assertLessEqual(len(ctx["cards"]), 6)
        self.assertLessEqual(sum(len(b) for b in bodies), 80000)

    def test_unrelated_card_does_not_change_binding(self):
        rec = self._capture("p", "s", "user", "e1", "aardvark marsupial")
        source = self.engine.store.source(rec["source_id"], rec["version"])
        job = self._make_owned_job(source)
        before = build_live_context(self.evo, job)
        self._assert_contract(before)
        before_ids = [p["source_id"] for p in self._parts(before)]
        before_owned = [p["text"] for p in self._owned(before)]
        self.evo.cards.seed("p", "k2", "memory", "unrelated", "Ships",
                            "nautical vessel hull " + ("r" * 400), evidence=[])
        after = build_live_context(self.evo, job)
        self._assert_contract(after)
        self.assertEqual([p["source_id"] for p in self._parts(after)],
                         before_ids)
        self.assertEqual([p["text"] for p in self._owned(after)], before_owned)
        # The binding is over the frozen window + selected cards; a card that
        # is not selected by bounded recall cannot change it.
        self.assertEqual(after["binding"], before["binding"])

    def test_no_model_or_knowledge_calls(self):
        rec = self._capture("p", "s", "user", "e1", "plain text")
        source = self.engine.store.source(rec["source_id"], rec["version"])
        job = self._make_owned_job(source)
        calls = []
        self.evo.knowledge = lambda *a, **k: calls.append((a, k))
        ctx = build_live_context(self.evo, job)
        self.assertEqual(calls, [])
        self.assertIsInstance(ctx["window"]["parts"], list)

    def test_recall_is_a_bounded_hint_not_a_uniqueness_claim(self):
        rec = self._capture("p", "s", "user", "e1", "plain text")
        source = self.engine.store.source(rec["source_id"], rec["version"])
        job = self._make_owned_job(source)
        ctx = build_live_context(self.evo, job)
        self._assert_contract(ctx)
        self.assertLessEqual(len(ctx["cards"]), 6)


if __name__ == "__main__":
    unittest.main()
