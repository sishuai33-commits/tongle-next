"""Bounded consumer *request interpretation* refinement (2026-10-02).

Synthetic fixtures only: no live vault, model, credentials or network, and no
real session text or card id. Where a card is seeded the test proves machine
behaviour, not semantic quality.

The independent broader replay found the earlier turn-level suppression
*structurally* wrong: it detected a whole-turn flag and then still let a
negated old-topic clause feed subject resolution, and it disabled correction
notices even when a fresh positive subject was retained. This suite covers the
one coherent clause-role interpretation that replaced it:

1. an unscoped "no history" / acknowledgement-only request yields nothing (no
   cards, no raw fallback, no notice, no receipt) -- including the negative
   verbs 引用/参考/使用 and ``请只确认收到`` == ``只需回一句已知悉`` -- while a
   bare ``回一句`` / ``无需展开`` alone does not (a short factual answer still
   needs evidence), and quoted/backticked content is not a user command;
2. a *scoped* negation of an old subject masks only that clause: a positive
   current question keeps its subject, and the raw fallback / correction notice
   / receipt stay on for that present subject;
3. a concrete CJK subject named without ``的`` beats a prior carry, while a
   pure deictic referral still carries and a generic-title decoy never wins;
4. the legacy adapter fallback crops on the UTF-16 boundary so an astral
   payload can never overflow the fixed 6000-unit bound.
"""
import json
import random
import sys
import tempfile
import unittest
from pathlib import Path

from tongle.context_retrieval import (_longest_common_substring,
                                      interpret_request,
                                      suppresses_current_memory)
from tongle.engine import Engine
from tongle.evolution import Evolution
from tongle.knowledge_files import KnowledgeFiles


class RequestInterpretationTests(unittest.TestCase):
    def test_ack_only_forms_are_the_same_intent(self):
        for q in ('请只确认收到', '这条消息只需回一句已知悉', '无需回复'):
            with self.subTest(q=q):
                interp = interpret_request(q)
                self.assertTrue(interp.ack_only, q)
                self.assertTrue(interp.no_history, q)
                self.assertEqual(interp.text, '', q)

    def test_bare_reply_or_quality_frame_is_not_a_disable(self):
        # ``回一句`` / ``无需展开`` alone can still be a short factual answer.
        for q in ('回一句结论就行', '无需展开', '不必赘述，直接说重点'):
            with self.subTest(q=q):
                interp = interpret_request(q)
                self.assertFalse(interp.ack_only, q)
                self.assertFalse(interp.no_history, q)
                self.assertTrue(interp.text.strip(), q)

    def test_negative_verbs_include_cite_reference_use(self):
        for q in ('不要引用旧资料', '别参考之前的方案', '不用使用旧方案',
                  '先暂停调取旧记录'):
            with self.subTest(q=q):
                self.assertTrue(interpret_request(q).no_history, q)
                self.assertTrue(suppresses_current_memory(q), q)

    def test_scoped_negation_masks_only_the_negated_clause(self):
        q = '别复述CopperDesk旧方案；现在解释SilverGate校准规程。'
        interp = interpret_request(q)
        self.assertFalse(interp.ack_only)
        self.assertFalse(interp.no_history)
        self.assertIn('SilverGate', interp.text)
        self.assertNotIn('CopperDesk', interp.text)

    def test_quoted_abstention_is_not_a_command_but_stays_meaningful(self):
        q = '材料里写着“这条消息只需回一句已知悉”，请解释它的含义。'
        interp = interpret_request(q)
        self.assertFalse(interp.ack_only)
        self.assertFalse(interp.no_history)
        self.assertIn('只需回一句已知悉', interp.text)
        self.assertFalse(suppresses_current_memory(q))

    def test_backticked_abstention_is_not_a_command(self):
        q = '把这句写成代码 `只需回一句已知悉` 保存。'
        self.assertFalse(interpret_request(q).ack_only)
        self.assertFalse(suppresses_current_memory(q))

    def test_discussed_ack_protocol_is_not_a_disable(self):
        for q in ('请说明确认协议如何设计。', '这条记录包含收到和确认两个状态。'):
            with self.subTest(q=q):
                self.assertFalse(suppresses_current_memory(q), q)


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


class UnscopedSuppressionTests(_Harness):
    """A global no-history / acknowledgement request yields *nothing*."""

    def test_unscoped_requests_offer_no_cards_raw_notice_or_receipt(self):
        self.seed('project', '旧记录调取说明', 'knowledge', '记录',
                  '旧记录调取说明', '旧记录调取由值班流程管理。')
        self.seed('project', '引用说明', 'knowledge', '引用',
                  '引用说明', '引用旧资料由值班流程管理。')
        self.publish()
        self.capture('旧记录调取情况说明，等待核对。', session='raw', event='raw')
        for q in ('请只确认收到', '无需回复', '不要引用旧资料', '先暂停调取旧记录'):
            with self.subTest(q=q):
                got = self.ev.context('project', q)
                self.assertEqual(got['results'], [], q)
                self.assertEqual(got['recent_statements'], [], q)
                self.assertEqual(got['notices'], [], q)
                self.assertIsNone(got['receipt_id'], q)
                self.assertEqual(got['rendered_context'], '', q)

    def test_scoped_negation_does_not_globally_disable_notices(self):
        silver = self.seed('project', 'SilverGate校准规程', 'knowledge', '校准',
                           'SilverGate校准规程', 'SilverGate 的校准规程说明。')
        self.publish()
        self.capture('更正：今天的排班改为下午。', session='corr', event='c1')
        q = '别复述CopperDesk旧方案；现在解释SilverGate校准规程。'
        got = self.ev.context('project', q)
        self.assertIn(silver['id'], self.ids(got))
        self.assertTrue(got['notices'])
        self.assertIsNotNone(got['receipt_id'])

    def test_quoted_abstention_keeps_the_discussion_card(self):
        card = self.seed('project', '材料含义说明', 'knowledge', '材料',
                         '材料含义说明', '材料含义说明正文。')
        self.publish()
        q = '材料里写着“这条消息只需回一句已知悉”，请解释它的含义。'
        got = self.ev.context('project', q)
        self.assertIn(card['id'], self.ids(got))

    def test_short_factual_answer_still_retrieves_evidence(self):
        card = self.seed('project', '冷链密封许可', 'knowledge', '物流',
                         '冷链密封许可', '冷链密封许可的有效条件。')
        self.publish()
        q = '只需回一句：冷链密封许可怎么处理'
        self.assertFalse(interpret_request(q).ack_only)
        self.assertIn(card['id'], self.ids(self.ev.context('project', q)))


