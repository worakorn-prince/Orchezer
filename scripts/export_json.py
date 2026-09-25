import os
import sys
import json
import glob
import argparse
import sqlite3
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MANAGER_DIR = os.path.join(ROOT, ".agent", "manager")
METRICS_FILE = os.path.join(MANAGER_DIR, "metrics.json")
EVENTS_FILE = os.path.join(MANAGER_DIR, "events.jsonl")
TOOLCALLS_FILE = os.path.join(MANAGER_DIR, "tool-calls.jsonl")
QUEUE_FILE = os.path.join(MANAGER_DIR, "queue.json")
CONFIG_FILE = os.path.join(MANAGER_DIR, "config.json")
REVIEWS_DIR = os.path.join(MANAGER_DIR, "reviews")
HISTORY_DIR = os.path.join(MANAGER_DIR, "history")
CHECKPOINT_FILE = os.path.join(ROOT, ".agent", "building", "checkpoint.json")
DEFAULT_OUT = os.path.join(ROOT, "dashboard", "data.json")
DB_PATH = os.path.join(MANAGER_DIR, "manager_index.db")
DB_MAX_AGE_SECONDS = 3600

try:
    from tools_inventory import build_tools_section
except ImportError:
    build_tools_section = None
try:
    from graph import build_graph
except ImportError:
    build_graph = None
try:
    from risk import assess as assess_risk
except ImportError:
    assess_risk = None
try:
    from failure import analyze as analyze_failures
except ImportError:
    analyze_failures = None
try:
    from observability import build_contract
except ImportError:
    build_contract = None

SESSION_EVENTS = ("WORKER_STARTED", "WORKER_RESUMED")
DONE_EVENT = "TASK_DONE"


def redact_secrets(text):
    import re
    if not isinstance(text, str):
        text = str(text or "")
    redacted = re.sub(
        r"(?i)(api_key|apikey|secret|password|passwd|pwd|token|bearer|authorization)\s*([:=]\s*|\\s+)(['\"]?)([^\s'\";,}]+)(['\"]?)",
        lambda m: m.group(1) + m.group(2) + m.group(3) + "[REDACTED]" + m.group(5),
        text,
    )
    redacted = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9\-._~+/=]+", "Bearer [REDACTED]", redacted)
    redacted = re.sub(r"\bsk-[A-Za-z0-9\-_]{8,}", "[REDACTED]", redacted)
    redacted = re.sub(r"\bgh[pousr]_[A-Za-z0-9]{8,}", "[REDACTED]", redacted)
    redacted = re.sub(r"\bxox[bpas]-[A-Za-z0-9\-]{8,}", "[REDACTED]", redacted)
    redacted = re.sub(r"\b[A-Za-z0-9_\-+/=]{20,}\b", "[REDACTED]", redacted)
    return redacted


def dump_json(payload, path):
    tool_calls = 0
    try:
        tool_calls = int(((payload.get("kpis", {}) or {}).get("total_tool_calls", 0)
                          or (payload.get("combined", {}) or {}).get("total_tool_calls", 0)) or 0)
    except Exception:
        pass
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        if tool_calls > 50000:
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(payload, f, indent=2, ensure_ascii=False)
FAILED_EVENTS = ("TASK_FAILED",)
CANCELLED_EVENTS = ("TASK_CANCELLED", "TASK_CANCELED")
STUCK_HOURS = 6
FEED_LIMIT = 20


def load_json(path, default=None):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def read_jsonl(path):
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, "r", encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def read_reviews(reviews_dir):
    records = []
    if not os.path.isdir(reviews_dir):
        return records
    for path in sorted(glob.glob(os.path.join(reviews_dir, "*.json"))):
        rec = load_json(path)
        if isinstance(rec, dict) and rec.get("task"):
            records.append(rec)
    return records


def read_history(history_dir):
    snaps = []
    if not os.path.isdir(history_dir):
        return snaps
    for path in sorted(glob.glob(os.path.join(history_dir, "*.json"))):
        s = load_json(path)
        if isinstance(s, dict) and s.get("date"):
            snaps.append(s)
    snaps.sort(key=lambda s: s.get("date", ""))
    return snaps


