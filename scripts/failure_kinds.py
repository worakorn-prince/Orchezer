KINDS = ("TOKEN_LIMIT", "MODEL_ERROR", "CODE_ERROR", "TEST_FAIL", "CONFLICT", "UNKNOWN")

_BUDGETS = {"TOKEN_LIMIT": 2, "MODEL_ERROR": 3, "CONFLICT": 2}


def classify(signals):
    if not isinstance(signals, dict):
        return ("UNKNOWN", False)
    if signals.get("limit_hit"):
        return ("TOKEN_LIMIT", True)
    if signals.get("model_error"):
        return ("MODEL_ERROR", True)
    if signals.get("test_failed"):
        return ("TEST_FAIL", False)
    if signals.get("conflict"):
        return ("CONFLICT", True)
    if signals.get("code_error"):
        return ("CODE_ERROR", False)
    return ("UNKNOWN", False)


def budget_for(kind):
    try:
        return int(_BUDGETS.get(kind, 0))
    except Exception:
        return 0


def should_retry(kind, attempts):
    try:
        n = int(attempts)
    except Exception:
        return False
    if n < 0:
        n = 0
    return n < budget_for(kind)
