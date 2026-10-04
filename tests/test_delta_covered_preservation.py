"""Synthetic covered-preservation regressions; no live assets and no models.

These tests pin the narrow ROOT correction for the isolated delta contract:

* A real synthetic case showed a partial update of a card plus an UNCHANGED
  manual paragraph being rejected SOLELY because the *card* was touched.  The
  correction is finer grained: a ``covered`` disposition may cite a segment of
  a card updated in THIS batch when that exact ORIGINAL segment is structurally
  untouched and its full bytes survive into the actual compiled resulting body.
* A ``replace`` targeting the cited segment must still reject, even when some
  of its words survive or an identical paragraph exists elsewhere.
* An ``insert_after`` on the cited segment preserves the original segment bytes
  and may therefore permit coverage; those exact bytes must still occur in the
  resulting body.
* A retiring card is never usable as ``covered``.
* One changed segment must not forbid coverage of a different untouched one.

Everything here is pure ``tongle.delta_contract`` (plus the pure receipt
``_dispositions``/``_short_refs`` helpers) with hand-authored fixtures.  No
model, network, vault, credentials or production asset is touched, and no
semantic model quality is claimed.
"""
import copy

import pytest

from tongle import delta_contract, delta_receipts, source_windows


# --------------------------------------------------------------- fixtures

def _part(part, text, responsibility='owned'):
    return {
        'part': part,
        'source_id': 'src',
        'version': 1,
        'role': 'user',
        'kind': 'message',
        'text': text,
        'session': 's1',
        'ledger_captured_at': '2026-01-01T00:00:00Z',
        'ledger_version_at': '2026-01-01T00:00:00Z',
        'responsibility': responsibility,
        'event_time': None,
    }


def _window(parts):
    return source_windows.freeze_window('proj', parts, 32000)


def _card(cid, title, body, manual=False, state='active', project='proj', version=1):
    return {
        'id': cid,
        'project': project,
        'version': version,
        'state': state,
        'manually_edited': manual,
        'title': title,
        'topic': 'topic',
        'basis_cards': [],
        'body': body,
        'segments': [],
    }


def _context(window, cards):
    return delta_contract.build_context(window, cards)


def _review(plan, manual_targets=()):
    checks = [{
        'index': i, 'E': 'pass', 'Q': 'pass', 'T': 'pass', 'N': 'pass',
        'reason': '证据与覆盖均复核通过',
    } for i in range(len(plan['dispositions']))]
    human = [{'target': t, 'preserved': True, 'reason': '人工段落逐字保留'}
             for t in manual_targets]
    return {
        'checks': checks,
        'whole_window': {'status': 'pass', 'reason': '整个窗口复核通过'},
        'human_preservation': human,
    }


# Two owned source paragraphs: one drives the update, the other is already
# fully contained by a different, untouched segment of the same card.
S1_TEXT = '本轮新增的待更新事实。'
S2_TEXT = '既有事实二。'
CARD_BODY = '待更新段落。\n\n既有事实二。'          # c1g1 = '待更新段落。\n\n', c1g2 = '既有事实二。'


def _two_span_window():
    return _window([_part(0, S1_TEXT), _part(1, S2_TEXT)])


def _covering_context(manual=True):
    window = _two_span_window()
    card = _card('c1', '混合卡', CARD_BODY, manual=manual)
    return window, _context(window, [{**card, 'evidence': []}])


def _update_then_cover(edit):
    """Plan response: n1 updates c1 with ``edit``; s1 material, s2 covered."""
    changes = [{
        'ref': 'n1', 'action': 'update', 'source_refs': ['s1'],
        'reason': '局部更新第一段', 'target': 'c1', 'edits': [edit],
    }]
    disps = [
        {'source_refs': ['s1'], 'decision': 'material', 'change_refs': ['n1'],
         'card_refs': [], 'reason': '本轮新事实由更新承接'},
        {'source_refs': ['s2'], 'decision': 'covered', 'change_refs': [],
         'card_refs': ['c1g2'], 'reason': '既有第二段已由未改写的原片段完整覆盖'},
    ]
    return changes, disps


# ------------------------------------------------------------------ cases

def test_partial_update_with_untouched_covered_segment_compiles_and_approves():
    # The exact ROOT case: another segment is rewritten, but the cited ORIGINAL
    # segment 2 is structurally untouched and its bytes survive the resulting
    # body, so coverage must be accepted (previously rejected solely because the
    # card was touched at all).
    window, ctx = _covering_context(manual=True)
    assert [seg['text'] for seg in ctx['cards'][0]['snapshot']['segments']] == [
        '待更新段落。\n\n', '既有事实二。']
    replace = {'op': 'replace', 'segment': 'c1g1', 'text': '更新后的段落。\n\n'}
    changes, disps = _update_then_cover(replace)

    plan = delta_contract.compile_plan(ctx, {'changes': changes, 'dispositions': disps})
    op = plan['operations'][0]
    assert op['op'] == 'update'
    # The untouched original segment bytes really are in the applied body.
    assert '既有事实二。' in op['body']
    assert [d['decision'] for d in plan['dispositions']] == ['material', 'covered']
    assert plan['dispositions'][1]['card_refs'] == ['c1g2']

    # The wire schema enumerates the still-citable active segment.
    schema = delta_contract.plan_payload(ctx)['response_schema']
    assert 'c1g2' in schema

    approved = delta_contract.approve_review(ctx, plan, _review(plan, manual_targets=['c1']))
    assert approved['approved'] is True
    assert approved['human_checks'][0]['id'] == 'c1'