class ScopedNegationRetrievalTests(_Harness):
    """A negated old subject masks only itself; the new subject stays live."""

    QUERY = '别复述CopperDesk旧方案；现在解释SilverGate校准规程。'

    def _cards(self):
        copper = self.seed('project', 'CopperDesk旧方案', 'knowledge', '方案',
                           'CopperDesk旧方案', 'CopperDesk 的旧方案说明。')
        silver = self.seed('project', 'SilverGate校准规程', 'knowledge', '校准',
                           'SilverGate校准规程', 'SilverGate 的校准规程说明。')
        self.publish()
        return copper, silver

    def test_only_the_positive_current_subject_is_offered(self):
        copper, silver = self._cards()
        ids = self.ids(self.ev.context('project', self.QUERY))
        self.assertIn(silver['id'], ids)
        self.assertNotIn(copper['id'], ids)

    def test_raw_fallback_stays_on_for_the_present_subject(self):
        self._cards()
        related = self.capture(
            'SilverGate 校准规程本轮另有人工核验条件，等待核对。',
            session='raw-rel', event='r1')
        unrelated = self.capture(
            'CopperDesk 旧方案本轮另有旧说明，等待核对。',
            session='raw-unrel', event='r2')
        got = self.ev.context('project', self.QUERY)
        raw_ids = [r['source_id'] for r in got['recent_statements']]
        self.assertIn(related['source_id'], raw_ids)
        self.assertNotIn(unrelated['source_id'], raw_ids)

    def test_pending_correction_stays_scoped_to_the_present_subject(self):
        silver = self.seed('project', 'SilverGate校准规程', 'knowledge', '校准',
                           'SilverGate校准规程', 'SilverGate 的校准规程说明。')
        self.publish()
        self.capture('更正：SilverGate 校准规程每周复核一次。',
                     session='corr', event='c1')
        got = self.ev.context('project', self.QUERY)
        self.assertIn(silver['id'], self.ids(got))
        self.assertTrue(got['notices'])

    def test_unknown_new_subject_still_blanks(self):
        self._cards()
        got = self.ev.context('project', '别复述CopperDesk旧方案；现在解释ZorBlax校准规程。')
        self.assertEqual(got['results'], [])


class ConcreteCJKSubjectTests(_Harness):
    """A concrete CJK subject without ``的`` beats a prior carry, but a pure
    deictic referral still carries and a generic-title decoy never wins."""

    def _snow(self, session, text='我们刚才在讨论雪松助手。'):
        card = self.seed('project', '雪松助手', 'knowledge', '助手',
                         '雪松助手', '雪松助手说明。')
        self.publish()
        self.capture(text, session=session, event='p1')
        return card

    def test_deictic_concrete_subject_beats_prior_carry(self):
        snow = self._snow('c1')
        seal = self.seed('project', '仓储封签规程', 'knowledge', '规程',
                         '仓储封签规程', '仓储封签规程说明。')
        self.publish()
        got = self.ev.context('project', '那个仓储封签规程呢？', session='c1')
        self.assertIn(seal['id'], self.ids(got))
        self.assertNotIn(snow['id'], self.ids(got))

    def test_framing_concrete_subject_beats_prior_carry(self):
        snow = self._snow('c2')
        pavilion = self.seed('project', '青沙馆布展安排', 'knowledge', '布展',
                             '青沙馆布展安排', '青沙馆布展说明。')
        self.publish()
        got = self.ev.context('project', '我们接着核对青沙馆布展。', session='c2')
        self.assertIn(pavilion['id'], self.ids(got))
        self.assertNotIn(snow['id'], self.ids(got))

    def test_pure_deictic_referral_still_carries_even_with_generic_decoy(self):
        snow = self._snow('c3', text='我们在讨论雪松助手。')
        decoy = self.seed('shared', '刚才那个说明', 'knowledge', '说明',
                          '刚才那个说明', '刚才那个说明正文。')
        self.publish()
        got = self.ev.context('project', '刚才那个怎么样？', session='c3')
        self.assertIn(snow['id'], self.ids(got))
        self.assertNotIn(decoy['id'], self.ids(got))

    def test_discussion_of_ack_keeps_the_card_and_drops_generic_noise(self):
        card = self.seed('project', '确认协议设计', 'knowledge', '协议',
                         '确认协议设计', '确认协议设计由现场主管维护。')
        decoy = self.seed('shared', '助手行为说明', 'knowledge', '助手',
                          '助手行为说明',
                          '确认收到并给出回答，改动后感觉更顺手。')
        self.publish()
        got = self.ev.context('project', '请说明确认协议如何设计。')
        self.assertIn(card['id'], self.ids(got))
        self.assertNotIn(decoy['id'], self.ids(got))


