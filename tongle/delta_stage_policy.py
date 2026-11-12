"""Opt-in bounded stage-local repair; legacy execution is deliberately separate.

The existing executor owns dispatch, storage and publishing. This module only
binds per-stage feedback and validates its finite transition chain.
"""
import copy

POLICY = 'stage-local-v1'
AUDIT = 'delta_stage_policy_v1'
IDENTITY_AUDIT = 'delta_execution_policy_v1'
RECEIPT_SCHEMA = 3
STAGES = ('delta_plan', 'delta_review')


def enabled(bundle):
    from .evolution import StageLimit
    if 'execution_policy' not in bundle:
        return False
    if bundle['execution_policy'] != POLICY:
        raise StageLimit('unknown execution policy')
    return True


def require_identity(evo, work_id, bundle):
    """A progressed policy task cannot downgrade on any recovery entry point."""
    from .evolution import StageLimit
    import json
    active = enabled(bundle) if isinstance(bundle, dict) else False
    rows = evo.db.execute('SELECT error FROM knowledge_failures WHERE work_id=? AND state=?',
                          (work_id, IDENTITY_AUDIT)).fetchall()
    if rows:
        try:
            identities = [json.loads(r['error']) for r in rows]
        except (TypeError, ValueError) as exc:
            raise StageLimit('execution policy identity malformed') from exc
        if identities != [{'work_id': work_id, 'execution_policy': POLICY}]:
            raise StageLimit('execution policy identity mismatch')
    if not active and (rows or evo.db.execute(
            'SELECT 1 FROM knowledge_failures WHERE work_id=? AND state=?',
            (work_id, AUDIT)).fetchone()):
        raise StageLimit('execution policy missing from progressed bundle')
    return active


def freeze_identity(evo, work_id, bundle):
    # Caller holds the existing live-lease checked bundle transaction. The
    # identity survives transport failures and lost caches without carrying
    # mutable context hashes, so zero-progress context refresh remains valid.
    from . import delta_execution as ex
    import time
    if not require_identity(evo, work_id, bundle):
        return
    if not evo.db.execute('SELECT 1 FROM knowledge_failures WHERE work_id=? AND state=?',
                           (work_id, IDENTITY_AUDIT)).fetchone():
        evo.db.execute('INSERT INTO knowledge_failures(work_id,state,error,created_at) VALUES(?,?,?,?)',
                       (work_id, IDENTITY_AUDIT,
                        ex._canon({'work_id': work_id, 'execution_policy': POLICY}), time.time()))


def initialize(bundle, cfg, project):
    from .evolution import StageLimit
    value = cfg.get('knowledge_execution_policy')
    if value is None:
        return
    if value != POLICY:
        raise StageLimit('unknown execution policy')
    projects = cfg.get('knowledge_execution_projects', [])
    if not isinstance(projects, list) or any(not isinstance(p, str) or not p for p in projects):
        raise StageLimit('execution project allowlist malformed')
    if project not in projects:
        return
    bundle.update(execution_policy=POLICY, plan_feedback=None, review_feedback=None)


def shape(bundle):
    from .evolution import StageLimit
    history = bundle.get('history')
    if (not isinstance(history, list) or len(history) > 2
            or type(bundle.get('revision')) is not int
            or bundle['revision'] != len(history)
            or bundle.get('repair_feedback') is not None):
        raise StageLimit('stage policy state malformed')
    for key in ('plan_feedback', 'review_feedback'):
        if key not in bundle or (bundle[key] is not None and not isinstance(bundle[key], dict)):
            raise StageLimit('stage feedback malformed')
    if not history and (bundle['plan_feedback'] is not None or bundle['review_feedback'] is not None):
        raise StageLimit('stage feedback without history')


def feedback(bundle, stage):
    return bundle.get('plan_feedback' if stage == 'delta_plan' else 'review_feedback')


