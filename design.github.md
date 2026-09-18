# Scopeboard — Viewer Design Notes

Short design doc for the **standalone viewer**. No agent framework, no orchestration —
just files in, dashboards out.

## Concepts

```text
Project
 └── Task            work unit: TASK-1, TASK-002, ...
      └── Attempt    retry counter: 1, 2, 3, ...
           └── Session        execution session: ses-1, ses-2, ...
                └── Agent     whoever did the work: any free-form name
                     ├── Tool Calls   one JSON row per call
                     └── Events       lifecycle markers (created/started/done/...)
```

The viewer never drives work. It only renders what the log files already say.
Any harness can produce the two JSONL sources; the viewer does the rest.

## Data flow

```text
.agent/manager/tool-calls.jsonl ─┐
.agent/manager/events.jsonl ─────┼─▶ metrics.py ─▶ metrics.json ─┐
.agent/manager/queue.json ───────┘                history/*.json ─┘
                                                                  ├─▶ export_json.py ─▶ dashboard/data.json ─▶ index.html
reviews/ · history/ · checkpoint ────────────────────────────────┘
                                                                      ▲
dashboard/all-projects.json ─────────────────────────────────────────┘
        ▲
aggregate.py (scans sibling projects, --projects to override)
```

Optional sources (read when present, ignored when absent):

```text
.agent/manager/operations.jsonl ──▶ idempotent operation records (observability only)
.agent/manager/manager.lock ──────▶ lease state (owner / acquired_at / expires_at)
.agent/manager/baselines/ ────────▶ per-task baseline snapshots (audit / diff views)
.agent/manager/tasks/ ────────────▶ per-task manifests (resume / decisions context)
```

`metrics.py` reads `tool-calls.jsonl` + `events.jsonl` + `queue.json` and writes
`metrics.json` + daily `history/` snapshots. `export_json.py` merges metrics,
events, tool calls, queue, reviews, history, checkpoint, and config into
19 sections in `dashboard/data.json`. `aggregate.py` merges N single-project
payloads into the cross-project `dashboard/all-projects.json`.

Everything downstream is **derived and rebuildable** — raw JSONL files are the only
source of truth. Delete any generated file and rebuild it with one command.

## Modules (one line each)

| Script | Reads | Writes |
|---|---|---|
| `manager.py` | config / state / queue (optional logging API + verification provider interface) | `tool-calls.jsonl` + `TOOL_CALL` events, `operations.jsonl`, `manager.lock` |
| `metrics.py` | events + tool-calls + queue | `metrics.json`, `history/YYYY-MM-DD.json` (6 metrics + efficiency) |
| `graph.py` | events + tool-calls | execution trees Task→Attempt→Session (in-memory + CLI) |
| `risk.py` | tool-calls + checkpoint | session risk levels (observation vs recommendation) |
| `observability.py` | everything above | `observability.json` contract (schema v1) |
| `failure.py` | events + tool-calls | 7-category failure taxonomy + recovery stats |
| `tools_inventory.py` | tool-calls + permission manifests | tool × agent matrix |
| `export_json.py` | all of the above | `dashboard/data.json` (19 sections, single-project view model) |
| `aggregate.py` | N projects | `dashboard/all-projects.json` (combined view model) |
| `bootstrap.py` | — | skeleton `.agent/` (+ `--demo` sample data) |
| `bench.py` | synthetic tmp logs | timing/memory table to stdout (+ `test_hardening` / `test_upgrade` cover recovery) |

## Dashboard views

- `index.html` — single project: KPIs (6 metrics), overview strip, per-task / execution /
  session / activity / errors / queue / durations / agents / slowest / files / findings /
  efficiency (+ per-agent) / risk / failure / contract / audit / tools / trend (19 sections)
- `all.html` — same, merged across projects with a `project` column everywhere

Both are dependency-free static pages (Chart.js via CDN with table fallback when offline).

## Conventions

- Python stdlib only. No installs, no services, no database.
- Optional verification provider: OpenVisio / pytest / npm / custom verifiers plug in
  behind one interface; Core runs without any of them.
- Core vs Observability separation: Manager emits events; Observability reads events.
  No dashboard logic lives inside orchestration.
- Additive schema evolution: new fields are optional, readers tolerate unknowns.
- Never log secrets or full prompts — store hashes, not content.
- Runtime files (`./.agent/`, `dashboard/*.json`) are gitignored by design.

## Note: v0.3.0-hardened

Hardening makes the Manager survive crashes without changing the viewer contract:
recovery hierarchy (6 layers: checkpoint → events → state → session → tree → timestamps),
worker health (5 states: HEALTHY / SLOW / STUCK / DEAD / UNKNOWN), atomic lock separation
(`manager.lock` lease apart from `state.json`), idempotent operations
(`task:operation:attempt` in `operations.jsonl`), per-task baseline + user-change
protection, and canonical `VERIFYING` (`REVIEWING → VERIFYING → DONE`) as evidence gate.
Viewers keep reading the same two JSONL files; every hardening record is optional input.
