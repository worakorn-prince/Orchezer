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
    for task in sorted(task_ids):
        sessions = sum(1 for e in events if e.get("task") == task and e.get("event") in SESSION_EVENTS)
        calls = sum(1 for r in toolcalls if r.get("task") == task)
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
        rows = [r for r in toolcalls if r.get("task") == task]
        calls = len(rows)
        tokens = sum(int(r.get("tokens_in") or 0) + int(r.get("tokens_out") or 0) for r in rows)
        per_task[task] = {
            "sessions_per_task": sessions,
            "tool_calls_per_task": calls,
            "recovery_count": recoveries,
            "review_loop": loops,
            "time_to_DONE": time_to_done,
            "done": done is not None,
            "tokens_total": tokens
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
        "tokens_total": sum(v.get("tokens_total", 0) for v in per_task.values())
    }
    return per_task, totals


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
    per_task, totals = compute_metrics(events, toolcalls)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "totals": totals,
        "per_task": per_task
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
