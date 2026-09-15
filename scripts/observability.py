"""observability.py — Manager Integration Contract (roadmap Phase 4, P1 step 5).

Writes `.agent/manager/observability.json` (schema_version 1): Scopeboard
owns observations + derived recommendations, the Manager owns actions and
must never rewrite historical events.

Usage: python scripts/observability.py [--task TASK-xxx] [--out PATH] [--print]
"""
import os
import sys
import json
import argparse
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
ROOT = os.path.dirname(HERE)
MANAGER_DIR = os.path.join(ROOT, ".agent", "manager")
EVENTS_FILE = os.path.join(MANAGER_DIR, "events.jsonl")
TOOLCALLS_FILE = os.path.join(MANAGER_DIR, "tool-calls.jsonl")
CHECKPOINT_FILE = os.path.join(ROOT, ".agent", "building", "checkpoint.json")
CONFIG_FILE = os.path.join(MANAGER_DIR, "config.json")
OUT_FILE = os.path.join(MANAGER_DIR, "observability.json")

SCHEMA_VERSION = 1

from graph import build_graph  # noqa: E402
from risk import assess as assess_risk, DEFAULT_THRESHOLDS  # noqa: E402


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


def load_json(path, default=None):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def pick_task(graph, task_id=None):
    tasks = graph.get("tasks", []) or []
    if task_id:
        return next((t for t in tasks if t["task"] == task_id), None)
    running = [t for t in tasks if t.get("terminal", {}).get("state") == "running"]
    if running:
        running.sort(key=lambda t: str(t.get("last_seen") or ""), reverse=True)
        return running[0]
    if tasks:
        tasks_sorted = sorted(tasks, key=lambda t: str(t.get("last_seen") or ""), reverse=True)
        return tasks_sorted[0]
    return None


def build_contract(task_id=None, events=None, toolcalls=None, checkpoint=None,
                   config=None, max_review_cycles=3):
    events = events if events is not None else read_jsonl(EVENTS_FILE)
    toolcalls = toolcalls if toolcalls is not None else read_jsonl(TOOLCALLS_FILE)
    checkpoint = checkpoint if checkpoint is not None else (
        load_json(CHECKPOINT_FILE, default={}) or {})
    config = config if config is not None else (load_json(CONFIG_FILE, default={}) or {})
    try:
        max_review_cycles = int(((config.get("limits", {}) or {})
                                 .get("max_review_cycles", max_review_cycles)))
    except Exception:
        pass

    graph = build_graph(events, toolcalls)
    node = pick_task(graph, task_id)
    risk = assess_risk(toolcalls, events, checkpoint, config)

    recommendations = []
    contract = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "task_id": node["task"] if node else task_id,
        "status": (node.get("terminal", {}).get("state") if node else "unknown"),
        "session": None,
        "recommendations": recommendations,
    }
    if node is None:
        return contract

    sessions = node.get("sessions", []) or []
    latest = sessions[-1] if sessions else None
    risk_level = "ok"
    if latest:
        match = next((s for s in risk.get("sessions", [])
                      if s.get("session_id") == latest.get("session_id")), None)
        if match:
            risk_level = match.get("level", "ok")
        contract["session"] = {
            "id": latest.get("session_id"),
            "agent": latest.get("agent"),
            "tool_calls": latest.get("tool_calls", 0),
            "duration_ms": None,
            "risk": risk_level,
        }
        t0 = latest.get("started")
        t1 = latest.get("last_seen")
        try:
            from datetime import datetime as _dt
            a = _dt.fromisoformat(str(t0).replace("Z", "+00:00"))
            b = _dt.fromisoformat(str(t1).replace("Z", "+00:00"))
            contract["session"]["duration_ms"] = int((b - a).total_seconds() * 1000)
        except Exception:
            pass

    if risk_level == "high":
        contract["session"] and recommendations.append({
            "type": "SESSION_ROLLOVER", "priority": "high",
            "reason": "latest session risk is high (tool-call budget, errors or stale checkpoint)"})
    if (node.get("review_loop", 0) or 0) >= max(1, max_review_cycles - 1):
        recommendations.append({
            "type": "REVIEW", "priority": "high" if node.get("review_loop", 0) >= max_review_cycles else "medium",
            "reason": f"review_loop={node.get('review_loop', 0)} near limit {max_review_cycles}"})
    if node.get("terminal", {}).get("state") == "running" and not sessions:
        recommendations.append({
            "type": "CHECKPOINT", "priority": "medium",
            "reason": "task running with no recorded session: write a checkpoint"})
    return contract


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build observability.json contract")
    parser.add_argument("--task", default=None)
    parser.add_argument("--out", default=OUT_FILE)
    parser.add_argument("--print", dest="do_print", action="store_true")
    args = parser.parse_args(argv)
    contract = build_contract(task_id=args.task)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(contract, f, indent=2, ensure_ascii=False)
    print(f"Contract: task={contract['task_id']} status={contract['status']} "
          f"risk={(contract['session'] or {}).get('risk')} "
          f"recommendations={len(contract['recommendations'])} -> {args.out}")
    if args.do_print:
        print(json.dumps(contract, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
