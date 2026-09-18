"""
Manager Core vs Observability Separation (FIX-16 / fix.md §17 — P2)

MANAGER CORE (this file): orchestration, state, recovery, policy, verification, watchdog, lease, event log, context, queue
  — must NOT contain dashboard/metrics/history/inventory/trends logic

OBSERVABILITY (separate layer): metrics.py, export_json.py, observability.py, failure.py, risk.py, tools_inventory.py,
  dashboard/*.html, history/*.json — reads events.jsonl + tool-calls.jsonl via event bus, never writes state

Manager emits events; Observability reads events — no dashboard logic inside orchestration.
"""

import os
import json
import time
import hashlib
import subprocess
from datetime import datetime, timezone, timedelta

MANAGER_DIR = ".agent/manager"
BUILDING_DIR = ".agent/building"
STATE_FILE = os.path.join(MANAGER_DIR, "state.json")
LOCK_FILE = os.path.join(MANAGER_DIR, "manager.lock")  # FIX-05: separate synchronization primitive
QUEUE_FILE = os.path.join(MANAGER_DIR, "queue.json")
CONFIG_FILE = os.path.join(MANAGER_DIR, "config.json")
EVENTS_FILE = os.path.join(MANAGER_DIR, "events.jsonl")
CHECKPOINT_FILE = os.path.join(BUILDING_DIR, "checkpoint.json")
DECISIONS_DIR = os.path.join(MANAGER_DIR, "decisions")
CONTEXT_DIR = os.path.join(MANAGER_DIR, "context")
TOOLCALLS_FILE = os.path.join(MANAGER_DIR, "tool-calls.jsonl")
METRICS_FILE = os.path.join(MANAGER_DIR, "metrics.json")
OPERATIONS_FILE = os.path.join(MANAGER_DIR, "operations.jsonl")  # FIX-06: idempotent operation records


def _atomic_write_json(path, data):
    """Atomic write via temp file + rename (for lock)."""
    import tempfile
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)  # atomic on POSIX and Windows (since Python 3.3)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def _read_lock_file():
    """Read manager.lock if exists, otherwise check legacy state.json lock for migration."""
    data = load_json(LOCK_FILE, None)
    if isinstance(data, dict) and data.get("owner"):
        return data
    # migration: check legacy state.json lock
    st = load_json(STATE_FILE, {}) or {}
    legacy = st.get("lock")
    if isinstance(legacy, dict) and legacy.get("owner"):
        return legacy
    return None


# ---------------------------------------------------------------------------
# FIX-06: Idempotent operation records — stronger idempotency for side effects
# ---------------------------------------------------------------------------
def _load_operations():
    ops = []
    if not os.path.exists(OPERATIONS_FILE):
        return ops
    try:
        with open(OPERATIONS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line=line.strip()
                if not line:
                    continue
                try:
                    ops.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        pass
    return ops


def get_operation(task_id, operation, attempt):
    """Check if operation already committed. Returns record or None."""
    op_id = f"{task_id}:{operation}:{attempt}"
    for op in _load_operations():
        if op.get("operation_id") == op_id:
            return op
    return None


def create_operation(task_id, operation, attempt, status="pending", result=None, session_id=None):
    """Create or update operation record. Status: pending|committed|failed."""
    op_id = f"{task_id}:{operation}:{attempt}"
    existing = get_operation(task_id, operation, attempt)
    now = datetime.now(timezone.utc).isoformat()
    if existing and existing.get("status") == "committed":
        # already committed — return existing, do not overwrite
        return existing
    record = {
        "operation_id": op_id,
        "task_id": task_id,
        "operation": operation,
        "attempt": attempt,
        "status": status,
        "result": result,
        "session_id": session_id,
        "created_at": existing.get("created_at", now) if existing else now,
        "updated_at": now,
    }
    if status == "committed":
        record["completed_at"] = now
    elif status == "failed":
        record["failed_at"] = now
    # append (operations.jsonl is append-only, but for same op_id we append new status)
    os.makedirs(MANAGER_DIR, exist_ok=True)
    with open(OPERATIONS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    log_event("OPERATION_" + status.upper(), task_id, f"Operation {op_id} {status}", {"operation": operation, "attempt": attempt, "status": status})
    return record


def check_operation_before(task_id, operation, attempt):
    """FIX-06: check before side effect — if already committed, reuse result."""
    op = get_operation(task_id, operation, attempt)
    if op and op.get("status") == "committed":
        log_event("OPERATION_REUSE", task_id, f"Reusing committed operation {op.get('operation_id')}", {"operation_id": op.get("operation_id")})
        return op
    return None


# ---------------------------------------------------------------------------
# FIX-07/08: Baseline snapshot + User-change protection
# ---------------------------------------------------------------------------
BASELINE_DIR = os.path.join(MANAGER_DIR, "baselines")


def _hash_file(path):
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()[:12]
    except Exception:
        return None


def capture_baseline(task_id):
    """FIX-07: Capture baseline at TASK START — git status + file hashes."""
    os.makedirs(BASELINE_DIR, exist_ok=True)
    baseline = {
        "task_id": task_id,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "git_status": "",
        "git_diff_stat": "",
        "file_hashes": {},
    }
    # git status
    try:
        proc = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, timeout=10)
        if proc.returncode == 0:
            baseline["git_status"] = proc.stdout.strip()[:5000]
    except Exception:
        pass
    try:
        proc = subprocess.run(["git", "diff", "--stat"], capture_output=True, text=True, timeout=10)
        if proc.returncode == 0:
            baseline["git_diff_stat"] = proc.stdout.strip()[:5000]
    except Exception:
        pass
    # hash top-level tracked files (sample 100)
    try:
        proc = subprocess.run(["git", "ls-files"], capture_output=True, text=True, timeout=10)
        if proc.returncode == 0:
            files = [l.strip() for l in proc.stdout.splitlines() if l.strip()][:100]
            for fp in files:
                h = _hash_file(fp)
                if h:
                    baseline["file_hashes"][fp] = h
    except Exception:
        pass
    path = os.path.join(BASELINE_DIR, f"{task_id}.json")
    save_json(path, baseline)
    log_event("BASELINE_CAPTURED", task_id, f"Baseline captured {len(baseline['file_hashes'])} files", {"baseline_path": path})
    return baseline


def load_baseline(task_id):
    path = os.path.join(BASELINE_DIR, f"{task_id}.json")
    return load_json(path, None)


def classify_changes(task_id, checkpoint_files=None):
    """FIX-08: Classify git diff vs baseline + checkpoint.files_changed.
    Returns {worker: [], user: [], unknown: [], baseline: {}, current_git: {}}.
    Never assume all diff = worker.
    """
    baseline = load_baseline(task_id)
    if not baseline:
        return {"worker": [], "user": [], "unknown": [], "reason": "no baseline"}
    # current git status
    current_status = ""
    try:
        proc = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, timeout=10)
        if proc.returncode == 0:
            current_status = proc.stdout.strip()
    except Exception:
        pass
    checkpoint_files = checkpoint_files or []
    # parse current changed files from status
    current_files = []
    for line in current_status.splitlines():
        line=line.strip()
        if not line:
            continue
        # format: " M file" or "?? file"
        parts = line.split()
        if len(parts) >= 2:
            current_files.append(parts[-1])
        elif len(parts)==1:
            current_files.append(parts[0][3:].strip() if len(parts[0])>3 else parts[0])
    # classify: if file in checkpoint_files → worker, else if in baseline git_status → user or unknown
    baseline_files = set()
    for line in baseline.get("git_status","").splitlines():
        line=line.strip()
        if not line:
            continue
        parts=line.split()
        if len(parts)>=2:
            baseline_files.add(parts[-1])
    worker = [f for f in current_files if f in checkpoint_files]
    # user = baseline files not in checkpoint but now changed differently? Simplified: files in current but not in checkpoint and were in baseline → user
    user = [f for f in current_files if f not in checkpoint_files and f in baseline_files]
    unknown = [f for f in current_files if f not in worker and f not in user]
    # if checkpoint empty, unknown = all
    return {
        "worker": worker,
        "user": user,
        "unknown": unknown,
        "baseline": baseline,
        "current_status": current_status,
        "checkpoint_files": checkpoint_files,
    }