def read_snapshot_via_db(db_path, events_path, queue_path):
    try:
        if not db_path or not os.path.exists(db_path):
            return (None, None)
        try:
            with open(events_path, "r", encoding="utf-8-sig") as f:
                line_count = sum(1 for _ in f)
        except FileNotFoundError:
            line_count = 0
        except Exception:
            return (None, None)
        conn = sqlite3.connect("file:%s?mode=ro" % os.path.abspath(db_path), uri=True, timeout=5)
        try:
            try:
                cur = conn.execute("SELECT key, value FROM sync_meta WHERE key IN ('last_sync_at', 'last_seq')")
                meta = {k: v for k, v in cur.fetchall()}
            except Exception:
                return (None, None)
            last_sync_at = parse_time(meta.get("last_sync_at"))
            if last_sync_at is None:
                return (None, None)
            age = (datetime.now(timezone.utc) - last_sync_at).total_seconds()
            if age < 0 or age >= DB_MAX_AGE_SECONDS:
                return (None, None)
            try:
                last_seq = int(meta.get("last_seq", "0"))
            except Exception:
                return (None, None)
            if last_seq < line_count:
                return (None, None)
            try:
                cur = conn.execute("SELECT raw_json FROM events ORDER BY seq")
                event_rows = cur.fetchall()
            except Exception:
                return (None, None)
            events = []
            for (raw,) in event_rows:
                try:
                    events.append(json.loads(raw))
                except Exception:
                    continue
            try:
                cur = conn.execute("SELECT raw_json FROM queue_snapshot")
                qrows = cur.fetchall()
            except Exception:
                return (None, None)
            tasks = []
            for (raw,) in qrows:
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue
                if isinstance(obj, dict):
                    tasks.append(obj)
            queue = {"tasks": tasks}
            return (events, queue)
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception:
        return (None, None)


def parse_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def percentile(sorted_vals, pct):
    if not sorted_vals:
        return 0
    idx = int(pct * (len(sorted_vals) - 1))
    return sorted_vals[idx]


