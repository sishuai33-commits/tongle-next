"""Consumer live-delta repair (2026-10-01): bounded, synthetic fixtures only.

No live vault, model, credentials or network, and no real session text or card
id. Every assertion drives the actual core (``discussion_text`` /
``select_consumer_topic`` / ``Evolution.context``) so it proves machine
behaviour, not semantic quality.

Three confirmed structural gaps from a real end-to-end consumer experiment:

1. ``discussion_text`` left an output schema inert only when the request used
   ``输出``/``返回``. An ordinary Chinese answer verb (``回复``/``作答``/``以 …
   回答``) is the same request paraphrased, so the schema leaked snake_case
   field names into the lexical terms and subject exclusion. The fix treats the
   same format *noun* next to those answer verbs as the cue, while a
   *discussed* ``分析日志中的 JSON 对象`` (no answer verb) stays meaningful;
   nothing after ``JSON`` is deleted wholesale and the transform stays
   idempotent.

2. Existing ``_ACK_WORDS`` only matched a whole short word, so a full sentence
   that (a) suppresses historical/project memory or (b) asks for a bare
   acknowledgement leaked past the gate and generic project/decision overlaps
   offered cards. A bounded turn-level eligibility decision now applies to the
   selected cards *and* the raw fallback / session carry. It is scoped: an
   explicit present subject wins (``不用复述Beta旧方案，请解释Gamma防漏检测``
   keeps Gamma), discussing an acknowledgement protocol is not a request, and
   neither a bare ``收到``/``确认`` substring nor quoted source text disables.

3. Conversational Chinese framing before an explicitly named subject must not
   create a false exclusive subject, and a following conditional referral must
   still retain that subject after the schema is separated; a new named or
   unknown subject must continue to override the carry.
"""
import json
import tempfile
import unittest
from pathlib import Path

from tongle.context_retrieval import (anchor_terms, discussion_text,
                                      interpret_request, is_acknowledgement,
                                      is_continuation, is_referential_followup,
                                      query_identifier_sets,
                                      raw_statement_subject_terms,
                                      resolve_query_subject,
                                      suppresses_current_memory)
from tongle.engine import Engine
from tongle.evolution import Evolution
from tongle.knowledge_files import KnowledgeFiles


