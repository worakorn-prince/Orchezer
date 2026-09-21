"""task_levels.py -- rule-based task level classifier (Phase 3 P3-01).

Input: task dict with owns / files / changed_files plus optional flags.
Output: (level, reasons) with level in {LOW, MEDIUM, HIGH}.
Stdlib only. Deterministic: HIGH checks run before LOW checks.
Spec: `dependencies` (DAG list) is NOT a dependency-change signal; only explicit
flags (dependency_change/deps_change/dep_change/changes_dependencies/schema_change) trigger HIGH.
"""

HIGH_SEGS = ("db", "database", "migration", "migrations", "core", "schema", "alembic")

# NOTE: `schema_change` intentionally duplicates the `schema` risk-zone by design (covers flag-set-but-no-path case).
DEP_KEYS = ("dependency_change", "deps_change", "dep_change", "changes_dependencies", "schema_change")

ARCH_KEYS = ("arch_impact", "architecture_impact", "arch_change")


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)]


def _task_paths(task):
    task = task or {}
    paths = []
    for key in ("files", "changed_files", "owns", "paths"):
        paths.extend(_as_list(task.get(key)))
    seen = set()
    uniq = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return sorted(uniq)


def _segments(path):
    parts = str(path).replace("\\", "/").split("/")
    return [p.strip().lower() for p in parts if p.strip()]


def _high_hit(paths):
    for p in paths:
        for seg in _segments(p):
            if seg in HIGH_SEGS or seg.startswith("migrat"):
                return p
    return ""


def _flag(task, keys):
    task = task or {}
    for k in keys:
        if task.get(k):
            return k
    return ""


def classify(task):
    task = task or {}
    paths = _task_paths(task)
    n = len(paths)

    hit = _high_hit(paths)
    if hit:
        return ("HIGH", ["risk-zone hit: %s" % hit])
    dep_key = _flag(task, DEP_KEYS)
    if dep_key:
        return ("HIGH", ["flag set: %s" % dep_key])

    if n == 0:
        return ("MEDIUM", ["no files: empty task with no high signal; default band"])

    arch_key = _flag(task, ARCH_KEYS)
    if n < 3 and not arch_key:
        return ("LOW", ["%d file(s) < 3 with no arch impact and no risk-zone hit" % n])

    if arch_key:
        return ("MEDIUM", ["flag set: %s; default band" % arch_key])
    if n >= 3:
        return ("MEDIUM", ["default band: %d file(s), no high signal" % n])
    return ("MEDIUM", ["default band: %d file(s)" % n])