def build_extra(events, toolcalls, queue, reviews, history, files_changed, config, per_task):
    events = [e for e in events if isinstance(e, dict)]
    toolcalls = [r for r in toolcalls if isinstance(r, dict)]
    tasks = (queue or {}).get("tasks", []) if isinstance(queue, dict) else []

    recent_activity = [
        {"time": e.get("time"), "event": e.get("event"), "task": e.get("task")}
        for e in events if e.get("event")
    ][-FEED_LIMIT:][::-1]

    err_rows = [r for r in toolcalls if r.get("status") == "error"]
    errors = {
        "count": len(err_rows),
        "recent": [
             {"time": r.get("time"), "task": r.get("task"),
              "tool": r.get("tool"), "error": redact_secrets(str(r.get("error") or ""))[:300]}
            for r in err_rows[-FEED_LIMIT:]
        ][::-1],
    }

    by_tool = {}
    for r in toolcalls:
        d = r.get("duration_ms")
        if not isinstance(d, (int, float)):
            continue
        by_tool.setdefault(r.get("tool") or "?", []).append(d)
    durations = {"overall_avg_ms": 0, "per_tool": []}
    all_d = [d for ds in by_tool.values() for d in ds]
    if all_d:
        durations["overall_avg_ms"] = round(sum(all_d) / len(all_d), 1)
    for tool, ds in sorted(by_tool.items(), key=lambda kv: -len(kv[1]))[:10]:
        ds_sorted = sorted(ds)
        durations["per_tool"].append({
            "tool": tool, "calls": len(ds_sorted),
            "avg_ms": round(sum(ds_sorted) / len(ds_sorted), 1),
            "p95_ms": percentile(ds_sorted, 0.95),
        })

    agents = {}
    for e in events:
        if e.get("event") in SESSION_EVENTS and e.get("agent"):
            a = agents.setdefault(e["agent"], {"calls": 0, "sessions": 0, "errors": 0, "tokens": 0})
            a["sessions"] += 1
    for r in toolcalls:
        a = agents.setdefault(r.get("agent") or "?", {"calls": 0, "sessions": 0, "errors": 0, "tokens": 0})
        a["calls"] += 1
        a["tokens"] += int(r.get("tokens_in") or 0) + int(r.get("tokens_out") or 0)
        if r.get("status") == "error":
            a["errors"] += 1
    agents = [{"agent": k, **v} for k, v in sorted(agents.items())]

    timed = [t for t in per_task if t.get("time_to_DONE") is not None]
    slowest_tasks = sorted(timed, key=lambda t: -(t.get("time_to_DONE") or 0))[:5]

    by_status = {}
    blocked, failed = [], []
    for t in tasks:
        st = t.get("status") or "?"
        by_status[st] = by_status.get(st, 0) + 1
        if st == "blocked":
            blocked.append(t.get("id"))
        if st == "failed":
            failed.append(t.get("id"))
    queue_info = {"by_status": by_status, "blocked": blocked, "failed": failed}

    _SEV_CANON = ("CRITICAL", "HIGH", "MEDIUM", "LOW")
    _SEV_LEGACY = {"critical": "CRITICAL", "major": "HIGH", "minor": "MEDIUM",
                   "high": "HIGH", "medium": "MEDIUM", "low": "LOW"}

    def _norm_sev(value, default="MEDIUM"):
        s = str(value if value is not None else default).strip()
        if s in _SEV_CANON:
            return s
        mapped = _SEV_LEGACY.get(s.lower())
        return mapped if mapped else default

    sev_total = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0}
    findings_recent = []
    for rec in sorted(reviews, key=lambda r: str(r.get("time") or "")):
        for f in rec.get("findings", []) or []:
            sev = _norm_sev((f or {}).get("severity"), "MEDIUM")
            sev_total[sev] += 1
            findings_recent.append({
                "task": rec.get("task"), "time": rec.get("time"),
                "status": rec.get("status"), "severity": sev,
                "file": f.get("file"), "issue": str(f.get("issue") or "")[:200],
            })
    findings = {"total": sum(sev_total.values()),
                "by_severity": {**sev_total},
                "recent": findings_recent[-10:][::-1]}

    max_cycles = ((config or {}).get("limits", {}) or {}).get("max_review_cycles", 3)
    try:
        max_cycles = int(max_cycles)
    except Exception:
        max_cycles = 3
    terminal = set()
    last_time = {}
    for e in events:
        t = e.get("task")
        if not t:
            continue
        if e.get("event") in (DONE_EVENT,) + FAILED_EVENTS + CANCELLED_EVENTS:
            terminal.add(t)
        if e.get("time"):
            last_time[t] = e.get("time")
    now = datetime.now(timezone.utc)
    alerts = []
    for t, lt in last_time.items():
        if t in terminal or not lt:
            continue
        dt = parse_time(lt)
        if dt and (now - dt).total_seconds() > STUCK_HOURS * 3600:
            hours = round((now - dt).total_seconds() / 3600, 1)
            alerts.append({"level": "error", "code": "STUCK",
                           "message": f"{t} no event for {hours}h (>{STUCK_HOURS}h)"})
    for row in per_task:
        if not row.get("done") and (row.get("review_loop") or 0) >= max(1, max_cycles - 1):
            alerts.append({"level": "warn", "code": "REVIEW_AT_RISK",
                           "message": f"{row['task']} review_loop={row['review_loop']} near limit {max_cycles}"})
    if blocked:
        alerts.append({"level": "error", "code": "QUEUE_BLOCKED",
                       "message": f"blocked tasks: {', '.join(blocked)}"})
    if failed:
        alerts.append({"level": "error", "code": "QUEUE_FAILED",
                       "message": f"failed tasks: {', '.join(failed)}"})
    open_alerts = sum(1 for a in alerts if a["level"] == "error")

    trend = [{"date": s.get("date"),
              "tasks": (s.get("totals", {}) or {}).get("tasks", 0),
              "done": (s.get("totals", {}) or {}).get("done", 0),
              "tool_calls": (s.get("totals", {}) or {}).get("tool_calls", 0),
              "success_rate": (s.get("totals", {}) or {}).get("success_rate", 0.0)}
             for s in history]

    tokens_total = sum(int(r.get("tokens_in") or 0) + int(r.get("tokens_out") or 0) for r in toolcalls)

    return {
        "recent_activity": recent_activity,
        "errors": errors,
        "durations": durations,
        "agents": agents,
        "slowest_tasks": slowest_tasks,
        "queue": queue_info,
        "files_changed": (files_changed or [])[:30],
        "findings": findings,
        "alerts": alerts,
        "open_alerts": open_alerts,
        "trend": trend,
        "tokens_total": tokens_total,
    }


