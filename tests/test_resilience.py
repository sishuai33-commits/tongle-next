"""Independent filesystem/provenance checks. Model fixtures are NOT semantic evidence."""
import json
from pathlib import Path
import shutil
import zipfile
import unittest
import test_acceptance as base


class Resilience(unittest.TestCase):
    setUp = base.Acceptance.setUp
    tearDown = base.Acceptance.tearDown
    call = base.Acceptance.call
    source = base.Acceptance.source
    record = base.Acceptance.record
    query = base.Acceptance.query
    def note_for(self, rid):
        notes = [p for p in (self.vault / 'tongle').rglob('*.md') if rid in p.read_text()]
        self.assertEqual(len(notes), 1, str(notes))
        return notes[0]

    def test_assistant_cannot_confirm_itself(self):
        s = self.source('harbor', '已执行删除全部资料。', role='assistant')
        self.call('record', '--project', 'harbor', '--key', 'delete', '--status', 'confirmed',
                  '--source-id', s['source_id'], '--source-version', '1',
                  text=json.dumps({'text': '已执行删除全部资料。', 'quote': '已执行删除全部资料。'}), ok=False)

    def test_candidate_cannot_replace_confirmed(self):
        a = self.source('harbor', '项目确定只交付流程。', 'a')
        old = self.record(a, 'harbor', '项目确定只交付流程。')
        b = self.source('harbor', '建议项目新增系统开发。', 'b', role='assistant')
        self.record(b, 'harbor', '建议项目新增系统开发。', status='candidate')
        result = self.query('harbor', '项目')
        self.assertIn(old['id'], [r['id'] for r in result])
        for r in result:
            if '新增' in r['text']:
                self.assertNotEqual(r['status'], 'confirmed')

    def test_manual_body_edit_preserved_and_not_confirmed(self):
        source = self.source('harbor', '流程仅限内部评审。')
        rec = self.record(source, 'harbor', '流程仅限内部评审。')
        note = self.note_for(rec['id'])
        original = note.read_text()
        edited = original.replace('流程仅限内部评审。', '流程需要外部复核。')
        self.assertNotEqual(edited, original)
        note.write_text(edited)
        self.call('sync')
        result = self.query('harbor', '外部复核')
        self.assertTrue(result)
        self.assertNotEqual(result[0]['status'], 'confirmed')
        self.assertEqual(note.read_text(), edited)

    def test_move_missing_return_and_withdraw_are_distinct(self):
        s = self.source('harbor', '交付流程设计。')
        r = self.record(s, 'harbor', '交付流程设计。')
        path = self.note_for(r['id'])
        moved = path.parent / '重命名资料.md'
        path.rename(moved)
        self.call('sync')
        self.assertIn(r['id'], [x['id'] for x in self.query('harbor', '流程')])
        saved = moved.read_bytes()
        moved.unlink()
        self.call('sync')
        self.assertFalse(self.query('harbor', '流程'))
        moved.write_bytes(saved)
        self.call('sync')
        self.assertTrue(self.query('harbor', '流程'))
        self.call('withdraw', '--project', 'harbor', '--id', r['id'])
        moved.write_bytes(saved)
        self.call('sync')
        self.assertFalse(self.query('harbor', '流程'))

    def test_duplicate_note_identity_is_not_arbitrarily_selected(self):
        s = self.source('harbor', '流程按第一版交付。')
        r = self.record(s, 'harbor', '流程按第一版交付。')
        note = self.note_for(r['id'])
        other = note.parent / 'same-identity.md'
        other.write_text(note.read_text().replace('第一版', '错误版'))
        # It is acceptable to reject sync or to mark the record conflicted.
        import subprocess, sys
        subprocess.run([sys.executable, '-m', 'tongle', '--home', str(self.home), 'sync'],
                       cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=30)
        self.assertFalse(self.query('harbor', '流程'))
        self.assertIn('第一版', note.read_text())
        self.assertIn('错误版', other.read_text())

    def test_restore_rejects_traversal_before_any_escape(self):
        archive = self.base / 'evil.zip'
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('../escape.txt', 'no')
        self.call('restore', '--from', str(archive), '--vault', str(self.base / 'v2'),
                  home=self.base / 'state2', ok=False)
        self.assertFalse((self.base / 'escape.txt').exists())

    def test_restore_into_live_destination_refused(self):
        s = self.source('harbor', '保留当前数据。')
        self.record(s, 'harbor', '保留当前数据。')
        archive = self.base / 'snapshot.zip'
        self.call('export', '--to', str(archive))
        self.call('restore', '--from', str(archive), '--vault', str(self.vault), ok=False)
        self.assertTrue(self.query('harbor', '保留当前'))

    def test_model_disabled_does_not_claim_processed(self):
        self.source('harbor', '需要真实编译。')
        self.call('process', '--limit', '1')
        status = self.call('status')
        self.assertGreater(status['jobs'].get('pending', 0), 0)
        self.assertEqual(status['jobs'].get('completed', 0) + status['jobs'].get('done', 0), 0)
