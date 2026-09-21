"""CU-08M merged: batch sequence log (from CU-06L prototype, stdlib only)."""

import time

SNAPSHOT_EVERY = 10

_SEQ = 0
_LOG = []


def reset():
    global _SEQ, _LOG
    _SEQ = 0
    _LOG = []


def current_seq():
    return _SEQ


def batch_append(events):
    global _SEQ
    items = list(events or [])
    n = len(items)
    if n == 0:
        return {"start": _SEQ + 1, "end": _SEQ, "count": 0}
    start = _SEQ + 1
    end = _SEQ + n
    for offset, ev in enumerate(items):
        rec = dict(ev) if isinstance(ev, dict) else {"value": ev}
        rec["seq"] = start + offset
        _LOG.append(rec)
    _SEQ = end
    return {"start": start, "end": end, "count": n}


def snapshot_policy(counter, every=SNAPSHOT_EVERY):
    step = int(every) if int(every) > 0 else SNAPSHOT_EVERY
    c = int(counter)
    return c > 0 and (c % step == 0)


def time_it(fn, *args, **kwargs):
    t0 = time.perf_counter()
    result = fn(*args, **kwargs)
    ms = (time.perf_counter() - t0) * 1000.0
    return result, float(ms)
