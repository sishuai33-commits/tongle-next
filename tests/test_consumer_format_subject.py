"""Synthetic tests for the consumer output-format/schema separation repair.

The confirmed defect: ``resolve_query_subject`` scanned the *entire* turn for
ASCII tokens, so an output JSON field name or an XML tag was read as an
"explicit unknown subject" -- blanking the real topic -- and, worse, a schema
field that happened to name an existing card promoted THAT card over the actual
subject. The repair separates the requested discussion from explicitly inert
output-format/schema/label spans before any consumer lexical scoring, subject
detection or continuation detection.

Every fixture is invented synthetic text: no live vault, model, credentials or
network, no real session text and no real card id. Tests drive the actual core
context (``Evolution.context`` / ``select_consumer_topic``) so they prove
machine behaviour, not semantic quality.

The remaining observed failures were two *mechanisms* rather than field-name
stopwords:

1. a declared-field connector list (``字段叫 canopyAction 与 latchCheck``) left
   unknown identifiers in the lexical turn, blanking the real subject -- the
   connector grammar is parsed instead;
2. an inert FORMAT CLAUSE (``先约定输出 JSON 形状`` / ``下面只约定输出形状，不是新讨论对象：``)
   left the generic word ``约定`` behind after the curly contents were removed,
   matching unrelated headings such as ``BirchRelay上线约定`` -- the clause itself
   is excluded, while real discussion before and after it is preserved.

Structural content is no longer erased unconditionally: a fenced ``BirchRelay()``
being explained, a discussed JSON configuration and an explicitly discussed
``<BirchRelay/>`` component are kept, so the subject survives; only an explicitly
requested output/schema literal is suppressed.
"""
import json
import tempfile
import unittest
from pathlib import Path

from tongle.context_retrieval import (discussion_text, query_identifier_sets,
                                      raw_statement_subject_terms,
                                      resolve_query_subject, specific_terms)
from tongle.engine import Engine
from tongle.evolution import Evolution
from tongle.knowledge_files import KnowledgeFiles


