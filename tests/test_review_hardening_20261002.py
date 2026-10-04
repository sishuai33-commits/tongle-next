"""Regression tests for the 2026-10-02 review-hardening round.

Pins down four bounded repairs found by the 2026-10-02 full review and
verified against source + the production database before fixing:

1. Pending-correction detection scans only the user's OWN voice: quoted spans
   (「…」/『…』/"…"/'…'/`…`/```…```) are blanked before the correction-term
   scan, so a task instruction merely *describing* corrections (the two live
   false positives observed in production: 「未完成更正压制相关记忆」逻辑 and
   the prompt-audit examples 「现在」「不再」) no longer raises a correction
   notice nor suppresses topically-related own memory cards. Linking terms
   still come from the full text, so a real correction quoting the card it
   corrects stays linked.
2. A v2 publication_pending row whose resume keeps failing with a PERMANENT
   StageLimit is counted and, past the bounded cap, surfaced as failed (plus a
   knowledge_failures audit row) instead of retrying silently every cycle
   forever, invisible to needs_attention (which only watches failed rows).
3. A settled compare call is bound to its page identity at settle time
   (knowledge_calls.page_id), so a cached page receipt can never borrow a
   sibling page's settled call identity; a legacy row without a page binding
   is never reusable -- the page simply redispatches one bounded comparison.
4. The factual-audit gate rejects hollow coverage: proposal_substring and a
   supported source_quote below the minimum length (3 chars) fail closed, and
   the task text now states both the 40-check ceiling and the minimum length
   so a verbose model stops burning a slot on a limit it was never told.
"""
import json
import tempfile
import time
import unittest
from pathlib import Path

from tongle.engine import Engine
from tongle.evolution import Evolution
from tongle.fact_review import (FACT_REVIEW_TASK, MAX_FACT_CLAIMS,
                                MIN_FACT_QUOTE_CHARS, validate_response)
from tongle.delta_routing import V2_PROTOCOL

SOURCE_TEXT = '冷冻柜门保持打开超过90秒时,值班技术员必须关闭柜门,并在恢复制冷之前记录该事件。'
BODY = '冷冻柜门保持打开超过90秒时,值班技术员必须关闭柜门。'


class _Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.e = Engine(self.root / 'state')
        self.e.init(self.root / 'vault'); self.addCleanup(self.e.store.close)
        cfg = {'knowledge_mode': 'evolving', 'model_command': ['stub'],
               'knowledge_quiet_seconds': 0,
               'knowledge_daily_token_budget': 100000000,
               'knowledge_daily_call_budget': 100000,
               'knowledge_max_publication_attempts': 3}
        (self.e.home / 'config.json').write_text(json.dumps(cfg))
        self.ev = Evolution(self.e)

    def capture(self, text, event='e', session='session'):
        rec = self.e.store.capture('project', session, 'user', event, text)
        self.ev.captured(rec['source_id'], 'user', session)
        return rec


class CorrectionVoiceTests(_Harness):
    def pending(self):
        return self.ev._pending_correction_records('project')

    def test_quoted_correction_term_is_not_a_correction(self):
        # Live false positive 1 (source 7770caaf…): a delegated review prompt
        # describing the suppression feature itself.
        self.capture('你在审查比较复核与投递代码。检索把关：「未完成更正压制相关记忆」逻辑。')
        self.assertEqual(self.pending(), [])

    def test_quoted_example_words_are_not_a_correction(self):
        # Live false positive 2 (source f6d773c0…): the prompt-audit
        # instruction quoting the example words 「现在」「不再」.
        self.capture('找出迁移相对措辞（「现在」「不再」）。空审计是合法结果，不要凑数。')
        self.assertEqual(self.pending(), [])

    def test_backtick_and_fence_spans_are_not_a_correction(self):
        self.capture('行为由 `withdrawn` 字段控制；```撤回``` 标记参与重放。')
        self.assertEqual(self.pending(), [])

    def test_own_voice_correction_is_detected(self):
        rec = self.capture('上次说错了，更正一下：预算是 200 条。')
        pending = self.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['source_id'], rec['source_id'])
        self.assertIn('依据编号', pending[0]['notice'])

    def test_quoted_target_of_a_real_correction_stays_linked(self):
        # Detection uses the masked text; linking terms still use the full
        # text, so the quoted card name remains available for scoping.
        rec = self.capture('把「三层防御」改为四层')
        pending = self.pending()
        self.assertEqual(len(pending), 1)
        # terms are bounded 2-char tokens; the quoted card name must still
        # contribute its tokens to the linking set.
        self.assertTrue({'三层', '防御'} <= set(pending[0]['terms']))


