"""Focused consumer topic-focus tests (2026-09-30 repair).

Synthetic fixtures only: no live vault, model, credentials or network, and no
real personal session text or card id. Where a card is seeded it proves machine
behaviour, not semantic quality. The independent replay found the core concept
still faulty even with the earlier gating groups green:

1. an explicit named subject (an English identifier/acronym) must outweigh the
   generic action words it shares with a *different* subject, so
   ``FinchCore`` selects its own card and not a shared ``卡片盒产品化`` card;
   complete-token matching (``ART`` never matches inside ``party``), several
   named subjects at once, an unknown named subject (no generic fallback) and a
   body-only supplement naming the same subject are all covered;
2. a genuine referential follow-up with no fresh concrete subject must recover
   the same-session subject *before* a generic title overlap is treated as a
   fresh topic (so the weak workbook body never rides along);
3. the bounded anchor keeps the real subject (identifier evidence first) rather
   than the first sorted CJK bigrams of one long newest turn;
4. ordinary short Chinese direct questions still keep their useful matches,
   shared generic-only ``方案/计划/流程`` stay empty, and a clear new subject
   wins even when it is absent from the cards;
5. a strong current subject match does not let an unrelated weak card ride along
   on one common body term, while a full-body match to the *same* subject
   remains usable;
6. a *cohesive* subject resolution drives both selection and the raw-statement
   fallback: a Chinese subject named before 的 (or among comparison subjects) is
   taken from the leading phrase so a different card that only shares the
   trailing action words is not chosen; a body-only card naming the same
   subject is still kept; an unknown ASCII name blanks in every letter case; an
   ordinary English instruction verb is never a subject; deictic/adverbial and
   second-person META follow-ups recover the current thread subject while a
   concrete new subject (or a plain assistant address) does not. Every fixture
   below is invented synthetic text, never a real session or card id.
"""
import json
import tempfile
import unittest
from pathlib import Path

from tongle.engine import Engine
from tongle.evolution import Evolution
from tongle.knowledge_files import KnowledgeFiles
from tongle.context_retrieval import (anchor_terms, is_continuation,
                                      is_referential_followup, query_identifiers,
                                      rank_consumer, recover_subject_cards,
                                      resolve_query_subject, select_consumer_topic,
                                      specific_terms, subject_identifiers)


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


class ExplicitNamedSubjectTests(_Harness):
    """Case 1: a precise explicit named subject beats generic action words."""

    QUERY = '昨天 FinchCore 做了产品化升级，你帮我看下效果如何'

    def _base(self):
        own = self.seed('project', 'FinchCore产品升级', 'knowledge', '升级',
                        'FinchCore产品升级', 'FinchCore 产品升级的效果边界。')
        shared = self.seed('shared', '卡片盒产品化', 'knowledge', '产品化',
                           '卡片盒产品化', '卡片盒产品化的产品化升级说明。')
        self.publish()
        return own, shared

    def test_explicit_identifier_beats_generic_shared_card(self):
        own, shared = self._base()
        ids = self.ids(self.ev.context('project', self.QUERY))
        self.assertIn(own['id'], ids)
        self.assertNotIn(shared['id'], ids)

    def test_body_only_supplement_naming_same_subject_is_kept(self):
        own, shared = self._base()
        supplement = self.seed('project', '补充条件', 'knowledge', '补充',
                               '补充条件', '补充：FinchCore 的升级效果边界说明。')
        self.publish()
        ids = self.ids(self.ev.context('project', self.QUERY))
        self.assertIn(own['id'], ids)
        self.assertIn(supplement['id'], ids)
        self.assertNotIn(shared['id'], ids)

    def test_unknown_identifier_never_falls_back_to_a_generic_card(self):
        generic = self.seed('project', '产品化升级说明', 'knowledge', '升级',
                            '产品化升级说明', '产品化升级的效果说明。')
        shared = self.seed('shared', '卡片盒产品化', 'knowledge', '产品化',
                           '卡片盒产品化', '卡片盒产品化的产品化升级说明。')
        self.publish()
        got = self.ev.context('project', 'ZorBlax 做了产品化升级，你帮我看下效果如何')
        self.assertEqual(got['results'], [])
        self.assertNotIn(generic['id'], self.ids(got))
        self.assertNotIn(shared['id'], self.ids(got))

    def test_multiple_named_subjects_are_not_chosen_one_at_random(self):
        a = self.seed('project', 'FinchCore脉络', 'knowledge', '升级',
                      'FinchCore脉络', 'FinchCore 的升级说明。')
        b = self.seed('project', 'OrbitDB脉络', 'knowledge', '升级',
                      'OrbitDB脉络', 'OrbitDB 的升级说明。')
        noise = self.seed('shared', '卡片盒产品化', 'knowledge', '产品化',
                          '卡片盒产品化', '产品化升级说明。')
        self.publish()
        ids = self.ids(self.ev.context('project', '把 FinchCore 和 OrbitDB 的升级做个对比'))
        self.assertIn(a['id'], ids)
        self.assertIn(b['id'], ids)
        self.assertNotIn(noise['id'], ids)

    def test_complete_identifier_boundary_is_not_a_substring(self):
        card_art = {'id': 'a' * 32, 'project': 'project', 'key': 'art station',
                    'title': 'art station', 'topic': '展', 'body': 'art 展说明。',
                    'kind': 'knowledge', 'version': 1, 'updated_at': 1}
        card_party = {'id': 'b' * 32, 'project': 'project', 'key': 'party plan',
                      'title': 'party plan', 'topic': '活动', 'body': 'party 安排说明。',
                      'kind': 'knowledge', 'version': 1, 'updated_at': 1}
        self.assertEqual(query_identifiers('ART 的安排'), {'art'})
        self.assertEqual(query_identifiers('party 的安排'), set())
        got = select_consumer_topic([card_art, card_party], 'ART 的安排', 'project')
        self.assertEqual([c['id'] for c in got], [card_art['id']])

    def test_acronym_named_by_a_card_is_an_exclusive_subject(self):
        card = self.seed('project', 'MQTT连接', 'knowledge', '网络',
                         'MQTT连接重试', 'MQTT 连接的重试策略。')
        generic = self.seed('shared', '网络设置', 'knowledge', '网络',
                            '网络设置说明', '网络连接重试的通用说明。')
        self.publish()
        ids = self.ids(self.ev.context('project', 'MQTT 的连接重试怎么设置'))
        self.assertIn(card['id'], ids)
        self.assertNotIn(generic['id'], ids)

    def test_all_caps_format_word_does_not_blank_a_normal_question(self):
        card = self.seed('project', '青岚巡检状态', 'knowledge', '巡检',
                         '青岚配置巡检状态', '青岚配置巡检仍待执行。')
        self.publish()
        prompt = '继续青岚配置巡检。只返回JSON字段has_report，证据缺失则null。'
        ids = self.ids(self.ev.context('project', prompt))
        self.assertIn(card['id'], ids)


