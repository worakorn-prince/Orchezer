"""Phase 0 baseline collection (P0-01).

Collects live execution numbers with stdlib only:
- test suite timing plus pass/fail counts via the project venv pytest
- one single pass of the scale run at
  .agent/manager/tasks/CU-09B/bench_scale.py (structure capture only)
- terminal counts from .agent/manager/queue.json

The LLM section is recorded as UNKNOWN following New-Architecture-v1
section 4: no live LLM telemetry exists in the repo, so values must be
investigated rather than assumed (ASSUMED != VERIFIED).

Writes baseline-report.json at the repo root.
"""

import datetime
import json
import pathlib
import re
import statistics
import subprocess
import sys
import time

SUITE_FILES = [
    "scripts/test_hardening.py",
    "scripts/test_logging.py",
    "scripts/test_upgrade.py",
    "scripts/test_lean_merge.py",
    "scripts/test_gates.py",
]

SCALE_SCRIPT = ".agent/manager/tasks/CU-09B/bench_scale.py"

QUEUE_FILE = pathlib.Path(".agent") / "manager" / "queue.json"

LLM_TELEMETRY_FILE = pathlib.Path(".agent") / "manager" / "llm-telemetry.jsonl"

_HERE = pathlib.Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import telemetry_collect


def build_llm(root):
    try:
        rows, _ = telemetry_collect.load_rows(str(root / LLM_TELEMETRY_FILE))
    except Exception:
        rows = []
    return telemetry_collect.llm_section(rows)

ROW_RE = re.compile(
    r"^(\d+),(\d+),([\d.]+),([\d.]+),([\d.]+),(\d+),(\d+),(\d+)\s*$"
)


def repo_root():
    return pathlib.Path(__file__).resolve().parents[1]


def git_commit(root):
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except Exception:
        pass
    return "UNKNOWN"


def run_suite(root):
    cmd = [sys.executable, "-m", "pytest"] + SUITE_FILES + ["-q"]
    start = time.perf_counter()
    proc = subprocess.run(
        cmd, cwd=str(root), capture_output=True, text=True, timeout=600
    )
    elapsed = time.perf_counter() - start
    text = (proc.stdout or "") + "\n" + (proc.stderr or "")
    passed = 0
    failed = 0
    found = re.search(r"(\d+)\s+passed", text)
    if found:
        passed = int(found.group(1))
    found = re.search(r"(\d+)\s+failed", text)
    if found:
        failed = int(found.group(1))
    total = passed + failed
    rate = (passed / total) if total else 0.0
    tail = "\n".join(text.strip().splitlines()[-5:])
    return {
        "seconds": round(elapsed, 3),
        "passed": passed,
        "failed": failed,
        "success_rate": round(rate, 4),
        "returncode": proc.returncode,
        "output_tail": tail,
    }


def run_scale(root):
    cmd = [sys.executable, SCALE_SCRIPT]
    proc = subprocess.run(
        cmd, cwd=str(root), capture_output=True, text=True, timeout=600
    )
    rows = []
    for line in (proc.stdout or "").splitlines():
        matched = ROW_RE.match(line.strip())
        if matched:
            rows.append(
                {
                    "tasks": int(matched.group(1)),
                    "workers": int(matched.group(2)),
                    "validate_ms": float(matched.group(3)),
                    "waves_ms": float(matched.group(4)),
                    "assign_ms": float(matched.group(5)),
                    "waves": int(matched.group(6)),
                    "batches": int(matched.group(7)),
                    "conflicts": int(matched.group(8)),
                }
            )
    assign_vals = [row["assign_ms"] for row in rows]
    peak = max(assign_vals) if assign_vals else 0.0
    mid = statistics.median(assign_vals) if assign_vals else 0.0
    return {
        "peak_ms": round(peak, 3),
        "median_ms": round(mid, 3),
        "rows": rows,
        "returncode": proc.returncode,
    }


def read_queue(root):
    path = root / QUEUE_FILE
    data = json.loads(path.read_text(encoding="utf-8"))
    tasks = data.get("tasks", [])
    completed = sum(1 for item in tasks if item.get("status") == "completed")
    cancelled = sum(1 for item in tasks if item.get("status") == "cancelled")
    total = len(tasks)
    return {
        "completed": completed,
        "cancelled": cancelled,
        "total": total,
        "terminal": completed + cancelled,
    }


def main():
    root = repo_root()
    suite = run_suite(root)
    scale = run_scale(root)
    queue = read_queue(root)
    llm = build_llm(root)
    generated_at = (
        datetime.datetime.now(datetime.timezone.utc).isoformat()
    )
    report = {
        "generated_at": generated_at,
        "commit": git_commit(root),
        "execution": {
            "suite_seconds": suite["seconds"],
            "passed": suite["passed"],
            "failed": suite["failed"],
            "success_rate": suite["success_rate"],
            "bench_max_ms": scale["peak_ms"],
            "bench_median_ms": scale["median_ms"],
            "bench_rows": scale["rows"],
            "queue_terminal": queue["terminal"],
            "queue_counts": {
                "completed": queue["completed"],
                "cancelled": queue["cancelled"],
                "total": queue["total"],
            },
            "suite_returncode": suite["returncode"],
            "scale_returncode": scale["returncode"],
        },
        "llm": llm,
        "confidence": {
            "execution": "VERIFIED",
            "llm": llm["status"],
            "note": (
                "execution numbers measured live in this run; "
                "llm values absent (UNKNOWN must be investigated; "
                "ASSUMED != VERIFIED per section 4)"
            ),
        },
    }
    out_path = root / "baseline-report.json"
    out_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print("wrote " + str(out_path))
    print(
        "suite: %.3fs passed=%d failed=%d rate=%.4f"
        % (
            suite["seconds"],
            suite["passed"],
            suite["failed"],
            suite["success_rate"],
        )
    )
    print(
        "scale: peak=%.3fms median=%.3fms rows=%d"
        % (scale["peak_ms"], scale["median_ms"], len(scale["rows"]))
    )
    print(
        "queue: completed=%d cancelled=%d total=%d terminal=%d"
        % (
            queue["completed"],
            queue["cancelled"],
            queue["total"],
            queue["terminal"],
        )
    )


if __name__ == "__main__":
    main()
