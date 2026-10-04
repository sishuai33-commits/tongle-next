"""Synthetic consumer-attribution boundary tests for the context renderer.

Fixture-only: no live vault, model, credentials, network or real personal
session text, and no real card id. These prove machine behaviour of
``tongle.context_delivery`` wording/consumer contract, never record semantics.

The defect under test: a faithful partial update may store a plain parameter
sentence derived from an assistant report. The receipt keeps the recorded role,
but the rendered_context preamble previously qualified only *raw user*
statements. The boundary must therefore also cover knowledge cards: they are
derived records, not independent verification; an assistant report is a report
and user authorization is not execution; consequential completion/current-state
claims require reading the original evidence or project state; historical
dates/status bind that record, not automatically now.
"""

import unittest

from tongle.context_delivery import (CONTEXT_BUDGET_UNITS, CONTEXT_PREAMBLE,
                                    render_context, utf16_units)

# Semantic phrases (not one exact string) that must appear together for the
# qualification to count as applied. Kept as substrings so the block may be
# reworded without silently dropping the boundary.
DERIVED_RECORD_MARKERS = ("knowledge cards", "derived")
NOT_VERIFICATION_MARKERS = ("not independent verification",)
REPORT_IS_REPORT_MARKERS = ("assistant report", "report")
AUTHORIZATION_NOT_EXECUTION_MARKERS = ("authorization", "not execution")
ORIGINAL_EVIDENCE_MARKERS = ("original evidence", "project state")
HISTORICAL_MARKERS = ("that record", "not automatically now")


def _synthetic_assistant_parameter_card():
    """A plain parameter sentence stored from an assistant report."""
    return {
        "id": "c" * 32,
        "key": "topic:agent-parameter",
        "title": "知识依据",
        "topic": "agent-parameter",
        "body": "助手汇报：参数已设置为 batch=8。",
        "version": 1,
        "updated_at": 1,
    }


class AttributionBoundaryTests(unittest.TestCase):
    def test_preamble_applies_boundary_to_knowledge_cards(self):
        low = CONTEXT_PREAMBLE.lower()
        for group in (DERIVED_RECORD_MARKERS, NOT_VERIFICATION_MARKERS,
                      REPORT_IS_REPORT_MARKERS, AUTHORIZATION_NOT_EXECUTION_MARKERS,
                      ORIGINAL_EVIDENCE_MARKERS, HISTORICAL_MARKERS):
            for phrase in group:
                self.assertIn(phrase, low)

    def test_plain_assistant_card_delivery_carries_prequalification(self):
        card = _synthetic_assistant_parameter_card()
        rendered, delivered, offered = render_context(
            [card], [], [], lambda c: "资料路径")

        # The plain assistant-derived parameter card is actually delivered as a
        # full block (its body fits), so its consumer payload must carry the
        # derived-record boundary.
        self.assertIn(card["body"], rendered)
        self.assertEqual(delivered[0]["delivery"], "full")
        self.assertEqual(offered[0]["delivery"], "full")
        self.assertEqual(offered[0]["body"], card["body"])

        # Boundary present (wording-level, not exact string equality).
        self.assertTrue(rendered.startswith(CONTEXT_PREAMBLE))
        low = rendered.lower()
        self.assertIn("derived", low)
        self.assertIn("not independent verification", low)

    def test_qualification_consumes_budget_and_retains_full_locator_honesty(self):
        # Preamble is small next to the whole-block budget, so real records
        # still fit; the qualification simply consumes part of that budget.
        self.assertLess(utf16_units(CONTEXT_PREAMBLE), CONTEXT_BUDGET_UNITS)

        # A tight budget anchored to the preamble plus a little room: one
        # delivered card must remain full, and a longer body must degrade to a
        # locator rather than slice -- never overflow.
        tight = utf16_units(CONTEXT_PREAMBLE) + 40

        small = _synthetic_assistant_parameter_card()
        rendered, delivered, offered = render_context(
            [small], [], [], lambda c: "资料路径", budget=tight)
        self.assertLessEqual(utf16_units(rendered), tight)
        self.assertEqual(delivered[0]["delivery"], "full")
        self.assertEqual(offered[0]["delivery"], "full")
        self.assertIn(small["body"], rendered)

        big = dict(small, id="d" * 32, body="助手汇报：" + "参" * 9000)
        rendered2, delivered2, offered2 = render_context(
            [big], [], [], lambda c: "资料路径", budget=tight)
        self.assertLessEqual(utf16_units(rendered2), tight)
        self.assertEqual(delivered2[0]["delivery"], "locator")
        self.assertEqual(offered2[0]["delivery"], "locator")
        self.assertEqual(offered2[0]["body"], "")
        self.assertIn("资料路径", rendered2)
        # Full and locator remain honest: the locator body is not the full body.
        self.assertNotIn(big["body"], rendered2)

    def test_empty_query_and_cards_stay_empty(self):
        rendered, delivered, offered = render_context([], [], [],
                                                      lambda c: "资料路径")
        self.assertEqual(rendered, "")
        self.assertEqual(delivered, [])
        self.assertEqual(offered, [])

        # Cards provided but none resolvable and too large for a full block:
        # nothing is delivered, so no preamble-only payload is emitted.
        big = dict(_synthetic_assistant_parameter_card(), id="e" * 32,
                   body="助手汇报：" + "参" * 9000)
        rendered2, delivered2, offered2 = render_context(
            [big], [], [], lambda c: "")
        self.assertEqual(rendered2, "")
        self.assertEqual(delivered2, [])
        self.assertEqual(offered2, [])


if __name__ == "__main__":
    unittest.main()
