"""Fail-closed tests for the self-contained source release and relocation.

These tests build a temporary copy of the *actual* safe-selected production
files plus explicit canaries (canaries are never written into the real source
tree). They then build the archive, extract it into a fresh directory, and
exercise the package's own consumers with PYTHONPATH removed. They never run
the recursive full test suite.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PREFIX = 'tongle-next/'

SAFE_TREES = {
    'tongle': ('.py',),
    'adapters': ('.py', '.ts'),
    'tests': ('.py', '.mjs'),
}
PRUNE_DIRS = {'__pycache__', 'cache', 'caches', 'runtime', 'private',
              'secrets', 'auth', 'node_modules', 'build', 'dist', 'env',
              'venv', 'evidence', 'vendor', '.runtime', '.cache', '.git',
              '.venv', '.DS_Store'}

REQUIRED_SUPPORT = [
    'README.md', 'README.en.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md', 'CONTRIBUTING.md', 'requirements-build.txt', 'pyproject.toml', 'AGENTS.md', '.gitignore',
    'docs/quickstart.md', 'docs/operations.md', 'docs/learning.md', 'docs/topic-synthesis.md', 'docs/release-validation.md', 'docs/core.md', 'docs/adapters.md',
    'docs/consumption-validation.md', 'docs/acceptance-cases.json',
    'scripts/build_release.py', 'scripts/tongle.py', 'scripts/maintain.py',
    'scripts/pi_model.py', 'scripts/model_budget.ts', 'scripts/daily.py',
     
    'examples/evaluation/status-boundary.json',
]
OPTIONAL_SUPPORT = ['scripts/http_model.py', 'scripts/product_entry.py']

REQUIRED_MEMBERS = [
    'tongle/__main__.py', 'adapters/installed_pi.ts', 'adapters/pi.ts',
    'scripts/maintain.py', 'scripts/model_budget.ts', 'scripts/daily.py',
      'scripts/build_release.py',
    'tests/pi-final-output.mjs', 'tests/pi-receipt-binding.mjs',
    'tests/test_evaluation_lifecycle.py', 'tests/test_launcher.py',
     'docs/quickstart.md', 'docs/operations.md', 'docs/learning.md', 'docs/topic-synthesis.md', 'docs/release-validation.md',
    'examples/evaluation/status-boundary.json', 'README.md', 'README.en.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md', 'CONTRIBUTING.md', 'requirements-build.txt', 'pyproject.toml',
    'AGENTS.md', '.gitignore',
]


class ReleasePackage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name).resolve()

    # ---- temporary fixture construction -------------------------------

    def _copy_safe_tree(self, dest):
        """Copy the actual selected production files into a temp source root."""
        dest.mkdir(parents=True, exist_ok=True)
        for tree, suffixes in SAFE_TREES.items():
            src = REPO_ROOT / tree
            if not src.is_dir():
                continue
            for dirpath, dirnames, filenames in os.walk(src, topdown=True, followlinks=False):
                here = Path(dirpath)
                dirnames[:] = sorted(n for n in dirnames if n not in PRUNE_DIRS and not n.startswith('.'))
                for name in list(dirnames):
                    if (here / name).is_symlink():
                        dirnames.remove(name)
                for name in sorted(filenames):
                    if name.startswith('.'):
                        continue
                    candidate = here / name
                    if candidate.is_symlink() or not candidate.is_file():
                        continue
                    if not name.endswith(suffixes):
                        continue
                    rel = candidate.relative_to(REPO_ROOT)
                    out = dest / rel
                    out.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(candidate, out)
        for rel in REQUIRED_SUPPORT + OPTIONAL_SUPPORT:
            src = REPO_ROOT / rel
            if src.is_file() and not src.is_symlink():
                out = dest / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, out)

    def _make_canaries(self, dest):
        """Add canaries only to the temp tree; never to real source."""
        canaries = {
            'tongle/.cache/canary.py': 'CACHE = True\n',
            'tongle/cache/canary.py': 'CACHE = True\n',
            'tongle/evidence/canary.py': 'EVIDENCE = True\n',
            'adapters/.runtime/canary.ts': '// runtime canary\n',
            'adapters/runtime/canary.ts': '// runtime canary\n',
            'tests/.hidden/canary.py': 'HIDDEN = True\n',
            'tests/private/canary.py': 'PRIVATE = True\n',
            'tongle/secrets/canary.py': 'SECRET = True\n',
            'adapters/node_modules/canary.ts': '// node_modules canary\n',
            'tests/build/canary.py': 'BUILD = True\n',
            'tongle/vendor/canary.py': 'VENDOR = True\n',
            'docs/operations.md': '# ops\n',
            'docs/private/secrets.md': 'SECRET = "canary"\n',
            'scripts/rogue_ops.py': 'ROGUE = True\n',
            'examples/unselected/bad.json': '{"material": "other"}\n',
            'auth/global.json': '{"token": "canary"}\n',
        }
        for rel, text in canaries.items():
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text)

    def _build_temp_source(self, include_optional=False):
        source = self.work / 'source'
        self._copy_safe_tree(source)
        self._make_canaries(source)
        if include_optional:
            (source / 'scripts').mkdir(parents=True, exist_ok=True)
            (source / 'scripts/http_model.py').write_text('# optional http model\n')
            (source / 'scripts/product_entry.py').write_text('# optional product entry\n')
            (source / 'tongle/product.py').write_text('# optional product package\n')
        return source

    def _builder_script(self, root):
        """Path to the builder inside ``root``. Tests must never call the
        original checkout builder for relocated/extracted roots."""
        return Path(root) / 'scripts/build_release.py'

    def _run_builder(self, source, target, builder_root=None, with_root=True):
        env = dict(os.environ)
        env.pop('PYTHONPATH', None)
        root = Path(builder_root) if builder_root else Path(source)
        cmd = [sys.executable, str(self._builder_script(root))]
        if with_root:
            cmd += ['--root', str(source)]
        cmd += ['--to', str(target)]
        return subprocess.run(cmd, cwd=str(root), env=env, text=True,
                              capture_output=True, timeout=120)

    def _extract(self, archive, dest):
        dest.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(dest)
        return dest / 'tongle-next'

    # ---- manifest / member checks -------------------------------------

    def test_builds_archive_with_manifest_and_required_members(self):
        source = self._build_temp_source()
        archive = self.work / 'release.zip'
        result = self._run_builder(source, archive)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(archive.is_file())
        with zipfile.ZipFile(archive) as zf:
            names = set(zf.namelist())
            manifest = json.loads(zf.read(PACKAGE_PREFIX + 'RELEASE-MANIFEST.json'))
            self.assertFalse(manifest['runtime_data_included'])
            for member in REQUIRED_MEMBERS:
                self.assertIn(PACKAGE_PREFIX + member, names)
            for key, digest in manifest['files'].items():
                import hashlib
                self.assertEqual(
                    hashlib.sha256(zf.read(PACKAGE_PREFIX + key)).hexdigest(), digest)
            # excluded canaries never leak
            for bad in ['docs/operations.md', 'scripts/rogue_ops.py',
                        'examples/unselected/bad.json', 'auth/global.json',
                        'tongle/.cache/canary.py', 'tongle/cache/canary.py',
                        'tongle/evidence/canary.py', 'adapters/.runtime/canary.ts',
                        'adapters/runtime/canary.ts', 'tests/.hidden/canary.py',
                        'tests/private/canary.py', 'tongle/secrets/canary.py',
                        'adapters/node_modules/canary.ts', 'tests/build/canary.py',
                        'tongle/vendor/canary.py']:
                self.assertNotIn(PACKAGE_PREFIX + bad, names)

    def test_optional_scripts_and_future_product_included_when_present(self):
        source = self._build_temp_source(include_optional=True)
        archive = self.work / 'release.zip'
        result = self._run_builder(source, archive)
        self.assertEqual(result.returncode, 0, result.stderr)
        with zipfile.ZipFile(archive) as zf:
            names = set(zf.namelist())
            self.assertIn(PACKAGE_PREFIX + 'scripts/http_model.py', names)
            self.assertIn(PACKAGE_PREFIX + 'scripts/product_entry.py', names)
            self.assertIn(PACKAGE_PREFIX + 'tongle/product.py', names)

    def _assert_selection_failure(self, result, archive):
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('error:', result.stderr)
        self.assertNotIn('Traceback (most recent call last)', result.stderr)
        self.assertFalse(archive.exists())

    def test_missing_required_fixture_rejects_before_artifact(self):
        source = self._build_temp_source()
        (source / 'examples/evaluation/status-boundary.json').unlink()
        archive = self.work / 'release.zip'
        result = self._run_builder(source, archive)
        self._assert_selection_failure(result, archive)
        self.assertIn('fixture', result.stderr)

    def test_non_synthetic_fixture_rejects(self):
        source = self._build_temp_source()
        (source / 'examples/evaluation/status-boundary.json').write_text(
            json.dumps({'material': 'production'}))
        archive = self.work / 'release.zip'
        result = self._run_builder(source, archive)
        self._assert_selection_failure(result, archive)
        self.assertIn('fixture', result.stderr)

    def test_non_dict_fixture_rejects_cleanly(self):
        source = self._build_temp_source()
        (source / 'examples/evaluation/status-boundary.json').write_text('[1, 2, 3]')
        archive = self.work / 'release.zip'
        result = self._run_builder(source, archive)
        self._assert_selection_failure(result, archive)
        self.assertIn('fixture', result.stderr)

    def test_fixture_material_must_equal_synthetic(self):
        source = self._build_temp_source()
        data = json.loads((source / 'examples/evaluation/status-boundary.json').read_text())
        self.assertEqual(data['material'], 'synthetic')
        data['material'] = 'other'
        (source / 'examples/evaluation/status-boundary.json').write_text(json.dumps(data))
        archive = self.work / 'release.zip'
        result = self._run_builder(source, archive)
        self._assert_selection_failure(result, archive)
        self.assertIn('fixture', result.stderr)

    def test_symlinked_required_file_rejected(self):
        source = self._build_temp_source()
        real = source / 'scripts/pi_model_real.py'
        real.write_text('# real\n')
        victim = source / 'scripts/pi_model.py'
        victim.unlink()
        try:
            victim.symlink_to(real)
        except (OSError, NotImplementedError):
            self.skipTest('symlinks unavailable')
        archive = self.work / 'release.zip'
        result = self._run_builder(source, archive)
        self._assert_selection_failure(result, archive)
        self.assertIn('symlink', result.stderr)

    def test_symlinked_ancestor_of_explicit_file_rejected(self):
        source = self._build_temp_source()
        docs = source / 'docs'
        moved = source / 'docs_real'
        shutil.move(str(docs), str(moved))
        try:
            docs.symlink_to(moved)
        except (OSError, NotImplementedError):
            self.skipTest('symlinks unavailable')
        archive = self.work / 'release.zip'
        result = self._run_builder(source, archive)
        self._assert_selection_failure(result, archive)
        self.assertIn('symlink', result.stderr)

    def test_symlinked_source_root_rejected(self):
        source = self._build_temp_source()
        linked_root = self.work / 'linked-root'
        try:
            linked_root.symlink_to(source)
        except (OSError, NotImplementedError):
            self.skipTest('symlinks unavailable')
        archive = self.work / 'release.zip'
        result = self._run_builder(linked_root, archive)
        self._assert_selection_failure(result, archive)
        self.assertIn('symlink', result.stderr)

    def test_nested_symlink_leak_excluded_or_rejected(self):
        source = self._build_temp_source()
        outside = self.work / 'outside.txt'
        outside.write_text('external bytes\n')
        link = source / 'tests/leak.py'
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest('symlinks unavailable')
        archive = self.work / 'release.zip'
        result = self._run_builder(source, archive)
        if result.returncode == 0:
            with zipfile.ZipFile(archive) as zf:
                self.assertNotIn(PACKAGE_PREFIX + 'tests/leak.py', set(zf.namelist()))
        else:
            self.assertFalse(archive.exists())
            self.assertIn('symlink', result.stderr)
            self.assertNotIn('Traceback (most recent call last)', result.stderr)

    # ---- relocation / consumer checks ---------------------------------

    def test_relocated_lifecycle_tests_pass_without_pythonpath(self):
        source = self._build_temp_source()
        archive = self.work / 'release.zip'
        result = self._run_builder(source, archive)
        self.assertEqual(result.returncode, 0, result.stderr)
        extracted = self._extract(archive, self.work / 'extracted')
        env = dict(os.environ)
        env.pop('PYTHONPATH', None)
        result = subprocess.run(
            [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests',
             '-p', 'test_evaluation_lifecycle.py'],
            cwd=str(extracted), env=env, text=True, capture_output=True, timeout=300)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_relocated_cli_help_and_fixture_read(self):
        source = self._build_temp_source()
        archive = self.work / 'release.zip'
        result = self._run_builder(source, archive)
        self.assertEqual(result.returncode, 0, result.stderr)
        extracted = self._extract(archive, self.work / 'extracted')
        env = dict(os.environ)
        env.pop('PYTHONPATH', None)
        result = subprocess.run(
            [sys.executable, '-m', 'tongle', '--help'],
            cwd=str(extracted), env=env, text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        fixture = json.loads(
            (extracted / 'examples/evaluation/status-boundary.json').read_text())
        self.assertEqual(fixture['material'], 'synthetic')

    def test_relocated_installed_pi_resolves_sibling_import(self):
        source = self._build_temp_source()
        archive = self.work / 'release.zip'
        result = self._run_builder(source, archive)
        self.assertEqual(result.returncode, 0, result.stderr)
        extracted = self._extract(archive, self.work / 'extracted')
        installed = (extracted / 'adapters/installed_pi.ts').read_text()
        self.assertIn('from "./pi.ts"', installed)
        self.assertNotIn('/Users/', installed)

    def test_relocated_installed_pi_import_with_node_optional(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node unavailable')
        source = self._build_temp_source()
        archive = self.work / 'release.zip'
        result = self._run_builder(source, archive)
        self.assertEqual(result.returncode, 0, result.stderr)
        extracted = self._extract(archive, self.work / 'extracted')
        config = self.work / 'disabled.json'
        config.write_text(json.dumps({'enabled': False}))
        harness = extracted / 'tests/import-installed.mjs'
        harness.write_text(
            'import ext from "../adapters/installed_pi.ts";\n'
            'let on;\n'
            'ext({on: (n, f) => { on = f; }});\n'
            'console.log("IMPORTED");\n')
        env = dict(os.environ, TONGLE_NEXT_INSTALL=str(config))
        env.pop('PYTHONPATH', None)
        result = subprocess.run([node, str(harness)], cwd=str(extracted),
                                env=env, text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('IMPORTED', result.stdout)

    def test_installed_pi_valid_sibling_import_accepted(self):
        source = self._build_temp_source()
        (source / 'adapters/installed_pi.ts').write_text(
            'import adapter from "./pi.ts";\nexport default adapter;\n')
        archive = self.work / 'release.zip'
        result = self._run_builder(source, archive)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(archive.is_file())

    def test_hardcoded_personal_path_is_rejected(self):
        source = self._build_temp_source()
        parts = ['', 'Users', 'example', 'vault', 'tongle.ts']
        personal_import = 'import adapter from "' + '/'.join(parts) + '";\n'
        (source / 'adapters/installed_pi.ts').write_text(
            '/** relocated */\n' + personal_import)
        archive = self.work / 'release.zip'
        result = self._run_builder(source, archive)
        self._assert_selection_failure(result, archive)
        self.assertIn('adapter', result.stderr)
        self.assertNotIn('Traceback (most recent call last)', result.stderr)

    def test_non_portable_adapter_import_is_rejected(self):
        source = self._build_temp_source()
        (source / 'adapters/installed_pi.ts').write_text(
            'import adapter from "./other.ts";\nexport default adapter;\n')
        archive = self.work / 'release.zip'
        result = self._run_builder(source, archive)
        self._assert_selection_failure(result, archive)
        self.assertIn('./pi.ts', result.stderr)

    def test_extra_default_adapter_import_is_rejected(self):
        source = self._build_temp_source()
        (source / 'adapters/installed_pi.ts').write_text(
            'import adapter from "./pi.ts";\n'
            'import adapter from "./foreign.ts";\n'
            'export default adapter;\n')
        archive = self.work / 'release.zip'
        result = self._run_builder(source, archive)
        self._assert_selection_failure(result, archive)
        self.assertIn('./pi.ts', result.stderr)

    def test_builder_rebuilds_from_extracted_root(self):
        source = self._build_temp_source()
        archive = self.work / 'release.zip'
        result = self._run_builder(source, archive)
        self.assertEqual(result.returncode, 0, result.stderr)
        with zipfile.ZipFile(archive) as zf:
            manifest = json.loads(
                zf.read(PACKAGE_PREFIX + 'RELEASE-MANIFEST.json'))
            prior_hashes = dict(manifest['files'])
        extracted = self._extract(archive, self.work / 'extracted')
        rebuilt = self.work / 'rebuilt.zip'
        # Invoke the extracted copy of the builder itself, cwd=extracted,
        # with PYTHONPATH unset and without --root.
        result = self._run_builder(extracted, rebuilt, builder_root=extracted,
                                   with_root=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(rebuilt.is_file())
        with zipfile.ZipFile(rebuilt) as zf:
            names = set(zf.namelist())
            manifest = json.loads(
                zf.read(PACKAGE_PREFIX + 'RELEASE-MANIFEST.json'))
            for key, digest in prior_hashes.items():
                self.assertIn(PACKAGE_PREFIX + key, names)
                self.assertEqual(manifest['files'].get(key), digest, key)

    def test_relocated_launcher_without_pythonpath(self):
        source = self._build_temp_source()
        archive = self.work / 'release.zip'
        result = self._run_builder(source, archive)
        self.assertEqual(result.returncode, 0, result.stderr)
        extracted = self._extract(archive, self.work / 'extracted')
        env = dict(os.environ)
        env.pop('PYTHONPATH', None)
        result = subprocess.run(
            [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests',
             '-p', 'test_launcher.py', '-v'],
            cwd=str(extracted), env=env, text=True, capture_output=True,
            timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Ran 1 test', result.stderr)

    def test_relocated_builder_launcher_from_extracted_cwd(self):
        source = self._build_temp_source()
        archive = self.work / 'release.zip'
        result = self._run_builder(source, archive)
        self.assertEqual(result.returncode, 0, result.stderr)
        extracted = self._extract(archive, self.work / 'extracted')
        env = dict(os.environ)
        env.pop('PYTHONPATH', None)
        result = subprocess.run(
            [sys.executable, 'scripts/build_release.py',
             '--to', str(self.work / 'launched.zip')],
            cwd=str(extracted), env=env, text=True, capture_output=True,
            timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.work / 'launched.zip').is_file())

    def test_no_existing_target_overwrite(self):
        source = self._build_temp_source()
        archive = self.work / 'release.zip'
        result = self._run_builder(source, archive)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self._run_builder(source, archive)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('Traceback (most recent call last)', result.stderr)


if __name__ == '__main__':
    unittest.main()
