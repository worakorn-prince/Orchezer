import os
import sys
import json
import argparse
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MANAGER_DIR = os.path.join(ROOT, ".agent", "manager")
EVENTS_FILE = os.path.join(MANAGER_DIR, "events.jsonl")
TOOLCALLS_FILE = os.path.join(MANAGER_DIR, "tool-calls.jsonl")
METRICS_FILE = os.path.join(MANAGER_DIR, "metrics.json")
HISTORY_DIR = os.path.join(MANAGER_DIR, "history")
HISTORY_KEEP_DAYS = 90

SESSION_EVENTS = ("WORKER_STARTED", "WORKER_RESUMED")
RECOVERY_EVENT = "RECOVERY_STARTED"
REVIEW_FAIL_EVENT = "REVIEW_FAILED"
REVIEW_PASS_EVENT = "REVIEW_PASSED"
MIN_SAMPLE_TASKS = 3
CREATED_EVENT = "TASK_CREATED"
DONE_EVENT = "TASK_DONE"
FAILED_EVENTS = ("TASK_FAILED",)
CANCELLED_EVENTS = ("TASK_CANCELLED", "TASK_CANCELED")


def read_jsonl(path):
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def parse_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _is_excluded_task(task):
    return isinstance(task, str) and (task.startswith("TEST-") or task.startswith("DEP-"))


def compute_metrics(events, toolcalls):
    task_ids = set()
    for e in events:
        t = e.get("task")
        if t and not _is_excluded_task(t):
            task_ids.add(t)
    for r in toolcalls:
        t = r.get("task")
        if t and not _is_excluded_task(t):
            task_ids.add(t)

    per_task = {}
    call_by_task = {}
    for r in toolcalls:
        if r.get("task") and not _is_excluded_task(r.get("task")):
            call_by_task.setdefault(r.get("task"), []).append(r)
    for task in sorted(task_ids):
        sessions = sum(1 for e in events if e.get("task") == task and e.get("event") in SESSION_EVENTS)
        rows = call_by_task.get(task, [])
        calls = sum(1 for r in rows if r.get("status") != "denied")
        denied = sum(1 for r in rows if r.get("status") == "denied")
        recoveries = sum(1 for e in events if e.get("task") == task and e.get("event") == RECOVERY_EVENT)
        loops = sum(1 for e in events if e.get("task") == task and e.get("event") == REVIEW_FAIL_EVENT)
        created = next((e for e in events if e.get("task") == task and e.get("event") == CREATED_EVENT), None)
        done = next((e for e in events if e.get("task") == task and e.get("event") == DONE_EVENT), None)
        time_to_done = None
        if created and done:
            t0 = parse_time(created.get("time"))
            t1 = parse_time(done.get("time"))
            if t0 and t1:
                time_to_done = (t1 - t0).total_seconds()
        rows = call_by_task.get(task, [])
        calls = len([r for r in rows if r.get("status") != "denied"])
        tokens = sum(int(r.get("tokens_in") or 0) + int(r.get("tokens_out") or 0) for r in rows)
        fail_times = [parse_time(r.get("time")) for r in rows
                      if r.get("status") == "error" and parse_time(r.get("time"))]
        fail_times += [parse_time(e.get("time")) for e in events
                       if e.get("task") == task
                       and e.get("event") in (REVIEW_FAIL_EVENT, RECOVERY_EVENT)
                       and parse_time(e.get("time"))]
        first_fail = min(fail_times) if fail_times else None
        rec_tokens = 0
        if first_fail is not None:
            for r in rows:
                rt = parse_time(r.get("time"))
                if rt and rt > first_fail:
                    rec_tokens += int(r.get("tokens_in") or 0) + int(r.get("tokens_out") or 0)
        review_passed = sum(1 for e in events if e.get("task") == task
                            and e.get("event") == REVIEW_PASS_EVENT)
        per_task[task] = {
            "sessions_per_task": sessions,
            "tool_calls_per_task": calls,
            "recovery_count": recoveries,
            "review_loop": loops,
            "time_to_DONE": time_to_done,
            "done": done is not None,
            "tokens_total": tokens,
            "recovery_tokens": rec_tokens,
            "review_passed": review_passed,
            "denied_attempts": denied
        }

    done_count = sum(1 for e in events if not _is_excluded_task(e.get("task")) and e.get("event") == DONE_EVENT)
    failed_count = sum(1 for e in events if not _is_excluded_task(e.get("task")) and e.get("event") in FAILED_EVENTS)
    cancelled_count = sum(1 for e in events if not _is_excluded_task(e.get("task")) and e.get("event") in CANCELLED_EVENTS)
    terminal = done_count + failed_count + cancelled_count
    success_rate = (done_count / terminal) if terminal else 0.0
    totals = {
        "tasks": len(per_task),
        "done": done_count,
        "failed": failed_count,
        "cancelled": cancelled_count,
        "success_rate": success_rate,
        "sessions": sum(v["sessions_per_task"] for v in per_task.values()),
        "tool_calls": sum(v["tool_calls_per_task"] for v in per_task.values()),
        "recoveries": sum(v["recovery_count"] for v in per_task.values()),
        "review_loops": sum(v["review_loop"] for v in per_task.values()),
        "tokens_total": sum(v.get("tokens_total", 0) for v in per_task.values()),
        "denied_attempts": sum(v.get("denied_attempts", 0) for v in per_task.values()),
        "recovery_tokens": sum(v.get("recovery_tokens", 0) for v in per_task.values()),
        "review_passed": sum(v.get("review_passed", 0) for v in per_task.values())
    }

    def _div(a, b):
        return (a / b) if b else None

    successful = done_count
    efficiency = {
        "task_efficiency": _div(done_count, len(per_task)),
        "token_per_success": _div(totals["tokens_total"], successful),
        "tools_per_success": _div(totals["tool_calls"], successful),
        "recovery_cost_ratio": _div(totals["recovery_tokens"], totals["tokens_total"]),
        "session_efficiency": _div(done_count, totals["sessions"]),
        "review_rework_rate": _div(totals["review_loops"],
                                   totals["review_loops"] + totals["review_passed"])
    }

    done_map = {t: v["done"] for t, v in per_task.items()}
    rec_task_set = {t for t, v in per_task.items()
                    if v["recovery_count"] > 0 or v["recovery_tokens"] > 0}
    aggr = {}
    for r in toolcalls:
        ag = r.get("agent") or "?"
        a = aggr.setdefault(ag, {"tasks": set(), "tokens": 0, "calls": 0,
                                 "denied": 0, "ms": 0.0})
        if r.get("task") and not _is_excluded_task(r.get("task")):
            a["tasks"].add(r["task"])
        if r.get("status") == "denied":
            a["denied"] += 1
            continue
        a["calls"] += 1
        a["tokens"] += int(r.get("tokens_in") or 0) + int(r.get("tokens_out") or 0)
        if isinstance(r.get("duration_ms"), (int, float)):
            a["ms"] += r["duration_ms"]
    for e in events:
        if e.get("event") in SESSION_EVENTS and e.get("agent") and e.get("task") \
                and not _is_excluded_task(e.get("task")):
            aggr.setdefault(e["agent"], {"tasks": set(), "tokens": 0,
                                         "calls": 0, "ms": 0.0})["tasks"].add(e["task"])
    agent_efficiency = []
    for ag in sorted(aggr):
        a = aggr[ag]
        n = len(a["tasks"])
        n_done = sum(1 for t in a["tasks"] if done_map.get(t))
        agent_efficiency.append({
            "agent": ag, "tasks": n, "denied": a["denied"],
            "success_rate": (n_done / n) if n else None,
            "avg_tokens": (a["tokens"] / n) if n else None,
            "avg_tools": (a["calls"] / n) if n else None,
            "avg_time_s": round(a["ms"] / 1000.0 / n, 1) if n else None,
            "recovery_rate": (sum(1 for t in a["tasks"] if t in rec_task_set) / n) if n else None,
            "low_sample": n < MIN_SAMPLE_TASKS
        })
    return per_task, totals, efficiency, agent_efficiency