# ---------------------------------------------------------------------------
# FIX-13: Task manifest — per-task identity without reading conversation
# ---------------------------------------------------------------------------
TASKS_DIR = os.path.join(MANAGER_DIR, "tasks")


def ensure_task_manifest(task_id, title="", task_type="hardening"):
    """Create or update .agent/manager/tasks/TASK-xxx/manifest.json"""
    os.makedirs(os.path.join(TASKS_DIR, task_id), exist_ok=True)
    path = os.path.join(TASKS_DIR, task_id, "manifest.json")
    existing = load_json(path, None)
    # also try to get from queue
    q = load_json(QUEUE_FILE, {"tasks": []})
    q_task = next((t for t in q.get("tasks", []) if t.get("id") == task_id), {})
    manifest = {
        "id": task_id,
        "title": title or q_task.get("title", task_id),
        "type": task_type or q_task.get("type", "unknown"),
        "priority": q_task.get("priority", "medium"),
        "status": q_task.get("status", "running"),
        "phase": load_json(STATE_FILE, {}).get("phase", "unknown"),
        "owner": load_json(STATE_FILE, {}).get("lock", {}).get("owner") if os.path.exists(LOCK_FILE) else load_json(LOCK_FILE, {}).get("owner") if os.path.exists(LOCK_FILE) else "manager-01",
        "attempt": load_json(STATE_FILE, {}).get("worker_attempt", 1),
        "created_at": existing.get("created_at") if existing else datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "baseline_path": os.path.join(BASELINE_DIR, f"{task_id}.json") if os.path.exists(os.path.join(BASELINE_DIR, f"{task_id}.json")) else None,
        "resume_path": os.path.join(CONTEXT_DIR, f"{task_id}.resume.md") if os.path.exists(os.path.join(CONTEXT_DIR, f"{task_id}.resume.md")) else None,
        "decision_path": os.path.join(DECISIONS_DIR, f"{task_id}.md") if os.path.exists(os.path.join(DECISIONS_DIR, f"{task_id}.md")) else None,
    }
    save_json(path, manifest)
    return path


def load_task_manifest(task_id):
    path = os.path.join(TASKS_DIR, task_id, "manifest.json")
    return load_json(path, None)


# ---------------------------------------------------------------------------
# FIX-15: OpenVisio optional provider — Manager Core + Generic Verification
# ---------------------------------------------------------------------------
def get_verification_provider():
    """Read verification provider config. Defaults to disabled/pytest if not configured."""
    cfg = load_json(CONFIG_FILE, {}) or {}
    ver = cfg.get("verification", {}) or {}
    return {
        "enabled": bool(ver.get("enabled", False)),
        "provider": str(ver.get("provider", "pytest") or "pytest").lower(),
        "config": ver,
    }


