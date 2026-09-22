"""bootstrap.py — เตรียมโปรเจกต์ให้ดูแดชบอร์ดได้ทันทีหลังโคลน (read-only ต่อข้อมูลเดิม).

ใช้:
  python scripts/bootstrap.py                 # สร้างโครง .agent/ + config ตั้งต้น (ไม่แตะของเดิม)
  python scripts/bootstrap.py --demo          # แถมข้อมูลตัวอย่าง → เปิดจอเห็นกราฟทันที
  python scripts/bootstrap.py --force         # เขียนทับไฟล์เดิมทั้งหมด

ไม่ต้อง pip install อะไร (stdlib ล้วน) รันจากที่ไหนก็ได้ด้วย --root
"""
import os
import sys
import json
import argparse
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def write_json(path, data, force):
    if os.path.exists(path) and not force:
        print(f"  skip (exists): {path}")
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"  wrote: {path}")
    return True


def append_jsonl(path, rows, force):
    if os.path.exists(path) and not force:
        print(f"  skip (exists): {path}")
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"  wrote: {path} ({len(rows)} rows)")
    return True


def default_config():
    return {
        "limits": {"max_worker_sessions": 5, "max_review_cycles": 3,
                   "max_retries": 3, "max_runtime_minutes": 120},
        "watchdog": {"heartbeat_timeout_seconds": 120,
                     "progress_timeout_seconds": 600,
                     "checkpoint_timeout_seconds": 900,
                     "worker_start_timeout_seconds": 60},
        "approval": {"require_for_destructive": True,
                     "require_for_architecture_change": True},
        "git": {"auto_commit": False},
    }


def sample_queue():
    return {"schema_version": 2, "tasks": [
        {"id": "TASK-001", "type": "feature", "title": "Demo task one",
         "priority": "high", "status": "completed", "dependencies": []},
        {"id": "TASK-002", "type": "feature", "title": "Demo task two",
         "priority": "high", "status": "completed", "dependencies": ["TASK-001"]},
        {"id": "TASK-003", "type": "feature", "title": "Demo task three (running)",
         "priority": "medium", "status": "running", "dependencies": ["TASK-002"]},
    ]}


def demo_rows(now):
    def t(minutes_ago):
        return (now - timedelta(minutes=minutes_ago)).isoformat()

    events = [
        {"event": "TASK_CREATED", "task": "TASK-001", "time": t(180)},
        {"event": "WORKER_STARTED", "task": "TASK-001", "time": t(175),
         "session": "ses-demo-1", "agent": "building", "attempt": 1},
        {"event": "PROGRESS", "task": "TASK-001", "time": t(150),
         "milestone": "half done"},
        {"event": "TASK_DONE", "task": "TASK-001", "time": t(120)},
        {"event": "REVIEW_PASSED", "task": "TASK-001", "time": t(119),
         "verdict": "PASS"},
        {"event": "TASK_CREATED", "task": "TASK-002", "time": t(110)},
        {"event": "WORKER_STARTED", "task": "TASK-002", "time": t(105),
         "session": "ses-demo-2", "agent": "building", "attempt": 1},
        {"event": "REVIEW_FAILED", "task": "TASK-002", "time": t(60),
         "findings": [{"severity": "HIGH", "file": "demo.py",
                       "issue": "missing validation",
                       "required_action": "add validation"}]},
        {"event": "WORKER_RESUMED", "task": "TASK-002", "time": t(55),
         "session": "ses-demo-3", "agent": "building", "attempt": 2},
        {"event": "TASK_DONE", "task": "TASK-002", "time": t(30)},
        {"event": "TASK_CREATED", "task": "TASK-003", "time": t(25)},
        {"event": "WORKER_STARTED", "task": "TASK-003", "time": t(20),
         "session": "ses-demo-4", "agent": "planning", "attempt": 1},
        {"event": "PROGRESS", "task": "TASK-003", "time": t(5),
         "milestone": "design draft"},
    ]
    calls = [
        {"time": t(174), "task": "TASK-001", "attempt": 1, "session_id": "ses-demo-1",
         "agent": "building", "operation": "DISPATCH", "tool": "Task",
         "duration_ms": 0, "status": "ok", "error": None,
         "prompt_hash": "demo00000001", "tokens_in": 1200, "tokens_out": 300},
        {"time": t(160), "task": "TASK-001", "attempt": 1, "session_id": "ses-demo-1",
         "agent": "building", "operation": "CALL", "tool": "Read",
         "duration_ms": 45, "status": "ok", "error": None,
         "prompt_hash": "demo00000002", "tokens_in": 800, "tokens_out": 200},
        {"time": t(140), "task": "TASK-001", "attempt": 1, "session_id": "ses-demo-1",
         "agent": "building", "operation": "CALL", "tool": "Edit",
         "duration_ms": 210, "status": "ok", "error": None,
         "prompt_hash": "demo00000003", "tokens_in": 1500, "tokens_out": 900},
        {"time": t(100), "task": "TASK-002", "attempt": 1, "session_id": "ses-demo-2",
         "agent": "building", "operation": "CALL", "tool": "Bash",
         "duration_ms": 1500, "status": "error", "error": "demo: tests failed (1)",
         "prompt_hash": "demo00000004", "tokens_in": 400, "tokens_out": 100},
        {"time": t(50), "task": "TASK-002", "attempt": 2, "session_id": "ses-demo-3",
         "agent": "building", "operation": "CALL", "tool": "Bash",
         "duration_ms": 900, "status": "ok", "error": None,
         "prompt_hash": "demo00000005", "tokens_in": 400, "tokens_out": 150},
        {"time": t(18), "task": "TASK-003", "attempt": 1, "session_id": "ses-demo-4",
         "agent": "planning", "operation": "CALL", "tool": "Read",
         "duration_ms": 60, "status": "ok", "error": None,
         "prompt_hash": "demo00000006", "tokens_in": 2000, "tokens_out": 500},
    ]
    return events, calls


