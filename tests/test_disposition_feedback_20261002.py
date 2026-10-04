"""Disposition feedback must name the offending ref, and the plan task must
teach the cross-reference rules the compiler enforces.

Regression context (2026-10-02): three production works died at StageLimit
because the model twice mis-wired dispositions and the repair feedback label
carried no offending ref name (``disposition 0 source ref not used by any
selected change`` -- which ref?).  The fix names the exact ref in every
disposition wiring error and teaches the two implicit compile rules in the
plan task text.
"""

import pytest

from tongle import delta_contract, source_windows


def _part(part, text, responsibility='owned', role='user'):
    return {
        'part': part,
        'source_id': 'src',
        'version': 1,
        'role': role,
        'kind': 'message',
        'text': text,
        'session': 's1',
        'ledger_captured_at': '2026-01-01T00:00:00Z',
        'ledger_version_at': '2026-01-01T00:00:00Z',
        'responsibility': responsibility,
        'event_time': None,
    }


def _ctx(parts):
    return delta_contract.build_context(source_windows.freeze_window('proj', parts, 32000), [])


def _create(ref, source_refs):
    return {
        'ref': ref,
        'action': 'create',
        'source_refs': source_refs,
        'reason': 'durable fact',
        'title': 'Title',
        'topic': 'topic',
        'body': 'Body text.',
    }


def _dispo(source_refs, decision, change_refs=()):
    return {
        'source_refs': list(source_refs),
        'decision': decision,
        'change_refs': list(change_refs),
        'card_refs': [],
        'reason': 'specific reason',
    }


def test_non_owned_ref_error_names_the_ref():
    # p1 is read-only reference (s1); the owned responsibility starts at s2.
    ctx = _ctx([_part(0, '前文参照。', 'reference'), _part(1, '正式来源。')])
    response = {'changes': [], 'dispositions': [_dispo(['s1'], 'nonmaterial')]}
    with pytest.raises(ValueError, match=r"disposition 0 source ref s1 is not an owned responsibility"):
        delta_contract.compile_plan(ctx, response)


def test_unused_ref_error_names_the_ref():
    # One owned event with two paragraphs -> s1 and s2 are both owned.
    ctx = _ctx([_part(0, '第一段。\n\n第二段。')])
    change = _create('n1', ['s1'])
    dispositions = [_dispo(['s1', 's2'], 'material', ['n1'])]
    with pytest.raises(ValueError, match=r"disposition 0 source ref s2 not used by any selected change"):
        delta_contract.compile_plan(ctx, {'changes': [change], 'dispositions': dispositions})


def test_unlinked_change_error_names_the_change():
    ctx = _ctx([_part(0, '来源。')])
    change = _create('n1', ['s1'])
    dispositions = [_dispo(['s1'], 'nonmaterial')]
    with pytest.raises(ValueError, match=r"change n1 not linked by material disposition"):
        delta_contract.compile_plan(ctx, {'changes': [change], 'dispositions': dispositions})


def test_plan_task_teaches_owned_list_and_cross_reference():
    ctx = _ctx([_part(0, '前文参照。', 'reference'), _part(1, '正式来源。')])
    task = delta_contract.plan_payload(ctx)['task']
    assert '逐字取自 owned_source_refs 清单' in task
    assert '必须同时出现在其链接的至少一个 change 的 source_refs 中' in task
    assert 'changes 必须为空数组' in task