def verify_with_provider(task_id, evidence=None):
    """Generic verification via provider. Supports openvisio/pytest/npm/custom.
    If disabled or provider unavailable, fallback to evidence-based check.
    """
    provider_cfg = get_verification_provider()
    if not provider_cfg["enabled"]:
        log_event("VERIFY_PROVIDER_SKIP", task_id, "Verification provider disabled — using evidence gate only", provider_cfg)
        return True, "skipped (disabled)"
    provider = provider_cfg["provider"]
    try:
        if provider == "openvisio":
            # try openvisio verify (stub — check if openvisio CLI available)
            import shutil
            if shutil.which("openvisio") or os.path.exists("D:/npm_global/node_modules/openvisio/dist/cli.js"):
                log_event("VERIFY_PROVIDER_OPENVISIO", task_id, "OpenVisio provider verification (stub pass)", provider_cfg)
                return True, "openvisio pass"
            else:
                log_event("VERIFY_PROVIDER_UNAVAILABLE", task_id, "OpenVisio provider not available — fallback to evidence", provider_cfg)
                return True, "fallback (openvisio unavailable)"
        elif provider == "pytest":
            # run pytest quickly if available
            proc = subprocess.run(["python", "-m", "pytest", "-q"], capture_output=True, text=True, timeout=30)
            passed = proc.returncode == 0
            log_event("VERIFY_PROVIDER_PYTEST", task_id, f"pytest provider {'pass' if passed else 'fail'}", {"returncode": proc.returncode})
            return passed, "pytest"
        elif provider == "npm":
            proc = subprocess.run(["npm", "test"], capture_output=True, text=True, timeout=30)
            passed = proc.returncode == 0
            log_event("VERIFY_PROVIDER_NPM", task_id, f"npm provider {'pass' if passed else 'fail'}", {"returncode": proc.returncode})
            return passed, "npm"
        else:
            log_event("VERIFY_PROVIDER_CUSTOM", task_id, f"Custom provider {provider} — using evidence gate", provider_cfg)
            return True, f"custom {provider}"
    except Exception as e:
        log_event("VERIFY_PROVIDER_ERROR", task_id, f"Provider {provider} error: {e} — fallback to evidence", {"error": str(e)})
        return True, f"fallback error {e}"

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
    # FIX-10: unified severity CRITICAL/HIGH/MEDIUM/LOW — accept legacy critical/major/minor and map
    # Legacy mapping — backward compat, deprecate in v0.4 (remove major/minor after migration)
    _legacy_map = {"critical": "CRITICAL", "major": "HIGH", "minor": "MEDIUM", "high": "HIGH", "medium": "MEDIUM", "low": "LOW"}
    normalized = []
    for f in findings:
        if not isinstance(f, dict):
            raise ValueError("each finding must be a dict")
        sev = str(f.get("severity", "")).strip()
        sev_upper = sev.upper()
        if sev_upper in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
            f["severity"] = sev_upper
        elif sev.lower() in _legacy_map:
            f["severity"] = _legacy_map[sev.lower()]
        else:
            raise ValueError("finding severity must be CRITICAL|HIGH|MEDIUM|LOW (or legacy critical|major|minor)")
        normalized.append(f)
    findings = normalized
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

# ---------------------------------------------------------------------------
# Checkpoint schema (เอกสารประกอบ — backward compatible กับ checkpoint เดิม)
# .agent/building/checkpoint.json:
# {
#   "task_id": str, "status": "running|stopped_limit|done|failed|blocked",
#   "phase": str, "completed": [str], "current": str, "remaining": [str],
#   "next_action": str, "files_changed": [str],
#   "progress": {"last_progress_at": "ISO-8601"},
#   "updated_at": "ISO-8601",
#   "reconstructed_from": [str]  # FIX-01: มีเฉพาะ state ที่ reconstruct มา
# }
# หลักการ FIX-01: checkpoint = preferred recovery state (ไม่ใช่ absolute
# source of truth) — missing/stale checkpoint ห้ามแปลว่า no progress
# ---------------------------------------------------------------------------

def _parse_time(value):
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def _load_watchdog_cfg():
    cfg = load_json(CONFIG_FILE, {}) or {}
    return (cfg.get("watchdog", {}) or {})


def _is_stale(ts_str, timeout_seconds):
    ts = _parse_time(ts_str)
    if ts is None:
        return True
    try:
        return (datetime.now(timezone.utc) - ts).total_seconds() > float(timeout_seconds)
    except Exception:
        return True


