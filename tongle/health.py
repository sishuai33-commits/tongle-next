"""One bounded host status line, injected only when something needs attention.

Why this exists
---------------
The 2026-10 scheduled backup failed continuously from 10-12 to 10-16. The
maintenance cycle *did* set ``needs_attention``, but nothing ever surfaced it to
the operator: the failure was technically visible and practically invisible.
The same shape appeared on 10-16, when a tripped circuit breaker froze the whole
work queue and every cycle reported ``processed=0`` with ``ok=true``.

So this line is rendered at the top of the injected context whenever a human
would want to look -- and stays absent otherwise. It is deliberately NOT an
always-on banner: a fixed line on every session becomes wallpaper, and the
failure mode being fixed here is invisibility, not absence of reassurance.

It is a HOST observation, not a model judgement and not a knowledge record: no
model call, no classification, no interpretation. It must never raise -- a
missing or malformed state file degrades to the fields still provable rather
than breaking context injection.
"""
import json
import sqlite3
import math
from pathlib import Path
import time

# Report the archive once it is this far through the portable export's bound,
# so growth is visible weeks before the fuse trips rather than after.
SIZE_ATTENTION_RATIO = 0.7

# Rows that should be running right now, yet nothing completes for this long,
# means the loop is not advancing: a breaker is holding it, the provider channel
# is down, or the process died. Legitimately *waiting* is excluded, not counted:
# a row parked on a future ``next_at`` (budget/backoff) is not ready, so an
# overnight budget wait never raises a false alarm.
STALL_HOURS = 6

_READY_STATES = ('pending', 'running', 'retry', 'budget_wait')


def _stamp(epoch):
    if not isinstance(epoch, (int, float)) or isinstance(epoch, bool):
        return None
    try:
        return time.strftime('%m-%d %H:%M', time.localtime(epoch))
    except (ValueError, OverflowError, OSError):
        # NaN / infinity / out of range: an unrenderable timestamp must degrade
        # to "absent", never take the whole status line (or the backup finding
        # riding on it) down with it.
        return None


def _backup_finding(home):
    """A human-readable backup problem, or ``''`` when none is provable."""
    path = home / 'maintenance' / 'backup.json'
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError):
        return ''
    if not isinstance(state, dict):
        return ''
    error = state.get('error')
    if isinstance(error, str) and error.strip():
        since = _stamp(state.get('last_success_at')) or '从未成功'
        return '备份失败(自%s; %s)' % (since, error.strip()[:80])
    verification = state.get('restore_verification')
    if state.get('path') and not (isinstance(verification, dict)
            and verification.get('method') == 'isolated_restore_v1'
            and isinstance(state.get('archive_sha256'), str)
            and verification.get('sha256') == state['archive_sha256']):
        return '备份已导出，恢复尚未验证'
    # Compare the UNCOMPRESSED payload size: that is what MAX_ARCHIVE bounds.
    # ``bytes`` (the written zip, ~1/3 of it) is deliberately not used here --
    # a threshold on it would never fire before the export had already failed.
    size = state.get('content_bytes')
    if isinstance(size, int) and not isinstance(size, bool) and size >= 0:
        # Lazy import: archive imports evolution, which imports this module.
        from .archive import MAX_ARCHIVE
        if size >= MAX_ARCHIVE * SIZE_ATTENTION_RATIO:
            return '备份偏大(%.0fMB/%.0fMB)' % (size / 1048576.0, MAX_ARCHIVE / 1048576.0)
    return ''