class LegacyAdapterUtf16Tests(unittest.TestCase):
    """The legacy fallback must crop on the UTF-16 boundary, not code points."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'adapters'))
        import common
        self.common = common

    def _cfg(self, body):
        script = self.root / 'core'
        script.write_text('#!' + sys.executable +
                          '\nimport sys\nsys.stdout.write(' + repr(body) + ')\n')
        script.chmod(0o755)
        return self.common.Config(home=str(self.root), project='p',
                                  python=sys.executable, root=str(self.root),
                                  cli_override=str(script))

    def test_legacy_astral_fallback_never_overflows_utf16_bound(self):
        big = {'ok': True,
               'results': [{'text': '\U0001F600' * 4000, 'status': 'candidate'}],
               'receipt_id': 'legacy-astral'}
        cfg = self._cfg(json.dumps(big))
        receipt = {}
        out, warn = self.common.fetch_context(cfg, '问题', session_id='s',
                                              receipt=receipt)
        self.assertIsNone(warn)
        self.assertIsNotNone(out)
        self.assertLessEqual(self.common._utf16_length(out),
                             self.common.QUERY_CONTEXT_BUDGET_CHARS)
        self.assertIn('truncated', out)
        # Honest truncation: no receipt is ever bound on the legacy path.
        self.assertEqual(receipt, {})

    def test_utf16_prefix_counts_astral_units(self):
        self.assertEqual(self.common._utf16_length('\U0001F600'), 2)
        self.assertEqual(self.common._utf16_prefix('a\U0001F600b', 3), 'a\U0001F600')
        self.assertEqual(self.common._utf16_prefix('a\U0001F600b', 2), 'a')
        self.assertLessEqual(self.common._utf16_length(
            self.common._utf16_truncate('\U0001F600' * 5000, 100, '…')), 100)


class NominalAcknowledgementFrameTests(_Harness):
    """A nominal ``only/request`` acknowledgement frame plus plain negation
    particles disables the whole turn, while discussing an acknowledgement
    protocol (or a quoted example) does not.

    Synthetic fixtures only; names are invented placeholders, never real
    session text or card ids.
    """

    QUERY = ('资料稍后再补充；现在仅作收件确认，不引用旧决定，'
             '也不用接续麦桥影社话题。')

    def test_nominal_ack_frame_is_ack_only(self):
        interp = interpret_request(self.QUERY)
        self.assertTrue(interp.ack_only)
        self.assertTrue(interp.no_history)
        self.assertEqual(interp.text, '')
        self.assertTrue(suppresses_current_memory(self.QUERY))

    def test_nominal_ack_turn_offers_nothing(self):
        self.seed('project', '旧决定收件确认', 'knowledge', '决定',
                  '旧决定收件确认', '旧决定收件确认由值班流程管理。')
        self.seed('project', '麦桥影社海报安排', 'knowledge', '海报',
                  '麦桥影社海报安排', '麦桥影社海报安排由独立小组负责。')
        self.publish()
        self.capture('旧决定收件确认与麦桥影社海报安排等待核对。',
                     session='raw', event='raw')
        got = self.ev.context('project', self.QUERY)
        self.assertEqual(got['results'], [])
        self.assertEqual(got['recent_statements'], [])
        self.assertEqual(got['notices'], [])
        self.assertIsNone(got['receipt_id'])
        self.assertEqual(got['rendered_context'], '')

    def test_plain_negation_particles_are_recognized(self):
        # Bounded particle+verb grammar (不/先不/也不用 + 引用/接续), never a
        # whole-turn ban and never only a list of complete phrases.
        for q in ('不引用旧决定', '先不引用旧资料', '也不用接续旧话题',
                  '不要接续旧话题'):
            with self.subTest(q=q):
                self.assertTrue(suppresses_current_memory(q), q)

    def test_discussing_an_ack_protocol_is_not_a_disable(self):
        for q in ('收件确认流程怎么做', '请说明收件确认流程如何设计。'):
            with self.subTest(q=q):
                self.assertFalse(interpret_request(q).ack_only, q)
                self.assertFalse(suppresses_current_memory(q), q)

    def test_quoted_nominal_ack_is_not_a_disable(self):
        q = '材料里写着“现在仅作收件确认”，请解释它的含义。'
        self.assertFalse(interpret_request(q).ack_only)
        self.assertFalse(suppresses_current_memory(q))


class PartialOldSubjectNegationTests(_Harness):
    """A scoped negation of one old subject masks only that clause and keeps
    the fresh positive subject live (``不要接续X...只说明Y`` / ``先不引用X旧
    话题；现在讨论Y``). No whole-turn ban for a partial old-subject negation.
    """

    def test_continuation_negation_keeps_only_the_fresh_subject(self):
        old = self.seed('project', '麦桥影社海报安排', 'knowledge', '海报',
                        '麦桥影社海报安排', '麦桥影社海报安排由独立小组负责。')
        survey = self.seed('project', '白榆测绘队作业准则', 'knowledge', '准则',
                           '白榆测绘队作业准则', '白榆测绘队作业准则说明。')
        self.publish()
        q = '不要接续麦桥影社海报安排，只说明白榆测绘队作业准则。'
        interp = interpret_request(q)
        self.assertFalse(interp.ack_only)
        self.assertFalse(interp.no_history)
        self.assertNotIn('麦桥影社', interp.text)
        self.assertIn('白榆测绘队', interp.text)
        ids = self.ids(self.ev.context('project', q))
        self.assertIn(survey['id'], ids)
        self.assertNotIn(old['id'], ids)

    def test_bare_negation_keeps_only_the_fresh_subject(self):
        survey = self.seed('project', '白榆测绘队作业准则', 'knowledge', '准则',
                           '白榆测绘队作业准则', '白榆测绘队作业准则说明。')
        library = self.seed('project', '瓷河图书馆闭馆清点', 'knowledge', '清点',
                            '瓷河图书馆闭馆清点', '瓷河图书馆闭馆清点说明。')
        self.publish()
        q = '先不引用白榆测绘队旧话题；现在讨论瓷河图书馆闭馆清点。'
        interp = interpret_request(q)
        self.assertFalse(interp.no_history)
        self.assertNotIn('白榆测绘队', interp.text)
        self.assertIn('瓷河图书馆', interp.text)
        ids = self.ids(self.ev.context('project', q))
        self.assertIn(library['id'], ids)
        self.assertNotIn(survey['id'], ids)


class ConversationalFrameBeforeSubjectTests(_Harness):
    """A conversational frame before the first ``的`` must not stop subject
    resolution from examining the positive substantive clauses after it, while
    a genuine leading named subject still keeps focus.
    """

    SESSION = 'frame-subject'

    def _cards(self):
        survey = self.seed('project', '白榆测绘队风速超标处理', 'knowledge', '风速',
                           '白榆测绘队风速超标处理', '白榆测绘队风速超标时按预案处理。')
        library = self.seed('project', '瓷河图书馆闭馆清点', 'knowledge', '清点',
                            '瓷河图书馆闭馆清点', '瓷河图书馆闭馆清点说明。')
        postal = self.seed('project', '旧决定收件确认', 'knowledge', '决定',
                           '旧决定收件确认', '旧决定收件确认说明。')
        self.publish()
        return survey, library, postal

    def test_frame_before_de_still_resolves_the_named_subject(self):
        survey, library, postal = self._cards()
        self.capture('我们刚才在讨论瓷河图书馆闭馆清点和旧决定收件确认。',
                     session=self.SESSION, event='p1')
        q = '我们回到前面的讨论，白榆测绘队风速超标时应怎样处理？'
        ids = self.ids(self.ev.context('project', q, session=self.SESSION))
        self.assertIn(survey['id'], ids)
        self.assertNotIn(library['id'], ids)
        self.assertNotIn(postal['id'], ids)

    def test_leading_named_subject_still_keeps_focus(self):
        main = self.seed('project', '岚谷温室维护安排', 'knowledge', '设施',
                         '岚谷温室维护安排', '岚谷温室有独立许可条件。')
        other = self.seed('project', '南岭设施低温灌溉', 'knowledge', '设施',
                          '南岭设施低温灌溉', '南岭设施的低温灌溉只适用于该设施。')
        self.publish()
        ids = self.ids(self.ev.context('project', '岚谷温室的低温灌溉怎么安排？'))
        self.assertIn(main['id'], ids)
        self.assertNotIn(other['id'], ids)


class LongestCommonSubstringEquivalenceTests(unittest.TestCase):
    """The C-search longest-common-substring is exactly equivalent to the
    original DP, including the earliest-end-in-``a`` tie-break. A fixed seed
    keeps the randomized cases reproducible.
    """

    @staticmethod
    def _reference_dp(a, b):
        # The original implementation, kept as the semantic reference.
        if not a or not b:
            return ''
        previous = [0] * (len(b) + 1)
        best = 0
        end = 0
        for i in range(1, len(a) + 1):
            current = [0] * (len(b) + 1)
            char = a[i - 1]
            for j in range(1, len(b) + 1):
                if char == b[j - 1]:
                    current[j] = previous[j - 1] + 1
                    if current[j] > best:
                        best = current[j]
                        end = i
            previous = current
        return a[end - best:end]

    def test_random_equivalence_to_reference_dp(self):
        rng = random.Random(20261002)
        alphabet = 'abc'
        for _ in range(500):
            a = ''.join(rng.choice(alphabet) for _ in range(rng.randint(0, 14)))
            b = ''.join(rng.choice(alphabet) for _ in range(rng.randint(0, 14)))
            with self.subTest(a=a, b=b):
                self.assertEqual(_longest_common_substring(a, b),
                                 self._reference_dp(a, b))

    def test_boundary_and_tie_cases(self):
        self.assertEqual(_longest_common_substring('', 'abc'), '')
        self.assertEqual(_longest_common_substring('abc', ''), '')
        self.assertEqual(_longest_common_substring('zzz', 'abc'), '')
        # Earliest occurrence in the first string wins.
        self.assertEqual(_longest_common_substring('ab', 'ba'), 'a')
        self.assertEqual(_longest_common_substring('abcab', 'cab'), 'cab')
        # Equal-length strings still take the earliest start.
        self.assertEqual(_longest_common_substring('xab', 'aby'), 'ab')


from tongle.context_retrieval import (MIN_SUBJECT_PHRASE, _CJK_RUN,
                                      _cjk_heading_matches, _frame_residual,
                                      _heading_cjk_runs, request_role_scan,
                                      resolve_query_subject)
from tongle.context_query import discussion_text


class SubjectCJKWorkEliminationEquivalenceTests(unittest.TestCase):
    """Deduplicated / hoisted CJK subject selection is *exactly* equivalent to
    the literal old nested loop (heading runs re-extracted once per query run,
    no dedup).

    Both updates are a strict ``>`` over first-occurrence run order, so dropping
    later duplicate query runs or duplicate per-card heading runs can never change
    the winning string, its tie order, the strong-card set or the bigram term
    set. Synthetic random cases use a fixed seed and deliberately mix repeated
    query phrases, duplicate key/title headings and equal-length ties. Only
    synthetic CJK placeholders are used -- no real session text or card id.
    """

    ALPHABET = '甲乙丙丁戊己庚辛'

    @classmethod
    def _rand_run(cls, rng, lo=1, hi=6):
        return ''.join(rng.choice(cls.ALPHABET)
                       for _ in range(rng.randint(lo, hi)))

    @staticmethod
    def _reference_heading_matches(scope, cards):
        # The literal old inner loop: heading runs extracted per query run, no
        # dedup, earliest-first strict ``>`` update.
        scope_runs = _CJK_RUN.findall(scope)
        if not scope_runs:
            return set(), set(), set()
        ids, terms, phrases = set(), set(), set()
        for card in cards:
            best = ''
            for run in scope_runs:
                for heading_run in _heading_cjk_runs(card):
                    common = _longest_common_substring(run, heading_run)
                    if len(common) > len(best):
                        best = common
            if len(best) >= MIN_SUBJECT_PHRASE and _frame_residual(best):
                ids.add(card.get('id'))
                phrases.add(best)
                terms |= {best[i:i + 2] for i in range(len(best) - 1)}
        return ids, terms, phrases

    @staticmethod
    def _reference_resolve(query, cards):
        # Pure-CJK cards/query: no ASCII identifiers, so ``known``/``unknown``
        # stay empty and only the leading-phrase loop can contribute.
        text = discussion_text(query)
        lead = text.split('\u7684', 1)[0] if '\u7684' in text else ''
        cjk_terms, strong_ids = set(), set()
        if lead:
            lead_runs = _CJK_RUN.findall(lead)
            for card in cards:
                best = ''
                for run in lead_runs:
                    for heading_run in _heading_cjk_runs(card):
                        common = _longest_common_substring(run, heading_run)
                        if len(common) > len(best):
                            best = common
                if len(best) >= MIN_SUBJECT_PHRASE:
                    strong_ids.add(card.get('id'))
                    cjk_terms |= {best[i:i + 2]
                                  for i in range(len(best) - 1)}
        strong, seen = [], set()
        for card in cards:
            cid = card.get('id')
            if cid in seen:
                continue
            if cid in strong_ids:
                strong.append(card)
                seen.add(cid)
        return cjk_terms, strong, False

    @classmethod
    def _draw_scope(cls, rng):
        # Building query pieces from fragments of one heading creates real
        # length-3 matches, while drawing them with replacement creates the
        # repeated phrases whose elimination the equivalence claims.
        heading = cls._rand_run(rng, 3, 7)
        pool = [heading[i:i + 3] for i in range(max(1, len(heading) - 2))]
        pool.append(cls._rand_run(rng, 1, 2))
        pieces = [rng.choice(pool) for _ in range(rng.randint(0, 6))]
        return heading, pool, ' '.join(pieces)

    def test_cjk_heading_matches_equals_literal_nested_loop(self):
        rng = random.Random(20261003)
        for _ in range(400):
            heading, pool, scope = self._draw_scope(rng)
            cards = []
            for cid in range(rng.randint(1, 4)):
                key = rng.choice(pool + [heading, self._rand_run(rng, 1, 6)])
                # Duplicate key/title headings exercise per-card dedup.
                title = key if rng.random() < 0.5 else self._rand_run(rng, 1, 6)
                topic = key if rng.random() < 0.3 else self._rand_run(rng, 1, 6)
                cards.append({'id': 'c%d' % cid, 'key': key, 'title': title,
                              'topic': topic})
            with self.subTest(scope=scope, cards=cards):
                self.assertEqual(_cjk_heading_matches(scope, cards),
                                 self._reference_heading_matches(scope, cards))

    def test_resolve_query_subject_equals_literal_nested_loop(self):
        rng = random.Random(20261004)
        for _ in range(300):
            heading, pool, scope = self._draw_scope(rng)
            query = scope + '\u7684' + self._rand_run(rng, 0, 3)
            cards = []
            for cid in range(rng.randint(1, 3)):
                key = rng.choice(pool + [heading, self._rand_run(rng, 1, 6)])
                title = key if rng.random() < 0.5 else self._rand_run(rng, 1, 6)
                cards.append({'id': 'c%d' % cid, 'key': key, 'title': title,
                              'topic': self._rand_run(rng, 1, 6)})
            with self.subTest(query=query, cards=cards):
                self.assertEqual(resolve_query_subject(query, cards),
                                 self._reference_resolve(query, cards))

    def test_repeated_runs_and_ties_are_handled_exactly(self):
        cards = [{'id': 'c1', 'key': '甲乙丙丁戊己', 'title': '', 'topic': ''}]
        # Two distinct length-3 runs tie; the first-occurrence run wins its
        # substring, and a repeated run cannot change that.
        without_dup = _cjk_heading_matches('甲乙丙 丁戊己', cards)
        with_dup = _cjk_heading_matches('甲乙丙 甲乙丙 丁戊己', cards)
        self.assertEqual(without_dup, ({'c1'}, {'甲乙', '乙丙'}, {'甲乙丙'}))
        self.assertEqual(with_dup, without_dup)
        # Duplicate headings collapse to the same result as a single heading.
        dup_heading = [{'id': 'c1', 'key': '甲乙丙丁戊己',
                        'title': '甲乙丙丁戊己', 'topic': '甲乙丙丁戊己'}]
        self.assertEqual(_cjk_heading_matches('甲乙丙', dup_heading), without_dup)


class ClauseExhaustionAckTests(_Harness):
    """An ``only``-marker + ack predicate must exhaust its clause except
    bounded particles/fillers/punctuation; a concrete subject anywhere in the
    clause keeps it genuine content. Synthetic placeholders only."""

    def test_marker_predicate_with_subject_is_content(self):
        for q in ('只需知道白榆测绘队结果',
                  '只要确认白榆测绘队协议没问题，我们就开工',
                  '我仅需收到白榆测绘队文件后再归档',
                  '请只回答你知道的白榆测绘队部分'):
            with self.subTest(q=q):
                interp = interpret_request(q)
                self.assertFalse(interp.ack_only, q)
                self.assertFalse(interp.no_history, q)
                self.assertIn('白榆测绘队', interp.text, q)
                self.assertFalse(suppresses_current_memory(q), q)

    def test_content_subject_card_is_offered(self):
        card = self.seed('project', '白榆测绘队结果汇总', 'knowledge', '测绘',
                         '白榆测绘队结果汇总', '白榆测绘队结果汇总说明。')
        self.publish()
        ids = self.ids(self.ev.context('project', '只需知道白榆测绘队结果'))
        self.assertIn(card['id'], ids)

    def test_bare_ack_forms_stay_ack_only(self):
        for q in ('现在仅作收件确认', '请只确认收到', '只需回一句已知悉'):
            with self.subTest(q=q):
                interp = interpret_request(q)
                self.assertTrue(interp.ack_only, q)
                self.assertTrue(interp.no_history, q)
                self.assertEqual(interp.text, '', q)


class QuotedAckCommandTests(_Harness):
    """An exact command frame carrying a quoted ack word is a genuine
    acknowledgement request; a quoted example inside an explanatory question
    stays content. The quoted text is data -- only the bounded frame around it
    is classified, never executed."""

    def test_quoted_ack_command_frame_is_ack_only(self):
        q = '请只回复"收到"'
        interp = interpret_request(q)
        self.assertTrue(interp.ack_only)
        self.assertTrue(interp.no_history)
        self.assertEqual(interp.text, '')
        got = self.ev.context('project', q)
        self.assertEqual(got['results'], [])
        self.assertIsNone(got['receipt_id'])

    def test_quoted_example_inside_explanatory_question_stays_content(self):
        q = '为什么有人只回复"收到"就算确认？'
        interp = interpret_request(q)
        self.assertFalse(interp.ack_only)
        self.assertFalse(interp.no_history)
        self.assertFalse(suppresses_current_memory(q))


class InterrogativeNegationDiscussionTests(_Harness):
    """A memory negation embedded in a ``为什么``/``为何`` policy discussion is
    not a directive; a command-position negation still negates."""

    def test_why_embedded_negation_is_not_a_directive(self):
        for q in ('请解释为什么不要引用白榆测绘队非正式来源',
                  '请解释为什么不要提及白榆测绘队名字'):
            with self.subTest(q=q):
                interp = interpret_request(q)
                self.assertFalse(interp.ack_only, q)
                self.assertFalse(interp.no_history, q)
                self.assertIn('白榆测绘队', interp.text, q)
                self.assertFalse(suppresses_current_memory(q), q)

    def test_why_discussion_keeps_the_subject_card(self):
        card = self.seed('project', '白榆测绘队非正式来源', 'knowledge', '来源',
                         '白榆测绘队非正式来源', '白榆测绘队非正式来源的认定说明。')
        self.publish()
        q = '请解释为什么不要引用白榆测绘队非正式来源'
        self.assertIn(card['id'], self.ids(self.ev.context('project', q)))

    def test_command_position_negations_still_negate(self):
        for q in ('不要引用旧资料', '别参考之前的方案', '不引用旧决定'):
            with self.subTest(q=q):
                self.assertTrue(suppresses_current_memory(q), q)
                self.assertTrue(interpret_request(q).no_history, q)

    def test_scoped_bare_negation_still_masks_only_the_old_subject(self):
        survey = self.seed('project', '白榆测绘队作业准则', 'knowledge', '准则',
                           '白榆测绘队作业准则', '白榆测绘队作业准则说明。')
        library = self.seed('project', '瓷河图书馆闭馆清点', 'knowledge', '清点',
                            '瓷河图书馆闭馆清点', '瓷河图书馆闭馆清点说明。')
        self.publish()
        q = '先不引用白榆测绘队旧话题；现在讨论瓷河图书馆闭馆清点。'
        ids = self.ids(self.ev.context('project', q))
        self.assertIn(library['id'], ids)
        self.assertNotIn(survey['id'], ids)

    def test_negation_after_a_why_opener_still_directive(self):
        # 为什么讨论 followed by its own command-position negation: the second
        # clause is a genuine directive and stays masked.
        q = '说说为什么不能参考非标文档；别引用旧资料'
        masked, roles = request_role_scan(discussion_text(q))
        self.assertIn('negate', roles)
        self.assertNotIn('旧资料', masked)


class AckClauseBeforePositiveQuestionTests(_Harness):
    """An ack clause is masked while a later genuine positive question keeps
    its subject; a deferment clause (ack + negation in one clause) keeps the
    turn ack-only, since a deferment is not a positive question."""

    QUERY = '第一句只需回一句已知悉；然后解释白榆测绘队作业准则条件'

    def test_ack_clause_masked_positive_question_kept(self):
        interp = interpret_request(self.QUERY)
        self.assertFalse(interp.ack_only)
        self.assertFalse(interp.no_history)
        self.assertNotIn('已知悉', interp.text)
        self.assertIn('白榆测绘队', interp.text)

    def test_positive_question_still_offers_its_card(self):
        card = self.seed('project', '白榆测绘队作业准则', 'knowledge', '准则',
                         '白榆测绘队作业准则', '白榆测绘队作业准则说明。')
        self.publish()
        ids = self.ids(self.ev.context('project', self.QUERY))
        self.assertIn(card['id'], ids)

    def test_deferment_clause_is_not_a_positive_question(self):
        q = '资料稍后再补充；现在仅作收件确认，不引用旧决定，也不用接续麦桥影社话题'
        interp = interpret_request(q)
        self.assertTrue(interp.ack_only)
        self.assertTrue(interp.no_history)
        self.assertEqual(interp.text, '')


class OrdinaryAsciiWordBeforeCjkSubjectTests(_Harness):
    """An ordinary ASCII instruction word is not a named subject: it never
    discards a known concrete CJK subject in the same turn, while a turn with
    only unknown subjects still stays empty."""

    def test_ascii_instruction_word_does_not_blank_known_cjk_subject(self):
        card = self.seed('project', '青沙馆布展安排', 'knowledge', '布展',
                         '青沙馆布展安排', '青沙馆布展安排说明。')
        self.publish()
        ids = self.ids(self.ev.context('project', 'policy 青沙馆布展怎么安排'))
        self.assertIn(card['id'], ids)

    def test_unknown_only_subject_still_blanks(self):
        self.seed('project', '青沙馆布展安排', 'knowledge', '布展',
                  '青沙馆布展安排', '青沙馆布展安排说明。')
        self.publish()
        got = self.ev.context('project', 'DuskForge 布展怎么安排')
        self.assertEqual(got['results'], [])

    def test_leading_named_subject_still_keeps_focus(self):
        main = self.seed('project', '岚谷温室维护安排', 'knowledge', '设施',
                         '岚谷温室维护安排', '岚谷温室有独立许可条件。')
        other = self.seed('project', '南岭设施低温灌溉', 'knowledge', '设施',
                          '南岭设施低温灌溉', '南岭设施的低温灌溉只适用于该设施。')
        self.publish()
        ids = self.ids(self.ev.context('project', '岚谷温室的低温灌溉怎么安排？'))
        self.assertIn(main['id'], ids)
        self.assertNotIn(other['id'], ids)

    def test_multi_known_focus_unchanged(self):
        survey = self.seed('project', '白榆测绘队作业准则', 'knowledge', '准则',
                           '白榆测绘队作业准则', '白榆测绘队作业准则说明。')
        library = self.seed('project', '瓷河图书馆闭馆清点', 'knowledge', '清点',
                            '瓷河图书馆闭馆清点', '瓷河图书馆闭馆清点说明。')
        noise = self.seed('project', '青沙馆布展安排', 'knowledge', '布展',
                          '青沙馆布展安排', '青沙馆布展安排说明。')
        self.publish()
        ids = self.ids(self.ev.context('project', '白榆测绘队和瓷河图书馆的安排'))
        self.assertIn(survey['id'], ids)
        self.assertIn(library['id'], ids)
        self.assertNotIn(noise['id'], ids)


class ReplyFrameContentExhaustionTests(_Harness):
    """A reply frame plus an ack *word* inside a substantive request is genuine
    content: every ack branch (reply/confirm/nominal) must be an exhaustive
    bounded command -- bounded lead before the frame, bounded ack/filler
    material after it -- so a substantive prefix/suffix disqualifies, while
    bounded lead particles/order words keep the ack reading. Synthetic
    placeholders only."""

    def test_reply_frame_with_substantive_suffix_is_content(self):
        for q in ('只需回一句白榆测绘队确认协议要求的结论',
                  '请只回复白榆测绘队确认协议中的关键条件'):
            with self.subTest(q=q):
                interp = interpret_request(q)
                self.assertFalse(interp.ack_only, q)
                self.assertFalse(interp.no_history, q)
                self.assertIn('白榆测绘队', interp.text, q)
                self.assertFalse(suppresses_current_memory(q), q)

    def test_reply_frame_content_keeps_its_card(self):
        card = self.seed('project', '白榆测绘队确认协议', 'knowledge', '协议',
                         '白榆测绘队确认协议', '白榆测绘队确认协议说明。')
        self.publish()
        for q in ('只需回一句白榆测绘队确认协议要求的结论',
                  '请只回复白榆测绘队确认协议中的关键条件'):
            with self.subTest(q=q):
                ids = self.ids(self.ev.context('project', q))
                self.assertIn(card['id'], ids)

    def test_bounded_lead_particles_keep_the_ack_reading(self):
        for q in ('第一句只需回一句已知悉', '这条消息只需回一句已知悉',
                  '请只确认收到', '只需回一句已知悉', '现在仅作收件确认'):
            with self.subTest(q=q):
                interp = interpret_request(q)
                self.assertTrue(interp.ack_only, q)
                self.assertTrue(interp.no_history, q)
                self.assertEqual(interp.text, '', q)


class UnknownSubjectVersusGenericSuffixTests(_Harness):
    """An explicit unknown named subject stays unknown in *every* letter case:
    a generic CJK suffix after it (``的升级方案``) is never promoted to the
    subject. A genuine known CJK subject in the leading structure still wins
    over a stray ordinary ASCII word. Synthetic placeholders only."""

    def _cards(self):
        generic = self.seed('project', '通用升级方案卡', 'knowledge', '升级',
                            '通用升级方案卡', '升级方案的通用说明。')
        pavilion = self.seed('project', '青沙馆布展安排', 'knowledge', '布展',
                             '青沙馆布展安排', '青沙馆布展安排说明。')
        self.publish()
        return generic, pavilion

    def test_unknown_subject_with_generic_suffix_stays_empty_in_any_case(self):
        generic, _pavilion = self._cards()
        for token in ('drifthollow', 'DriftHollow', 'DRIFTHOLLOW'):
            q = '请说明 %s 的升级方案。' % token
            with self.subTest(q=q):
                got = self.ev.context('project', q)
                self.assertEqual(got['results'], [], q)
                self.assertNotIn(generic['id'], self.ids(got), q)

    def test_ordinary_word_does_not_blank_known_leading_cjk_subject(self):
        generic, pavilion = self._cards()
        ids = self.ids(self.ev.context('project', 'policy 青沙馆布展怎么安排'))
        self.assertIn(pavilion['id'], ids)
        self.assertNotIn(generic['id'], ids)


class LongAckReceiptDefermentTests(_Harness):
    """The frozen long-ack turn: a bounded deferred/no-current-judgment clause
    plus a bounded receipt-confirmation request (``只要确认消息已经收到``) is
    acknowledgement-only -- no cards, no raw, no notices -- while the genuine
    content forms fixed earlier keep their evidence. Synthetic placeholders
    only."""

    LONG_ACK = ('后续安排暂时不做判断。现在只要确认消息已经收到；'
                '请不要带入任何以前项目背景。')

    def _seed_world(self):
        old = self.seed('project', '旧项目背景说明', 'knowledge', '背景',
                        '旧项目背景说明', '旧项目背景说明正文。')
        topic = self.seed('project', '后续安排说明', 'knowledge', '安排',
                          '后续安排说明', '后续安排说明正文。')
        self.publish()
        self.capture('旧项目背景另有补充，等待核对。', session='raw', event='r1')
        return old, topic

    def test_long_ack_deferment_yields_nothing(self):
        self._seed_world()
        interp = interpret_request(self.LONG_ACK)
        self.assertTrue(interp.ack_only)
        self.assertTrue(interp.no_history)
        got = self.ev.context('project', self.LONG_ACK)
        self.assertEqual(got['results'], [])
        self.assertEqual(got['recent_statements'], [])
        self.assertEqual(got['notices'], [])

    def test_bounded_receipt_confirmation_is_ack_only(self):
        for q in ('请只确认收到', '现在只要确认消息已经收到', '只需回一句已知悉'):
            with self.subTest(q=q):
                interp = interpret_request(q)
                self.assertTrue(interp.ack_only, q)
                self.assertTrue(interp.no_history, q)

    def test_receipt_protocol_discussion_stays_content(self):
        # An actual request *about* a receipt/protocol is never an ack.
        for q in ('请说明收件确认协议的设计要求。',
                  '只需回一句白榆测绘队确认协议要求的结论',
                  '请只回复白榆测绘队确认协议中的关键条件'):
            with self.subTest(q=q):
                interp = interpret_request(q)
                self.assertFalse(interp.ack_only, q)
                self.assertTrue(interp.text.strip(), q)

    def test_ack_then_genuine_question_keeps_the_question(self):
        card = self.seed('project', '冷链密封许可', 'knowledge', '物流',
                         '冷链密封许可', '冷链密封许可的有效条件。')
        self.publish()
        q = '第一句只需回一句已知悉；然后解释冷链密封许可的有效条件。'
        interp = interpret_request(q)
        self.assertFalse(interp.ack_only)
        self.assertIn(card['id'], self.ids(self.ev.context('project', q)))


class UnknownSubjectPendingRawTests(_Harness):
    """The unknown-subject decision is *shared* by compiled cards and pending
    raw statements: an explicit unknown named subject constrains raw to the
    actual complete ASCII token (case-insensitive), never borrowing generic
    action words or old session anchors -- while a pending statement actually
    naming that subject is still offered, and a genuine known concrete CJK
    subject keeps its relevant raw. Synthetic placeholders only."""

    def _seed_world(self):
        generic = self.seed('project', '通用升级方案卡', 'knowledge', '升级',
                            '通用升级方案卡', '升级方案的通用说明。')
        pavilion = self.seed('project', '青沙馆布展安排', 'knowledge', '布展',
                             '青沙馆布展安排', '青沙馆布展安排说明。')
        self.publish()
        unrelated = self.capture('木栈系统的升级方案尚待审批，先保留原有部署。',
                                 session='raw-unrel', event='r1')
        return generic, pavilion, unrelated

    def test_unknown_subject_excludes_unrelated_pending_raw_in_any_case(self):
        generic, _pavilion, unrelated = self._seed_world()
        for token in ('drifthollow', 'DriftHollow', 'DRIFTHOLLOW'):
            q = '请说明 %s 的升级方案。' % token
            with self.subTest(q=q):
                got = self.ev.context('project', q)
                self.assertEqual(got['results'], [], q)
                raw_ids = [r['source_id'] for r in got['recent_statements']]
                self.assertNotIn(unrelated['source_id'], raw_ids, q)
                self.assertNotIn(generic['id'], self.ids(got), q)

    def test_pending_raw_naming_the_unknown_subject_is_still_offered(self):
        self._seed_world()
        exact = self.capture('DriftHollow 的升级方案尚待审批，先保留原有部署。',
                             session='raw-exact', event='r2')
        got = self.ev.context('project', '请说明 drifthollow 的升级方案。')
        self.assertEqual(got['results'], [])
        raw_ids = [r['source_id'] for r in got['recent_statements']]
        self.assertIn(exact['source_id'], raw_ids)

    def test_known_cjk_subject_with_ordinary_english_keeps_relevant_raw(self):
        _generic, pavilion, unrelated = self._seed_world()
        relevant = self.capture('青沙馆布展的物料清单尚待审批，等待核对。',
                                session='raw-rel', event='r3')
        got = self.ev.context('project', 'policy 青沙馆布展怎么安排')
        self.assertIn(pavilion['id'], self.ids(got))
        raw_ids = [r['source_id'] for r in got['recent_statements']]
        self.assertIn(relevant['source_id'], raw_ids)
        self.assertNotIn(unrelated['source_id'], raw_ids)

    def test_unknown_without_possessive_cannot_select_generic_action_card(self):
        generic, _pavilion, unrelated = self._seed_world()
        for token in ('drifthollow', 'DriftHollow'):
            q = '请说明 %s 升级方案' % token
            with self.subTest(q=q):
                got = self.ev.context('project', q)
                self.assertEqual(got['results'], [], q)
                self.assertNotIn(generic['id'], self.ids(got), q)
                raw_ids = [r['source_id'] for r in got['recent_statements']]
                self.assertNotIn(unrelated['source_id'], raw_ids, q)


if __name__ == '__main__':
    unittest.main()
