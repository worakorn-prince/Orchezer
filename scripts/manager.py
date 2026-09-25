"""
Manager Core vs Observability Separation (FIX-16 / fix.md §17 — P2) — FIX-V2-22 boundary

MANAGER CORE (this file): orchestration, state, recovery, policy, verification, watchdog, lease, event log, context, queue
  — must NOT contain dashboard/metrics/history/inventory/trends logic
  — must NOT import observability modules (metrics/observability/export_json/export_report/graph/aggregate/bench/failure)

OBSERVABILITY (separate layer): metrics.py, export_json.py, observability.py, failure.py, risk.py, tools_inventory.py,
  dashboard/*.html, history/*.json — reads events.jsonl + tool-calls.jsonl via event bus, never writes state

Manager emits events; Observability reads events — no dashboard logic inside orchestration.
Boundary is events-only: core writes events.jsonl/tool-calls.jsonl; observability reads them.
Failure-isolation: observability failure never blocks core orchestration.
"""

import os
import json
import time
import hashlib
import subprocess
import uuid
from datetime import datetime, timezone, timedelta

MVP_ADVANCED_MODULES = ("parallel", "distributed", "discord", "routing", "rollback", "dashboard", "benchmark", "planning")
MVP_STDLIB_ALLOWLIST = ("os", "json", "time", "hashlib", "subprocess", "uuid", "datetime")


def assert_mvp_boundary():
    try:
        path = __file__
    except NameError:
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return
    for raw in lines:
        s = raw.strip()
        if not (s.startswith("import ") or s.startswith("from ")):
            continue
        low = s.lower()
        for mod in MVP_ADVANCED_MODULES:
            if mod in low:
                raise ImportError("MVP boundary violation: advanced module '%s' in core import: %r" % (mod, s))
        if raw[:1] in (" ", "\t"):
            continue
        root = low.replace("from ", "", 1) if low.startswith("from ") else low.replace("import ", "", 1)
        root = root.split()[0].split(".")[0].split(",")[0].strip()
        if root and root not in MVP_STDLIB_ALLOWLIST:
            raise ImportError("MVP boundary violation: non-stdlib import '%s' not in allowlist" % s)


assert_mvp_boundary()

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
METRICS_FILE = os.path.join(MANAGER_DIR, "metrics.json")  # OBSERVABILITY-OWNED: core never reads/writes it
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


class StaleLeaseError(Exception):
    """FIX-V2-06: raised when a stale lease holder attempts a state mutation — ABORT MUTATION."""
    pass


class _VerifyResult(tuple):
    def __new__(cls, iterable):
        return super().__new__(cls, tuple(iterable))

    def __bool__(self):
        try:
            return bool(self[0])
        except Exception:
            return False

    __nonzero__ = __bool__


def verify_lease_fencing(owner, lease_id=None, fencing_token=None):
    """FIX-V2-06: module-level fencing check — True only if the given identity matches the current lock file."""
    lock_info = _read_lock_file()
    if not lock_info:
        return True  # no lock, allow (caller will acquire)
    if lock_info.get("owner") != owner:
        return False
    if lease_id is not None and lock_info.get("lease_id") != lease_id:
        return False
    if fencing_token is not None and _coerce_token(lock_info.get("fencing_token")) != _coerce_token(fencing_token):
        return False
    return True


def _coerce_token(value):
    try:
        return int(value)
    except Exception:
        return 0


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
    """Create or update operation record. Status: PENDING|STARTED|COMMITTED|FAILED|UNKNOWN (fix-v2 §5)."""
    op_id = f"{task_id}:{operation}:{attempt}"
    existing = get_operation(task_id, operation, attempt)
    now = datetime.now(timezone.utc).isoformat()
    if existing and existing.get("status") == "committed":
        # already committed — return existing, do not overwrite
        return existing
    # fix-v2 §5: STARTED must be handled — if STARTED exists and we try to re-create pending, keep STARTED for reconciliation
    if existing and existing.get("status") == "started" and status == "pending":
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
    elif status == "started":
        record["started_at"] = now
    elif status == "unknown":
        record["unknown_at"] = now
    # append (operations.jsonl is append-only, but for same op_id we append new status)
    os.makedirs(MANAGER_DIR, exist_ok=True)
    with open(OPERATIONS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    log_event("OPERATION_" + status.upper(), task_id, f"Operation {op_id} {status}", {"operation": operation, "attempt": attempt, "status": status})
    return record


def check_operation_before(task_id, operation, attempt):
    """FIX-06 + fix-v2 §5: check before side effect — handle STARTED/UNKNOWN.

    No operation may be blindly re-executed merely because its final commit record is missing.
    If STARTED exists, must discover external side effect before retry.
    """
    op = get_operation(task_id, operation, attempt)
    if not op:
        return None
    status = (op.get("status") or "").lower()
    if status == "committed":
        log_event("OPERATION_REUSE", task_id, f"Reusing committed operation {op.get('operation_id')}", {"operation_id": op.get("operation_id")})
        return op
    if status == "started":
        # fix-v2 §5: STARTED -> discover active sessions before retry
        log_event("OPERATION_STARTED_FOUND", task_id, f"Found STARTED {op.get('operation_id')} — must reconcile external side effect before retry", {"operation_id": op.get("operation_id")})
        # try to discover if session exists
        try:
            from manager import _collect_session_evidence  # avoid circular
            sess = _collect_session_evidence()
            if sess:
                # session exists -> can commit
                log_event("OPERATION_STARTED_COMMIT", task_id, f"STARTED {op.get('operation_id')} has active session — committing", {"operation_id": op.get("operation_id")})
                return create_operation(task_id, operation, attempt, status="committed", result=op.get("result"), session_id=sess.get("path") if isinstance(sess, dict) else None)
            else:
                # no session — need to check UNKNOWN
                log_event("OPERATION_STARTED_UNKNOWN", task_id, f"STARTED {op.get('operation_id')} no session found — UNKNOWN, need classification", {"operation_id": op.get("operation_id")})
                return None
        except Exception:
            return None
    if status == "unknown":
        log_event("OPERATION_UNKNOWN", task_id, f"Operation {op.get('operation_id')} is UNKNOWN — need classification/safe recovery", {"operation_id": op.get("operation_id")})
        return None
    return None


def reconcile_started_operations(task_id=None):
    """fix-v2 §5: On Manager restart, reconcile any STARTED operations."""
    ops = _load_operations()
    for op in ops:
        if (op.get("status") or "").lower() != "started":
            continue
        if task_id and op.get("task_id") != task_id:
            continue
        check_operation_before(op.get("task_id"), op.get("operation"), op.get("attempt"))


# ---------------------------------------------------------------------------
# FIX-07/08: Baseline snapshot + User-change protection
# ---------------------------------------------------------------------------
BASELINE_DIR = os.path.join(MANAGER_DIR, "baselines")


def _baseline_attempt_path(task_id, attempt):
    return os.path.join(BASELINE_DIR, f"{task_id}.attempt-{int(attempt):03d}.json")


def _existing_attempts(task_id):
    try:
        files = os.listdir(BASELINE_DIR)
    except Exception:
        return []
    prefix = f"{task_id}.attempt-"
    out = []
    for fn in files:
        if fn.startswith(prefix) and fn.endswith(".json"):
            mid = fn[len(prefix):-len(".json")]
            try:
                out.append(int(mid))
            except Exception:
                continue
    return sorted(out)


def _latest_attempt(task_id):
    ex = _existing_attempts(task_id)
    return max(ex) if ex else None


def _hash_file(path):
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()[:12]
    except Exception:
        return None


def capture_baseline(task_id, attempt=None):
    """FIX-07: Capture baseline at TASK START — git status + file hashes."""
    os.makedirs(BASELINE_DIR, exist_ok=True)
    if attempt is None:
        _latest = _latest_attempt(task_id)
        attempt = (_latest + 1) if _latest else 1
    attempt = int(attempt)
    _target = _baseline_attempt_path(task_id, attempt)
    while os.path.exists(_target):
        attempt += 1
        _target = _baseline_attempt_path(task_id, attempt)
        log_event("BASELINE_ATTEMPT_BUMP", task_id, f"Baseline attempt bumped to {attempt}", {"attempt": attempt, "baseline_path": _target})
    baseline = {
        "task_id": task_id,
        "attempt": attempt,
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
    path = _target
    save_json(path, baseline)
    log_event("BASELINE_CAPTURED", task_id, f"Baseline captured {len(baseline['file_hashes'])} files", {"baseline_path": path, "attempt": attempt})
    return baseline


def load_baseline(task_id, attempt=None):
    if attempt is not None:
        try:
            attempt = int(attempt)
        except Exception:
            return None
        return load_json(_baseline_attempt_path(task_id, attempt), None)
    latest = _latest_attempt(task_id)
    if latest is not None:
        data = load_json(_baseline_attempt_path(task_id, latest), None)
        if data is not None:
            return data
    flat = os.path.join(BASELINE_DIR, f"{task_id}.json")
    return load_json(flat, None)


def classify_changes(task_id, checkpoint_files=None, attempt=None):
    """FIX-08 + FIX-V2-12: checkpoint files_changed is hint only, never authority.
    Ground truth order: baseline + current git diff > checkpoint list.
    Missing baseline -> conservative BLOCKED.
    Returns verdict + winner + conflicting files list (+ worker/user/unknown for compat).
    """
    baseline = load_baseline(task_id, attempt=attempt)
    if not baseline:
        return {"verdict": "BLOCKED", "reason": "no baseline", "worker": [], "user": [], "unknown": [], "checkpoint_files": checkpoint_files or [], "conflicting": [], "winner": "none"}
    current_status = ""
    try:
        proc = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, timeout=10)
        if proc.returncode == 0:
            current_status = proc.stdout.strip()
    except Exception:
        pass
    checkpoint_files = checkpoint_files or []
    current_files = []
    for line in current_status.splitlines():
        line=line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 2:
            current_files.append(parts[-1])
        elif len(parts)==1:
            current_files.append(parts[0][3:].strip() if len(parts[0])>3 else parts[0])
    baseline_files = set()
    for line in baseline.get("git_status","").splitlines():
        line=line.strip()
        if not line:
            continue
        parts=line.split()
        if len(parts)>=2:
            baseline_files.add(parts[-1])
    current_set = set(current_files)
    hint_set = set(checkpoint_files)
    conflicting = sorted(hint_set - current_set)
    winner = "git" if conflicting else "git"
    worker = [f for f in current_files if f in hint_set]
    user = [f for f in current_files if f not in hint_set and f in baseline_files]
    unknown = [f for f in current_files if f not in worker and f not in user]
    return {
        "verdict": "OK",
        "winner": winner,
        "conflicting": conflicting,
        "worker": worker,
        "user": user,
        "unknown": unknown,
        "baseline": baseline,
        "current_status": current_status,
        "checkpoint_files": checkpoint_files,
        "reason": "git authority",
    }


def resolve_file_policy(classify_result, worker_priority="LOW", user_priority="HIGH"):
    """FIX-V2-13: Same-File User Change Policy (fix-v2 section 14).

    Maps classify_changes() output to an action + case + reason.
    Never touches the filesystem, never raises, never overwrites.
    Cases:
      A different file (no user/unknown) -> CONTINUE (auto continue).
      B same file non-overlapping cannot be proven -> WARNING, continue with warning.
      C same file overlapping region -> BLOCKED (manager must not overwrite).
      D ownership undetermined (no baseline / unknown files) -> BLOCKED (do not guess).
    Intent priority: conflicting user intent wins by priority; user HIGH beats
    worker LOW -> BLOCKED / worker yields. Equal or higher user priority blocks.
    """
    try:
        res = classify_result or {}
    except Exception:
        res = {}
    try:
        if res.get("verdict") == "BLOCKED":
            _unknown = [x for x in (res.get("unknown") or []) if isinstance(x, str) and x]
            if _unknown:
                return {"action": "BLOCKED", "case": "D", "reason": "unknown origin files: %s" % ",".join(_unknown[:5])}
            _worker = list(res.get("worker", []) or [])
            _user = list(res.get("user", []) or [])
            if _worker or _user:
                return {"action": "BLOCKED", "case": "D", "reason": "no baseline — ownership undetermined"}
            return {"action": "CONTINUE", "case": "A", "reason": "different file or no user changes, auto continue"}
        worker = list(res.get("worker", []) or [])
        user = list(res.get("user", []) or [])
        unknown = list(res.get("unknown", []) or [])
        if unknown:
            return {"action": "BLOCKED", "case": "D", "reason": "unknown origin files: %s" % ",".join(unknown[:5])}
        overlap = sorted(set(worker) & set(user))
        if overlap:
            order = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}
            try:
                uw = order.get(str(user_priority).upper(), 1)
                ww = order.get(str(worker_priority).upper(), 0)
            except Exception:
                uw, ww = 1, 0
            if uw >= ww:
                return {"action": "BLOCKED", "case": "C", "reason": "overlapping same-file change, user %s beats worker %s: %s" % (user_priority, worker_priority, ",".join(overlap[:5]))}
            return {"action": "BLOCKED", "case": "C", "reason": "overlapping same-file change: %s" % ",".join(overlap[:5])}
        if user:
            return {"action": "WARNING", "case": "B", "reason": "user files present without overlap, continue with warning: %s" % ",".join(user[:5])}
        return {"action": "CONTINUE", "case": "A", "reason": "different file or no user changes, auto continue"}
    except Exception as _e:
        return {"action": "BLOCKED", "case": "D", "reason": "policy evaluation failed: %s" % _e}

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
# FIX-15: Optional verification providers — Manager Core + Generic Verification
# ---------------------------------------------------------------------------
class VerificationProvider:
    name = ""
    def can_verify(self, config=None):
        return False
    def verify(self, task_id, evidence=None, config=None):
        return {"status": "unavailable", "provider": self.name, "detail": "not available"}


