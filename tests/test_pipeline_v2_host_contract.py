"""Independent host boundary checks; no model or private data."""
import json
import unittest
from tongle.source_windows import freeze_window, resolve_evidence, validate_dispositions
from tongle.card_delta import freeze_card, apply_delta

class PersistenceBoundaryTests(unittest.TestCase):
    def test_source_survives_canonical_persistence_without_semantic_approval(self):
        w = freeze_window("p", [dict(source_id="s", version=1, part=0,
            role="assistant", kind="chat", text="仅报告检查通过，尚未验收。")])
        restored = json.loads(json.dumps(w, sort_keys=True))
        sid = w["spans"][0]["id"]
        self.assertEqual(resolve_evidence(restored, [sid])[0]["quote"], "仅报告检查通过，尚未验收。")
        d = dict(part_id=w["parts"][0]["part_id"], decision="covered", span_ids=[sid], reason="模型声称覆盖")
        self.assertIs(validate_dispositions(restored, [d])["resolved"], False)

    def test_card_persistence_preserves_untouched_exception_and_requires_review(self):
        c = freeze_card(dict(id="c", project="p", version=4, state="active",
            manually_edited=True, body="默认7天。\n\n例外：离线任务30天，仍需人工批准。"))
        restored = json.loads(json.dumps(c, sort_keys=True))
        result = apply_delta(restored, [dict(op="replace", segment_id=c["segments"][0]["id"], text="默认5天。\n\n")])
        self.assertEqual(result["body"], "默认5天。\n\n例外：离线任务30天，仍需人工批准。")
        self.assertIs(result["requires_semantic_review"], True)
        self.assertIs(result["requires_human_review"], True)
        self.assertEqual(result["expected_version"], 4)

    def test_card_identity_and_version_fail_before_alias_review(self):
        from tongle.delta_contract import build_context, revalidate_context
        from copy import deepcopy
        w = freeze_window("p", [dict(source_id="s", version=1, part=0,
            role="user", kind="chat", text="修改阈值，保留人工例外。")])
        card = dict(id="c", project="p", version=2, state="active",
            manually_edited=True, body="现有人工限定。", evidence=[])
        for cards in ([card, deepcopy(card)], [dict(card,id=None)], [dict(card,version=None)]):
            with self.assertRaises(ValueError):
                build_context(w, cards)
        ctx = build_context(w, [card])
        forged = deepcopy(ctx);forged["cards"].append(deepcopy(ctx["cards"][0]))
        with self.assertRaises(ValueError):
            revalidate_context(forged)
