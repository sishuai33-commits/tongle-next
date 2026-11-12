"""Host-independent Tongle protocol declarations."""
from copy import deepcopy

from .memory_help import memory_help

PROTOCOL_VERSION = "1.0"

_ERROR_SEMANTICS = {
    "applied": {
        "applied": True, "stored": True, "publication_pending": False,
        "description": "Successfully stored."
    },
    "publication_pending": {
        "applied": True, "stored": True, "publication_pending": True,
        "description": "Database committed; file publication is pending."
    },
    "draft": {
        "applied": False, "stored": False, "publication_pending": False,
        "description": "Draft created; the attempted write was not applied."
    },
    "conflict": {
        "applied": False, "stored": False, "publication_pending": False,
        "requires_refreshed_state": True,
        "description": "Not applied; refresh state before any changed attempt."
    },
    "invalid_request": {
        "applied": False, "retry_blindly": False,
        "description": "Input is invalid; correct it before retrying."
    },
    "not_found": {
        "applied": False, "retry_blindly": False,
        "description": "Requested object was not found; do not retry blindly."
    },
    "unavailable": {
        "applied": False, "retry_blindly": False,
        "description": "Dependency or state unavailable; do not retry blindly."
    },
    "internal_error": {
        "applied": None, "commit_state": "unknown", "safe_retry": False,
        "reuse_event_without_verification": False, "new_event": False,
        "description": "Commit state unknown; do not claim safe retry or create a new event."
    },
}


def capabilities():
    """Return a new, JSON-serializable declaration of the CLI contract."""
    return {
        "name": "tongle",
        "protocol_version": PROTOCOL_VERSION,
        "core_version": "0.4.1",
        "baseline_hosts": ["pi", "codex"],
        "transport": "cli-json",
        "outcomes": deepcopy(_ERROR_SEMANTICS),
        "write_contract": deepcopy(memory_help()),
        "limits": {
            "statement_chars": 6000, "evidence_entries": 12,
            "conditions": 8, "condition_chars": 400,
            "note_page_chars": 4000, "search_candidates": 8,
        },
        "roles": ["project", "insight", "wiki", "material", "reflection"],
        "navigation": {
            "relations": ["related", "extends", "contrasts"],
            "invalidation": False,
        },
        "evidence": {
            "relations": ["supports", "refutes", "supersedes"],
            "version_bound": True, "needs_validation": True,
            "semantic_approval": False,
        },
        "declaration_only": True,
        "host_installed_or_working_proven": False,
        "server": False,
        "meaning": "Declared interface, not proof that a host is installed or working.",
    }


def classify_error(code):
    """Return fixed outcome semantics, rejecting undeclared codes."""
    if not isinstance(code, str) or code not in _ERROR_SEMANTICS:
        raise ValueError("unknown protocol error code: %r" % (code,))
    return deepcopy(_ERROR_SEMANTICS[code])


def require_compatible(version):
    if version is not None and version != PROTOCOL_VERSION:
        raise ValueError('unsupported protocol version; query capabilities before sending operations')
