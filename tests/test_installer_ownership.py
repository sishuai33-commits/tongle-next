"""Independent test: uninstall must use exact ownership, not substring matching."""
import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class InstallerOwnership(unittest.TestCase):
    def test_foreign_hook_mentioning_product_name_survives(self):
        spec = importlib.util.spec_from_file_location('tongle_installer_review', ROOT/'adapters/install.py')
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        foreign = {'type': 'command', 'command': 'echo check-tongle-next-availability'}
        existing = {'hooks': {'Stop': [{'hooks': [foreign]}]}}
        cleaned = mod.unmerge_cc(existing)
        self.assertEqual(cleaned, existing, 'foreign handler removed because its text mentions product name')
