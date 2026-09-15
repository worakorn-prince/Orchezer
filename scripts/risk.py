"""risk.py — deterministic session-risk signals (roadmap Phase 3, P0).

OBSERVATION vs RECOMMENDATION are kept separate on purpose: Scopeboard
observes and recommends, the Manager owns actions. No ML, no opaque
scores — every signal cites its inputs. All thresholds configurable.
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
TOOLCALLS_FILE = os.path.join(MANAGER_DIR, "tool-calls.jsonl")
EVENTS_FILE = os.path.join(MANAGER_DIR, "events.jsonl")
CHECKPOINT_FILE = os.path.join(ROOT, ".agent", "building", "checkpoint.json")
CONFIG_FILE = os.path.join(MANAGER_DIR, "config.json")

DEFAULT_THRESHOLDS = {
    "warn_calls": 60,
    "high_calls": 90,
    "error_rate_warn": 0.2,
    "error_rate_high": 0.4,
    "checkpoint_age_min_warn": 15,
    "checkpoint_age_min_high": 30,
    "min_history_sessions": 3,
}


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


def parse_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def median(values):
    s = sorted(values)
    if not s:
        return None
    mid = len(s) // 2
    return float(s[mid]) if len(s) % 2 else (s[mid - 1] + s[mid]) / 2.0


def session_stats(toolcalls):
    stats = {}
    for r in toolcalls or []:
        if not isinstance(r, dict):
            continue
        sid = r.get("session_id") or "?"
        s = stats.setdefault(sid, {"session_id": sid, "agent": r.get("agent") or "?",
                                   "task": r.get("task"), "calls": 0, "errors": 0,
                                   "tokens": 0, "first": None, "last": None})
        s["calls"] += 1
        if r.get("status") == "error":
            s["errors"] += 1
        s["tokens"] += int(r.get("tokens_in") or 0) + int(r.get("tokens_out") or 0)
        if r.get("agent") and s["agent"] == "?":
            s["agent"] = r["agent"]
        if r.get("time"):
            if s["first"] is None or str(r["time"]) < str(s["first"]):
                s["first"] = r["time"]
            if s["last"] is None or str(r["time"]) > str(s["last"]):
                s["last"] = r["time"]
    for s in stats.values():
        t0, t1 = parse_time(s["first"]), parse_time(s["last"])
        s["duration_s"] = round((t1 - t0).total_seconds(), 1) if t0 and t1 else None
        s["error_rate"] = round(s["errors"] / s["calls"], 3) if s["calls"] else 0.0
    return stats


def assess_session(sid, s, median_calls, checkpoint_age_min, th):
    observations = [f"session {sid} ({s['agent']}) made {s['calls']} tool calls "
                    f"with {s['errors']} errors (rate {s['error_rate']})"]
    recommendations = []
    level = "ok"
    if median_calls is not None:
        observations.append(f"historical median per session: {median_calls} calls")
    else:
        observations.append("not enough history to compare (low data)")
    if s["calls"] >= th["high_calls"]:
        level = "high"
        recommendations.append("Create checkpoint now and prepare a new session "
                               f"(calls {s['calls']} >= high threshold {th['high_calls']})")
    elif s["calls"] >= th["warn_calls"]:
        level = "watch"
        recommendations.append("Plan checkpoint soon "
                               f"(calls {s['calls']} >= warn threshold {th['warn_calls']})")
    if s["error_rate"] >= th["error_rate_high"]:
        level = "high"
        recommendations.append(f"Error rate {s['error_rate']} >= {th['error_rate_high']}: "
                               "inspect last errors before continuing")
    elif s["error_rate"] >= th["error_rate_warn"] and s["calls"] >= 5:
        if level == "ok":
            level = "watch"
        recommendations.append(f"Error rate {s['error_rate']} rising: review recent failures")
    if checkpoint_age_min is not None:
        observations.append(f"checkpoint age: {checkpoint_age_min} min")
        if checkpoint_age_min >= th["checkpoint_age_min_high"]:
            level = "high" if level != "ok" else "watch"
            recommendations.append("Checkpoint is stale: write a fresh checkpoint")
    return {"session_id": sid, "agent": s["agent"], "task": s.get("task"),
            "level": level, "observation": observations,
            "recommendation": recommendations,
            "signals": {"calls": s["calls"], "errors": s["errors"],
                        "error_rate": s["error_rate"], "tokens": s["tokens"],
                        "duration_s": s.get("duration_s"),
                        "median_calls": median_calls,
                        "checkpoint_age_min": checkpoint_age_min}}


def assess(toolcalls=None, events=None, checkpoint=None, config=None):
    toolcalls = toolcalls if toolcalls is not None else read_jsonl(TOOLCALLS_FILE)
    checkpoint = checkpoint if checkpoint is not None else (
        load_json(CHECKPOINT_FILE, default={}) or {})
    config = config if config is not None else (load_json(CONFIG_FILE, default={}) or {})
    th = dict(DEFAULT_THRESHOLDS)
    th.update((config.get("risk", {}) or {}))

    stats = session_stats(toolcalls)
    hist = [s["calls"] for s in stats.values() if s["calls"] > 0]
    median_calls = median(hist) if len(hist) >= th["min_history_sessions"] else None

    cp_age = None
    cp_time = ((checkpoint.get("progress", {}) or {}).get("last_progress_at")
               or checkpoint.get("updated_at"))
    dt = parse_time(cp_time)
    if dt:
        cp_age = round((datetime.now(timezone.utc) - dt).total_seconds() / 60.0, 1)

    sessions = [assess_session(sid, s, median_calls, cp_age, th)
                for sid, s in sorted(stats.items())]
    worst = "high" if any(s["level"] == "high" for s in sessions) \
        else "watch" if any(s["level"] == "watch" for s in sessions) else "ok"
    return {"thresholds": th, "history_sessions": len(hist),
            "median_calls": median_calls, "overall": worst, "sessions": sessions}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Deterministic session-risk signals")
    args = parser.parse_args(argv)
    result = assess()
    print(f"Risk: {result['overall']} "
          f"({len(result['sessions'])} sessions, median {result['median_calls']})")
    for s in result["sessions"]:
        print(f"  [{s['level'].upper()}] {s['session_id']} ({s['agent']}): "
              f"{s['signals']['calls']} calls, err {s['signals']['error_rate']}")
        for r in s["recommendation"]:
            print(f"    -> {r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
