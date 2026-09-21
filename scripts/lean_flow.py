"""CU-08M merged: lean dispatch pipeline (from CU-05L prototype, stdlib only)."""

PACKAGE_FIELDS = (
    "Project",
    "Task",
    "Files",
    "Requirements",
    "Dependencies",
    "Constraints",
    "Risks",
)


def _compiler():
    try:
        from scripts import ctx_compiler as mod
        return mod
    except ImportError:
        import ctx_compiler as mod
        return mod


def _scheduler():
    try:
        from scripts import dag_waves as mod
        return mod
    except ImportError:
        import dag_waves as mod
        return mod


def worker_compile(task, schemas):
    mod = _compiler()
    return mod.compile_task(task, schemas=schemas)


def manager_validate(pkg):
    mod = _compiler()
    ok, missing = mod.validate_completeness(pkg)
    return bool(ok), list(missing)


def dispatch_wave(wave_tasks, completed=None, capacity_hint=None):
    mod = _scheduler()
    ordered = sorted(list(wave_tasks or []), key=lambda t: t.get("id", ""))
    n = len(ordered)
    if isinstance(capacity_hint, int) and capacity_hint > 0:
        limit = min(5, capacity_hint, n) if n else 0
    else:
        limit = min(5, n) if n else 0
    if limit <= 0:
        limit = None
    return mod.assign(ordered, capacity_hint=limit)