def save_snapshot(totals):
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        os.makedirs(HISTORY_DIR, exist_ok=True)
        path = os.path.join(HISTORY_DIR, f"{day}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"date": day, "totals": totals}, f, indent=2, ensure_ascii=False)
        for fn in sorted(os.listdir(HISTORY_DIR)):
            if len(fn) == 15 and fn.endswith(".json") and fn[:10] < day:
                try:
                    dt = datetime.strptime(fn[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    age = (datetime.now(timezone.utc) - dt).days
                    if age > HISTORY_KEEP_DAYS:
                        os.remove(os.path.join(HISTORY_DIR, fn))
                except Exception:
                    continue
    except Exception:
        pass


def rebuild(task_filter=None):
    events = read_jsonl(EVENTS_FILE)
    toolcalls = read_jsonl(TOOLCALLS_FILE)
    per_task, totals, efficiency, agent_efficiency = compute_metrics(events, toolcalls)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "totals": totals,
        "per_task": per_task,
        "efficiency": efficiency,
        "agent_efficiency": agent_efficiency
    }
    os.makedirs(MANAGER_DIR, exist_ok=True)
    with open(METRICS_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    save_snapshot(totals)
    show = {task_filter: per_task[task_filter]} if task_filter and task_filter in per_task else per_task
    print(f"Metrics rebuilt: {totals['tasks']} tasks, {totals['tool_calls']} tool calls, success_rate {totals['success_rate']:.2f}")
    if task_filter:
        if task_filter in per_task:
            print(f"Task {task_filter}: {json.dumps(per_task[task_filter], ensure_ascii=False)}")
        else:
            print(f"Task {task_filter}: no data")
    else:
        for tid in sorted(show):
            print(f"Task {tid}: {json.dumps(show[tid], ensure_ascii=False)}")
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(description="Offline metrics from events.jsonl + tool-calls.jsonl")
    parser.add_argument("--rebuild", action="store_true", help="Recompute all metrics and rewrite metrics.json (idempotent)")
    parser.add_argument("--task", default=None, help="Show only TASK-xxx in summary")
    args = parser.parse_args(argv)
    rebuild(task_filter=args.task)
    return 0


if __name__ == "__main__":
    sys.exit(main())
