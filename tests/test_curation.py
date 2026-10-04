"""Focused tests for tongle.curation (offline, synthetic vaults in TemporaryDirectory)."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tongle.curation import inventory, manifest_digest, validate_plan

NOTE_A = "---\ntype: note\ncreated: 2026-01-01\ntongle_id: tongle-abc\n---\n# Alpha\n\nSee [[Beta]] and [gam](Gamma%20Note.md#part).\n"
NOTE_B = "# Beta\n\nBack to [[Alpha]].\n"
NOTE_G = "# Gamma Note\n\n```\n[[Alpha]] is example only\n`[[Beta]]`\n```\n"
NOTE_DUP_BODY_A = "# Alpha\n\nSee [[Beta]] and [gam](Gamma Note.md#part).\n"
NOTE_INVALID = b"\xff\xfe\x00markdown"


def build_vault(root: Path) -> Path:
    vault = root / "vault"
    (vault / "知识").mkdir(parents=True)
    (vault / "Alpha.md").write_text(NOTE_A, encoding="utf-8")
    (vault / "Beta.md").write_text(NOTE_B, encoding="utf-8")
    (vault / "Gamma Note.md").write_text(NOTE_G, encoding="utf-8")
    (vault / "copy.md").write_text(NOTE_A, encoding="utf-8")
    (vault / "知识" / "中文文档.md").write_text("# 中文文档\n", encoding="utf-8")
    (vault / "binary.md").write_bytes(NOTE_INVALID)
    (vault / ".hidden.md").write_text("# hidden\n", encoding="utf-8")
    (vault / ".secretdir").mkdir()
    (vault / ".secretdir" / "x.md").write_text("# x\n", encoding="utf-8")
    (vault / "link.md").symlink_to(vault / "Alpha.md")
    return vault


def make_plan(manifest, decisions, **extra):
    plan = {
        "schema_version": 1,
        "manifest_digest": manifest["manifest_digest"],
        "decisions": decisions,
    }
    plan.update(extra)
    return plan


def keep_all(manifest, review="content_reviewed"):
    return [
        {"path": p, "expected_sha256": it["sha256"], "action": "keep",
         "target": p, "reason": "unchanged", "review_status": review}
        for p, it in sorted(manifest["items"].items())
    ]


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.vault = build_vault(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_inventory_basics(self):
        m = inventory(self.vault)
        paths = set(m["items"])
        self.assertIn("Alpha.md", paths)
        self.assertIn("知识/中文文档.md", paths)
        self.assertNotIn(".hidden.md", paths)
        self.assertNotIn("link.md", paths)
        reasons = {(e["path"], e["reason"]) for e in m["excluded"]}
        self.assertIn((".hidden.md", "hidden"), reasons)
        self.assertIn(("link.md", "symlink"), reasons)
        self.assertIn((".secretdir", "symlink") if False else (".secretdir", "hidden"), reasons)
        alpha = m["items"]["Alpha.md"]
        self.assertEqual(alpha["type"], "note")
        self.assertEqual(alpha["tongle_id"], "tongle-abc")
        self.assertEqual(alpha["title"], "Alpha")
        self.assertEqual(alpha["byte_count"], len(NOTE_A.encode()))
        self.assertTrue(m["items"]["binary.md"]["nontext"])
        # exact duplicates
        self.assertIn(["Alpha.md", "copy.md"], m["duplicates"]["exact"])
        # links: resolved; code-block examples are not links
        gamma = m["items"]["Gamma Note.md"]
        self.assertEqual(gamma.get("links", []), [])
        alpha_links = {(l["kind"], l["target"], l["status"], l["resolved_path"]) for l in alpha["links"]}
        self.assertIn(("wikilink", "Beta", "resolved", "Beta.md"), alpha_links)
        self.assertIn(("markdown", "Gamma Note.md#part", "resolved", "Gamma Note.md"), alpha_links)
        self.assertEqual(m["manifest_digest"], manifest_digest(
            {k: v for k, v in m.items() if k != "manifest_digest"}))

    def test_vault_root_symlink_rejected(self):
        link = self.root / "vaultlink"
        link.symlink_to(self.vault, target_is_directory=True)
        with self.assertRaises(ValueError):
            inventory(link)

    def test_ambiguous_and_broken_links(self):
        (self.vault / "Same.md").write_text("# Same\n", encoding="utf-8")
        sub = self.vault / "sub"
        sub.mkdir()
        (sub / "Same.md").write_text("# Same\n", encoding="utf-8")
        (self.vault / "知识" / "Linker.md").write_text("[[Same]] and [[Missing]]\n", encoding="utf-8")
        m = inventory(self.vault)
        links = {l["target"]: l["status"] for l in m["items"]["知识/Linker.md"]["links"]}
        self.assertEqual(links["Same"], "ambiguous")
        self.assertEqual(links["Missing"], "broken")

    def test_cli_inventory_and_output_location(self):
        out = self.root / "manifest.json"
        rc = subprocess.call(
            [sys.executable, "-m", "tongle.curation", "inventory",
             "--vault", str(self.vault), "--output", str(out)],
            cwd=Path(__file__).resolve().parent.parent)
        self.assertEqual(rc, 0)
        data = json.loads(out.read_text(encoding="utf-8"))
        self.assertIn("Alpha.md", data["items"])
        bad = self.vault / "out.json"
        rc = subprocess.call(
            [sys.executable, "-m", "tongle.curation", "inventory",
             "--vault", str(self.vault), "--output", str(bad)],
            cwd=Path(__file__).resolve().parent.parent)
        self.assertEqual(rc, 2)
        self.assertFalse(bad.exists())


class ValidatePlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.vault = build_vault(self.root)
        self.manifest = inventory(self.vault)

    def tearDown(self):
        self.tmp.cleanup()

    def codes(self, result):
        return [i["code"] for i in result["issues"]]

    def test_clean_keep_all_is_structurally_valid_but_not_ready_for_managed(self):
        result = validate_plan(self.vault, self.manifest, make_plan(self.manifest, keep_all(self.manifest)))
        self.assertTrue(result["readiness"]["structurally_valid"])
        self.assertTrue(result["readiness"]["content_review_complete"])
        self.assertFalse(result["readiness"]["ready_for_application"])  # tongle_id -> DB sync
        self.assertIn("db_sync_required", self.codes(result))

    def test_incomplete_coverage_blocks_ready(self):
        decisions = keep_all(self.manifest)[:-1]
        result = validate_plan(self.vault, self.manifest, make_plan(self.manifest, decisions))
        self.assertIn("missing_decision", self.codes(result))
        self.assertFalse(result["readiness"]["structurally_valid"])
        self.assertFalse(result["readiness"]["ready_for_application"])

    def test_unknown_and_duplicate_sources(self):
        decisions = keep_all(self.manifest)
        decisions.append(dict(decisions[1]))
        decisions.append(dict(decisions[2], path="Nope.md"))
        result = validate_plan(self.vault, self.manifest, make_plan(self.manifest, decisions))
        codes = self.codes(result)
        self.assertIn("unknown_decision_source", codes)
        self.assertIn("duplicate_decision_source", codes)

    def test_changed_removed_added_files(self):
        decisions = keep_all(self.manifest)
        (self.vault / "Beta.md").write_text("# Beta changed\n", encoding="utf-8")
        (self.vault / "Alpha.md").unlink()
        (self.vault / "New.md").write_text("# New\n", encoding="utf-8")
        result = validate_plan(self.vault, self.manifest, make_plan(self.manifest, decisions))
        codes = self.codes(result)
        self.assertIn("changed_file", codes)
        self.assertIn("removed_file", codes)
        self.assertIn("added_file", codes)
        self.assertFalse(result["ok"])

    def test_digest_mismatch(self):
        plan = make_plan(self.manifest, keep_all(self.manifest))
        plan["manifest_digest"] = "0" * 64
        result = validate_plan(self.vault, self.manifest, plan)
        self.assertIn("manifest_digest_mismatch", self.codes(result))

    def test_target_collisions_and_traversal(self):
        decisions = keep_all(self.manifest)
        for d in decisions:
            if d["path"] == "Beta.md":
                d.update(action="move", target="知识/BETA.MD", reason="rename")
            if d["path"] == "copy.md":
                d.update(action="move", target="知识/beta.md", reason="rename")
        result = validate_plan(self.vault, self.manifest, make_plan(self.manifest, decisions))
        self.assertIn("destination_collision", self.codes(result))

        decisions = keep_all(self.manifest)
        decisions[0].update(action="move", target="../evil.md", reason="escape")
        result = validate_plan(self.vault, self.manifest, make_plan(self.manifest, decisions))
        codes = self.codes(result)
        self.assertIn("traversal_in_target", codes)

        decisions = keep_all(self.manifest)
        decisions[0].update(action="move", target="C:\\win\\x.md", reason="bad")
        result = validate_plan(self.vault, self.manifest, make_plan(self.manifest, decisions))
        self.assertIn("backslash_in_target", self.codes(result))

    def test_new_directory_must_have_chinese_or_exist(self):
        decisions = keep_all(self.manifest)
        for d in decisions:
            if d["path"] == "Beta.md":
                d.update(action="move", target="tech/Beta.md", reason="move")
        result = validate_plan(self.vault, self.manifest, make_plan(self.manifest, decisions))
        self.assertIn("invalid_directory_name", self.codes(result))
        decisions = keep_all(self.manifest)
        for d in decisions:
            if d["path"] == "Beta.md":
                d.update(action="move", target="技术/Beta.md", reason="move")
        result = validate_plan(self.vault, self.manifest, make_plan(self.manifest, decisions))
        self.assertNotIn("invalid_directory_name", self.codes(result))

    def test_file_folder_overlap(self):
        decisions = keep_all(self.manifest)
        for d in decisions:
            if d["path"] == "Beta.md":
                d.update(action="move", target="知识/中文文档.md/x.md", reason="overlap")
        result = validate_plan(self.vault, self.manifest, make_plan(self.manifest, decisions))
        self.assertIn("file_folder_overlap", self.codes(result))

    def test_merge_rules(self):
        # merge copy.md into Alpha.md, archived
        decisions = keep_all(self.manifest, review="content_reviewed")
        for d in decisions:
            if d["path"] == "copy.md":
                d.update(action="merge", merge_into="Alpha.md",
                         target="archive/copy.md", reason="duplicate",
                         evidence=["dup-check:sha256"])
        # remove managed-file ready block by archiving managed file? keep simple: check validity
        result = validate_plan(self.vault, self.manifest, make_plan(self.manifest, decisions))
        codes = self.codes(result)
        self.assertNotIn("missing_decision", codes)
        self.assertNotIn("invalid_merge_target", codes)

        # missing evidence
        bad = keep_all(self.manifest)
        for d in bad:
            if d["path"] == "copy.md":
                d.update(action="merge", merge_into="Alpha.md",
                         target="archive/copy.md", reason="dup")
        result = validate_plan(self.vault, self.manifest, make_plan(self.manifest, bad))
        self.assertIn("missing_evidence", self.codes(result))

        # self merge
        bad = keep_all(self.manifest)
        for d in bad:
            if d["path"] == "copy.md":
                d.update(action="merge", merge_into="copy.md",
                         target="archive/copy.md", reason="dup", evidence=["e"])
        result = validate_plan(self.vault, self.manifest, make_plan(self.manifest, bad))
        self.assertIn("merge_self", self.codes(result))

        # cycle via mutual merge
        bad = keep_all(self.manifest)
        for d in bad:
            if d["path"] == "copy.md":
                d.update(action="merge", merge_into="binary.md",
                         target="archive/copy.md", reason="dup", evidence=["e"])
            if d["path"] == "binary.md":
                d.update(action="merge", merge_into="copy.md",
                         target="archive/binary.md", reason="dup", evidence=["e"])
        result = validate_plan(self.vault, self.manifest, make_plan(self.manifest, bad))
        self.assertIn("merge_cycle", self.codes(result))

        # archive destination already exists
        (self.vault / "archive").mkdir()
        (self.vault / "archive" / "copy.md").write_text("# old\n", encoding="utf-8")
        result = validate_plan(self.vault, self.manifest, make_plan(self.manifest, decisions))
        self.assertIn("archive_overwrites_existing", self.codes(result))

    def test_symlink_parent_escape_blocked(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "dir").mkdir()
        (self.vault / "escdir").symlink_to(outside / "dir", target_is_directory=True)
        self.manifest = inventory(self.vault)
        decisions = keep_all(self.manifest)
        for d in decisions:
            if d["path"] == "Beta.md":
                d.update(action="move", target="escdir/Beta.md", reason="move")
        result = validate_plan(self.vault, self.manifest, make_plan(self.manifest, decisions))
        self.assertIn("symlink_parent_escape", self.codes(result))

    def test_link_rewrite_required_for_move(self):
        # Beta is linked from Alpha ([[Beta]]); moving Beta without a link update is a blocker
        decisions = keep_all(self.manifest)
        for d in decisions:
            if d["path"] == "Beta.md":
                d.update(action="move", target="知识/Beta.md", reason="reorg")
        plan = make_plan(self.manifest, decisions)
        result = validate_plan(self.vault, self.manifest, plan)
        self.assertIn("unaddressed_incoming_link", self.codes(result))
        self.assertFalse(result["readiness"]["structurally_valid"])
        entry = [e for e in result["link_report"] if e["status"] == "unaddressed"]
        self.assertEqual(entry[0]["from_path"], "Alpha.md")
        self.assertEqual(entry[0]["old_target"], "Beta")

        # with a correct link_updates entry it is addressed
        plan["link_updates"] = [
            {"from_path": "Alpha.md", "old_target": "Beta", "new_target": "知识/Beta.md"},
            {"from_path": "copy.md", "old_target": "Beta", "new_target": "知识/Beta.md"}]
        result = validate_plan(self.vault, self.manifest, plan)
        self.assertNotIn("unaddressed_incoming_link", self.codes(result))
        self.assertEqual(result["link_report"][0]["status"], "addressed")

        # a wrong new_target is flagged
        plan["link_updates"] = [
            {"from_path": "Alpha.md", "old_target": "Beta", "new_target": "Gamma Note.md"}]
        result = validate_plan(self.vault, self.manifest, plan)
        self.assertIn("invalid_link_update", self.codes(result))

        # Current files cannot bypass broken links by claiming to be historical.
        plan = make_plan(self.manifest, decisions, preserve_historical=[
            {"from_path": "Alpha.md", "old_target": "Beta", "preserve_historical": True,
             "reason": "historic snapshot link"}])
        result = validate_plan(self.vault, self.manifest, plan)
        self.assertIn("invalid_historical_exception", self.codes(result))
        self.assertIn("unaddressed_incoming_link", self.codes(result))

        # preserve_historical without reason does not count
        plan = make_plan(self.manifest, decisions, preserve_historical=[
            {"from_path": "Alpha.md", "old_target": "Beta", "preserve_historical": True}])
        result = validate_plan(self.vault, self.manifest, plan)
        self.assertIn("unaddressed_incoming_link", self.codes(result))

    def test_malformed_inputs_fail_closed(self):
        result = validate_plan(self.vault, self.manifest, {"schema_version": 2, "decisions": []})
        self.assertFalse(result["readiness"]["ready_for_application"])
        self.assertIn("unsupported_schema_version", self.codes(result))
        result = validate_plan(self.vault, self.manifest, {"schema_version": 1})
        self.assertIn("malformed_plan", self.codes(result))
        result = validate_plan(self.vault, {"nope": 1}, {"schema_version": 1, "decisions": []})
        self.assertIn("malformed_manifest", self.codes(result))
        self.assertFalse(result["ok"])

    def test_draft_plan_never_ready(self):
        result = validate_plan(self.vault, self.manifest,
                               make_plan(self.manifest, keep_all(self.manifest), status="draft"))
        self.assertFalse(result["readiness"]["ready_for_application"])
        self.assertIn("draft_plan", self.codes(result))


if __name__ == "__main__":
    unittest.main()