class FormatSeparationTests(unittest.TestCase):
    """The separation removes inert syntax and keeps the discussed subject."""

    JSON_AFTER = ('海棠试验场降雪后谁负责巡查？'
                  '请输出 JSON：{"inspection_role":null,"crewReady":false}。')

    def test_inline_json_is_separated_from_the_question(self):
        raw = self.JSON_AFTER
        # The schema field names really are complete tokens in the raw turn ...
        self.assertIn('crewready', query_identifier_sets(raw)[0])
        # ... and separating the discussion drops them while keeping the ask.
        cleaned = discussion_text(raw)
        self.assertNotIn('crewReady', cleaned)
        self.assertNotIn('inspection_role', cleaned)
        self.assertIn('海棠试验场降雪后谁负责巡查', cleaned)
        self.assertEqual(query_identifier_sets(cleaned)[0], set())

    def test_format_statement_before_the_question(self):
        raw = ('先约定输出形状 {"inspection_role":null}。'
               '然后回答：海棠试验场降雪后谁负责巡查？')
        cleaned = discussion_text(raw)
        self.assertNotIn('inspection_role', cleaned)
        self.assertIn('海棠试验场降雪后谁负责巡查', cleaned)

    def test_xml_tags_are_data(self):
        raw = '刚才那个怎么样？用 XML 输出 <next_check></next_check>。'
        cleaned = discussion_text(raw)
        self.assertNotIn('next_check', cleaned)
        self.assertIn('刚才那个怎么样', cleaned)

    def test_fenced_schema_field_is_not_a_subject(self):
        raw = ('试验场规则是什么？以下只是输出形状，不是讨论对象：\n'
               '```json\n{"CedarFlow": null}\n```。')
        # The raw turn really carries the schema field as an identifier ...
        self.assertIn('cedarflow', query_identifier_sets(raw)[0])
        # ... the separated discussion carries no subject at all.
        cleaned = discussion_text(raw)
        self.assertNotIn('CedarFlow', cleaned)
        self.assertEqual(query_identifier_sets(cleaned)[0], set())
        self.assertEqual(query_identifier_sets(cleaned)[1], set())
        self.assertIn('试验场规则', cleaned)

    def test_backticked_label_field_is_not_a_subject(self):
        raw = '船坞规则是什么？输出字段名暂用 `CedarFlow`，这只是标签。'
        self.assertIn('cedarflow', query_identifier_sets(raw)[0])
        cleaned = discussion_text(raw)
        self.assertNotIn('CedarFlow', cleaned)
        self.assertIn('船坞规则', cleaned)

    def test_declared_field_list_is_data(self):
        raw = '船坞规则是什么？输出字段：inspection_role、crewReady，不是讨论对象。'
        cleaned = discussion_text(raw)
        self.assertNotIn('inspection_role', cleaned)
        self.assertNotIn('crewReady', cleaned)
        self.assertIn('船坞规则', cleaned)

    def test_discussed_code_identifier_is_preserved(self):
        # A code identifier explicitly discussed as the subject has no
        # output/format cue and must NOT be erased.
        raw = '当前主题是代码 `CedarFlow()` 对应组件，请说明它的运行约定。'
        self.assertIn('CedarFlow', discussion_text(raw))

    def test_discussed_backticked_name_is_preserved(self):
        raw = '换个话题，讨论 `HazeSpinner` 本身。'
        self.assertIn('HazeSpinner', discussion_text(raw))

    def test_discussed_plain_identifier_is_preserved(self):
        # A bare (un-backticked) discussed identifier with no field cue/connector
        # is never a declared field and must be kept.
        raw = '当前主题是代码 CedarFlow 对应组件，请说明它的运行约定。'
        self.assertIn('CedarFlow', discussion_text(raw))

    def test_schema_field_is_not_a_raw_subject_anchor(self):
        cedar = {'id': 'c' * 32, 'project': 'project', 'key': 'CedarFlow事项',
                 'title': 'CedarFlow事项', 'topic': '事项',
                 'body': 'CedarFlow 说明。', 'kind': 'knowledge',
                 'version': 1, 'updated_at': 1}
        q = '船坞规则是什么？输出字段名暂用 `CedarFlow`，这只是标签。'
        # The *same* separated discussion drives the raw-statement subject gate.
        self.assertEqual(raw_statement_subject_terms(q, [cedar]), set())
        terms_, _strong, blank = resolve_query_subject(q, [cedar])
        self.assertEqual(terms_, set())
        self.assertFalse(blank)


class DeclaredFieldConnectorTests(unittest.TestCase):
    """Mechanism 1: a finite declared-field connector list is parsed.

    The identifiers named by ``字段叫 X 与 Y`` are field declarations, not new
    discussion objects. They are removed so an unknown field name can no longer
    blank the real subject; a code identifier with no cue/connector stays.
    """

    QUESTION = '青蘅观测站遇到冰雹时怎样处理？'
    FIELDS = '返回 JSON，字段叫 canopyAction 与 latchCheck。'

    def test_connector_declared_fields_are_removed(self):
        cleaned = discussion_text(self.QUESTION + self.FIELDS)
        self.assertNotIn('canopyAction', cleaned)
        self.assertNotIn('latchCheck', cleaned)
        self.assertIn('青蘅观测站遇到冰雹时怎样处理', cleaned)

    def test_unknown_declared_fields_do_not_blank_the_subject(self):
        raw = self.QUESTION + self.FIELDS
        # Before separation the field names really are complete identifiers ...
        strict, _acronyms = query_identifier_sets(raw)
        self.assertIn('canopyaction', strict)
        self.assertIn('latchcheck', strict)
        # ... and the separated discussion carries no unknown ASCII subject.
        self.assertEqual(query_identifier_sets(discussion_text(raw))[0], set())

    def test_finite_connectors_and_joiners(self):
        cases = (
            '船坞规则是什么？字段叫 alpha 与 beta。',
            '船坞规则是什么？字段为 alpha、beta。',
            '船坞规则是什么？字段是 alpha, beta。',
            '船坞规则是什么？字段名为 alpha 和 beta，不是讨论对象。',
        )
        for raw in cases:
            with self.subTest(raw=raw):
                cleaned = discussion_text(raw)
                self.assertIn('船坞规则是什么', cleaned)
                self.assertNotIn('alpha', cleaned)
                self.assertNotIn('beta', cleaned)

    def test_declared_field_parse_never_deletes_discussed_identifiers(self):
        # No field cue/connector: a discussed identifier is preserved verbatim.
        raw = '当前主题是代码 CedarFlow 对应组件，请说明它的运行约定。'
        self.assertIn('CedarFlow', discussion_text(raw))
        raw2 = '换个话题，讨论 `HazeSpinner` 本身。'
        self.assertIn('HazeSpinner', discussion_text(raw2))


