STATES = ("KNOWN", "ASSUMED", "UNKNOWN", "VERIFIED")
def tag(entry):
    c = (entry or {}).get("confidence", "ASSUMED")
    if c not in STATES: return "ASSUMED"
    if c == "VERIFIED" and not (entry or {}).get("verified_by"): return "ASSUMED"
    return c
def verify_package(pkg):
    reqs = (pkg or {}).get("Requirements") or []
    unverified = []
    for i, r in enumerate(reqs):
        s = tag(r if isinstance(r, dict) else {"confidence": "ASSUMED"})
        if s == "UNKNOWN": unverified.append(i)
        elif s == "ASSUMED" and not (r.get("verification") if isinstance(r, dict) else None): unverified.append(i)
    return (len(unverified) == 0, unverified)
