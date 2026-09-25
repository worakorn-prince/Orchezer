"""feedback_loop.py -- event store helpers.

Event schema: required keys task/result/failure (default "" when missing);
optional keys plan/execution/owns when available.
"""

def record_event(store, event):
    item = dict(event)
    if "task" not in item:
        item["task"] = ""
    if "result" not in item:
        item["result"] = ""
    if "failure" not in item:
        item["failure"] = ""
    return [*store, item]


def _zone_of(entry):
    owns = entry.get("owns", None)
    if owns is None:
        task = entry.get("task", None)
        if isinstance(task, dict):
            owns = task.get("owns", [])
    if isinstance(owns, str):
        owns = [owns]
    if not isinstance(owns, list):
        return None
    for path in owns:
        if isinstance(path, str) and path:
            head = path.split("/")[0].strip()
            if head:
                return head
    return None


def summarize(store):
    by_result = {}
    by_failure = {}
    zone_counts = {}
    success = 0
    total = len(store)
    for entry in store:
        result = entry.get("result", "")
        by_result[result] = by_result.get(result, 0) + 1
        if result == "PASS":
            success += 1
        failure = entry.get("failure", "")
        if failure:
            by_failure[failure] = by_failure.get(failure, 0) + 1
        zone = _zone_of(entry)
        if zone:
            zone_counts[zone] = zone_counts.get(zone, 0) + 1
    top_zone = None
    if zone_counts:
        best = max(zone_counts.values())
        candidates = sorted([z for z, c in zone_counts.items() if c == best])
        top_zone = candidates[0]
    if total > 0:
        success_rate = success / total
    else:
        success_rate = 1.0
    return {
        "total": total,
        "by_result": by_result,
        "by_failure": by_failure,
        "top_zone": top_zone,
        "success_rate": success_rate,
    }


def suggest(summary):
    out = []
    by_failure = summary.get("by_failure", {})
    if not isinstance(by_failure, dict):
        by_failure = {}
    for kind in sorted(by_failure.keys()):
        count = by_failure[kind]
        if not isinstance(count, (int, float)):
            continue
        if count >= 2:
            out.append(
                "adjust contract/validator at point of %s (seen %d times)" % (kind, count)
            )
    rate = summary.get("success_rate", 1.0)
    if isinstance(rate, (int, float)):
        low = float(rate) < 1.0
    else:
        low = False
    if low:
        out.append("add acceptance examples to cover failing cases")
    return out
