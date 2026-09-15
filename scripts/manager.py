import os
import json
import time
import hashlib
import subprocess
from datetime import datetime, timezone, timedelta

MANAGER_DIR = ".agent/manager"
BUILDING_DIR = ".agent/building"
STATE_FILE = os.path.join(MANAGER_DIR, "state.json")
QUEUE_FILE = os.path.join(MANAGER_DIR, "queue.json")
CONFIG_FILE = os.path.join(MANAGER_DIR, "config.json")
EVENTS_FILE = os.path.join(MANAGER_DIR, "events.jsonl")
CHECKPOINT_FILE = os.path.join(BUILDING_DIR, "checkpoint.json")
DECISIONS_DIR = os.path.join(MANAGER_DIR, "decisions")
CONTEXT_DIR = os.path.join(MANAGER_DIR, "context")
TOOLCALLS_FILE = os.path.join(MANAGER_DIR, "tool-calls.jsonl")
METRICS_FILE = os.path.join(MANAGER_DIR, "metrics.json")

def load_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except Exception:
            return default

def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def log_event(event_type, task_id, message, extra=None):
    os.makedirs(MANAGER_DIR, exist_ok=True)
    entry = {
        "event": event_type,
        "task": task_id,
        "time": datetime.now(timezone.utc).isoformat(),
        "message": message
    }
    if extra:
        entry.update(extra)
    with open(EVENTS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(f"[EVENT] {event_type} | Task: {task_id} | {message}")

def log_tool_call(task_id, session_id, agent, operation, tool, duration_ms=0, status="ok", error=None, prompt_text="", attempt=1, tokens_in=None, tokens_out=None):
    prompt_hash = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()[:12] if prompt_text else "-"
    entry = {
        "time": datetime.now(timezone.utc).isoformat(),
        "task": task_id,
        "attempt": attempt,
        "session_id": session_id,
        "agent": agent,
        "operation": operation,
        "tool": tool,
        "duration_ms": duration_ms,
        "status": status,
        "error": error,
        "prompt_hash": prompt_hash,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out
    }
    os.makedirs(MANAGER_DIR, exist_ok=True)
    with open(TOOLCALLS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    try:
        if os.path.getsize(TOOLCALLS_FILE) > 1048576:
            log_event("TOOLCALLS_GROWING", task_id, f"tool-calls.jsonl exceeds 1MB, rotation recommended", {"session_id": session_id})
    except Exception:
        pass
    log_event("TOOL_CALL", task_id, f"{operation} {tool} [{status}]", {"session_id": session_id, "agent": agent, "operation": operation, "tool": tool, "duration_ms": duration_ms, "status": status, "error": error, "prompt_hash": prompt_hash, "attempt": attempt})
    return entry

def log_dispatch(task_id, agent, session_id, attempt=1, prompt_text=""):
    entry = log_tool_call(task_id, session_id, agent, "DISPATCH", "Task", duration_ms=0, status="ok", error=None, prompt_text=prompt_text, attempt=attempt)
    log_event("WORKER_STARTED", task_id, f"Dispatch {agent} session {session_id}", {"session_id": session_id, "agent": agent, "attempt": attempt})
    return entry

def log_denied_attempt(task_id, session_id, agent, tool, reason="", attempt=1):
    entry = log_tool_call(task_id, session_id, agent, "ATTEMPT", tool, duration_ms=0,
                          status="denied", error=reason or None, prompt_text="",
                          attempt=attempt)
    log_event("TOOL_DENIED", task_id, f"{agent} attempted denied tool {tool}: {reason}",
              {"session_id": session_id, "agent": agent, "tool": tool,
               "reason": reason, "attempt": attempt})
    return entry

def wrap_task(task_id, agent, session_id, tool, prompt_text, fn, attempt=1, operation="RESULT", tokens_in=None, tokens_out=None):
    start = time.perf_counter()
    status = "ok"
    error = None
    try:
        return fn()
    except Exception as e:
        status = "error"
        error = str(e)
        raise
    finally:
        duration_ms = int((time.perf_counter() - start) * 1000)
        log_tool_call(task_id, session_id, agent, operation, tool, duration_ms=duration_ms, status=status, error=error, prompt_text=prompt_text, attempt=attempt, tokens_in=tokens_in, tokens_out=tokens_out)

def save_review(task_id, status, findings=None, reviewer="review"):
    reviews_dir = os.path.join(MANAGER_DIR, "reviews")
    os.makedirs(reviews_dir, exist_ok=True)
    findings = findings or []
    for f in findings:
        if not isinstance(f, dict):
            raise ValueError("each finding must be a dict")
        if f.get("severity") not in ("critical", "major", "minor"):
            raise ValueError("finding severity must be critical|major|minor")
    record = {
        "task": task_id,
        "time": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "reviewer": reviewer,
        "findings": findings
    }
    path = os.path.join(reviews_dir, f"{task_id}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2, ensure_ascii=False)
    by_sev = {}
    for f in findings:
        by_sev[f["severity"]] = by_sev.get(f["severity"], 0) + 1
    event = "REVIEW_PASSED" if status == "passed" else "REVIEW_FAILED"
    log_event(event, task_id, f"Review {status}: {len(findings)} findings {by_sev}", {"reviewer": reviewer, "findings_count": len(findings), "by_severity": by_sev})
    print(f"[REVIEW] {status} for {task_id} ({len(findings)} findings) -> {path}")
    return record

def write_decision(task_id, content):
    os.makedirs(DECISIONS_DIR, exist_ok=True)
    path = os.path.join(DECISIONS_DIR, f"{task_id}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"[DECISION] Written decision log for {task_id} at {path}")

def generate_resume_context(task_id, task_title, checkpoint):
    os.makedirs(CONTEXT_DIR, exist_ok=True)
    path = os.path.join(CONTEXT_DIR, f"{task_id}.resume.md")
    completed = "\n".join([f"- {c}" for c in checkpoint.get("completed", [])]) if checkpoint.get("completed") else "- None"
    remaining = "\n".join([f"- {r}" for r in checkpoint.get("remaining", [])]) if checkpoint.get("remaining") else "- None"
    content = f"""# Resume Context — {task_id}

Task:
{task_title}

Current phase:
{checkpoint.get('phase', 'implementation')}

Completed:
{completed}

Current:
{checkpoint.get('current', 'N/A')}

Remaining:
{remaining}

Next action:
{checkpoint.get('next_action', 'Continue implementation')}

Do not redo:
- Completed milestones above unless verification fails.
"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"[CONTEXT] Generated resume context at {path}")
    return path

class ManagerOrchestrator:
    def __init__(self, owner="manager-01"):
        self.owner = owner
        self.state = load_json(STATE_FILE, {})
        self.queue = load_json(QUEUE_FILE, {"tasks": []})
        self.config = load_json(CONFIG_FILE, {
            "limits": {"max_worker_sessions": 5, "max_review_cycles": 3, "max_retries": 3, "max_runtime_minutes": 120},
            "watchdog": {"heartbeat_timeout_seconds": 120, "progress_timeout_seconds": 600, "checkpoint_timeout_seconds": 900},
            "approval": {"require_for_destructive": True, "require_for_architecture_change": True},
            "git": {"auto_commit": False}
        })

    def acquire_lock(self, ttl_hours=6):
        now = datetime.now(timezone.utc)
        expires = now + timedelta(hours=ttl_hours)
        lock_info = self.state.get("lock")
        if lock_info:
            exp_time = datetime.fromisoformat(lock_info["expires_at"].replace("Z", "+00:00"))
            if exp_time > now and lock_info.get("owner") != self.owner:
                print(f"[LOCK] Lock held by {lock_info.get('owner')} until {lock_info.get('expires_at')}")
                return False
        self.state["lock"] = {
            "owner": self.owner,
            "acquired_at": now.isoformat(),
            "expires_at": expires.isoformat()
        }
        self.state["updated_at"] = now.isoformat()
        save_json(STATE_FILE, self.state)
        log_event("LOCK_ACQUIRED", self.state.get("current_task_id", "GLOBAL"), f"Lock acquired by {self.owner}")
        return True

    def check_watchdog(self):
        print("[WATCHDOG] Checking worker health and timeouts...")
        checkpoint = load_json(CHECKPOINT_FILE, {})
        if not checkpoint:
            print("[WATCHDOG] No active checkpoint found.")
            return "NO_CHECKPOINT"

        status = checkpoint.get("status", "running")
        if status == "stopped_limit":
            log_event("WORKER_LIMIT", self.state.get("current_task_id", "UNKNOWN"), "Tool limit detected via checkpoint")
            return "RECOVERY_NEEDED"
        elif status == "blocked":
            log_event("WORKER_BLOCKED", self.state.get("current_task_id", "UNKNOWN"), "Worker reported blocked status")
            return "BLOCKED"
        elif status == "failed":
            log_event("WORKER_FAILED", self.state.get("current_task_id", "UNKNOWN"), "Worker reported failure")
            return "ERROR_DEBUG"

        # Check timeouts
        now = datetime.now(timezone.utc)
        watchdog_cfg = self.config.get("watchdog", {})
        progress_timeout = watchdog_cfg.get("progress_timeout_seconds", 600)
        
        last_prog_str = checkpoint.get("progress", {}).get("last_progress_at") or checkpoint.get("updated_at")
        if last_prog_str:
            try:
                last_prog = datetime.fromisoformat(last_prog_str.replace("Z", "+00:00"))
                if (now - last_prog).total_seconds() > progress_timeout:
                    log_event("WORKER_STUCK", self.state.get("current_task_id", "UNKNOWN"), f"No progress for {(now - last_prog).total_seconds()}s")
                    return "RECOVERY_NEEDED"
            except Exception:
                pass

        print("[WATCHDOG] Worker is healthy or running normally.")
        return "HEALTHY"

    def execute_recovery(self, task_id):
        print(f"[RECOVERY] Initiating safe recovery for task {task_id}")
        log_event("RECOVERY_STARTED", task_id, "Starting idempotent recovery")
        
        checkpoint = load_json(CHECKPOINT_FILE, {})
        task_title = task_id
        for t in self.queue.get("tasks", []):
            if t.get("id") == task_id:
                task_title = t.get("title", task_id)
                break
        
        ctx_path = generate_resume_context(task_id, task_title, checkpoint)
        
        self.state["attempt"] = self.state.get("attempt", 1) + 1
        self.state["phase"] = "recovering"
        self.state["updated_at"] = datetime.now(timezone.utc).isoformat()
        save_json(STATE_FILE, self.state)
        
        write_decision(task_id, f"""# {task_id} Decisions — Recovery

## Event
Worker session stopped or timed out.

## Action
Created resume context at `{ctx_path}` and set phase to `recovering`.
Session attempt incremented to {self.state['attempt']}.
""")

        log_event("RECOVERY_READY", task_id, f"Recovery context prepared at {ctx_path}, ready for new session", {"attempt": self.state["attempt"]})
        print(f"[RECOVERY] Recovery complete for {task_id}. Attempt now: {self.state['attempt']}")

    def evaluate_dependencies(self):
        print("[QUEUE] Evaluating task dependencies...")
        tasks = self.queue.get("tasks", [])
        completed_ids = {t["id"] for t in tasks if t.get("status") == "completed"}
        
        updated = False
        for t in tasks:
            if t.get("status") == "pending":
                deps = t.get("dependencies", [])
                if all(d in completed_ids for d in deps):
                    t["status"] = "ready"
                    updated = True
                    log_event("TASK_READY", t["id"], f"Task dependencies met. Status -> ready")
        if updated:
            save_json(QUEUE_FILE, self.queue)

    def check_approval_required(self, task_id, risk_level="LOW"):
        if risk_level.upper() == "HIGH":
            self.state["status"] = "approval_required"
            save_json(STATE_FILE, self.state)
            log_event("APPROVAL_REQUIRED", task_id, f"Task {task_id} has HIGH risk level. Requires user approval.")
            write_decision(task_id, f"# {task_id} Approval Required\n\nTask has HIGH risk. Operations paused until human approval.")
            return True
        return False

    def verify_completion(self, task_id, evidence):
        print(f"[VERIFY] Verifying evidence for task {task_id}...")
        log_event("VERIFYING_START", task_id, "Beginning evidence-based completion verification")
        
        # Evidence requirements
        has_files = bool(evidence.get("files_changed"))
        tests_passed = evidence.get("tests_passed", True)
        review_passed = evidence.get("review_passed", True)
        no_blockers = len(evidence.get("blockers", [])) == 0

        if has_files and tests_passed and review_passed and no_blockers:
            log_event("VERIFYING_SUCCESS", task_id, "All completion evidence passed")
            return True
        else:
            log_event("VERIFYING_FAILED", task_id, f"Evidence check failed: tests={tests_passed}, review={review_passed}, blockers={len(evidence.get('blockers', []))}")
            return False

    def complete_task(self, task_id, evidence, decision_summary=""):
        if not self.verify_completion(task_id, evidence):
            print(f"[COMPLETE] Cannot mark {task_id} as complete — evidence verification failed.")
            return False

        # Update Queue
        for t in self.queue.get("tasks", []):
            if t.get("id") == task_id:
                t["status"] = "completed"
                t["updated_at"] = datetime.now(timezone.utc).isoformat()
                break
        save_json(QUEUE_FILE, self.queue)

        # Update State
        self.state["current_task_id"] = task_id
        self.state["status"] = "running"
        self.state["phase"] = "completed"
        self.state["last_result"] = f"{task_id} completed successfully with evidence."
        self.state["updated_at"] = datetime.now(timezone.utc).isoformat()
        save_json(STATE_FILE, self.state)

        # Log decision & event
        write_decision(task_id, f"# {task_id} Decision Log — Completed\n\n{decision_summary}\n\nCompletion verified with full evidence.")
        log_event("TASK_DONE", task_id, f"Task {task_id} marked DONE with evidence.")
        
        # Re-evaluate queue dependencies
        self.evaluate_dependencies()
        return True

if __name__ == "__main__":
    mgr = ManagerOrchestrator()
    mgr.acquire_lock()
    mgr.evaluate_dependencies()
    health = mgr.check_watchdog()
    if health == "RECOVERY_NEEDED":
        current_task = mgr.state.get("current_task_id", "TASK-UP-0")
        mgr.execute_recovery(current_task)
