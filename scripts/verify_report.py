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
    if isinstance(files_changed, str):
        files = [files_changed]
    else:
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
        FIELDS[0]: tests,
        FIELDS[1]: {"ok": bool(build_ok)},
        FIELDS[2]: files,
        FIELDS[3]: acceptance,
        FIELDS[4]: review,
        "verdict": verdict,
        "reasons": reasons,
        "generated_at": _utcnow(),
    }


def valid_report(rep) -> bool:
    if not isinstance(rep, dict):
        return False
    for key in (*FIELDS, "verdict", "generated_at"):
        if key not in rep:
            return False
    if rep.get("verdict") not in ("PASS", "FAIL"):
        return False
    return True


def _nonempty_value(value) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict, set, tuple)):
        return len(value) > 0
    return bool(value)


def require_verification(contract):
    if not isinstance(contract, dict):
        return (False, ["verification"])
    if _nonempty_value(contract.get("verification")):
        return (True, [])
    acceptance_ok = _nonempty_value(contract.get("acceptance"))
    review_ok = _nonempty_value(contract.get("review"))
    if acceptance_ok and review_ok:
        return (True, [])
    if not acceptance_ok and not review_ok:
        return (False, ["acceptance", "review"])
    return (False, ["verification"])
