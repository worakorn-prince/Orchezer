# Scopeboard — Agent Observability Dashboard

> **v0.3.0-hardened** — Manager Hardening complete: 18 tasks
> (`FIX-17`, `FIX-01`–`FIX-16`, `FIX-18`), 46 tests passing.
> Recovery hierarchy (6 layers), worker health
> (`HEALTHY`/`SLOW`/`STUCK`/`DEAD`/`UNKNOWN`), atomic lock separation,
> idempotent operations, baseline + user-change protection, canonical
> `VERIFYING` state, optional verification provider, Manager Core vs
> Observability separation. See `fix.md` and `design.md` §26 for details.

File-based observability dashboards (pure Python stdlib — no `pip install`)
for AI-agent tool-call / session / review history, across projects.
Works with any harness or framework — just write logs in the schema below.

The Manager (orchestration, state, recovery, policy, verification) emits
events; the Observability layer (metrics, dashboards, history, inventory)
only reads them. No dashboard logic lives inside orchestration.

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

Run the test suites (46 tests, stdlib `unittest`, no installs):

```powershell
python -m unittest discover -s scripts -p "test_logging.py"    # 22 tests — log schema + metrics + dashboards
python -m unittest discover -s scripts -p "test_hardening.py"  # 17 tests — crash / recovery / lock / idempotency / user-change
python -m unittest discover -s scripts -p "test_upgrade.py"    # 7 tests — watchdog + idempotent recovery + queue
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

Manager internals (`state_version` / `event_sequence` in state, worker
`health`, idempotent `operations.jsonl` records) are consistency and
recovery machinery — viewers only need the two files above. New fields
are always optional; readers tolerate unknowns.

Log from any language — only the file format above matters — then run the
same 3 commands and the dashboards light up.

## File layout

```text
scripts/
  manager.py         # Manager Core: orchestration + recovery + policy + verification
                     # (~1,190 lines: 6-layer recovery hierarchy, 5-state health,
                     #  atomic manager.lock lease, idempotent operations.jsonl,
                     #  baseline capture, canonical VERIFYING, task manifest,
                     #  optional verification provider; no dashboard logic)
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
  test_logging.py    # 22 tests (schema + metrics + views)
  test_hardening.py  # 17 tests (crash / hierarchy / lock / idempotency / user-change)
  test_upgrade.py    # 7 tests (watchdog + recovery + queue)
.agent/manager/
  tool-calls.jsonl   # raw tool-call log (source of truth for dashboards)
  events.jsonl       # lifecycle + recovery events (source of truth)
  operations.jsonl   # idempotent operation records (task:operation:attempt)
  manager.lock       # atomic lease (owner / acquired_at / expires_at), separate from state
  baselines/         # per-task baseline snapshot (hashes at TASK START)
  tasks/             # per-task manifest (manifest.json + resume + decisions)
dashboard/
  index.html         # single-project view (5 charts + 17 tables)
  all.html           # cross-project view (6 charts + 20 tables)
docs/
  BENCHMARKS.md      # measured numbers (10K/100K/500K) + how to run 1M/10M
```

## Manager Hardening (v0.3.0)

The `v0.3.0-hardened` release makes the Manager survive worker, session,
and Manager-own crashes without losing tasks or duplicating sessions:

- **Recovery hierarchy (6 layers)** — checkpoint (preferred) → event log →
  manager state → active session → working tree / git evidence →
  filesystem timestamps. A missing checkpoint never means "no progress".
- **Worker health (5 states)** — `HEALTHY` / `SLOW` / `STUCK` / `DEAD` /
  `UNKNOWN`, fused from heartbeat, progress, checkpoint freshness, session
  status, and event activity. Heartbeat alone is evidence, not proof.
- **Lock separation** — `manager.lock` is an atomic lease
  (`owner` / `acquired_at` / `expires_at`), separate from `state.json`,
  so two managers can never control the same task and a crash never
  leaves a permanent lock.
- **Idempotent operations** — every side effect (`CREATE_SESSION`,
  `RESUME`, `RECOVER`, `REVIEW`, `DISPATCH`) is recorded as
  `task:operation:attempt` in `operations.jsonl`; committed results are
  reused after restart instead of re-executed.
- **Baseline + user-change protection** — a per-task baseline is captured
  at start; recovery combines baseline + checkpoint files + current tree
  + git diff, and never resets or overwrites user edits.
- **Canonical `VERIFYING`** — `REVIEWING → VERIFYING → DONE`; completion
  requires evidence (requirements, tests, diff, state), never a worker
  claim alone.
- **Optional verification provider** — OpenVisio / pytest / custom
  verifiers plug in behind one interface; Manager Core runs without any
  of them.
- **Core vs Observability** — orchestration emits events; metrics,
  dashboards, and history only read them.

Delivered in 5 phases (see `fix.md` §22, implemented in `design.md` §26):

```text
FIX-1 Recovery Foundation  — hierarchy, state/event consistency, self-recovery,
                             watchdog, lock separation, idempotent operations
FIX-2 Safety               — baseline snapshot, user-change protection,
                             approval integration, recovery evidence
FIX-3 State/Contract       — severity CRITICAL/HIGH/MEDIUM/LOW, attempt semantics
  Cleanup                    (worker_attempt / recovery_attempt / task_retry /
                             review_cycle), canonical VERIFYING, re-entrant
                             recovery, task manifest
FIX-4 Architecture Cleanup — optional verification provider,
                             Core vs Observability separation
FIX-5 Validation           — T11–T22 matrix, all P0 tests passing (46/46)
```

## Tool permission tables (optional)

With agent capability manifests (a folder of `.md` files containing a
`permission:` block mapping `tool → allow/deny` per agent), the dashboards
render a "tool × agent (allowed/denied/actually used)" matrix.
Without manifests that section stays empty — actual usage still shows fully.

Run tests: `python -m unittest discover -s scripts -p "test_*.py"` (46 tests, all passing)
