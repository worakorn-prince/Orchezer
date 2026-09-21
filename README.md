# Scopeboard — Observability Dashboard

File-based observability dashboards (pure Python stdlib — no `pip install`)
for project task / tool-call / session history, across projects.
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

Run the test suite (161 tests: `test_logging` 29 + `test_hardening` 78 + `test_upgrade` 10 + `test_lean_merge` 44):

```powershell
pytest scripts/ -q   # or: python -m unittest discover -s scripts -p "test_*.py" (stdlib, no installs)
```

## Log schema (the only contract to follow)

**`./.agent/manager/tool-calls.jsonl`** — one JSON object per line:
```json
{"time": "2026-09-14T08:00:00+00:00", "task": "TASK-1", "attempt": 1,
 "session_id": "ses-1", "source": "worker-1", "operation": "CALL",
 "tool": "Read", "duration_ms": 45, "status": "ok", "error": null,
 "prompt_hash": "-", "tokens_in": 800, "tokens_out": 200}
```
`source` can be anything (`worker-1`, `cli`, `my-bot` — shown as-is),
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

New fields are always optional; readers tolerate unknowns.
Log from any language — only the file format above matters — then run the
same 3 commands and the dashboards light up.

## File layout

```text
scripts/
  manager.py         # optional logging helper (log_tool_call / log_dispatch / wrap_task)
  metrics.py         # 6 metrics + efficiency + daily history snapshots
  export_json.py     # metrics+events → dashboard/data.json (single view)
  aggregate.py       # cross-project merge → dashboard/all-projects.json
  tools_inventory.py # 24-tool catalog × permission rules (optional) × actual usage
  graph.py           # Execution Graph Task→Attempt→Session (no schema change)
  risk.py            # deterministic session-risk signals
  observability.py   # manager contract → .agent/manager/observability.json (schema v1)
  failure.py         # failure taxonomy + recovery analytics
  bench.py           # synthetic benchmarks (see docs/BENCHMARKS.md)
   bootstrap.py       # init .agent/ skeleton (+ --demo sample data)
   test_logging.py    # 29 tests (schema + metrics + views)
   test_hardening.py  # 78 tests (crash/recovery/lock/idempotent/user-guard)
   test_upgrade.py    # 10 tests (watchdog + recovery + queue)
   test_lean_merge.py # 44 tests (14 lean modules merge)
   # 14 lean modules (pure, no I/O side effects):
   lean_flow.py / task_levels.py / task_contract.py / dag_waves.py
   batch_seq.py / worker_choice.py / ctx_cache.py / ctx_compiler.py
   confidence_tags.py / verify_report.py / feedback_loop.py
   failure_kinds.py / export_report.py / telemetry_collect.py
dashboard/
   index.html         # single-project view (5 charts + 17 tables)
   all.html           # cross-project view (6 charts + 20 tables)
docs/
   BENCHMARKS.md           # measured numbers + how to reproduce
   llm-telemetry-design.md # LLM telemetry: 4 rows → collector → gate
   ownership-policy.md     # file ownership + local-only rules
# root docs (spec + plan + baseline):
# design.md / implementation-plan.md / baseline-report.json
```

## Tool permission tables (optional)

With capability manifests (a folder of `.md` files containing a
`permission:` block mapping `tool → allow/deny` per profile), the dashboards
render a "tool × profile (allowed/denied/actually used)" matrix.
Without manifests that section stays empty — actual usage still shows fully.
