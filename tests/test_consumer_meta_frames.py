"""Genuine behaviour tests for the consumer generic-frame repair (2026-09-30).

Synthetic fixtures only: no live vault, model, credentials or network, and no
real session text or card id.

The failure being repaired: after an explicit concrete discussion, an ordinary
reflexive assistant-behaviour / appraisal question (``你自己会查询背景资料吗`` /
``你感觉回答质量有改变吗`` / ``你自己查询时感觉顺手吗`` / ``您自己怎么看现在
的效果``) was still classified as a *new subject*, because the bounded frame left
generic self-reference, lookup and subjective-appraisal vocabulary behind. The
turn then fell through to the ordinary direct rank, where an unrelated
cross-domain card that merely repeats those generic words could enter.

The repair adds a small, topic-free class (``自己``/``查询``/``感觉``/``看``/
``觉得``) to the bounded frame vocabulary, so those turns reduce to no concrete
remainder and recover the bounded same-session subject instead. It does not
recognise whole sentences, does not assume any second-person question is a
follow-up, and does not strip a concrete noun, so a fresh topic such as
``你自己会查询海豚迁徙吗`` / ``你感觉火山喷发怎样`` / ``换个话题，查询松鼠资料``
/ ``CedarFlow和MossDesk有什么不同`` still never carries the previous subject.
"""
import json
import tempfile
import unittest
from pathlib import Path

from tongle.context_retrieval import (is_continuation, is_referential_followup,
                                      rank_consumer, select_consumer_topic,
                                      specific_terms)
from tongle.engine import Engine
from tongle.evolution import Evolution
from tongle.knowledge_files import KnowledgeFiles


class MetaFrameDetectorTests(unittest.TestCase):
    """The detector reasons about the bounded frame, never about an address."""

    POSITIVES = (
        '你自己会查询背景资料吗？',
        '你感觉回答质量有改变吗？',
        '你自己查询时感觉顺手吗？',
        '您自己怎么看现在的效果？',
        # same small class, different ordering / punctuation
        '现在的效果您自己怎么看',
        '回答质量有改变吗，你感觉？',
        '你现在感觉回答质量有改变吗！',
        '查询背景资料时，你自己感觉顺手吗？',
        # one combined multi-clause example
        '你自己会查询背景资料吗，你感觉回答质量有改变吗？',
    )

    NEGATIVES = (
        '你自己会查询海豚迁徙吗',
        '你感觉火山喷发怎样',
        '换个话题，查询松鼠资料',
        'CedarFlow和MossDesk有什么不同',
        # second person / short length with a concrete remainder never carries
        '你会做披萨吗',
        '你能解释海豚吗',
        '你觉得自己会做寿司吗',
        '海豚怎么定位',
    )

    def test_meta_questions_are_generic_referrals(self):
        for q in self.POSITIVES:
            with self.subTest(q=q):
                self.assertTrue(is_referential_followup(q), q)
                self.assertFalse(is_continuation(q), q)

    def test_fresh_topics_are_not_referrals(self):
        for q in self.NEGATIVES:
            with self.subTest(q=q):
                self.assertFalse(is_referential_followup(q), q)

    def test_second_person_or_short_is_not_enough(self):
        # The new frame words are generic; they must not turn a short or
        # second-person question that still names a concrete thing into a carry.
        for q in ('你能解释海豚吗', '你会做披萨吗', '海豚怎么定位',
                  '你查一下火山喷发', '你看这个海豚迁徙'):
            with self.subTest(q=q):
                self.assertFalse(is_referential_followup(q), q)


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


