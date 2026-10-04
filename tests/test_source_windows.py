import unittest

from tongle.source_windows import (
    freeze_window,
    resolve_evidence,
    validate_dispositions,
)

import copy


def part(source_id, version, part_no, role, kind, text, **kw):
    d = {
        "source_id": source_id,
        "version": version,
        "part": part_no,
        "role": role,
        "kind": kind,
        "text": text,
    }
    d.update(kw)
    return d


class FreezeWindowTests(unittest.TestCase):
    def test_cross_role_same_text_not_merged_and_no_time_upgrade(self):
        w = freeze_window(
            "proj",
            [
                part("s1", 1, 0, "user", "chat", "hello", ledger_captured_at="2024-01-01T00:00:00Z"),
                part("s2", 1, 0, "assistant", "chat", "hello"),
            ],
        )
        self.assertNotEqual(w["parts"][0]["part_id"], w["parts"][1]["part_id"])
        self.assertEqual(w["parts"][0]["role"], "user")
        self.assertEqual(w["parts"][1]["role"], "assistant")
        # captured_at is preserved, but event_time must not be inferred.
        self.assertEqual(w["parts"][0]["ledger_captured_at"], "2024-01-01T00:00:00Z")
        self.assertIsNone(w["parts"][0]["event_time"])

    def test_crlf_chinese_emoji_reassemble_verbatim(self):
        text = "第一段\r\n\r\n第二段 🎉\r\n\r\n第三段"
        w = freeze_window("proj", [part("s1", 1, 0, "document", "doc", text)])
        spans = [s for s in w["spans"]]
        joined = "".join(s["text"] for s in spans)
        self.assertEqual(joined, text)
        for s in spans:
            self.assertEqual(text[s["start"]:s["end"]], s["text"])

    def test_id_stable_and_changes_on_text_edit(self):
        base = [part("s1", 1, 0, "document", "doc", "alpha")]
        w1 = freeze_window("proj", base)
        w2 = freeze_window("proj", base)
        self.assertEqual(w1["window_id"], w2["window_id"])
        w3 = freeze_window("proj", [part("s1", 1, 0, "document", "doc", "alpha!")])
        self.assertNotEqual(w1["window_id"], w3["window_id"])

    def test_other_project_and_bool_version_part_rejected(self):
        with self.assertRaises(ValueError):
            freeze_window(
                "proj",
                [part("s1", 1, 0, "user", "chat", "x", project="other")],
            )
        with self.assertRaises(ValueError):
            freeze_window("proj", [part("s1", True, 0, "user", "chat", "x")])
        with self.assertRaises(ValueError):
            freeze_window("proj", [part("s1", 1, False, "user", "chat", "x")])

    def test_duplicate_and_over_limit_zero_truncation(self):
        with self.assertRaises(ValueError):
            freeze_window(
                "proj",
                [
                    part("s1", 1, 0, "user", "chat", "x"),
                    part("s1", 1, 0, "user", "chat", "x"),
                ],
            )
        with self.assertRaises(ValueError):
            freeze_window("proj", [part("s1", 1, 0, "user", "chat", "abc")], max_chars=2)
        w = freeze_window("proj", [part("s1", 1, 0, "user", "chat", "abc")], max_chars=3)
        self.assertEqual(w["parts"][0]["text"], "abc")

    def test_empty_text_produces_zero_span(self):
        w = freeze_window("proj", [part("s1", 1, 0, "user", "chat", "")])
        self.assertEqual(len(w["spans"]), 1)
        s = w["spans"][0]
        self.assertEqual((s["start"], s["end"], s["text"]), (0, 0, ""))


class ResolveEvidenceTests(unittest.TestCase):
    def test_tampered_window_or_span_rejected(self):
        w = freeze_window("proj", [part("s1", 1, 0, "document", "doc", "alpha\n\nbeta")])
        span_id = w["spans"][0]["id"]
        self.assertEqual(len(resolve_evidence(w, [span_id])), 1)

        bad = freeze_window("proj", [part("s1", 1, 0, "document", "doc", "alpha\n\nbeta")])
        bad["window_id"] = "0" * 64
        with self.assertRaises(ValueError):
            resolve_evidence(bad, [span_id])

        bad2 = freeze_window("proj", [part("s1", 1, 0, "document", "doc", "alpha\n\nbeta")])
        bad2["spans"][0]["text"] = "tampered"
        with self.assertRaises(ValueError):
            resolve_evidence(bad2, [bad2["spans"][0]["id"]])

    def test_order_preserved_and_quote_is_real(self):
        text = "one\n\ntwo\n\nthree"
        w = freeze_window("proj", [part("s1", 1, 0, "document", "doc", text)])
        ids = [s["id"] for s in w["spans"]]
        rev = resolve_evidence(w, list(reversed(ids)))
        self.assertEqual([r["quote"] for r in rev], [s["text"] for s in reversed(w["spans"])])
        for r in rev:
            self.assertIn(r["quote"], text)

    def test_foreign_duplicate_blank_evidence_rejected(self):
        w = freeze_window(
            "proj",
            [
                part("s1", 1, 0, "document", "doc", "alpha"),
                part("s2", 1, 0, "document", "doc", "beta"),
            ],
        )
        sid = w["spans"][0]["id"]
        with self.assertRaises(ValueError):
            resolve_evidence(w, [sid, sid])
        with self.assertRaises(ValueError):
            resolve_evidence(w, ["deadbeef" * 8])
        empty_w = freeze_window("proj", [part("s9", 1, 0, "document", "doc", "")])
        with self.assertRaises(ValueError):
            resolve_evidence(empty_w, [empty_w["spans"][0]["id"]])


