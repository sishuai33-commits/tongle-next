#!/usr/bin/env python3
"""Build a portable Tongle download with a verified CPython distribution."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tarfile
import tempfile
import urllib.request
import zipfile


ROOT = Path(__file__).resolve().parents[1]
PYTHON_RELEASE = "20261110"
PYTHON_VERSION = "3.12.16"
RUNTIMES = {
    "macos-arm64": (
        "aarch64-apple-darwin",
        "2f11b8aa3726bdbd87dd9eb76568412faddf4f47e26913fa45d6a2e424972acd",
        25237581,
        "bin/python3",
    ),
    "windows-x64": (
        "x86_64-pc-windows-msvc",
        "355ee61b8207b91574491b1c6dc9b0ebb4408fc56bfafe498f3ef439314b52dc",
        22365213,
        "python.exe",
    ),
}
SUPPORT_FILES = (
    "LICENSE", "THIRD_PARTY_NOTICES.md", "docs/friend-quickstart.md",
    "scripts/friend_setup.py", "scripts/tongle.py", "scripts/daily.py",
    "scripts/maintain.py", "scripts/doctor.py", "scripts/pi_model.py",
    "scripts/model_budget.ts",
    "adapters/common.py", "adapters/codex_generated.py",
    "adapters/codex_identity.py", "adapters/hook.py", "adapters/install.py",
    "adapters/installed_hook.py", "adapters/installed_pi.ts", "adapters/pi.ts",
)
BUILD_FILES = (
    "scripts/build_friend_package.py", "tests/friend_package_acceptance.py",
    "tests/friend_pi_acceptance.py", ".github/workflows/friend-package.yml", "pyproject.toml",
)
MAC_START = """#!/bin/sh
set -eu
cd -- "$(/usr/bin/dirname -- "$0")"
unset PYTHONHOME PYTHONPATH
exec ./runtime/bin/python3 -s -B -X utf8 scripts/friend_setup.py "$@"
"""
WIN_START = (
    "@echo off\r\n"
    "setlocal DisableDelayedExpansion\r\n"
    "set \"PYTHONHOME=\"\r\n"
    "set \"PYTHONPATH=\"\r\n"
    "chcp 65001 >nul\r\n"
    "\"%~dp0runtime\\python.exe\" -s -B -X utf8 "
    "\"%~dp0scripts\\friend_setup.py\" %*\r\n"
    "exit /b %errorlevel%\r\n"
)


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def copy_product(to: Path, *, build_files: bool = False) -> None:
    # Stage only runnable product source and explicit support files. Development
    # history, local fixtures, configs, or private data cannot enter via git.
    paths = set(SUPPORT_FILES)
    paths.update(p.relative_to(ROOT).as_posix() for p in (ROOT / "tongle").glob("*.py"))
    if build_files:
        paths.update(BUILD_FILES)
        paths.add(".gitignore")
    for relative in sorted(paths):
        path = ROOT / relative
        if not path.is_file() or path.resolve() != path:
            raise ValueError(f"Missing or linked product source: {relative}")
        target = to / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    shutil.copyfile(to / "docs/friend-quickstart.md", to / "README.md")


def runtime_url(triple: str) -> str:
    name = f"cpython-{PYTHON_VERSION}%2B{PYTHON_RELEASE}-{triple}-install_only_stripped.tar.gz"
    return (
        "https://github.com/astral-sh/python-build-standalone/releases/download/"
        f"{PYTHON_RELEASE}/{name}"
    )


def install_runtime(platform: str, staging: Path, archive: Path | None) -> str:
    triple, digest, expected_size, executable = RUNTIMES[platform]
    url = runtime_url(triple)
    if archive is None:
        archive = staging / "runtime.tar.gz"
        with urllib.request.urlopen(url, timeout=60) as response, archive.open("xb") as out:
            remaining = expected_size
            while remaining:
                data = response.read(min(remaining, 1024 * 1024))
                if not data:
                    break
                out.write(data)
                remaining -= len(data)
            if remaining or response.read(1):
                raise ValueError("Unexpected Python archive size")
    if archive.stat().st_size != expected_size or sha256(archive) != digest:
        raise ValueError(f"Python archive does not match the pinned {platform} release")
    unpacked = staging / "unpacked"
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(unpacked, filter="data")
    python = unpacked / "python"
    if not (python / executable).is_file():
        raise ValueError("Expected Python executable missing from verified archive")
    # ZIP extraction utilities differ in symlink support. Materialize the
    # distribution's internal links so the installed package uses real files.
    for path in python.rglob("*"):
        if path.is_symlink() and not path.resolve(strict=True).is_relative_to(python):
            raise ValueError("Python archive contains an external link")
    shutil.copytree(python, staging / "Tongle/runtime")
    return url


def build(platform: str, target: Path, archive: Path | None) -> dict:
    target = target.absolute()
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"Output already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="tongle-package-", dir=target.parent) as tmp:
        staging = Path(tmp).resolve()
        package = staging / "Tongle"
        copy_product(package)
        url = install_runtime(platform, staging, archive)
        if platform == "macos-arm64":
            start = package / "Start Tongle.command"
            start.write_text(MAC_START, encoding="ascii")
            start.chmod(0o755)
        else:
            start = package / "Start Tongle.cmd"
            start.write_bytes(WIN_START.encode("ascii"))
        files = sorted(p for p in package.rglob("*") if p.is_file())
        manifest = {
            "format": 1,
            "platform": platform,
            "python": {"version": PYTHON_VERSION, "url": url, "sha256": RUNTIMES[platform][1]},
            "files": {p.relative_to(package).as_posix(): sha256(p) for p in files},
        }
        manifest_path = package / "PACKAGE-MANIFEST.json"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        # Publish the ZIP only after the build is complete.
        output = staging / "archive.zip"
        with zipfile.ZipFile(output, "x", zipfile.ZIP_DEFLATED, compresslevel=6) as result:
            for path in [*files, manifest_path]:
                name = "Tongle/" + path.relative_to(package).as_posix()
                info = zipfile.ZipInfo(name, date_time=(2026, 11, 12, 0, 0, 0))
                info.create_system = 3
                mode = 0o755 if path.stat().st_mode & stat.S_IXUSR else 0o644
                info.external_attr = (stat.S_IFREG | mode) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                with path.open("rb") as source, result.open(info, "w") as member:
                    shutil.copyfileobj(source, member)
        os.link(output, target)
    return {"platform": platform, "files": len(files), "sha256": sha256(target)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    stage = commands.add_parser("stage")
    stage.add_argument("--to", type=Path, required=True)
    pack = commands.add_parser("build")
    pack.add_argument("--platform", choices=RUNTIMES, required=True)
    pack.add_argument("--to", type=Path, required=True)
    pack.add_argument("--runtime-archive", type=Path)
    args = parser.parse_args()
    if args.command == "stage":
        args.to.mkdir(parents=True, exist_ok=False)
        copy_product(args.to, build_files=True)
    else:
        print(json.dumps(build(args.platform, args.to, args.runtime_archive)))


if __name__ == "__main__":
    main()
