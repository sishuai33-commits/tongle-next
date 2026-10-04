#!/usr/bin/env python3
"""Build a safe self-contained source distribution with code checksums.

Selection is explicit and bounded to the archive source root. Only the
python package tree ``tongle`` (.py), ``adapters`` (.py/.ts), ``tests``
(.py/.mjs), an explicit docs/script/support allowlist, and the synthetic
fixture after material verification are ever read. Hidden, cache, runtime,
evidence and virtualenv directories inside those trees are pruned before
traversal and never followed. Symlinked source roots, selected files, and
any symlink ancestor of an explicitly named file are rejected. The whole
repository is never scanned.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import zipfile

PACKAGE_PREFIX = 'tongle-next/'
MANIFEST_NAME = 'RELEASE-MANIFEST.json'

# Top-level source tree roots that are read (and only these).
SOURCE_TREE_ROOTS = ('tongle', 'adapters', 'tests')
# Allowed suffixes per tree root.
TREE_SUFFIXES = {'tongle': ('.py',), 'adapters': ('.py', '.ts'), 'tests': ('.py', '.mjs')}
# Directory names pruned before traversal, anywhere inside an allowed tree.
# Hidden directories are always pruned as well; non-hidden runtime/cache/vendor
# dirs are listed explicitly so a normal-looking dir cannot leak into the archive.
PRUNE_DIRS = {
    '__pycache__', 'cache', 'caches', 'runtime', 'private', 'secrets',
    'auth', 'node_modules', 'build', 'dist', 'env', 'venv', 'evidence',
    'vendor', '.runtime', '.cache', '.git', '.venv', '.DS_Store',
}

# Exact portable adapter import required in the selected installed_pi.ts.
INSTALLED_PI_REL = 'adapters/installed_pi.ts'
# Matches a default binding named ``adapter`` imported from a quoted specifier;
# the captured specifier list must be exactly ['./pi.ts'], so any extra or
# different adapter import (from elsewhere) is rejected.
INSTALLED_PI_IMPORT_RE = re.compile(
    r"^\s*import\s+adapter\s+from\s+['\"]([^'\"]+)['\"]\s*;?\s*$", re.MULTILINE)

# Explicitly selected support files. Each must exist as a regular file.
REQUIRED_FILES = (
    'README.md', 'README.en.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md', 'CONTRIBUTING.md', 'requirements-build.txt',
    'pyproject.toml',
    'AGENTS.md',
    '.gitignore',
    'docs/quickstart.md', 'docs/operations.md', 'docs/learning.md', 'docs/topic-synthesis.md', 'docs/release-validation.md',
    'docs/core.md',
    'docs/adapters.md',
    'docs/consumption-validation.md',
    'docs/acceptance-cases.json',
    'scripts/build_release.py',
    'scripts/tongle.py',
    'scripts/maintain.py',
    'scripts/pi_model.py',
    'scripts/model_budget.ts',
    'scripts/daily.py',
    
    
    'examples/evaluation/status-boundary.json',
)


class SelectionError(Exception):
    """Raised when the source tree cannot be selected safely."""


def _is_symlink(path):
    return path.is_symlink()


def _rel(root, path):
    """Relative POSIX-ish key string for a path below root."""
    return Path(path).relative_to(Path(root)).as_posix()


def _require_regular_file(path, label, root=None, rel=None):
    """Require a real regular file; diagnostics use a safe label, never a raw path."""
    where = rel if rel is not None else Path(path).name
    if _is_symlink(path):
        raise SelectionError('symlinked %s rejected: %s' % (label, where))
    if not path.is_file():
        raise SelectionError('missing %s: %s' % (label, where))


def _reject_symlink_ancestors(root, rel, allow_missing=False):
    """Every ancestor of an explicitly named file must be a real directory.

    When allow_missing is true, a genuinely missing ancestor ends validation
    with False, signalling the caller may simply omit the optional file. Any
    symlink ancestor (including dangling) is still rejected, and ancestors
    before the first missing one are still validated as non-symlink dirs.
    """
    current = root
    for part in rel.split('/')[:-1]:
        current = current / part
        if _is_symlink(current):
            raise SelectionError('symlinked ancestor rejected for %s' % rel)
        if not current.is_dir():
            if allow_missing and not current.exists():
                return False
            raise SelectionError('missing directory for %s' % rel)
    return True


# Optional files included when present as regular files (never symlinks).
OPTIONAL_FILES = (
    'scripts/http_model.py',
    'scripts/product_entry.py',
    'scripts/build_binary.py',
    'scripts/start_guide.command',
    'scripts/start_guide.cmd',
    'tests/windows_acceptance.py',
    'tests/test_portability.py',
    '.github/workflows/preview.yml',
)

# Explicit files whose absence is allowed but whose presence is verified.
FIXTURE_REL = 'examples/evaluation/status-boundary.json'
FIXTURE_MATERIAL = 'synthetic'


def _verify_fixture(root, rel):
    path = root / rel
    _reject_symlink_ancestors(root, rel)
    _require_regular_file(path, 'fixture', root=root, rel=rel)
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise SelectionError('fixture %s is not valid JSON: %s' % (rel, exc))
    if not isinstance(data, dict):
        raise SelectionError('fixture %s must be a JSON object' % rel)
    if data.get('material') != FIXTURE_MATERIAL:
        raise SelectionError('fixture %s material must be %r' % (rel, FIXTURE_MATERIAL))


def _source_root(root, rel):
    if _is_symlink(root):
        raise SelectionError('source root is a symlink: %s' % root)
    path = root / rel
    if _is_symlink(path):
        raise SelectionError('symlinked source tree rejected: %s' % rel)
    if not path.is_dir():
        raise SelectionError('missing source tree: %s' % rel)
    return path


def _pruned_dirs(names):
    return [n for n in names if n in PRUNE_DIRS or n.startswith('.')]


def _check_installed_pi_adapter(root):
    """Enforce the exact portable adapter import in adapters/installed_pi.ts.

    Only this selected connector file is inspected for a hardcoded personal
    path. A non-portable or otherwise unknown adapter import is rejected with
    a concise SelectionError; the rest of the source tree is never scanned for
    user home strings.
    """
    rel = INSTALLED_PI_REL
    path = root / rel
    _reject_symlink_ancestors(root, rel)
    _require_regular_file(path, 'adapter', root=root, rel=rel)
    try:
        text = path.read_text(encoding='utf-8')
    except OSError as exc:
        raise SelectionError('cannot read adapter %s: %s' % (rel, exc))
    matches = INSTALLED_PI_IMPORT_RE.findall(text)
    if matches != ['./pi.ts']:
        raise SelectionError(
            'adapter %s must import adapter from quoted ./pi.ts exactly once' % rel)


def _collect_tree(root, rel, suffixes):
    """Walk one allowed source tree with pruning and symlink rejection."""
    base = _source_root(root, rel)
    found = []
    for dirpath, dirnames, filenames in os.walk(base, topdown=True, followlinks=False):
        here = Path(dirpath)
        dirnames[:] = sorted(n for n in dirnames if n not in _pruned_dirs(dirnames))
        for name in sorted(dirnames):
            candidate = here / name
            if _is_symlink(candidate):
                raise SelectionError('symlinked directory rejected: %s' % _rel(root, candidate))
        for name in sorted(filenames):
            candidate = here / name
            key = _rel(root, candidate)
            if name.startswith('.'):
                continue
            if _is_symlink(candidate):
                raise SelectionError('symlinked file rejected: %s' % key)
            if not candidate.is_file():
                continue
            if name.endswith(suffixes):
                found.append((key, candidate))
    return found


def _explicit_candidates(root, rels, optional=False):
    out = []
    for rel in rels:
        if _reject_symlink_ancestors(root, rel, allow_missing=optional) is False:
            continue
        path = root / rel
        if optional and (not path.exists() or _is_symlink(path)):
            if _is_symlink(path):
                raise SelectionError('symlinked optional file rejected: %s' % rel)
            continue
        label = 'fixture' if rel == FIXTURE_REL else 'file'
        _require_regular_file(path, label, root=root, rel=rel)
        out.append((rel, path))
    return out


def select_sources(root):
    """Return ordered (key, Path) pairs for all selected source files.

    ``root`` is the archive source root. Raises SelectionError on any
    symlink, missing required file, or fixture that is not authorized
    synthetic material.
    """
    root = Path(root)
    if _is_symlink(root):
        raise SelectionError('source root is a symlink: %s' % root)
    if not root.is_dir():
        raise SelectionError('source root is not a directory: %s' % root)
    selected = {}
    loaded_extras = _explicit_candidates(root, REQUIRED_FILES)
    optional = _explicit_candidates(root, OPTIONAL_FILES, optional=True)
    for key, path in loaded_extras + optional:
        selected[key] = path
    for tree, suffixes in TREE_SUFFIXES.items():
        for key, path in _collect_tree(root, tree, suffixes):
            if key not in selected:
                selected[key] = path
    _check_installed_pi_adapter(root)
    _verify_fixture(root, FIXTURE_REL)
    if FIXTURE_REL not in selected:
        selected[FIXTURE_REL] = root / FIXTURE_REL
    ordered = sorted(selected.items())
    return ordered


REQUIRED_MEMBERS = (
    'tongle/__main__.py',
    'adapters/installed_pi.ts',
    'adapters/pi.ts',
    'scripts/maintain.py',
    'scripts/model_budget.ts',
    'scripts/daily.py',
    
    
    'scripts/build_release.py',
    'tests/pi-final-output.mjs',
    'tests/pi-receipt-binding.mjs',
    
    'tests/test_evaluation_lifecycle.py',
    'tests/test_launcher.py',
    'docs/quickstart.md', 'docs/operations.md', 'docs/learning.md', 'docs/topic-synthesis.md', 'docs/release-validation.md',
    'examples/evaluation/status-boundary.json',
    'README.md', 'README.en.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md', 'CONTRIBUTING.md', 'requirements-build.txt',
    'pyproject.toml',
    'AGENTS.md',
    '.gitignore',
)


def build_archive(root, target):
    """Build the release archive from ``root`` into ``target`` (created)."""
    root = Path(root)
    target = Path(target)
    if target.exists():
        raise SelectionError('refusing to overwrite existing release: %s' % target)
    ordered = select_sources(root)
    contents = {}
    for key, path in ordered:
        try:
            contents[key] = path.read_bytes()
        except OSError as exc:
            raise SelectionError('cannot read %s: %s' % (key, exc))
    missing = [m for m in REQUIRED_MEMBERS if m not in contents]
    if missing:
        raise SelectionError('required members missing: ' + ', '.join(missing))
    manifest = {
        'format': 1,
        'kind': 'source-package',
        'runtime_data_included': False,
        'selection': {
            'trees': list(SOURCE_TREE_ROOTS),
            'tree_suffixes': {k: list(v) for k, v in TREE_SUFFIXES.items()},
            'pruned_directories': sorted(PRUNE_DIRS),
            'explicit_docs_scripts': sorted(REQUIRED_FILES),
            'optional_scripts': sorted(OPTIONAL_FILES),
            'optional_workflow': [f for f in OPTIONAL_FILES if f.startswith('.github/workflows/')],
            'symlink_policy': 'reject',
            'fixture_material_required': FIXTURE_MATERIAL,
        },
        'files': {key: hashlib.sha256(data).hexdigest() for key, data in sorted(contents.items())},
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        for key in sorted(contents):
            archive.writestr(PACKAGE_PREFIX + key, contents[key])
        archive.writestr(PACKAGE_PREFIX + MANIFEST_NAME, json.dumps(manifest, indent=2, sort_keys=True))
    with zipfile.ZipFile(target) as archive:
        for key, digest in manifest['files'].items():
            actual = hashlib.sha256(archive.read(PACKAGE_PREFIX + key)).hexdigest()
            if actual != digest:
                raise SelectionError('manifest hash mismatch for %s' % key)
    print(json.dumps({'archive': str(target), 'files': len(contents),
                      'sha256': hashlib.sha256(target.read_bytes()).hexdigest()}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--to', required=True)
    parser.add_argument('--root', default=None, help='source root (defaults to checkout root)')
    args = parser.parse_args()
    raw_root = Path(args.root) if args.root else Path(__file__).resolve().parents[1]
    root = raw_root.absolute()
    # Reject a symlinked source root before any resolution can rewrite it.
    if _is_symlink(root):
        raise SystemExit('error: source root is a symlink: %s' % root)
    target = Path(args.to).absolute()
    try:
        build_archive(root, target)
    except SelectionError as exc:
        raise SystemExit('error: %s' % exc)


if __name__ == '__main__':
    main()
