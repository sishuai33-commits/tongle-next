#!/usr/bin/env python3
"""Read-only snapshot for the DSH Tongle console.

Opens the tongle-next SQLite store with ``mode=ro`` and aggregates only
existing runtime/knowledge tables. It never writes to the store or the Vault,
and it does not start maintenance or model work.

Usage:
  python3 -m tongle.console_report [--install /abs/active.json] [--days 14]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DAY = 86400
DEFAULT_DAYS = 14
MAX_RECENT = 8
MAX_ALERTS = 5


def load_install(explicit: str | None) -> tuple[dict, Path]:
    raw = explicit or os.environ.get("TONGLE_NEXT_INSTALL") or str(
        Path.home() / ".config" / "tongle-next" / "active.json"
    )
    cfg = json.loads(Path(raw).read_text(encoding="utf-8"))
    if not cfg.get("enabled"):
        raise ValueError("tongle-next installation is disabled")
    home = Path(str(cfg.get("home") or ""))
    if not home.is_absolute():
        raise ValueError("active.json home must be absolute")
    db = home / "state.sqlite3"
    if not db.is_file():
        raise FileNotFoundError("state database not found")
    return cfg, db


def connect(db: Path) -> sqlite3.Connection:
    uri = db.as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=2)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only = ON")
    return con


def parse_ts(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def iso(value):
    parsed = parse_ts(value)
    return parsed.isoformat() if parsed else None


def day_of(value):
    parsed = parse_ts(value)
    return parsed.date().isoformat() if parsed else None


def epoch_day(value):
    try:
        return datetime.fromtimestamp(float(value) * DAY, timezone.utc).date().isoformat()
    except (OverflowError, OSError, TypeError, ValueError):
        return None


def pct(values, ratio):
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * ratio))))
    return round(ordered[index], 1)


def read_review_status(home, project, session, payload):
    """Use the existing review read contract without Engine or observability writes."""
    from types import SimpleNamespace
    from .review_work import ReviewWork
    # Validate even when the instance or ledger is absent. Only the status
    # operation is admitted; write methods never receive a read-only handle.
    if (not isinstance(payload, dict) or payload.get('op') != 'status' or
            set(payload) - {'op', 'review_id', 'limit', 'include_closed'}):
        raise ValueError('invalid review status fields')
    rid = payload.get('review_id')
    limit = payload.get('limit', 10)
    if rid is not None and (not isinstance(rid, str) or not rid.strip() or len(rid) > 160):
        raise ValueError('review_id must be a bounded nonempty string')
    if type(limit) is not int or not 1 <= limit <= 10 or type(payload.get('include_closed', False)) is not bool:
        raise ValueError('invalid review status options')
    base = {'reviews': None, 'evidence_freshness': 'recorded_checkpoint_only; use check to revalidate',
            'benefit': 'unknown', 'queued_jobs': 0, 'read_only': True}
    db = Path(home).expanduser().absolute() / 'state.sqlite3'
    if db.is_symlink(): raise ValueError('database symlink rejected')
    if not db.is_file(): return dict(base, state='missing', reason='database_missing')
    con = None
    try:
        con = connect(db)
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_work_units'").fetchone():
            return dict(base, state='unsupported', reason='review_ledger_missing')
        result = ReviewWork(SimpleNamespace(db=con)).run(project, session, None, payload)
        if any(not isinstance(row, dict) for row in result['reviews']):
            return dict(base, state='error', reason='invalid_review_record')
        return dict(result, read_only=True, state='available')
    except (sqlite3.Error, json.JSONDecodeError):
        return dict(base, state='error', reason='review_query_failed')
    finally:
        if con is not None: con.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--install")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    args = parser.parse_args(argv)
    days = max(1, min(30, args.days))
    warnings: list[str] = []
    failed_tables = set()

    def value(con, sql, params=()):
        try:
            row = con.execute(sql, params).fetchone()
            return row[0] if row is not None else None
        except sqlite3.Error as exc:
            failed_tables.update(re.findall(r'(?:FROM|JOIN)\s+(\w+)', sql, re.I))
            if len(warnings) < 8:
                warnings.append(f"{sql.split()[0]}: {exc}")
            return None

    def rows(con, sql, params=()):
        try:
            return [dict(row) for row in con.execute(sql, params).fetchall()]
        except sqlite3.Error as exc:
            failed_tables.update(re.findall(r'(?:FROM|JOIN)\s+(\w+)', sql, re.I))
            if len(warnings) < 8:
                warnings.append(f"{sql.split()[0]}: {exc}")
            return []

    try:
        cfg, db = load_install(args.install)
        con = connect(db)
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({"schemaVersion": 1, "capabilities": {"metricStates": True, "semanticOutcome": False},
            "scope": {"kind": "instance", "project": None},
            "metricStates": {"report": {"status": "missing" if isinstance(exc, FileNotFoundError) else "error",
                "reason": "installation_or_database_missing" if isinstance(exc, FileNotFoundError) else "unreadable"}},
            "error": str(exc)[:300]}, ensure_ascii=False))
        return 1

    now = datetime.now(timezone.utc)
    today = now.date()
    cutoff_24h = (now - timedelta(hours=24)).isoformat()
    cutoff_7d = (now - timedelta(days=7)).isoformat()
    recent_days = [(today - timedelta(days=offset)).isoformat() for offset in range(days - 1, -1, -1)]
    series = {day: {"date": day, "events": 0, "captures": 0, "context": 0, "degraded": 0} for day in recent_days}

    events_total = value(con, "SELECT COUNT(*) FROM events") or 0
    events_24h = value(con, "SELECT COUNT(*) FROM events WHERE created_at >= ?", (cutoff_24h,)) or 0
    events_7d = value(con, "SELECT COUNT(*) FROM events WHERE created_at >= ?", (cutoff_7d,)) or 0
    sources_total = value(con, "SELECT COUNT(*) FROM sources") or 0
    sources_24h = value(con, "SELECT COUNT(*) FROM sources WHERE created_at >= ?", (cutoff_24h,)) or 0
    cards_active = value(con, "SELECT COUNT(*) FROM knowledge_cards WHERE state = 'active'") or 0
    cards_retired = value(con, "SELECT COUNT(*) FROM knowledge_cards WHERE state = 'retired'") or 0

    for row in rows(con, "SELECT created_at FROM events WHERE created_at >= ?", ((today - timedelta(days=days - 1)).isoformat(),)):
        key = day_of(row.get("created_at"))
        if key in series:
            series[key]["events"] += 1
    for row in rows(con, "SELECT created_at FROM sources WHERE created_at >= ?", ((today - timedelta(days=days - 1)).isoformat(),)):
        key = day_of(row.get("created_at"))
        if key in series:
            series[key]["captures"] += 1

    for row in rows(con, "SELECT day, operation, status, SUM(count) AS total FROM runtime_daily GROUP BY day, operation, status"):
        key = epoch_day(row.get("day"))
        if key not in series:
            continue
        operation = str(row.get("operation") or "")
        status = str(row.get("status") or "")
        total = int(row.get("total") or 0)
        if operation == "context":
            series[key]["context"] += total
            if status == "degraded":
                series[key]["degraded"] += total

    jobs = {row.get("state"): int(row.get("total") or 0) for row in rows(con, "SELECT state, COUNT(*) AS total FROM jobs GROUP BY state")}
    receipt_rows = rows(con, "SELECT state, host, elapsed_ms, rendered_cards, prepared_at, answer_bound_at FROM memory_context_receipts")
    latencies = [float(row["elapsed_ms"]) for row in receipt_rows if isinstance(row.get("elapsed_ms"), (int, float))]
    rendered_cards = 0
    for row in receipt_rows:
        try:
            rendered_cards += len(json.loads(row.get("rendered_cards") or "[]"))
        except (TypeError, ValueError):
            pass
    receipts_bound = sum(1 for row in receipt_rows if row.get("answer_bound_at") is not None)
    day_ago = now.timestamp() - DAY
    prepared_24h = sum(
        1 for row in receipt_rows
        if isinstance(row.get("prepared_at"), (int, float)) and float(row["prepared_at"]) >= day_ago
    )
    bound_24h = sum(
        1 for row in receipt_rows
        if isinstance(row.get("answer_bound_at"), (int, float)) and float(row["answer_bound_at"]) >= day_ago
    )
    receipts_by_host = {}
    for row in receipt_rows:
        host = str(row.get("host") or "unknown")
        receipts_by_host[host] = receipts_by_host.get(host, 0) + 1

    consumption = {row.get("state"): int(row.get("total") or 0) for row in rows(con, "SELECT state, COUNT(*) AS total FROM knowledge_consumption GROUP BY state")}
    feedback_rows = rows(con, "SELECT state, payload FROM knowledge_feedback")
    feedback_states = {}
    verdicts = {}
    findings = 0
    for row in feedback_rows:
        state = str(row.get("state") or "unknown")
        feedback_states[state] = feedback_states.get(state, 0) + 1
        try:
            payload = json.loads(row.get("payload") or "{}")
        except (TypeError, ValueError):
            continue
        for finding in payload.get("findings") or []:
            if not isinstance(finding, dict):
                continue
            findings += 1
            verdict = str(finding.get("verdict") or "unknown")
            verdicts[verdict] = verdicts.get(verdict, 0) + 1

    alerts_rows = rows(
        con,
        "SELECT category, severity, reason_code, first_seen, last_seen, occurrences, transition, acknowledged "
        "FROM runtime_alerts WHERE severity > 0 ORDER BY last_seen DESC LIMIT ?",
        (MAX_ALERTS,),
    )
    alerts = [
        {
            "category": row.get("category"),
            "severity": row.get("severity"),
            "reasonCode": row.get("reason_code"),
            "firstSeen": iso(row.get("first_seen")),
            "lastSeen": iso(row.get("last_seen")),
            "occurrences": row.get("occurrences"),
            "transition": row.get("transition"),
            "acknowledged": bool(row.get("acknowledged")),
        }
        for row in alerts_rows
    ]
    open_alerts = value(
        con,
        "SELECT COUNT(*) FROM runtime_alerts WHERE acknowledged = 0 AND severity > 0",
    ) or 0

    maintenance_row = None
    try:
        maintenance_row = con.execute(
            "SELECT status, observed_at, elapsed_ms, stages, reason_code FROM runtime_observations "
            "WHERE operation = 'maintenance' ORDER BY observed_at DESC LIMIT 1"
        ).fetchone()
    except sqlite3.Error as exc:
        if len(warnings) < 8:
            warnings.append(f"maintenance: {exc}")
        failed_tables.add('runtime_observations')
    maintenance = None
    if maintenance_row is not None:
        try:
            stages = json.loads(maintenance_row["stages"] or "{}")
        except (TypeError, ValueError):
            stages = {}
        maintenance = {
            "status": maintenance_row["status"],
            "observedAt": iso(maintenance_row["observed_at"]),
            "elapsedMs": round(float(maintenance_row["elapsed_ms"]), 1) if maintenance_row["elapsed_ms"] is not None else None,
            "reasonCode": maintenance_row["reason_code"],
            "stages": stages,
        }

    capture_by_host = {}
    for row in rows(
        con,
        "SELECT CASE WHEN instr(event_id, ':') > 0 THEN substr(event_id, 1, instr(event_id, ':') - 1) ELSE event_id END AS prefix, "
        "COUNT(*) AS total FROM events GROUP BY prefix",
    ):
        prefix = str(row.get("prefix") or "")
        if prefix in ("pi", "codex", "cc", "dsh"):
            capture_by_host[prefix] = int(row.get("total") or 0)

    knowledge_by_project = [
        {"project": row.get("project"), "count": int(row.get("total") or 0)}
        for row in rows(
            con,
            "SELECT project, COUNT(*) AS total FROM knowledge_cards WHERE state = 'active' GROUP BY project ORDER BY total DESC LIMIT 8",
        )
    ]
    recent_cards = [
        {
            "title": row.get("title"),
            "project": row.get("project"),
            "topic": row.get("topic"),
            "state": row.get("state"),
            "path": row.get("path"),
            "updatedAt": iso(row.get("updated_at")),
        }
        for row in rows(
            con,
            "SELECT title, project, topic, state, path, updated_at FROM knowledge_cards ORDER BY updated_at DESC LIMIT ?",
            (MAX_RECENT,),
        )
    ]
    recent_captures = [
        {
            "project": row.get("project"),
            "role": row.get("role"),
            "session": row.get("session"),
            "createdAt": iso(row.get("created_at")),
            "excerpt": row.get("excerpt"),
        }
        for row in rows(
            con,
            "SELECT s.project, s.role, s.session, s.created_at, "
            "(SELECT substr(v.text, 1, 140) FROM versions v WHERE v.source_id = s.id ORDER BY v.version DESC LIMIT 1) AS excerpt "
            "FROM sources s ORDER BY s.created_at DESC LIMIT ?",
            (MAX_RECENT,),
        )
    ]

    revisions_7d = value(con, "SELECT COUNT(*) FROM knowledge_revisions WHERE created_at >= ?", (now.timestamp() - 7 * DAY,)) or 0
    cards_7d = value(con, "SELECT COUNT(*) FROM knowledge_cards WHERE updated_at >= ?", (now.timestamp() - 7 * DAY,)) or 0
    usage_calls = value(con, "SELECT COUNT(*) FROM usage") or 0
    usage_tokens = value(con, "SELECT SUM(COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0)) FROM usage") or 0
    note_uses = {row.get("kind") or "unknown": int(row.get("total") or 0) for row in rows(con, "SELECT kind, COUNT(*) AS total FROM memory_note_uses GROUP BY kind")}
    source_reads = value(con, "SELECT COUNT(*) FROM memory_source_reads") or 0
    drafts = {row.get("status") or "unknown": int(row.get("total") or 0) for row in rows(con, "SELECT status, COUNT(*) AS total FROM inline_draft_states GROUP BY status")}
    work = {row.get("state") or "unknown": int(row.get("total") or 0) for row in rows(con, "SELECT state, COUNT(*) AS total FROM knowledge_work GROUP BY state")}

    # Optional current review ledger is distinct from legacy consumption/work.
    tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    reason_counts = None
    if 'runtime_reason_daily' in tables:
        reason_counts = rows(con, '''SELECT host,operation,status,reason_code,SUM(count) AS count
            FROM runtime_reason_daily WHERE day>=? AND day<=?
            GROUP BY host,operation,status,reason_code ORDER BY count DESC LIMIT 50''',
            (int(now.timestamp()//DAY)-days+1, int(now.timestamp()//DAY)))
    graph = None
    if {'runtime_observation_metrics','runtime_observations'} <= tables:
        from .observability import METRICS
        measured = rows(con, '''SELECT m.metrics,o.stages FROM runtime_observation_metrics m
            JOIN runtime_observations o USING(event_key) WHERE o.observed_at>=? AND o.observed_at<=?''',
            (now.timestamp()-days*DAY, now.timestamp()))
        counters, elapsed = {key: [] for key in METRICS}, []
        for item in measured:
            try:
                metrics = json.loads(item['metrics']); stages = json.loads(item['stages'])
                if not isinstance(metrics, dict) or not isinstance(stages, dict): raise ValueError()
                for key in METRICS:
                    n = metrics.get(key)
                    if type(n) is int and 0 <= n <= 1000000: counters[key].append(n)
                n = stages.get('graph')
                if type(n) in (int,float) and 0 <= n < float('inf'): elapsed.append(n)
            except (ValueError,TypeError):
                failed_tables.add('runtime_observation_metrics')
        graph = {'counts': {key: {'known':len(items),'sum':sum(items) if items else None} for key,items in sorted(counters.items())},
                 'latency': {'known':len(elapsed),'p50Ms':pct(elapsed,.5),'p95Ms':pct(elapsed,.95)},
                 'benefit':'unknown'}
    reviews = None
    if 'memory_work_units' in tables:
        reviews = []
        for item in rows(con, """SELECT project,result FROM memory_work_units WHERE rowid IN (
                SELECT MAX(rowid) FROM memory_work_units
                WHERE json_extract(payload,'$.kind')='review'
                GROUP BY project,json_extract(payload,'$.review_id')) ORDER BY created_at DESC LIMIT 20"""):
            try:
                result = json.loads(item['result'])
                if not isinstance(result, dict): raise ValueError('review result must be an object')
                reviews.append(dict(project=item['project'], **{k:result.get(k) for k in
                    ('review_id','scope','revision','status','mechanical_status','semantic_quality','benefit')}))
            except (ValueError, TypeError):
                warnings.append('review result is unreadable')
                failed_tables.add('memory_work_units')
    snapshot = {
        "generatedAt": now.isoformat(),
        "instance": {
            "enabled": bool(cfg.get("enabled")),
            "home": cfg.get("home"),
            "vault": cfg.get("vault"),
            "dbBytes": db.stat().st_size,
            "days": days,
        },
        "summary": {
            "events": events_total,
            "events24h": events_24h,
            "events7d": events_7d,
            "sources": sources_total,
            "sources24h": sources_24h,
            "cards": {"active": cards_active, "retired": cards_retired},
            "jobs": jobs,
            "alertsOpen": open_alerts,
            "captureByHost": capture_by_host,
            "receiptsByHost": receipts_by_host,
            "usage": {"calls": usage_calls, "tokens": int(usage_tokens or 0)},
        },
        "runtime": {
            "maintenance": maintenance,
            "failureReasons": reason_counts,
            "graph": graph,
            "context": {
                "prepared": len(receipt_rows),
                "prepared24h": prepared_24h,
                "bound": receipts_bound,
                "bound24h": bound_24h,
                "renderedCards": rendered_cards,
                "degraded": sum(int(item.get("degraded") or 0) for item in series.values()),
                "latency": {"p50": pct(latencies, 0.5), "p95": pct(latencies, 0.95), "max": round(max(latencies), 1) if latencies else None},
            },
            "alerts": alerts,
            "daily": list(series.values()),
        },
        "effect": {
            "note": "process indicators, not proof of net benefit",
            "noteUses": note_uses,
            "sourceReads": source_reads,
            "consumption": consumption,
            "feedback": {"states": feedback_states, "findings": findings, "verdicts": verdicts},
            "drafts": drafts,
            "work": work,
            "knowledge": {"revisions7d": revisions_7d, "cardsUpdated7d": cards_7d},
        },
        "knowledge": {"byProject": knowledge_by_project, "recent": recent_cards},
        "captures": recent_captures,
        "warnings": warnings,
    }
    requirements = {
        'summary.events': ['events'], 'summary.events24h':['events'], 'summary.events7d':['events'],
        'summary.sources':['sources'], 'summary.sources24h':['sources'], 'summary.cards':['knowledge_cards'],
        'summary.jobs':['jobs'], 'summary.alertsOpen':['runtime_alerts'], 'summary.captureByHost':['events'],
        'summary.receiptsByHost':['memory_context_receipts'], 'summary.usage':['usage'],
        'runtime.context':['memory_context_receipts'], 'runtime.context.degraded':['runtime_daily'],
        'runtime.alerts':['runtime_alerts'], 'runtime.daily':['events','sources','runtime_daily'],
        'runtime.failureReasons':['runtime_reason_daily'],
        'runtime.graph':['runtime_observation_metrics','runtime_observations'],
        'effect.noteUses':['memory_note_uses'], 'effect.sourceReads':['memory_source_reads'],
        'effect.consumption':['knowledge_consumption'], 'effect.feedback':['knowledge_feedback'],
        'effect.drafts':['inline_draft_states'], 'effect.work':['knowledge_work'],
        'effect.knowledge':['knowledge_revisions','knowledge_cards'], 'knowledge':['knowledge_cards'],
        'captures':['sources','versions'], 'effect.reviews':['memory_work_units']}
    snapshot['effect']['reviews'] = reviews
    states = {}
    for path, required in requirements.items():
        missing = sorted(set(required) - tables)
        failed = sorted(set(required) & failed_tables)
        status = 'unsupported' if missing else ('error' if failed else 'available')
        states[path] = {'status':status, 'reason': 'missing_table' if missing else ('query_failed' if failed else None)}
        if status != 'available':
            obj = snapshot
            parts = path.split('.')
            for part in parts[:-1]:
                if not isinstance(obj, dict): break
                obj = obj.get(part)
            if isinstance(obj, dict): obj[parts[-1]] = None
    trace = None
    try:
        config = json.loads((db.parent / 'config.json').read_text())
        trace = config.get('memory_trace_enabled') is True
    except (OSError,ValueError,TypeError):
        pass
    if states['runtime.context']['status'] == 'available' and not receipt_rows:
        states['runtime.context'] = {'status':'unmeasured', 'reason':'trace_disabled' if trace is False else 'no_receipts'}
        snapshot['runtime']['context'] = None
    states['runtime.maintenance'] = {'status': 'available' if maintenance else
        ('unsupported' if 'runtime_observations' not in tables else ('error' if 'runtime_observations' in failed_tables else 'unmeasured')), 'reason': None}
    snapshot.update(schemaVersion=1, capabilities={
        'metricStates': True, 'explicitAssetCoverage': (Path(__file__).with_name('asset_coverage.py').is_file()), 'currentReviews': states['effect.reviews']['status']=='available',
        'memoryTraceEnabled':trace, 'semanticOutcome':False}, metricStates=states,
        scope={'kind':'instance','project':None}, windows={
        'summary':'lifetime except *24h/*7d', 'runtime.context':'retained_receipts',
        'runtime.daily':{'days':days}, 'effect':'lifetime except *7d',
        'runtime.failureReasons':{'days':days,'bucket':'UTC calendar day',
            'coverage':'recorded since instrumented code; no historical backfill; SQLite contention can omit events'},
        'runtime.graph':{'days':days,'scope':'retained measured events; counts are discovery, not answer benefit'},
        'reviews':'latest state per project/review, up to 20',
        'notice':'Different windows are not a conversion funnel.'})
    con.close()
    print(json.dumps(snapshot, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