class AnswerVerbSeparationTests(unittest.TestCase):
    """Gap 1: an answer-format request scopes the schema regardless of verb."""

    QUESTION = '青蘅观测站遇到冰雹时怎样处理'
    SCHEMA = '{"inspection_role":null,"crewReady":false}'
    REQUESTS = (
        '请以 JSON 作答：',       # verb after the format noun
        '请回复以下 JSON：',      # verb before the format noun
        '请以如下形状回答：',     # another ordinary paraphrase
    )

    def test_answer_verb_requests_remove_the_schema(self):
        for request in self.REQUESTS:
            raw = self.QUESTION + '？' + request + self.SCHEMA + '。'
            with self.subTest(raw=raw):
                cleaned = discussion_text(raw)
                self.assertIn(self.QUESTION, cleaned)
                self.assertNotIn('inspection_role', cleaned)
                self.assertNotIn('crewReady', cleaned)
                # No snake_case identifier survives as a subject/term.
                self.assertEqual(query_identifier_sets(cleaned)[0], set())
                self.assertEqual(query_identifier_sets(cleaned)[1], set())

    def test_raw_turn_really_carries_the_field_identifier(self):
        raw = self.QUESTION + '？请以 JSON 作答：' + self.SCHEMA + '。'
        # The schema field really is a complete mixed-case identifier before
        # separation (so the leak would otherwise blank/hijack the subject) ...
        self.assertIn('crewready', query_identifier_sets(raw)[0])

    def test_discussed_json_is_not_a_format_request(self):
        # ``分析`` is a discussion verb, not an answer verb: the JSON object is
        # the thing being discussed and must be preserved, not deleted.
        raw = '请分析日志中的 JSON 对象 {"level":"warn","code":42} 是否异常。'
        cleaned = discussion_text(raw)
        self.assertIn('分析日志中的', cleaned)
        self.assertIn('{"level":"warn","code":42}', cleaned)
        self.assertNotIn('字段', cleaned)

    def test_no_unconstrained_deletion_after_json(self):
        # A format request removes the requested literal but keeps the real
        # discussion that follows it.
        raw = ('青蘅观测站遇到冰雹时怎样处理？请以 JSON 作答：{"count":null}。'
               '还请注意冰雹后的巡查安排。')
        cleaned = discussion_text(raw)
        self.assertIn('青蘅观测站遇到冰雹时怎样处理', cleaned)
        self.assertIn('还请注意冰雹后的巡查安排', cleaned)
        self.assertNotIn('count', cleaned)

    def test_answer_verb_separation_is_idempotent(self):
        samples = (
            '青蘅观测站遇到冰雹时怎样处理？请以 JSON 作答：' + self.SCHEMA + '。',
            '青蘅观测站遇到冰雹时怎样处理？请回复以下 JSON：' + self.SCHEMA + '。',
            '请分析日志中的 JSON 对象 {"level":"warn","code":42} 是否异常。',
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


class AnswerVerbIntegrationTests(_Harness):
    """Gap 1 end-to-end: the schema never blanks or hijacks the real subject."""

    def _cards(self):
        real = self.seed('project', '青蘅观测站冰雹处理', 'knowledge', '冰雹',
                         '青蘅观测站冰雹处理',
                         '青蘅观测站遇到冰雹时按预案处理。')
        hijack = self.seed('project', 'CrewReady事项', 'knowledge', '事项',
                           'CrewReady事项', 'CrewReady 由独立小组负责推进。')
        self.publish()
        return real, hijack

    def _assert_real_wins(self, query):
        real, hijack = self._cards()
        ids = self.ids(self.ev.context('project', query))
        self.assertIn(real['id'], ids, query)
        self.assertNotIn(hijack['id'], ids, query)

    def test_json_answer_verb_keeps_the_real_subject(self):
        self._assert_real_wins(
            '青蘅观测站遇到冰雹时怎样处理？请以 JSON 作答：'
            '{"inspection_role":null,"crewReady":false}。')

    def test_reply_verb_keeps_the_real_subject(self):
        self._assert_real_wins(
            '青蘅观测站遇到冰雹时怎样处理？请回复以下 JSON：'
            '{"inspection_role":null,"crewReady":false}。')

    def test_shape_answer_verb_keeps_the_real_subject(self):
        self._assert_real_wins(
            '青蘅观测站遇到冰雹时怎样处理？请以如下形状回答：'
            '{"inspection_role":null,"crewReady":false}。')


class MemoryEligibilityTests(_Harness):
    """Gap 2: a bounded, scoped turn-level current-memory decision."""

    ABSTAIN = '先暂停调取旧记录；这条消息只需回一句已知悉。'

    def _session(self, session='mem'):
        card = self.seed('project', '旧记录调取说明', 'knowledge', '记录',
                         '旧记录调取说明', '旧记录调取由值班流程管理。')
        self.publish()
        # A raw statement that a non-suppressed turn would surface.
        self.capture('旧记录调取情况说明，等待核对。', session='other', event='raw')
        return card

    def test_long_abstention_is_not_a_short_acknowledgement(self):
        self.assertFalse(is_acknowledgement(self.ABSTAIN))
        self.assertTrue(suppresses_current_memory(self.ABSTAIN))

    def test_long_abstention_offers_zero_cards_and_no_raw_fallback(self):
        self._session()
        got = self.ev.context('project', self.ABSTAIN, session='mem')
        self.assertEqual(got['results'], [])
        self.assertEqual(got['recent_statements'], [])
        self.assertIsNone(got['receipt_id'])
        self.assertEqual(got['rendered_context'], '')

    def test_scoped_negation_keeps_the_present_subject(self):
        gamma = self.seed('project', 'Gamma防漏检测', 'knowledge', '检测',
                          'Gamma防漏检测', 'Gamma 防漏检测由独立小组负责。')
        self.publish()
        query = '不用复述Beta旧方案，请解释Gamma防漏检测。'
        # The abstention marker is present ...
        self.assertTrue(suppresses_current_memory(query))
        # ... but an explicit present subject must win over it.
        ids = self.ids(self.ev.context('project', query))
        self.assertIn(gamma['id'], ids)

    def test_acknowledgement_protocol_discussion_is_not_a_disable(self):
        card = self.seed('project', '确认协议设计', 'knowledge', '协议',
                         '确认协议设计', '确认协议设计由现场主管维护。')
        self.publish()
        query = '请说明确认协议如何设计。'
        self.assertFalse(suppresses_current_memory(query))
        self.assertIn(card['id'], self.ids(self.ev.context('project', query)))

    def test_bare_confirmation_words_are_not_a_global_disable(self):
        self.assertFalse(suppresses_current_memory('这条记录包含收到和确认两个状态。'))

    def test_quoted_abstention_text_is_not_a_disable(self):
        query = '材料里写着“这条消息只需回一句已知悉”，请解释它的含义。'
        self.assertFalse(suppresses_current_memory(query))

    def test_quote_free_abstention_still_disables(self):
        self.assertTrue(suppresses_current_memory('这条消息只需回一句已知悉。'))


class ClauseRoleInterpretationTests(_Harness):
    """One coherent clause-role reading: a scoped negation keeps the new
    subject live (raw/correction/receipt stay on), an unscoped request blanks."""

    def test_scoped_negation_keeps_the_new_subject_live(self):
        gamma = self.seed('project', 'Gamma防漏检测', 'knowledge', '检测',
                          'Gamma防漏检测', 'Gamma 防漏检测由独立小组负责。')
        self.publish()
        q = '别复述Beta旧方案；现在解释Gamma防漏检测。'
        interp = interpret_request(q)
        self.assertFalse(interp.ack_only)
        self.assertFalse(interp.no_history)
        self.assertNotIn('Beta', interp.text)
        self.assertIn('Gamma', interp.text)
        got = self.ev.context('project', q)
        self.assertIn(gamma['id'], self.ids(got))
        self.assertIsNotNone(got['receipt_id'])

    def test_unscoped_negative_verbs_blank_everything(self):
        self.seed('project', '引用说明', 'knowledge', '引用',
                  '引用说明', '引用旧资料由值班流程管理。')
        self.publish()
        for q in ('不要引用旧资料', '请只确认收到'):
            with self.subTest(q=q):
                got = self.ev.context('project', q)
                self.assertEqual(got['results'], [], q)
                self.assertEqual(got['recent_statements'], [], q)
                self.assertEqual(got['notices'], [], q)
                self.assertIsNone(got['receipt_id'], q)
                self.assertEqual(got['rendered_context'], '', q)

    def test_bare_reply_frame_alone_is_not_a_disable(self):
        card = self.seed('project', '冷链密封许可', 'knowledge', '物流',
                         '冷链密封许可', '冷链密封许可的有效条件。')
        self.publish()
        q = '只需回一句：冷链密封许可怎么处理'
        self.assertFalse(interpret_request(q).ack_only)
        self.assertIn(card['id'], self.ids(self.ev.context('project', q)))


class FramingCarryTests(_Harness):
    """Gap 3: framing + named subject survives, and a conditional referral keeps it."""

    TURN1 = '咱们继续核查绯杉仓库盘点，返回 JSON 字段：{"count":null}。'

    def _thread(self, session):
        target = self.seed('project', '绯杉仓库盘点', 'knowledge', '仓储',
                           '绯杉仓库盘点', '绯杉仓库盘点由夜班负责复核。')
        self.publish()
        self.capture(self.TURN1, session=session, event='p1')
        return target

    def test_framing_is_not_a_false_exclusive_subject(self):
        target = self.seed('project', '绯杉仓库盘点', 'knowledge', '仓储',
                           '绯杉仓库盘点', '绯杉仓库盘点由夜班负责复核。')
        self.publish()
        cleaned = discussion_text(self.TURN1)
        self.assertIn('绯杉仓库盘点', cleaned)
        self.assertNotIn('count', cleaned)
        terms_, strong, blank = resolve_query_subject(cleaned, [target])
        self.assertFalse(blank)
        self.assertEqual(strong, [])
        self.assertFalse(is_continuation(self.TURN1))

    def test_conditional_referral_retains_the_named_subject(self):
        target = self._thread('g1')
        query = '那若标签破损，该怎么处理？'
        self.assertTrue(is_referential_followup(query))
        self.assertIn(target['id'],
                      self.ids(self.ev.context('project', query, session='g1')))

    def test_named_new_subject_overrides_the_carry(self):
        old = self._thread('g2')
        mapledock = self.seed('project', 'MapleDock标签破损处理', 'knowledge', '标签',
                              'MapleDock标签破损处理', 'MapleDock 的标签破损按新规则处理。')
        self.publish()
        query = '那若 MapleDock 标签破损呢，该怎么处理？'
        ids = self.ids(self.ev.context('project', query, session='g2'))
        self.assertIn(mapledock['id'], ids)
        self.assertNotIn(old['id'], ids)

    def test_unknown_new_subject_blanks_instead_of_carrying(self):
        old = self._thread('g3')
        query = '那若 ZorBlax 标签破损呢，该怎么处理？'
        got = self.ev.context('project', query, session='g3')
        self.assertEqual(got['results'], [])
        self.assertNotIn(old['id'], self.ids(got))


class ScopedNegatedSubjectNonLeakTests(_Harness):
    """A negated old-subject clause must never feed direct selection, the raw
    fallback or the session anchor with that old subject."""

    QUERY = '不要接续麦桥影社海报安排，只说明白榆测绘队作业准则。'

    def _cards(self):
        old = self.seed('project', '麦桥影社海报安排', 'knowledge', '海报',
                        '麦桥影社海报安排', '麦桥影社海报安排说明。')
        survey = self.seed('project', '白榆测绘队作业准则', 'knowledge', '准则',
                           '白榆测绘队作业准则', '白榆测绘队作业准则说明。')
        self.publish()
        return old, survey

    def test_negated_subject_never_feeds_raw_or_anchor_terms(self):
        old, survey = self._cards()
        terms_ = raw_statement_subject_terms(self.QUERY, [old, survey], [])
        self.assertTrue(terms_)
        self.assertFalse(any(t in {'麦桥', '桥影', '影社'} for t in terms_))
        self.assertTrue(any(t in {'白榆', '榆测', '测绘'} for t in terms_))
        anchor = anchor_terms([(self.QUERY, 1.0, 's1')])
        self.assertFalse(any(t in {'麦桥', '桥影', '影社'} for t in anchor))

    def test_negated_subject_never_enters_direct_selection(self):
        old, survey = self._cards()
        ids = self.ids(self.ev.context('project', self.QUERY))
        self.assertIn(survey['id'], ids)
        self.assertNotIn(old['id'], ids)

    def test_negated_clause_is_masked_from_the_retrieval_text(self):
        interp = interpret_request(self.QUERY)
        self.assertFalse(interp.ack_only)
        self.assertFalse(interp.no_history)
        self.assertNotIn('麦桥影社', interp.text)
        self.assertIn('白榆测绘队', interp.text)


if __name__ == '__main__':
    unittest.main()