def test_covered_receipt_keeps_original_segment_version_and_digest():
    # The proof recorded for coverage stays the ORIGINAL segment/version, and
    # the plan's applied card keeps its own real resulting version: no receipt
    # schema change, no substitution of the rewritten bytes for the cited ones.
    window, ctx = _covering_context(manual=False)
    replace = {'op': 'replace', 'segment': 'c1g1', 'text': '更新后的段落。\n\n'}
    changes, disps = _update_then_cover(replace)
    plan = delta_contract.compile_plan(ctx, {'changes': changes, 'dispositions': disps})

    original_version = ctx['cards'][0]['snapshot']['version']
    original_seg = ctx['cards'][0]['snapshot']['segments'][1]
    recorded = delta_receipts._dispositions(
        plan, ctx, delta_receipts._short_refs(ctx))
    covered = [d for d in recorded if d['decision'] == 'covered'][0]
    assert covered['covered'] == [{
        'ref': 'c1g2',
        'card_id': 'c1',
        'version': original_version,
        'segment_id': original_seg['id'],
        'segment_sha': delta_receipts._sha(original_seg['text']),
    }]
    # The rewritten resulting bytes are a distinct, normal applied version.
    assert plan['operations'][0]['expected_version'] == original_version


def test_replacing_the_cited_segment_rejects():
    # The update rewrites segment 2 itself: it can never be covered.
    window, ctx = _covering_context(manual=False)
    replace = {'op': 'replace', 'segment': 'c1g2', 'text': '改写后的第二段。'}
    changes, disps = _update_then_cover(replace)
    with pytest.raises(ValueError):
        delta_contract.compile_plan(ctx, {'changes': changes, 'dispositions': disps})


def test_replacing_cited_segment_rejects_even_when_words_survive():
    # A replace whose new text still CONTAINS the original segment bytes must
    # not be treated as preservation: the cited segment was structurally
    # rewritten.
    window, ctx = _covering_context(manual=False)
    replace = {'op': 'replace', 'segment': 'c1g2', 'text': '既有事实二。补充说明。'}
    changes, disps = _update_then_cover(replace)
    with pytest.raises(ValueError):
        delta_contract.compile_plan(ctx, {'changes': changes, 'dispositions': disps})


def test_retiring_covered_card_rejects():
    # A retire removes the card from active state, so none of its segments may
    # close a duty as covered.
    window = _window([_part(0, S1_TEXT), _part(1, S2_TEXT)])
    card = _card('c1', '待退休', CARD_BODY, manual=False)
    ctx = _context(window, [{**card, 'evidence': []}])
    changes = [{
        'ref': 'n1', 'action': 'retire', 'source_refs': ['s1'],
        'reason': '该卡已过时', 'target': 'c1',
    }]
    disps = [
        {'source_refs': ['s1'], 'decision': 'material', 'change_refs': ['n1'],
         'card_refs': [], 'reason': '退休决策由 retire 承接'},
        {'source_refs': ['s2'], 'decision': 'covered', 'change_refs': [],
         'card_refs': ['c1g2'], 'reason': '误用被退休卡作为覆盖'},
    ]
    with pytest.raises(ValueError):
        delta_contract.compile_plan(ctx, {'changes': changes, 'dispositions': disps})


def test_duplicate_text_elsewhere_cannot_excuse_replaced_cited_segment():
    # A second card holds a verbatim-identical paragraph and the same card's
    # first segment still contains the cited text, yet the CITED segment was
    # replaced, so coverage must still reject.
    window = _window([_part(0, S1_TEXT), _part(1, '重复段落。')])
    dup_body = '重复段落。\n\n重复段落。'   # both segments carry identical text
    target = _card('c1', '目标', dup_body, manual=False)
    twin = _card('c2', '同文卡', dup_body, manual=False)
    ctx = _context(window, [{**target, 'evidence': []}, {**twin, 'evidence': []}])
    assert [s['text'] for s in ctx['cards'][0]['snapshot']['segments']] == [
        '重复段落。\n\n', '重复段落。']
    # Replace the SECOND segment of c1 (the cited one) with new text; the
    # identical first segment and the identical twin card both keep the bytes.
    replace = {'op': 'replace', 'segment': 'c1g2', 'text': '改写第二段。'}
    changes = [{
        'ref': 'n1', 'action': 'update', 'source_refs': ['s1'],
        'reason': '改写第二段', 'target': 'c1', 'edits': [replace],
    }]
    disps = [
        {'source_refs': ['s1'], 'decision': 'material', 'change_refs': ['n1'],
         'card_refs': [], 'reason': '更新承接'},
        {'source_refs': ['s2'], 'decision': 'covered', 'change_refs': [],
         'card_refs': ['c1g2'], 'reason': '别处有相同文字并不意味着本片段被保留'},
    ]
    with pytest.raises(ValueError):
        delta_contract.compile_plan(ctx, {'changes': changes, 'dispositions': disps})


