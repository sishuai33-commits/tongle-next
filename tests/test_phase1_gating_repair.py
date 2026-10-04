"""Focused product tests for the 2026-10-01 phase1 gating/delivery repair.

Synthetic fixtures only: no live vault, model, credentials or network, and no
real personal session text or card id. Where a card is seeded it proves machine
behaviour, not semantic quality.

Covers the concrete phase1 failures:

1. generic-only terms must not admit shared cards (heading/substring/body);
   an exact *specific* identity still finds its long card as a locator;
2. a zero-hit short/new topic stays empty; only a continuation or a referential
   follow-up recovers a small own project-status set;
3. a referential follow-up recovers the same-session subject and is not hijacked
   by weak generic office-word body overlaps; a fresh topic overrides it;
4. ``anchor_terms`` is deterministically bounded (no whole-turn union) and the
   exact-identity rank cannot be widened by an unbounded anchor text;
5. ``context_delivery`` card markers render exactly one bracket each;
6. the referral/continuation detector is a bounded *generic-frame* test: a
   second-person address or a short question length alone never grants carry,
   an explicit topic-switch phrase disables it, and a lone own-project memory
   card is never carried into an unrelated fresh subject (with unseen domains).
"""
import json
import tempfile
import unittest
from pathlib import Path

from tongle.engine import Engine
from tongle.evolution import Evolution
from tongle.knowledge_files import KnowledgeFiles
from tongle.context_delivery import (CARD_FULL_FORMAT, CARD_LOCATOR_FORMAT,
                                     render_context, utf16_units)
from tongle.context_retrieval import (anchor_terms, is_continuation,
                                      is_referential_followup, rank_consumer)


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


class GenericSharedNotCrossTopicTests(_Harness):
    def test_generic_only_terms_never_pull_shared_cards(self):
        own = self.seed('project', '本项目方案', 'memory', '方案', '本项目方案',
                        '本项目方案的正文。')
        families = (('方案', ('招聘方案', '部署方案', '投资方案')),
                    ('计划', ('健身计划', '航海计划', '园艺计划')),
                    ('流程', ('结算流程', '诊疗流程', '报关流程')))
        shared = []
        for term, keys in families:
            for key in keys:
                shared.append((term, self.seed(
                    'shared', key, 'knowledge', term, key,
                    '与%s有关的无关共享资料。' % term)))
        self.publish()
        for term, _keys in families:
            ids = self.ids(self.ev.context('project', term))
            for shared_term, card in shared:
                if shared_term == term:
                    self.assertNotIn(card['id'], ids, (term, card['key']))
        # A real own-project card that genuinely carries the generic word wins.
        self.assertIn(own['id'], self.ids(self.ev.context('project', '方案')))

    def test_exact_specific_identity_long_card_is_a_locator(self):
        for key in ('发票', '光圈', '潮汐'):
            card = self.seed('shared', key, 'knowledge', '票据', key, '据' * 9000)
            self.publish()
            got = self.ev.context('project', key)
            result = next(r for r in got['results'] if r['id'] == card['id'])
            self.assertFalse(result['complete'], key)
            self.assertTrue(result['path'], key)
            self.assertLessEqual(utf16_units(got['rendered_context']), 6000)
            self.assertNotIn('据' * 9000, got['rendered_context'])