def main(argv=None):
    parser = argparse.ArgumentParser(description="Init project for dashboard viewing")
    parser.add_argument("--root", default=os.path.dirname(HERE),
                        help="Project root (default: parent of scripts/)")
    parser.add_argument("--demo", action="store_true",
                        help="Also write sample data so graphs show immediately")
    parser.add_argument("--force", action="store_true",
                        help="Overwrite existing files")
    args = parser.parse_args(argv)
    root = os.path.abspath(args.root)
    mgr = os.path.join(root, ".agent", "manager")
    now = datetime.now(timezone.utc)

    print(f"[bootstrap] root: {root}")
    write_json(os.path.join(mgr, "config.json"), default_config(), args.force)
    write_json(os.path.join(mgr, "queue.json"), sample_queue(), args.force)
    write_json(os.path.join(root, ".agent", "building", "checkpoint.json"), {
        "task_id": "TASK-003", "status": "running", "phase": "planning",
        "completed": ["TASK-001", "TASK-002"], "current": "TASK-003 design draft",
        "remaining": ["TASK-003 implementation"], "files_changed": ["demo.py"],
        "tests": {"status": "passed", "details": "demo"},
        "blockers": [], "next_action": "implement demo",
        "progress": {"last_progress_at": now.isoformat()},
        "updated_at": now.isoformat(),
    }, args.force)

    if args.demo:
        events, calls = demo_rows(now)
        append_jsonl(os.path.join(mgr, "events.jsonl"), events, args.force)
        append_jsonl(os.path.join(mgr, "tool-calls.jsonl"), calls, args.force)
        sys.path.insert(0, os.path.join(root, "scripts"))
        import metrics as metrics_mod
        import export_json as export_mod
        import manager as manager_mod
        manager_mod.MANAGER_DIR = mgr
        manager_mod.EVENTS_FILE = os.path.join(mgr, "events.jsonl")
        metrics_mod.MANAGER_DIR = mgr
        metrics_mod.EVENTS_FILE = os.path.join(mgr, "events.jsonl")
        metrics_mod.TOOLCALLS_FILE = os.path.join(mgr, "tool-calls.jsonl")
        metrics_mod.METRICS_FILE = os.path.join(mgr, "metrics.json")
        metrics_mod.HISTORY_DIR = os.path.join(mgr, "history")
        manager_mod.save_review("TASK-002", "failed", findings=[
            {"severity": "HIGH", "file": "demo.py", "issue": "missing validation",
             "required_action": "add validation"}])
        metrics_mod.rebuild()
        export_mod.export_data(
            os.path.join(root, "dashboard", "data.json"),
            metrics_path=os.path.join(mgr, "metrics.json"),
            events_path=os.path.join(mgr, "events.jsonl"),
            toolcalls_path=os.path.join(mgr, "tool-calls.jsonl"),
            queue_path=os.path.join(mgr, "queue.json"),
            reviews_dir=os.path.join(mgr, "reviews"),
            history_dir=os.path.join(mgr, "history"),
            checkpoint_path=os.path.join(root, ".agent", "building", "checkpoint.json"),
            config_path=os.path.join(mgr, "config.json"))

    print("[bootstrap] done. Next: run_dashboard.bat (or: python scripts/metrics.py "
          "--rebuild; python scripts/export_json.py; python -m http.server)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
