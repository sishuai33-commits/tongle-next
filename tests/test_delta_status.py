"""A paused maintenance version must stay visible as retained responsibility.

Real temporary Engine/Evolution: a ``protocol_paused`` v2 row is a *waiting
cause*, never done/obsolete/responsibility-free. The snapshot exposes a distinct
count that is included in waiting/pending and selected into the operator list,
and the rendered 运行情况 page names the pause and its recovery condition.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tongle.engine import Engine
from tongle.evolution import Evolution
from tongle.views import refresh, status_snapshot


class ProtocolPausedStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.e = Engine(self.root / 'state')
        self.e.init(self.root / 'vault')
        self.addCleanup(self.e.store.close)
        (self.e.home / 'config.json').write_text(
            json.dumps({'knowledge_mode': 'evolving', 'model_command': ['stub']}))
        self.evo = Evolution(self.e)

    def _paused_row(self, work_id='w-paused', project='版本暂停项目'):
        # The real paused shape written by delta_migration.park: state
        # ``protocol_paused`` plus the short safe error, lease cleared.
        self.e.db.execute(
            "INSERT INTO knowledge_work(id,project,session,inputs,state,attempts,"
            "next_at,lease_token,lease_until,error,created_at)"
            " VALUES(?,?,?,?, 'protocol_paused',1,0.0,NULL,0.0,'protocol paused',0.0)",
            (work_id, project, 'sess', '[]'))
        self.e.db.commit()

    def test_snapshot_counts_paused_as_distinct_waiting_cause(self):
        self._paused_row()
        snap = status_snapshot(self.e, {'active': 0, 'conflict': 0})
        self.assertEqual(snap['protocol_paused'], 1)
        self.assertEqual(snap['work'].get('protocol_paused'), 1)
        self.assertEqual(snap['waiting'], 1)
        self.assertEqual(snap['pending'], 1)
        # Never silently folded into a terminal/running bucket.
        self.assertEqual(snap['done'], 0)
        self.assertEqual(snap['obsolete'], 0)
        self.assertEqual(snap['failed'], 0)
        self.assertEqual(snap['auto_pending'], 0)
        # Selected into the operator list, so the pause is visible.
        self.assertIn('protocol_paused', [e['state'] for e in snap['errors']])

    def test_rendered_page_names_pause_and_retained_responsibility(self):
        self._paused_row()
        refresh(self.e)
        page = (self.root / 'vault' / '运行情况.md').read_text()
        self.assertIn('维护版本已暂停（责任仍保留）', page)
        self.assertIn('恢复条件：恢复对应版本后按原暂停凭据继续', page)
        # The waiting line names version pause as a cause; the paused count and
        # pending total both include the row.
        self.assertIn('条件等待（额度/来源/预算/版本暂停）：1', page)
        self.assertIn('维护版本已暂停（责任仍保留，不计入完成/作废）：1 项', page)
        self.assertIn('待处理合计（不含已停止与已提交）：1', page)
        # And it is never rendered as completed/no-responsibility.
        self.assertIn('已完成维护批次：0', page)
        self.assertNotIn('已完成维护批次：1', page)

    def test_other_states_are_not_affected(self):
        self._paused_row()
        self.e.db.execute(
            "INSERT INTO knowledge_work(id,project,session,inputs,state,attempts,"
            "next_at,lease_token,lease_until,created_at)"
            " VALUES('w-done','p','s','[]','done',1,0.0,NULL,0.0,0.0)")
        self.e.db.commit()
        snap = status_snapshot(self.e, {'active': 0, 'conflict': 0})
        self.assertEqual(snap['protocol_paused'], 1)
        self.assertEqual(snap['waiting'], 1)   # done is not a waiting cause
        self.assertEqual(snap['pending'], 1)   # done is not actionable backlog
        self.assertEqual(snap['done'], 1)


if __name__ == '__main__':
    unittest.main()