class MetaFrameIntegrationTests(_Harness):
    """A meta question recovers the bounded same-session subject and, unlike the
    old behaviour, does not admit the cross-domain card that repeats the generic
    self-reference/lookup/appraisal words."""

    SESSION = 'meta-frames'
    PRIOR = '我们正在讨论 CedarFlow 的事项。'

    def _session(self):
        target = self.seed('project', 'CedarFlow事项', 'knowledge', '事项',
                           'CedarFlow事项', 'CedarFlow 由独立小组负责推进。')
        # An unrelated cross-domain card whose *body* only repeats the generic
        # self-reference/lookup/appraisal words a naive rank would match; its
        # heading carries none of them, so it can never be a heading-level
        # subject match for a fresh topic either.
        decoy = self.seed('shared', '助手行为说明', 'knowledge', '助手',
                          '助手行为说明',
                          '查询背景资料并给出回答，改动后感觉更顺手，质量与效果都有改变。')
        extra = self.seed('project', '陶艺窑炉记录', 'knowledge', '窑炉',
                          '陶艺窑炉记录', '陶艺窑炉需要有人盯着，保证稳定运转。')
        self.publish()
        self.capture(self.PRIOR, session=self.SESSION, event='p1')
        return target, decoy, extra

    # One designated query that actually repeats the decoy body's generic
    # vocabulary (查询/感觉/顺手). The two ``怎么看`` wordings share none of the
    # decoy's words, so the contamination proof cannot be made for every
    # variant; the product assertions below still must hold for all of them.
    CONTAMINATING = '你自己查询时感觉顺手吗？'

    def test_plain_rank_is_contaminated_by_the_generic_decoy(self):
        target, decoy, _extra = self._session()
        admitted = [c['id'] for c in rank_consumer(
            [target, decoy], self.CONTAMINATING, specific_terms(self.CONTAMINATING),
            lambda c: c['project'] == 'project')]
        # The contamination is real: the plain rank admits the weak
        # cross-domain card on the generic words alone, and not the subject.
        self.assertIn(decoy['id'], admitted)
        self.assertNotIn(target['id'], admitted)

    def test_meta_question_recovers_subject_not_cross_domain_card(self):
        target, decoy, _extra = self._session()
        for q in MetaFrameDetectorTests.POSITIVES:
            with self.subTest(q=q):
                ids = self.ids(self.ev.context('project', q, session=self.SESSION))
                self.assertIn(target['id'], ids, q)
                self.assertNotIn(decoy['id'], ids, q)

    def test_meta_question_does_not_leak_an_extra_own_card(self):
        target, _decoy, extra = self._session()
        for q in ('你自己会查询背景资料吗？', '您自己怎么看现在的效果？',
                  '你自己会查询背景资料吗，你感觉回答质量有改变吗？'):
            with self.subTest(q=q):
                ids = self.ids(self.ev.context('project', q, session=self.SESSION))
                self.assertIn(target['id'], ids, q)
                self.assertNotIn(extra['id'], ids, q)

    def test_fresh_topic_never_carries_previous_subject(self):
        target, _decoy, _extra = self._session()
        for q in ('你自己会查询海豚迁徙吗', '你感觉火山喷发怎样',
                  '换个话题，查询松鼠资料'):
            with self.subTest(q=q):
                self.assertNotIn(
                    target['id'],
                    self.ids(self.ev.context('project', q, session=self.SESSION)), q)

    def test_explicit_comparison_uses_named_subjects_not_carry(self):
        target, decoy, _extra = self._session()
        moss = self.seed('project', 'MossDesk事项', 'knowledge', '事项',
                         'MossDesk事项', 'MossDesk 有独立适用范围。')
        self.publish()
        q = 'CedarFlow和MossDesk有什么不同'
        self.assertFalse(is_referential_followup(q))
        self.assertFalse(is_continuation(q))
        ids = self.ids(self.ev.context('project', q, session=self.SESSION))
        self.assertIn(target['id'], ids)
        self.assertIn(moss['id'], ids)
        self.assertNotIn(decoy['id'], ids)

    def test_fresh_concrete_topic_surfaces_its_own_card(self):
        target, decoy, _extra = self._session()
        dolphin = self.seed('project', '海豚迁徙', 'knowledge', '生物',
                           '海豚迁徙', '海豚按季节迁徙。')
        self.publish()
        ids = self.ids(self.ev.context('project', '你自己会查询海豚迁徙吗',
                                       session=self.SESSION))
        self.assertIn(dolphin['id'], ids)
        self.assertNotIn(target['id'], ids)
        self.assertNotIn(decoy['id'], ids)


class PriorLazinessTests(unittest.TestCase):
    """The prior-turn provider is consulted only for a genuine referral."""

    CARD = {'id': 't' * 32, 'project': 'project', 'key': 'CedarFlow事项',
            'title': 'CedarFlow事项', 'topic': '事项',
            'body': 'CedarFlow 由独立小组负责推进。', 'kind': 'knowledge',
            'version': 1, 'updated_at': 1}

    def test_provider_used_only_for_a_meta_referral(self):
        calls = []

        def provider():
            calls.append(1)
            return [('我们正在讨论 CedarFlow 的事项。', 1.0, 's1')]

        self.assertEqual(
            select_consumer_topic([self.CARD], '你自己会查询海豚迁徙吗',
                                  'project', provider), [])
        self.assertEqual(calls, [])
        got = select_consumer_topic([self.CARD], '您自己怎么看现在的效果？',
                                    'project', provider)
        self.assertEqual([c['id'] for c in got], [self.CARD['id']])
        self.assertEqual(calls, [1])


if __name__ == '__main__':
    unittest.main()
