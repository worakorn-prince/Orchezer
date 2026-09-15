"""graph.py — Execution Graph (roadmap Phase 1, P0).

Builds Task -> Attempt -> Session -> Agent -> Tool Calls/Events from raw
logs WITHOUT any schema change (all fields already exist). Unknown events
are carried in the timeline and never break the graph.

Usage: python scripts/graph.py [--task TASK-xxx]  (prints ASCII tree)
       from graph import build_graph  (events, toolcalls) -> dict
"""
import os
import sys
import json
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
ROOT = os.path.dirname(HERE)
MANAGER_DIR = os.path.join(ROOT, ".agent", "manager")
EVENTS_FILE = os.path.join(MANAGER_DIR, "events.jsonl")
TOOLCALLS_FILE = os.path.join(MANAGER_DIR, "tool-calls.jsonl")

SESSION_EVENTS = ("WORKER_STARTED", "WORKER_RESUMED")
RECOVERY_EVENTS = ("RECOVERY_STARTED", "RECOVERY_READY")
REVIEW_EVENTS = ("REVIEW_FAILED", "REVIEW_PASSED")
TERMINAL_EVENTS = ("TASK_DONE", "TASK_FAILED", "TASK_CANCELLED", "TASK_CANCELED")
TIMELINE_CAP = 200


def _is_excluded_task(task):
    return isinstance(task, str) and (task.startswith("TEST-") or task.startswith("DEP-"))


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


def build_graph(events, toolcalls):
    """Return {tasks: [task nodes...], summary: {...}} sorted by task id."""
    events = [e for e in (events or []) if isinstance(e, dict) and e.get("task")
              and not _is_excluded_task(e.get("task"))]
    toolcalls = [r for r in (toolcalls or []) if isinstance(r, dict) and r.get("task")
                 and not _is_excluded_task(r.get("task"))]
    task_ids = sorted({e.get("task") for e in events} | {r.get("task") for r in toolcalls})

    ev_by_task = {}
    for e in events:
        ev_by_task.setdefault(e.get("task"), []).append(e)
    call_by_task = {}
    for r in toolcalls:
        call_by_task.setdefault(r.get("task"), []).append(r)

    tasks = []
    for tid in task_ids:
        evs = ev_by_task.get(tid, [])
        calls = call_by_task.get(tid, [])

        attempts = sorted({int(r.get("attempt", 1) or 1) for r in calls
                           if str(r.get("attempt", "1")).lstrip("-").isdigit()})

        sessions = {}
        for e in evs:
            if e.get("event") in SESSION_EVENTS and e.get("session_id"):
                s = sessions.setdefault(e["session_id"], {
                    "session_id": e["session_id"], "agent": e.get("agent") or "?",
                    "started": e.get("time"), "last_seen": e.get("time"),
                    "tool_calls": 0, "errors": 0, "tokens": 0})
                if e.get("time") and (s["started"] is None or str(e["time"]) < str(s["started"])):
                    s["started"] = e["time"]
                    if e.get("agent"):
                        s["agent"] = e["agent"]
                if e.get("time") and (s["last_seen"] is None or str(e["time"]) > str(s["last_seen"])):
                    s["last_seen"] = e["time"]
        for r in calls:
            sid = r.get("session_id") or "?"
            s = sessions.setdefault(sid, {
                "session_id": sid, "agent": r.get("agent") or "?",
                "started": r.get("time"), "last_seen": r.get("time"),
                "tool_calls": 0, "errors": 0, "tokens": 0})
            s["tool_calls"] += 1
            if r.get("status") == "error":
                s["errors"] += 1
            s["tokens"] += int(r.get("tokens_in") or 0) + int(r.get("tokens_out") or 0)
            if r.get("time"):
                if s["started"] is None or str(r["time"]) < str(s["started"]):
                    s["started"] = r["time"]
                if s["last_seen"] is None or str(r["time"]) > str(s["last_seen"]):
                    s["last_seen"] = r["time"]
            if r.get("agent") and s["agent"] in ("?", None):
                s["agent"] = r["agent"]
        session_list = sorted(sessions.values(), key=lambda s: str(s["started"] or ""))

        review_chain = [{"time": e.get("time"), "event": e.get("event")}
                        for e in sorted(evs, key=lambda e: str(e.get("time") or ""))
                        if e.get("event") in REVIEW_EVENTS]
        recovery_chain = [{"time": e.get("time"), "event": e.get("event")}
                          for e in sorted(evs, key=lambda e: str(e.get("time") or ""))
                          if e.get("event") in RECOVERY_EVENTS]
        terminal = {"state": "running", "time": None}
        for e in sorted(evs, key=lambda e: str(e.get("time") or "")):
            if e.get("event") in TERMINAL_EVENTS:
                terminal = {"state": ("done" if e["event"] == "TASK_DONE"
                                      else "failed" if e["event"] == "TASK_FAILED"
                                      else "cancelled"),
                            "time": e.get("time")}

        timeline = ([{"time": e.get("time"), "kind": "event",
                      "event": e.get("event"), "session_id": e.get("session_id"),
                      "agent": e.get("agent")} for e in evs] +
                    [{"time": r.get("time"), "kind": "tool",
                      "tool": r.get("tool"), "operation": r.get("operation"),
                      "status": r.get("status"), "session_id": r.get("session_id"),
                      "agent": r.get("agent"), "attempt": r.get("attempt")} for r in calls])
        timeline.sort(key=lambda x: str(x.get("time") or ""))
        timeline = timeline[-TIMELINE_CAP:]

        times = [x["time"] for x in timeline if x.get("time")]
        tasks.append({
            "task": tid,
            "attempts": attempts,
            "n_attempts": len(attempts),
            "sessions": session_list,
            "n_sessions": len(session_list),
            "agents": sorted({s["agent"] for s in session_list}),
            "review_loop": sum(1 for e in evs if e.get("event") == "REVIEW_FAILED"),
            "review_chain": review_chain,
            "recovery_chain": recovery_chain,
            "terminal": terminal,
            "first_seen": min(times) if times else None,
            "last_seen": max(times) if times else None,
            "timeline": timeline,
        })

    summary = {
        "tasks": len(tasks),
        "completed": sum(1 for t in tasks if t["terminal"]["state"] == "done"),
        "failed": sum(1 for t in tasks if t["terminal"]["state"] == "failed"),
        "running": sum(1 for t in tasks if t["terminal"]["state"] == "running"),
        "total_sessions": sum(t["n_sessions"] for t in tasks),
        "total_attempts": sum(t["n_attempts"] for t in tasks),
    }
    return {"tasks": tasks, "summary": summary}