class RebuildShapeTests(unittest.TestCase):
    def _frozen(self):
        return freeze_window(
            "proj", [part("s1", 1, 0, "document", "doc", "alpha\n\nbeta")]
        )

    def test_duplicate_span_substitution_rejected(self):
        w = self._frozen()
        # Duplicating a real span (same id) must not satisfy the rebuild,
        # even though every individual field would compare equal. The exact
        # canonical shape rejects a duplicated/omitted span.
        dup = copy.deepcopy(w)
        dup["spans"] = [dup["spans"][0], dup["spans"][0]]
        with self.assertRaises(ValueError):
            resolve_evidence(dup, [w["spans"][0]["id"]])
        # Omitting a span is likewise rejected.
        omitted = copy.deepcopy(w)
        omitted["spans"] = omitted["spans"][:1]
        with self.assertRaises(ValueError):
            resolve_evidence(omitted, [w["spans"][0]["id"]])
        # Reordering spans is rejected too.
        if len(w["spans"]) > 1:
            reordered = copy.deepcopy(w)
            reordered["spans"] = list(reversed(reordered["spans"]))
            with self.assertRaises(ValueError):
                resolve_evidence(reordered, [w["spans"][0]["id"]])

    def test_extra_frozen_field_rejected(self):
        w = self._frozen()
        extra_span = copy.deepcopy(w)
        extra_span["spans"][0]["note"] = "x"
        with self.assertRaises(ValueError):
            resolve_evidence(extra_span, [w["spans"][0]["id"]])
        extra_top = copy.deepcopy(w)
        extra_top["extra"] = 1
        with self.assertRaises(ValueError):
            resolve_evidence(extra_top, [w["spans"][0]["id"]])
        missing_top = copy.deepcopy(w)
        del missing_top["protocol"]
        with self.assertRaises(ValueError):
            resolve_evidence(missing_top, [w["spans"][0]["id"]])

    def test_json_false_is_not_zero_in_bounds(self):
        # A bool offset (JSON false) must not pass for the int 0.
        with self.assertRaises(ValueError):
            freeze_window(
                "proj",
                [part(
                    "s1", 1, 0, "document", "doc", "abc",
                    source_bounds={
                        "start": False, "end": 3, "source_length": 3,
                        "text_start": 0, "text_end": 3, "partial": True,
                    },
                )],
            )

    def test_custom_cap_over_32000_round_trips(self):
        text = "x" * 40000
        w = freeze_window("proj", [part("s1", 1, 0, "document", "doc", text)], max_chars=50000)
        # Rebuild/revalidation must not impose an extra default cap.
        out = resolve_evidence(w, [w["spans"][0]["id"]])
        self.assertEqual(out[0]["quote"], text)

    def test_whitespace_identity_rejected_values_preserved(self):
        with self.assertRaises(ValueError):
            freeze_window("   ", [part("s1", 1, 0, "document", "doc", "x")])
        with self.assertRaises(ValueError):
            freeze_window("proj", [part("  ", 1, 0, "document", "doc", "x")])
        with self.assertRaises(ValueError):
            freeze_window("proj", [part("s1", 1, 0, "document", "  ", "x")])
        # A meaningful value with surrounding space is preserved verbatim.
        w = freeze_window("proj", [part(" s1 ", 1, 0, "document", " doc ", "x")])
        self.assertEqual(w["parts"][0]["source_id"], " s1 ")
        self.assertEqual(w["parts"][0]["kind"], " doc ")

    def test_event_time_null_and_not_inferred(self):
        w = freeze_window(
            "proj",
            [part("s1", 1, 0, "document", "doc", "x", ledger_captured_at="2024-01-01T00:00:00Z")],
        )
        self.assertIsNone(w["parts"][0]["event_time"])
        self.assertEqual(w["parts"][0]["ledger_captured_at"], "2024-01-01T00:00:00Z")
        # v1 has no grounded event-time contract; reject a supplied date.
        with self.assertRaises(ValueError):
            freeze_window("proj", [part("s1", 1, 0, "document", "doc", "x",
                event_time="2020-05-05T00:00:00Z")])

    def test_bounds_identity_and_invalid_bounds(self):
        bounds = {
            "start": 10, "end": 20, "source_length": 100,
            "text_start": 8, "text_end": 22, "partial": True,
        }
        text = "abcdefghijklmn"  # len == text_end - text_start == 14
        w = freeze_window(
            "proj", [part("s1", 1, 0, "document", "doc", text, source_bounds=bounds)]
        )
        self.assertEqual(w["parts"][0]["source_bounds"], bounds)
        # Unknown bounds stay None and change identity.
        w_none = freeze_window("proj", [part("s1", 1, 0, "document", "doc", text)])
        self.assertIsNone(w_none["parts"][0]["source_bounds"])
        self.assertNotEqual(w["window_id"], w_none["window_id"])

        def bad(overrides):
            b = dict(bounds)
            b.update(overrides)
            with self.assertRaises(ValueError):
                freeze_window(
                    "proj",
                    [part("s1", 1, 0, "document", "doc", text, source_bounds=b)],
                )

        # ordering violations
        bad({"text_start": 11})
        bad({"start": 21})
        bad({"end": 9})
        bad({"text_end": 21})  # text_end < end
        bad({"source_length": 21})  # text_end > source_length
        bad({"text_start": -1})
        # text length mismatch (text_end - text_start != len(text))
        bad({"text_end": 23})
        # partial inconsistency
        bad({"partial": False})
        # extra/missing keys
        with self.assertRaises(ValueError):
            freeze_window(
                "proj",
                [part("s1", 1, 0, "document", "doc", text, source_bounds=dict(bounds, extra=1))],
            )
        with self.assertRaises(ValueError):
            freeze_window(
                "proj",
                [part("s1", 1, 0, "document", "doc", text, source_bounds={"start": 0})],
            )