def build_payload(metrics, events, toolcalls=None, queue=None, reviews=None,
                  history=None, files_changed=None, config=None, tools_section=None,
                  checkpoint=None):
    totals = (metrics or {}).get("totals", {}) if isinstance(metrics, dict) else {}
    per_task_raw = (metrics or {}).get("per_task", {}) if isinstance(metrics, dict) else {}
    if not isinstance(per_task_raw, dict):
        per_task_raw = {}

    per_task = []
    for task in sorted(per_task_raw):
        v = per_task_raw[task] or {}
        per_task.append({
            "task": task,
            "done": bool(v.get("done", False)),
            "sessions": int(v.get("sessions_per_task", 0) or 0),
            "tool_calls": int(v.get("tool_calls_per_task", 0) or 0),
            "recovery": int(v.get("recovery_count", 0) or 0),
            "review_loop": int(v.get("review_loop", 0) or 0),
            "time_to_DONE": v.get("time_to_DONE"),
            "tokens": int(v.get("tokens_total", 0) or 0),
            "recovery_tokens": int(v.get("recovery_tokens", 0) or 0),
            "review_passed": int(v.get("review_passed", 0) or 0),
            "denied": int(v.get("denied_attempts", 0) or 0),
        })

    timeline = []
    for e in events:
        if not isinstance(e, dict):
            continue
        if not e.get("event"):
            continue
        timeline.append({
            "time": e.get("time"),
            "event": e.get("event"),
            "task": e.get("task"),
        })

    done = int(totals.get("done", 0) or 0)
    failed = int(totals.get("failed", 0) or 0)
    cancelled = int(totals.get("cancelled", 0) or 0)
    total_tasks = int(totals.get("tasks", len(per_task)) or 0)
    try:
        success_rate = float(totals.get("success_rate", 0.0) or 0.0)
    except Exception:
        success_rate = 0.0

    extra = build_extra(events, toolcalls or [], queue or {}, reviews or [],
                        history or [], files_changed or [], config or {}, per_task)

    efficiency = (metrics or {}).get("efficiency", {}) if isinstance(metrics, dict) else {}
    agent_efficiency = ((metrics or {}).get("agent_efficiency", [])
                        if isinstance(metrics, dict) else [])

    execution = {"tasks": [], "summary": {"tasks": 0, "completed": 0,
                 "failed": 0, "running": 0, "total_sessions": 0, "total_attempts": 0}}
    if build_graph is not None:
        try:
            execution = build_graph(events, toolcalls or [])
        except Exception:
            pass

    risk = {"overall": "ok", "sessions": [], "thresholds": {},
            "history_sessions": 0, "median_calls": None}
    if assess_risk is not None:
        try:
            risk = assess_risk(toolcalls or [], events, checkpoint, config or {})
        except Exception:
            pass

    failure = {"tasks": {}, "summary": {"tasks_with_failures": 0,
               "total_failures": 0, "total_recovery_attempts": 0,
               "recovery_success_rate": None, "total_recovery_tokens": 0}}
    if analyze_failures is not None:
        try:
            failure = analyze_failures(events, toolcalls or [])
        except Exception:
            pass
    contract = {"schema_version": 1, "generated_at": None, "task_id": None,
                "status": "unknown", "session": None, "recommendations": []}
    if build_contract is not None:
        try:
            contract = build_contract(events=events, toolcalls=toolcalls or [],
                                      checkpoint=checkpoint, config=config or {})
        except Exception:
            pass

    if tools_section is None:
        if build_tools_section is not None:
            try:
                tools_section = build_tools_section(toolcalls or [])
            except Exception:
                tools_section = {"catalog_n": 0, "matrix": [], "agents": {}}
        else:
            tools_section = {"catalog_n": 0, "matrix": [], "agents": {}}

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "kpis": {
            "total_tasks": total_tasks,
            "done": done,
            "failed": failed,
            "cancelled": cancelled,
            "success_rate": success_rate,
            "total_tool_calls": int(totals.get("tool_calls", 0) or 0),
            "recoveries": int(totals.get("recoveries", 0) or 0),
            "review_fails": int(totals.get("review_loops", 0) or 0),
            "tokens_total": int(totals.get("tokens_total", extra["tokens_total"]) or 0),
            "open_alerts": extra["open_alerts"],
        },
        "per_task": per_task,
        "timeline": timeline,
        "recent_activity": extra["recent_activity"],
        "errors": extra["errors"],
        "durations": extra["durations"],
        "agents": extra["agents"],
        "slowest_tasks": extra["slowest_tasks"],
        "queue": extra["queue"],
        "files_changed": extra["files_changed"],
        "findings": extra["findings"],
        "alerts": extra["alerts"],
        "trend": extra["trend"],
        "execution": execution,
        "efficiency": efficiency,
        "agent_efficiency": agent_efficiency,
        "risk": risk,
        "failure": failure,
        "contract": contract,
        "tools": tools_section,
    }
    return payload