class PublicationCapTests(_Harness):
    def seed_stuck_publication(self):
        with self.e.transaction():
            self.e.db.execute(
                'INSERT INTO knowledge_work(id,project,session,inputs,state,created_at,'
                ' pipeline_protocol,bundle)'
                " VALUES('stuck','project','s','[]','publication_pending',?,?,'not-json')",
                (time.time(), V2_PROTOCOL))

    def row(self):
        return dict(self.e.db.execute(
            "SELECT * FROM knowledge_work WHERE id='stuck'").fetchone())

    def test_permanent_stagelimit_is_capped_into_visible_failure(self):
        self.seed_stuck_publication()
        for expected in (1, 2):
            self.ev.process()
            row = self.row()
            self.assertEqual(row['state'], 'publication_pending')
            self.assertEqual(row['publication_attempts'], expected)
        self.ev.process()
        row = self.row()
        self.assertEqual(row['state'], 'failed')
        self.assertEqual(row['publication_attempts'], 3)
        self.assertIn('publication resume exhausted', row['error'])
        audit = self.e.db.execute(
            "SELECT * FROM knowledge_failures WHERE work_id='stuck'"
            " AND state='publication_pending'").fetchall()
        self.assertEqual(len(audit), 1)
        # Terminal: a later cycle no longer retries or increments anything.
        self.ev.process()
        self.assertEqual(self.row()['publication_attempts'], 3)


class CompareCallPageBindingTests(_Harness):
    def setUp(self):
        super().setUp()
        with self.e.transaction():
            self.e.db.execute(
                'INSERT INTO knowledge_calls(id,work_id,stage,day,reserved,created_at,'
                " elapsed,page_id) VALUES('c1','w','compare','2026-10-02',1,?,1.0,'page-一')",
                (time.time(),))
            self.e.db.execute(
                'INSERT INTO knowledge_calls(id,work_id,stage,day,reserved,created_at,'
                " elapsed) VALUES('c2','w','compare','2026-10-02',1,?,1.0)",
                (time.time(),))

    def test_settled_call_is_accepted_only_for_its_own_page(self):
        self.assertTrue(self.ev._compare_call_ok('w', 'c1', 'page-一'))
        self.assertFalse(self.ev._compare_call_ok('w', 'c1', 'page-二'))
        self.assertFalse(self.ev._compare_call_ok('other', 'c1', 'page-一'))

    def test_legacy_call_without_page_binding_is_never_reusable(self):
        self.assertFalse(self.ev._compare_call_ok('w', 'c2', 'page-一'))

    def test_missing_page_argument_is_rejected(self):
        self.assertFalse(self.ev._compare_call_ok('w', 'c1', None))
        self.assertFalse(self.ev._compare_call_ok('w', 'c1', ''))


class FactReviewMinLengthTests(unittest.TestCase):
    def content(self):
        return {'task': 't', 'sources': [{'text': SOURCE_TEXT}],
                'operations': [{'op': 0, 'proposal_title': '', 'proposal_body': BODY}]}

    def check(self, substring, quote):
        return {'op': 0, 'claim': '门开超时关闭', 'verdict': 'supported',
                'proposal_substring': substring, 'source_quote': quote}

    def test_hollow_single_char_coverage_fails_closed(self):
        response = {'checks': [self.check('的', '的')],
                    'unsupported_count': 0, 'decision': 'pass'}
        self.assertFalse(validate_response(self.content(), response)['ok'])

    def test_two_char_quote_below_minimum_fails_closed(self):
        response = {'checks': [self.check('冷冻', '冷冻')],
                    'unsupported_count': 0, 'decision': 'pass'}
        self.assertFalse(validate_response(self.content(), response)['ok'])

    def test_minimum_length_exact_quote_passes(self):
        quote = SOURCE_TEXT[:MIN_FACT_QUOTE_CHARS]
        response = {'checks': [self.check(BODY[:MIN_FACT_QUOTE_CHARS], quote)],
                    'unsupported_count': 0, 'decision': 'pass'}
        self.assertTrue(validate_response(self.content(), response)['ok'])

    def test_task_text_states_the_bounded_limits(self):
        self.assertIn(str(MAX_FACT_CLAIMS), FACT_REVIEW_TASK)
        self.assertIn('不少于 %d 个字符' % MIN_FACT_QUOTE_CHARS, FACT_REVIEW_TASK)


if __name__ == '__main__':
    unittest.main()