class ValidateDispositionsTests(unittest.TestCase):
    def test_valid_structure_does_not_mean_resolved(self):
        w = freeze_window(
            "proj",
            [part("s1", 1, 0, "document", "doc", "alpha\n\nbeta")],
        )
        pid = w["parts"][0]["part_id"]
        sids = w["parts"][0]["span_ids"]
        res = validate_dispositions(
            w,
            [{"part_id": pid, "decision": "material", "span_ids": sids, "reason": "used"}],
        )
        self.assertTrue(res["valid"])
        self.assertFalse(res["resolved"])
        self.assertEqual(res["parts"][0]["part_id"], pid)

    def test_errors_and_coverage_omission(self):
        w = freeze_window(
            "proj",
            [
                part("s1", 1, 0, "document", "doc", "alpha"),
                part("s2", 1, 0, "document", "doc", "beta"),
            ],
        )
        pids = [p["part_id"] for p in w["parts"]]
        # empty dispositions list must not be treated as correct
        with self.assertRaises(ValueError):
            validate_dispositions(w, [])
        # missing one part
        with self.assertRaises(ValueError):
            validate_dispositions(
                w,
                [{"part_id": pids[0], "decision": "covered", "span_ids": w["parts"][0]["span_ids"], "reason": "r"}],
            )
        # part with blank span only rejected
        blank = freeze_window("proj", [part("s3", 1, 0, "document", "doc", "x")])
        bp = blank["parts"][0]
        with self.assertRaises(ValueError):
            validate_dispositions(
                blank,
                [{"part_id": bp["part_id"], "decision": "material", "span_ids": [], "reason": "r"}],
            )
        # invalid decision
        with self.assertRaises(ValueError):
            validate_dispositions(
                w,
                [
                    {"part_id": pids[0], "decision": "bogus", "span_ids": w["parts"][0]["span_ids"], "reason": "r"},
                    {"part_id": pids[1], "decision": "unresolved", "span_ids": w["parts"][1]["span_ids"], "reason": "r"},
                ],
            )

    def test_unknown_disposition_key_and_whitespace_reason_rejected(self):
        w = freeze_window(
            "proj",
            [part("s1", 1, 0, "document", "doc", "alpha")],
        )
        pid = w["parts"][0]["part_id"]
        sids = w["parts"][0]["span_ids"]
        with self.assertRaises(ValueError):
            validate_dispositions(
                w,
                [{"part_id": pid, "decision": "material", "span_ids": sids,
                  "reason": "r", "extra": 1}],
            )
        with self.assertRaises(ValueError):
            validate_dispositions(
                w,
                [{"part_id": pid, "decision": "material", "span_ids": sids,
                  "reason": "   "}],
            )


if __name__ == "__main__":
    unittest.main()