def _work_findings(engine):
    """``(unhandled_failed_count, stalled_text)``; empty when not provable.

    A row parked by an explicit triage decision is not an unhandled failure:
    it stays visible in ``scripts/doctor.py`` with its reason and next action,
    but it must not turn the injected status line into permanent wallpaper.
    Fall back to counting every failed row when the triage table is absent.
    """
    try:
        failed = engine.db.execute(
            "SELECT COUNT(*) FROM knowledge_work w WHERE w.state='failed' AND tongle_process(w.project)"
            " AND NOT EXISTS (SELECT 1 FROM knowledge_triage t"
            "                 WHERE t.work_id=w.id AND t.action='parked')").fetchone()[0]
    except sqlite3.Error:
        try:
            failed = engine.db.execute(
                "SELECT COUNT(*) FROM knowledge_work WHERE state='failed' AND tongle_process(project)").fetchone()[0]
        except sqlite3.Error:
            return 0, ''
    stalled = ''
    try:
        now = time.time()
        ready = engine.db.execute(
            "SELECT COUNT(*), MIN(MAX(created_at, COALESCE(next_at, 0)))"
            " FROM knowledge_work WHERE tongle_process(project) AND state IN (%s)"
            " AND (next_at IS NULL OR next_at<=?)"
            % ','.join('?' * len(_READY_STATES)),
            _READY_STATES + (now,)).fetchone()
        last_ok = engine.db.execute(
            'SELECT MAX(created_at) FROM knowledge_calls WHERE error IS NULL AND elapsed IS NOT NULL').fetchone()[0]
        count, ready_since = ready
        if count and isinstance(ready_since, (int, float)):
            # Overnight budget waits are not runnable time. Start the stall
            # clock at eligibility, including installations with no success yet.
            anchor = max(ready_since, last_ok) if isinstance(last_ok, (int, float)) else ready_since
            idle = (now - anchor) / 3600.0
            if idle >= STALL_HOURS:
                stalled = '队列停滞%d项就绪但%.0fh无成功调用' % (int(count), idle)
    except (sqlite3.Error, TypeError, ValueError):
        pass
    return int(failed), stalled


def attention_line(engine):
    """The host status line, or ``''`` when nothing needs attention."""
    try:
        findings = [_backup_finding(engine.home)]
        maintenance = maintenance_snapshot(engine.home)
        if maintenance['state'] == 'stale':
            findings.append('维护心跳已陈旧，当前运行状态需检查')
        if maintenance['last_cycle_status'] == 'failed':
            findings.append('最近维护周期失败(%s)' % ','.join(maintenance['failed_stages']))
        failed, stalled = _work_findings(engine)
        if failed:
            findings.append('未处置失败%d' % failed)
        if stalled:
            findings.append(stalled)
        findings = [f for f in findings if f]
        if not findings:
            return ''
        return '[tongle 主机状态，供判断；非模型结论] ' + ' ｜ '.join(findings)
    except Exception:  # never let a status line break context injection
        return ''


def maintenance_snapshot(home, now=None, stale_after=300):
    """Read existing maintenance files only, including an externally stale heartbeat.

    Fresh bytes are evidence of a heartbeat, not process-liveness proof. An old
    one-shot cycle is stale evidence, not proof that an unconfigured daemon died.
    """
    now = time.time() if now is None else now
    if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
        raise ValueError('invalid observation time')
    if isinstance(stale_after, bool) or not isinstance(stale_after, (int, float)) or not math.isfinite(stale_after) or stale_after <= 0:
        raise ValueError('invalid maintenance freshness bound')
    directory = Path(home) / 'maintenance'
    def read(name):
        try:
            value = json.loads((directory / name).read_text())
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}
    def age(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value > now:
            return None
        return max(0, now - value)
    state = read('state.json')
    cycle = read('last-cycle.json')
    heartbeat_age = age(state.get('heartbeat_at'))
    cycle_age = age(cycle.get('finished_at'))
    status = 'unknown'
    if state.get('running') is True and heartbeat_age is not None:
        status = 'stale' if heartbeat_age > stale_after else 'recent_heartbeat'
    elif state.get('running') is False:
        status = 'stopped'
    errors = cycle.get('errors') if isinstance(cycle.get('errors'), list) else []
    stages = sorted(set(item.get('stage') if isinstance(item, dict) and
        isinstance(item.get('stage'), str) and item['stage'].replace('_', '').isalnum()
        and len(item['stage']) <= 40 else 'unknown' for item in errors))
    cycle_status = 'failed' if cycle.get('ok') is False or errors else 'success' if cycle.get('ok') is True else 'unknown'
    return {'state': status, 'process_alive': 'unknown', 'heartbeat_age_seconds': heartbeat_age,
            'last_cycle_age_seconds': cycle_age, 'last_cycle_status': cycle_status,
            'last_cycle_freshness': 'unknown' if cycle_age is None else 'stale' if cycle_age > stale_after else 'recent',
            'failed_stages': stages or (['unknown'] if cycle_status == 'failed' else []),
            'stale_after_seconds': stale_after, 'observation': 'read-only; timestamps do not prove process liveness'}