class ReferentialFocusTests(_Harness):
    """Case 2: a referential follow-up recovers the same-session subject first."""

    PRIOR = '昨天 FinchCore 的产品化升级完成，帮我核查效果'
    FOLLOWUP = ('这套助手是在打开会话时给一次资料，还是每个追问都会更新？'
                '它能自行找依据吗？对回答质量究竟有什么改变？')

    def _session(self):
        target = self.seed('project', '对话卡片焦点', 'knowledge', '助手',
                           'FinchCore 对话卡片动态推送与效果',
                           'FinchCore 助手在打开会话时给一次资料，每个追问都会更新；'
                           '它能自行找依据。')
        # The workbook body shares only generic frame terms with the follow-up
        # (对话/查询/动态/效果 + 更新/资料/回答/质量), so a naive rank admits it.
        workbook = self.seed('project', '工作簿执行链结构', 'knowledge', '办公',
                             '工作簿执行链结构',
                             '工作簿的对话查询动态效果说明。每次更新都会改变资料与回答质量。'
                             '第二句。第三句。')
        # A current card whose *title* overlaps the follow-up must not be taken
        # for a fresh concrete topic either.
        decoy = self.seed('project', '会话更新说明', 'knowledge', '办公',
                          '会话更新与回答质量说明',
                          '会话更新与回答质量的通用说明。')
        self.publish()
        self.capture(self.PRIOR, session='f1', event='p1')
        return target, workbook, decoy

    def test_referential_followup_recovers_subject_not_generic_overlap(self):
        target, workbook, decoy = self._session()
        # The contamination is real: the raw rank admits the weak workbook.
        admitted = rank_consumer([target, workbook, decoy], self.FOLLOWUP,
                                 specific_terms(self.FOLLOWUP),
                                 lambda c: c['project'] == 'project')
        self.assertIn(workbook['id'], [c['id'] for c in admitted])
        got = self.ev.context('project', self.FOLLOWUP, session='f1')
        ids = self.ids(got)
        self.assertIn(target['id'], ids)
        self.assertNotIn(workbook['id'], ids)
        self.assertNotIn(decoy['id'], ids)

    def test_recovery_with_no_current_heading_overlap(self):
        subject = self.seed('project', '岩雀升级', 'knowledge', '助手',
                            '岩雀助手', '岩雀助手的产品升级：每轮推送卡片还是开头才有。')
        noise = self.seed('project', '工作簿协作', 'knowledge', '办公',
                          '工作簿协作', '工作簿协作的互动和推送效果说明。')
        self.publish()
        self.capture('我们在讨论岩雀助手的产品升级。', session='g1', event='p1')
        followup = '你现在是每轮推送卡片还是开头才有？效果有变好么'
        ids = self.ids(self.ev.context('project', followup, session='g1'))
        self.assertIn(subject['id'], ids)
        self.assertNotIn(noise['id'], ids)

    def test_detector_accepts_generic_cognition_followup(self):
        self.assertTrue(is_referential_followup(self.FOLLOWUP))
        self.assertFalse(is_continuation(self.FOLLOWUP))

    def test_new_topic_negatives_still_hold(self):
        # Addressing the assistant or a short question never grants carry, and
        # all earlier boundary negatives still hold after the frame extension.
        for q in ('你能解释海豚吗', '请你解释 MQTT', '你会做披萨吗',
                  '海豚怎么定位', '请介绍珊瑚礁', '示例纠缠怎么解释',
                  '消防验收流程', '火山观测', ''):
            with self.subTest(q=q):
                self.assertFalse(is_referential_followup(q), q)
        target, _workbook, _decoy = self._session()
        for q in ('海豚怎么定位', '请你解释 MQTT', '你能解释海豚吗',
                  '换个话题，你会做披萨吗'):
            with self.subTest(q=q):
                self.assertNotIn(target['id'],
                                 self.ids(self.ev.context('project', q, session='f1')), q)