class FormatClauseSeparationTests(unittest.TestCase):
    """Mechanism 2: an inert FORMAT CLAUSE is excluded, not only its contents."""

    BEFORE = ('先约定输出 JSON 形状 {"status":null}。'
              '然后回答：青蘅观测站遇到冰雹时怎样处理？')
    AFTER = ('青蘅观测站遇到冰雹时怎样处理？下面只约定输出形状，不是新讨论对象：\n'
             '```json\n{"status":null}\n```。')

    def test_format_clause_before_the_question_is_excluded(self):
        cleaned = discussion_text(self.BEFORE)
        self.assertIn('青蘅观测站遇到冰雹时怎样处理', cleaned)
        self.assertNotIn('约定', cleaned)
        self.assertNotIn('约定', specific_terms(self.BEFORE))

    def test_format_clause_after_the_question_is_excluded(self):
        cleaned = discussion_text(self.AFTER)
        self.assertIn('青蘅观测站遇到冰雹时怎样处理', cleaned)
        self.assertNotIn('约定', cleaned)
        self.assertNotIn('约定', specific_terms(self.AFTER))

    def test_discussion_before_and_after_is_preserved(self):
        raw = ('青蘅观测站遇到冰雹时怎样处理？先约定输出形状 {"status":null}。'
               '还请注意冰雹后的巡查安排。')
        cleaned = discussion_text(raw)
        self.assertIn('青蘅观测站遇到冰雹时怎样处理', cleaned)
        self.assertIn('冰雹后的巡查安排', cleaned)
        self.assertNotIn('约定', cleaned)

    def test_whole_turn_is_not_blanked(self):
        for raw in (self.BEFORE, self.AFTER):
            with self.subTest(raw=raw):
                self.assertTrue(discussion_text(raw).strip())


class StructuralScopeUnitTests(unittest.TestCase):
    """Structure is suppressed only under a cue, and preserved when discussed."""

    def test_fenced_discussed_code_is_kept(self):
        raw = '请解释下面代码里的组件：\n```python\nBirchRelay()\n```\n它负责什么？'
        self.assertIn('BirchRelay', discussion_text(raw))

    def test_discussed_json_config_is_kept(self):
        raw = '请说明 BirchRelay 的 JSON 配置：{"retry":"fast"} 各字段含义。'
        cleaned = discussion_text(raw)
        self.assertIn('BirchRelay', cleaned)
        self.assertIn('retry', cleaned)

    def test_discussed_xml_component_is_kept(self):
        raw = '请说明 <BirchRelay/> 组件的用途。'
        self.assertIn('BirchRelay', discussion_text(raw))

    def test_output_fenced_schema_is_suppressed(self):
        raw = '请输出格式如下：\n```json\n{"CedarFlow": null}\n```'
        self.assertNotIn('CedarFlow', discussion_text(raw))

    def test_discussion_text_is_idempotent(self):
        samples = (
            '青蘅观测站遇到冰雹时怎样处理？返回 JSON，字段叫 canopyAction 与 latchCheck。',
            FormatClauseSeparationTests.BEFORE,
            FormatClauseSeparationTests.AFTER,
            '当前主题是代码 `CedarFlow()` 对应组件，请说明它的运行约定。',
            '请解释下面代码里的组件：\n```python\nBirchRelay()\n```\n它负责什么？',
        )
        for raw in samples:
            with self.subTest(raw=raw):
                once = discussion_text(raw)
                self.assertEqual(discussion_text(once), once)


