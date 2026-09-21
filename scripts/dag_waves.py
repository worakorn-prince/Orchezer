"""CU-08M merged: DAG scheduler (from CU-04 prototype, stdlib only)."""

from __future__ import annotations

import fnmatch

READY_STATUSES = ("pending", "ready")
DONE_STATUS = "completed"


def _by_id(tasks):
    index = {}
    for t in tasks:
        tid = t.get("id", "")
        if tid in index:
            return None, tid
        index[tid] = t
    return index, ""


def validate_dag(tasks):
    index, dup = _by_id(tasks)
    if index is None:
        return False, "duplicate_id: " + dup
    for t in sorted(tasks, key=lambda x: x.get("id", "")):
        for dep in sorted(t.get("dependencies") or []):
            if dep not in index:
                return False, "unknown_dep: %s->%s" % (t.get("id", ""), dep)
    color = {}
    stack = []

    def visit(tid):
        color[tid] = 1
        stack.append(tid)
        deps = sorted((index[tid].get("dependencies") or []))
        for dep in deps:
            state = color.get(dep, 0)
            if state == 1:
                cycle = stack[stack.index(dep):] + [dep]
                return "cycle: " + "->".join(cycle)
            if state == 0:
                hit = visit(dep)
                if hit:
                    return hit
        stack.pop()
        color[tid] = 2
        return ""

    for tid in sorted(index):
        if color.get(tid, 0) == 0:
            hit = visit(tid)
            if hit:
                return False, hit
    return True, ""


def ready_tasks(tasks, completed=None):
    if completed is None:
        done = {t.get("id", "") for t in tasks if t.get("status") == DONE_STATUS}
    else:
        done = set(completed)
    out = [
        t
        for t in tasks
        if t.get("status", "pending") in READY_STATUSES
        and all(d in done for d in (t.get("dependencies") or []))
    ]
    return sorted(out, key=lambda x: x.get("id", ""))


def build_waves(tasks):
    ok, err = validate_dag(tasks)
    if not ok:
        raise ValueError(err)
    index, _ = _by_id(tasks)
    pending_deps = {tid: set(t.get("dependencies") or []) for tid, t in index.items()}
    waves = []
    done = set()
    while pending_deps:
        layer = sorted(tid for tid, deps in pending_deps.items() if deps <= done)
        if not layer:
            raise ValueError("cycle: no progress on remaining " + ",".join(sorted(pending_deps)))
        waves.append(layer)
        for tid in layer:
            done.add(tid)
            del pending_deps[tid]
    return waves


def _owns_overlap(pat_a, pat_b):
    return (
        pat_a == pat_b
        or fnmatch.fnmatch(pat_a, pat_b)
        or fnmatch.fnmatch(pat_b, pat_a)
    )


def check_conflict(task_a, task_b):
    if task_a.get("id") == task_b.get("id"):
        return False
    owns_a = task_a.get("owns") or []
    owns_b = task_b.get("owns") or []
    for pa in owns_a:
        for pb in owns_b:
            if _owns_overlap(pa, pb):
                return True
    return False


def assign(tasks, wave=None, capacity_hint=None):
    if wave is None:
        wave_tasks = list(tasks)
    else:
        index, _ = _by_id(tasks)
        wave_tasks = [index[i] for i in wave if i in index]
    ordered = sorted(wave_tasks, key=lambda x: x.get("id", ""))
    limit = capacity_hint if isinstance(capacity_hint, int) and capacity_hint > 0 else None
    batches = []
    conflicts = []
    # pigeonhole (tight): owns-conflict เกิดได้เฉพาะเมื่อ capacity_hint > distinct owns-zone ในเวฟ ไม่ใช่ >= ขนาดเวฟ
    capacity_skips = 0
    for t in ordered:
        placed = False
        for batch in batches:
            if limit is not None and len(batch) >= limit:
                capacity_skips += 1
                continue
            hit = None
            for member in batch:
                if check_conflict(t, member):
                    hit = member
                    break
            if hit is None:
                batch.append(t)
                placed = True
                break
            pair = sorted([t.get("id", ""), hit.get("id", "")])
            if pair not in conflicts:
                conflicts.append(pair)
        if not placed:
            batches.append([t])
    plan = {
        "batches": [[m.get("id", "") for m in b] for b in batches],
        "conflicts": sorted(conflicts),
        "capacity_skips": capacity_skips,
        "reason": "wave of %d split into %d conflict-free batches; %d conflict pairs; %d capacity skips"
        % (len(ordered), len(batches), len(conflicts), capacity_skips),
    }
    return plan