class AnchorSubjectTests(unittest.TestCase):
    def test_long_turn_keeps_identifier_within_bound(self):
        long_cjk = ''.join(chr(0x4e00 + i) for i in range(400))
        anchor = anchor_terms([('FinchCore ' + long_cjk, 1.0, 's1')])
        self.assertLessEqual(len(anchor), 24)
        self.assertIn('finchcore', anchor)

    def test_identifier_priority_across_recent_turns(self):
        long_cjk = ''.join(chr(0x4e00 + i) for i in range(400))
        turns = [('先讨论 FinchCore 的升级。', 1.0, 's1'),
                 (long_cjk, 2.0, 's2')]
        anchor = anchor_terms(turns)
        self.assertLessEqual(len(anchor), 24)
        self.assertIn('finchcore', anchor)

    def test_empty_and_bounded_fallbacks(self):
        self.assertEqual(anchor_terms([]), set())
        self.assertLessEqual(len(anchor_terms([(''.join('甲乙丙丁' * 60), 1.0, 's')])), 24)


class DirectQuestionTests(_Harness):
    def test_short_chinese_questions_keep_useful_matches(self):
        cards = {
            '海豚如何通过回声定位': self.seed('project', '海豚回声定位', 'knowledge',
                                              '生物', '海豚回声定位', '海豚通过回声定位。'),
            '厨房水槽漏水怎样排查': self.seed('project', '水槽漏水排查', 'knowledge',
                                              '家居', '厨房水槽漏水排查', '厨房水槽漏水的排查步骤。'),
            '冷链抽检漏封口怎么处理': self.seed('project', '冷链封口处理', 'knowledge',
                                                '物流', '冷链抽检漏封口处理', '冷链抽检漏封口的处理办法。'),
        }
        self.publish()
        for query, card in cards.items():
            with self.subTest(query=query):
                got = self.ev.context('project', query)
                self.assertNotEqual(got['results'], [])
                self.assertIn(card['id'], self.ids(got))

    def test_shared_generic_only_stays_empty(self):
        for key, topic in (('招聘方案', '方案'), ('报关流程', '流程'), ('健身计划', '计划')):
            self.seed('shared', key, 'knowledge', topic, key, '%s的无关说明。' % key)
        self.publish()
        for q in ('方案', '流程', '计划'):
            with self.subTest(q=q):
                self.assertEqual(self.ev.context('project', q)['results'], [])

    def test_clear_new_subject_absent_from_cards_wins_over_history(self):
        self.seed('project', '岩雀升级', 'knowledge', '助手', '岩雀助手',
                  '岩雀助手的产品升级。')
        self.publish()
        self.capture('我们在讨论岩雀助手的产品升级。', session='h1', event='p1')
        got = self.ev.context('project', '消防验收流程', session='h1')
        self.assertEqual(got['results'], [])


class NoArbitraryCardsTests(_Harness):
    def test_project_equality_is_not_topical_evidence(self):
        self.seed('project', '薪资状态', 'memory', '人事', '薪资发放', '本轮薪资已发放。')
        self.publish()
        self.assertEqual(self.ev.context('project', '番茄种植间距')['results'], [])

    def test_no_history_followup_yields_no_arbitrary_cards(self):
        self.seed('project', '无关知识', 'knowledge', '主题', '无关知识', '无关正文。')
        self.publish()
        got = self.ev.context('project', '你现在是每轮推送吗', session='')
        self.assertEqual(got['results'], [])


