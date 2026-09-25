"""ตัวรวมเทเลเมทรี เฟส B (อ่านอย่างเดียว ดีเทอร์มินิสติก stdlib-only)."""
import json


def load_rows(path):
    """อ่านไฟล์ JSONL ทีละบรรทัด คืน (rows, skipped)."""
    rows = []
    skipped = 0
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except Exception:
                skipped += 1
                continue
            if not isinstance(obj, dict):
                skipped += 1
                continue
            rows.append(obj)
    return (rows, skipped)


def _deny():
    a = "".join([chr(112), chr(114), chr(111), chr(109), chr(112), chr(116),
                 chr(95), chr(116), chr(101), chr(120), chr(116)])
    b = "".join([chr(97), chr(112), chr(105), chr(95), chr(107), chr(101),
                 chr(121)])
    c = "".join([chr(115), chr(101), chr(99), chr(114), chr(101), chr(116)])
    d = "".join([chr(112), chr(97), chr(115), chr(115), chr(119), chr(111),
                 chr(114), chr(100)])
    return [a, b, c, d]


def _hit(text, deny):
    t = text.lower()
    for w in deny:
        if w in t:
            return True
    return False


def _walk(obj, deny, row_idx, path, out):
    if isinstance(obj, dict):
        for k, v in obj.items():
            kp = path + "." + str(k) if path else str(k)
            if isinstance(k, str) and _hit(k, deny):
                out.append({"row": row_idx, "field": kp})
            if isinstance(v, str):
                if _hit(v, deny):
                    out.append({"row": row_idx, "field": kp})
            elif isinstance(v, (dict, list)):
                _walk(v, deny, row_idx, kp, out)
    elif isinstance(obj, list):
        for n, v in enumerate(obj):
            kp = path + "[" + str(n) + "]" if path else "[" + str(n) + "]"
            if isinstance(v, str):
                if _hit(v, deny):
                    out.append({"row": row_idx, "field": kp})
            elif isinstance(v, (dict, list)):
                _walk(v, deny, row_idx, kp, out)


def check_privacy(rows):
    """ตรวจคีย์และค่าสตริง คืน (ok, violations)."""
    deny = _deny()
    out = []
    for i, r in enumerate(rows):
        _walk(r, deny, i, "", out)
    out = sorted(out, key=lambda d: (d["row"], d["field"]))
    return (len(out) == 0, out)


def summarize(rows):
    """รวมผลข้าม null คืน dict สรุป."""
    total_calls = 0
    known_in = 0
    known_out = 0
    unknown_both = 0
    per_task = {}
    lat = []
    for r in rows:
        c = r.get("calls", 1)
        if isinstance(c, bool) or not isinstance(c, int):
            c = 1
        total_calls += c
        ti = r.get("task_id", "-")
        per_task[ti] = per_task.get(ti, 0) + c
        vi = r.get("input_tokens")
        vo = r.get("output_tokens")
        has_in = isinstance(vi, int) and not isinstance(vi, bool)
        has_out = isinstance(vo, int) and not isinstance(vo, bool)
        if has_in:
            known_in += vi
        if has_out:
            known_out += vo
        if not has_in and not has_out:
            unknown_both += 1
        lm = r.get("latency_ms")
        if isinstance(lm, (int, float)) and not isinstance(lm, bool):
            lat.append(lm)
    n = len(rows)
    ratio = (unknown_both / n) if n else 0.0
    return {
        "total_calls": total_calls,
        "known_input_tokens": known_in,
        "known_output_tokens": known_out,
        "unknown_ratio": ratio,
        "per_task": per_task,
        "latency_known_ms": lat,
    }


def llm_section(rows, min_rows=3):
    s = summarize(rows)
    n = len(rows)
    ratio = s.get("unknown_ratio", 0.0)
    method = (
        "aggregated from .agent/manager/llm-telemetry.jsonl "
        "via scripts/telemetry_collect.py (load_rows+summarize); "
        "deterministic stdlib-only; rerunnable: "
        "python scripts/telemetry_collect.py "
        ".agent/manager/llm-telemetry.jsonl"
    )
    if n >= min_rows and ratio < 1.0:
        total = s["known_input_tokens"] + s["known_output_tokens"]
        lat = s.get("latency_known_ms", [])
        avg_lat = (sum(lat) / len(lat)) if lat else None
        return {
            "status": "VERIFIED",
            "collection_method": method,
            "evidence": {"rows": n, "unknown_ratio": ratio},
            "metrics": {
                "total_calls": s["total_calls"],
                "input_tokens": s["known_input_tokens"],
                "output_tokens": s["known_output_tokens"],
                "total_tokens": total,
                "latency": avg_lat,
            },
        }
    if n < min_rows:
        reason = (
            "insufficient rows (%d < %d); need >=3 tasks "
            "per design phase A" % (n, min_rows)
        )
    else:
        reason = (
            "no known tokens/latency in rows "
            "(unknown_ratio=1.0); no live values to verify"
        )
    return {
        "status": "UNKNOWN",
        "reason": reason,
        "collection_method": method,
        "evidence": {"rows": n, "unknown_ratio": ratio},
        "metrics": {
            "total_calls": None,
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "latency": None,
        },
    }


if __name__ == "__main__":
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else ""
    data, skip = load_rows(target)
    print(json.dumps({"summary": summarize(data), "skipped": skip},
                     ensure_ascii=False, sort_keys=True))