def payload(bundle, ctx, stage, plan=None):
    from . import delta_execution as ex
    version = ex._bundle_version(bundle)
    if stage == 'delta_plan':
        return ex._plan_payload_v(ctx, feedback(bundle, stage), version)
    return ex._review_payload_v(ctx, plan, feedback(bundle, stage), version)


def prior_hash(ctx, bundle, steps):
    from . import delta_execution as ex
    step = steps.get('delta_plan')
    if not step:
        return None
    try:
        return ex._compile_plan(ctx, step['response'], ex._bundle_version(bundle))['plan_hash']
    except ValueError:
        return None


def attempt_meta(bundle, ctx, stage):
    from . import delta_execution as ex
    if stage == 'delta_review':
        predecessor = prior_hash(ctx, bundle, bundle['steps'])
    else:
        history = bundle.get('history') or []
        predecessor = history[-1]['prior_plan_hash'] if history else None
    return {'execution_policy': POLICY,
            'feedback_sha': ex._hash(feedback(bundle, stage)),
            'predecessor_plan_hash': predecessor}


def audit(evo, work_id, bundle, stage, step):
    from . import delta_execution as ex
    import time
    obj = dict(attempt_meta(bundle, bundle['context'], stage),
               work_id=work_id, stage=stage, call_id=step['call_id'],
               binding=bundle['binding'], payload_hash=step['payload_hash'],
               response_hash=step['response_hash'])
    evo.db.execute('INSERT INTO knowledge_failures(work_id,state,error,created_at) VALUES(?,?,?,?)',
                   (work_id, AUDIT, ex._canon(obj), time.time()))


def verify_attempt(evo, work_id, bundle, ctx, stage, step):
    from . import delta_execution as ex
    import json
    expected = dict(attempt_meta(bundle, ctx, stage), work_id=work_id,
                    stage=stage, call_id=step['call_id'], binding=bundle['binding'],
                    payload_hash=step['payload_hash'], response_hash=step['response_hash'])
    found = []
    for row in evo.db.execute('SELECT error FROM knowledge_failures WHERE work_id=? AND state=?',
                              (work_id, AUDIT)):
        obj = json.loads(row['error'])
        if obj.get('call_id') == step['call_id']:
            found.append(obj)
    if found != [expected]:
        raise ex.StageLimit('stage policy audit mismatch')



def dispatch_counts(evo, work_id):
    """Every reserved ledger dispatch consumes a slot, including provider errors.

    This policy intentionally differs from legacy semantic-attempt accounting:
    settled transport failures remain auditable and cannot buy extra requests.
    Local preflight failures recorded in the ledger count conservatively too.
    """
    from .evolution import StageLimit
    counts = {stage: 0 for stage in STAGES}
    for row in evo.db.execute('SELECT stage,COUNT(*) AS n FROM knowledge_calls WHERE work_id=? GROUP BY stage',
                              (work_id,)):
        if row['stage'] not in counts:
            raise StageLimit('unexpected stage in bounded dispatch ledger')
        counts[row['stage']] = row['n']
    if sum(counts.values()) > 4 or any(n > 2 for n in counts.values()):
        raise StageLimit('actual dispatch cap exceeded')
    return counts


def before_dispatch(evo, work_id, stage):
    from .evolution import StageLimit
    counts = dispatch_counts(evo, work_id)
    if evo.db.execute('SELECT 1 FROM knowledge_calls WHERE work_id=? AND elapsed IS NULL',
                      (work_id,)).fetchone():
        raise StageLimit('unproven inflight stage call')
    if stage not in counts or counts[stage] >= 2 or sum(counts.values()) >= 4:
        raise StageLimit('actual dispatch cap reached')