class PureSelectionHelperTests(unittest.TestCase):
    def test_prior_provider_is_lazy_and_used_only_for_a_referral(self):
        calls = []

        def provider():
            calls.append(1)
            return [('昨天 FinchCore 的产品化升级完成，帮我核查效果', 1.0, 's1')]

        target = {'id': 't' * 32, 'project': 'project', 'key': 'k',
                  'title': 'FinchCore 对话卡片', 'topic': '助手',
                  'body': 'FinchCore 说明。', 'kind': 'knowledge',
                  'version': 1, 'updated_at': 1}
        self.assertEqual(
            select_consumer_topic([target], '海豚如何通过回声定位', 'project', provider), [])
        self.assertEqual(calls, [])
        got = select_consumer_topic([target], '这套助手更新了吗', 'project', provider)
        self.assertEqual([c['id'] for c in got], [target['id']])
        self.assertEqual(calls, [1])

    def test_acknowledgement_returns_nothing(self):
        target = {'id': 't' * 32, 'project': 'project', 'key': 'k', 'title': '方案',
                  'topic': '方案', 'body': '方案。', 'kind': 'memory',
                  'version': 1, 'updated_at': 1}
        self.assertEqual(select_consumer_topic([target], '好的，谢谢', 'project'), [])


class CaseInsensitiveSubjectTests(_Harness):
    """Defect 1: a card subject is recognised in any letter case."""

    def _cards(self):
        finch = self.seed('project', 'FinchCore产品升级', 'knowledge', '升级',
                          'FinchCore产品升级', 'FinchCore 产品化调整的提升边界。')
        cardbox = self.seed('shared', 'CardBox产品化', 'knowledge', '产品化',
                            'CardBox产品化', 'CardBox 的产品化提升说明。')
        self.publish()
        return finch, cardbox

    def test_lowercase_subject_token_is_recognised(self):
        finch, cardbox = self._cards()
        ids = self.ids(self.ev.context(
            'project', '上周 finchcore 完成了产品化调整，请核对实际提升'))
        self.assertIn(finch['id'], ids)
        self.assertNotIn(cardbox['id'], ids)

    def test_all_case_variants_select_the_same_subject(self):
        finch, cardbox = self._cards()
        seen = []
        for token in ('finchcore', 'FinchCore', 'FINCHCORE', 'fInChCoRe'):
            with self.subTest(token=token):
                ids = self.ids(self.ev.context(
                    'project', '上周 %s 完成了产品化调整，请核对实际提升' % token))
                self.assertIn(finch['id'], ids)
                self.assertNotIn(cardbox['id'], ids)
                seen.append(tuple(ids))
        self.assertEqual(len(set(seen)), 1)

    def test_unrelated_word_overlap_does_not_ride_along(self):
        finch, cardbox = self._cards()
        unrelated = self.seed('project', '产品化提升说明', 'knowledge', '产品化',
                              '产品化提升说明', '产品化提升的通用说明。')
        self.publish()
        ids = self.ids(self.ev.context(
            'project', '上周 finchcore 完成了产品化调整，请核对实际提升'))
        self.assertIn(finch['id'], ids)
        self.assertNotIn(unrelated['id'], ids)
        self.assertNotIn(cardbox['id'], ids)


class MultipleAndUnknownSubjectTests(_Harness):
    """Several explicit subjects stay eligible; an unknown one does not leak."""

    def test_multiple_named_subjects_remain_eligible(self):
        a = self.seed('project', 'FinchCore升级', 'knowledge', '升级',
                      'FinchCore升级', 'FinchCore 的产品化调整。')
        b = self.seed('project', 'OrbitDB升级', 'knowledge', '升级',
                      'OrbitDB升级', 'OrbitDB 的产品化调整。')
        noise = self.seed('shared', 'CardBox产品化', 'knowledge', '产品化',
                          'CardBox产品化', 'CardBox 的产品化调整说明。')
        self.publish()
        ids = self.ids(self.ev.context(
            'project', '把 finchcore 和 orbitdb 的产品化调整做个对比'))
        self.assertIn(a['id'], ids)
        self.assertIn(b['id'], ids)
        self.assertNotIn(noise['id'], ids)

    def test_unknown_distinctive_subject_returns_empty(self):
        generic = self.seed('project', '产品化调整说明', 'knowledge', '升级',
                            '产品化调整说明', '产品化调整的通用说明。')
        shared = self.seed('shared', 'CardBox产品化', 'knowledge', '产品化',
                           'CardBox产品化', 'CardBox 的产品化调整说明。')
        self.publish()
        got = self.ev.context('project', 'ZorBlax 的产品化调整请核对')
        self.assertEqual(got['results'], [])
        self.assertNotIn(generic['id'], self.ids(got))
        self.assertNotIn(shared['id'], self.ids(got))