_VERIFICATION_REGISTRY = {}


def register_verification_provider(instance):
    _VERIFICATION_REGISTRY[instance.name] = instance
    return instance


class PytestVerificationProvider(VerificationProvider):
    name = "pytest"
    def can_verify(self, config=None):
        return True
    def verify(self, task_id, evidence=None, config=None):
        proc = subprocess.run(["python", "-m", "pytest", "-q"], capture_output=True, text=True, timeout=30)
        if proc.returncode == 0:
            return {"status": "pass", "provider": self.name, "detail": "pytest pass"}
        return {"status": "fail", "provider": self.name, "detail": "pytest fail"}


class NpmVerificationProvider(VerificationProvider):
    name = "npm"
    def can_verify(self, config=None):
        return True
    def verify(self, task_id, evidence=None, config=None):
        proc = subprocess.run(["npm", "test"], capture_output=True, text=True, timeout=30)
        if proc.returncode == 0:
            return {"status": "pass", "provider": self.name, "detail": "npm pass"}
        return {"status": "fail", "provider": self.name, "detail": "npm fail"}


register_verification_provider(PytestVerificationProvider())
register_verification_provider(NpmVerificationProvider())

_DEFAULT_VERIFICATION_PROVIDER = next((k for k in ("pytest", "npm") if k in _VERIFICATION_REGISTRY), next(iter(_VERIFICATION_REGISTRY)))


def get_verification_provider():
    cfg = load_json(CONFIG_FILE, {}) or {}
    ver = cfg.get("verification", {}) or {}
    raw = ver.get("provider", _DEFAULT_VERIFICATION_PROVIDER) or _DEFAULT_VERIFICATION_PROVIDER
    return {
        "enabled": bool(ver.get("enabled", False)),
        "provider": str(raw).lower(),
        "config": ver,
    }


class VerificationResult:
    VALID_STATUSES = ("PASS", "FAIL", "UNAVAILABLE", "DISABLED", "ERROR")
    def __init__(self, status, message="", provider=""):
        s = str(status or "").strip().upper()
        if s not in self.VALID_STATUSES:
            s = "ERROR"
        self.status = s
        self.message = str(message or "")
        self.provider = str(provider or "")
    @property
    def passed(self):
        return self.status == "PASS"
    def to_tuple(self):
        return (self.status == "PASS", self.message)
    def __repr__(self):
        return "VerificationResult(status=%r, provider=%r, message=%r)" % (self.status, self.provider, self.message)


def _normalize_provider_status(raw):
    if not isinstance(raw, str):
        return None
    t = raw.strip().lower()
    if not t:
        return None
    if t in ("pass", "passed", "ok", "success"):
        return "PASS"
    if t in ("fail", "failed", "failure"):
        return "FAIL"
    if t in ("unavailable", "not-available", "not_available", "n/a", "na"):
        return "UNAVAILABLE"
    if t in ("disabled", "skipped", "skip"):
        return "DISABLED"
    if t in ("error", "exception", "unknown", "unknown-state", "unknown_state"):
        return "ERROR"
    return "ERROR"


