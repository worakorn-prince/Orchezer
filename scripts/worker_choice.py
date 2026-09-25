"""worker_choice.py -- pick agent count by task load (Phase 8 P8-01).

Input: task count, clash count, token budget dict.
Output: (n, reasons) with n in {1, 3, 5}.
Stdlib only, zero imports. Deterministic: same input gives same output.
"""


def _as_int(value, default=0):
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def _budget_tight(token_budget):
    try:
        if not isinstance(token_budget, dict):
            return False
        if "remaining" not in token_budget or "total" not in token_budget:
            return False
        rem = float(token_budget["remaining"])
        tot = float(token_budget["total"])
        if tot <= 0:
            return False
        return (rem / tot) < 0.2
    except (ValueError, TypeError):
        return False


def choose_workers(task_count, conflict_pairs=0, token_budget=None):
    n_tasks = _as_int(task_count, 0)
    n_clash = _as_int(conflict_pairs, 0)
    if n_clash < 0:
        n_clash = 0
    tight = _budget_tight(token_budget)

    if n_tasks <= 2:
        return (1, ["task_count<=2 (%d): single unit keeps order" % n_tasks])
    if n_clash > 0:
        return (1, ["clash>0 (%d pairs): single unit avoids file owns overlap" % n_clash])
    if tight:
        return (1, ["budget tight (remaining/total<0.2): single unit saves tokens"])

    if n_tasks >= 10 and n_clash == 0 and not tight:
        return (5, ["task_count>=10 (%d) with no clash and budget ok: scale to 5" % n_tasks])

    return (3, ["mid band (%d tasks, no clash, budget ok): default 3" % n_tasks])