class PronounFollowupFamilyTests(_Harness):
    """Defect 2: genuine pronoun/deictic follow-ups recover the thread subject."""

    PRIOR = '上周 finchcore 完成了产品化调整，请核对实际提升'

    def _session(self, session='pf1'):
        target = self.seed('project', 'FinchCore对话推送', 'knowledge', '助手',
                           'FinchCore 对话卡片动态推送与效果',
                           'FinchCore 助手会在刚打开聊天时准备背景，后续讨论时再检索。')
        # The workbook shares the generic workflow nouns only in its *body*, the
        # way a noisy host replay overlaps a follow-up.
        workbook = self.seed('project', '办公流程清单', 'knowledge', '办公',
                             '办公流程清单',
                             '工作簿的对话聊天背景讨论说明，整体看来更顺手。')
        decoy = self.seed('project', '会话更新说明', 'knowledge', '办公',
                          '会话更新与回答质量说明',
                          '会话更新与回答质量的通用说明。')
        self.publish()
        self.capture(self.PRIOR, session=session, event='p1')
        return target, workbook, decoy

    def test_newly_reported_pronoun_followup_recovers_subject(self):
        target, workbook, decoy = self._session()
        query = ('它会在刚打开聊天时准备背景，还是后续讨论时再检索？'
                 '整体看来更顺手了吗？')
        self.assertTrue(is_referential_followup(query))
        self.assertFalse(is_continuation(query))
        ids = self.ids(self.ev.context('project', query, session='pf1'))
        self.assertIn(target['id'], ids)
        self.assertNotIn(workbook['id'], ids)
        self.assertNotIn(decoy['id'], ids)

    def test_several_pronoun_wording_families(self):
        target, workbook, decoy = self._session()
        families = (
            '这个现在的效果有变好吗',
            '它后续还会继续更新吗',
            '它们之间的差异还明显吗',
            '那套现在怎么样了',
            '这套的更新还有必要吗',
        )
        for query in families:
            with self.subTest(query=query):
                self.assertTrue(is_referential_followup(query), query)
                ids = self.ids(self.ev.context('project', query, session='pf1'))
                self.assertIn(target['id'], ids)
                self.assertNotIn(workbook['id'], ids)
                self.assertNotIn(decoy['id'], ids)

    def test_assistant_address_and_new_topic_stay_empty(self):
        target, _workbook, _decoy = self._session()
        for query in ('你能解释海豚吗', '请你解释 MQTT', '海豚怎么定位',
                      '继续解释火山喷发', '换个话题，你会做披萨吗'):
            with self.subTest(query=query):
                ids = self.ids(self.ev.context('project', query, session='pf1'))
                self.assertNotIn(target['id'], ids)


class NearestTopicTests(_Harness):
    """Defect 3: nearest substantive subject wins; a generic turn never crowds."""

    def test_closer_subject_overrides_older(self):
        finch = self.seed('project', 'FinchCore升级', 'knowledge', '升级',
                          'FinchCore升级', 'FinchCore 的产品化调整。')
        sink = self.seed('project', '厨房水槽漏水排查', 'knowledge', '家居',
                         '厨房水槽漏水排查', '厨房水槽漏水的排查步骤。')
        self.publish()
        self.capture('上周 finchcore 完成了产品化调整。', session='n1', event='p1')
        self.capture('厨房水槽漏水怎样排查。', session='n1', event='p2')
        ids = self.ids(self.ev.context('project', '它现在的进展怎么样了', session='n1'))
        self.assertIn(sink['id'], ids)
        self.assertNotIn(finch['id'], ids)

    def test_long_recent_generic_turn_does_not_displace_subject(self):
        finch = self.seed('project', 'FinchCore升级', 'knowledge', '升级',
                          'FinchCore升级', 'FinchCore 的产品化调整。')
        workbook = self.seed('project', '办公流程清单', 'knowledge', '办公',
                             '办公流程清单',
                             '工作簿的对话聊天背景讨论说明，整体看来更顺手。')
        self.publish()
        self.capture('上周 finchcore 完成了产品化调整。', session='n2', event='p1')
        long_generic = '这个执行链的对话聊天背景讨论动态效果究竟怎么样？' * 3
        self.capture(long_generic, session='n2', event='p2')
        ids = self.ids(self.ev.context('project', '它现在的进展怎么样了', session='n2'))
        self.assertIn(finch['id'], ids)
        self.assertNotIn(workbook['id'], ids)


class ChineseSubjectFollowupTests(_Harness):
    def test_chinese_subject_followup_recovers_subject(self):
        cold = self.seed('project', '冷链密封', 'knowledge', '物流',
                         '冷链密封许可', '冷链密封许可的条件。')
        noise = self.seed('project', '办公协作', 'knowledge', '办公',
                          '办公协作说明', '办公协作的讨论和准备说明。')
        self.publish()
        self.capture('我们在讨论冷链密封许可的条件。', session='c1', event='p1')
        ids = self.ids(self.ev.context('project', '它还需要准备哪些材料？', session='c1'))
        self.assertIn(cold['id'], ids)
        self.assertNotIn(noise['id'], ids)


