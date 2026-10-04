"""Frozen/source entry point for the Tongle product CLI.

Dispatches to internal modules/scripts without importing private runtime
or emitting credentials. Runs under PyInstaller (data root is
``sys._MEIPASS``) and from a source checkout (data root is the repo root
containing ``tongle/`` and ``scripts/``).
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path


def _data_root() -> Path:
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass).resolve()
    return Path(__file__).resolve().parents[1]


ROOT = _data_root()

_INTERNAL_MODULES = frozenset({"tongle", "tongle.evaluate"})


def _allowed_scripts(root: Path) -> dict:
    return {
        (root / "scripts" / "maintain.py").resolve(): True,
        (root / "scripts" / "http_model.py").resolve(): True,
        (root / "adapters" / "hook.py").resolve(): True,
        (root / "adapters" / "production.py").resolve(): True,
    }


def _run_module(module: str, rest, root: Path) -> int:
    if module not in _INTERNAL_MODULES:
        return _reject(module)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from tongle.portability import configure_stdio
    configure_stdio()
    sys.argv = [module] + list(rest)
    runpy.run_module(module, run_name="__main__")
    return 0


def _run_script(candidate: str, rest, root: Path) -> int:
    try:
        resolved = Path(candidate).resolve()
    except (OSError, ValueError):
        return _reject(candidate)
    if resolved not in _allowed_scripts(root):
        return _reject(candidate)
    parent = str(resolved.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from tongle.portability import configure_stdio
    configure_stdio()
    sys.argv = [str(resolved)] + list(rest)
    runpy.run_path(str(resolved), run_name="__main__")
    return 0


def _reject(value: str) -> int:
    sys.stderr.write("tongle: refusing unknown entry: %s\n" % (value,))
    return 2


def main() -> int:
    argv = list(sys.argv[1:])

    if argv and argv[0] == "-m":
        if len(argv) >= 2:
            module = argv[1]
            rest = argv[2:]
            return _run_module(module, rest, ROOT)
        return _reject("-m")

    if argv and argv[0] and Path(argv[0]).is_absolute():
        return _run_script(argv[0], argv[1:], ROOT)

    # Default: product CLI with argv unchanged.
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from tongle.portability import configure_stdio
    configure_stdio()
    sys.argv = ["tongle.product"] + argv
    runpy.run_module("tongle.product", run_name="__main__")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except KeyboardInterrupt:
        raise SystemExit(130)
