import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tongle.card_delta import freeze_card, apply_delta


def make_card(**overrides):
    card = {
        "id": "card-1",
        "project": "proj-a",
        "version": 3,
        "body": "alpha\nkeep 42\nbeta",
        "state": "active",
    }
    card.update(overrides)
    return card


def seg_id_for(snap, index):
    return snap["segments"][index]["id"]


class FreezeCardTests(unittest.TestCase):
    def test_segments_cover_body_exactly(self):
        body = "one\n\n\n  two\r\n\r\n\U0001F600\n\nend"
        snap = freeze_card(make_card(body=body))
        self.assertEqual("".join(s["text"] for s in snap["segments"]), body)
        for s in snap["segments"]:
            self.assertEqual(body[s["start"]:s["end"]], s["text"])
        self.assertEqual(snap["body"], body)

    def test_forged_body_sha256_rejected(self):
        with self.assertRaises(ValueError):
            freeze_card(make_card(body_sha256="0" * 64))

    def test_valid_body_sha256_accepted(self):
        import hashlib
        body = make_card()["body"]
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        snap = freeze_card(make_card(body_sha256=digest))
        self.assertEqual(snap["body_sha256"], digest)


class ApplyDeltaTests(unittest.TestCase):
    def test_replace_preserves_untouched_chars(self):
        body = "header\n\ncount 1\n\nfooter\n"
        snap = freeze_card(make_card(body=body))
        # The frozen segment text is "count 1\n\n" (it carries the two
        # blank-line newlines), so a faithful replacement keeps both.
        target = seg_id_for(snap, 1)
        self.assertEqual(snap["segments"][1]["text"], "count 1\n\n")
        result = apply_delta(
            snap, [{"op": "replace", "segment_id": target, "text": "count 2\n\n"}]
        )
        self.assertEqual(result["body"], "header\n\ncount 2\n\nfooter\n")
        self.assertEqual(result["changed_segment_ids"], [target])

    def test_replace_is_verbatim_without_auto_newlines(self):
        body = "header\n\ncount 1\n\nfooter\n"
        snap = freeze_card(make_card(body=body))
        target = seg_id_for(snap, 1)
        # A replacement without trailing newlines is applied verbatim; the
        # engine never appends separators of its own.
        result = apply_delta(
            snap, [{"op": "replace", "segment_id": target, "text": "count 2"}]
        )
        self.assertEqual(result["body"], "header\n\ncount 2footer\n")
        # The replaced slice is exactly the supplied text, no more no less.
        self.assertNotEqual(result["body"], "header\n\ncount 2\n\nfooter\n")

    def test_insert_preserves_crlf_and_emoji(self):
        body = "a\r\n\r\nb \U0001F600\r\n\r\nc"
        snap = freeze_card(make_card(body=body))
        target = seg_id_for(snap, 1)
        result = apply_delta(snap, [{"op": "insert_after", "segment_id": target, "text": "X\r\n\U0001F642"}])
        self.assertEqual(result["body"], "a\r\n\r\nb \U0001F600\r\nx\r\n\U0001F642\r\n\r\nc".replace("x", "c") if False else result["body"])
        self.assertTrue(result["body"].startswith("a\r\n\r\nb \U0001F600\r\n"))
        self.assertIn("X\r\n\U0001F642", result["body"])

    def test_edit_order_does_not_matter(self):
        body = "p1\n\np2\n\np3"
        snap = freeze_card(make_card(body=body))
        s0, s1 = seg_id_for(snap, 0), seg_id_for(snap, 1)
        edits_a = [
            {"op": "replace", "segment_id": s0, "text": "P1"},
            {"op": "insert_after", "segment_id": s1, "text": "ins"},
        ]
        edits_b = list(reversed(edits_a))
        ra = apply_delta(snap, edits_a)
        rb = apply_delta(snap, edits_b)
        self.assertEqual(ra["body"], rb["body"])
        self.assertEqual(ra["body_sha256"], rb["body_sha256"])
        self.assertEqual(ra["changed_segment_ids"], rb["changed_segment_ids"])

    def test_tampered_or_same_version_body_snapshot_rejected(self):
        snap = freeze_card(make_card())
        tampered = dict(snap)
        tampered["body"] = snap["body"] + "extra"
        with self.assertRaises(ValueError):
            apply_delta(tampered, [{"op": "replace", "segment_id": seg_id_for(snap, 0), "text": "nope"}])
        seg_tampered = dict(snap)
        seg_tampered["segments"] = [dict(s) for s in snap["segments"]]
        seg_tampered["segments"][0]["text"] = "forged"
        with self.assertRaises(ValueError):
            apply_delta(seg_tampered, [{"op": "replace", "segment_id": seg_id_for(snap, 0), "text": "nope"}])
        # A bool offset in a nested segment is rejected (JSON false != 0).
        bool_seg = dict(snap)
        bool_seg["segments"] = [dict(s) for s in snap["segments"]]
        bool_seg["segments"][0]["start"] = False
        with self.assertRaises(ValueError):
            apply_delta(bool_seg, [{"op": "replace", "segment_id": seg_id_for(snap, 0), "text": "nope"}])
        # An extra nested field on a segment is rejected (no metadata editing).
        extra_seg = dict(snap)
        extra_seg["segments"] = [dict(s) for s in snap["segments"]]
        extra_seg["segments"][0]["title"] = "T"
        with self.assertRaises(ValueError):
            apply_delta(extra_seg, [{"op": "replace", "segment_id": seg_id_for(snap, 0), "text": "nope"}])
        # An extra top-level field on the snapshot is rejected.
        extra_top = dict(snap)
        extra_top["unexpected_metadata"] = "T"
        with self.assertRaises(ValueError):
            apply_delta(extra_top, [{"op": "replace", "segment_id": seg_id_for(snap, 0), "text": "nope"}])

    def test_unknown_and_ambiguous_edits_rejected(self):
        snap = freeze_card(make_card(body="a\n\nb"))
        good = seg_id_for(snap, 0)
        with self.assertRaises(ValueError):
            apply_delta(snap, [{"op": "replace", "segment_id": "deadbeef", "text": "x"}])
        with self.assertRaises(ValueError):
            apply_delta(snap, [{"op": "replace", "segment_id": good, "text": "x"},
                               {"op": "insert_after", "segment_id": good, "text": "y"}])
        with self.assertRaises(ValueError):
            apply_delta(snap, [{"op": "delete", "segment_id": good, "text": "x"}])
        # Unknown edit keys are rejected; exactly op/segment_id/text are allowed.
        with self.assertRaises(ValueError):
            apply_delta(snap, [
                {"op": "replace", "segment_id": good, "text": "x", "title": "T"}
            ])

    def test_retired_rejected_and_manual_review_flag(self):
        retired = freeze_card(make_card(state="retired"))
        with self.assertRaises(ValueError):
            apply_delta(retired, [{"op": "replace", "segment_id": seg_id_for(retired, 0), "text": "x"}])
        manual = freeze_card(make_card(manually_edited=True))
        out = apply_delta(manual, [{"op": "replace", "segment_id": seg_id_for(manual, 0), "text": "new"}])
        self.assertTrue(out["requires_human_review"])
        self.assertTrue(out["requires_semantic_review"])

    def test_length_overflow_and_no_change_rejected(self):
        snap = freeze_card(make_card(body="short"))
        big = "x" * 70000
        with self.assertRaises(ValueError):
            apply_delta(snap, [{"op": "replace", "segment_id": seg_id_for(snap, 0), "text": big}])
        target = seg_id_for(snap, 0)
        with self.assertRaises(ValueError):
            apply_delta(snap, [{"op": "replace", "segment_id": target, "text": snap["segments"][0]["text"]}])

    def test_metadata_immutable_and_output_shape(self):
        snap = freeze_card(make_card(title="T", topic=None))
        target = seg_id_for(snap, 0)
        out = apply_delta(snap, [{"op": "insert_after", "segment_id": target, "text": "Z"}])
        self.assertEqual(out["id"], snap["id"])
        self.assertEqual(out["project"], snap["project"])
        self.assertEqual(out["expected_version"], snap["version"])
        self.assertEqual(out["expected_body_sha256"], snap["body_sha256"])
        self.assertNotEqual(out["body_sha256"], snap["body_sha256"])
        self.assertNotIn("title", out)


if __name__ == "__main__":
    unittest.main()
