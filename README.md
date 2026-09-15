# Scopeboard — Agent Observability Dashboard

File-based observability dashboards (pure Python stdlib — no `pip install`)
for AI-agent tool-call / session / review history, across projects.
Works with any harness or framework — just write logs in the schema below.

## Get started in 2 steps (after cloning)

```bat
python scripts\bootstrap.py --demo
run_dashboard.bat
```
(On macOS/Linux use `sh run_dashboard.sh` for the second step.)

- `--demo` seeds sample data first → the browser opens with charts populated
- If real data already exists (`./.agent/`), drop `--demo` (the script never touches existing files)
- The browser opens the **combined view** (`dashboard/all.html`); the
  **single-project view** is `dashboard/index.html`

## Manual run (without the one-click script)

```powershell
python scripts/metrics.py --rebuild   # compute from .agent/manager/*.jsonl
python scripts/export_json.py         # → dashboard/data.json (single view)
python scripts/aggregate.py           # → dashboard/all-projects.json (combined view)
python -m http.server                 # open dashboard/*.html over http (do not open file:// directly, fetch gets blocked)
```

## Log schema (the only contract to follow)

**`./.agent/manager/tool-calls.jsonl`** — one JSON object per line:
```json
{"time": "2026-09-14T08:00:00+00:00", "task": "TASK-1", "attempt": 1,
 "session_id": "ses-1", "agent": "builder", "operation": "CALL",
 "tool": "Read", "duration_ms": 45, "status": "ok", "error": null,
 "prompt_hash": "-", "tokens_in": 800, "tokens_out": 200}
```
`agent` can be anything (`builder`, `planner`, `my-bot` — shown as-is),
recommended `operation`: `DISPATCH|CALL|RESULT`,
recommended `status`: `ok|error|blocked|limit`

**`./.agent/manager/events.jsonl`** — any rows with `time/event/task`:
```json
{"time": "...", "event": "TASK_DONE", "task": "TASK-1"}
```
Events the dashboards understand: `TASK_CREATED`,
`WORKER_STARTED/WORKER_RESUMED` (session counts), `RECOVERY_STARTED`,
`REVIEW_FAILED/REVIEW_PASSED`, `TASK_DONE/TASK_FAILED/TASK_CANCELLED`.
Unknown event names are skipped safely.

Log from any language — only the file format above matters — then run the
same 3 commands and the dashboards light up.

## File layout

```text
scripts/
  manager.py         # Optional Python logging API (log_tool_call / save_review / log_denied_attempt)
  metrics.py         # 6 metrics + efficiency + daily history/ snapshots
  export_json.py     # metrics+events → dashboard/data.json (19 sections)
  aggregate.py       # cross-project merge → dashboard/all-projects.json (auto-discovers siblings)
  tools_inventory.py # 24-tool catalog × agent permissions (optional) × actual usage
  graph.py           # Execution Graph Task→Attempt→Session (no schema change)
  risk.py            # Deterministic session-risk signals (observation vs recommendation)
  observability.py   # Manager contract → .agent/manager/observability.json (schema v1)
  failure.py         # Failure taxonomy (7 categories) + recovery analytics
  bench.py           # Synthetic benchmarks (see docs/BENCHMARKS.md)
  bootstrap.py       # init .agent/ skeleton (+ --demo sample data)
  test_logging.py    # 22 tests
dashboard/
  index.html         # single-project view (5 charts + 17 tables)
  all.html           # cross-project view (6 charts + 20 tables)
docs/
  BENCHMARKS.md      # measured numbers (10K/100K/500K) + how to run 1M/10M
```

## Tool permission tables (optional)

With agent capability manifests (a folder of `.md` files containing a
`permission:` block mapping `tool → allow/deny` per agent), the dashboards
render a "tool × agent (allowed/denied/actually used)" matrix.
Without manifests that section stays empty — actual usage still shows fully.

Run tests: `python -m unittest discover -s scripts -p "test_logging.py"`
