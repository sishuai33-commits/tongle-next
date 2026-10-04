"""Scoped ROOT-review tests for the delta wire presentation boundary.

Synthetic fixtures only: no model, network, vault, credentials or production
asset is touched. These tests pin three narrow corrections in
``tongle.delta_contract``:

* the model wire presents OWNED responsibilities under ``sources`` and the
  read-only prior context under ``reference_context`` (complete, explicitly
  read-only), with every part keeping its ORIGINAL ``pN``/``sN`` alias and its
  full untruncated text -- no renumbering, no truncation, no duplicated part;
* both payloads enumerate ``owned_source_refs``; dispositions stay owned-only
  and a reference span can never close (or open) an owned duty;
* newly generated create title/topic/body and update edit text may not
  introduce an internal host alias (``sN``/``cN``/``cNgN`` or the legacy
  ``srcNNN``/``cardNNN``/``refNNN``/``receiptNNN`` tokens) as prose, unless the
  exact token is a genuine literal in the supplied original source (or the
  specific original target card), and genuine literals/embedded substrings are
  never falsely rejected.
"""
import json

import pytest

from tongle import delta_contract, source_windows


def _part(part, text, responsibility='owned', role='user', kind='message'):
    return {
        'part': part,
        'source_id': 'src',
        'version': 1,
        'role': role,
        'kind': kind,
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


def _owned_disps(window, decision_by_ref, changes):
    out = []
    n = 0
    for part in window['parts']:
        for _span in part['span_ids']:
            n += 1
            ref = f's{n}'
            if part.get('responsibility') == 'reference':
                continue
            decision = decision_by_ref.get(ref, 'nonmaterial')
            out.append({
                'source_refs': [ref],
                'decision': decision,
                'change_refs': [c['ref'] for c in changes] if decision == 'material' else [],
                'card_refs': [],
                'reason': 'specific reason for ' + ref,
            })
    return out


def _review(plan, manual_targets=()):
    checks = [{
        'index': i, 'E': 'pass', 'Q': 'pass', 'T': 'pass', 'N': 'pass',
        'reason': 'checked evidence and coverage',
    } for i in range(len(plan['dispositions']))]
    human = [{'target': t, 'preserved': True, 'reason': 'manual preserved'} for t in manual_targets]
    return {
        'checks': checks,
        'whole_window': {'status': 'pass', 'reason': 'whole window reviewed'},
        'human_preservation': human,
    }


def _spans_by_part(window):
    """Return [(part_index, responsibility, [ref, ...]), ...] in window order."""
    out = []
    n = 0
    for part in window['parts']:
        refs = []
        for _span in part['span_ids']:
            n += 1
            refs.append(f's{n}')
        out.append((part.get('responsibility'), refs))
    return out


# --------------------------------------------------------------- partition

def test_plan_payload_partitions_reference_first_with_unchanged_refs():
    # A LONG prior reference event is placed BEFORE the owned event, exactly the
    # shape that used to push the model to dispose of the reference span.
    window = _window([
        _part(0, 'REFERENCE-CONTEXT-FULL-TEXT ' + ('r' * 300), responsibility='reference'),
        _part(1, 'OWNED-EVENT-FIRST ' + ('o' * 50) + '\n\nOWNED-EVENT-SECOND ' + ('p' * 50)),
    ])
    ctx = _context(window, [])
    payload = delta_contract.plan_payload(ctx)

    refs = _spans_by_part(window)
    assert refs[0][0] == 'reference' and len(refs[0][1]) == 1
    assert refs[1][0] == 'owned' and len(refs[1][1]) == 2

    # Only the owned part is presented as a responsibility-bearing source.
    assert [p['responsibility'] for p in payload['sources']] == ['owned']
    assert payload['sources'][0]['part_ref'] == 'p2'
    assert payload['sources'][0]['spans'][0]['ref'] == 's2'
    assert payload['sources'][0]['spans'][1]['ref'] == 's3'
    assert 'OWNED-EVENT-FIRST' in payload['sources'][0]['spans'][0]['text']
    assert 'OWNED-EVENT-SECOND' in payload['sources'][0]['spans'][1]['text']

    # The reference part is complete and explicitly read-only, alias unchanged.
    assert [p['responsibility'] for p in payload['reference_context']] == ['reference']
    assert payload['reference_context'][0]['part_ref'] == 'p1'
    assert payload['reference_context'][0]['spans'][0]['ref'] == 's1'
    assert payload['reference_context'][0]['spans'][0]['text'] == window['spans'][0]['text']
    assert len(payload['reference_context'][0]['spans'][0]['text']) > 300

    # Exact lists and identities, no duplication, no renumbering.
    assert payload['owned_source_refs'] == ['s2', 's3']
    assert payload['reference_source_refs'] == ['s1']
    presented = payload['sources'] + payload['reference_context']
    assert len(presented) == len(window['parts'])
    all_refs = sorted(s['ref'] for p in presented for s in p['spans'])
    assert all_refs == ['s1', 's2', 's3']

    # The pure helper still returns every part for plain contract tests.
    everything = delta_contract._source_payload(window)
    assert [p['part_ref'] for p in everything] == ['p1', 'p2']


def test_review_payload_partitions_and_enumerates_owned_refs():
    window = _window([
        _part(0, 'ref context', responsibility='reference'),
        _part(1, 'owned durable note'),
    ])
    ctx = _context(window, [])
    change = {
        'ref': 'n1', 'action': 'create', 'source_refs': ['s2'],
        'reason': 'durable fact', 'title': 'Title', 'topic': 'topic',
        'body': 'Owned durable note body.',
    }
    disps = _owned_disps(window, {'s2': 'material'}, [change])
    plan = delta_contract.compile_plan(ctx, {'changes': [change], 'dispositions': disps})
    payload = delta_contract.review_payload(ctx, plan)

    assert payload['owned_source_refs'] == ['s2']
    assert payload['reference_source_refs'] == ['s1']
    assert [p['responsibility'] for p in payload['sources']] == ['owned']
    assert [p['responsibility'] for p in payload['reference_context']] == ['reference']
    assert payload['reference_context'][0]['spans'][0]['ref'] == 's1'
    assert payload['sources'][0]['spans'][0]['ref'] == 's2'
    json.dumps(payload, sort_keys=True, allow_nan=False)


def test_compile_still_rejects_reference_disposition():
    window = _window([_part(0, 'reference only', responsibility='reference')])
    ctx = _context(window, [])
    # A reference span may be cited as interpretation/evidence in a change but
    # must never be dispositioned: it carries no owned responsibility.
    with pytest.raises(ValueError):
        delta_contract.compile_plan(ctx, {
            'changes': [{
                'ref': 'n1', 'action': 'create', 'source_refs': ['s1'],
                'reason': 'reason', 'title': 't', 'topic': 'q', 'body': 'b',
            }],
            'dispositions': [{
                'source_refs': ['s1'], 'decision': 'material',
                'change_refs': ['n1'], 'card_refs': [], 'reason': 'claimed duty',
            }],
        })


# ------------------------------------------------------------ alias guard

def test_guard_rejects_host_alias_prose_in_create_fields():
    window = _window([_part(0, '真实业务来源文本。')])
    ctx = _context(window, [])

    def _compile(body, title='Title', topic='topic'):
        change = {
            'ref': 'n1', 'action': 'create', 'source_refs': ['s1'],
            'reason': 'durable fact', 'title': title, 'topic': topic, 'body': body,
        }
        return delta_contract.compile_plan(ctx, {
            'changes': [change],
            'dispositions': _owned_disps(window, {'s1': 'material'}, [change]),
        })

    # Only-host-alias prose is rejected in body, title and topic.
    for body in ('结论见 s9。', '见 card003 与 c1g1。', '参考 src001/receipt002。'):
        with pytest.raises(ValueError):
            _compile(body)
    with pytest.raises(ValueError):
        _compile('clean body.', title='标题 c1')
    with pytest.raises(ValueError):
        _compile('clean body.', topic='topic s14')


def test_guard_allows_embedded_substrings_and_exact_literals():
    window = _window([_part(0, '产品 S14 规格：见 abcS14x 示例。')])
    ctx = _context(window, [])
    change = {
        'ref': 'n1', 'action': 'create', 'source_refs': ['s1'],
        'reason': 'durable fact', 'title': 'S14 规格标题', 'topic': 'S14',
        'body': '见 abcS14x 示例，S14 是真实业务标识。',
    }
    plan = delta_contract.compile_plan(ctx, {
        'changes': [change],
        'dispositions': _owned_disps(window, {'s1': 'material'}, [change]),
    })
    assert plan['operations'][0]['op'] == 'create'
    assert plan['operations'][0]['title'] == 'S14 规格标题'


def test_guard_allows_literal_segment_alias_present_in_source():
    # If the supplied original source genuinely contains ``g1``-bearing text,
    # the literal is real content and must not be rejected.
    window = _window([_part(0, '合同编号 c12g3 在原文出现。')])
    ctx = _context(window, [])
    change = {
        'ref': 'n1', 'action': 'create', 'source_refs': ['s1'],
        'reason': 'durable fact', 'title': 'Title', 'topic': 'topic',
        'body': '合同编号 c12g3 已确认。',
    }
    plan = delta_contract.compile_plan(ctx, {
        'changes': [change],
        'dispositions': _owned_disps(window, {'s1': 'material'}, [change]),
    })
    assert plan['operations'][0]['op'] == 'create'


def test_guard_rejects_alias_in_update_edit_but_allows_target_literal():
    window = _window([_part(0, '新增说明。')])
    card = _card('c1', 'Target', '正文包含真实标识 S14。', manual=False)
    ctx = _context(window, [{**card, 'evidence': []}])
    # The target card literal ``S14`` is a valid allow-list literal.
    ok_change = {
        'ref': 'n1', 'action': 'update', 'source_refs': ['s1'],
        'reason': 'append detail', 'target': 'c1',
        'edits': [{'op': 'insert_after', 'segment': 'c1g1', 'text': '补充 S14 说明。'}],
    }
    plan = delta_contract.compile_plan(ctx, {
        'changes': [ok_change],
        'dispositions': _owned_disps(window, {'s1': 'material'}, [ok_change]),
    })
    assert plan['operations'][0]['op'] == 'update'
    # But a host alias that is neither in the source nor the target card fails.
    bad_change = {
        'ref': 'n1', 'action': 'update', 'source_refs': ['s1'],
        'reason': 'append detail', 'target': 'c1',
        'edits': [{'op': 'insert_after', 'segment': 'c1g1', 'text': '见 c1 的说明。'}],
    }
    with pytest.raises(ValueError):
        delta_contract.compile_plan(ctx, {
            'changes': [bad_change],
            'dispositions': _owned_disps(window, {'s1': 'material'}, [bad_change]),
        })


def test_guard_case_insensitive_match_but_case_exact_literal_check():
    window = _window([_part(0, '业务文本不含别名。')])
    ctx = _context(window, [])
    # Upper-case S14 is still considered, and without a matching literal it is
    # rejected as an alias rather than trusted.
    change = {
        'ref': 'n1', 'action': 'create', 'source_refs': ['s1'],
        'reason': 'durable fact', 'title': 'Title', 'topic': 'topic',
        'body': '见 S14。',
    }
    with pytest.raises(ValueError):
        delta_contract.compile_plan(ctx, {
            'changes': [change],
            'dispositions': _owned_disps(window, {'s1': 'material'}, [change]),
        })


# ------------------------------------------------------ no approval widening

def test_fail_and_unknown_states_still_block_approval():
    window = _window([_part(0, 'durable note')])
    ctx = _context(window, [])
    change = {
        'ref': 'n1', 'action': 'create', 'source_refs': ['s1'],
        'reason': 'durable fact', 'title': 'Title', 'topic': 'topic', 'body': 'Body.',
    }
    plan = delta_contract.compile_plan(ctx, {
        'changes': [change],
        'dispositions': _owned_disps(window, {'s1': 'material'}, [change]),
    })
    for letter, state in (('E', 'fail'), ('Q', 'unknown'), ('T', 'fail'), ('N', 'unknown')):
        review = _review(plan)
        review['checks'][0][letter] = state
        with pytest.raises(ValueError, match='semantic review not approved'):
            delta_contract.approve_review(ctx, plan, review)
    # An unmodified all-pass review still approves.
    approved = delta_contract.approve_review(ctx, plan, _review(plan))
    assert approved['approved'] is True


def test_guard_is_deterministic_and_does_not_alter_response():
    # The guard only validates; it never rewrites claims or strips tokens.
    window = _window([_part(0, '真实业务来源文本。')])
    ctx = _context(window, [])
    change = {
        'ref': 'n1', 'action': 'create', 'source_refs': ['s1'],
        'reason': 'durable fact', 'title': 'Clean title', 'topic': 'clean',
        'body': 'A clean business body without aliases.',
    }
    response = {'changes': [change], 'dispositions': _owned_disps(window, {'s1': 'material'}, [change])}
    plan = delta_contract.compile_plan(ctx, response)
    assert plan['changes'][0]['body'] == change['body']
    assert plan['changes'][0]['title'] == change['title']
