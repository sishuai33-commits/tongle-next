"""Raw-statement fallback tests for the evolving context (synthetic, no live data).

These prove the read-only presentation contract: still-unprocessed real user
utterances are surfaced as ``recent_statements`` when the model cannot compile,
never as verified knowledge, and never bypassing maintenance.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tongle.engine import Engine
from tongle.evolution import Evolution

try:  # sibling import under ``unittest discover -s tests``
    from fact_review_stub import with_fact_audit
except ImportError:  # pragma: no cover - package-import invocation
    from tests.fact_review_stub import with_fact_audit


class RecentFallbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.e = Engine(self.root / 'state'); self.e.init(self.root / 'vault')
        self.addCleanup(self.e.store.close)
        self.cfg = {'knowledge_mode': 'evolving', 'model_command': ['stub'],
                    'knowledge_quiet_seconds': 0}
        self.write_cfg(); self.ev = Evolution(self.e)

    def write_cfg(self):
        (self.e.home / 'config.json').write_text(json.dumps(self.cfg))

    def capture(self, text, event='one', role='user', session='s1'):
        rec = self.e.store.capture('project', session, role, event, text)
        self.ev.captured(rec['source_id'], role, session)
        return rec

    def test_disabled_model_still_presents_fresh_statement(self):
        self.capture('北湾项目本轮交付三张流程图，验收口令为苍蓝松柏。')
        self.cfg['model_command'] = None; self.write_cfg()
        got = self.e.context('project', '流程图交付', session='new')
        self.assertTrue(got['recent_statements'])
        item = got['recent_statements'][-1]
        self.assertEqual(item['status'], 'raw_unverified')
        self.assertEqual(item['role'], 'user')
        self.assertIn('三张流程图', item['text'])
        self.assertIn('recent_statements_note', got)

    def test_foreign_revoked_and_synthetic_are_excluded(self):
        mine = self.capture('本项目只做流程图。', session='s1')
        self.e.store.capture('foreign', 's1', 'user', 'f', '别的项目的流程图。')
        revoked = self.capture('已撤回的流程图要求。', event='rev', session='s1')
        self.e.source_state('project', revoked['source_id'], 'revoked')
        self.e.store.capture('project', 'framework-maintenance', 'user', 'fw',
                             '框架维护请求：整理流程图。')
        got = self.e.context('project', '流程图', session='new')
        ids = {i['source_id'] for i in got['recent_statements']}
        self.assertIn(mine['source_id'], ids)
        self.assertNotIn(revoked['source_id'], ids)

    def test_completed_source_is_not_duplicated(self):
        self.capture('只做流程图，不开发接口。', session='s1')
        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'proposal' in data: return {'approved': True, 'findings': []}, {}
            s = data['sources'][-1]
            return {'operations': [{'op': 'create', 'key': '范围', 'kind': 'memory',
                    'topic': '方案', 'title': '范围', 'body': s['text'],
                    'evidence': [{'source_id': s['source_id'], 'version': s['version'],
                                  'quote': s['text']}], 'reason': '明确边界'}]}, {}
        with patch('tongle.worker.invoke_json', side_effect=with_fact_audit(model)):
            self.ev.process(force=True)
        got = self.e.context('project', '流程图', session='new')
        self.assertEqual(got['recent_statements'], [])

    def test_long_statement_with_late_negation_is_skipped(self):
        long_text = '流程图要求。' * 200 + '但以上全部不适用。'
        self.assertTrue(len(long_text) > 600)
        self.capture(long_text)
        got = self.e.context('project', '流程图', session='new')
        self.assertEqual(got['recent_statements'], [])

    def test_failed_old_statement_stays_non_authoritative(self):
        self.capture('旧说法：接口开发照常进行。', event='old', session='s0')
        def boom(cmd, prompt, timeout, env=None):
            raise RuntimeError('down')
        with patch('tongle.worker.invoke_json', side_effect=boom):
            self.ev.process(force=True)
        self.capture('新决定：本轮只做流程图，接口暂缓。', event='new', session='s1')
        def model(cmd, prompt, timeout, env=None):
            data = json.loads(prompt)
            if 'proposal' in data: return {'approved': True, 'findings': []}, {}
            s = data['sources'][-1]
            return {'operations': [{'op': 'create', 'key': '本轮范围', 'kind': 'memory',
                    'topic': '方案', 'title': '本轮范围', 'body': s['text'],
                    'evidence': [{'source_id': s['source_id'], 'version': s['version'],
                                  'quote': s['text']}], 'reason': '明确边界'}]}, {}
        with patch('tongle.worker.invoke_json', side_effect=with_fact_audit(model)):
            self.ev.process(force=True)
        got = self.e.context('project', '接口流程图', session='new')
        self.assertTrue(got['recent_statements'])
        self.assertTrue(all(i['status'] == 'raw_unverified' for i in got['recent_statements']))
        self.assertIn('recent_statements_note', got)
        # The compiled newer decision is presented as reviewed knowledge, not
        # as a raw statement, so the old failed utterance cannot silently win.
        self.assertTrue(any(r.get('source_role') == 'reviewed_knowledge'
                            for r in got['results']))


if __name__ == '__main__':
    unittest.main()
