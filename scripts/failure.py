"""failure.py — Failure taxonomy + recovery analytics (roadmap Phase 6, P1 step 6).

Answers "how did the system handle the failure" deterministically:
fixed categories, recovery metrics per task, repeated-pattern detection
with an escalate recommendation. Scopeboard never stops a task itself.
"""
import os
import sys
import json
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

CATEGORIES = ("TOOL_ERROR", "TEST_FAILURE", "REVIEW_FAILURE",
              "SESSION_LIMIT", "TIMEOUT", "BLOCKED", "UNKNOWN")
REPEAT_THRESHOLD = 3


def _parse_time(value):
    if not value:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def read_jsonl(path):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    rows.append(obj)
            except Exception:
                continue
    return rows


def classify(text, event=None):
    t = str(text or "").lower()
    if event == "REVIEW_FAILED":
        return "REVIEW_FAILURE"
    if event in ("TASK_FAILED",) and not t:
        return "UNKNOWN"
    if "stopped_limit" in t or ("limit" in t and "tool" in t):
        return "SESSION_LIMIT"
    if "timeout" in t or "timed out" in t:
        return "TIMEOUT"
    if "blocked" in t:
        return "BLOCKED"
    if any(k in t for k in ("pytest", "unittest", "test failed", "tests failed",
                            "assertionerror", "test_")):
        return "TEST_FAILURE"
    if t:
        return "TOOL_ERROR"
    return "UNKNOWN"


def _is_excluded_task(task):
    return isinstance(task, str) and (task.startswith("TEST-") or task.startswith("DEP-"))


def analyze(events, toolcalls):
    events = [e for e in (events or []) if isinstance(e, dict) and e.get("task")
              and not _is_excluded_task(e.get("task"))]
    toolcalls = [r for r in (toolcalls or []) if isinstance(r, dict) and r.get("task")
                 and not _is_excluded_task(r.get("task"))]
    task_ids = sorted({e.get("task") for e in events} | {r.get("task") for r in toolcalls})

    call_by_task = {}
    for r in toolcalls:
        if r.get("task"):
            call_by_task.setdefault(r.get("task"), []).append(r)

    tasks = {}
    for tid in task_ids:
        task_calls = call_by_task.get(tid, [])
        failures = []
        for r in task_calls:
            if r.get("status") == "error":
                failures.append({"time": r.get("time"), "category": classify(r.get("error")),
                                 "detail": str(r.get("error") or "")[:200],
                                 "tool": r.get("tool"), "session_id": r.get("session_id")})
        for e in events:
            if e.get("task") == tid and e.get("event") in ("REVIEW_FAILED", "TASK_FAILED"):
                failures.append({"time": e.get("time"),
                                 "category": classify(e.get("message"), e.get("event")),
                                 "detail": str(e.get("message") or "")[:200],
                                 "tool": None, "session_id": e.get("session_id")})
            if e.get("task") == tid and e.get("event") == "WORKER_BLOCKED":
                failures.append({"time": e.get("time"), "category": "BLOCKED",
                                 "detail": str(e.get("message") or "")[:200],
                                 "tool": None, "session_id": e.get("session_id")})
        failures.sort(key=lambda f: str(f.get("time") or ""))

        rec_events = sorted(
            (e for e in events if e.get("task") == tid
             and e.get("event") in ("RECOVERY_STARTED", "RECOVERY_READY", "WORKER_RESUMED")),
            key=lambda e: str(e.get("time") or ""))
        done = next((e for e in events if e.get("task") == tid and e.get("event") == "TASK_DONE"), None)

        rec_tokens = 0
        time_to_recovery = None
        if failures and rec_events:
            first_fail = _parse_time(failures[0].get("time"))
            first_rec = _parse_time(rec_events[0].get("time"))
            if first_fail and first_rec and first_rec >= first_fail:
                time_to_recovery = round((first_rec - first_fail).total_seconds(), 1)
                for r in task_calls:
                    rt = _parse_time(r.get("time"))
                    if rt and rt > first_fail:
                        rec_tokens += int(r.get("tokens_in") or 0) + int(r.get("tokens_out") or 0)

        by_cat = {}
        for f in failures:
            by_cat[f["category"]] = by_cat.get(f["category"], 0) + 1
        repeated = [{"category": c, "count": n,
                     "recommendation": "Escalate to Manager / human review"}
                    for c, n in sorted(by_cat.items()) if n >= REPEAT_THRESHOLD]

        tasks[tid] = {
            "failures": len(failures),
            "by_category": by_cat,
            "recovery_attempts": len(rec_events),
            "recovered": done is not None and len(failures) > 0,
            "time_to_recovery_s": time_to_recovery,
            "recovery_tokens": rec_tokens,
            "repeated": repeated,
            "failure_list": failures[-10:],
        }

    tot_fail = sum(t["failures"] for t in tasks.values())
    tot_rec = sum(t["recovery_attempts"] for t in tasks.values())
    rec_tasks = [t for t in tasks.values() if t["failures"] > 0]
    summary = {
        "tasks_with_failures": len(rec_tasks),
        "total_failures": tot_fail,
        "total_recovery_attempts": tot_rec,
        "recovery_success_rate": (sum(1 for t in rec_tasks if t["recovered"]) / len(rec_tasks))
        if rec_tasks else None,
        "total_recovery_tokens": sum(t["recovery_tokens"] for t in tasks.values()),
    }
    return {"tasks": tasks, "summary": summary}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Failure taxonomy + recovery analytics")
    parser.add_argument("--events", default=None)
    parser.add_argument("--toolcalls", default=None)
    args = parser.parse_args(argv)
    from manager import EVENTS_FILE as _E, TOOLCALLS_FILE as _T  # noqa
    result = analyze(read_jsonl(args.events or _E), read_jsonl(args.toolcalls or _T))
    s = result["summary"]
    print(f"Failures: {s['total_failures']} in {s['tasks_with_failures']} tasks, "
          f"recovery success {s['recovery_success_rate']}, "
          f"recovery tokens {s['total_recovery_tokens']}")
    for tid, t in sorted(result["tasks"].items()):
        if t["failures"]:
            print(f"  {tid}: {t['failures']} failures {t['by_category']}, "
                  f"recovered={t['recovered']}, repeated={[r['category'] for r in t['repeated']]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
