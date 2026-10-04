#!/usr/bin/env python3
"""Build a self-contained tongle binary bundle with PyInstaller.

Creates an onedir distribution named ``tongle`` from a staged subset of the
repository (scripts + adapters + docs), then copies the result to the
requested output directory and writes a public manifest JSON.

This builder:
  * refuses to overwrite an existing output target,
  * stages only a whitelist of files (no tests, no private data),
  * discovers every ``tongle/*.py`` module as a hidden import,
  * freezes ``scripts/product_entry.py`` as the entry point,
  * never invokes an arbitrary shell.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import sysconfig
import importlib.metadata
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

BUNDLE_NAME = "tongle"
ENTRY_SCRIPT = "scripts/product_entry.py"

# Files copied verbatim (relative to ROOT) into the staging directory.
STAGED_SCRIPTS = [
    "scripts/maintain.py",
    "scripts/http_model.py",
]
STAGED_ROOT_DOCS = [
    "README.md",
    "README.en.md",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
]
STAGED_DOCS = [
    "docs/quickstart.md",
]
LAUNCHER_SOURCE_MAC = "scripts/start_guide.command"
LAUNCHER_TARGET_MAC = "开始使用.command"
LAUNCHER_SOURCE_WIN = "scripts/start_guide.cmd"
LAUNCHER_TARGET_WIN = "开始使用.cmd"


def _launcher_names():
    if os.name == "nt":
        return LAUNCHER_SOURCE_WIN, LAUNCHER_TARGET_WIN
    return LAUNCHER_SOURCE_MAC, LAUNCHER_TARGET_MAC
STAGED_ADAPTERS = [
    "adapters/hook.py",
    "adapters/common.py",
    "adapters/production.py",
    "adapters/pi.ts",
]
OPTIONAL_ADAPTERS = [
    "adapters/__init__.py",
]
OPTIONAL_DOCS = [
    "docs/quickstart.md",
]

# Explicit hidden imports that must exist even if not reachable statically.
EXPLICIT_HIDDEN_IMPORTS = [
    "tongle.__main__",
    "tongle.evaluate",
    "tongle.product",
    "adapters.common",
    "scripts.http_model",
    "scripts.maintain",
    "adapters.hook",
    "adapters.production",
    "urllib.request",
    "http.client",
    "ssl",
]

MANIFEST_VERSION = 1


def _log(message: str) -> None:
    print(message, flush=True)


def _die(message: str, code: int = 1) -> "int":
    print("error: {}".format(message), file=sys.stderr, flush=True)
    return code


def _discover_tongle_modules() -> list:
    """Every module under ``tongle/*.py`` except ``__main__``."""
    pkg_dir = ROOT / "tongle"
    modules = []
    if not pkg_dir.is_dir():
        return modules
    for path in sorted(pkg_dir.glob("*.py")):
        stem = path.stem
        if stem == "__init__":
            modules.append("tongle")
        elif stem == "__main__":
            # __main__ is allowed only when added explicitly.
            continue
        else:
            modules.append("tongle.{}.".format(stem).rstrip("."))
            modules[-1] = "tongle.{}".format(stem)
    return modules


def _collect_hidden_imports() -> list:
    seen = set()
    ordered = []
    for name in list(EXPLICIT_HIDDEN_IMPORTS) + _discover_tongle_modules():
        if name and name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def _copy_into(stage_root: Path, rel_paths: list, required: bool) -> list:
    copied = []
    for rel in rel_paths:
        src = ROOT / rel
        if not src.is_file():
            if required:
                raise FileNotFoundError("required file missing: {}".format(rel))
            continue
        if src.is_symlink():
            raise ValueError("refusing symlink source: {}".format(rel))
        dst = stage_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied.append(rel)
    return copied


def _stage_sources(stage_root: Path) -> dict:
    staged = {
        "scripts": _copy_into(stage_root, STAGED_SCRIPTS, required=True),
        "adapters": _copy_into(stage_root, STAGED_ADAPTERS, required=True)
        + _copy_into(stage_root, OPTIONAL_ADAPTERS, required=False),
        "docs": _copy_into(stage_root, OPTIONAL_DOCS, required=False),
        "root_docs": _copy_into(stage_root, STAGED_ROOT_DOCS, required=True),
    }
    return staged


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hash_tree(root: Path) -> dict:
    hashes = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            hashes[str(path.relative_to(root))] = _sha256(path)
    return hashes


def _add_data_args(stage_root: Path, staged: dict) -> list:
    args = []
    for label in ("scripts", "adapters", "docs"):
        if not staged.get(label):
            continue
        src = stage_root / label
        if not src.is_dir():
            continue
        args.extend(["--add-data", "{}{}{}".format(src, os.pathsep, label)])
    return args


def _run_pyinstaller(argv: list, env: dict, cwd: Path) -> int:
    _log("running: {}".format(" ".join(argv)))
    proc = subprocess.run(
        argv,
        cwd=str(cwd),
        env=env,
        shell=False,
        check=False,
    )
    return proc.returncode


def _stage_entry_into(stage_root: Path) -> Path:
    src = ROOT / ENTRY_SCRIPT
    if not src.is_file():
        raise FileNotFoundError("required file missing: {}".format(ENTRY_SCRIPT))
    if src.is_symlink():
        raise ValueError("refusing symlink source: {}".format(ENTRY_SCRIPT))
    dst = stage_root / "entry.py"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return dst


def _build_pyinstaller_args(
    staged_entry: Path,
    stage_root: Path,
    work_path: Path,
    spec_path: Path,
    dist_path: Path,
    staged: dict,
) -> list:
    args = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--name",
        BUNDLE_NAME,
        "--distpath",
        str(dist_path),
        "--workpath",
        str(work_path),
        "--specpath",
        str(spec_path),
        "--paths",
        str(ROOT),
        "--hidden-import",
    ]
    # Expand hidden imports as repeated flags.
    hidden = _collect_hidden_imports()
    args = args[:-1]
    for name in hidden:
        args.extend(["--hidden-import", name])
    args.extend(_add_data_args(stage_root, staged))
    args.append(str(staged_entry))
    return args


def _make_manifest(dist_dir: Path, bundle_root: Path) -> dict:
    exe_name = BUNDLE_NAME + (".exe" if os.name == "nt" else "")
    exe_path = bundle_root / exe_name
    return {
        "version": MANIFEST_VERSION,
        "name": BUNDLE_NAME,
        "platform": platform.system(),
        "machine": platform.machine(),
        "runtime_bundled": True,
        "host_integration_requires_host": True,
        "signed": False,
        "entry_point": ENTRY_SCRIPT,
        "executable": exe_name,
        "executable_relative": exe_path.relative_to(dist_dir).as_posix()
        if exe_path.exists()
        else None,
        "files": _hash_tree(dist_dir),
    }


def _write_manifest(bundle_root: Path, manifest: dict) -> Path:
    manifest_path = bundle_root / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest_path


def _copy_runtime_licenses(bundle: Path) -> None:
    dest = bundle / "licenses"
    dest.mkdir()
    candidates = [Path(sysconfig.get_path("stdlib")) / "LICENSE.txt",
                  Path(sys.base_prefix) / "LICENSE.txt"]
    python_license = next((p for p in candidates if p.is_file()), None)
    if python_license is None:
        raise FileNotFoundError("Python runtime license missing")
    shutil.copy2(python_license, dest / "Python-LICENSE.txt")
    for name in ("pyinstaller", "pyinstaller-hooks-contrib", "altgraph", "packaging"):
        dist = importlib.metadata.distribution(name)
        found = []
        for rel in dist.files or []:
            if any(part.lower().startswith(("license", "copying")) for part in rel.parts):
                src = Path(dist.locate_file(rel))
                if src.is_file():
                    target = dest / name / str(rel)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, target)
                    found.append(str(rel))
        if name == "pyinstaller" and not found:
            raise FileNotFoundError("PyInstaller license missing")


def _verify_pyinstaller() -> bool:
    try:
        import PyInstaller  # noqa: F401
    except Exception:
        return False
    return True


def _find_pyinstaller_module() -> bool:
    proc = subprocess.run(
        [sys.executable, "-c", "import PyInstaller, sys; sys.exit(0)"],
        shell=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc.returncode == 0


def main(argv: list = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the tongle binary bundle with PyInstaller."
    )
    parser.add_argument(
        "--to",
        required=True,
        help="Destination directory for the built bundle (must not exist).",
    )
    args = parser.parse_args(argv)

    target = Path(args.to).expanduser()
    try:
        target = target.resolve(strict=False)
    except Exception:
        pass

    if target.exists():
        return _die("refusing to overwrite existing target: {}".format(target))

    if not (ROOT / ENTRY_SCRIPT).is_file():
        return _die("entry script not found: {}".format(ROOT / ENTRY_SCRIPT))

    if not _find_pyinstaller_module():
        return _die(
            "PyInstaller is not available for {}. Install it first.".format(
                sys.executable
            )
        )

    temp_dir = tempfile.mkdtemp(prefix="tongle-build-")
    tmp_root = Path(temp_dir)
    stage_root = tmp_root / "stage"
    work_path = tmp_root / "build"
    spec_path = tmp_root / "spec"
    dist_path = tmp_root / "out"

    try:
        stage_root.mkdir(parents=True, exist_ok=True)
        work_path.mkdir(parents=True, exist_ok=True)
        spec_path.mkdir(parents=True, exist_ok=True)
        dist_path.mkdir(parents=True, exist_ok=True)

        staged = _stage_sources(stage_root)
        staged_entry = _stage_entry_into(stage_root)
        _log("staged: {}".format(json.dumps(staged, sort_keys=True)))

        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        env["PYINSTALLER_CONFIG_DIR"] = str(tmp_root / "cache")

        pyi_args = _build_pyinstaller_args(
            staged_entry, stage_root, work_path, spec_path, dist_path, staged
        )
        rc = _run_pyinstaller(pyi_args, env=env, cwd=tmp_root)
        if rc != 0:
            return _die("PyInstaller failed with exit code {}".format(rc), rc)

        built_bundle = dist_path / BUNDLE_NAME
        if not built_bundle.is_dir():
            return _die("expected bundle missing: {}".format(built_bundle))

        built_exe_name = BUNDLE_NAME + (".exe" if os.name == "nt" else "")
        built_exe_path = built_bundle / built_exe_name
        if not built_exe_path.is_file():
            return _die(
                "built executable missing: {}".format(built_exe_path)
            )
        if os.name != "nt" and not os.access(str(built_exe_path), os.X_OK):
            return _die(
                "built executable is not executable: {}".format(built_exe_path)
            )

        smoke_env = dict(os.environ)
        smoke_env.pop("PYTHONPATH", None)
        smoke_env.pop("PYTHONHOME", None)
        for smoke_args in (["--help"], ["-m", "tongle", "--help"]):
            smoke_cmd = [str(built_exe_path)] + smoke_args
            smoke = subprocess.run(
                smoke_cmd,
                cwd=str(tmp_root),
                env=smoke_env,
                shell=False,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if smoke.returncode != 0:
                return _die(
                    "smoke test failed: args={} exit={} stdout={} stderr={}".format(
                        smoke_args,
                        smoke.returncode,
                        (smoke.stdout or "")[-4000:],
                        (smoke.stderr or "")[-4000:],
                    )
                )
            _log("smoke test passed: {}".format(smoke_args))

        # Copy root docs and launcher into the bundle before manifest hashing.
        _copy_into(
            built_bundle, STAGED_ROOT_DOCS, required=True
        )
        _copy_into(built_bundle, ["docs/quickstart.md", "docs/topic-synthesis.md",
                   "docs/learning.md", "docs/release-validation.md", "docs/adapters.md",
                   "docs/operations.md", "docs/consumption-validation.md"], required=True)
        _copy_runtime_licenses(built_bundle)
        launcher_source, launcher_target = _launcher_names()
        launcher_src = ROOT / launcher_source
        if not launcher_src.is_file():
            return _die(
                "required file missing: {}".format(launcher_source)
            )
        if launcher_src.is_symlink():
            return _die(
                "refusing symlink source: {}".format(launcher_source)
            )
        launcher_dst = built_bundle / launcher_target
        shutil.copy2(launcher_src, launcher_dst)
        if os.name != "nt":
            os.chmod(launcher_dst, 0o755)

        manifest = _make_manifest(built_bundle, built_bundle)
        manifest_path = _write_manifest(built_bundle, manifest)
        _log("wrote manifest: {}".format(manifest_path))

        if target.exists():
            return _die(
                "target appeared during build, refusing to overwrite: {}".format(
                    target
                )
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(str(built_bundle), str(target), symlinks=True)

        exe_name = BUNDLE_NAME + (".exe" if os.name == "nt" else "")
        exe_path = target / exe_name
        _log("output directory: {}".format(target))
        _log("executable: {}".format(exe_path))
        if not exe_path.is_file():
            return _die(
                "copied executable missing: {}".format(exe_path)
            )
        if os.name != "nt" and not os.access(str(exe_path), os.X_OK):
            return _die(
                "copied executable is not executable: {}".format(exe_path)
            )
        _log("manifest: {}".format(target / "manifest.json"))
        return 0
    except Exception as exc:  # noqa: BLE001
        return _die("build failed: {}".format(exc))
    finally:
        shutil.rmtree(str(tmp_root), ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
