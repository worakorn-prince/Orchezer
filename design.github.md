# Orchezer — Viewer Design Notes

Short design doc for **Orchezer**: stdlib-only orchestrator (`manager.py`) +
file-based observability viewer — logs in, dashboards out.

## Concepts

```text
Project
 └── Task            work unit: TASK-1, TASK-002, ...
      └── Attempt    retry counter: 1, 2, 3, ...
           └── Session        execution session: ses-1, ses-2, ...
                 └── Source    whoever did the work: any free-form name
                     ├── Tool Calls   one JSON row per call
                     └── Events       lifecycle markers (created/started/done/...)
```

The viewer itself never drives work — orchestration lives in `manager.py`
(dispatch / queue / lease / recovery / verification / watchdog).
The viewer only renders what the log files already say.
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

Optional SQLite read-model (derived cache, stdlib `sqlite3` only):
`sqlite_sync.py` syncs `events.jsonl` + `queue.json` + `checkpoint.json` into
`.agent/manager/manager_index.db` (tables `events` / `queue_snapshot` /
`checkpoints` / `sync_meta`, incremental via `last_seq`, `--rebuild` for full).
`metrics.py` / `export_json.py` accept `--db` (default `manager_index.db`),
`--no-db` (force JSONL), `--auto-sync` (opt-in sync before read,
warn-and-continue). `aggregate.py` accepts `--no-db` (default: auto-try DB
per project). DB rows are used only when fresh — `last_sync_at` age < 1h,
`last_seq` covers the current JSONL line count, and source mtime + byte size
match — otherwise readers fall back to JSONL silently.

Everything downstream is **derived and rebuildable** — raw JSONL files are the only
source of truth. Delete any generated file and rebuild it with one command.

## Modules (one line each)

| Script | Reads | Writes |
|---|---|---|
| `manager.py` | config / state / queue (optional logging API + verification provider interface) | `tool-calls.jsonl` + `TOOL_CALL` events, `operations.jsonl`, `manager.lock` |
| `metrics.py` | events + tool-calls + queue (or `manager_index.db` when fresh; `--db` / `--no-db` / `--auto-sync`) | `metrics.json`, `history/YYYY-MM-DD.json` (6 metrics + efficiency) |
| `graph.py` | events + tool-calls | execution trees Task→Attempt→Session (in-memory + CLI) |
| `risk.py` | tool-calls + checkpoint | session risk levels (observation vs recommendation) |
| `observability.py` | everything above | `observability.json` contract (schema v1) |
| `failure.py` | events + tool-calls | 7-category failure taxonomy + recovery stats |
| `tools_inventory.py` | tool-calls + permission manifests | tool × profile matrix |
| `export_json.py` | all of the above (or `manager_index.db` when fresh; `--db` / `--no-db` / `--auto-sync`) | `dashboard/data.json` (19 sections, single-project view model) |
| `aggregate.py` | N projects (per-project DB auto-try; `--no-db` forces JSONL, `--projects` overrides scan) | `dashboard/all-projects.json` (combined view model) |
| `bootstrap.py` | — | skeleton `.agent/` (+ `--demo` sample data, `--config` copies, `--sync` runs sqlite_sync) |
| `sqlite_sync.py` | events.jsonl + queue.json + checkpoint | `.agent/manager/manager_index.db` (incremental via last_seq, `--rebuild` full, `--dry-run` preview) |
| `bench.py` | synthetic tmp logs | timing/memory table to stdout (+ `test_hardening` / `test_upgrade` cover recovery) |

## Dashboard views

- `index.html` — single project: KPIs (6 metrics), overview strip, per-task / execution /
   session / activity / errors / queue / durations / profiles / slowest / files / findings /
   efficiency (+ per-profile) / risk / failure / contract / audit / tools / trend (19 sections)
- `all.html` — same, merged across projects with a `project` column everywhere

Both are dependency-free static pages (Chart.js via CDN with table fallback when offline).

## Conventions

- Python stdlib only (incl. `sqlite3`). No installs, no services, no external
  database — the optional SQLite read-model (`manager_index.db`) is a derived
  cache; JSONL files stay the only source of truth.
- Optional verification provider: OpenVisio / pytest / npm / custom verifiers plug in
  behind one interface; Core runs without any of them.
- Core vs Observability separation: Manager emits events; Observability reads events.
  No dashboard logic lives inside orchestration.
- Additive schema evolution: new fields are optional, readers tolerate unknowns.
- Never log secrets or full prompts — store hashes, not content.
- Runtime files (`./.agent/`, `dashboard/*.json`) are gitignored by design.

## Note: v0.3.0-hardened

Dashboard hardening keeps the viewer contract unchanged:
recovery hierarchy (6 layers: checkpoint → events → state → session → tree → timestamps),
worker health (5 states: HEALTHY / SLOW / STUCK / DEAD / UNKNOWN), atomic lock separation
(`manager.lock` lease apart from `state.json`), idempotent operations
(`task:operation:attempt` in `operations.jsonl`), per-task baseline + user-change
protection, and canonical `VERIFYING` (`REVIEWING → VERIFYING → DONE`) as evidence gate.
Viewers keep reading the same two JSONL files; every hardening record is optional input.
