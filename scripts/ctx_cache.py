"""CU-08M merged: context cache (from CU-03 prototype, stdlib only)."""

import copy
import hashlib
import json
from datetime import datetime, timezone

_CACHE = {}
_HITS = 0
_MISSES = 0
_INVALIDATIONS = 0


def _utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hash_content(content):
    raw = json.dumps(content, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def store(artifact_id, content, depends_on=None):
    if not isinstance(artifact_id, str) or not artifact_id:
        raise ValueError("artifact_id must be a non-empty string")
    deps = list(depends_on) if depends_on else []
    source_hash = _hash_content(content)
    existing = _CACHE.get(artifact_id)
    if existing is not None and existing.get("source_hash") == source_hash:
        return copy.deepcopy(existing)
    version = int(existing.get("version", 0)) + 1 if existing is not None else 1
    record = {
        "artifact": artifact_id,
        "version": version,
        "source_hash": source_hash,
        "generated_at": _utcnow(),
        "depends_on": deps,
        "content": copy.deepcopy(content),
    }
    _CACHE[artifact_id] = copy.deepcopy(record)
    return copy.deepcopy(record)


def lookup(artifact_id):
    global _HITS, _MISSES
    record = _CACHE.get(artifact_id)
    if record is not None:
        _HITS += 1
        return copy.deepcopy(record)
    _MISSES += 1
    return None


def invalidate(changed_artifact):
    global _INVALIDATIONS
    visited = set()
    queue = [changed_artifact]
    affected = []
    if changed_artifact in _CACHE and changed_artifact not in visited:
        visited.add(changed_artifact)
        affected.append(changed_artifact)
    index = 0
    while index < len(queue):
        current = queue[index]
        index += 1
        for aid in sorted(_CACHE.keys()):
            if aid in visited:
                continue
            deps = _CACHE[aid].get("depends_on", [])
            if current in deps:
                visited.add(aid)
                queue.append(aid)
                affected.append(aid)
    for aid in affected:
        _CACHE.pop(aid, None)
    _INVALIDATIONS += len(affected)
    return list(affected)


def refresh_stale(record, current_hash):
    if record is None:
        return True
    return record.get("source_hash") != current_hash


def stats():
    total = _HITS + _MISSES
    hit_rate = (_HITS / total) if total else 0.0
    return {
        "hits": _HITS,
        "misses": _MISSES,
        "hit_rate": hit_rate,
        "invalidations": _INVALIDATIONS,
    }


def clear():
    global _HITS, _MISSES, _INVALIDATIONS
    _CACHE.clear()
    _HITS = 0
    _MISSES = 0
    _INVALIDATIONS = 0
