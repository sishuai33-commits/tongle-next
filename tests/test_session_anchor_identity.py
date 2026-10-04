"""Focused real-core regressions for current-turn identity in anchors.

These exercise only ``Evolution._session_anchor_turns`` (via its caller
``Evolution.context``) with synthetic fixtures: no live vault, model,
credentials or network. The model stays disabled throughout -- source capture
and published real cards are enough. Passing proves machine behaviour, not
semantic quality.
"""
import json
import tempfile
import unittest
from pathlib import Path

from tongle.engine import Engine
from tongle.evolution import Evolution
from tongle.knowledge_files import KnowledgeFiles

# The reproduced real core: a prior substantive same-session user turn that
# happens to contain the short continuation ``继续`` as a substring.
PRIOR = '我们继续核对 CedarFlow 的运行安排。'
CURRENT = '继续'


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

    def capture(self, text, event='e', role='user', session='s1'):
        rec = self.e.store.capture('project', session, role, event, text)
        self.ev.captured(rec['source_id'], role, session)
        return rec

    def anchors(self, session, query):
        return [t[2] for t in self.ev._session_anchor_turns('project', session, query)]

    def cedarflow(self):
        return self.seed('project', 'CedarFlow', 'knowledge', '运行',
                         'CedarFlow 运行安排', 'CedarFlow 的运行安排说明。')


class SessionAnchorIdentityTests(_Harness):
    def test_short_current_inside_prior_statement_keeps_anchor_and_delivers(self):
        # The earlier valid topic must survive a short current '继续' that only
        # occurs *inside* the prior statement's text; the core context then
        # recovers and delivers the published knowledge card.
        card = self.cedarflow(); self.publish()
        prior = self.capture(PRIOR, event='p1')
        cur = self.capture(CURRENT, event='p2')   # host captured the current turn
        ids = self.anchors('s1', CURRENT)
        self.assertIn(prior['source_id'], ids)
        self.assertNotIn(cur['source_id'], ids)
        got = self.ev.context('project', CURRENT, session='s1')
        self.assertIn(card['id'], [r['id'] for r in got['results']])
        self.assertIn('CedarFlow 的运行安排说明。', got['rendered_context'])

    def test_exact_captured_current_row_is_excluded(self):
        # Whole-text equality still excludes the current turn itself.
        cur = self.capture(CURRENT, event='c1')
        self.assertEqual(self.anchors('s1', CURRENT), [])
        self.assertNotIn(cur['source_id'], self.anchors('s1', CURRENT))

    def test_capped_prefix_of_full_current_excluded_without_false_positive(self):
        # The adapter caps the retained query at 4000 chars but captures the full
        # original turn. Its capped prefix is still the current turn (excluded),
        # while a shorter prior turn that is merely a substring of that prefix is
        # never classified as the current turn.
        prior = self.capture('继续核对 CedarFlow 运行安排。', event='p1')
        full = '继续核对 CedarFlow 运行安排。' + '乙' * 4400
        cur = self.capture(full, event='p2')
        query = full[:4000]
        self.assertEqual(len(query), 4000)
        self.assertTrue(full.startswith(query))
        ids = self.anchors('s1', query)
        self.assertNotIn(cur['source_id'], ids)
        self.assertIn(prior['source_id'], ids)

    def test_other_session_turn_is_not_an_anchor(self):
        self.capture(PRIOR, event='p1', session='s2')
        self.assertEqual(self.anchors('s1', CURRENT), [])

    def test_revoked_turn_is_not_an_anchor(self):
        prior = self.capture(PRIOR, event='p1')
        self.e.source_state('project', prior['source_id'], 'revoked')
        self.assertNotIn(prior['source_id'], self.anchors('s1', CURRENT))

    def test_short_current_knowledge_case_is_scoped_to_same_session(self):
        # The only subject-naming turn lives in another session, so a short
        # current continuation must not recover that knowledge cross-session.
        card = self.cedarflow(); self.publish()
        self.capture(PRIOR, event='other', session='s2')
        got = self.ev.context('project', CURRENT, session='s1')
        self.assertNotIn(card['id'], [r['id'] for r in got['results']])


if __name__ == '__main__':
    unittest.main()
