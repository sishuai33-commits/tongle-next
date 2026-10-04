"""Integration tests for ROOT-reviewed migration read hints in the v2 context.

Drives the real ``Engine`` / ``Evolution`` / ``delta_migration.prepare`` /
``delta_migration.apply`` seam against a REAL temporary store and real captured
sources, then builds the v2 context with the real ``delta_context`` adapter. No
model is ever called in these tests.

Covers: a migrated child that carries ROOT-reviewed mandatory full-card READ
hints (a card previously lexically missed is still included FULL, and keeps
normal own edit rights), the ordinary no-hint path staying byte-identical, a
required card whose version/body legitimately advanced after migration (the
current body is read, not the lineage digest), an out-of-scope required target
rejected, a forged/missing grant or audit mismatch rejected, mandatory bodies
over the 80k cap failing (never truncated), and optional recall being evicted
by required hints at the 12-card cap.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from tongle import delta_contract, delta_migration as dm
from tongle.engine import Engine
from tongle.evolution import CoverageWait, Evolution

from tongle.delta_context import build_live_context

LEGACY = dm.LEGACY_PROTOCOL
V2 = dm.V2_PROTOCOL


def _canon(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.realpath(self.tmp.name)
        self.engine = Engine(os.path.join(self.root, "state"))
        self.engine.init(os.path.join(self.root, "vault"))
        self.addCleanup(self.engine.store.close)
        (self.engine.home / "config.json").write_text(json.dumps({
            "knowledge_mode": "evolving",
            "model_command": ["stub-never-called"],
            "knowledge_quiet_seconds": 0,
        }))
        self.evo = Evolution(self.engine)

    # ------------------------------------------------------- fixtures

    def _capture(self, project, session, role, event_id, text):
        return self.engine.store.capture(project, session, role, event_id, text)

    def _legacy_row(self, work_id, project, session, inputs, state="pending"):
        self.engine.db.execute(
            "INSERT INTO knowledge_work(id,project,session,inputs,state,"
            "created_at,pipeline_protocol) VALUES(?,?,?,?,?,0.0,?)",
            (work_id, project, session, _canon(inputs), state, LEGACY))
        for part in inputs:
            self.engine.db.execute(
                "INSERT OR IGNORE INTO knowledge_seen VALUES(?,?,?,?)",
                (part["source_id"], part["version"], part["part"], work_id))
        self.engine.db.commit()

    def _request(self, grant="grant-a", parents=None, cards=None):
        return {
            "grant_id": grant,
            "code_sha": "a" * 64,
            "acceptance_sha": "b" * 64,
            "parents": parents if parents is not None else [],
            "required_cards": cards if cards is not None else {},
        }

    def _migrate(self, parent_id, source_id, card_ids=(), grant="grant-a"):
        """Run the real prepare/apply for one single-source parent."""
        source = self.engine.store.source(source_id)
        parts = list(self.evo._source_parts(source))
        self._legacy_row(parent_id, source["project"], source["session"], parts)
        cards = {source_id: list(card_ids)} if card_ids else {}
        plan = dm.prepare(self.evo, self._request(
            grant=grant, parents=[{"id": parent_id, "mode": "uncommitted"}],
            cards=cards))
        receipt = dm.apply(self.evo, plan)
        child_id = plan["children"][0]["child_id"]
        self.assertEqual(receipt["children"][0]["child_id"], child_id)
        return plan, child_id

    def _job(self, child_id):
        row = self.engine.db.execute(
            "SELECT * FROM knowledge_work WHERE id=?", (child_id,)).fetchone()
        return {"id": row["id"], "project": row["project"],
                "session": row["session"], "inputs": row["inputs"]}

    def _ordinary_job(self, source):
        """Queue one source exactly like the ordinary v2 path does."""
        parts = list(self.evo._source_parts(source))
        jid = "work-" + source["source_id"]
        with self.engine.transaction() as db:
            db.execute(
                "INSERT INTO knowledge_work(id,project,session,inputs,state,"
                "created_at) VALUES(?,?,?,?,'pending',0.0)",
                (jid, source["project"], source.get("session"), _canon(parts)))
            for part in parts:
                db.execute(
                    "INSERT OR REPLACE INTO knowledge_seen"
                    "(source_id,version,part,work_id) VALUES(?,?,?,?)",
                    (source["source_id"], source["version"], part["part"], jid))
        return {"id": jid, "project": source["project"],
                "session": source.get("session"), "inputs": _canon(parts)}

    @staticmethod
    def _bodies(ctx):
        return [c["snapshot"]["body"] for c in ctx["cards"]]

    @staticmethod
    def _ids(ctx):
        return [c["snapshot"]["id"] for c in ctx["cards"]]

    def _audit_row(self, child_id):
        return self.engine.db.execute(
            "SELECT error FROM knowledge_failures WHERE work_id=? AND state=?",
            (child_id, dm.CHILD_AUDIT)).fetchone()

    def _rewrite_audit(self, child_id, mutate):
        row = self._audit_row(child_id)
        audit = json.loads(row["error"])
        mutate(audit)
        self.engine.db.execute(
            "UPDATE knowledge_failures SET error=? WHERE work_id=? AND state=?",
            (_canon(audit), child_id, dm.CHILD_AUDIT))
        self.engine.db.commit()


class RequiredHintTests(_Base):
    def test_lexically_missed_required_card_included_full_with_edit_rights(self):
        # A card with NO lexical overlap with the owned turn: ordinary bounded
        # recall would never surface it, but the ROOT-reviewed hint requires it.
        body = "zooplankton trophic cascade " + ("z" * 300)
        card = self.evo.cards.seed("p", "k-req", "knowledge", "oceanography",
                                   "Zooplankton", body, evidence=[])
        rec = self._capture("p", "s", "user", "e1",
                            "aardvark marsupial taxonomy report")
        plan, child_id = self._migrate("p1", rec["source_id"], [card["id"]])
        job = self._job(child_id)

        ctx = build_live_context(self.evo, job)
        delta_contract.revalidate_context(ctx)

        # Included FULL, as the first (deterministic audit order) card.
        self.assertEqual(self._ids(ctx)[0], card["id"])
        self.assertIn(body, self._bodies(ctx))
        snapshot = ctx["cards"][0]["snapshot"]
        self.assertEqual(snapshot["body"], body)
        self.assertEqual(snapshot["project"], "p")
        self.assertEqual(snapshot["state"], "active")
        self.assertFalse(snapshot["manually_edited"])

        # Normal own edit rights are preserved: a real plan may update c1.
        payload = delta_contract.plan_payload(ctx)
        owned = payload["owned_source_refs"]
        self.assertTrue(owned)
        target = payload["cards"][0]["ref"]
        plan_out = delta_contract.compile_plan(ctx, {
            "changes": [{
                "ref": "n1", "action": "update", "source_refs": owned,
                "reason": "append required detail", "target": target,
                "edits": [{"op": "insert_after",
                           "segment": "%sg1" % target, "text": " extra"}],
            }],
            "dispositions": [{
                "source_refs": owned, "decision": "material",
                "change_refs": ["n1"], "card_refs": [],
                "reason": "material update of required card",
            }],
        })
        self.assertEqual(plan_out["operations"][0]["op"], "update")

    def test_ordinary_no_hint_output_unchanged(self):
        # A lexically missed card is NOT surfaced on the ordinary path.
        missed = self.evo.cards.seed("p", "k-miss", "knowledge", "oceanography",
                                     "Zooplankton", "zooplankton trophic cascade " + ("z" * 300),
                                     evidence=[])
        # A lexically relevant card that ordinary bounded recall DOES admit.
        decoy = self.evo.cards.seed("p", "k-decoy", "memory", "aardvark",
                                    "Marsupial", "aardvark marsupial taxonomy",
                                    evidence=[])
        rec = self._capture("p", "s", "user", "e1",
                            "aardvark marsupial taxonomy report")
        source = self.engine.store.source(rec["source_id"], rec["version"])
        ctx = build_live_context(self.evo, self._ordinary_job(source))
        delta_contract.revalidate_context(ctx)
        self.assertEqual(self._ids(ctx), [decoy["id"]])

        # A migrated child of a DIFFERENT source with no hints selects exactly
        # the same ordinary cards: the no-hint path is unchanged.
        rec2 = self._capture("p", "s", "user", "e2",
                             "aardvark marsupial taxonomy report")
        _plan, child_id = self._migrate("p1", rec2["source_id"], [])
        migrated = build_live_context(self.evo, self._job(child_id))
        delta_contract.revalidate_context(migrated)
        self.assertEqual(self._ids(migrated), self._ids(ctx))

        # And the lexically missed card stays absent in both.
        self.assertNotIn(missed["id"], self._ids(migrated))

    def test_advanced_required_card_reads_latest_body(self):
        card = self.evo.cards.seed("p", "k-req", "knowledge", "topic",
                                   "Account", "old body v1", evidence=[])
        rec = self._capture("p", "s", "user", "e1", "plain owned text")
        plan, child_id = self._migrate("p1", rec["source_id"], [card["id"]])

        # A migrated sibling process legitimately advances the card after apply.
        self.engine.db.execute(
            "UPDATE knowledge_cards SET body=?, version=version+1 WHERE id=?",
            ("new body v2", card["id"]))
        self.engine.db.commit()
        advanced = self.evo.cards.get(card["id"])
        self.assertNotEqual(advanced["version"], card["version"])

        ctx = build_live_context(self.evo, self._job(child_id))
        delta_contract.revalidate_context(ctx)
        snapshot = ctx["cards"][0]["snapshot"]
        self.assertEqual(snapshot["id"], card["id"])
        self.assertEqual(snapshot["version"], advanced["version"])
        self.assertEqual(snapshot["body"], "new body v2")

    def test_out_of_scope_required_card_rejected(self):
        card = self.evo.cards.seed("p", "k-req", "knowledge", "topic",
                                   "Title", "required body", evidence=[])
        rec = self._capture("p", "s", "user", "e1", "plain owned text")
        _plan, child_id = self._migrate("p1", rec["source_id"], [card["id"]])
        # Move the required card out of the job's authorized scope.
        self.engine.db.execute(
            "UPDATE knowledge_cards SET project='other-scope' WHERE id=?",
            (card["id"],))
        self.engine.db.commit()
        with self.assertRaises(CoverageWait):
            build_live_context(self.evo, self._job(child_id))

    def test_missing_required_card_rejected(self):
        card = self.evo.cards.seed("p", "k-req", "knowledge", "topic",
                                   "Title", "required body", evidence=[])
        rec = self._capture("p", "s", "user", "e1", "plain owned text")
        _plan, child_id = self._migrate("p1", rec["source_id"], [card["id"]])
        self.engine.db.execute("DELETE FROM knowledge_cards WHERE id=?",
                               (card["id"],))
        self.engine.db.commit()
        with self.assertRaises(CoverageWait):
            build_live_context(self.evo, self._job(child_id))


class GrantAndAuditTests(_Base):
    def _migrated(self):
        card = self.evo.cards.seed("p", "k-req", "knowledge", "topic",
                                   "Title", "required body", evidence=[])
        rec = self._capture("p", "s", "user", "e1", "plain owned text")
        _plan, child_id = self._migrate("p1", rec["source_id"], [card["id"]])
        return card, child_id

    def test_missing_grant_receipt_rejected(self):
        _card, child_id = self._migrated()
        self.engine.db.execute("DELETE FROM meta WHERE key=?",
                               (dm.MIGRATION_META_PREFIX + "grant-a",))
        self.engine.db.commit()
        with self.assertRaises(CoverageWait):
            build_live_context(self.evo, self._job(child_id))

    def test_forged_grant_id_rejected(self):
        _card, child_id = self._migrated()
        self._rewrite_audit(child_id, lambda a: a.__setitem__("grant_id", "forged"))
        with self.assertRaises(CoverageWait):
            build_live_context(self.evo, self._job(child_id))

    def test_audit_text_sha_mismatch_rejected(self):
        _card, child_id = self._migrated()
        self._rewrite_audit(child_id, lambda a: a.__setitem__("text_sha", "0" * 64))
        with self.assertRaises(CoverageWait):
            build_live_context(self.evo, self._job(child_id))

    def test_audit_required_cards_mismatch_rejected(self):
        _card, child_id = self._migrated()
        self._rewrite_audit(
            child_id,
            lambda a: a.__setitem__("required_cards", []))
        with self.assertRaises(CoverageWait):
            build_live_context(self.evo, self._job(child_id))

    def test_malformed_and_duplicate_audit_rejected(self):
        _card, child_id = self._migrated()
        self.engine.db.execute(
            "UPDATE knowledge_failures SET error='not json' WHERE work_id=? AND state=?",
            (child_id, dm.CHILD_AUDIT))
        self.engine.db.commit()
        with self.assertRaises(CoverageWait):
            build_live_context(self.evo, self._job(child_id))

        # Restore a valid audit, then duplicate it.
        row = self.engine.db.execute(
            "SELECT error,created_at FROM knowledge_failures WHERE work_id=? AND state=?",
            (child_id, dm.CHILD_AUDIT)).fetchone()
        self.engine.db.execute(
            "INSERT INTO knowledge_failures(work_id,state,error,created_at)"
            " VALUES(?,?,?,?)",
            (child_id, dm.CHILD_AUDIT, '{"grant_id":"x"}', row["created_at"]))
        self.engine.db.commit()
        with self.assertRaises(CoverageWait):
            build_live_context(self.evo, self._job(child_id))


class BudgetTests(_Base):
    def test_oversize_mandatory_bodies_fail_not_truncate(self):
        # Two mandatory cards whose bodies together exceed the 80k context cap.
        big_a = "a" * 45000
        big_b = "b" * 45000
        card_a = self.evo.cards.seed("p", "k-a", "knowledge", "topic",
                                     "A", big_a, evidence=[])
        card_b = self.evo.cards.seed("p", "k-b", "knowledge", "topic",
                                     "B", big_b, evidence=[])
        rec = self._capture("p", "s", "user", "e1", "plain owned text")
        _plan, child_id = self._migrate(
            "p1", rec["source_id"], [card_a["id"], card_b["id"]])
        with self.assertRaises(CoverageWait):
            build_live_context(self.evo, self._job(child_id))

    def test_optional_evicted_for_required_at_twelve_cap(self):
        # Seven mandatory hints plus many lexically relevant decoys: the total
        # context is capped at 12, so only 5 optional cards may be appended.
        body = "aardvark marsupial taxonomy"
        required_ids = []
        for i in range(7):
            card = self.evo.cards.seed("p", "k-r%d" % i, "knowledge",
                                       "oceanography", "Required %d" % i,
                                       "zooplankton %d body" % i, evidence=[])
            required_ids.append(card["id"])
        for i in range(6):
            self.evo.cards.seed("p", "k-d%d" % i, "memory", "aardvark",
                                "aardvark decoy %d" % i,
                                body + " decoy %d" % i, evidence=[])
        rec = self._capture("p", "s", "user", "e1", body)
        _plan, child_id = self._migrate("p1", rec["source_id"], required_ids)

        ctx = build_live_context(self.evo, self._job(child_id))
        delta_contract.revalidate_context(ctx)
        ids = self._ids(ctx)
        self.assertEqual(len(ids), 12)
        # Every mandatory hint is present, in deterministic audit order first.
        self.assertEqual(ids[:7], required_ids)
        self.assertLessEqual(sum(len(b) for b in self._bodies(ctx)), 80000)


if __name__ == "__main__":
    unittest.main()
