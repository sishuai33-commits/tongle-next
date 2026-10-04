"""Make sibling test modules importable regardless of the invocation dir.

tests/ modules import each other directly (e.g. ``import test_acceptance``);
without this, ``pytest tests`` from the repo root fails on ModuleNotFoundError
and the suite can only be run from inside tests/.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))