def validate(evo, work_id, ctx, bundle):
    """Reconstruct every failed snapshot, exact payload and feedback transition."""
    from . import delta_execution as ex
    shape(bundle)
    require_identity(evo, work_id, bundle)
    dispatch_counts(evo, work_id)
    pf = rf = None
    required_plan = None
    calls = {}
    history = bundle['history']
    for index in range(len(history) + 1):
        entry = history[index] if index < len(history) else bundle
        if not isinstance(entry, dict) or entry.get('plan_feedback') != pf or entry.get('review_feedback') != rf:
            raise ex.StageLimit('stage feedback chain mismatch')
        steps = entry.get('steps')
        if not isinstance(steps, dict) or set(steps) - set(STAGES):
            raise ex.StageLimit('stage history steps malformed')
        if required_plan is not None and steps.get('delta_plan') != required_plan:
            raise ex.StageLimit('review repair changed plan step')
        view = dict(bundle, steps=steps, plan_feedback=pf, review_feedback=rf,
                    history=history[:index])
        for stage in STAGES:
            if stage not in steps:
                continue
            plan = None
            if stage == 'delta_review':
                if 'delta_plan' not in steps:
                    raise ex.StageLimit('review without plan')
                plan = ex._compile_plan(ctx, steps['delta_plan']['response'], ex._bundle_version(bundle))
            ex._verify_step(evo, work_id, stage, bundle, ex._hash(payload(view, ctx, stage, plan)), steps)
            # A reused plan retains its ORIGINAL metadata, not the later
            # review-only transition's predecessor.
            cid = steps[stage]['call_id']
            if cid not in calls:
                verify_attempt(evo, work_id, view, ctx, stage, steps[stage])
                calls[cid] = (stage, copy.deepcopy(steps[stage]))
            elif calls[cid] != (stage, steps[stage]):
                raise ex.StageLimit('stage call reused with different bytes')
        if index == len(history):
            break
        derived = ex._recompute_repair(evo, ctx, steps, ex._bundle_version(bundle))
        if entry.get('feedback') != derived or entry.get('prior_plan_hash') != prior_hash(ctx, bundle, steps):
            raise ex.StageLimit('stage repair feedback mismatch')
        if derived.get('target') == ex.delta_versions.REVIEW_FORMAT_TARGET:
            rf = derived
            required_plan = steps['delta_plan']
        else:
            pf, rf, required_plan = derived, None, None
    ledger = evo.db.execute(
        'SELECT id,stage FROM knowledge_calls WHERE work_id=? AND (error IS NULL OR elapsed IS NULL)',
        (work_id,)).fetchall()
    if {(r['id'], r['stage']) for r in ledger} != {(cid, value[0]) for cid, value in calls.items()}:
        raise ex.StageLimit('unproven settled or inflight stage call')
    for stage in STAGES:
        if sum(s == stage for s, _ in calls.values()) > 2:
            raise ex.StageLimit('stage history call cap exceeded')
        if ex._ledger_count(evo, work_id, stage) > 2:
            raise ex.StageLimit('stage ledger call cap exceeded')


def try_repair(evo, work_id, token, ctx, cfg, factory, project, bundle, failure):
    from . import delta_execution as ex
    if failure is None or failure['kind'] == 'operations':
        return False
    validate(evo, work_id, ctx, bundle)
    review_only = failure.get('target') == ex.delta_versions.REVIEW_FORMAT_TARGET
    counts = dispatch_counts(evo, work_id)
    if counts['delta_review' if review_only else 'delta_plan'] >= 2:
        return False
    if not review_only and counts['delta_review'] >= 2:
        return False
    if len(bundle['history']) >= 2 or not bundle['steps'].get('delta_plan'):
        return False
    derived = ex._recompute_repair(evo, ctx, bundle['steps'], ex._bundle_version(bundle))
    expected = ex._make_feedback(failure['kind'], failure['label'], failure['draft'])
    if review_only:
        expected['target'] = ex.delta_versions.REVIEW_FORMAT_TARGET
    if expected != derived:
        raise ex.StageLimit('stage repair cause mismatch')
    updated = copy.deepcopy(bundle)
    updated['history'].append({
        'steps': copy.deepcopy(bundle['steps']),
        'plan_feedback': copy.deepcopy(bundle['plan_feedback']),
        'review_feedback': copy.deepcopy(bundle['review_feedback']),
        'feedback': derived, 'prior_plan_hash': prior_hash(ctx, bundle, bundle['steps'])})
    updated['revision'] = len(updated['history'])
    if review_only:
        updated['review_feedback'] = derived
        updated['steps'] = {'delta_plan': copy.deepcopy(bundle['steps']['delta_plan'])}
    else:
        updated['plan_feedback'] = derived
        updated['review_feedback'] = None
        updated['steps'] = {}
    with evo.engine.locked():
        if ex._load_bundle(evo, work_id) != bundle:
            raise ex.StageLimit('bundle changed before stage repair')
        ex._persist_bundle(evo, work_id, token, updated, factory, project)
    bundle.clear()
    bundle.update(updated)
    return True