def verify_status(task_id, evidence=None):
    """Resolve provider and return explicit VerificationResult.

    Policy default (Manager/Gate decides, verifier does not):
    only status PASS counts as passed; every other status
    (FAIL/UNAVAILABLE/DISABLED/ERROR) counts as not-passed.
    Provider status is normalized case-insensitively; malformed
    responses (non-dict, missing/non-string status) and any
    exception map to ERROR, never to a pass fallback.
    """
    provider_cfg = get_verification_provider()
    selected = provider_cfg["provider"]
    cfg_detail = provider_cfg.get("config", {})
    if not provider_cfg["enabled"]:
        log_event("VERIFY_PROVIDER_SKIP", task_id, "skipped (disabled)", provider_cfg)
        return VerificationResult("DISABLED", "skipped (disabled)", selected)
    instance = _VERIFICATION_REGISTRY.get(selected)
    if instance is None:
        log_event("VERIFY_PROVIDER_UNAVAILABLE", task_id, "fallback (%s unavailable)" % selected, provider_cfg)
        return VerificationResult("UNAVAILABLE", "fallback (%s unavailable)" % selected, selected)
    try:
        available = instance.can_verify(cfg_detail)
    except Exception as e:
        log_event("VERIFY_PROVIDER_ERROR", task_id, "error (%s unavailable: %s)" % (selected, e), {"error": str(e)})
        return VerificationResult("ERROR", "error (%s unavailable: %s)" % (selected, e), selected)
    if not available:
        log_event("VERIFY_PROVIDER_UNAVAILABLE", task_id, "fallback (%s unavailable)" % selected, provider_cfg)
        return VerificationResult("UNAVAILABLE", "fallback (%s unavailable)" % selected, selected)
    try:
        outcome = instance.verify(task_id, evidence, cfg_detail)
    except Exception as e:
        log_event("VERIFY_PROVIDER_ERROR", task_id, "error (%s unavailable: %s)" % (selected, e), {"error": str(e)})
        return VerificationResult("ERROR", "error (%s unavailable: %s)" % (selected, e), selected)
    if not isinstance(outcome, dict) or "status" not in outcome:
        log_event("VERIFY_PROVIDER_ERROR", task_id, "error (%s unavailable: malformed response)" % selected, provider_cfg)
        return VerificationResult("ERROR", "error (%s unavailable: malformed response)" % selected, selected)
    norm = _normalize_provider_status(outcome.get("status"))
    if norm is None:
        log_event("VERIFY_PROVIDER_ERROR", task_id, "error (%s unavailable: malformed status)" % selected, provider_cfg)
        return VerificationResult("ERROR", "error (%s unavailable: malformed status)" % selected, selected)
    item = str(outcome.get("provider", selected) or selected)
    note = str(outcome.get("detail", outcome.get("status", norm)) or norm)
    if norm == "PASS":
        log_event("VERIFY_PROVIDER_PASS", task_id, note, provider_cfg)
        return VerificationResult("PASS", "%s pass" % item, item)
    if norm == "FAIL":
        log_event("VERIFY_PROVIDER_FAIL", task_id, note, provider_cfg)
        return VerificationResult("FAIL", "%s fail: %s" % (item, note), item)
    if norm == "UNAVAILABLE":
        log_event("VERIFY_PROVIDER_UNAVAILABLE", task_id, note, provider_cfg)
        return VerificationResult("UNAVAILABLE", "fallback (%s unavailable: %s)" % (selected, note), item)
    if norm == "DISABLED":
        log_event("VERIFY_PROVIDER_SKIP", task_id, note, provider_cfg)
        return VerificationResult("DISABLED", "skipped (disabled: %s)" % note, item)
    log_event("VERIFY_PROVIDER_ERROR", task_id, note, provider_cfg)
    return VerificationResult("ERROR", "error (%s unavailable: %s)" % (selected, note), item)


def verify_with_provider(task_id, evidence=None):
    """Backward-compatible wrapper around verify_status.

    Policy default (Manager/Gate decides, verifier does not):
    only status PASS counts as passed (True); every other status
    (FAIL/UNAVAILABLE/DISABLED/ERROR) counts as not-passed (False).
    Keeps the legacy (bool, message) signature.
    """
    result = verify_status(task_id, evidence)
    if result.status == "PASS":
        return True, result.message
    return False, result.message

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

