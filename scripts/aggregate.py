"""aggregate.py — รวมข้อมูลวัดผลข้ามโปรเจกต์เป็นจอเดียว (read-only).

อ่าน metrics.json + events.jsonl + tool-calls.jsonl + queue.json + reviews/ +
history/ + checkpoint ของแต่ละโปรเจกต์ แล้วเขียน dashboard/all-projects.json
ค่า default: ["Agent", "mcp", "Dashboard"] ใต้โฟลเดอร์แม่ของรีโปนี้ (ย้ายรีโปได้)
ใช้: python scripts/aggregate.py [--projects A,B,C] [--out dashboard/all-projects.json]
"""
import os
import sys
import json
import argparse
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
ROOT = os.path.dirname(HERE)               # repo root ของรีโปที่สคริปต์นี้อยู่
BASE = os.path.dirname(ROOT)               # D:/Coding_Project (หรือที่ไหนก็ได้)
DEFAULT_OUT = os.path.join(ROOT, "dashboard", "all-projects.json")
OUT_DEFAULT = DEFAULT_OUT
TIMELINE_CAP = 500


def default_projects(base=None):
    """โปรเจกต์พี่น้องที่มี .agent/manager/metrics.json (เรียงชื่อ) —
    โคลนไปวางที่ไหนก็เจอกันเอง ไม่ต้องระบุ --projects"""
    base = base or BASE
    found = []
    if os.path.isdir(base):
        for name in sorted(os.listdir(base)):
            if name.startswith("."):
                continue
            if os.path.exists(os.path.join(base, name, ".agent", "manager", "metrics.json")):
                found.append(os.path.join(base, name))
    return found or [ROOT]

from export_json import (  # noqa: E402
    read_jsonl, load_json, read_reviews, read_history, build_extra,
)
from tools_inventory import build_tools_section  # noqa: E402


def collect_project(root):
    """อ่าน 1 โปรเจกต์ → dict (ทนไฟล์หาย/เสีย: ok=False แทนการพัง)."""
    name = os.path.basename(os.path.normpath(root))
    mgr = os.path.join(root, ".agent", "manager")
    metrics = load_json(os.path.join(mgr, "metrics.json"))
    if not metrics:
        return {"name": name, "root": root, "ok": False,
                "error": "metrics.json not found — run metrics.py --rebuild in that project first"}
    events = read_jsonl(os.path.join(mgr, "events.jsonl"))
    toolcalls = read_jsonl(os.path.join(mgr, "tool-calls.jsonl"))
    queue = load_json(os.path.join(mgr, "queue.json"), default={}) or {}
    reviews = read_reviews(os.path.join(mgr, "reviews"))
    history = read_history(os.path.join(mgr, "history"))
    checkpoint = load_json(os.path.join(root, ".agent", "building", "checkpoint.json"), default={}) or {}
    config = load_json(os.path.join(mgr, "config.json"), default={}) or {}
    totals = metrics.get("totals", {}) or {}
    per_task_raw = metrics.get("per_task", {}) or {}
    per_task = [{
        "task": t, "done": bool((v or {}).get("done", False)),
        "sessions": int((v or {}).get("sessions_per_task", 0) or 0),
        "tool_calls": int((v or {}).get("tool_calls_per_task", 0) or 0),
        "recovery": int((v or {}).get("recovery_count", 0) or 0),
        "review_loop": int((v or {}).get("review_loop", 0) or 0),
        "time_to_DONE": (v or {}).get("time_to_DONE"),
        "tokens": int((v or {}).get("tokens_total", 0) or 0),
    } for t, v in sorted(per_task_raw.items())]
    extra = build_extra(events, toolcalls, queue, reviews, history,
                        checkpoint.get("files_changed", []), config, per_task)
    return {"name": name, "root": root, "ok": True, "totals": totals,
            "per_task": per_task, "extra": extra, "events": events,
            "history": history, "tools": build_tools_section(toolcalls)}