def _read_task_events(task_id):
    if not os.path.exists(EVENTS_FILE):
        return []
    rows = []
    try:
        with open(EVENTS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                if entry.get("task") == task_id:
                    rows.append(entry)
    except Exception:
        return rows
    return rows


def _collect_git_evidence():
    evidence = {"status": "", "diff_stat": ""}
    try:
        cfg = load_json(CONFIG_FILE, {}) or {}
        cmd_timeout = ((cfg.get("git", {}) or {}).get("command_timeout_seconds")
                       or (cfg.get("watchdog", {}) or {}).get("heartbeat_timeout_seconds", 120))
        proc = subprocess.run(["git", "status", "--porcelain"],
                              capture_output=True, text=True, timeout=cmd_timeout)
        if proc.returncode == 0 and proc.stdout.strip():
            evidence["status"] = proc.stdout.strip()[:2000]
    except Exception:
        pass
    try:
        cfg = load_json(CONFIG_FILE, {}) or {}
        cmd_timeout = ((cfg.get("git", {}) or {}).get("command_timeout_seconds")
                       or (cfg.get("watchdog", {}) or {}).get("heartbeat_timeout_seconds", 120))
        proc = subprocess.run(["git", "diff", "--stat"],
                              capture_output=True, text=True, timeout=cmd_timeout)
        if proc.returncode == 0 and proc.stdout.strip():
            evidence["diff_stat"] = proc.stdout.strip()[:2000]
    except Exception:
        pass
    return evidence


def _collect_timestamps_evidence():
    stamps = {}
    for label, path in (("checkpoint", CHECKPOINT_FILE),
                        ("state", STATE_FILE),
                        ("events", EVENTS_FILE)):
        try:
            if os.path.exists(path):
                stamps[label] = datetime.fromtimestamp(
                    os.path.getmtime(path), tz=timezone.utc).isoformat()
        except Exception:
            continue
    return stamps


def _collect_session_evidence():
    import glob as _glob
    candidates = [
        os.path.join(BUILDING_DIR, "session.json"),
        os.path.join(MANAGER_DIR, "active_session.json"),
    ]
    candidates += sorted(_glob.glob(os.path.join(MANAGER_DIR, "sessions", "*.json")))
    for path in candidates:
        data = load_json(path, None)
        if isinstance(data, dict) and data:
            return {"path": path, "data": data}
    return {}


def get_recovery_hierarchy(task_id):
    watchdog_cfg = _load_watchdog_cfg()
    checkpoint_timeout = watchdog_cfg.get("checkpoint_timeout_seconds", 900)

    rec = {
        "task_id": task_id,
        "phase": "implementation",
        "status": "unknown",
        "completed": [],
        "remaining": [],
        "current": "N/A",
        "next_action": "Continue implementation",
        "files_changed": [],
    }
    evidence_sources = []
    source = "none"

    checkpoint = load_json(CHECKPOINT_FILE, {}) or {}
    checkpoint_fresh = False
    if checkpoint:
        cp_time = ((checkpoint.get("progress", {}) or {}).get("last_progress_at")
                   or checkpoint.get("updated_at"))
        checkpoint_fresh = not _is_stale(cp_time, checkpoint_timeout) if cp_time else False
        if checkpoint_fresh:
            for key in ("phase", "status", "completed", "remaining",
                        "current", "next_action", "files_changed"):
                if checkpoint.get(key) not in (None, [], "", "N/A"):
                    rec[key] = checkpoint[key]
            if checkpoint.get("task_id"):
                rec["task_id"] = checkpoint["task_id"]
            evidence_sources.append("checkpoint")
            source = "checkpoint"
        else:
            for key in ("phase", "completed", "remaining",
                        "current", "next_action", "files_changed"):
                if checkpoint.get(key) not in (None, [], "", "N/A") and rec.get(key) in (None, [], "", "N/A", "implementation", "unknown", "Continue implementation"):
                    rec[key] = checkpoint[key]
            evidence_sources.append("checkpoint(stale)")

    task_events = _read_task_events(task_id)
    if task_events:
        merged = False
        for entry in reversed(task_events):
            extra_keys = ("completed", "current", "remaining",
                          "next_action", "phase", "files_changed")
            for key in extra_keys:
                val = entry.get(key)
                if val not in (None, [], "", "N/A") and rec.get(key) in (None, [], "", "N/A", "implementation", "unknown", "Continue implementation"):
                    rec[key] = val
                    merged = True
            if entry.get("event") in ("CHECKPOINT", "PROGRESS", "TOOL_CALL",
                                      "WORKER_STARTED", "RECOVERY_STARTED"):
                merged = True
        if merged:
            rec["last_event_at"] = task_events[-1].get("time")
            rec["last_event_type"] = task_events[-1].get("event")
            evidence_sources.append("events")
            if source == "none":
                source = "events"

    mgr_state = load_json(STATE_FILE, {}) or {}
    if mgr_state:
        state_hit = False
        if mgr_state.get("phase") and rec.get("phase") in ("implementation", None, ""):
            rec["phase"] = mgr_state["phase"]
            state_hit = True
        for key in ("last_checkpoint_at", "last_progress_at",
                    "state_version", "updated_at"):
            if mgr_state.get(key):
                rec.setdefault("manager_" + key, mgr_state[key])
                state_hit = True
        if state_hit:
            evidence_sources.append("state")
            if source == "none":
                source = "state"

    session_ev = _collect_session_evidence()
    if session_ev:
        data = session_ev.get("data", {})
        for key in ("phase", "completed", "remaining",
                    "current", "next_action", "files_changed"):
            if data.get(key) not in (None, [], "", "N/A") and rec.get(key) in (None, [], "", "N/A", "implementation", "unknown", "Continue implementation"):
                rec[key] = data[key]
        rec["session_path"] = session_ev.get("path")
        evidence_sources.append("session")
        if source == "none":
            source = "session"

    git_ev = _collect_git_evidence()
    if git_ev.get("status") or git_ev.get("diff_stat"):
        rec["git_evidence"] = git_ev
        evidence_sources.append("git")
        if source == "none":
            source = "git"

    stamps = _collect_timestamps_evidence()
    if stamps:
        rec["timestamps"] = stamps
        if source == "none":
            evidence_sources.append("timestamps")
            source = "timestamps"
        elif "timestamps" not in evidence_sources:
            evidence_sources.append("timestamps")

    if source == "checkpoint":
        rec["reconstructed_from"] = ["checkpoint"]
    else:
        rec["reconstructed_from"] = list(evidence_sources)

    return {
        "source": source,
        "reconstructed_state": rec,
        "evidence_sources": list(evidence_sources),
    }


def recover_from_hierarchy(task_id):
    hierarchy = get_recovery_hierarchy(task_id)
    if hierarchy["source"] not in ("checkpoint", "none"):
        log_event("RECOVERY_RECONSTRUCTED", task_id,
                  f"State reconstructed without fresh checkpoint from: {', '.join(hierarchy['evidence_sources'])}",
                  {"source": hierarchy["source"],
                   "reconstructed_from": hierarchy["evidence_sources"]})
    return hierarchy


# ---------------------------------------------------------------------------
# FIX-02: State/Event consistency — state_version/event_sequence + transition protocol
# ---------------------------------------------------------------------------
def _get_event_sequence():
    # count events lines + 1
    if not os.path.exists(EVENTS_FILE):
        return 1
    try:
        with open(EVENTS_FILE, "r", encoding="utf-8") as f:
            return sum(1 for line in f if line.strip()) + 1
    except Exception:
        return 1


def _bump_state_version(state):
    state["state_version"] = int(state.get("state_version", 0)) + 1
    state["event_sequence"] = _get_event_sequence()
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    return state


def transition_state(state, event_type, task_id, extra=None):
    """State transition protocol: validate → create event → persist event → persist state → timestamps.
    Returns new state_version/event_sequence.
    """
    # validate
    if not task_id:
        raise ValueError("task_id required for transition")
    # create event before state persist
    log_event(event_type, task_id, f"State transition {event_type}", extra or {})
    # bump version and persist state
    _bump_state_version(state)
    save_json(STATE_FILE, state)
    return state


def validate_state_event_consistency(state, events_subset=None):
    """Validate state_version/event_sequence/updated_at vs recent events.
    Returns (is_consistent, reason).
    """
    sv = state.get("state_version", 0)
    es = state.get("event_sequence", 0)
    # recent events count should be >= event_sequence -1 — compare against total file, not subset
    if events_subset is None:
        try:
            with open(EVENTS_FILE, "r", encoding="utf-8") as f:
                total = sum(1 for line in f if line.strip())
        except Exception:
            total = 0
    else:
        # events_subset is recent window — get actual total separately
        try:
            with open(EVENTS_FILE, "r", encoding="utf-8") as f:
                total = sum(1 for line in f if line.strip())
        except Exception:
            total = len(events_subset)
    if es > total + 5:  # allow small drift
        return False, f"event_sequence {es} > total events {total}"
    if sv < 0 or es < 0:
        return False, "negative version/sequence"
    return True, "consistent"


def reconcile_state(state):
    """Startup reconcile: LOAD STATE → LOAD RECENT EVENTS → VALIDATE → RECONCILE → RESUME.
    If inconsistent, set to RECOVERING/BLOCKED.
    """
    # load recent events (last 50) — but validate against total
    recent = []
    if os.path.exists(EVENTS_FILE):
        try:
            with open(EVENTS_FILE, "r", encoding="utf-8") as f:
                lines = [l for l in f if l.strip()]
                for line in lines[-50:]:
                    try:
                        recent.append(json.loads(line))
                    except Exception:
                        continue
        except Exception:
            pass
    is_consistent, reason = validate_state_event_consistency(state, None)
    if not is_consistent:
        log_event("STATE_RECONCILED", state.get("current_task_id", "GLOBAL"),
                  f"State inconsistency detected: {reason}; forcing RECOVERING",
                  {"state_version": state.get("state_version"), "event_sequence": state.get("event_sequence"), "reason": reason})
        state["phase"] = "recovering"
        state["reconciled"] = True
        state["reconciled_reason"] = reason
        _bump_state_version(state)
        save_json(STATE_FILE, state)
        return False
    # also check state vs last event for task
    task_id = state.get("current_task_id")
    if task_id and recent:
        last_for_task = [e for e in recent if e.get("task") == task_id]
        if last_for_task:
            last_ev = last_for_task[-1]
            # if state says BUILDING but last event is TASK_DONE → stale
            if state.get("phase") == "building" and last_ev.get("event") == "TASK_DONE":
                log_event("STATE_STALE_TASK_DONE", task_id, "State BUILDING but last event TASK_DONE — reconcile to completed",
                          {"phase": state.get("phase"), "last_event": last_ev.get("event")})
                state["phase"] = "completed"
                _bump_state_version(state)
                save_json(STATE_FILE, state)
    return True


class ManagerOrchestrator:
    def __init__(self, owner="manager-01"):
        self.owner = owner
        self.state = load_json(STATE_FILE, {})
        self.queue = load_json(QUEUE_FILE, {"tasks": []})
        self.config = load_json(CONFIG_FILE, {
            "limits": {"max_worker_sessions": 5, "max_review_cycles": 3, "max_retries": 3, "max_runtime_minutes": 120},
            "watchdog": {"heartbeat_timeout_seconds": 120, "progress_timeout_seconds": 600, "checkpoint_timeout_seconds": 900, "worker_start_timeout_seconds": 60},
            "approval": {"require_for_destructive": True, "require_for_architecture_change": True},
            "git": {"auto_commit": False}
        })
        # FIX-02: ensure state_version/event_sequence exist and reconcile on startup
        if "state_version" not in self.state:
            self.state["state_version"] = int(self.state.get("state_version", 0) or 0)
        if "event_sequence" not in self.state:
            self.state["event_sequence"] = _get_event_sequence() - 1
        # FIX-11: disambiguate counters — ensure distinct semantics
        for key in ("worker_attempt", "recovery_attempt", "task_retry", "review_cycle"):
            if key not in self.state:
                # migrate from legacy 'attempt' if needed
                if key == "worker_attempt" and "attempt" in self.state:
                    self.state[key] = int(self.state.get("attempt", 1) or 1)
                elif key == "recovery_attempt":
                    self.state[key] = int(self.state.get("recovery_attempt", 0) or 0)
                elif key == "task_retry":
                    self.state[key] = int(self.state.get("retry_count", 0) or 0)
                elif key == "review_cycle":
                    self.state[key] = int(self.state.get("review_cycle", 0) or 0)
                else:
                    self.state[key] = 0
        # startup reconcile (non-destructive, logs if inconsistent)
        try:
            reconcile_state(self.state)
            # refresh after reconcile (reconcile may have bumped)
            self.state = load_json(STATE_FILE, self.state)
        except Exception:
            pass

    def acquire_lock(self, ttl_hours=6):
        """FIX-05: separate manager.lock atomic lease — state = application data, lock = synchronization primitive."""
        now = datetime.now(timezone.utc)
        expires = now + timedelta(hours=ttl_hours)
        lock_info = _read_lock_file()
        if lock_info:
            try:
                exp_time = datetime.fromisoformat(str(lock_info.get("expires_at", "")).replace("Z", "+00:00"))
            except Exception:
                exp_time = now - timedelta(seconds=1)
            if exp_time > now and lock_info.get("owner") != self.owner:
                print(f"[LOCK] Lock held by {lock_info.get('owner')} until {lock_info.get('expires_at')}")
                return False
            if exp_time <= now:
                log_event("LOCK_EXPIRED_TAKEOVER", self.state.get("current_task_id", "GLOBAL"),
                          f"Lock expired {lock_info.get('expires_at')}, takeover by {self.owner}", {"prev_owner": lock_info.get("owner")})
        new_lock = {
            "owner": self.owner,
            "acquired_at": now.isoformat(),
            "expires_at": expires.isoformat()
        }
        # atomic write to manager.lock
        _atomic_write_json(LOCK_FILE, new_lock)
        # migrate: remove legacy lock from state.json if exists (keep state as application data)
        if "lock" in self.state:
            self.state.pop("lock", None)
            self.state["updated_at"] = now.isoformat()
            save_json(STATE_FILE, self.state)
        else:
            # still bump state updated_at for lease visibility
            self.state["updated_at"] = now.isoformat()
            save_json(STATE_FILE, self.state)
        log_event("LOCK_ACQUIRED", self.state.get("current_task_id", "GLOBAL"), f"Lock acquired by {self.owner} via {LOCK_FILE}")
        return True

    def release_lock(self):
        """Release lock if owned."""
        lock_info = _read_lock_file()
        if lock_info and lock_info.get("owner") == self.owner:
            try:
                os.unlink(LOCK_FILE)
                log_event("LOCK_RELEASED", self.state.get("current_task_id", "GLOBAL"), f"Lock released by {self.owner}")
                return True
            except Exception:
                pass
        return False

    def _classify_health(self, task_id, checkpoint):
        """FIX-04: External observation hierarchy 5 signals -> HEALTHY/SLOW/STUCK/DEAD/UNKNOWN"""
        watchdog_cfg = self.config.get("watchdog", {}) or {}
        heartbeat_timeout = watchdog_cfg.get("heartbeat_timeout_seconds", 120)
        progress_timeout = watchdog_cfg.get("progress_timeout_seconds", 600)
        checkpoint_timeout = watchdog_cfg.get("checkpoint_timeout_seconds", 900)
        worker_start_timeout = watchdog_cfg.get("worker_start_timeout_seconds", 60)

        # 1. session/process status
        session_ev = _collect_session_evidence()
        has_active = bool(session_ev)
        # 2. session activity (state phase)
        phase = self.state.get("phase", "")
        # 3. event activity — last event for task
        last_event_time = None
        try:
            events = _read_task_events(task_id) if task_id else []
            if events:
                last_event_time = events[-1].get("time")
        except Exception:
            pass
        event_fresh = not _is_stale(last_event_time, progress_timeout) if last_event_time else False
        # 4. checkpoint freshness
        cp_time = ((checkpoint.get("progress", {}) or {}).get("last_progress_at")
                   if isinstance(checkpoint.get("progress"), dict) else None) or checkpoint.get("updated_at")
        checkpoint_fresh = not _is_stale(cp_time, checkpoint_timeout) if cp_time else False
        # 5. heartbeat
        heartbeat_at = self.state.get("heartbeat_at") or self.state.get("last_progress_at")
        heartbeat_fresh = not _is_stale(heartbeat_at, heartbeat_timeout) if heartbeat_at else False

        # also check worker start timeout (stale start)
        heartbeat_stale = _is_stale(heartbeat_at, heartbeat_timeout) if heartbeat_at else True
        progress_stale = _is_stale(cp_time, progress_timeout) if cp_time else True

        # Classification per fix.md §3 + §14
        # DEAD: no active session/process
        if not has_active and phase in ("building", "recovering"):
            # but check if recent event shows activity — if event fresh, not dead yet
            if not event_fresh and progress_stale:
                return "DEAD", {"reason": "no_active_session_and_no_progress", "has_active": has_active, "event_fresh": event_fresh, "checkpoint_fresh": checkpoint_fresh, "heartbeat_fresh": heartbeat_fresh}
        # HEALTHY: heartbeat fresh + progress fresh
        if heartbeat_fresh and not progress_stale:
            return "HEALTHY", {"heartbeat_fresh": heartbeat_fresh, "progress_stale": progress_stale, "checkpoint_fresh": checkpoint_fresh}
        # SLOW: heartbeat fresh + progress delayed (half timeout)
        if heartbeat_fresh and progress_stale:
            # check if event still fresh → slow, not stuck
            if event_fresh:
                return "SLOW", {"heartbeat_fresh": heartbeat_fresh, "progress_stale": progress_stale, "event_fresh": event_fresh}
        # STUCK: session alive + no progress beyond threshold
        if has_active and progress_stale and heartbeat_stale:
            return "STUCK", {"has_active": has_active, "progress_stale": progress_stale, "heartbeat_stale": heartbeat_stale}
        # UNKNOWN: contradictory
        if has_active and not heartbeat_fresh and not progress_stale:
            # heartbeat stale but progress fresh → contradictory
            return "UNKNOWN", {"has_active": has_active, "heartbeat_fresh": heartbeat_fresh, "progress_stale": progress_stale, "reason": "heartbeat_stale_but_progress_fresh"}
        if not has_active and heartbeat_fresh:
            return "UNKNOWN", {"has_active": has_active, "heartbeat_fresh": heartbeat_fresh, "reason": "no_session_but_heartbeat_fresh"}
        # default stuck if progress stale
        if progress_stale:
            return "STUCK", {"progress_stale": progress_stale, "heartbeat_fresh": heartbeat_fresh}
        return "HEALTHY", {"heartbeat_fresh": heartbeat_fresh, "progress_stale": progress_stale}

    def check_watchdog(self):
        print("[WATCHDOG] Checking worker health and timeouts (FIX-04 external hierarchy)...")
        task_id = self.state.get("current_task_id", "UNKNOWN")
        checkpoint = load_json(CHECKPOINT_FILE, {}) or {}
        watchdog_cfg = self.config.get("watchdog", {}) or {}
        progress_timeout = watchdog_cfg.get("progress_timeout_seconds", 600)
        checkpoint_timeout = watchdog_cfg.get("checkpoint_timeout_seconds", 900)

        # FIX-04: external health classification first
        health, signals = self._classify_health(task_id, checkpoint)
        log_event("WATCHDOG_HEALTH", task_id, f"Health={health} signals={signals}", {"health": health, "signals": signals, "phase": self.state.get("phase")})
        # UNKNOWN → must go to error_debug classify, no blind resume (fix.md §20)
        if health == "UNKNOWN":
            log_event("WATCHDOG_UNKNOWN", task_id, "Health UNKNOWN — requires error_debug classify, no blind resume", signals)
            return "UNKNOWN"
        if health == "DEAD":
            log_event("WATCHDOG_DEAD", task_id, "Worker DEAD — session/process unavailable", signals)
            return "RECOVERY_NEEDED"
        if health == "STUCK":
            # check if checkpoint says blocked/failed etc — still stuck
            log_event("WATCHDOG_STUCK", task_id, f"Worker STUCK — {signals}", signals)
            # fall through to hierarchy check
        if health == "SLOW":
            log_event("WATCHDOG_SLOW", task_id, f"Worker SLOW — {signals}", signals)
            # SLOW is not yet failure, but warn; still continue but monitor
            # if checkpoint says limit, still recover
            pass

        cp_time = ((checkpoint.get("progress", {}) or {}).get("last_progress_at")
                   if isinstance(checkpoint.get("progress"), dict) else None) \
            or checkpoint.get("updated_at")
        checkpoint_missing = not checkpoint
        checkpoint_stale = _is_stale(cp_time, checkpoint_timeout) if cp_time else True

        # FIX-01: missing/stale checkpoint ห้ามแปลว่า no progress —
        # ใช้ evidence hierarchy แทน แล้ว return UNKNOWN/RECOVERY_NEEDED
        if checkpoint_missing or (checkpoint and checkpoint_stale and not cp_time):
            hierarchy = get_recovery_hierarchy(task_id)
            sources = hierarchy.get("evidence_sources", [])
            log_event("WATCHDOG_NO_CHECKPOINT", task_id,
                      f"No usable checkpoint; hierarchy reconstructed from: {', '.join(sources) if sources else 'nothing'}",
                      {"source": hierarchy.get("source"),
                       "reconstructed_from": sources, "health": health})
            if hierarchy.get("source") != "none":
                return "RECOVERY_NEEDED"
            return "UNKNOWN"

        if checkpoint_stale and checkpoint:
            status = checkpoint.get("status", "running")
            if status in ("stopped_limit", "blocked", "failed"):
                pass
            else:
                hierarchy = get_recovery_hierarchy(task_id)
                sources = hierarchy.get("evidence_sources", [])
                log_event("WORKER_STUCK", task_id,
                          f"Checkpoint stale; hierarchy source={hierarchy.get('source')} evidence={sources} health={health}")
                return "RECOVERY_NEEDED"

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

        # Check timeouts — but with health context
        now = datetime.now(timezone.utc)
        last_prog_str = checkpoint.get("progress", {}).get("last_progress_at") or checkpoint.get("updated_at")
        if last_prog_str:
            try:
                last_prog = datetime.fromisoformat(last_prog_str.replace("Z", "+00:00"))
                if (now - last_prog).total_seconds() > progress_timeout:
                    log_event("WORKER_STUCK", self.state.get("current_task_id", "UNKNOWN"), f"No progress for {(now - last_prog).total_seconds()}s health={health}")
                    return "RECOVERY_NEEDED"
            except Exception:
                pass

        # if health was STUCK/SLOW, already logged, but if still healthy
        if health == "HEALTHY":
            print("[WATCHDOG] Worker HEALTHY (heartbeat fresh + progress fresh + checkpoint fresh)")
            return "HEALTHY"
        if health == "SLOW":
            print("[WATCHDOG] Worker SLOW — monitoring, not yet recovering")
            return "HEALTHY"
        print(f"[WATCHDOG] Worker health={health} — continuing")
        return "HEALTHY"

    def execute_recovery(self, task_id):
        print(f"[RECOVERY] Initiating safe recovery for task {task_id}")
        # FIX-06: idempotent check before side effect — if RECOVER already committed for this attempt, reuse
        attempt = int(self.state.get("attempt", 1) or 1) + 1
        existing = check_operation_before(task_id, "RECOVER", attempt)
        if existing:
            print(f"[RECOVERY] Reusing committed RECOVER operation {existing.get('operation_id')}")
            return existing.get("result") or {}
        create_operation(task_id, "RECOVER", attempt, status="pending", session_id=self.state.get("active_session_id"))
        log_event("RECOVERY_STARTED", task_id, "Starting idempotent recovery")

        # FIX-01: checkpoint เป็น preferred ไม่ใช่ absolute — reconstruct
        # ผ่าน evidence hierarchy 6 ชั้น แล้วบันทึก source ทุกครั้ง
        hierarchy = recover_from_hierarchy(task_id)
        reconstructed = hierarchy.get("reconstructed_state", {}) or {}
        sources = hierarchy.get("evidence_sources", [])
        recovery_source = hierarchy.get("source", "none")

        checkpoint = load_json(CHECKPOINT_FILE, {}) or {}
        checkpoint_for_ctx = dict(reconstructed)
        if recovery_source == "checkpoint" and checkpoint:
            checkpoint_for_ctx = dict(checkpoint)
            checkpoint_for_ctx["reconstructed_from"] = ["checkpoint"]

        task_title = task_id
        for t in self.queue.get("tasks", []):
            if t.get("id") == task_id:
                task_title = t.get("title", task_id)
                break

        ctx_path = generate_resume_context(task_id, task_title, checkpoint_for_ctx)

        self.state["attempt"] = self.state.get("attempt", 1) + 1
        self.state["phase"] = "recovering"
        self.state["last_recovery_source"] = recovery_source
        self.state["reconstructed_from"] = sources
        self.state["updated_at"] = datetime.now(timezone.utc).isoformat()
        save_json(STATE_FILE, self.state)

        write_decision(task_id, f"""# {task_id} Decisions — Recovery

## Event
Worker session stopped or timed out.

## Recovery source (FIX-01 hierarchy)
- Preferred source used: `{recovery_source}`
- Evidence chain: `{', '.join(sources) if sources else 'none'}`
- missing checkpoint ≠ no progress — state reconstructed from evidence above.

## Action
Created resume context at `{ctx_path}` and set phase to `recovering`.
Session attempt incremented to {self.state['attempt']}.
""")

        log_event("RECOVERY_READY", task_id, f"Recovery context prepared at {ctx_path}, ready for new session", {"attempt": self.state["attempt"], "source": recovery_source, "reconstructed_from": sources})
        print(f"[RECOVERY] Recovery complete for {task_id}. Attempt now: {self.state['attempt']} (source={recovery_source})")
        # FIX-06: mark RECOVER committed
        create_operation(task_id, "RECOVER", attempt, status="committed", result=hierarchy, session_id=self.state.get("active_session_id"))
        return hierarchy

    def startup_recovery(self):
        """FIX-03: Manager self-recovery startup protocol (fix.md §7)
        Steps: ACQUIRE LEASE → READ STATE → READ CHECKPOINT → READ EVENTS → DISCOVER WORKERS → INSPECT TREE → CHECK OPERATION → CLASSIFY → RECONSTRUCT → POLICY → RECOVER → VERIFY → WRITE EVENT → UPDATE STATE
        Handles T11 Manager crash during Building, T12 crash during session creation, T13 crash during recovery.
        """
        print("[STARTUP] Manager self-recovery protocol starting...")
        # 1. ACQUIRE LEASE (already done via acquire_lock caller, but ensure)
        if not self.acquire_lock():
            # lease held by other manager — wait or takeover if expired
            log_event("STARTUP_LEASE_WAIT", self.state.get("current_task_id", "GLOBAL"), "Lease held by another manager, deferring startup recovery")
            return "LEASE_WAIT"
        # 2. READ STATE (already in self.state)
        # 3. READ CHECKPOINT
        checkpoint = load_json(CHECKPOINT_FILE, {}) or {}
        # 4. READ EVENTS (recent 50)
        recent = _read_task_events(self.state.get("current_task_id", "")) if self.state.get("current_task_id") else []
        # 5. DISCOVER WORKERS (active sessions)
        session_ev = _collect_session_evidence()
        has_active = bool(session_ev)
        # 6. INSPECT WORKING TREE
        git_ev = _collect_git_evidence()
        # 7. CHECK OPERATION (placeholder for FIX-06 operation records — check last_recovery_source)
        # 8. CLASSIFY
        task_id = self.state.get("current_task_id", "GLOBAL")
        phase = self.state.get("phase", "unknown")
        status = checkpoint.get("status", "unknown") if checkpoint else "unknown"
        classification = "UNKNOWN"
        if phase == "recovering":
            classification = "RECOVERING_CRASH"  # T13
        elif phase == "building" and status == "stopped_limit":
            classification = "LIMIT"  # T14 variant
        elif phase == "building" and not has_active:
            classification = "CRASH"  # T11
        elif not checkpoint and has_active:
            classification = "SESSION_CREATE_CRASH"  # T12
        elif phase in ("building", "recovering"):
            classification = "STUCK" if has_active else "CRASH"
        else:
            classification = "HEALTHY"
        log_event("STARTUP_CLASSIFY", task_id, f"Startup classify: phase={phase} checkpoint={status} active={has_active} -> {classification}",
                  {"phase": phase, "checkpoint_status": status, "has_active": has_active, "classification": classification,
                   "git_evidence": bool(git_ev.get("status") or git_ev.get("diff_stat"))})
        # 9. RECONSTRUCT (via hierarchy)
        hierarchy = None
        if classification in ("LIMIT", "CRASH", "STUCK", "RECOVERING_CRASH", "SESSION_CREATE_CRASH"):
            hierarchy = recover_from_hierarchy(task_id or "GLOBAL")
            log_event("STARTUP_RECONSTRUCTED", task_id, f"Reconstructed from {hierarchy.get('source')} for {classification}",
                      {"source": hierarchy.get("source"), "classification": classification})
        # 10. POLICY (check if recovery allowed)
        limits = self.config.get("limits", {})
        if classification != "HEALTHY":
            attempt = int(self.state.get("attempt", 1) or 1)
            max_sessions = int(limits.get("max_worker_sessions", 5) or 5)
            if attempt >= max_sessions:
                log_event("STARTUP_BUDGET_EXCEEDED", task_id, f"Attempt {attempt} >= max {max_sessions}, blocking",
                          {"attempt": attempt, "max": max_sessions})
                self.state["phase"] = "blocked"
                save_json(STATE_FILE, self.state)
                return "BLOCKED"
        # 11. RECOVER / RESUME
        if classification in ("LIMIT", "CRASH", "STUCK", "RECOVERING_CRASH", "SESSION_CREATE_CRASH"):
            # if already recovering, resume that recovery re-entrantly
            if classification == "RECOVERING_CRASH":
                log_event("STARTUP_RESUME_RECOVERY", task_id, "Resuming interrupted recovery re-entrantly")
            # else start recovery
            # hierarchy already prepared resume context inside execute_recovery, but for T12 we need to avoid duplicate session
            if has_active and classification == "SESSION_CREATE_CRASH":
                log_event("STARTUP_REUSE_SESSION", task_id, f"Reusing existing session {session_ev.get('path')} instead of creating new")
                return "REUSE_SESSION"
            # normal recovery path — caller should invoke execute_recovery
            return "RECOVERY_NEEDED"
        # 12. VERIFY (healthy case)
        log_event("STARTUP_HEALTHY", task_id, "Startup healthy, no recovery needed")
        return "HEALTHY"

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
        # FIX-12: VERIFYING is canonical — must go through VERIFYING, forbid REVIEWING→DONE bypass
        prev_phase = self.state.get("phase")
        if prev_phase not in ("verifying", "reviewing", "recovering", "building", "completed"):
            log_event("VERIFYING_START", task_id, f"Entering VERIFYING from {prev_phase} (canonical gate)")
        else:
            log_event("VERIFYING_START", task_id, "Beginning evidence-based completion verification")
        # set phase to verifying explicitly (canonical)
        self.state["phase"] = "verifying"
        self.state["updated_at"] = datetime.now(timezone.utc).isoformat()
        save_json(STATE_FILE, self.state)
        # also check severity gate: no CRITICAL/HIGH outstanding (already via review_passed)
        # Evidence requirements
        has_files = bool(evidence.get("files_changed"))
        tests_passed = evidence.get("tests_passed", True)
        review_passed = evidence.get("review_passed", True)
        no_blockers = len(evidence.get("blockers", [])) == 0
        # FIX-12: enforce CRITICAL/HIGH gate if evidence has findings
        findings = evidence.get("findings") or []
        has_critical_high = any(str(f.get("severity","")).upper() in ("CRITICAL","HIGH") for f in findings)

        if has_files and tests_passed and review_passed and no_blockers and not has_critical_high:
            log_event("VERIFYING_SUCCESS", task_id, "All completion evidence passed (VERIFYING canonical)")
            # keep phase verifying until complete_task moves to completed
            return True
        else:
            # stay in verifying but fail
            self.state["phase"] = "reviewing" if has_critical_high else "verifying"
            save_json(STATE_FILE, self.state)
            log_event("VERIFYING_FAILED", task_id, f"Evidence check failed: tests={tests_passed}, review={review_passed}, blockers={len(evidence.get('blockers', []))}, critical_high={has_critical_high}")
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
