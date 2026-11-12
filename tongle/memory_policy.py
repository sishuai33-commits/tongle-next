"""Processing-mode policy for Tongle homes (stdlib only, no evolution import).

Memory-only mode keeps the knowledge store (``knowledge_cards`` and the rest of
``state.sqlite3``) working but disables semantic processing for explicitly
scoped projects. The scope is an *exact* project-name list: no wildcard, no
prefix, no implicit expansion. Anything malformed raises ``MemoryPolicyError``
instead of falling through to "processing enabled".

This module never imports ``tongle.evolution`` and never prints configuration.
It only reads ``<home>/config.json`` and answers questions about it.
"""

from __future__ import annotations

import json
from pathlib import Path

# Three accepted knowledge modes. ``legacy`` is the pre-existing storage
# backend and must NOT be routed through the knowledge store.
VALID_MODES = ("legacy", "evolving", "memory")
KNOWLEDGE_MODES = ("evolving", "memory")
DISPOSITION_PROCESSING = "processing"
DISPOSITION_MEMORY = "memory"
_DISPOSITIONS = (DISPOSITION_PROCESSING, DISPOSITION_MEMORY)


class MemoryPolicyError(ValueError):
    """Raised when knowledge mode or project policy is malformed."""


# ----------------------------------------------------------------- config

def read_home_config(home):
    """Return the parsed ``<home>/config.json`` mapping, or ``{}`` if absent.

    A missing file is the documented default (no mode key, empty project list).
    A present-but-malformed file raises, because a broken config must not be
    silently interpreted as "processing enabled".
    """
    path = Path(home) / "config.json"
    if path.is_symlink():
        raise MemoryPolicyError("configuration symlink rejected")
    if not path.is_file():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
        settings = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise MemoryPolicyError(
            "unreadable configuration: %s" % type(exc).__name__) from exc
    if not isinstance(settings, dict):
        raise MemoryPolicyError("configuration must be a JSON object")
    return settings


def _validate_mode(settings):
    if "knowledge_mode" not in settings:
        return None
    mode = settings["knowledge_mode"]
    if not isinstance(mode, str) or mode not in VALID_MODES:
        raise MemoryPolicyError(
            "invalid knowledge_mode %r; expected one of %s"
            % (mode, ", ".join(VALID_MODES)))
    return mode


def _validate_projects(settings):
    if "knowledge_memory_projects" not in settings:
        return []
    projects = settings["knowledge_memory_projects"]
    if not isinstance(projects, list):
        raise MemoryPolicyError("knowledge_memory_projects must be a list")
    seen = []
    for item in projects:
        if not isinstance(item, str) or not item or not item.strip():
            raise MemoryPolicyError(
                "knowledge_memory_projects entries must be non-empty strings")
        if any(ch in item for ch in ("*", "?", "[", "]")):
            raise MemoryPolicyError(
                "knowledge_memory_projects does not accept wildcards: %r" % item)
        if item not in seen:
            seen.append(item)
    return seen


# ---------------------------------------------------------------- queries

def uses_knowledge_store(settings):
    """True only for ``evolving`` or ``memory`` knowledge modes.

    Absent mode (no ``knowledge_mode`` key) and explicit ``legacy`` both mean
    the pre-existing storage backend: return False and do NOT reroute it to
    the knowledge store. Project-memory overrides alone never switch an
    otherwise-legacy backend; they only disable processing within an enabled
    store. Malformed values raise.
    """
    if settings is None:
        settings = {}
    if not isinstance(settings, dict):
        raise MemoryPolicyError("settings must be a mapping")
    mode = _validate_mode(settings)
    _validate_projects(settings)
    return mode in KNOWLEDGE_MODES


def processing_allowed(settings, project):
    """True if semantic processing may run for ``project`` under ``settings``.

    * A global ``knowledge_mode='memory'`` disables processing everywhere.
    * List membership in ``knowledge_memory_projects`` is EXACT and opts a
      single project out even while the global mode is ``evolving`` (an
      evolving instance can have memory-only projects).
    * ``legacy`` and absent modes keep their own processing path; the
      project-memory override does not apply because there is no knowledge
      store to gate.
    * Projects not named keep prior behavior, i.e. processing is allowed.
    """
    if settings is None:
        settings = {}
    if not isinstance(settings, dict):
        raise MemoryPolicyError("settings must be a mapping")
    if not isinstance(project, str) or not project.strip():
        raise MemoryPolicyError("project must be a non-empty string")
    mode = _validate_mode(settings)
    memory_projects = _validate_projects(settings)
    if mode == "memory":
        return False
    if mode not in KNOWLEDGE_MODES:
        # legacy/absent: knowledge-store gating does not apply at all.
        return True
    if project in memory_projects:
        return False
    return True


def capture_disposition(settings, project):
    """The disposition string to persist with a captured source/version.

    Persisting at capture time makes the disposition immutable for that
    source/version regardless of later mode changes.
    """
    return DISPOSITION_PROCESSING if processing_allowed(settings, project) \
        else DISPOSITION_MEMORY


def validate_disposition(value):
    if value not in _DISPOSITIONS:
        raise MemoryPolicyError(
            "unknown processing disposition %r" % (value,))
    return value


def admission_for(disposition, *, mode_memory=False):
    """Whether semantic processing may enqueue a persisted source/version.

    ``disposition`` is the value that was written at capture time. It is the
    authority: a source captured as memory-only stays memory-only even if the
    mode later changes (and vice versa is also true -- playback is by the
    recorded value, not the current mode).
    """
    validate_disposition(disposition)
    # ``mode_memory`` is accepted for callers that want an extra explicit gate.
    # It can only NARROW, never widen, admission.
    return disposition == DISPOSITION_PROCESSING and not mode_memory