class _Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.e = Engine(self.root / 'state')
        self.e.init(self.root / 'vault'); self.addCleanup(self.e.store.close)
        (self.e.home / 'config.json').write_text(json.dumps(
            {'knowledge_mode': 'evolving', 'model_command': ['stub'],
             'knowledge_quiet_seconds': 0}))
        self.ev = Evolution(self.e)

    def seed(self, project, key, kind, topic, title, body, **kw):
        return self.ev.cards.seed(project, key, kind, topic, title, body, **kw)

    def publish(self):
        return KnowledgeFiles(self.e, self.ev.cards).publish()

    def capture(self, text, event='e', role='user', session='session',
                project='project'):
        rec = self.e.store.capture(project, session, role, event, text)
        self.ev.captured(rec['source_id'], role, session)
        return rec

    def ids(self, got):
        return [r['id'] for r in got['results']]


class FormatIntegrationTests(_Harness):
    """End-to-end: inert output syntax never blanks or hijacks the topic."""

    def test_topic_survives_inline_json(self):
        row = self.seed('project', '海棠试验场降雪巡查', 'knowledge', '巡查',
                        '海棠试验场降雪巡查', '海棠试验场降雪后由夜班负责巡查。')
        json_card = self.seed('project', 'CedarFlow事项', 'knowledge', '事项',
                              'CedarFlow事项', 'CedarFlow 由独立小组负责推进。')
        self.publish()
        q = ('海棠试验场降雪后谁负责巡查？'
             '请输出 JSON：{"inspection_role":null,"crewReady":false}。')
        ids = self.ids(self.ev.context('project', q))
        self.assertIn(row['id'], ids)
        self.assertNotIn(json_card['id'], ids)

    def test_topic_survives_format_statement_before_question(self):
        row = self.seed('project', '海棠试验场降雪巡查', 'knowledge', '巡查',
                        '海棠试验场降雪巡查', '海棠试验场降雪后由夜班负责巡查。')
        self.publish()
        q = ('先约定输出形状 {"inspection_role":null}。'
             '然后回答：海棠试验场降雪后谁负责巡查？')
        self.assertIn(row['id'], self.ids(self.ev.context('project', q)))

    def test_referral_after_xml_format_recovers_prior_subject(self):
        row = self.seed('project', '海试安排事项', 'knowledge', '事项',
                        '海试安排事项', '海试安排由独立小组负责。')
        self.publish()
        self.capture('本轮讨论海试安排，海试安排由独立小组负责。',
                     session='fmt-xml', event='p1')
        q = '刚才那个怎么样？用 XML 输出 <next_check></next_check>。'
        ids = self.ids(self.ev.context('project', q, session='fmt-xml'))
        self.assertIn(row['id'], ids)

    def test_fenced_schema_field_named_like_a_card_does_not_promote_it(self):
        row = self.seed('project', '试验场规则说明', 'knowledge', '规则',
                        '试验场规则说明', '试验场规则由现场主管维护。')
        hijack = self.seed('project', 'CedarFlow事项', 'knowledge', '事项',
                           'CedarFlow事项', 'CedarFlow 由独立小组负责推进。')
        self.publish()
        q = ('试验场规则是什么？以下只是输出形状，不是讨论对象：\n'
             '```json\n{"CedarFlow": null}\n```。')
        ids = self.ids(self.ev.context('project', q))
        self.assertIn(row['id'], ids)
        self.assertNotIn(hijack['id'], ids)

    def test_backticked_field_name_does_not_promote_that_card(self):
        row = self.seed('project', '船坞规则说明', 'knowledge', '规则',
                        '船坞规则说明', '船坞规则由值班员维护。')
        hijack = self.seed('project', 'CedarFlow事项', 'knowledge', '事项',
                           'CedarFlow事项', 'CedarFlow 由独立小组负责推进。')
        self.publish()
        q = '船坞规则是什么？输出字段名暂用 `CedarFlow`，这只是标签。'
        ids = self.ids(self.ev.context('project', q))
        self.assertIn(row['id'], ids)
        self.assertNotIn(hijack['id'], ids)

    def test_discussed_code_identifier_remains_the_subject(self):
        target = self.seed('project', 'CedarFlow组件', 'knowledge', '组件',
                           'CedarFlow组件', 'CedarFlow 的运行约定。')
        self.publish()
        q = '当前主题是代码 `CedarFlow()` 对应组件，请说明它的运行约定。'
        self.assertIn(target['id'], self.ids(self.ev.context('project', q)))

    def test_switch_topic_to_backticked_name_remains_the_subject(self):
        target = self.seed('project', 'HazeSpinner组件', 'knowledge', '组件',
                           'HazeSpinner组件', 'HazeSpinner 本身说明。')
        self.publish()
        q = '换个话题，讨论 `HazeSpinner` 本身。'
        self.assertIn(target['id'], self.ids(self.ev.context('project', q)))

    def test_named_subject_with_trailing_json_remains_the_subject(self):
        target = self.seed('project', 'CedarFlow组件', 'knowledge', '组件',
                           'CedarFlow组件', 'CedarFlow 的运行约定。')
        self.publish()
        q = '现在改谈 CedarFlow；输出 JSON:{"ready_flag":null}。'
        self.assertIn(target['id'], self.ids(self.ev.context('project', q)))

    def test_new_concrete_subject_outranks_old_thread_despite_json(self):
        old = self.seed('project', '海试安排事项', 'knowledge', '事项',
                        '海试安排事项', '海试安排由独立小组负责。')
        new = self.seed('project', '海棠试验场降雪巡查', 'knowledge', '巡查',
                        '海棠试验场降雪巡查', '海棠试验场降雪后由夜班负责巡查。')
        self.publish()
        self.capture('本轮讨论海试安排的事项。', session='fmt-new', event='p1')
        q = ('海棠试验场降雪后谁负责巡查？'
             '请输出 JSON：{"inspection_role":null,"crewReady":false}。')
        ids = self.ids(self.ev.context('project', q, session='fmt-new'))
        self.assertIn(new['id'], ids)
        self.assertNotIn(old['id'], ids)

    def test_explicit_unknown_subject_still_blanks_despite_format(self):
        generic = self.seed('project', '试验场规则说明', 'knowledge', '规则',
                            '试验场规则说明', '试验场规则由现场主管维护。')
        self.publish()
        got = self.ev.context('project', '请说明 ZorBlax 的事项，输出 JSON:{"x":null}。')
        self.assertEqual(got['results'], [])
        self.assertNotIn(generic['id'], self.ids(got))