def render_ascii(node):
    """One task as an ASCII tree (Phase 1 §3.2 style)."""
    lines = [node["task"]]
    for i, s in enumerate(node["sessions"]):
        last = (i == len(node["sessions"]) - 1 and not node["review_chain"]
                and node["terminal"]["state"] == "running")
        branch = "`-- " if last else "|-- "
        lines.append(f"  {branch}{s['agent']} / {s['session_id']} "
                     f"({s['tool_calls']} calls, {s['errors']} errors)")
    for r in node["review_chain"]:
        lines.append(f"  |-- {r['event']}")
    term = node["terminal"]
    lines.append(f"  `-- {term['state'].upper()}" + (f" @ {term['time']}" if term["time"] else ""))
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Execution graph from raw logs")
    parser.add_argument("--task", default=None, help="Show ASCII tree for one task")
    parser.add_argument("--events", default=EVENTS_FILE)
    parser.add_argument("--toolcalls", default=TOOLCALLS_FILE)
    args = parser.parse_args(argv)
    graph = build_graph(read_jsonl(args.events), read_jsonl(args.toolcalls))
    print(f"Graph: {graph['summary']['tasks']} tasks, "
          f"{graph['summary']['total_sessions']} sessions, "
          f"{graph['summary']['total_attempts']} attempts")
    if args.task:
        node = next((t for t in graph["tasks"] if t["task"] == args.task), None)
        print(render_ascii(node) if node else f"Task {args.task}: no data")
    else:
        for t in graph["tasks"]:
            print(f"  {t['task']}: {t['n_sessions']} sessions, "
                  f"{t['n_attempts']} attempts, review_loop={t['review_loop']}, "
                  f"terminal={t['terminal']['state']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