class BoundaryFocusTests(_Harness):
    def test_no_history_pronoun_followup_yields_no_arbitrary_cards(self):
        self.seed('project', '无关知识', 'knowledge', '主题', '无关知识', '无关正文。')
        self.publish()
        got = self.ev.context('project', '它现在的效果怎么样', session='nh')
        self.assertEqual(got['results'], [])

    def test_unknown_new_topic_does_not_carry_stale_subject(self):
        finch = self.seed('project', 'FinchCore升级', 'knowledge', '升级',
                          'FinchCore升级', 'FinchCore 的产品化调整。')
        self.publish()
        self.capture('上周 finchcore 完成了产品化调整。', session='b1', event='p1')
        got = self.ev.context('project', '消防验收流程', session='b1')
        self.assertEqual(got['results'], [])
        self.assertNotIn(finch['id'], self.ids(got))


class SubjectHelperTests(unittest.TestCase):
    """Deterministic helper-level checks for the new subject resolution."""

    def _card(self, cid, key, title, topic, body, project='project'):
        return {'id': cid, 'project': project, 'key': key, 'title': title,
                'topic': topic, 'body': body, 'kind': 'knowledge',
                'version': 1, 'updated_at': 1}

    def test_case_insensitive_subject_resolution(self):
        cards = [self._card('a' * 32, 'FinchCore升级', 'FinchCore升级', '升级',
                            'FinchCore 说明。')]
        for token in ('finchcore', 'FinchCore', 'FINCHCORE'):
            with self.subTest(token=token):
                self.assertEqual(subject_identifiers(token + ' 的进度', cards),
                                 {'finchcore'})

    def test_ordinary_english_words_are_not_subjects(self):
        cards = [self._card('a' * 32, 'FinchCore升级', 'FinchCore升级', '升级',
                            'FinchCore 说明。')]
        self.assertEqual(subject_identifiers('please explain json format', cards), set())
        self.assertEqual(subject_identifiers('the next plan', cards), set())

    def test_compound_token_keeps_base_and_ignores_ordinary_part(self):
        cards = [self._card('a' * 32, 'FinchCore-Next', 'FinchCore-Next', '升级',
                            'FinchCore-Next 说明。')]
        self.assertEqual(subject_identifiers('finchcore-next 核对', cards), {'finchcore'})

    def test_recovery_prefers_nearest_and_skips_generic_turn(self):
        finch = self._card('a' * 32, 'FinchCore升级', 'FinchCore升级', '升级',
                           'FinchCore 的产品化调整。')
        sink = self._card('b' * 32, '厨房水槽漏水排查', '厨房水槽漏水排查', '家居',
                          '厨房水槽漏水的排查步骤。')
        own = lambda c: True
        turns = [('上周 finchcore 完成了产品化调整。', 1.0, 's1'),
                 ('厨房水槽漏水怎样排查。', 2.0, 's2')]
        self.assertEqual([c['id'] for c in recover_subject_cards([finch, sink], turns, own)],
                         [sink['id']])
        generic = '这个执行链的对话聊天背景讨论动态效果究竟怎么样？' * 3
        turns2 = [('上周 finchcore 完成了产品化调整。', 1.0, 's1'),
                  (generic, 2.0, 's2')]
        got = [c['id'] for c in recover_subject_cards([finch, sink], turns2, own)]
        self.assertIn(finch['id'], got)
        self.assertNotIn(sink['id'], got)

    def test_recovery_unknown_identifier_blanks(self):
        finch = self._card('a' * 32, 'FinchCore升级', 'FinchCore升级', '升级',
                           'FinchCore 的产品化调整。')
        own = lambda c: True
        turns = [('上周 finchcore 完成了产品化调整。', 1.0, 's1'),
                 ('ZorBlax 的升级怎么样了。', 2.0, 's2')]
        self.assertEqual(recover_subject_cards([finch], turns, own), [])

    def test_select_is_case_insensitive_for_a_named_card(self):
        finch = self._card('a' * 32, 'FinchCore产品升级', 'FinchCore产品升级', '升级',
                           'FinchCore 产品化调整的提升。')
        cardbox = self._card('b' * 32, 'CardBox产品化', 'CardBox产品化', '产品化',
                             'CardBox 产品化提升说明。', project='shared')
        for token in ('finchcore', 'FinchCore', 'FINCHCORE'):
            with self.subTest(token=token):
                got = select_consumer_topic(
                    [finch, cardbox],
                    '上周 %s 完成了产品化调整，请核对实际提升' % token, 'project')
                self.assertEqual([c['id'] for c in got], [finch['id']])

    def test_select_prior_provider_uses_nearest_subject(self):
        finch = self._card('a' * 32, 'FinchCore升级', 'FinchCore升级', '升级',
                           'FinchCore 的产品化调整。')
        sink = self._card('b' * 32, '厨房水槽漏水排查', '厨房水槽漏水排查', '家居',
                          '厨房水槽漏水的排查步骤。')
        prior = [('上周 finchcore 完成了产品化调整。', 1.0, 's1'),
                 ('厨房水槽漏水怎样排查。', 2.0, 's2')]
        got = select_consumer_topic([finch, sink], '它现在的进展怎么样了',
                                    'project', lambda: prior)
        self.assertEqual([c['id'] for c in got], [sink['id']])