class DeclaredFieldIntegrationTests(_Harness):
    """Mechanism 1 end-to-end: fields no longer blank or hijack a subject."""

    def test_declared_fields_keep_the_real_subject(self):
        real = self.seed('project', '青蘅观测站冰雹处理', 'knowledge', '冰雹',
                         '青蘅观测站冰雹处理',
                         '青蘅观测站遇到冰雹时按预案处理。')
        field_card = self.seed('project', 'CanopyAction事项', 'knowledge', '事项',
                               'CanopyAction事项', 'CanopyAction 有独立说明。')
        self.publish()
        q = ('青蘅观测站遇到冰雹时怎样处理？'
             '返回 JSON，字段叫 canopyAction 与 latchCheck。')
        ids = self.ids(self.ev.context('project', q))
        self.assertIn(real['id'], ids)
        self.assertNotIn(field_card['id'], ids)


class FormatClauseIntegrationTests(_Harness):
    """Mechanism 2 end-to-end: a format clause never summons ``约定`` cards."""

    def _cards(self):
        real = self.seed('project', '青蘅观测站冰雹处理', 'knowledge', '冰雹',
                         '青蘅观测站冰雹处理',
                         '青蘅观测站遇到冰雹时按预案处理。')
        relay = self.seed('project', 'BirchRelay上线约定', 'knowledge', '约定',
                          'BirchRelay上线约定', 'BirchRelay 上线约定说明。')
        dock = self.seed('project', '栖月船坞夜航约定', 'knowledge', '约定',
                         '栖月船坞夜航约定', '栖月船坞夜航约定说明。')
        self.publish()
        return real, relay, dock

    def test_format_clause_before_the_question(self):
        real, relay, dock = self._cards()
        q = ('先约定输出 JSON 形状 {"status":null}。'
             '然后回答：青蘅观测站遇到冰雹时怎样处理？')
        ids = self.ids(self.ev.context('project', q))
        self.assertIn(real['id'], ids)
        self.assertNotIn(relay['id'], ids)
        self.assertNotIn(dock['id'], ids)

    def test_format_clause_after_the_question(self):
        real, relay, dock = self._cards()
        q = ('青蘅观测站遇到冰雹时怎样处理？下面只约定输出形状，不是新讨论对象：\n'
             '```json\n{"status":null}\n```。')
        ids = self.ids(self.ev.context('project', q))
        self.assertIn(real['id'], ids)
        self.assertNotIn(relay['id'], ids)
        self.assertNotIn(dock['id'], ids)