class ZeroHitAndFollowupTests(_Harness):
    def test_short_zero_hit_topic_is_empty(self):
        self.seed('project', '冷链密封', 'knowledge', '物流', '冷链密封',
                  '冷链密封许可条件。')
        self.publish()
        for query in ('火山观测', '方案', '示例报告'):
            self.assertEqual(self.ev.context('project', query)['results'], [], query)

    def test_explicit_continuation_recovers_only_small_memory(self):
        mem = self.seed('project', '项目状态', 'memory', '项目', '当前进展',
                        '先完成方案。')
        noise = [self.seed('project', '旧资料%d' % i, 'knowledge', '主题',
                           '旧资料%d' % i, '很久以前的无关资料。') for i in range(6)]
        self.publish()
        got = self.ev.context('project', '继续', session='fresh')
        ids = self.ids(got)
        self.assertIn(mem['id'], ids)
        self.assertLessEqual(len(ids), 4)
        for card in noise:
            self.assertNotIn(card['id'], ids)

    def test_referential_followup_recovers_subject_not_weak_noise(self):
        subject = self.seed('project', '岩雀升级', 'knowledge', '助手', '岩雀助手',
                            '岩雀助手的产品升级：每轮推送卡片还是开头才有。')
        # The unrelated cards must overlap the follow-up's *body* spec on a
        # generic office-communication bigram (互动); they are weak matches that
        # must not hijack the bounded same-session subject.
        noise1 = self.seed('project', '简历协作', 'knowledge', '招聘', '简历协作',
                           '简历协作的互动和推送效果说明。')
        noise2 = self.seed('project', '工作簿协作', 'knowledge', '办公', '工作簿协作',
                           '工作簿协作的互动和推送效果说明。')
        self.publish()
        self.capture('我们在讨论岩雀助手的产品升级。', session='f1', event='p1')
        followup = '问你个问题，你现在是对话一开始会有卡片还是每轮交互动态推送？效果有变好么？'
        ids = self.ids(self.ev.context('project', followup, session='f1'))
        self.assertIn(subject['id'], ids)
        self.assertNotIn(noise1['id'], ids)
        self.assertNotIn(noise2['id'], ids)
        # A fresh concrete topic overrides the previous subject; its zero hit
        # stays empty instead of carrying 岩雀.
        fresh = self.ev.context('project', '消防验收流程', session='f1')
        self.assertNotIn(subject['id'], self.ids(fresh))
        self.assertEqual(fresh['results'], [])

    def test_anchor_is_same_session_user_only_and_not_self_or_revoked(self):
        rec = self.capture('岩雀助手的产品升级。', session='f1', event='p1')
        self.assertEqual(self.ev._session_anchor_turns('project', 'f2', '继续'), [])
        turns = self.ev._session_anchor_turns('project', 'f1', '继续')
        self.assertIn(rec['source_id'], [t[2] for t in turns])
        # Assistant-only turns never anchor.
        self.capture('这是助手的回复。', role='assistant', session='f1', event='a1')
        texts = [t[0] for t in self.ev._session_anchor_turns('project', 'f1', '继续')]
        self.assertNotIn('这是助手的回复。', texts)
        # The current turn never anchors on itself.
        cur = self.capture('继续岩雀升级。', session='f1', event='p2')
        turns = self.ev._session_anchor_turns('project', 'f1', '继续岩雀升级。')
        self.assertNotIn(cur['source_id'], [t[2] for t in turns])
        # A revoked source never anchors.
        self.e.source_state('project', rec['source_id'], 'revoked')
        turns = self.ev._session_anchor_turns('project', 'f1', '继续')
        self.assertNotIn(rec['source_id'], [t[2] for t in turns])


class FollowupDetectionTests(unittest.TestCase):
    """The detector reasons about *structure*, not about the mere presence of a
    second-person address or a short question length. These assertions replace
    the earlier, narrower checks that only exercised the happy path.
    """

    def test_genuine_status_and_deictic_followups_still_refer(self):
        for q in ('你现在是每轮推送吗？', '效果有变好么', '刚才那个怎么样',
                  '那现在怎么样了', '现在做到哪一步了', '进展如何', '有几张',
                  '这次几张'):
            with self.subTest(q=q):
                self.assertTrue(is_referential_followup(q), q)

    def test_assistant_address_or_short_length_never_grants_carry(self):
        # Addressing the assistant or a short question can still open a fresh
        # concrete subject, which must never be treated as a referral.
        for q in ('你能解释海豚吗', '请你解释 MQTT', '你会做披萨吗',
                  '海豚怎么定位', '你能解释企鹅吗', '请介绍珊瑚礁',
                  '企鹅怎么迁徙', '你会做寿司吗', '示例纠缠怎么解释',
                  '消防验收流程', '火山观测', ''):
            with self.subTest(q=q):
                self.assertFalse(is_referential_followup(q), q)

    def test_explicit_new_topic_marker_disables_referral(self):
        # Even a second-person address or a follow-up word cannot grant carry
        # once a topic switch is explicit.
        for q in ('换个话题，你会做披萨吗', '换个话题，你现在怎么样',
                  '换个问题，效果有变好么', '另一个话题，你懂天文吗',
                  '说点别的，你知道企鹅吗', '别的，继续'):
            with self.subTest(q=q):
                self.assertFalse(is_referential_followup(q), q)
                self.assertFalse(is_continuation(q), q)

    def test_continuation_requires_a_pure_continuation_frame(self):
        for q in ('继续', '接着', '下一步做什么', '然后呢', '还有呢',
                  '继续项目'):
            with self.subTest(q=q):
                self.assertTrue(is_continuation(q), q)
        # A continuation marker embedded in a new concrete topic is not a
        # reason to carry an unrelated previous subject.
        for q in ('继续解释火山喷发', '继续岩雀升级', '接下来我们做披萨',
                  '然后我们讨论海豚', '方案'):
            with self.subTest(q=q):
                self.assertFalse(is_continuation(q), q)