def check_unchanged(ctx, bundle, plan):
    """A semantic plan repair must not resubmit exactly the rejected plan."""
    from . import delta_execution as ex
    if not bundle['history']:
        return
    entry = bundle['history'][-1]
    if entry['feedback'].get('target') == ex.delta_versions.REVIEW_FORMAT_TARGET:
        return
    old = prior_hash(ctx, bundle, entry['steps'])
    if old is not None and old == plan['plan_hash']:
        raise ex.StageLimit('unchanged plan')


def receipt_fields(evo, work_id, bundle):
    from . import delta_execution as ex
    from . import delta_receipts as dr
    validate(evo, work_id, bundle['context'], bundle)
    attempts, seen = [], set()
    history = bundle['history']
    for index, entry in enumerate(history + [bundle]):
        view = dict(bundle, steps=entry['steps'], history=history[:index],
                    plan_feedback=entry['plan_feedback'], review_feedback=entry['review_feedback'])
        for stage in STAGES:
            step = entry['steps'].get(stage)
            if not step or step['call_id'] in seen:
                continue
            seen.add(step['call_id'])
            attempts.append(dict(dr._proof(evo, work_id, stage, bundle['binding'], step),
                                 **attempt_meta(view, bundle['context'], stage)))
    transitions = []
    for entry in history:
        fb = entry['feedback']
        failing = 'delta_review' if 'delta_review' in entry['steps'] else 'delta_plan'
        transitions.append({'kind': fb['kind'], 'target': fb.get('target'),
                            'feedback_sha': ex._hash(fb),
                            'prior_plan_hash': entry['prior_plan_hash'],
                            'failed_call_id': entry['steps'][failing]['call_id']})
    return {'execution_policy': POLICY, 'attempts': attempts,
            'transitions': transitions, 'contract_version': ex._bundle_version(bundle)}


