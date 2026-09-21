"""task_contract.py -- contract gate (P2-02). Stdlib only."""

import task_levels
import verify_report
import confidence_tags
from verify_report import _nonempty_value as _present

CONTRACT_FIELDS = ("id", "objective", "inputs", "outputs", "dependencies", "owns", "constraints", "acceptance", "verification", "risk")


def validate_contract(contract):
    data = contract if isinstance(contract, dict) else {}
    missing = []
    for key in ("id", "objective", "acceptance"):
        if not _present(data.get(key)):
            missing.append(key)
    level = task_levels.classify(data)[0]
    if level != "LOW":
        ok_v, _ = verify_report.require_verification(data)
        if not ok_v:
            missing.append("verification")
    if data.get("Requirements"):
        ok_c, _ = confidence_tags.verify_package(data)
        if not ok_c:
            missing.append("confidence-unverified")
    return (len(missing) == 0, missing, level)