class DiscussedStructureIntegrationTests(_Harness):
    """A discussed code/data literal stays the subject; output does not."""

    def test_discussed_fenced_code_selects_its_component(self):
        target = self.seed('project', 'BirchRelay组件', 'knowledge', '组件',
                           'BirchRelay组件', 'BirchRelay 是一个组件。')
        self.publish()
        q = ('请解释下面代码里的组件：\n```python\nBirchRelay()\n```\n它负责什么？')
        self.assertIn(target['id'], self.ids(self.ev.context('project', q)))

    def test_discussed_json_config_selects_its_component(self):
        target = self.seed('project', 'BirchRelay组件', 'knowledge', '组件',
                           'BirchRelay组件', 'BirchRelay 的配置说明。')
        self.publish()
        q = '请说明 BirchRelay 的 JSON 配置：{"retry":"fast"} 各字段含义。'
        self.assertIn(target['id'], self.ids(self.ev.context('project', q)))

    def test_discussed_xml_component_selects_it(self):
        target = self.seed('project', 'BirchRelay组件', 'knowledge', '组件',
                           'BirchRelay组件', 'BirchRelay 组件说明。')
        self.publish()
        q = '请说明 <BirchRelay/> 组件的用途。'
        self.assertIn(target['id'], self.ids(self.ev.context('project', q)))

    def test_unknown_discussed_code_subject_still_blanks(self):
        generic = self.seed('project', '组件说明', 'knowledge', '组件',
                            '组件说明', '通用组件说明。')
        self.publish()
        q = '请解释下面代码里的组件：\n```python\nZorBlax()\n```\n它负责什么？'
        got = self.ev.context('project', q)
        self.assertEqual(got['results'], [])
        self.assertNotIn(generic['id'], self.ids(got))

    def test_output_schema_literal_is_suppressed(self):
        target = self.seed('project', '试验场规则说明', 'knowledge', '规则',
                           '试验场规则说明', '试验场规则由现场主管维护。')
        hijack = self.seed('project', 'BirchRelay组件', 'knowledge', '组件',
                           'BirchRelay组件', 'BirchRelay 说明。')
        self.publish()
        q = ('试验场规则是什么？以下只是输出形状，不是讨论对象：\n'
             '```json\n{"BirchRelay": null}\n```。')
        ids = self.ids(self.ev.context('project', q))
        self.assertIn(target['id'], ids)
        self.assertNotIn(hijack['id'], ids)


if __name__ == '__main__':
    unittest.main()
