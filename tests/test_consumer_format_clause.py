"""Regression tests for the clause-local format-cue boundary in ``_cue_before``.

The confirmed defect: a format cue looked back across a *comma*, so in
``请输出 JSON 格式的结果，这个对象 {"AlphaGlade":1} 合法吗？`` the output cue
``请输出 JSON`` reached into the next clause and the *discussed* object literal
was erased as if it were a response schema. The semicolon form and an object
with no format cue already worked.

The repair makes a comma close the cue's clause, while keeping the two
inert cases that must still be removed:

* a literal directly adjacent to the cue across the comma (``输出 JSON，{...}``
  -- nothing but a pause between cue and literal);
* a literal re-attached by mere format metalanguage after the comma
  (``…输出形状，不是讨论对象：{...}``).

A true response schema ``请严格输出 JSON：{"AlphaGlade":"回答字段"}。`` must
STILL be removed while the real ``CopperNest`` discussion after it is kept.

Every fixture is invented synthetic text: no live vault, model, credentials or
network, no real session text and no real card id. Nothing here is hardcoded to
the specific identifiers; they are arbitrary placeholders.
"""
import unittest

from tongle.context_query import discussion_text


class CommaClauseBoundaryTests(unittest.TestCase):
    """A cue must not cross a comma into a clause that discusses the object."""

    DISCUSSED = '请输出 JSON 格式的结果，这个对象 {"AlphaGlade":1} 合法吗？'

    def test_discussed_object_after_comma_is_preserved(self):
        cleaned = discussion_text(self.DISCUSSED)
        # The actual discussed object literal survives verbatim ...
        self.assertIn('{"AlphaGlade":1}', cleaned)
        # ... and so does the genuine discussion around it.
        self.assertIn('这个对象', cleaned)
        self.assertIn('合法吗', cleaned)

    def test_semicolon_form_still_preserved(self):
        raw = '请输出 JSON 格式的结果；这个对象 {"AlphaGlade":1} 合法吗？'
        cleaned = discussion_text(raw)
        self.assertIn('{"AlphaGlade":1}', cleaned)
        self.assertIn('合法吗', cleaned)

    def test_object_without_any_format_cue_is_preserved(self):
        raw = '这个对象 {"AlphaGlade":1} 合法吗？'
        self.assertIn('{"AlphaGlade":1}', discussion_text(raw))

    def test_whole_turn_is_not_blanked(self):
        self.assertTrue(discussion_text(self.DISCUSSED).strip())


class TrueSchemaStillRemovedTests(unittest.TestCase):
    """A genuine response schema is still suppressed; the discussion stays."""

    SCHEMA_TURN = ('请严格输出 JSON：{"AlphaGlade":"回答字段"}。'
                   '现在解释 CopperNest 准则。')

    def test_colon_attached_schema_is_removed(self):
        cleaned = discussion_text(self.SCHEMA_TURN)
        self.assertNotIn('AlphaGlade', cleaned)
        self.assertNotIn('回答字段', cleaned)

    def test_real_discussion_after_schema_is_retained(self):
        cleaned = discussion_text(self.SCHEMA_TURN)
        self.assertIn('现在解释', cleaned)
        self.assertIn('CopperNest', cleaned)
        self.assertIn('准则', cleaned)

    def test_schema_adjacent_to_cue_across_comma_is_removed(self):
        # Only a pause between the cue and the literal: still an output shape.
        raw = '请输出 JSON，{"AlphaGlade":null}。现在解释 CopperNest 准则。'
        cleaned = discussion_text(raw)
        self.assertNotIn('AlphaGlade', cleaned)
        self.assertIn('CopperNest', cleaned)

    def test_label_lead_in_after_comma_is_removed(self):
        # The text after the comma is itself inert format metalanguage, so the
        # cue still scopes the literal it labels.
        raw = ('请先约定输出形状，不是讨论对象：{"AlphaGlade":null}。'
               '现在解释 CopperNest 准则。')
        cleaned = discussion_text(raw)
        self.assertNotIn('AlphaGlade', cleaned)
        self.assertIn('CopperNest', cleaned)


class ClauseBoundaryIdempotenceTests(unittest.TestCase):
    """The transform stays a no-op on already-separated text."""

    SAMPLES = (
        '请输出 JSON 格式的结果，这个对象 {"AlphaGlade":1} 合法吗？',
        '请输出 JSON 格式的结果；这个对象 {"AlphaGlade":1} 合法吗？',
        '请严格输出 JSON：{"AlphaGlade":"回答字段"}。现在解释 CopperNest 准则。',
        '请先约定输出形状，不是讨论对象：{"AlphaGlade":null}。'
        '现在解释 CopperNest 准则。',
        '请输出 JSON，{"AlphaGlade":null}。现在解释 CopperNest 准则。',
        '这个对象 {"AlphaGlade":1} 合法吗？',
    )

    def test_discussion_text_is_idempotent(self):
        for raw in self.SAMPLES:
            with self.subTest(raw=raw):
                once = discussion_text(raw)
                self.assertEqual(discussion_text(once), once)


if __name__ == '__main__':
    unittest.main()
