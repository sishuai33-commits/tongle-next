"""Version-4 delta contract: the rules the host enforces but never stated, and
structural review feedback that names the offending entry.

Why a new module instead of editing version 3
---------------------------------------------
``tongle.delta_versions`` pins each bundle to exactly one frozen contract
module, because a payload is *hashed into the cached step*:
``delta_execution._verify_step`` re-builds the payload it would send now and
requires the cached step to match byte-for-byte, and a semantic repair carries
the previous rejection text inside the hashed ``repair_feedback``. Editing a
live module's task text or rejection text therefore re-hashes every in-flight
cached step and turns an otherwise healthy ``budget_wait``/
``publication_pending`` row into a terminal ``cached payload hash mismatch``
failure. Version 3 exists for exactly this reason; this module follows the same
pattern rather than editing it.

What it adds over version 3
---------------------------
* The task text states three rules the host already vetoed deterministically but
  the model-facing text never said: only ``scope=own`` cards may be update/retire
  targets, ``checks`` must be exactly one per disposition, and every ``reason``
  must be non-empty and within ``REASON_MAX``.
* Structural review rejections name the offending entry -- ``check <i> reason``,
  ``check <i> <E|Q|T|N>``, required vs got counts -- so the single bounded repair
  round is not blind (the same gap class fixed for plan dispositions on
  2026-10-02).
* The FINAL semantic gate keeps ``delta_versions.SEMANTIC_REJECTION``
  byte-exact: the executor classifies on that string.

The structural pre-check below mirrors the frozen module's rules exactly. It
only *names* defects the frozen module would also reject, so a valid review is
never rejected here; anything it cannot name falls through to the frozen module
unchanged.
"""
from . import delta_contract as _v2
from . import delta_contract_v3 as _v3

REVIEW_STATES = ('pass', 'fail', 'unknown')

_PLAN_APPEND = (
    'update 与 retire 只能指向 scope=own 的卡片（own_source_refs 对应的自身范围）：'
    'reference 卡片只用于理解，host 会确定性拒绝把 reference 卡片当作 update/retire 目标。'
)

_REVIEW_APPEND = (
    '结构补充：checks 必须恰好一条对应一条 disposition，数量必须等于 disposition 数量、'
    'index 必须不重不漏地覆盖每条；每条 check 的 reason、whole_window 的 reason、以及每条 '
    'human_preservation 的 reason 都必须非空且不超过 400 字符。'
)


def _append_task(payload, extra):
    task = payload.get('task')
    payload['task'] = (task + extra) if isinstance(task, str) else extra
    return payload


def plan_payload(ctx):
    return _append_task(_v3.plan_payload(ctx), _PLAN_APPEND)


def review_payload(ctx, plan):
    return _append_task(_v3.review_payload(ctx, plan), _REVIEW_APPEND)


def _reason_defect(idx, reason):
    """A body-free description of a bad check reason.

    The model's reason text never enters the message: the rejection label is
    replayed into the next prompt and stored in the audit ledger, so only the
    shape (empty / too long) is reported.
    """
    if not isinstance(reason, str):
        return 'check %d reason must be a string' % idx
    if not reason.strip():
        return 'check %d reason must not be blank' % idx
    if len(reason) > _v2.REASON_MAX:
        return ('check %d reason is %d chars, over the %d char limit'
                % (idx, len(reason), _v2.REASON_MAX))
    return None


def _named_structural_defect(plan, response):
    """Name a structural review defect the frozen module would also reject.

    Returns ``None`` when this pre-check cannot name the defect, so the frozen
    module stays the authority for every shape this function does not cover.
    """
    if not isinstance(plan, dict) or not isinstance(response, dict):
        return None
    dispositions = plan.get('dispositions')
    if not isinstance(dispositions, list):
        return None
    checks = response.get('checks')
    if not isinstance(checks, list):
        return ('checks must be exactly one per disposition'
                ' (required %d, got not a list)' % len(dispositions))
    if len(checks) != len(dispositions):
        return ('checks must be exactly one per disposition'
                ' (required %d, got %d)' % (len(dispositions), len(checks)))
    seen = set()
    for check in checks:
        if not isinstance(check, dict):
            return None
        if set(check) != {'index', 'E', 'Q', 'T', 'N', 'reason'}:
            return None
        idx = check.get('index')
        if not _v2._is_int(idx):
            return None
        if idx < 0 or idx >= len(dispositions):
            return ('check index %d out of range 0..%d'
                    % (idx, len(dispositions) - 1))
        if idx in seen:
            return 'duplicate check index %d' % idx
        seen.add(idx)
        for letter in ('E', 'Q', 'T', 'N'):
            if check.get(letter) not in REVIEW_STATES:
                return ('check %d %s must be pass/fail/unknown, got %r'
                        % (idx, letter, check.get(letter)))
        defect = _reason_defect(idx, check.get('reason'))
        if defect:
            return defect
    missing = [i for i in range(len(dispositions)) if i not in seen]
    if missing:
        return ('missing check for disposition index %s'
                % ', '.join(str(i) for i in missing))
    return None


def approve_review(ctx, plan, response):
    named = _named_structural_defect(plan, response)
    if named is not None:
        raise ValueError(named)
    return _v3.approve_review(ctx, plan, response)


def __getattr__(name):
    return getattr(_v3, name)
