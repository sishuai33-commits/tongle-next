import copy
import json

import pytest

from tongle import card_delta, delta_contract, source_windows


def _part(part, text, responsibility='owned', role='user', kind='message', ledger_version_at='2026-01-01T00:00:00Z'):
    return {
        'part': part,
        'source_id': 'src',
        'version': 1,
        'role': role,
        'kind': kind,
        'text': text,
        'session': 's1',
        'ledger_captured_at': '2026-01-01T00:00:00Z',
        'ledger_version_at': ledger_version_at,
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


def _spans(window):
    refs = []
    n = 0
    for part in window['parts']:
        for _ in part['span_ids']:
            n += 1
            refs.append(f's{n}')
    return refs


def _disps(window, decision_by_ref, changes):
    out = []
    n = 0
    for part in window['parts']:
        for _ in part['span_ids']:
            n += 1
            ref = f's{n}'
            decision = decision_by_ref.get(ref, 'nonmaterial')
            out.append({
                'source_refs': [ref],
                'decision': decision,
                'change_refs': [c['ref'] for c in changes] if decision == 'material' else [],
                'card_refs': [],
                'reason': 'specific reason for ' + ref,
            })
    return out


def _basic(extra=''):
    window = _window([_part(0, 'Remember the deadline is Friday. ' + extra)])
    ctx = _context(window, [])
    change = {
        'ref': 'n1',
        'action': 'create',
        'source_refs': ['s1'],
        'reason': 'durable deadline',
        'title': 'Deadline',
        'topic': 'schedule',
        'body': 'Friday deadline',
    }
    disps = _disps(window, {'s1': 'material'}, [change])
    return ctx, change, disps


def _review(plan, manual_targets=(), reason='manual content preserved exactly'):
    checks = []
    for i, _ in enumerate(plan['dispositions']):
        checks.append({
            'index': i,
            'E': 'pass',
            'Q': 'pass',
            'T': 'pass',
            'N': 'pass',
            'reason': 'checked evidence and coverage',
        })
    human = [{'target': target, 'preserved': True, 'reason': reason} for target in manual_targets]
    return {
        'checks': checks,
        'whole_window': {'status': 'pass', 'reason': 'whole window reviewed'},
        'human_preservation': human,
    }


INTERNAL_KEYS = {'id', 'version', 'hash', 'body_sha', 'body_sha256', 'segment_id'}


def _assert_no_internal_keys(value):
    if isinstance(value, dict):
        for key, val in value.items():
            assert key not in INTERNAL_KEYS, key
            _assert_no_internal_keys(val)
    elif isinstance(value, list):
        for item in value:
            _assert_no_internal_keys(item)


def test_create_and_persistence_deterministic():
    ctx, change, disps = _basic()
    response = {'changes': [change], 'dispositions': disps}
    plan = delta_contract.compile_plan(ctx, response)
    again = delta_contract.compile_plan(ctx, copy.deepcopy(response))
    assert plan == again
    payload = delta_contract.plan_payload(ctx)
    json.dumps(payload, sort_keys=True, allow_nan=False)
    assert payload['sources'][0]['spans'][0]['ref'] == 's1'
    assert plan['operations'][0]['op'] == 'create'
    assert plan['operations'][0]['key'].startswith('delta-')
    _assert_no_internal_keys(payload)


def test_local_update_preserves_existing_text_and_roundtrip():
    window = _window([_part(0, 'Add a note about safety.')])
    card = _card('c1', 'Existing', 'Existing text.', manual=True)
    ctx = _context(window, [{**card, 'evidence': [{'source_id': 'src', 'version': 1, 'quote': 'old'}]}])
    assert ctx['cards'][0]['snapshot']['segments']
    edit_text = chr(10) + '  indented safety note.' + chr(10)
    change = {
        'ref': 'n1',
        'action': 'update',
        'source_refs': ['s1'],
        'reason': 'append safety note',
        'target': 'c1',
        'edits': [{'op': 'insert_after', 'segment': 'c1g1', 'text': edit_text}],
    }
    plan = delta_contract.compile_plan(ctx, {'changes': [change], 'dispositions': _disps(window, {'s1': 'material'}, [change])})
    op = plan['operations'][0]
    assert op['op'] == 'update'
    assert 'Existing text.' in op['body']
    assert edit_text in op['body']
    assert plan['changes'][0]['edits'][0]['segment'] == 'c1g1'
    assert 'segment_id' not in plan['changes'][0]['edits'][0]
    payload = delta_contract.review_payload(ctx, plan)
    assert payload['draft']['changes'] == plan['changes']
    _assert_no_internal_keys(payload)
    approved = delta_contract.approve_review(ctx, plan, _review(plan, manual_targets=['c1']))
    assert approved['approved'] is True
    assert approved['human_checks']
    assert approved['human_checks'][0]['reason'] == 'manual content preserved exactly'
    assert approved['human_checks'][0]['id'] == 'c1'


def test_manual_retire_rejected_and_shared_update_rejected():
    window = _window([_part(0, 'Retire the old card.')])
    manual = _card('c1', 'Manual', 'text', manual=True)
    ctx = _context(window, [{**manual, 'evidence': []}])
    with pytest.raises(ValueError):
        delta_contract.compile_plan(ctx, {
            'changes': [{'ref': 'n1', 'action': 'retire', 'source_refs': ['s1'], 'reason': 'obsolete now', 'target': 'c1'}],
            'dispositions': _disps(window, {'s1': 'material'}, [{'ref': 'n1'}]),
        })
    shared = _card('c1', 'Shared', 'text', project='shared')
    ctx2 = _context(window, [{**shared, 'evidence': []}])
    with pytest.raises(ValueError):
        delta_contract.compile_plan(ctx2, {
            'changes': [{'ref': 'n1', 'action': 'retire', 'source_refs': ['s1'], 'reason': 'obsolete now', 'target': 'c1'}],
            'dispositions': _disps(window, {'s1': 'material'}, [{'ref': 'n1'}]),
        })


def test_retired_target_rejected():
    window = _window([_part(0, 'Retire the old card.')])
    retired = _card('c1', 'Old', 'text', state='retired')
    ctx = _context(window, [{**retired, 'evidence': []}])
    with pytest.raises(ValueError):
        delta_contract.compile_plan(ctx, {
            'changes': [{'ref': 'n1', 'action': 'retire', 'source_refs': ['s1'], 'reason': 'obsolete now', 'target': 'c1'}],
            'dispositions': _disps(window, {'s1': 'material'}, [{'ref': 'n1'}]),
        })


def test_covered_noop_and_uncovered_owned_reject():
    window = _window([_part(0, 'Same fact.')])
    card = _card('c1', 'Fact', 'Same fact.')
    ctx = _context(window, [{**card, 'evidence': []}])
    assert ctx['cards'][0]['snapshot']['segments']
    disps = [{
        'source_refs': ['s1'],
        'decision': 'covered',
        'change_refs': [],
        'card_refs': ['c1g1'],
        'reason': 'full entailment by card segment',
    }]
    plan = delta_contract.compile_plan(ctx, {'changes': [], 'dispositions': disps})
    assert plan['operations'] == []
    assert plan['unresolved'] is False
    approved = delta_contract.approve_review(ctx, plan, _review(plan))
    assert approved['approved'] is True
    assert approved['completion_kind'] == 'no_op'
    with pytest.raises(ValueError):
        delta_contract.compile_plan(ctx, {'changes': [], 'dispositions': []})


def test_covered_unknown_segment_rejected():
    window = _window([_part(0, 'Same fact.')])
    card = _card('c1', 'Fact', 'Same fact.')
    ctx = _context(window, [{**card, 'evidence': []}])
    disps = [{
        'source_refs': ['s1'],
        'decision': 'covered',
        'change_refs': [],
        'card_refs': ['c1g999'],
        'reason': 'claimed but missing segment',
    }]
    with pytest.raises(ValueError):
        delta_contract.compile_plan(ctx, {'changes': [], 'dispositions': disps})


def test_reference_context_cannot_close_duty():
    window = _window([_part(0, 'Reference only.', responsibility='reference')])
    ctx = _context(window, [])
    assert ctx['protocol'] == delta_contract.PROTOCOL
    payload = delta_contract.plan_payload(ctx)
    assert 's1' in json.dumps(payload)
    with pytest.raises(ValueError):
        delta_contract.compile_plan(ctx, {
            'changes': [{'ref': 'n1', 'action': 'create', 'source_refs': ['s1'], 'reason': 'reason', 'title': 't', 'topic': 'q', 'body': 'b'}],
            'dispositions': [{'source_refs': ['s1'], 'decision': 'material', 'change_refs': ['n1'], 'card_refs': [], 'reason': 'claimed responsibility'}],
        })


def test_unresolved_blocks_approval_with_no_changes():
    window = _window([_part(0, 'Ambiguous fact.')])
    ctx = _context(window, [])
    plan = delta_contract.compile_plan(ctx, {
        'changes': [],
        'dispositions': [{'source_refs': ['s1'], 'decision': 'unresolved', 'change_refs': [], 'card_refs': [], 'reason': 'cannot decide now'}],
    })
    assert plan['unresolved'] is True
    with pytest.raises(ValueError, match='semantic review not approved'):
        delta_contract.approve_review(ctx, plan, _review(plan))


def test_false_review_and_unknown_extra_fields_reject():
    ctx, change, disps = _basic()
    plan = delta_contract.compile_plan(ctx, {'changes': [change], 'dispositions': disps})
    review = _review(plan)
    review['checks'][0]['N'] = 'fail'
    with pytest.raises(ValueError, match='semantic review not approved'):
        delta_contract.approve_review(ctx, plan, review)
    bad = {'changes': [dict(change, extra=1)], 'dispositions': disps}
    with pytest.raises(ValueError):
        delta_contract.compile_plan(ctx, bad)


def test_duplicate_source_text_distinct_references():
    window = _window([_part(0, 'Dup'), _part(1, 'Dup')])
    ctx = _context(window, [])
    refs = _spans(window)
    assert len(refs) >= 2
    assert window['spans'][0]['text'] == window['spans'][1]['text']
    assert window['spans'][0]['id'] != window['spans'][1]['id']
    changes = [{'ref': 'n1', 'action': 'create', 'source_refs': [refs[0]], 'reason': 'first durable fact', 'title': 'A', 'topic': 'a', 'body': 'a'}]
    disps = [
        {'source_refs': [refs[0]], 'decision': 'material', 'change_refs': ['n1'], 'card_refs': [], 'reason': 'first fact material'},
        {'source_refs': [refs[1]], 'decision': 'nonmaterial', 'change_refs': [], 'card_refs': [], 'reason': 'duplicate transport text'},
    ]
    plan = delta_contract.compile_plan(ctx, {'changes': changes, 'dispositions': disps})
    assert len(plan['operations'][0]['evidence']) == 1


def test_two_changes_share_source_both_keep_evidence():
    window = _window([_part(0, 'Shared fact.')])
    ctx = _context(window, [])
    changes = [
        {'ref': 'n1', 'action': 'create', 'source_refs': ['s1'], 'reason': 'first', 'title': 'A', 'topic': 'a', 'body': 'a'},
        {'ref': 'n2', 'action': 'create', 'source_refs': ['s1'], 'reason': 'second', 'title': 'B', 'topic': 'b', 'body': 'b'},
    ]
    disps = [{'source_refs': ['s1'], 'decision': 'material', 'change_refs': ['n1', 'n2'], 'card_refs': [], 'reason': 'two facts from same span'}]
    plan = delta_contract.compile_plan(ctx, {'changes': changes, 'dispositions': disps})
    assert len(plan['operations']) == 2
    for op in plan['operations']:
        assert len(op['evidence']) == 1
        assert op['evidence'][0]['quote'] == 'Shared fact.'


def test_wrong_manual_target_reject_and_correct_reason_retained():
    window = _window([_part(0, 'Add note.')])
    card = _card('c1', 'Manual', 'Existing text.', manual=True)
    ctx = _context(window, [{**card, 'evidence': []}])
    edit_text = chr(10) + '  note' + chr(10)
    change = {
        'ref': 'n1',
        'action': 'update',
        'source_refs': ['s1'],
        'reason': 'append note',
        'target': 'c1',
        'edits': [{'op': 'insert_after', 'segment': 'c1g1', 'text': edit_text}],
    }
    plan = delta_contract.compile_plan(ctx, {'changes': [change], 'dispositions': _disps(window, {'s1': 'material'}, [change])})
    wrong = _review(plan, manual_targets=['c2'], reason='wrong target reason')
    with pytest.raises(ValueError):
        delta_contract.approve_review(ctx, plan, wrong)
    correct = _review(plan, manual_targets=['c1'], reason='custom reviewer reason')
    approved = delta_contract.approve_review(ctx, plan, correct)
    assert approved['human_checks'][0]['reason'] == 'custom reviewer reason'
    assert approved['human_checks'][0]['id'] == 'c1'
    assert approved['human_checks'][0]['version'] == 1


def test_nonmanual_update_no_synthetic_human_checks():
    window = _window([_part(0, 'Add note.')])
    card = _card('c1', 'Nonmanual', 'Existing text.', manual=False)
    ctx = _context(window, [{**card, 'evidence': []}])
    edit_text = chr(10) + '  note' + chr(10)
    change = {
        'ref': 'n1',
        'action': 'update',
        'source_refs': ['s1'],
        'reason': 'append note',
        'target': 'c1',
        'edits': [{'op': 'insert_after', 'segment': 'c1g1', 'text': edit_text}],
    }
    plan = delta_contract.compile_plan(ctx, {'changes': [change], 'dispositions': _disps(window, {'s1': 'material'}, [change])})
    approved = delta_contract.approve_review(ctx, plan, _review(plan, manual_targets=[]))
    assert approved['human_checks'] == []
    extra = _review(plan, manual_targets=['c1'], reason='not manual')
    with pytest.raises(ValueError):
        delta_contract.approve_review(ctx, plan, extra)


def test_wire_payload_excludes_internal_keys_and_schema_is_dynamic():
    ctx, change, disps = _basic()
    plan = delta_contract.compile_plan(ctx, {'changes': [change], 'dispositions': disps})
    plan_payload = delta_contract.plan_payload(ctx)
    review_payload = delta_contract.review_payload(ctx, plan)
    _assert_no_internal_keys(plan_payload)
    _assert_no_internal_keys(review_payload)
    schema = json.loads(plan_payload['response_schema'])
    variants = schema['properties']['changes']['items']['oneOf']
    # No writable card in this context: only the create arm may exist, and no
    # empty-enum update/retire arm is fabricated.
    assert [v['title'] for v in variants] == ['create']
    by_title = {v['title']: v for v in variants}
    assert set(by_title['create']['required']) == {'ref', 'action', 'source_refs', 'reason', 'title', 'topic', 'body'}
    assert by_title['create']['properties']['source_refs']['items']['enum'] == ['s1']
    assert len(schema['examples']) == 1
    dispositions = schema['properties']['dispositions']['items']['oneOf']
    assert [v['title'] for v in dispositions] == ['material', 'nonmaterial', 'unresolved']
    assert set(v['properties']['decision']['const'] for v in dispositions) == {'material', 'nonmaterial', 'unresolved'}
    by_decision = {v['properties']['decision']['const']: v for v in dispositions}
    assert by_decision['material']['properties']['change_refs']['minItems'] == 1
    assert by_decision['material']['properties']['card_refs']['maxItems'] == 0
    assert by_decision['nonmaterial']['properties']['change_refs']['maxItems'] == 0
    assert by_decision['nonmaterial']['properties']['card_refs']['maxItems'] == 0
    assert by_decision['unresolved']['properties']['change_refs']['maxItems'] == 0
    assert by_decision['unresolved']['properties']['card_refs']['maxItems'] == 0
    assert by_decision['material']['properties']['source_refs']['items']['enum'] == ['s1']
    review_schema = json.loads(review_payload['response_schema'])
    assert set(review_schema['properties']['checks']['items']['required']) == {'index', 'E', 'Q', 'T', 'N', 'reason'}
    assert review_schema['properties']['checks']['items']['properties']['index']['enum'] == [0]
    assert review_schema['properties']['checks']['maxItems'] == 1
    # No touched manual target: array pinned empty, never an empty enum.
    assert review_schema['properties']['human_preservation']['maxItems'] == 0
    assert 'items' not in review_schema['properties']['human_preservation']
    assert plan_payload['owned_source_refs'] == ['s1']
    assert plan_payload['reference_source_refs'] == []
    assert review_payload['required_human_preservation_targets'] == []


def _plan_schema(ctx):
    return json.loads(delta_contract.plan_payload(ctx)['response_schema'])


def _arm_titles(schema):
    items = schema['properties']['changes']['items']
    return [v['title'] for v in items.get('oneOf', [])]


def _target_enum(schema, title):
    for variant in schema['properties']['changes']['items'].get('oneOf', []):
        if variant['title'] == title:
            return variant['properties']['target'].get('enum')
    return None


def test_plan_schema_arms_follow_host_write_state():
    window = _window([_part(0, 'A durable note.')])
    own_active = _card('c1', 'Own', 'Own body.', manual=False)
    own_manual = _card('c2', 'Manual', 'Manual body.', manual=True)
    own_retired = _card('c3', 'Old', 'Old body.', state='retired')
    shared = _card('c4', 'Shared', 'Shared body.', project='shared')
    own_conflict = _card('c5', 'Conflict', 'Conflict body.', state='conflict')
    ctx = _context(window, [
        {**own_active, 'evidence': []},
        {**own_manual, 'evidence': []},
        {**own_retired, 'evidence': []},
        {**shared, 'evidence': []},
        {**own_conflict, 'evidence': []},
    ])
    schema = _plan_schema(ctx)
    assert _arm_titles(schema) == ['create', 'update', 'retire']
    # Only writable own active/conflict cards are legal mutation targets.
    assert _target_enum(schema, 'update') == ['c1', 'c2', 'c5']
    assert _target_enum(schema, 'retire') == ['c1', 'c5']
    payload = delta_contract.plan_payload(ctx)
    allowed = {c['ref']: c['allowed_actions'] for c in payload['cards']}
    assert allowed['c1'] == ['update', 'retire']
    assert allowed['c2'] == ['update']
    assert allowed['c3'] == []
    assert allowed['c4'] == []
    assert allowed['c5'] == ['update', 'retire']


def test_plan_schema_removes_illegal_arms_and_keeps_no_empty_enum():
    window = _window([_part(0, 'A durable note.')])
    # Manual-only catalog: update stays, retire must vanish entirely.
    manual = _card('c1', 'Manual', 'Manual body.', manual=True)
    ctx = _context(window, [{**manual, 'evidence': []}])
    schema = _plan_schema(ctx)
    assert _arm_titles(schema) == ['create', 'update']
    assert _target_enum(schema, 'retire') is None
    # Shared-only catalog: no writable target at all, only create.
    shared = _card('c1', 'Shared', 'Shared body.', project='shared')
    ctx2 = _context(window, [{**shared, 'evidence': []}])
    schema2 = _plan_schema(ctx2)
    assert _arm_titles(schema2) == ['create']
    # Already-retired own card is never a write target either.
    retired = _card('c1', 'Old', 'Old body.', state='retired')
    ctx3 = _context(window, [{**retired, 'evidence': []}])
    assert _arm_titles(_plan_schema(ctx3)) == ['create']


def test_reference_only_context_has_no_owned_disposition_enum():
    window = _window([_part(0, 'Reference only.', responsibility='reference')])
    ctx = _context(window, [])
    payload = delta_contract.plan_payload(ctx)
    assert payload['owned_source_refs'] == []
    assert payload['reference_source_refs'] == ['s1']
    schema = json.loads(payload['response_schema'])
    assert _arm_titles(schema) == ['create']
    # Nothing owned to disposition: array pinned empty, no empty enum.
    assert schema['properties']['dispositions']['maxItems'] == 0
    assert 'items' not in schema['properties']['dispositions']
    plan = delta_contract.compile_plan(ctx, {'changes': [], 'dispositions': []})
    review = delta_contract.review_payload(ctx, plan)
    review_schema = json.loads(review['response_schema'])
    assert review_schema['properties']['checks']['maxItems'] == 0
    assert 'items' not in review_schema['properties']['checks']


def test_review_schema_pins_required_checks_and_manual_targets():
    window = _window([_part(0, 'Add a note about safety.')])
    card = _card('c1', 'Manual', 'Existing text.', manual=True)
    ctx = _context(window, [{**card, 'evidence': []}])
    edit_text = chr(10) + '  safety note.' + chr(10)
    change = {
        'ref': 'n1',
        'action': 'update',
        'source_refs': ['s1'],
        'reason': 'append safety note',
        'target': 'c1',
        'edits': [{'op': 'insert_after', 'segment': 'c1g1', 'text': edit_text}],
    }
    plan = delta_contract.compile_plan(ctx, {'changes': [change], 'dispositions': _disps(window, {'s1': 'material'}, [change])})
    payload = delta_contract.review_payload(ctx, plan)
    assert payload['required_human_preservation_targets'] == ['c1']
    schema = json.loads(payload['response_schema'])
    human = schema['properties']['human_preservation']
    assert human['minItems'] == human['maxItems'] == 1
    assert human['items']['properties']['target']['enum'] == ['c1']
    checks = schema['properties']['checks']
    assert checks['minItems'] == checks['maxItems'] == len(plan['dispositions'])
    assert checks['items']['properties']['index']['enum'] == list(range(len(plan['dispositions'])))


def test_review_schema_empty_manual_array_when_no_manual_update():
    window = _window([_part(0, 'Add a note.')])
    card = _card('c1', 'Nonmanual', 'Existing text.', manual=False)
    ctx = _context(window, [{**card, 'evidence': []}])
    change = {
        'ref': 'n1',
        'action': 'update',
        'source_refs': ['s1'],
        'reason': 'append note',
        'target': 'c1',
        'edits': [{'op': 'insert_after', 'segment': 'c1g1', 'text': chr(10) + ' note' + chr(10)}],
    }
    plan = delta_contract.compile_plan(ctx, {'changes': [change], 'dispositions': _disps(window, {'s1': 'material'}, [change])})
    payload = delta_contract.review_payload(ctx, plan)
    assert payload['required_human_preservation_targets'] == []
    human = json.loads(payload['response_schema'])['properties']['human_preservation']
    assert human['maxItems'] == 0
    assert 'items' not in human


def test_raw_forbidden_targets_still_rejected_by_compiler():
    # The generated enums never loosen the strict compile-time enforcement.
    window = _window([_part(0, 'Retire the old card.')])
    retired = _card('c1', 'Old', 'text', state='retired')
    ctx = _context(window, [{**retired, 'evidence': []}])
    with pytest.raises(ValueError):
        delta_contract.compile_plan(ctx, {
            'changes': [{'ref': 'n1', 'action': 'retire', 'source_refs': ['s1'], 'reason': 'obsolete now', 'target': 'c1'}],
            'dispositions': _disps(window, {'s1': 'material'}, [{'ref': 'n1'}]),
        })
    shared = _card('c1', 'Shared', 'text', project='shared')
    ctx2 = _context(window, [{**shared, 'evidence': []}])
    with pytest.raises(ValueError):
        delta_contract.compile_plan(ctx2, {
            'changes': [{'ref': 'n1', 'action': 'update', 'source_refs': ['s1'], 'reason': 'x', 'target': 'c1',
                         'edits': [{'op': 'insert_after', 'segment': 'c1g1', 'text': ' x'}]}],
            'dispositions': _disps(window, {'s1': 'material'}, [{'ref': 'n1'}]),
        })
    manual = _card('c1', 'Manual', 'text', manual=True)
    ctx3 = _context(window, [{**manual, 'evidence': []}])
    # A model-guessed/forbidden retire of a manual card must still fail.
    with pytest.raises(ValueError):
        delta_contract.compile_plan(ctx3, {
            'changes': [{'ref': 'n1', 'action': 'retire', 'source_refs': ['s1'], 'reason': 'x', 'target': 'c1'}],
            'dispositions': _disps(window, {'s1': 'material'}, [{'ref': 'n1'}]),
        })


def test_repeated_positions_share_one_change_without_discarding():
    window = _window([_part(0, 'Dup'), _part(1, 'Dup')])
    ctx = _context(window, [])
    refs = _spans(window)
    assert len(refs) >= 2
    # A single change may legitimately cite both identical distinct positions;
    # both owned responsibilities stay recorded, neither is discarded.
    change = {'ref': 'n1', 'action': 'create', 'source_refs': [refs[0], refs[1]],
              'reason': 'one durable fact repeated', 'title': 'A', 'topic': 'a', 'body': 'a'}
    disps = [
        {'source_refs': [refs[0]], 'decision': 'material', 'change_refs': ['n1'], 'card_refs': [], 'reason': 'first position material'},
        {'source_refs': [refs[1]], 'decision': 'material', 'change_refs': ['n1'], 'card_refs': [], 'reason': 'second position material'},
    ]
    plan = delta_contract.compile_plan(ctx, {'changes': [change], 'dispositions': disps})
    assert len(plan['operations']) == 1
    assert [d['decision'] for d in plan['dispositions']] == ['material', 'material']
    # One shared create still dedupes the identical evidence to a single quote.
    assert len(plan['operations'][0]['evidence']) == 1


def test_recorded_conflict_is_a_fact_not_a_resolution_demand():
    # A known unresolved business disagreement is itself recordable: the card is
    # a legal target and the plan records unresolved without resolving it.
    window = _window([_part(0, '团队对截止日期仍有分歧。')])
    conflict = _card('c1', 'Deadline', 'body', state='conflict')
    ctx = _context(window, [{**conflict, 'evidence': []}])
    schema = _plan_schema(ctx)
    assert _target_enum(schema, 'update') == ['c1']
    assert _target_enum(schema, 'retire') == ['c1']
    plan = delta_contract.compile_plan(ctx, {
        'changes': [],
        'dispositions': [{'source_refs': ['s1'], 'decision': 'unresolved', 'change_refs': [], 'card_refs': [],
                          'reason': '记录为已知未决分歧，本轮不裁决'}],
    })
    assert plan['unresolved'] is True
    assert plan['operations'] == []


def _dispo_arms(schema):
    return schema['properties']['dispositions']['items']['oneOf']


def _dispo_arm(schema, decision):
    for variant in _dispo_arms(schema):
        if variant['properties']['decision']['const'] == decision:
            return variant
    return None


DISPO_KEYS = ['source_refs', 'decision', 'change_refs', 'card_refs', 'reason']


def test_disposition_schema_has_exact_action_arms():
    window = _window([_part(0, 'A durable note.')])
    card = _card('c1', 'Fact', 'Existing body.')
    ctx = _context(window, [{**card, 'evidence': []}])
    assert ctx['cards'][0]['snapshot']['segments']
    schema = _plan_schema(ctx)
    assert [v['title'] for v in _dispo_arms(schema)] == ['material', 'covered', 'nonmaterial', 'unresolved']
    for variant in _dispo_arms(schema):
        assert variant['type'] == 'object'
        assert variant['additionalProperties'] is False
        assert list(variant['required']) == DISPO_KEYS
        assert set(variant['properties'].keys()) == set(DISPO_KEYS)
    material = _dispo_arm(schema, 'material')
    assert material['properties']['change_refs']['minItems'] == 1
    assert material['properties']['card_refs']['maxItems'] == 0
    covered = _dispo_arm(schema, 'covered')
    assert covered['properties']['card_refs']['minItems'] == 1
    assert covered['properties']['change_refs']['maxItems'] == 0
    for decision in ('nonmaterial', 'unresolved'):
        arm = _dispo_arm(schema, decision)
        assert arm['properties']['change_refs']['maxItems'] == 0
        assert arm['properties']['card_refs']['maxItems'] == 0


def test_disposition_schema_card_refs_active_only_and_excludes_empty_covered():
    window = _window([_part(0, 'A durable note.')])
    active = _card('c1', 'Active', 'Active body.')
    retired = _card('c2', 'Old', 'Old body.', state='retired')
    ctx = _context(window, [{**active, 'evidence': []}, {**retired, 'evidence': []}])
    assert ctx['cards'][0]['snapshot']['segments']
    assert ctx['cards'][1]['snapshot']['segments']
    covered = _dispo_arm(_plan_schema(ctx), 'covered')
    enum = covered['properties']['card_refs']['items']['enum']
    assert enum == ['c1g1']
    assert 'c2g1' not in enum
    # No active card segment anywhere: the covered arm must vanish, never an empty enum.
    ctx2 = _context(window, [{**retired, 'evidence': []}])
    schema2 = _plan_schema(ctx2)
    assert [v['title'] for v in _dispo_arms(schema2)] == ['material', 'nonmaterial', 'unresolved']
    assert _dispo_arm(schema2, 'covered') is None


def test_strict_review_fail_with_self_correcting_prose_still_rejected():
    # The reason field may only carry the final conclusion; prose that reverses
    # itself cannot turn a fail into an approval.
    ctx, change, disps = _basic()
    plan = delta_contract.compile_plan(ctx, {'changes': [change], 'dispositions': disps})
    review = _review(plan)
    review['checks'][0]['N'] = 'fail'
    review['checks'][0]['reason'] = 'initial doubt resolved on complete reading; N should be pass'
    with pytest.raises(ValueError, match='semantic review not approved'):
        delta_contract.approve_review(ctx, plan, review)
    review['checks'][0]['N'] = 'unknown'
    with pytest.raises(ValueError, match='semantic review not approved'):
        delta_contract.approve_review(ctx, plan, review)
