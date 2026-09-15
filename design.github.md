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

## Data flow

```text
.agent/manager/tool-calls.jsonl ─┐
.agent/manager/events.jsonl ─────┼─▶ metrics.py ─▶ metrics.json ─┐
.agent/manager/queue.json ───────┘                                ├─▶ export_json.py ─▶ dashboard/data.json ─▶ index.html
reviews/ · history/ · checkpoint ────────────────────────────────┘
                                                                     ▲
dashboard/all-projects.json ─────────────────────────────────────────┘
        ▲
aggregate.py (scans sibling projects, --projects to override)
```

Everything downstream is **derived and rebuildable** — raw JSONL files are the only
source of truth. Delete any generated file and rebuild it with one command.

## Modules (one line each)

| Script | Reads | Writes |
|---|---|---|
| `manager.py` | — | `tool-calls.jsonl` + `TOOL_CALL` events (optional logging API) |
| `metrics.py` | events + tool-calls | `metrics.json`, `history/YYYY-MM-DD.json` |
| `graph.py` | events + tool-calls | execution trees Task→Attempt→Session (in-memory + CLI) |
| `risk.py` | tool-calls + checkpoint | session risk levels (observation vs recommendation) |
| `observability.py` | everything above | `observability.json` contract (schema v1) |
| `failure.py` | events + tool-calls | 7-category failure taxonomy + recovery stats |
| `tools_inventory.py` | tool-calls + permission manifests | tool × agent matrix |
| `export_json.py` | all of the above | `dashboard/data.json` (single-project view model) |
| `aggregate.py` | N projects | `dashboard/all-projects.json` (combined view model) |
| `bootstrap.py` | — | skeleton `.agent/` (+ `--demo` sample data) |
| `bench.py` | synthetic tmp logs | timing/memory table to stdout |

## Dashboard views

- `index.html` — single project: KPIs, overview strip, per-task / execution / session /
  activity / errors / queue / durations / agents / slowest / files / findings /
  efficiency / risk / failure / contract / audit / tools / trend
- `all.html` — same, merged across projects with a `project` column everywhere

Both are dependency-free static pages (Chart.js via CDN with table fallback when offline).

## Conventions

- Python stdlib only. No installs, no services, no database.
- Additive schema evolution: new fields are optional, readers tolerate unknowns.
- Never log secrets or full prompts — store hashes, not content.
- Runtime files (`./.agent/`, `dashboard/*.json`) are gitignored by design.