def build_payload(projects):
    collected = [collect_project(p) for p in projects]
    ok = [c for c in collected if c.get("ok")]

    def s(key):
        return sum(int((c.get("totals", {}) or {}).get(key, 0) or 0) for c in ok)

    done, failed, cancelled = s("done"), s("failed"), s("cancelled")
    terminal = done + failed + cancelled
    tokens = sum((c.get("extra", {}) or {}).get("tokens_total", 0) for c in ok)
    open_alerts = sum((c.get("extra", {}) or {}).get("open_alerts", 0) for c in ok)
    combined = {
        "projects": len(ok),
        "total_tasks": s("tasks"),
        "done": done,
        "failed": failed,
        "cancelled": cancelled,
        "success_rate": (done / terminal) if terminal else 0.0,
        "total_tool_calls": s("tool_calls"),
        "recoveries": s("recoveries"),
        "review_fails": s("review_loops"),
        "tokens_total": tokens,
        "open_alerts": open_alerts,
    }

    per_project = []
    for c in collected:
        t = c.get("totals", {}) or {}
        ex = c.get("extra", {}) or {}
        per_project.append({
            "project": c["name"], "ok": bool(c.get("ok")),
            "tasks": int(t.get("tasks", 0) or 0), "done": int(t.get("done", 0) or 0),
            "tool_calls": int(t.get("tool_calls", 0) or 0),
            "tokens": int(ex.get("tokens_total", 0) or 0),
            "recoveries": int(t.get("recoveries", 0) or 0),
            "review_fails": int(t.get("review_loops", 0) or 0),
            "open_alerts": int(ex.get("open_alerts", 0) or 0),
            "queue": (ex.get("queue", {}) or {}).get("by_status", {}),
        })

    per_task = []
    for c in ok:
        for row in c.get("per_task", []) or []:
            per_task.append({"project": c["name"], **row})
    per_task.sort(key=lambda r: (r["project"], r["task"]))

    timeline = []
    for c in ok:
        for e in c.get("events", []) or []:
            if isinstance(e, dict) and e.get("event"):
                timeline.append({"time": e.get("time"), "event": e.get("event"),
                                 "task": e.get("task"), "project": c["name"]})
    timeline.sort(key=lambda e: str(e.get("time") or ""))
    timeline = timeline[-TIMELINE_CAP:]

    recent_activity, errors, alerts, findings_recent = [], {"count": 0, "recent": []}, [], []
    sev_total = {"critical": 0, "major": 0, "minor": 0}
    files, agents, durations = {}, {}, {}
    slowest, trend_by_date = [], {}
    for c in ok:
        ex = c.get("extra", {}) or {}
        for a in ex.get("recent_activity", []) or []:
            recent_activity.append({"project": c["name"], **a})
        for r in (ex.get("errors", {}) or {}).get("recent", []) or []:
            errors["recent"].append({"project": c["name"], **r})
        errors["count"] += int((ex.get("errors", {}) or {}).get("count", 0) or 0)
        for a in ex.get("alerts", []) or []:
            alerts.append({"project": c["name"], **a})
        for f in (ex.get("findings", {}) or {}).get("recent", []) or []:
            findings_recent.append({"project": c["name"], **f})
        for k, v in ((ex.get("findings", {}) or {}).get("by_severity", {}) or {}).items():
            sev_total[k] = sev_total.get(k, 0) + int(v or 0)
        for f in ex.get("files_changed", []) or []:
            files[f] = files.get(f, 0) + 1
        for a in ex.get("agents", []) or []:
            g = agents.setdefault(a["agent"], {"calls": 0, "sessions": 0, "errors": 0, "tokens": 0})
            for k in g:
                g[k] += int(a.get(k, 0) or 0)
        for d in (ex.get("durations", {}) or {}).get("per_tool", []) or []:
            g = durations.setdefault(d["tool"], {"calls": 0, "ms_sum": 0.0, "p95_max": 0})
            g["calls"] += int(d.get("calls", 0) or 0)
            g["ms_sum"] += float(d.get("avg_ms", 0) or 0) * int(d.get("calls", 0) or 0)
            g["p95_max"] = max(g["p95_max"], float(d.get("p95_ms", 0) or 0))
        for t in ex.get("slowest_tasks", []) or []:
            slowest.append({"project": c["name"], **t})
        for h in c.get("history", []) or []:
            g = trend_by_date.setdefault(h.get("date"), {})
            g[c["name"]] = {"done": ((h.get("totals", {}) or {}).get("done", 0)),
                            "tool_calls": ((h.get("totals", {}) or {}).get("tool_calls", 0))}
    recent_activity.sort(key=lambda e: str(e.get("time") or ""), reverse=True)
    recent_activity = recent_activity[:20]
    slowest = sorted([t for t in slowest if t.get("time_to_DONE") is not None],
                     key=lambda t: -(t.get("time_to_DONE") or 0))[:10]
    trend = [{"date": d, "projects": trend_by_date[d]} for d in sorted(trend_by_date)]
    files_top = sorted(files.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
    files_top = [{"file": f, "projects": n} for f, n in files_top]

    tools_agents = {}
    merged_tools = {}
    for c in ok:
        t = c.get("tools", {}) or {}
        if not tools_agents and t.get("agents"):
            tools_agents = t["agents"]
        for m in t.get("matrix", []) or []:
            g = merged_tools.setdefault(m["tool"], {
                "tool": m["tool"], "category": m.get("category", "?"),
                "desc": m.get("desc", ""), "allowed_by": m.get("allowed_by", []),
                "denied_by": m.get("denied_by", []), "calls": 0, "errors": 0,
                "agents": {}, "last_used": None})
            g["calls"] += int(m.get("calls", 0) or 0)
            g["errors"] += int(m.get("errors", 0) or 0)
            for ag, n in (m.get("agents", {}) or {}).items():
                g["agents"][ag] = g["agents"].get(ag, 0) + int(n or 0)
            if m.get("last_used") and (g["last_used"] is None
                                       or str(m["last_used"]) > str(g["last_used"])):
                g["last_used"] = m["last_used"]
    tools = {"catalog_n": (ok[0].get("tools", {}) or {}).get("catalog_n", 0) if ok else 0,
             "matrix": [merged_tools[k] for k in sorted(merged_tools)],
             "agents": tools_agents}

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "projects": [{"name": c["name"], "root": c["root"], "ok": c.get("ok", False),
                      "error": c.get("error"),
                      "totals": c.get("totals", {}) if c.get("ok") else {}}
                     for c in collected],
        "combined": combined,
        "per_project": per_project,
        "per_task": per_task,
        "timeline": timeline,
        "recent_activity": recent_activity,
        "errors": errors,
        "agents": [{"agent": k, **v} for k, v in sorted(agents.items())],
        "durations": {"per_tool": [
            {"tool": k, "calls": v["calls"],
             "avg_ms": round(v["ms_sum"] / v["calls"], 1) if v["calls"] else 0,
             "p95_ms": v["p95_max"]}
            for k, v in sorted(durations.items(), key=lambda kv: -kv[1]["calls"])[:10]]},
        "slowest_tasks": slowest,
        "findings": {"total": sum(sev_total.values()), "by_severity": sev_total,
                     "recent": sorted(findings_recent, key=lambda r: str(r.get("time") or ""),
                                      reverse=True)[:10]},
        "files_top": files_top,
        "alerts": alerts,
        "trend": trend,
        "tools": tools,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Aggregate cross-project metrics")
    parser.add_argument("--projects", default=None,
                        help="Comma-separated project roots (default: auto-scan siblings with metrics.json)")
    parser.add_argument("--out", default=OUT_DEFAULT, help="Output JSON path")
    args = parser.parse_args(argv)
    projects = ([p.strip() for p in args.projects.split(",") if p.strip()]
                if args.projects else default_projects())
    payload = build_payload(projects)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    c = payload["combined"]
    print(f"Aggregated: {c['projects']} projects, {c['total_tasks']} tasks, "
          f"{c['done']} done, success_rate {c['success_rate']:.2f}, "
          f"{c['total_tool_calls']} tool calls, {c['tokens_total']} tokens, "
          f"{c['open_alerts']} open alerts -> {args.out}")
    for p in payload["projects"]:
        print(f"  {p['name']}: {'OK' if p['ok'] else 'SKIP (' + str(p.get('error')) + ')'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