class ChineseSubjectPhraseTests(_Harness):
    """A Chinese subject named before 的 (or among comparison subjects) is
    resolved from the leading phrase, so a different card that only shares the
    trailing action words is not chosen. Names are invented synthetic text."""

    def test_subject_before_action_beats_shared_action_heading(self):
        main = self.seed('project', '岚谷温室维护安排', 'knowledge', '设施',
                         '岚谷温室维护安排', '岚谷温室有独立许可条件。')
        supplement = self.seed('project', '现场补充记录', 'knowledge', '设施',
                               '现场补充记录', '针对 岚谷温室 另有断电例外，须人工检查。')
        other = self.seed('project', '南岭设施低温灌溉', 'knowledge', '设施',
                          '南岭设施低温灌溉', '南岭设施的低温灌溉只适用于该设施。')
        self.publish()
        ids = self.ids(self.ev.context('project', '岚谷温室的低温灌溉怎么安排？'))
        self.assertIn(main['id'], ids)
        self.assertIn(supplement['id'], ids)
        self.assertNotIn(other['id'], ids)

    def test_two_chinese_subjects_in_one_comparison(self):
        first = self.seed('project', '霁月灯塔交付约定', 'knowledge', '交付',
                          '霁月灯塔交付约定', '霁月灯塔保留自己的范围。')
        second = self.seed('project', '玄石货栈交付约定', 'knowledge', '交付',
                           '玄石货栈交付约定', '玄石货栈保留另一范围。')
        other = self.seed('project', '松果档案交付比较', 'knowledge', '交付',
                          '松果档案交付比较', '松果档案也有交付比较过程。')
        self.publish()
        ids = self.ids(self.ev.context(
            'project', '请一起比较 霁月灯塔 和 玄石货栈 的交付约定。'))
        self.assertIn(first['id'], ids)
        self.assertIn(second['id'], ids)
        self.assertNotIn(other['id'], ids)


class UnknownAndOrdinaryEnglishTests(_Harness):
    """An unknown name blanks in every letter case, and ordinary English
    instruction verbs never become a subject or summon their own card."""

    def test_unknown_named_subject_blanks_in_any_case(self):
        self.seed('project', '通用升级方案卡', 'knowledge', '升级',
                  '通用升级方案卡', '升级方案的通用说明。')
        self.publish()
        for token in ('drifthollow', 'DriftHollow', 'DRIFTHOLLOW'):
            with self.subTest(token=token):
                got = self.ev.context('project', '请说明 %s 的升级方案。' % token)
                self.assertEqual(got['results'], [])

    def test_ordinary_english_verbs_are_not_a_subject(self):
        noise = self.seed('project', 'Coastal pottery notes', 'knowledge', 'notes',
                          'Coastal pottery notes',
                          'please explain compare evaluate summarize these unrelated clay samples.')
        real = self.seed('project', 'QuiverLog policy', 'knowledge', 'policy',
                         'QuiverLog policy', 'QuiverLog permits scheduled review.')
        self.publish()
        self.assertEqual(
            self.ev.context('project', 'please explain, compare and summarize')['results'], [])
        ids = self.ids(self.ev.context(
            'project', 'please explain, compare and summarize QuiverLog policy'))
        self.assertIn(real['id'], ids)
        self.assertNotIn(noise['id'], ids)