def verify_receipt(evo, work_id, receipt):
    """Verify body-free dispatch chain after large bundle reclamation."""
    from . import delta_receipts as dr
    from . import delta_versions
    import json
    if (receipt.get('execution_policy') != POLICY or type(receipt.get('schema')) is not int
            or receipt.get('schema') != RECEIPT_SCHEMA):
        raise ValueError('stage policy receipt mismatch')
    delta_versions.resolve_version(bundle={'contract_version': receipt.get('contract_version')})
    from .evolution import StageLimit
    try:
        dispatch_counts(evo, work_id)
    except StageLimit as exc:
        raise ValueError(str(exc)) from exc
    attempts, transitions = receipt.get('attempts'), receipt.get('transitions')
    if not isinstance(attempts, list) or not 2 <= len(attempts) <= 4:
        raise ValueError('stage attempts malformed')
    if (not isinstance(transitions, list) or len(transitions) > 2
            or type(receipt.get('revision')) is not int or receipt['revision'] != len(transitions)
            or receipt.get('repair') is not None or receipt.get('history_stages') != []):
        raise ValueError('stage transitions malformed')
    ids, counts, current_plan = set(), {s: 0 for s in STAGES}, None
    pf = rf = dr._hash(None)
    prior_plan = None
    pending = None
    remaining = list(transitions)
    final = {}
    for attempt in attempts:
        if not isinstance(attempt, dict):
            raise ValueError('stage attempt malformed')
        stage, cid = attempt.get('stage'), attempt.get('call_id')
        if stage not in STAGES or cid in ids:
            raise ValueError('stage attempt repeated')
        if pending is not None:
            if stage != pending:
                raise ValueError('stage repair target mismatch')
            pending = None
        elif stage == 'delta_plan' and current_plan is not None:
            raise ValueError('unjustified plan retry')
        elif stage == 'delta_review' and 'delta_review' in final:
            raise ValueError('unjustified review retry')
        if attempt.get('binding') != receipt.get('binding') or attempt.get('execution_policy') != POLICY:
            raise ValueError('stage attempt binding mismatch')
        for field in ('payload_hash', 'response_hash', 'feedback_sha'):
            if not dr._hex(attempt.get(field)):
                raise ValueError('stage attempt digest malformed')
        pred = attempt.get('predecessor_plan_hash')
        if pred is not None and not dr._hex(pred):
            raise ValueError('stage predecessor malformed')
        if attempt.get('feedback_sha') != (pf if stage == 'delta_plan' else rf):
            raise ValueError('stage feedback chain mismatch')
        if stage == 'delta_plan':
            if pred != prior_plan:
                raise ValueError('plan predecessor mismatch')
            current_plan = cid
            final.pop('delta_review', None)
        elif current_plan is None or pred is None:
            raise ValueError('review predecessor missing')
        ids.add(cid)
        counts[stage] += 1
        if counts[stage] > 2:
            raise ValueError('stage attempt cap exceeded')
        dr._require_stage(evo, work_id, attempt)
        expected = dict(attempt, work_id=work_id)
        matches = []
        for row in evo.db.execute('SELECT error FROM knowledge_failures WHERE work_id=? AND state=?',
                                  (work_id, AUDIT)):
            obj = json.loads(row['error'])
            if obj.get('call_id') == cid:
                matches.append(obj)
        if matches != [expected]:
            raise ValueError('stage policy audit mismatch')
        final[stage] = {k: attempt[k] for k in ('stage','call_id','binding','payload_hash','response_hash')}
        if remaining and remaining[0].get('failed_call_id') == cid:
            tr = remaining.pop(0)
            if tr.get('kind') not in ('plan', 'unresolved', 'review') or not dr._hex(tr.get('feedback_sha')):
                raise ValueError('stage transition malformed')
            predecessor = tr.get('prior_plan_hash')
            if predecessor is not None and not dr._hex(predecessor):
                raise ValueError('transition predecessor malformed')
            if tr.get('target') == 'review_response':
                if stage != 'delta_review' or tr['kind'] != 'review' or predecessor != pred:
                    raise ValueError('review transition mismatch')
                rf, pending = tr['feedback_sha'], 'delta_review'
            elif tr.get('target') is None:
                if (stage == 'delta_review') != (tr['kind'] == 'review'):
                    raise ValueError('plan transition mismatch')
                if stage == 'delta_review' and predecessor != pred:
                    raise ValueError('semantic predecessor mismatch')
                pf, rf, prior_plan = tr['feedback_sha'], dr._hash(None), predecessor
                pending = 'delta_plan'
            else:
                raise ValueError('unknown repair target')
    if remaining or pending is not None or set(final) != set(STAGES):
        raise ValueError('stage transition chain incomplete')
    if receipt.get('stages') != [final[s] for s in STAGES]:
        raise ValueError('final stage identity mismatch')
    if attempts[-1]['stage'] != 'delta_review' or attempts[-1]['predecessor_plan_hash'] != receipt.get('plan_hash'):
        raise ValueError('final review plan mismatch')
    if attempts[-1]['response_hash'] != receipt.get('final_response_hash'):
        raise ValueError('final review response mismatch')
    ledger = evo.db.execute('SELECT id,stage FROM knowledge_calls WHERE work_id=? AND (error IS NULL OR elapsed IS NULL)', (work_id,)).fetchall()
    if {(r['id'], r['stage']) for r in ledger} != {(a['call_id'], a['stage']) for a in attempts}:
        raise ValueError('stage receipt ledger coverage mismatch')