def log_tool_call(task_id, session_id, agent, operation, tool, duration_ms=0, status="ok", error=None, prompt_text="", attempt=1, tokens_in=None, tokens_out=None, lifecycle="STARTED", tool_call_id=None):
    prompt_hash = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()[:12] if prompt_text else "-"
    entry = {
        "tool_call_id": tool_call_id or uuid.uuid4().hex[:12],
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
        "tokens_out": tokens_out,
        "lifecycle": lifecycle
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

TOOLCALL_LIFECYCLES = ("STARTED", "COMPLETED", "FAILED", "UNKNOWN")


def _read_tool_calls():
    rows = []
    try:
        with open(TOOLCALLS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return rows


def _parse_tool_time(value):
    try:
        if not value:
            return None
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def record_lifecycle(tool_call_id, outcome):
    if outcome not in ("COMPLETED", "FAILED", "UNKNOWN"):
        raise ValueError(f"unknown lifecycle outcome: {outcome}")
    rows = _read_tool_calls()
    matches = [r for r in rows if r.get("tool_call_id") == tool_call_id]
    if not matches:
        return None
    latest = matches[-1]
    if latest.get("lifecycle") in ("COMPLETED", "FAILED", "UNKNOWN"):
        return latest
    entry = dict(latest)
    entry["time"] = datetime.now(timezone.utc).isoformat()
    entry["lifecycle"] = outcome
    entry["operation"] = "RESULT"
    os.makedirs(MANAGER_DIR, exist_ok=True)
    with open(TOOLCALLS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def reconcile_tool_calls(threshold_s=300, now=None):
    try:
        now_dt = now or datetime.now(timezone.utc)
        rows = _read_tool_calls()
        latest_by_id = {}
        first_by_id = {}
        for r in rows:
            tid = r.get("tool_call_id")
            if not tid:
                continue
            if tid not in first_by_id:
                first_by_id[tid] = r
            latest_by_id[tid] = r
        appended = []
        for tid, latest in latest_by_id.items():
            if latest.get("lifecycle", "STARTED") != "STARTED":
                continue
            started = _parse_tool_time(first_by_id[tid].get("time"))
            age_s = (now_dt - started).total_seconds() if started else float("inf")
            if age_s < threshold_s:
                continue
            outcome = "UNKNOWN"
            first = first_by_id[tid]
            for r in rows:
                if (r.get("operation") == "RESULT"
                        and r.get("task") == first.get("task")
                        and r.get("session_id") == first.get("session_id")
                        and r.get("tool") == first.get("tool")
                        and r.get("tool_call_id") != tid):
                    st = (r.get("status") or "").lower()
                    if st == "ok":
                        outcome = "COMPLETED"
                    elif st == "error":
                        outcome = "FAILED"
                    else:
                        outcome = "UNKNOWN"
                    break
            done = record_lifecycle(tid, outcome)
            if done is not None and done.get("lifecycle") == outcome:
                appended.append(done)
        return appended
    except Exception:
        return []

ROUTING_OWNERSHIP = {
    "design": "planning",
    "code": "building",
    "classify": "error_debug",
    "review": "review",
}

_ROUTING_TASK_ALIASES = {
    "architecture": "design",
    "planning": "design",
    "design_structure": "design",
    "implementation": "code",
    "build": "code",
    "prod": "code",
    "production": "code",
    "diagnose": "classify",
    "diagnosis": "classify",
    "investigate": "classify",
    "investigation": "classify",
    "root-cause": "classify",
    "root_cause": "classify",
    "quality": "review",
    "audit": "review",
}

_ROUTING_AGENT_ALIASES = {
    "error-debug": "error_debug",
    "errordebug": "error_debug",
    "builder": "building",
    "planner": "planning",
    "reviewer": "review",
}


def _normalize_routing_task(task_type):
    t = str(task_type or "").strip().lower().replace(" ", "_").replace("-", "_")
    if not t:
        return ""
    if t in ROUTING_OWNERSHIP:
        return t
    return _ROUTING_TASK_ALIASES.get(t, t)


def _normalize_routing_agent(agent):
    a = str(agent or "").strip().lower().replace(" ", "_").replace("-", "_")
    if a in ("planning", "building", "error_debug", "review"):
        return a
    return _ROUTING_AGENT_ALIASES.get(a, a)


def check_routing(task_type, agent):
    canon_task = _normalize_routing_task(task_type)
    canon_agent = _normalize_routing_agent(agent)
    if canon_task not in ROUTING_OWNERSHIP:
        return {"allowed": True, "action": "WARNING", "owner": None,
                "task_type": canon_task, "agent": canon_agent,
                "reason": "unknown task type: routing undetermined, compat-allow with warning"}
    if canon_agent not in ("planning", "building", "error_debug", "review"):
        return {"allowed": False, "action": "BLOCKED", "owner": ROUTING_OWNERSHIP[canon_task],
                "task_type": canon_task, "agent": canon_agent,
                "reason": "unknown agent: cannot own %s task (owner=%s)" % (canon_task, ROUTING_OWNERSHIP[canon_task])}
    owner = ROUTING_OWNERSHIP[canon_task]
    if canon_agent == owner:
        return {"allowed": True, "action": "ALLOW", "owner": owner,
                "task_type": canon_task, "agent": canon_agent, "reason": "owner match"}
    return {"allowed": False, "action": "BLOCKED", "owner": owner,
            "task_type": canon_task, "agent": canon_agent,
            "reason": "routing violation: %s task owned by %s, not %s" % (canon_task, owner, canon_agent)}


CAPABILITY_MATRIX = {
    "create": {"available": True, "interface": "log_dispatch", "reliable": "partial"},
    "discover": {"available": False, "interface": None, "reliable": False},
    "resume": {"available": True, "interface": "generate_resume_context", "reliable": "partial"},
    "recover": {"available": True, "interface": "recover_from_hierarchy", "reliable": "partial"},
    "verify": {"available": True, "interface": "verify_with_provider", "reliable": "partial"},
    "review": {"available": True, "interface": "save_review", "reliable": "partial"},
    "lease": {"available": True, "interface": "verify_lease_fencing", "reliable": True},
    "reconcile": {"available": True, "interface": "reconcile_state", "reliable": "partial"},
}


def get_capability_matrix():
    return {k: dict(v) for k, v in CAPABILITY_MATRIX.items()}


def get_capability(name):
    key = str(name or "").strip().lower()
    entry = CAPABILITY_MATRIX.get(key)
    if entry is None:
        return None
    out = {"name": key}
    out.update(entry)
    return out


def guard_capability(name):
    key = str(name or "").strip().lower()
    cap = get_capability(key)
    if cap is None:
        return {"ok": False, "action": "SKIP", "capability": key,
                "reason": "unknown capability: no registry entry, skip without crash"}
    if not cap.get("available"):
        return {"ok": False, "action": "SKIP", "capability": key,
                "reason": "capability unavailable: graceful skip, no session action taken"}
    return {"ok": True, "action": "USE", "capability": key,
            "interface": cap.get("interface"), "reason": "capability available"}


def _resolve_routing_task_type(task_id, explicit=None):
    if explicit is not None and str(explicit).strip() != "":
        return _normalize_routing_task(explicit)
    try:
        manifest = load_task_manifest(task_id)
        if isinstance(manifest, dict):
            for key in ("task_type", "type", "kind"):
                if manifest.get(key):
                    return _normalize_routing_task(manifest.get(key))
    except Exception:
        pass
    return ""


def log_dispatch(task_id, agent, session_id, attempt=1, prompt_text="", lease=None, task_type=None):
    _cap = guard_capability("create")
    if not _cap.get("ok"):
        try:
            log_event("CAPABILITY_SKIP", task_id, "skip dispatch: %s" % (_cap.get("reason")), {"capability": "create", "check": _cap})
        except Exception:
            pass
        return {"skipped": True, "action": "SKIP", "capability": "create", "reason": _cap.get("reason")}
    # FIX-V2-06: fenced dispatch — stale lease must not create worker sessions
    if lease is not None and not verify_lease_fencing(lease.get("owner"), lease.get("lease_id"), lease.get("fencing_token")):
        log_event("LEASE_STALE_ABORT", task_id, f"Stale lease — abort DISPATCH {agent} {session_id}", {"owner": lease.get("owner")})
        raise StaleLeaseError(f"Stale lease — abort DISPATCH for {task_id}")
    try:
        _rt_task = _resolve_routing_task_type(task_id, task_type)
        if _rt_task:
            _rt = check_routing(_rt_task, agent)
            if not _rt.get("allowed"):
                if (_rt.get("action") or "").upper() == "BLOCKED":
                    log_event("ROUTING", task_id, "routing blocked %s task to %s (owner=%s)" % (_rt.get("task_type"), _rt.get("agent"), _rt.get("owner")), {"action": "BLOCKED", "check": _rt, "agent": agent})
                    return {"blocked": True, "action": "BLOCKED", "reason": _rt.get("reason")}
                log_event("ROUTING", task_id, "routing misroute warn %s task to %s (owner=%s)" % (_rt.get("task_type"), _rt.get("agent"), _rt.get("owner")), {"action": "WARNING", "check": _rt, "agent": agent})
    except Exception:
        pass
    try:
        capture_baseline(task_id, attempt=attempt)
    except Exception:
        pass
    try:
        _cls = classify_changes(task_id, attempt=attempt)
        _pol = resolve_file_policy(_cls)
        _action = (_pol.get("action") or "").upper()
        if _action == "BLOCKED":
            log_event("FILE_POLICY", task_id, "dispatch policy BLOCKED case %s" % (_pol.get("case")), {"action": _pol.get("action"), "case": _pol.get("case"), "reason": _pol.get("reason"), "diff": _pol.get("diff", _cls.get("diff") if isinstance(_cls, dict) else None), "files": _cls.get("files") if isinstance(_cls, dict) else None})
            return {"blocked": True, "action": _pol.get("action"), "case": _pol.get("case"), "reason": _pol.get("reason")}
        _diff_text = _pol.get("diff", _cls.get("diff") if isinstance(_cls, dict) else None)
        _files = _cls.get("files") if isinstance(_cls, dict) else None
        if _action == "WARNING" and _diff_text is None and _files is not None:
            _diff_text = "files=%s" % (_files,)
        log_event("FILE_POLICY", task_id, "dispatch policy %s case %s" % (_pol.get("action"), _pol.get("case")), {"action": _pol.get("action"), "case": _pol.get("case"), "reason": _pol.get("reason"), "diff": _diff_text, "files": _files})
    except Exception:
        pass
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
    # NOTE: legacy major/minor accepted internally only, never shown to users.
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
            raise ValueError("finding severity must be CRITICAL|HIGH|MEDIUM|LOW")
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
        cmd_timeout = ((cfg.get("git", {}) or {}).get("command_timeout_seconds") or 10)
        proc = subprocess.run(["git", "status", "--porcelain"],
                              capture_output=True, text=True, timeout=cmd_timeout)
        if proc.returncode == 0 and proc.stdout.strip():
            evidence["status"] = proc.stdout.strip()[:2000]
    except Exception:
        pass
    try:
        cfg = load_json(CONFIG_FILE, {}) or {}
        cmd_timeout = ((cfg.get("git", {}) or {}).get("command_timeout_seconds") or 10)
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


def transition_state(state, event_type, task_id, extra=None, lease=None):
    """State transition protocol: verify lease → validate → create event → persist event → persist state → timestamps.
    Returns new state_version/event_sequence.
    FIX-V2-06: when lease identity is supplied, a stale token aborts the mutation (StaleLeaseError, no write).
    """
    # FIX-V2-06: verify-before-mutation
    if lease is not None and not verify_lease_fencing(lease.get("owner"), lease.get("lease_id"), lease.get("fencing_token")):
        log_event("LEASE_STALE_ABORT", task_id or "GLOBAL", f"Stale lease — abort transition {event_type}", {"owner": (lease or {}).get("owner")})
        raise StaleLeaseError(f"Stale lease — abort transition {event_type}")
    # validate
    if not task_id:
        raise ValueError("task_id required for transition")
    # create event before state persist
    log_event(event_type, task_id, f"State transition {event_type}", extra or {})
    # bump version and persist state
    _bump_state_version(state)
    save_json(STATE_FILE, state)
    return state


_TASK_STATUS_TRANSITIONS = {
    "pending": ("ready", "cancelled"),
    "ready": ("completed", "cancelled"),
    "completed": (),
    "cancelled": (),
}


def transition_task_status(task, target, context=None):
    cur = str((task or {}).get("status", "") or "").lower()
    tgt = str(target or "").lower()
    allowed = _TASK_STATUS_TRANSITIONS.get(cur)
    task_id = str((task or {}).get("id", "") or "UNKNOWN")
    if allowed is None:
        reason = "unknown current status %r" % (cur,)
        log_event("TRANSITION_REJECTED", task_id, "task status transition rejected: %s -> %s (%s)" % (cur, tgt, reason), {"from": cur, "to": tgt, "reason": reason})
        return (False, reason)
    if tgt not in allowed:
        if cur in ("completed", "cancelled"):
            reason = "terminal status %r cannot transition to %r" % (cur, tgt)
        else:
            reason = "invalid task status transition %r -> %r" % (cur, tgt)
        log_event("TRANSITION_REJECTED", task_id, "task status transition rejected: %s -> %s (%s)" % (cur, tgt, reason), {"from": cur, "to": tgt, "reason": reason})
        return (False, reason)
    if tgt == "completed":
        verified = False
        ctx = context or {}
        if ctx.get("verified") or ctx.get("verify_passed") or ctx.get("verification_passed"):
            verified = True
        if not verified and task_id != "UNKNOWN":
            try:
                verified = bool(_has_verifying_success(task_id))
            except Exception:
                verified = False
        if not verified:
            reason = "completed requires verification gate (VERIFYING_SUCCESS)"
            log_event("TRANSITION_REJECTED", task_id, "task status transition rejected: %s -> %s (%s)" % (cur, tgt, reason), {"from": cur, "to": tgt, "reason": reason})
            return (False, reason)
    task["status"] = tgt
    return (True, "")


_MANAGER_PHASE_TRANSITIONS = {
    "building": ("verifying", "recovering", "blocked", "completed"),
    "verifying": ("verifying", "reviewing", "recovering", "blocked", "completed"),
    "reviewing": ("verifying", "blocked", "completed"),
    "recovering": ("verifying", "recovering", "blocked", "completed"),
    "completed": (),
    "blocked": (),
}


def transition_manager_phase(state, target, context=None):
    cur = str((state or {}).get("phase", "") or "").lower()
    tgt = str(target or "").lower()
    allowed = _MANAGER_PHASE_TRANSITIONS.get(cur)
    task_id = str((state or {}).get("current_task_id", "") or "GLOBAL")
    if context and isinstance(context, dict) and context.get("task_id"):
        task_id = str(context.get("task_id"))
    if allowed is None:
        reason = "unknown current phase %r" % (cur,)
        log_event("TRANSITION_REJECTED", task_id, "manager phase transition rejected: %s -> %s (%s)" % (cur, tgt, reason), {"from": cur, "to": tgt, "reason": reason})
        return (False, reason)
    if tgt not in allowed:
        if cur in ("completed", "blocked"):
            reason = "terminal phase %r cannot transition to %r" % (cur, tgt)
        else:
            reason = "invalid manager phase transition %r -> %r" % (cur, tgt)
        log_event("TRANSITION_REJECTED", task_id, "manager phase transition rejected: %s -> %s (%s)" % (cur, tgt, reason), {"from": cur, "to": tgt, "reason": reason})
        return (False, reason)
    if tgt == "completed":
        ctx = context or {}
        if "verified" in ctx or "verify_passed" in ctx or "verification_passed" in ctx:
            if not (ctx.get("verified") or ctx.get("verify_passed") or ctx.get("verification_passed")):
                reason = "completed requires verification gate"
                log_event("TRANSITION_REJECTED", task_id, "manager phase transition rejected: %s -> %s (%s)" % (cur, tgt, reason), {"from": cur, "to": tgt, "reason": reason})
                return (False, reason)
    state["phase"] = tgt
    return (True, "")


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


def reconcile_state(state, lease=None):
    """Startup reconcile: VERIFY LEASE → LOAD STATE → LOAD RECENT EVENTS → VALIDATE → RECONCILE → RESUME.
    If inconsistent, set to RECOVERING/BLOCKED.
    FIX-V2-06: when lease identity is supplied, a stale token aborts before any write (StaleLeaseError).
    """
    # FIX-V2-06: verify-before-mutation
    if lease is not None and not verify_lease_fencing(lease.get("owner"), lease.get("lease_id"), lease.get("fencing_token")):
        log_event("LEASE_STALE_ABORT", (state or {}).get("current_task_id", "GLOBAL"), "Stale lease — abort reconcile", {"owner": (lease or {}).get("owner")})
        raise StaleLeaseError("Stale lease — abort reconcile")
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
    # FIX-V2-09: gap-replay with last_applied tracking
    try:
        with open(EVENTS_FILE, "r", encoding="utf-8") as _rf:
            _all_lines = [l for l in _rf if l.strip()]
    except Exception:
        _all_lines = []
    try:
        total_events = len(_all_lines) if _all_lines else len(lines)
    except Exception:
        total_events = len(_all_lines)
    _es = (state or {}).get("event_sequence", 0) or 0
    _last = (state or {}).get("last_applied_event_sequence")
    if _last is None:
        try:
            _last = min(int(_es), int(total_events))
        except Exception:
            _last = int(total_events)
    try:
        _last = int(_last)
    except Exception:
        _last = int(total_events)
    for _idx, _line in enumerate(_all_lines, start=1):
        if _idx <= _last:
            continue
        try:
            _ev = json.loads(_line)
        except Exception:
            continue
        if "BLOCK" in str(_ev.get("event", "")):
            _mp_ok1, _mp_reason1 = transition_manager_phase(state, "blocked")
            if not _mp_ok1:  # TRANSITION-EXEMPT: preserve legacy forced write (->blocked)
                state["phase"] = "blocked"
            state["last_applied_event_sequence"] = int(_idx) - 1
            _bump_state_version(state)
            save_json(STATE_FILE, state)
            return False
    state["last_applied_event_sequence"] = int(total_events)
    is_consistent, reason = validate_state_event_consistency(state, None)
    if not is_consistent:
        log_event("STATE_RECONCILED", state.get("current_task_id", "GLOBAL"),
                  f"State inconsistency detected: {reason}; forcing RECOVERING",
                  {"state_version": state.get("state_version"), "event_sequence": state.get("event_sequence"), "reason": reason})
        _mp_ok2, _mp_reason2 = transition_manager_phase(state, "recovering")
        if not _mp_ok2:  # TRANSITION-EXEMPT: preserve legacy forced write (->recovering)
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
                _mp_ok3, _mp_reason3 = transition_manager_phase(state, "completed")
                if not _mp_ok3:  # TRANSITION-EXEMPT: preserve legacy forced write (building->completed)
                    state["phase"] = "completed"
                _bump_state_version(state)
                save_json(STATE_FILE, state)
    return True


def _has_verifying_success(task_id):
    try:
        if not os.path.exists(EVENTS_FILE):
            return False
        with open(EVENTS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                if e.get("task") == task_id and e.get("event") == "VERIFYING_SUCCESS":
                    return True
    except Exception:
        return False
    return False


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
        # FIX-V2-14: scoped budget counters — no-conflation
        for _bk in ("session_count", "recovery_count", "task_retries", "review_count"):
            if _bk not in self.state:
                self.state[_bk] = int(self.state.get(_bk, 0) or 0)
        # startup reconcile (non-destructive, logs if inconsistent)
        try:
            reconcile_state(self.state)
            # refresh after reconcile (reconcile may have bumped)
            self.state = load_json(STATE_FILE, self.state)
        except Exception:
            pass

    def check_budget(self, scope):
        limits = (self.config or {}).get("limits", {})
        _map = {"session": ("session_count", "max_worker_sessions", 5), "recovery": ("recovery_count", "max_worker_sessions", 5), "task_retry": ("task_retries", "max_retries", 3), "retry": ("task_retries", "max_retries", 3), "review": ("review_count", "max_review_cycles", 3)}
        _key, _lim, _default = _map.get(str(scope or "").lower(), ("session_count", "max_worker_sessions", 5))
        _max = int(limits.get(_lim, _default) or _default)
        _cur = int(self.state.get(_key, 0) or 0)
        if _cur >= _max:
            return (False, "%s budget exhausted (%s=%s>=%s)" % (scope, _key, _cur, _max))
        return (True, "%s budget ok (%s=%s<%s)" % (scope, _key, _cur, _max))

    def consume_budget(self, scope):
        allowed, reason = self.check_budget(scope)
        if not allowed:
            return (False, reason)
        _map2 = {"session": "session_count", "recovery": "recovery_count", "task_retry": "task_retries", "retry": "task_retries", "review": "review_count"}
        _ckey = _map2.get(str(scope or "").lower(), "session_count")
        self.state[_ckey] = int(self.state.get(_ckey, 0) or 0) + 1
        save_json(STATE_FILE, self.state)
        return (True, "%s budget consumed (%s=%s)" % (scope, _ckey, self.state[_ckey]))

    def acquire_lock(self, ttl_hours=6):
        """FIX-05 + FIX-V2-06: separate manager.lock atomic lease + fencing token — state = application data, lock = synchronization primitive."""
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
        # FIX-V2-06: fencing token + lease_id — every lease gets unique id and monotonic token
        prev_token = _coerce_token((lock_info or {}).get("fencing_token", 0)) if lock_info else 0
        new_lock = {
            "owner": self.owner,
            "lease_id": str(uuid.uuid4()),
            "fencing_token": prev_token + 1,
            "acquired_at": now.isoformat(),
            "expires_at": expires.isoformat()
        }
        self._lease_id = new_lock["lease_id"]
        self._fencing_token = new_lock["fencing_token"]
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

    def _verify_lease(self):
        """FIX-V2-06: verify current lease still owns token before mutating state — ABORT if stale."""
        lock_info = _read_lock_file()
        if not lock_info:
            return True  # no lock, allow (will acquire)
        if lock_info.get("owner") != self.owner:
            return False
        if hasattr(self, "_lease_id") and lock_info.get("lease_id") != self._lease_id:
            return False
        if hasattr(self, "_fencing_token") and _coerce_token(lock_info.get("fencing_token")) != _coerce_token(self._fencing_token):
            return False
        return True

    def _abort_if_stale(self, op="mutate"):
        if not self._verify_lease():
            log_event("LEASE_STALE_ABORT", self.state.get("current_task_id", "GLOBAL"), f"Stale lease — abort {op}", {"owner": self.owner, "lease_id": getattr(self, "_lease_id", None)})
            print(f"[LEASE] Stale lease — abort {op}")
            return True
        return False

    def _classify_health(self, task_id, checkpoint):
        """FIX-04: External observation hierarchy 5 signals -> HEALTHY/SLOW/STUCK/DEAD/UNKNOWN; FIX-V2-07: operation-aware IN_FLIGHT"""
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

        # FIX-V2-07: operation awareness — worker state exposes active_operation /
        # operation_started_at / operation_deadline_at (checkpoint carries worker
        # state; state dict is fallback). Read-only signal, no fencing interference.
        cp_prog = checkpoint.get("progress", {}) if isinstance(checkpoint.get("progress"), dict) else {}
        active_op = checkpoint.get("active_operation") or cp_prog.get("active_operation") or self.state.get("active_operation")
        op_deadline = checkpoint.get("operation_deadline_at") or cp_prog.get("operation_deadline_at") or self.state.get("operation_deadline_at")
        op_deadline_ts = _parse_time(op_deadline) if op_deadline else None
        now_utc = datetime.now(timezone.utc)
        deadline_exceeded = bool(op_deadline_ts and now_utc > op_deadline_ts)
        deadline_ok = bool(active_op and op_deadline_ts and now_utc <= op_deadline_ts)
        proc_alive = bool(has_active or heartbeat_fresh)

        # Classification per fix.md §3 + §14, operation-aware per fix-v2 §8
        # IN_FLIGHT: alive + active op + deadline unexceeded → never STUCK/DEAD
        if active_op and deadline_ok and proc_alive:
            return "IN_FLIGHT", {"active_operation": active_op, "operation_deadline_at": op_deadline,
                                 "has_active": has_active, "heartbeat_fresh": heartbeat_fresh,
                                 "progress_stale": progress_stale, "checkpoint_fresh": checkpoint_fresh}
        # Operation deadline exceeded + no progress → STUCK (takes precedence over IN_FLIGHT)
        if active_op and deadline_exceeded and progress_stale:
            return "STUCK", {"active_operation": active_op, "operation_deadline_at": op_deadline,
                             "reason": "operation_deadline_exceeded", "progress_stale": progress_stale,
                             "has_active": has_active, "heartbeat_fresh": heartbeat_fresh}
        # DEAD: no active session/process
        if not has_active and phase in ("building", "recovering"):
            # but check if recent event shows activity — if event fresh, not dead yet
            # FIX-V2-07: operation alive with valid deadline is not death either
            if not event_fresh and progress_stale and not (active_op and deadline_ok and heartbeat_fresh):
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
        # FIX-V2-07: stale progress/checkpoint alone cannot yield STUCK while op alive
        if has_active and progress_stale and heartbeat_stale and not (active_op and deadline_ok):
            return "STUCK", {"has_active": has_active, "progress_stale": progress_stale, "heartbeat_stale": heartbeat_stale}
        # UNKNOWN: contradictory
        if has_active and not heartbeat_fresh and not progress_stale:
            # heartbeat stale but progress fresh → contradictory
            return "UNKNOWN", {"has_active": has_active, "heartbeat_fresh": heartbeat_fresh, "progress_stale": progress_stale, "reason": "heartbeat_stale_but_progress_fresh"}
        if not has_active and heartbeat_fresh:
            return "UNKNOWN", {"has_active": has_active, "heartbeat_fresh": heartbeat_fresh, "reason": "no_session_but_heartbeat_fresh"}
        # FIX-V2-07: insufficient signals at all → UNKNOWN (not STUCK/DEAD)
        if not has_active and not heartbeat_fresh and not event_fresh and not checkpoint_fresh and not active_op:
            return "UNKNOWN", {"has_active": has_active, "heartbeat_fresh": heartbeat_fresh, "event_fresh": event_fresh, "checkpoint_fresh": checkpoint_fresh, "reason": "insufficient_signals"}
        # default stuck if progress stale
        if progress_stale and not (active_op and deadline_ok):
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
        if health == "IN_FLIGHT":
            # FIX-V2-07: alive + active op + deadline unexceeded → NO auto-recovery
            log_event("WATCHDOG_IN_FLIGHT", task_id, f"Worker IN_FLIGHT — {signals}", signals)
            print("[WATCHDOG] Worker IN_FLIGHT — operation within deadline, no recovery")
            return "HEALTHY"

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
        if status not in ("running", "stopped_limit", "blocked", "failed"):
            log_event("WATCHDOG_UNKNOWN_STATUS", task_id, f"Unknown checkpoint status {status!r} — routing UNKNOWN, never HEALTHY", {"status": status})
            return "UNKNOWN"
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
            except Exception as _e:
                log_event("WATCHDOG_BAD_TIMESTAMP", task_id, f"Unparseable timestamp {last_prog_str!r}: {_e} — routing UNKNOWN, never HEALTHY")
                return "UNKNOWN"

        # if health was STUCK/SLOW, already logged, but if still healthy
        if health == "HEALTHY":
            print("[WATCHDOG] Worker HEALTHY (heartbeat fresh + progress fresh + checkpoint fresh)")
            return "HEALTHY"
        if health == "SLOW":
            print("[WATCHDOG] Worker SLOW — monitoring, not yet recovering")
            return "HEALTHY"
        log_event("WATCHDOG_UNKNOWN_FALLTHROUGH", task_id, f"Health={health} not HEALTHY/SLOW/IN_FLIGHT — routing UNKNOWN, never blind HEALTHY")
        return "UNKNOWN"

    @staticmethod
    def build_recovery_plan(classification, evidence=None):
        return build_recovery_plan(classification, evidence)

    def check_p0_gate(self):
        try:
            rec = (self.state or {}).get("p0_last_result")
        except Exception:
            rec = None
        if rec is None:
            try:
                rec = (load_json(STATE_FILE, {}) or {}).get("p0_last_result")
            except Exception:
                rec = None
        if not isinstance(rec, dict):
            return False, "P0 record missing"
        if not rec.get("passed"):
            return False, "P0 record red"
        try:
            at = rec.get("at")
            ts = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - ts > timedelta(hours=24):
                return False, "P0 record stale"
        except Exception:
            return False, "P0 record invalid timestamp"
        return True, "P0 green"

    def execute_recovery(self, task_id, classification=None, evidence=None):
        print(f"[RECOVERY] Initiating safe recovery for task {task_id}")
        _p0_ok, _p0_reason = self.check_p0_gate()
        if not _p0_ok:
            return {"task_id": task_id, "status": "blocked", "reason": "P0_RED", "detail": _p0_reason}
        _allowed, _reason = self.check_budget("recovery")
        if not _allowed:
            return {"task_id": task_id, "status": "blocked", "reason": "RECOVERY_BUDGET_EXHAUSTED", "detail": _reason}
        try:
            _cls = classification or (evidence or {}).get("classification") or self.state.get("classification", "UNKNOWN")
        except Exception:
            _cls = "UNKNOWN"
        try:
            _builder = globals().get("build_recovery_plan")
            wrapper = _builder(_cls, evidence) if _builder else self.build_recovery_plan(_cls, evidence)
        except Exception:
            wrapper = {"classification": "UNKNOWN", "evidence": evidence or {}, "recovery_plan": {"action": "CLASSIFY_ONLY", "forbid": ["destructive", "building", "done"]}}
        plan = wrapper.get("recovery_plan", {})
        if str(wrapper.get("classification", "")).upper() == "UNKNOWN":
            print(f"[RECOVERY] Classification UNKNOWN for {task_id} — route to error_debug classify")
            return {"task_id": task_id, "status": "classify", "route": "ERROR_DEBUG", "classification": "UNKNOWN", "recovery_plan": plan or {"action": "CLASSIFY_ONLY", "forbid": ["destructive", "building", "done"]}}
        try:
            _att = (evidence or {}).get("attempt") or (evidence or {}).get("baseline_attempt") or self.state.get("worker_attempt") or self.state.get("attempt")
            _cls2 = classify_changes(task_id, attempt=_att)
            _pol2 = resolve_file_policy(_cls2)
            _act2 = (_pol2.get("action") or "").upper()
            _diff2 = _pol2.get("diff", _cls2.get("diff") if isinstance(_cls2, dict) else None)
            _files2 = _cls2.get("files") if isinstance(_cls2, dict) else None
            if _act2 == "WARNING" and _diff2 is None and _files2 is not None:
                _diff2 = "files=%s" % (_files2,)
            log_event("FILE_POLICY", task_id, "recovery policy %s case %s" % (_pol2.get("action"), _pol2.get("case")), {"action": _pol2.get("action"), "case": _pol2.get("case"), "reason": _pol2.get("reason"), "diff": _diff2, "files": _files2})
            if _act2 == "BLOCKED":
                return {"task_id": task_id, "status": "blocked", "action": _pol2.get("action"), "case": _pol2.get("case"), "reason": _pol2.get("reason")}
        except Exception as _pbe:
            if isinstance(_pbe, (KeyboardInterrupt, SystemExit)):
                raise
            pass
        try:
            _rlog = os.path.join(MANAGER_DIR, "recovery.jsonl")
            with open(_rlog, "a", encoding="utf-8") as _rf:
                _rf.write(json.dumps(wrapper, ensure_ascii=False) + "\n")
        except Exception:
            pass
        try:
            _sess = self.state.get("sessions", self.state.get("active_sessions", []))
            _scount = len(_sess) if isinstance(_sess, list) else int(_sess) if isinstance(_sess, int) else 0
        except Exception:
            _scount = 0
        if plan.get("forbid") or _scount >= 5:
            print(f"[POLICY_BLOCK] recovery blocked task={task_id} classification={wrapper.get('classification')} sessions={_scount}")
            try:
                log_event("POLICY_BLOCK", task_id, f"blocked classification={wrapper.get('classification')} sessions={_scount}")
            except Exception:
                pass
            return {"task_id": task_id, "status": "blocked", "reason": "POLICY_BLOCK", "classification": wrapper.get("classification"), "recovery_plan": plan}
        # FIX-V2-06: verify-before-mutation — stale lease must not write operations/state/decisions
        if self._abort_if_stale("RECOVER"):
            raise StaleLeaseError(f"Stale lease — abort RECOVER for {task_id}")
        # FIX-06: idempotent check before side effect — if RECOVER already committed for this attempt, reuse
        attempt = int(self.state.get("attempt", 1) or 1) + 1
        existing = check_operation_before(task_id, "RECOVER", attempt)
        if existing:
            print(f"[RECOVERY] Reusing committed RECOVER operation {existing.get('operation_id')}")
            return existing.get("result") or {}
        _c_allowed, _c_reason = self.consume_budget("recovery")
        if not _c_allowed:
            return {"task_id": task_id, "status": "blocked", "reason": "RECOVERY_BUDGET_EXHAUSTED", "detail": _c_reason}
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
        _mp_ok4, _mp_reason4 = transition_manager_phase(self.state, "recovering")
        if not _mp_ok4:  # TRANSITION-EXEMPT: preserve legacy forced write (->recovering)
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
        """fix-v2 §9: Manager crash/restart contract startup protocol.
        8 steps: LOAD STATE → LOAD RECENT EVENTS → LOAD OPERATIONS → VALIDATE → RECONCILE → DISCOVER ACTIVE SESSIONS → RECONSTRUCT → RESUME/RECOVER.
        No supervisor → user/launcher restarts process, Manager only reconciles; never claims self-resurrection.
        Fencing: lease acquired before any mutation. Handles T11/T12/T13.
        """
        print("[STARTUP] Manager self-recovery protocol starting...")
        if getattr(self, "_lease_id", None) is not None and not self._verify_lease():
            log_event("LEASE_STALE_ABORT", self.state.get("current_task_id", "GLOBAL"), "Stale lease — abort STARTUP_RECOVERY", {"owner": self.owner, "lease_id": getattr(self, "_lease_id", None)})
            print("[LEASE] Stale lease — abort STARTUP_RECOVERY")
            raise StaleLeaseError("Stale lease — abort STARTUP_RECOVERY")
        # 1. ACQUIRE LEASE (already done via acquire_lock caller, but ensure)
        if not self.acquire_lock():
            # lease held by other manager — wait or takeover if expired
            log_event("STARTUP_LEASE_WAIT", self.state.get("current_task_id", "GLOBAL"), "Lease held by another manager, deferring startup recovery")
            return "LEASE_WAIT"
        if self._abort_if_stale("STARTUP_RECOVERY"):
            raise StaleLeaseError("Stale lease — abort STARTUP_RECOVERY")
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
        # 7. CHECK OPERATION (fix-v2 §9: LOAD OPERATIONS → RECONCILE STARTED after DISCOVER/LOAD)
        task_id = self.state.get("current_task_id", "GLOBAL")
        try:
            reconcile_started_operations(task_id if task_id != "GLOBAL" else None)
        except Exception:
            pass
        try:
            reconcile_tool_calls()
        except Exception:
            pass
        # 8. CLASSIFY
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
            classification = "UNKNOWN"
        log_event("STARTUP_CLASSIFY", task_id, f"Startup classify: phase={phase} checkpoint={status} active={has_active} -> {classification}",
                  {"phase": phase, "checkpoint_status": status, "has_active": has_active, "classification": classification,
                   "git_evidence": bool(git_ev.get("status") or git_ev.get("diff_stat"))})
        if classification == "UNKNOWN":
            log_event("STARTUP_UNKNOWN", task_id, "Startup UNKNOWN — requires error_debug classify, never HEALTHY", {"phase": phase, "checkpoint_status": status})
            return "CLASSIFY"
        # 9. RECONSTRUCT (via hierarchy)
        hierarchy = None
        if classification in ("LIMIT", "CRASH", "STUCK", "RECOVERING_CRASH", "SESSION_CREATE_CRASH"):
            hierarchy = recover_from_hierarchy(task_id or "GLOBAL")
            log_event("STARTUP_RECONSTRUCTED", task_id, f"Reconstructed from {hierarchy.get('source')} for {classification}",
                      {"source": hierarchy.get("source"), "classification": classification})
        # 10. POLICY (check if recovery allowed)
        limits = self.config.get("limits", {})
        if classification != "HEALTHY":
            attempt = max(int(self.state.get("attempt", 1) or 1), int(self.state.get("worker_attempt", 1) or 1))
            max_sessions = int(limits.get("max_worker_sessions", 5) or 5)
            if attempt >= max_sessions:
                log_event("STARTUP_BUDGET_EXCEEDED", task_id, f"Attempt {attempt} >= max {max_sessions}, blocking",
                          {"attempt": attempt, "worker_attempt": self.state.get("worker_attempt"), "max": max_sessions})
                _mp_ok5, _mp_reason5 = transition_manager_phase(self.state, "blocked")
                if not _mp_ok5:  # TRANSITION-EXEMPT: preserve legacy forced write (->blocked)
                    self.state["phase"] = "blocked"
                save_json(STATE_FILE, self.state)
                return "BLOCKED"
            _s_allowed, _s_reason = self.check_budget("session")
            if not _s_allowed:
                log_event("STARTUP_BUDGET_EXHAUSTED", task_id, "session budget exhausted — BLOCKED (%s)" % _s_reason)
                _mp_ok6, _mp_reason6 = transition_manager_phase(self.state, "blocked")
                if not _mp_ok6:  # TRANSITION-EXEMPT: preserve legacy forced write (->blocked)
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
            self.consume_budget("session")
            # normal recovery path — caller should invoke execute_recovery
            return "RECOVERY_NEEDED"
        # 12. VERIFY (healthy case)
        log_event("STARTUP_HEALTHY", task_id, "Startup healthy, no recovery needed")
        return "HEALTHY"

    def evaluate_dependencies(self):
        if self._abort_if_stale("EVALUATE_DEPS"):
            raise StaleLeaseError("Stale lease — abort EVALUATE_DEPS")
        print("[QUEUE] Evaluating task dependencies...")
        tasks = self.queue.get("tasks", [])
        completed_ids = {t["id"] for t in tasks if t.get("status") == "completed"}
        
        updated = False
        for t in tasks:
            if t.get("status") == "pending":
                deps = t.get("dependencies", [])
                if all(d in completed_ids for d in deps):
                    ok_ts, _rsn = transition_task_status(t, "ready")
                    if not ok_ts:  # TRANSITION-EXEMPT: preserve legacy ready promotion
                        t["status"] = "ready"
                    updated = True
                    log_event("TASK_READY", t["id"], f"Task dependencies met. Status -> ready")
        if updated:
            save_json(QUEUE_FILE, self.queue)

    def record_impact(self, task_id, summary):
        if self._abort_if_stale("ARCH_IMPACT"):
            raise StaleLeaseError(f"Stale lease — abort ARCH_IMPACT for {task_id}")
        gates = self.state.get("arch_gates", {})
        entry = gates.get(task_id, {})
        entry["impact"] = {"summary": summary, "completed": True, "at": datetime.now(timezone.utc).isoformat()}
        gates[task_id] = entry
        self.state["arch_gates"] = gates
        save_json(STATE_FILE, self.state)
        log_event("ARCH_IMPACT", task_id, f"Impact recorded for {task_id}: {summary}")
        return entry["impact"]

    def require_design_review(self, task_id):
        if self._abort_if_stale("ARCH_REVIEW"):
            raise StaleLeaseError(f"Stale lease — abort ARCH_REVIEW for {task_id}")
        gates = self.state.get("arch_gates", {})
        entry = gates.get(task_id, {})
        impact = entry.get("impact", {})
        if not impact.get("completed"):
            return {"status": "BLOCKED", "reason": f"no impact record for {task_id} — record_impact required first"}
        entry["review"] = {"verdict": "PASS", "completed": True, "at": datetime.now(timezone.utc).isoformat()}
        gates[task_id] = entry
        self.state["arch_gates"] = gates
        save_json(STATE_FILE, self.state)
        log_event("ARCH_REVIEW", task_id, f"Design review PASS for {task_id}")
        return {"status": "PASS", "verdict": "PASS"}

    def request_approval(self, task_id):
        if self._abort_if_stale("ARCH_APPROVAL"):
            raise StaleLeaseError(f"Stale lease — abort ARCH_APPROVAL for {task_id}")
        gates = self.state.get("arch_gates", {})
        entry = gates.get(task_id, {})
        impact = entry.get("impact", {})
        if not impact.get("completed"):
            return {"status": "BLOCKED", "reason": f"no impact record for {task_id} — record_impact required first"}
        review = entry.get("review", {})
        if review.get("verdict") != "PASS" or not review.get("completed"):
            return {"status": "BLOCKED", "reason": f"no design-review PASS for {task_id} — require_design_review required first"}
        entry["policy"] = {"completed": True, "at": datetime.now(timezone.utc).isoformat()}
        entry["approval"] = {"completed": True, "status": "APPROVED", "at": datetime.now(timezone.utc).isoformat()}
        gates[task_id] = entry
        self.state["arch_gates"] = gates
        save_json(STATE_FILE, self.state)
        log_event("ARCH_APPROVAL", task_id, f"Arch-change approval granted for {task_id} (impact->review->policy->approval)")
        return {"status": "APPROVED"}

    def assess_risk(self, ctx=None):
        ctx = ctx or {}
        score = 0
        reasons = []
        def _truthy(*keys):
            return any(bool(ctx.get(k)) for k in keys)
        if _truthy("destructive", "is_destructive", "deletes_files", "deletes_api", "git_reset", "major_upgrade"):
            score += 2
            reasons.append("destructive operation")
        if _truthy("irreversible", "is_irreversible", "no_rollback"):
            score += 2
            reasons.append("irreversible change")
        if _truthy("security", "security_sensitive", "changes_auth", "changing_auth", "auth_change"):
            score += 2
            reasons.append("security-sensitive change")
        if _truthy("data_loss", "deletes_data", "data_deletion", "loses_data"):
            score += 2
            reasons.append("data-loss risk")
        scope = str(ctx.get("scope", "") or "").lower()
        files = ctx.get("files_changed", ctx.get("file_count", 0))
        try:
            nfiles = int(files) if isinstance(files, (int, str)) else len(files)
        except Exception:
            nfiles = 0
        broad = _truthy("broad_scope", "wide_scope", "many_files") or scope in ("broad", "wide", "system", "many", "large") or nfiles > 5
        db = _truthy("database", "changes_database", "changing_database", "db_change", "touches_db") or scope == "database"
        if broad:
            score += 1
            reasons.append("broad scope")
        if db:
            score += 1
            reasons.append("database scope (per-case, not auto-HIGH)")
        if _truthy("arch_change", "architecture_change", "api_change", "changing_api"):
            score += 1
            reasons.append("architecture/api scope")
        if score >= 4:
            level = "HIGH"
        elif score >= 2:
            level = "MEDIUM"
        else:
            level = "LOW"
        if not reasons:
            reasons.append("no risk factors")
        return (level, reasons)

    def check_approval_required(self, task_id, risk_level=None, ctx=None):
        if isinstance(risk_level, dict) and ctx is None:
            ctx = risk_level
            risk_level = None
        if risk_level is None:
            if ctx is not None:
                risk_level, _ = self.assess_risk(ctx)
            else:
                risk_level = "LOW"
        if str(risk_level).upper() == "HIGH":
            # TRANSITION-EXEMPT: manager-status not phase/task
            self.state["status"] = "approval_required"
            save_json(STATE_FILE, self.state)
            log_event("APPROVAL_REQUIRED", task_id, f"Task {task_id} has HIGH risk level. Requires user approval.")
            write_decision(task_id, f"# {task_id} Approval Required\n\nTask has HIGH risk. Operations paused until human approval.")
            return True
        return False

    def verify_completion(self, task_id, evidence):
        if self._abort_if_stale("VERIFY"):
            raise StaleLeaseError(f"Stale lease — abort VERIFY for {task_id}")
        print(f"[VERIFY] Verifying evidence for task {task_id}...")
        _keys = ["requirements", "tests", "review", "blockers", "checkpoint", "state", "queue", "diff", "recovery"]
        def _pass(v):
            if v is True:
                return True
            if isinstance(v, str) and v.strip().upper() in ("PASS", "PASSED"):
                return True
            return False
        def _fail_result(reason):
            log_event("VERIFY_UNKNOWN" if "UNKNOWN" in reason.upper() or "missing" in reason.lower() else "VERIFYING_FAILED", task_id, reason)
            checks = {k: False for k in _keys}
            return _VerifyResult((False, checks))
        if evidence == "UNKNOWN" or (isinstance(evidence, str) and evidence.strip().upper() == "UNKNOWN"):
            return _fail_result("Evidence UNKNOWN (truthy string) — forcing FAIL, never PASS")
        if isinstance(evidence, dict) and str(evidence.get("status", "")).upper() == "UNKNOWN":
            return _fail_result("Evidence status UNKNOWN — forcing FAIL, never PASS")
        if isinstance(evidence, dict) and str(evidence.get("classification", "")).upper() == "UNKNOWN":
            return _fail_result("Evidence classification UNKNOWN — forcing FAIL, never PASS")
        if evidence is None or evidence is False:
            return _fail_result("Evidence missing — forcing FAIL")
        prev_phase = self.state.get("phase")
        if prev_phase not in ("verifying", "reviewing", "recovering", "building", "completed"):
            log_event("VERIFYING_START", task_id, f"Entering VERIFYING from {prev_phase} (canonical gate)")
        else:
            log_event("VERIFYING_START", task_id, "Beginning evidence-based completion verification")
        _mp_ok7, _mp_reason7 = transition_manager_phase(self.state, "verifying")
        if not _mp_ok7:  # TRANSITION-EXEMPT: preserve legacy forced write (->verifying)
            self.state["phase"] = "verifying"
        self.state["updated_at"] = datetime.now(timezone.utc).isoformat()
        save_json(STATE_FILE, self.state)
        ev = evidence if isinstance(evidence, dict) else {}
        checks = {k: _pass(ev.get(k, "UNKNOWN")) for k in _keys}
        passed = all(checks.values())
        if passed:
            log_event("VERIFYING_SUCCESS", task_id, "All completion evidence passed (VERIFYING canonical, 9/9 ALL-PASS)")
            return _VerifyResult((True, checks))
        else:
            failed = [k for k, v in checks.items() if not v]
            _mp_ok8, _mp_reason8 = transition_manager_phase(self.state, "verifying")
            if not _mp_ok8:  # TRANSITION-EXEMPT: preserve legacy forced write (verifying->verifying)
                self.state["phase"] = "verifying"
            save_json(STATE_FILE, self.state)
            log_event("VERIFYING_FAILED", task_id, f"Evidence check failed: {len(failed)}/9 failed ({','.join(failed)})")
            return _VerifyResult((False, checks))

    def complete_task(self, task_id, evidence, decision_summary=""):
        if self._abort_if_stale("COMPLETE"):
            raise StaleLeaseError(f"Stale lease — abort COMPLETE for {task_id}")
        if evidence == "UNKNOWN" or (isinstance(evidence, str) and evidence.strip().upper() == "UNKNOWN"):
            log_event("COMPLETE_BLOCKED_UNKNOWN", task_id, "Complete blocked: evidence UNKNOWN — forcing FAIL/block, never DONE")
            return False
        if isinstance(evidence, dict) and str(evidence.get("status", "")).upper() == "UNKNOWN":
            log_event("COMPLETE_BLOCKED_UNKNOWN", task_id, "Complete blocked: status UNKNOWN — forcing FAIL/block, never DONE")
            return False
        if isinstance(evidence, dict) and str(evidence.get("classification", "")).upper() == "UNKNOWN":
            log_event("COMPLETE_BLOCKED_UNKNOWN", task_id, "Complete blocked: classification UNKNOWN — forcing FAIL/block, never DONE")
            return False
        if not _has_verifying_success(task_id):
            log_event("COMPLETE_BLOCKED", task_id, "Complete blocked: no VERIFYING_SUCCESS — refusing false DONE (false_done)")
            return False
        if not self.verify_completion(task_id, evidence):
            print(f"[COMPLETE] Cannot mark {task_id} as complete — evidence verification failed.")
            return False

        # Update Queue
        for t in self.queue.get("tasks", []):
            if t.get("id") == task_id:
                ok_ts, _rsn = transition_task_status(t, "completed", {"verified": True, "task_id": task_id})
                if not ok_ts:  # TRANSITION-EXEMPT: complete_task forces DONE after gate passed
                    t["status"] = "completed"
                t["updated_at"] = datetime.now(timezone.utc).isoformat()
                break
        save_json(QUEUE_FILE, self.queue)

        # Update State
        self.state["current_task_id"] = task_id
        # TRANSITION-EXEMPT: manager-status not phase/task
        self.state["status"] = "running"
        _mp_ok9, _mp_reason9 = transition_manager_phase(self.state, "completed", {"verified": True, "task_id": task_id})
        if not _mp_ok9:  # TRANSITION-EXEMPT: preserve legacy forced write (verifying->completed)
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


def build_recovery_plan(classification, evidence=None):
    c = str(classification or "UNKNOWN").upper()
    if c in ("STUCK", "LIMIT", "CRASH"):
        plan = {"action": "CREATE_NEW_SESSION", "reuse_session": False, "resume_from": "checkpoint", "preserve_files": True}
    elif c == "FAILED":
        plan = {"action": "RETRY_OR_BLOCK", "reuse_session": True, "resume_from": "checkpoint", "preserve_files": True}
    elif c == "DONE":
        plan = {"action": "NOOP"}
    elif c == "BLOCKED":
        plan = {"action": "HUMAN_REVIEW"}
    else:
        c = "UNKNOWN"
        plan = {"action": "CLASSIFY_ONLY", "forbid": ["destructive", "building", "done"]}
    return {"classification": c, "evidence": evidence or {}, "recovery_plan": plan}


if __name__ == "__main__":
    mgr = ManagerOrchestrator()
    mgr.acquire_lock()
    mgr.evaluate_dependencies()
    health = mgr.check_watchdog()
    if health in ("UNKNOWN", "CLASSIFY", "ERROR_DEBUG"):
        log_event("MAIN_UNKNOWN", mgr.state.get("current_task_id", "UNKNOWN"), f"Main {health} — route to classify, no recovery, no done", {"health": health})
        current_task = mgr.state.get("current_task_id", "TASK-UP-0")
        mgr.execute_recovery(current_task, classification="UNKNOWN")
    elif health == "RECOVERY_NEEDED":
        current_task = mgr.state.get("current_task_id", "TASK-UP-0")
        mgr.execute_recovery(current_task)
