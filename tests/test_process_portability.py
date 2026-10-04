"""Independent lifecycle checks with bounded synthetic child processes."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from tongle.model_errors import ModelError
from tongle.worker import invoke_json


class ProcessPortabilityTests(unittest.TestCase):
    def test_non_ascii_subprocess_payload_roundtrip(self):
        program = 'import sys,json; value=json.loads(sys.stdin.read()); print(json.dumps(value,ensure_ascii=False))'
        value, _ = invoke_json([sys.executable, '-c', program], '{"text":"中文 空格 四张"}', 5)
        self.assertEqual(value['text'], '中文 空格 四张')

    def test_timeout_stops_model_descendant(self):
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / 'heartbeat'
            child = "import time,pathlib; p=pathlib.Path(%r);\nfor i in range(100):\n p.write_text(str(i)); time.sleep(.1)\n" % str(marker)
            parent = 'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",%r]); time.sleep(15)' % child
            with self.assertRaises(ModelError) as caught:
                invoke_json([sys.executable, '-c', parent], '{}', 2)
            self.assertEqual(caught.exception.code, 'timeout')
            self.assertTrue(marker.exists(), 'descendant must really start')
            before = marker.read_bytes()
            time.sleep(.4)
            self.assertEqual(marker.read_bytes(), before, 'descendant survived model timeout')


class LockFailureTests(unittest.TestCase):
    def test_real_lock_errors_are_not_reported_as_running(self):
        import importlib.util
        from unittest.mock import patch
        spec = importlib.util.spec_from_file_location('maintain_lock_probe', Path(__file__).resolve().parents[1] / 'scripts/maintain.py')
        maintain = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(maintain)
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(maintain, 'lock_file', side_effect=OSError(5, 'synthetic IO failure')):
                with self.assertRaises(OSError): maintain.live_lock(Path(temp))
                with self.assertRaises(OSError): maintain.run_loop(None, Path(temp))
            with patch.object(maintain, 'lock_file', side_effect=BlockingIOError()):
                self.assertTrue(maintain.live_lock(Path(temp)))
                self.assertEqual(maintain.run_loop(None, Path(temp)), {'already_running': True})


class PortableFilenameTests(unittest.TestCase):
    def test_reserved_names_are_safe_for_auto_generated_files(self):
        from tongle.presentation import label
        with tempfile.TemporaryDirectory() as temp:
            for name in ('CON', 'nul', 'AUX', 'COM1', 'LPT9', 'COM¹', 'NUL.extra'):
                filename = label(name) + '.md'
                self.assertTrue(filename.startswith('_'), filename)
                target = Path(temp) / filename
                target.write_text('中文卡片', encoding='utf8')
                self.assertEqual(target.read_text(encoding='utf8'), '中文卡片')

    def test_reserved_explicit_paths_are_rejected(self):
        from tongle.knowledge_store import _norm_path
        for path in ('知识/CON.md', '项目/COM1/a.md', '知识/NUL.extra.md'):
            with self.assertRaises(ValueError): _norm_path(path)

    def test_normal_labels_preserved(self):
        from tongle.presentation import label
        self.assertEqual(label('咨询方案'), '咨询方案')
        self.assertEqual(label('CONCEPT'), 'CONCEPT')


if __name__ == '__main__': unittest.main()