def test_insert_after_on_cited_segment_is_accepted():
    # insert_after preserves the original segment bytes (card_delta keeps the
    # segment), so the same-segment citation may stand.
    window, ctx = _covering_context(manual=False)
    insert = {'op': 'insert_after', 'segment': 'c1g2', 'text': '\n\n补充一句。'}
    changes, disps = _update_then_cover(insert)
    # Cite the segment the insert was attached to (c1g2), proving preservation.
    disps[1]['card_refs'] = ['c1g2']
    plan = delta_contract.compile_plan(ctx, {'changes': changes, 'dispositions': disps})
    op = plan['operations'][0]
    assert op['op'] == 'update'
    assert '既有事实二。' in op['body']
    assert '补充一句。' in op['body']
    assert [d['decision'] for d in plan['dispositions']] == ['material', 'covered']
    approved = delta_contract.approve_review(ctx, plan, _review(plan))
    assert approved['approved'] is True


def test_other_changed_segment_does_not_forbid_untouched_covered_segment():
    # Explicit contrast with the reject cases: replacing segment 1 leaves
    # segment 2 fully citable, and the untouched-segment citation succeeds.
    window, ctx = _covering_context(manual=False)
    replace = {'op': 'replace', 'segment': 'c1g1', 'text': '完全改写的第一段。\n\n'}
    changes, disps = _update_then_cover(replace)
    plan = delta_contract.compile_plan(ctx, {'changes': changes, 'dispositions': disps})
    assert plan['dispositions'][1]['decision'] == 'covered'
    assert plan['dispositions'][1]['card_refs'] == ['c1g2']
    approved = delta_contract.approve_review(ctx, plan, _review(plan))
    assert approved['approved'] is True
    # Deterministic: identical input yields the identical plan.
    assert delta_contract.compile_plan(
        ctx, {'changes': copy.deepcopy(changes), 'dispositions': copy.deepcopy(disps)}) == plan


def test_active_only_and_supplied_segment_rules_still_hold():
    # Confined to the pre-existing active/cited rules the correction must keep:
    # a retired card is not a legal covered destination, a non-supplied segment
    # ref is rejected, and a shared (read-only) card is still not a write target.
    retired = _card('c1', 'Old', CARD_BODY, state='retired')
    ctx = _context(_two_span_window(), [{**retired, 'evidence': []}])
    create = {
        'ref': 'n1', 'action': 'create', 'source_refs': ['s1'],
        'reason': 'new fact', 'title': 'Title', 'topic': 'topic', 'body': 'Body.',
    }
    disps = [
        {'source_refs': ['s1'], 'decision': 'material', 'change_refs': ['n1'],
         'card_refs': [], 'reason': 'new fact saved'},
        {'source_refs': ['s2'], 'decision': 'covered', 'change_refs': [],
         'card_refs': ['c1g2'], 'reason': 'retired card cannot cover'},
    ]
    with pytest.raises(ValueError):
        delta_contract.compile_plan(ctx, {'changes': [create], 'dispositions': disps})

    # Unknown segment ref is still rejected on an otherwise valid plan.
    card2 = _card('c1', 'Own', CARD_BODY)
    ctx2 = _context(_two_span_window(), [{**card2, 'evidence': []}])
    bad = copy.deepcopy(disps)
    bad[1]['card_refs'] = ['c1g9']
    with pytest.raises(ValueError):
        delta_contract.compile_plan(ctx2, {'changes': [create], 'dispositions': bad})

    # A shared card remains an illegal write target (unchanged rule).
    shared = _card('c1', 'Shared', CARD_BODY, project='shared')
    ctx3 = _context(_two_span_window(), [{**shared, 'evidence': []}])
    update_shared = {
        'ref': 'n1', 'action': 'update', 'source_refs': ['s1'],
        'reason': 'x', 'target': 'c1',
        'edits': [{'op': 'replace', 'segment': 'c1g1', 'text': 'y\n\n'}],
    }
    with pytest.raises(ValueError):
        delta_contract.compile_plan(ctx3, {'changes': [update_shared], 'dispositions': disps})


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-q']))
