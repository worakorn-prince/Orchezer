"""CU-08M merged: context compiler (from CU-02 prototype, stdlib only)."""

import hashlib
import json
from datetime import datetime, timezone

PACKAGE_FIELDS = (
    "Project",
    "Task",
    "Files",
    "Requirements",
    "Dependencies",
    "Constraints",
    "Risks",
)

SPEC_FIELD_MAP = {
    "Project": "project_context",
    "Task": "task_context",
    "Files": "relevant_files",
    "Requirements": "relevant_requirements",
    "Dependencies": "relevant_dependencies",
    "Constraints": "constraints",
    "Risks": "known_risks",
}


def _utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hash(obj):
    raw = json.dumps(obj, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def compile_global(project_meta):
    meta = dict(project_meta or {})
    data = {
        "project": meta.get("project", ""),
        "language_runtime": meta.get("language_runtime", ""),
        "conventions": list(meta.get("conventions", [])),
        "safety": list(meta.get("safety", [])),
    }
    artifact = meta.get("artifact", "ctx://global")
    version = int(meta.get("version", 1))
    return {
        "artifact": artifact,
        "version": version,
        "source_hash": meta.get("source_hash") or _hash(data),
        "generated_at": meta.get("generated_at") or _utcnow(),
        "depends_on": list(meta.get("depends_on", [])),
        "data": data,
    }


def _find_artifact(artifacts, key):
    for a in artifacts or []:
        if a.get("artifact") == key:
            return a
    return None


def compile_task(task, schemas=None, global_context=None, upstream_outputs=None):
    task = dict(task or {})
    schemas = dict(schemas or {})
    context_artifacts = list((schemas.get("context") or {}).get("artifacts", []))
    if global_context is None:
        global_context = _find_artifact(context_artifacts, "ctx://global") or {}
    upstream_outputs = dict(upstream_outputs or {})

    task_id = task.get("id", "")
    task_ref = task.get("context_ref", "ctx://tasks/%s/v1" % task_id if task_id else "")

    task_artifact = _find_artifact(context_artifacts, "ctx://tasks/%s" % task_id) or {}
    task_data = dict(task_artifact.get("data", {}))

    file_layers = [a for a in context_artifacts if a.get("layer") == "file"]
    dep_layers = [a for a in context_artifacts if a.get("layer") == "dependency"]
    relevant_deps = [
        d for d in dep_layers
        if (task_id and task_id in str(d.get("data", {}).get("to", "")))
        or task_id in list(d.get("data", {}).get("from", []))
        or task_id in list(d.get("depends_on", []))
    ]
    if not relevant_deps:
        relevant_deps = [
            d for d in dep_layers
            if any(dep in str(d.get("depends_on", [])) for dep in task.get("dependencies", []))
        ]

    gdata = dict(global_context.get("data", {}))

    project = {
        "name": gdata.get("project", ""),
        "context_ref": "%s/v%s" % (
            global_context.get("artifact", "ctx://global"),
            global_context.get("version", 1),
        ),
        "runtime": gdata.get("language_runtime", ""),
    }
    task_ctx = {
        "id": task_id,
        "title": task.get("title", ""),
        "objective": task_data.get("objective", task.get("title", "")),
        "acceptance": list(task_data.get("acceptance", [])),
        "context_ref": task_ref,
        "owns": list(task.get("owns", [])),
    }
    files = [
        {"path": a.get("data", {}).get("path", ""), "scope": a.get("data", {}).get("scope", "")}
        for a in file_layers
    ]
    requirements = list(task_ctx["acceptance"]) + list(gdata.get("conventions", []))
    dependencies = {
        "task_dependencies": list(task.get("dependencies", [])),
        "artifacts": [
            {"artifact": d.get("artifact", ""), "payload": d.get("data", {}).get("payload", "")}
            for d in relevant_deps
        ],
        "upstream_outputs": dict(upstream_outputs),
    }
    constraints = list(task_data.get("constraints", [])) + list(gdata.get("conventions", []))
    risks = list(gdata.get("safety", []))
    if task_ctx["acceptance"] and "stale context if source_hash unchecked" not in risks:
        risks = risks + ["stale context if source_hash unchecked"]

    return {
        "Project": project,
        "Task": task_ctx,
        "Files": files,
        "Requirements": requirements,
        "Dependencies": dependencies,
        "Constraints": constraints,
        "Risks": risks,
    }


def validate_completeness(pkg):
    missing = []
    if not isinstance(pkg, dict):
        return False, list(PACKAGE_FIELDS)
    for field in PACKAGE_FIELDS:
        value = pkg.get(field)
        if value is None or value == "" or value == [] or value == {}:
            missing.append(field)
    task = pkg.get("Task") or {}
    if isinstance(task, dict):
        if not task.get("id"):
            missing.append("Task.id")
        if not task.get("acceptance"):
            missing.append("Task.acceptance")
    if "Task.acceptance" in missing and "Task" in missing:
        missing = [m for m in missing if m != "Task.acceptance"]
    return (len(missing) == 0, missing)


def estimate_tokens(pkg):
    raw = json.dumps(pkg, ensure_ascii=False, sort_keys=True, default=str)
    return len(raw) // 4
