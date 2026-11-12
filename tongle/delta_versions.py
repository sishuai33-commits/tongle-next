"""Explicit, fail-closed contract-version routing for v2 delta bundles.

A delta work bundle is executed against exactly ONE frozen contract module:

* version 1 -- the historical production contract, frozen byte-for-byte in
  ``tongle.delta_contract_legacy``. A bundle that predates the version field
  (no ``contract_version`` key at all) IS a version-1 bundle: every payload
  hash, compile, review approval, repair-feedback replay and completion
  receipt for it is reconstructed with the legacy module, so historical
  failed/publication_pending/budget-wait bundles keep replaying exactly.
* version 2 -- the frozen ``tongle.delta_contract``.
* version 3 -- ``tongle.delta_contract_v3`` fixes schema examples and narrowly
  normalizes plain-prose block separators.
* version 4 -- ``tongle.delta_contract_v4`` states the three rules the host
  already enforced but the task text never said, and names the offending entry
  in structural review rejections. Every NEW bundle uses version 4.

The version is DATA carried by the bundle itself; it is resolved through the
pure functions below at every use site. There is no global or mutable routing
state, so the mapping is thread-safe and one work row's version can never
redirect another row's contract.

A missing key defaults to version 1 (historical bundles). Anything else that
is not exactly one of the known integers -- a bool, a string, a float, an unknown
integer -- is REJECTED, never coerced or silently defaulted: swapping the
contract module re-compiles and re-hashes every cached step, so an unknown
version fails closed long before any payload is rebuilt.

Because a payload is hashed into the cached step, ANY change to a contract's
task or rejection text must land in a NEW version rather than editing a live
module: editing one re-hashes every in-flight step and fails otherwise healthy
budget-wait/publication-pending rows with ``cached payload hash mismatch``.
"""

from __future__ import annotations

LEGACY_VERSION = 1
CURRENT_VERSION = 5
KNOWN_VERSIONS = (LEGACY_VERSION, 2, 3, 4, CURRENT_VERSION)

# The bundle key carrying the explicit contract version.
VERSION_FIELD = 'contract_version'

# The exact ValueError text the contract's ``approve_review`` raises at its
# FINAL semantic gate (a disposition check E/Q/T/N, the whole-window verdict,
# an unresolved plan or a human-preservation verdict that is not a pass).
# Every EARLIER raise is a structural defect of the review reply itself
# (shape, indices, bounds), never a semantic verdict. The executor's version-2
# path classifies on this exact string; version 1 keeps the historical
# conflation byte-for-byte and never reads it.
SEMANTIC_REJECTION = 'semantic review not approved'

# Bounded version-2 repair target: the review REPLY was structurally malformed
# while the plan stayed valid, so only the reviewer is asked to fix its format
# (one extra review call inside the existing 2-call review budget; no plan
# re-dispatch). The existing kinds stay valid and unchanged.
REVIEW_FORMAT_TARGET = 'review_response'

LEGACY_REPAIR_KINDS = ('plan', 'unresolved', 'review')
REPAIR_KINDS = LEGACY_REPAIR_KINDS

# Versions whose contract supports the bounded REVIEW-FORMAT repair target: the
# review REPLY was structurally malformed while the plan stayed valid, so only
# the reviewer is asked to fix its format (one extra review call inside the
# existing budget, no plan re-dispatch). Derived from KNOWN_VERSIONS so a new
# wrapping version inherits it instead of being silently excluded by a
# hard-coded pair.
REVIEW_FORMAT_VERSIONS = tuple(v for v in KNOWN_VERSIONS if v >= 2)

__all__ = [
    'LEGACY_VERSION', 'CURRENT_VERSION', 'KNOWN_VERSIONS', 'VERSION_FIELD',
    'SEMANTIC_REJECTION', 'REVIEW_FORMAT_TARGET', 'LEGACY_REPAIR_KINDS',
    'REPAIR_KINDS', 'REVIEW_FORMAT_VERSIONS', 'resolve_version', 'contract_for',
]


def resolve_version(bundle=None, version=None):
    """Resolve the explicit contract version, or raise ValueError.

    ``version`` (when given) wins; otherwise the bundle's stored
    ``contract_version``; only a missing key is a historical version-1
    bundle. An explicit null is invalid. A bool, a non-integer or an unknown integer is rejected.
    """
    if version is None and isinstance(bundle, dict):
        if VERSION_FIELD not in bundle:
            return LEGACY_VERSION
        version = bundle[VERSION_FIELD]
        if version is None:
            raise ValueError('bundle contract version malformed')
    if version is None:
        return LEGACY_VERSION
    if isinstance(version, bool) or not isinstance(version, int):
        raise ValueError('bundle contract version malformed')
    if version not in KNOWN_VERSIONS:
        raise ValueError('bundle contract version unsupported')
    return version


def contract_for(bundle=None, version=None):
    """The frozen contract module for one explicit bundle version.

    Versions 1..3 retain their frozen modules; version 4 wraps version 3.
    Imports are local so this module itself
    stays dependency-free and import-order safe.
    """
    resolved = resolve_version(bundle=bundle, version=version)
    if resolved == LEGACY_VERSION:
        from . import delta_contract_legacy
        return delta_contract_legacy
    if resolved == 2:
        from . import delta_contract
        return delta_contract
    if resolved == 3:
        from . import delta_contract_v3
        return delta_contract_v3
    if resolved == 4:
        from . import delta_contract_v4
        return delta_contract_v4
    from . import delta_contract_v5
    return delta_contract_v5