def export_data(out_path, metrics_path=METRICS_FILE, events_path=EVENTS_FILE,
                toolcalls_path=None, queue_path=None, reviews_dir=None,
                history_dir=None, checkpoint_path=None, config_path=None,
                db_path=None, use_db=True):
    toolcalls_path = toolcalls_path or TOOLCALLS_FILE
    queue_path = queue_path or QUEUE_FILE
    reviews_dir = reviews_dir or REVIEWS_DIR
    history_dir = history_dir or HISTORY_DIR
    checkpoint_path = checkpoint_path or CHECKPOINT_FILE
    config_path = config_path or CONFIG_FILE
    metrics = load_json(metrics_path, default=None)
    events = None
    queue = None
    source = "jsonl"
    if use_db:
        db_events, db_queue = read_snapshot_via_db(db_path or DB_PATH, events_path, queue_path)
        if db_events is not None and db_queue is not None:
            events = db_events
            queue = db_queue
            source = "db"
    if events is None:
        events = read_jsonl(events_path)
    if queue is None:
        queue = load_json(queue_path, default={}) or {}
    print("source=%s" % source, file=sys.stderr)
    toolcalls = read_jsonl(toolcalls_path)
    reviews = read_reviews(reviews_dir)
    history = read_history(history_dir)
    checkpoint = load_json(checkpoint_path, default={}) or {}
    config = load_json(config_path, default={}) or {}
    payload = build_payload(metrics, events, toolcalls, queue, reviews,
                            history, checkpoint.get("files_changed", []), config,
                            checkpoint=checkpoint)
    dump_json(payload, out_path)
    k = payload["kpis"]
    print(f"Exported: {k['total_tasks']} tasks, {k['done']} done, "
          f"success_rate {k['success_rate']:.2f}, "
          f"{k['total_tool_calls']} tool calls, {k['tokens_total']} tokens, "
          f"{k['recoveries']} recoveries, {k['review_fails']} review fails, "
          f"{k['open_alerts']} open alerts, "
          f"{len(payload['timeline'])} timeline events -> {out_path}")
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(description="Export metrics.json + events.jsonl to dashboard/data.json")
    parser.add_argument("--out", default=DEFAULT_OUT, help="Output data.json path")
    parser.add_argument("--metrics", default=METRICS_FILE, help="Input metrics.json path")
    parser.add_argument("--events", default=EVENTS_FILE, help="Input events.jsonl path")
    parser.add_argument("--db", default=DB_PATH, help="SQLite read-model path")
    parser.add_argument("--no-db", action="store_true", help="Force reading from JSON files")
    args = parser.parse_args(argv)
    export_data(args.out, metrics_path=args.metrics, events_path=args.events,
                db_path=args.db, use_db=not args.no_db)
    return 0


if __name__ == "__main__":
    sys.exit(main())
