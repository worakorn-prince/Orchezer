"""verify_report.py -- verification report builder (Phase 6 P6-01).

Stdlib only. Pure functions, no file IO.
"""

from datetime import datetime, timezone

FIELDS = ("tests", "build", "files", "acceptance", "review")


def _utcnow():
    return datetime.now(timezone.utc).isoformat()


def build_report(tests: dict, build_ok: bool, files_changed: list, acceptance: dict, review: dict) -> dict:
    tests = dict(tests or {})
    acceptance = dict(acceptance or {})
    review = dict(review or {})
    files = list(files_changed or [])
    failed = tests.get("failed", 1)
    try:
        failed = int(failed)
    except (ValueError, TypeError):
        failed = 1
    all_met = acceptance.get("all_met", False)
    status = review.get("status", "")
    reasons = []
    if failed != 0:
        reasons.append("tests.failed != 0 (%r)" % (failed,))
    if not build_ok:
        reasons.append("build not ok")
    if not all_met:
        reasons.append("acceptance not all met")
    if status != "PASS":
        reasons.append("review not PASS (%r)" % (status,))
    verdict = "PASS" if not reasons else "FAIL"
    return {
        "tests": tests,
        "build": {"ok": bool(build_ok)},
        "files": files,
        "acceptance": acceptance,
        "review": review,
        "verdict": verdict,
        "reasons": reasons,
        "generated_at": _utcnow(),
    }


def valid_report(rep) -> bool:
    if not isinstance(rep, dict):
        return False
    for key in ("tests", "build", "files", "acceptance", "review", "verdict", "generated_at"):
        if key not in rep:
            return False
    if rep.get("verdict") not in ("PASS", "FAIL"):
        return False
    return True