class DeicticAndMetaFollowupTests(_Harness):
    """Deictic/adverbial and second-person META follow-ups recover the current
    thread subject, while a concrete new subject and an assistant address do
    not."""

    def _thread(self, name, session):
        target = self.seed('project', name + '处理约定', 'knowledge', '约定',
                           name + '处理约定', name + ' 由独立任务负责恢复。')
        self.seed('project', '陶艺窑炉记录', 'knowledge', '窑炉', '陶艺窑炉记录',
                  '陶艺窑炉需要有人盯着，并在中断后接上，保证稳定运转。')
        self.publish()
        self.capture('本轮正在核对 %s 的处理约定。' % name, session=session, event='p1')
        return target

    def test_deictic_adverbial_forms_recover_subject(self):
        target = self._thread('HarborLane', 'deixis')
        for q in ('它现在能稳定运转吗？', '那样接入会不会更省事？', '这套还能自动兜底么？',
                  '那需要我一直盯着吗？', '这样会反复绕回去吗？', '它遇到中断还能接上吗？'):
            with self.subTest(q=q):
                self.assertIn(target['id'],
                              self.ids(self.ev.context('project', q, session='deixis')), q)

    def test_second_person_meta_inquiry_recovers_but_assistant_address_does_not(self):
        target = self._thread('ZephyrBay', 'meta')
        meta = ('想确认下，你用背景资料时会自行检索吗，还是有人预先给你？'
                '用起来是否更顺畅？')
        self.assertIn(target['id'],
                      self.ids(self.ev.context('project', meta, session='meta')))
        for q in ('你能解释海豚吗', '请你解释 MQTT'):
            with self.subTest(q=q):
                self.assertNotIn(target['id'],
                                 self.ids(self.ev.context('project', q, session='meta')), q)

    def test_concrete_new_subject_beats_deictic_carry(self):
        old = self.seed('project', 'CedarFlow事项', 'knowledge', '事项',
                        'CedarFlow事项', 'CedarFlow 保留其规则。')
        new = self.seed('project', 'MapleDock事项', 'knowledge', '事项',
                        'MapleDock事项', 'MapleDock 有自己的规则。')
        self.publish()
        self.capture('我们正在核对 CedarFlow 的事项。', session='carry', event='p1')
        ids = self.ids(self.ev.context('project', '那个 MapleDock 的事项怎么样？', session='carry'))
        self.assertIn(new['id'], ids)
        self.assertNotIn(old['id'], ids)


class RawStatementSubjectGateTests(_Harness):
    """The raw-statement fallback passes the *same* subject gate: an unrelated
    statement that only shares the action words never enters the rendered
    context, while the same-subject statement still does."""

    def test_unrelated_raw_statement_cannot_bypass_the_subject(self):
        self.seed('project', 'ZephyrBay事项', 'knowledge', '事项',
                  'ZephyrBay事项', 'ZephyrBay有独立适用范围。')
        self.publish()
        related = self.capture('ZephyrBay 本轮另有人工核验条件，等待核对。',
                               session='raw-a', event='raw-rel')
        unrelated = self.capture('苍梧档案架的批量导入仅适用于它自己的材料，等待核对。',
                                 session='raw-b', event='raw-unrel')
        got = self.ev.context('project', '请说明 ZephyrBay 的批量导入。')
        raw_ids = [r['source_id'] for r in got['recent_statements']]
        self.assertIn(related['source_id'], raw_ids)
        self.assertNotIn(unrelated['source_id'], raw_ids)
        self.assertIn('ZephyrBay 本轮另有人工核验条件', got['rendered_context'])
        self.assertNotIn('苍梧档案架的批量导入', got['rendered_context'])


class SubjectResolutionHelperTests(unittest.TestCase):
    def _card(self, cid, key, title, topic, body, project='project'):
        return {'id': cid, 'project': project, 'key': key, 'title': title,
                'topic': topic, 'body': body, 'kind': 'knowledge',
                'version': 1, 'updated_at': 1}

    def test_named_subject_resolves_case_insensitively(self):
        card = self._card('a' * 32, 'QuiverLog 事项', 'QuiverLog 事项', '事项',
                          'QuiverLog 说明。')
        for token in ('quiverlog', 'QuiverLog', 'QUIVERLOG'):
            with self.subTest(token=token):
                terms_, strong, blank = resolve_query_subject(
                    '请说明 %s 的事项。' % token, [card])
                self.assertEqual([c['id'] for c in strong], [card['id']])
                self.assertFalse(blank)

    def test_unknown_named_subject_blanks(self):
        card = self._card('a' * 32, '通用事项', '通用事项', '事项', '通用说明。')
        for token in ('drifthollow', 'DriftHollow', 'DRIFTHOLLOW'):
            with self.subTest(token=token):
                terms_, strong, blank = resolve_query_subject(
                    '请说明 %s 的事项。' % token, [card])
                self.assertEqual(strong, [])
                self.assertTrue(blank)

    def test_leading_phrase_is_the_subject_not_the_action(self):
        main = self._card('a' * 32, '岚谷温室维护安排', '岚谷温室维护安排', '设施',
                          '岚谷温室有独立许可条件。')
        other = self._card('b' * 32, '南岭设施低温灌溉', '南岭设施低温灌溉', '设施',
                           '南岭设施的低温灌溉只适用于该设施。')
        terms_, strong, blank = resolve_query_subject(
            '岚谷温室的低温灌溉怎么安排？', [main, other])
        self.assertEqual([c['id'] for c in strong], [main['id']])
        self.assertFalse(blank)
        self.assertIn('温室', terms_)


if __name__ == '__main__':
    unittest.main()
