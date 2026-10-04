"""Supervisor-authored black-box regression tests; no external models or live data.

Passing these does not prove real host capture or semantic compilation.
"""
import concurrent.futures
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]


class Acceptance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='tongle-accept-')
        self.base = Path(self.tmp.name)
        self.home = self.base / 'state'
        self.vault = self.base / 'vault'
        self.call('init', '--vault', str(self.vault))

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, *args, text='', home=None, ok=True):
        p = subprocess.run([sys.executable, '-m', 'tongle', '--home', str(home or self.home), *args],
                           input=text, text=True, capture_output=True, cwd=REPO, timeout=30)
        if ok:
            self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
            return json.loads(p.stdout)
        self.assertNotEqual(p.returncode, 0, p.stdout)
        return p

    def source(self, project, text, eid='event-1', role='user'):
        return self.call('capture', '--project', project, '--session', 's1',
                         '--role', role, '--event-id', eid, text=text)

    def record(self, source, project, text, key='scope', status='confirmed'):
        return self.call('record', '--project', project, '--key', key, '--status', status,
                         '--source-id', source['source_id'], '--source-version', str(source['version']),
                         text=json.dumps({'text': text, 'quote': text, 'kind': 'memory'}, ensure_ascii=False))

    def query(self, project, text, home=None):
        return self.call('query', '--project', project, '--text', text, home=home)['results']

    def test_chinese_retrieval_and_project_isolation(self):
        a = self.source('harbor', '海港方案只交付流程设计。')
        self.record(a, 'harbor', '海港方案只交付流程设计。')
        b = self.source('orchard', '果园项目需要开发演示系统。')
        self.record(b, 'orchard', '果园项目需要开发演示系统。')
        self.assertTrue(self.query('harbor', '交付流程'))
        self.assertFalse(self.query('orchard', '海港方案'))
        self.assertNotIn('果园', json.dumps(self.query('harbor', '方案'), ensure_ascii=False))

    def test_capture_replay_is_idempotent_and_collision_rejected(self):
        a = self.source('harbor', '只交付流程设计。')
        b = self.source('harbor', '只交付流程设计。')
        self.assertEqual(a['source_id'], b['source_id'])
        self.assertEqual(self.call('status')['events'], 1)
        self.call('capture', '--project', 'harbor', '--session', 's1', '--role', 'user',
                  '--event-id', 'event-1', text='内容被改变。', ok=False)

    def test_equal_text_in_distinct_turns_not_dropped(self):
        a = self.source('harbor', '暂缓。', 'e1')
        b = self.source('harbor', '暂缓。', 'e2')
        self.assertNotEqual(a['source_id'], b['source_id'])
        self.assertEqual(self.call('status')['events'], 2)

    def test_confirmed_correction_replaces_current_not_history(self):
        a = self.source('harbor', '项目交付三张图。', 'e1')
        old = self.record(a, 'harbor', '项目交付三张图。')
        b = self.source('harbor', '项目改为交付两张图。', 'e2')
        new = self.record(b, 'harbor', '项目改为交付两张图。')
        ids = [r['id'] for r in self.query('harbor', '项目交付')]
        self.assertIn(new['id'], ids)
        self.assertNotIn(old['id'], ids)

    def test_source_quote_and_scope_are_enforced(self):
        s = self.source('harbor', '保留原始资料。')
        self.call('record', '--project', 'harbor', '--key', 'bad', '--status', 'confirmed',
                  '--source-id', s['source_id'], '--source-version', '1',
                  text=json.dumps({'text': '已删除资料', 'quote': '不存在的引文'}), ok=False)
        self.call('record', '--project', 'other', '--key', 'bad', '--status', 'confirmed',
                  '--source-id', s['source_id'], '--source-version', '1',
                  text=json.dumps({'text': '保留原始资料。', 'quote': '保留原始资料。'}), ok=False)

    def test_withdraw_survives_record_replay(self):
        s = self.source('harbor', '交付流程设计。')
        r = self.record(s, 'harbor', '交付流程设计。')
        self.call('withdraw', '--project', 'harbor', '--id', r['id'])
        # Replaying the source must not revive the withdrawn derived entry.
        self.source('harbor', '交付流程设计。')
        self.assertFalse(self.query('harbor', '流程设计'))

    def test_concurrent_capture_preserves_acknowledged_events(self):
        def capture(i):
            return self.source('harbor', f'并发事件第{i}条。', f'e{i}')['source_id']
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            ids = list(pool.map(capture, range(30)))
        self.assertEqual(len(set(ids)), 30)
        self.assertEqual(self.call('status')['events'], 30)

    def test_document_idempotence_and_source_revision_invalidation(self):
        p = self.base / 'article.md'
        p.write_text('缓存只适用于允许五分钟延迟的查询。')
        a = self.call('ingest', '--project', 'study', '--path', str(p), '--source-id', 'article-17')
        b = self.call('ingest', '--project', 'study', '--path', str(p), '--source-id', 'article-17')
        self.assertEqual(a['version'], b['version'])
        self.record(a, 'study', p.read_text(), status='candidate')
        self.assertTrue(self.query('study', '缓存'))
        p.write_text('缓存只适用于允许一分钟延迟的查询。')
        c = self.call('ingest', '--project', 'study', '--path', str(p), '--source-id', 'article-17')
        self.assertGreater(c['version'], a['version'])
        self.assertFalse(self.query('study', '五分钟'))

    def test_portable_restore_preserves_content_and_withdrawal(self):
        a = self.source('harbor', '当前交付流程设计。', 'e1')
        self.record(a, 'harbor', '当前交付流程设计。')
        b = self.source('harbor', '废弃临时演示方案。', 'e2')
        withdrawn = self.record(b, 'harbor', '废弃临时演示方案。', key='temp')
        self.call('withdraw', '--project', 'harbor', '--id', withdrawn['id'])
        archive = self.base / 'backup.zip'
        self.call('export', '--to', str(archive))
        restored = self.base / 'restored'
        self.call('restore', '--from', str(archive), '--vault', str(self.base / 'restored-vault'), home=restored)
        self.assertEqual([x['id'] for x in self.query('harbor', '流程设计')],
                         [x['id'] for x in self.query('harbor', '流程设计', home=restored)])
        self.assertFalse(self.query('harbor', '临时演示', home=restored))


if __name__ == '__main__':
    unittest.main()