class TopicBoundaryProbeTests(_Harness):
    """The newly reproduced consumer topic-boundary bug (real core, synthetic
    data only).

    A lone own-project ``memory`` card about the greenhouse must never be
    carried into an unrelated fresh subject merely because the turn addresses
    the assistant, is a short question, or embeds a continuation/follow-up word.
    The same-session positive controls must still recover the greenhouse subject.
    """

    def setUp(self):
        super().setUp()
        self.card = self.seed('lab', '温室通风状态', 'memory', '温室',
                              '温室通风安排', '温室通风由北窗排气，周五检验。')
        self.publish()
        self.prior = '温室通风安排现在做到哪一步了'

    def _ids(self, query, session=''):
        return self.ids(self.ev.context('lab', query, session=session))

    def test_no_history_assistant_address_is_empty(self):
        for q in ('你能解释海豚吗', '请你解释 MQTT', '你会做披萨吗'):
            with self.subTest(q=q):
                self.assertEqual(self.ev.context('lab', q)['results'], [], q)

    def test_same_session_fresh_subject_is_empty(self):
        self.capture(self.prior, session='lab1', event='p1', project='lab')
        for q in ('海豚怎么定位', '请你解释 MQTT', '你能解释海豚吗'):
            with self.subTest(q=q):
                self.assertEqual(
                    self.ev.context('lab', q, session='lab1')['results'], [], q)

    def test_explicit_topic_switch_is_empty(self):
        self.capture(self.prior, session='lab1', event='p1', project='lab')
        for q in ('换个话题，你会做披萨吗', '换个话题，你懂天文吗',
                  '另一个话题，你现在怎么样'):
            with self.subTest(q=q):
                self.assertEqual(
                    self.ev.context('lab', q, session='lab1')['results'], [], q)

    def test_continuation_of_fresh_topic_is_empty(self):
        self.capture(self.prior, session='lab1', event='p1', project='lab')
        for q in ('继续解释火山喷发', '继续，我们做披萨', '接下来讨论海豚'):
            with self.subTest(q=q):
                self.assertEqual(
                    self.ev.context('lab', q, session='lab1')['results'], [], q)

    def test_positive_controls_still_recover_greenhouse(self):
        self.capture(self.prior, session='lab1', event='p1', project='lab')
        for q in ('继续', '那现在怎么样了', '现在做到哪一步了'):
            with self.subTest(q=q):
                self.assertIn(self.card['id'], self._ids(q, session='lab1'), q)

    def test_unseen_domains_never_carry_lone_memory(self):
        self.capture(self.prior, session='lab1', event='p1', project='lab')
        for q in ('你能解释企鹅吗', '请介绍珊瑚礁', '企鹅怎么迁徙',
                  '你会做寿司吗', '示例纠缠怎么解释', '帮我讲讲潮汐',
                  '换个话题，你了解星座吗'):
            with self.subTest(q=q):
                self.assertEqual(
                    self.ev.context('lab', q, session='lab1')['results'], [], q)


class AnchorBoundTests(unittest.TestCase):
    def test_single_long_turn_cannot_expand_anchor_past_bound(self):
        long_turn = ''.join('词%03d ' % i for i in range(200))
        self.assertLessEqual(len(anchor_terms([(long_turn, 1.0, 's1')])), 24)

    def test_anchor_terms_respect_term_and_turn_bounds(self):
        turns = [(''.join('甲%03d ' % i for i in range(50)), float(i), 's%d' % i)
                 for i in range(10)]
        self.assertLessEqual(len(anchor_terms(turns)), 24)
        self.assertLessEqual(len(anchor_terms(turns, max_terms=5)), 5)
        self.assertEqual(anchor_terms([]), set())


class IdentityBypassTests(unittest.TestCase):
    def test_bounded_identity_does_not_widen_to_whole_turn(self):
        card = {'id': 'a' * 32, 'key': '发票', 'title': '发票', 'topic': '票据',
                'body': '正文。', 'version': 1, 'updated_at': 1}
        own = lambda c: True
        # An unbounded whole-turn identity string would admit the card through
        # the substring rank ...
        widened = rank_consumer([card], 'zz', set(), own,
                                identity='前面说过很多无关内容然后提到发票这个词')
        self.assertEqual([c['id'] for c in widened], [card['id']])
        # ... but the bounded anchor path passes identity='' and never does.
        self.assertEqual(rank_consumer([card], 'zz', set(), own, identity=''), [])


class DeliveryFormatTests(unittest.TestCase):
    def test_card_markers_render_exactly_one_bracket(self):
        self.assertEqual(CARD_FULL_FORMAT % ('标题', '正文'), '- [完整卡片] 标题\n正文')
        self.assertEqual(CARD_LOCATOR_FORMAT % ('标题', '路径'),
                         '- [资料位置，需读取全文] 标题\n路径')
        self.assertNotIn('[[', CARD_FULL_FORMAT)
        self.assertNotIn('[[', CARD_LOCATOR_FORMAT)

    def test_render_uses_single_brackets_and_keeps_receipt_truth(self):
        card = {'id': 'a' * 32, 'key': 'k', 'title': '标题', 'topic': 't',
                'body': '正文。', 'version': 1, 'updated_at': 1}
        rendered, _delivered, offered = render_context([card], [], [], lambda c: '')
        self.assertIn('- [完整卡片] 标题\n正文。', rendered)
        self.assertNotIn('[[', rendered)
        self.assertEqual(offered[0]['delivery'], 'full')
        self.assertEqual(offered[0]['body'], '正文。')
        big = dict(card, id='b' * 32, body='巨' * 9000)
        rendered2, _delivered2, offered2 = render_context([big], [], [],
                                                          lambda c: '资料路径')
        self.assertIn('- [资料位置，需读取全文] 标题\n资料路径', rendered2)
        self.assertNotIn('[[', rendered2)
        self.assertEqual(offered2[0]['delivery'], 'locator')
        self.assertEqual(offered2[0]['body'], '')


if __name__ == '__main__':
    unittest.main()
