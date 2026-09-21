# Manager Agent Architecture

## 1. Goal

Build a **Manager Agent** for OpenCode as the orchestrator of an agent team. Users talk
to the Manager, which owns workflow control, the task queue, agent routing, and worker
session lifecycles.

The single most important goal of v1 is to fix this problem:

> A Building agent works until it hits the tool-call limit and stops, forcing the user
> to open a new session and order "continue" manually.

The Manager must be able to open a new Building session and resume work from a
persistent checkpoint automatically.

---

## 2. Available agents

Current agents:

1. **Planning** — plans work and writes the structure into `design.md`
2. **Building** — reads `design.md` and writes/edits code
3. **Generic** — general work or tasks that fit no specific role
4. **Review** — reviews code, tests, regressions and security
5. **error_debug** — debugs, analyzes root causes and fixes complex logic issues
6. **Manager** — orchestrator (upgraded to a reliable orchestration layer, see §26):
   Worker Watchdog, Progress/Heartbeat, Evidence-based Completion, Idempotent Recovery,
   Event Log, Context Manager, Lease/Lock and Policy/Human Approval
7. **openvisio-planner / openvisio-builder / openvisio-reviewer** — thin per-project
   wrappers (3 roles × 2 projects = 6 files under `.opencode/agent/`) that always call
   the shared `openvisio-graph` skill before searching/planning/reviewing. Not brand-new
   agents (see §25).

---

## 3. Key principles

The Manager **must not become a second coding agent**.

The Manager is responsible for:

- receiving requirements from the user
- analyzing requirements
- breaking work into tasks
- ordering tasks
- selecting the right agent
- creating and controlling OpenCode sessions
- checking worker status
- detecting tool-limit stops
- opening new sessions when needed
- resuming work from checkpoints
- handling failure/retry
- sending implementations to Review
- looping the workflow until tasks pass completion criteria

Building owns production code directly.

---

# 4. High-Level Architecture

```text
                           USER
                            |
                            v
                    +---------------+
                    |    MANAGER    |
                    |  Orchestrator |
                    +-------+-------+
                            |
          +-----------------+-----------------+
          |                 |                 |
          v                 v                 v
       PLANNING         ERROR_DEBUG        TASK QUEUE
          |                 |                 |
          v                 v                 v
     design.md          findings         prioritized
                            |
                            +----------------+
                                             |
                                             v
                                       +-----------+
                                       | BUILDING  |
                                       +-----+-----+
                                             |
                                      checkpoint
                                             |
                                             v
                                      tool-call limit
                                             |
                                             v
                                    +----------------+
                                    | NEW BUILDING  |
                                    |    SESSION    |
                                    +-------+--------+
                                            |
                                            v
                                      resume work
                                            |
                                            v
                                       +---------+
                                       | REVIEW  |
                                       +----+----+
                                            |
                              +-------------+-------------+
                              |                           |
                            PASS                         FAIL
                              |                           |
                              v                           v
                            DONE                      BUILDING
```

---

# 5. User Interaction Model

Users should talk to the Manager first and foremost.

Examples:

```text
Finish everything in design.md
```

or:

```text
Add a Graph Export API
```

or:

```text
Fix slow search when memory volume is large
```

Users never need to decide whether to call Planning, error_debug, Building or Review.

The Manager routes for them.

---

# 6. Main Workflow

## 6.1 New work from design.md

```text
USER
 |
 v
MANAGER
  |
  v
Read design.md
  |
  v
Check current state
  |
  v
BUILDING
  |
  v
checkpoint
  |
  v
Hit tool limit?
  |
  +-- NO --> continue
  |
  +-- YES
        |
        v
    Manager checks checkpoint
        |
        v
    Open new Building session
        |
        v
    resume
        |
        v
    continue
```

Repeat until the implementation is done.

Then:

```text
BUILDING
   |
   v
REVIEW
   |
   +-- PASS --> DONE
   |
   +-- FAIL --> BUILDING
                     |
                     v
                   REVIEW
```

Review FAIL over 3 rounds → mark BLOCKED + notify the user | max 5 worker sessions per task

---

# 7. Persistent State

Never rely primarily on a Building session's conversation context.

Every session must be restartable from state on disk.

Recommended structure:

```text
.agent/
├── manager/
│   ├── state.json
│   ├── queue.json
│   ├── events.jsonl
│   ├── config.json
│   ├── decisions/
│   ├── context/
│   └── changes/
│
└── building/
    └── checkpoint.json
```

Runtime files under `.agent/` (state.json, queue.json, checkpoint.json, events.jsonl) must stay in `.gitignore`, never enter git.

Principles (see §26):

- `state.json` = current truth
- `events.jsonl` = historical truth (append-only)
- `config.json` = policy/limits (never hard-code in logic)
- `context/` = resume context per task
- `decisions/` = decision log per task

---

# 8. Manager State

File:

```text
.agent/manager/state.json   # application data
.agent/manager/manager.lock # synchronization primitive (separate file, see §26.7)
```

Example (schema_version 2, hardened):

```json
{
  "schema_version": 2,
  "state_version": 14,
  "event_sequence": 118,
  "last_applied_event_sequence": 118,
  "project": "example",
  "status": "running",
  "current_task_id": "TASK-001",
  "phase": "building",
  "active_agent": "building",
  "active_session_id": "session-id",
  "worker_attempt": 2,
  "recovery_attempt": 1,
  "task_retry": 0,
  "review_cycle": 1,
  "last_progress_at": "ISO-8601",
  "last_checkpoint_at": "ISO-8601",
  "heartbeat_at": "ISO-8601",
  "updated_at": "ISO-8601"
}
```

Lock lives separately in `manager.lock` (atomic lease — see §26.7), not inside state.json:

```json
{ "owner": "manager-01", "acquired_at": "ISO-8601", "expires_at": "ISO-8601" }
```

Should store at minimum:

- schema_version + state_version + event_sequence
- project
- overall status + phase
- current task
- active agent / session
- worker_attempt / recovery_attempt / task_retry / review_cycle (disambiguated, see §26.21)
- heartbeat / progress / checkpoint timestamps
- updated_at
- last known result

State transition protocol (see §26.5): validate → create event → persist event → persist state → update timestamps. On startup (see §26.29 crash/restart contract): LOAD STATE → LOAD RECENT EVENTS → LOAD OPERATIONS → VALIDATE → RECONCILE (incl. `reconcile_started_operations()`) → DISCOVER ACTIVE SESSIONS → RECONSTRUCT → RESUME/RECOVER. Inconsistent state → RECOVERING or BLOCKED, never duplicate worker. The Manager process never resurrects itself — with a supervisor the supervisor restarts it, without one the user/launcher restarts it and the Manager reconciles.

---

# 9. Task Queue

File:

```text
.agent/manager/queue.json
```

Example:

```json
{
  "tasks": [
    {
      "id": "TASK-001",
      "type": "feature",
      "title": "Graph Export API",
      "priority": "normal",
      "status": "pending",
      "dependencies": []
    }
  ]
}
```

Each task should have:

- id
- type
- title
- description
- priority
- status
- dependencies
- created_at
- updated_at

---

# 10. Building Checkpoint

File:

```text
.agent/building/checkpoint.json
```

Example:

```json
{
  "task_id": "TASK-001",
  "status": "running",
  "phase": "implementation",
  "completed": [
    "database schema",
    "repository layer"
  ],
  "current": "graph traversal API",
  "remaining": [
    "graph traversal API",
    "tests",
    "integration"
  ],
  "files_changed": [],
  "tests": {
    "status": "not_run",
    "details": ""
  },
  "blockers": [],
  "next_action": "implement graph traversal API",
  "updated_at": "ISO-8601"
}
```

Building should update the checkpoint after major milestones and before a session ends, whenever possible.

---

# 11. Automatic Session Resume

When a Building session hits the tool-call limit:

### The Manager must

1. verify the session has actually stopped
2. read `state.json`
3. read `checkpoint.json`
4. inspect the working tree
5. check whether unfinished work remains
6. create a new Building session
7. send the necessary task context
8. send `design.md`
9. send `checkpoint.json`
10. order continuation from `next_action`
11. never redo completed work unnecessarily
12. monitor the new session
13. repeat until the task is complete

### Detection signals (status protocol)

- Building must write `checkpoint.json` with a `status` field every time before a session ends. Valid values: `running` | `stopped_limit` | `done` | `failed` | `blocked`
- Checkpoint is the **preferred recovery state**, not the absolute source of truth (see fix.md §2)
- The Manager must use the recovery evidence hierarchy when checkpoint is missing or stale:

```text
1. Checkpoint (preferred)
2. Event Log (.agent/manager/events.jsonl)
3. Manager State (.agent/manager/state.json + state_version/event_sequence)
4. Active Session State (OpenCode session/process status)
5. Working Tree / Git Evidence (git status, git diff, file hashes vs baseline)
6. Filesystem timestamps
```

Flow when checkpoint missing/stale:

```text
Checkpoint → Events → State → Active session → git status/diff → timestamps → reconstruct state
```

Rules:
- `missing checkpoint ≠ no progress` — Manager must reconstruct and record which evidence was used (`reconstructed_from`)
- Tool-limit before checkpoint and crash before checkpoint must both be recoverable
- Recovery must log the evidence source that led to the decision

---

# 12. Resume Prompt Contract

Every new Building session should receive instructions shaped like this:

```text
Continue the current task.

Read:
- design.md
- .agent/manager/state.json
- .agent/building/checkpoint.json

Check the current working tree before making changes.

Continue from the recorded `next_action`.

Do not redo completed work unless verification shows that it is incomplete.

Preserve unrelated user changes.

Update checkpoint.json after meaningful milestones.

Run relevant tests before reporting completion.

Call the `openvisio-graph` skill (`graph-skeleton` + equivalent proof) before every resume when the task touches indexed projects (see §25).
```

---

# 13. Adding a feature missing from design.md

Users don't need to know which agent should receive the work.

The user simply tells the Manager:

```text
Add a Graph Export API
```

The Manager analyzes impact.

## Case A — Small Change

No impact on architecture or core design:

```text
USER
 |
 v
MANAGER
  |
  v
Analyze
  |
  v
Add task
  |
  v
BUILDING
```

No need to call Planning again.

---

## Case B — Design Change

The feature needs more detail in design but doesn't change architecture:

```text
USER
 |
 v
MANAGER
 |
 v
Planning
 |
 v
update design.md
 |
 v
Building
 |
 v
Review
```

---

## Case C — Architectural Change

Examples:

```text
Switch from SQLite to PostgreSQL
```

or:

```text
Change the memory system architecture
```

Workflow:

```text
USER
 |
 v
MANAGER
 |
 v
Impact Analysis
 |
 v
PLANNING
 |
 v
update design.md
 |
 v
Design Review
 |
 v
BUILDING
 |
 v
REVIEW
```

The Manager must never send an architectural change straight to Building.

---

# 14. Change Request

The Manager is advised to record new features/requirements as change requests.

Structure:

```text
.agent/manager/changes/
├── CR-001.md
├── CR-002.md
└── CR-003.md
```

Example:

```markdown
# CR-001 — Graph Export API

## Request

Add an API to export the memory graph as JSON

## Source

User request

## Impact

Medium

## Decision

Must update design.md before implementation

## Status

planning
```

The advantage is traceability: where a new requirement came from and how the Manager decided.

---

# 15. Agent Routing Rules

| Situation | Agent |
|---|---|
| design architecture | Planning |
| edit/add design | Planning |
| don't know where code lives | error_debug |
| analyze root cause | error_debug |
| write code | Building |
| fix general bugs | Building |
| fix complex logic bugs | error_debug |
| review implementation | Review |
| check regression | Review |
| check security | Review |
| need graph/symbol first | openvisio-planner + openvisio-graph skill |
| check graph before review | openvisio-reviewer (export-verify + baseline compare) |
| worker stuck / state UNKNOWN | error_debug (classify before resume — no blind resume) |
| HIGH risk operation | user approval first (APPROVAL_REQUIRED) |
| general work | Generic |
| control workflow | Manager (reliable orchestration layer — see §26) |
| cloudflare audit (explicit full 6-phase audit) | Review + cloudflare-security-audit skill (checklist + validator, guidance default, severity CRITICAL/HIGH/MEDIUM/LOW, needs_validation blocker) |
| security review (default) | Review + security-reviewer (never call cloudflare, fast SAST path) |

---

# 16. Manager Decision Rules

1. The Manager must not edit production code directly in v1
2. Architectural changes must go through Planning
3. Unknown codebase/root cause goes to error_debug first
4. Implementation goes to Building
5. Meaningful implementations must pass Review
6. If Building stops early, resume from checkpoint
7. If Building hits the tool limit, open a new session automatically
8. Never consider a task done just because Building says so
9. Must check tests/state/review before marking DONE
10. Never delete or overwrite unrelated user changes
11. Never git-reset without verification
12. Must keep unfinished tasks in the queue
13. Limit retries on repeated failure
14. If requirements are ambiguous with significant implementation impact, ask the user

---

# 17. State Machine (Canonical — fix.md §19 / §26.10)

The Manager must use the canonical state machine (VERIFYING is a real state, RECOVERING is separate):

```text
                         ┌──────────────┐
                         │     IDLE     │
                         └──────┬───────┘
                                ↓
                         ┌──────────────┐
                         │   PLANNING   │
                         └──────┬───────┘
                                ↓
                         ┌──────────────┐
                         │   BUILDING   │
                         └───┬──────┬───┘
                             │      │
                  LIMIT/STUCK│      │normal completion
                             ↓      ↓
                      ┌──────────┐  ┌──────────────┐
                      │RECOVERING│  │   REVIEWING  │
                      └────┬─────┘  └──────┬───────┘
                           │                │
                           ↓                │FAIL
                      BUILDING ←────────────┘
                                            │PASS
                                            ↓
                                     ┌──────────────┐
                                     │  VERIFYING   │
                                     └──────┬───────┘
                                            │
                                      ┌─────┴─────┐
                                      ↓           ↓
                                    DONE       BLOCKED

Other terminal/control states: FAILED, CANCELLED, APPROVAL_REQUIRED
```

Rules:
- `REVIEWING → DONE` bypassing `VERIFYING` is forbidden — every PASS must go through VERIFYING (evidence gate)
- `RECOVERING` separates recovery from normal building; recovery itself is stateful and re-entrant (see §26.24)
- `BUILDING → RECOVERING → BUILDING` loop is bounded by `max_worker_sessions` (see §26.8 / §26.21)

Definitions:
- `REVIEWING` = detect implementation quality / bugs / regression / security
- `VERIFYING` = detect requirement + evidence + state + tests + diff + final acceptance

---

# 18. Failure Handling

## Tool-call Limit

```text
Building
  |
  v
LIMIT
  |
  v
checkpoint
  |
  v
new session
  |
  v
resume
```

## Unexpected Stop

```text
Building stopped
 |
 v
Manager inspect state
 |
 v
inspect working tree
 |
 v
inspect checkpoint
 |
 v
resume
```

## Test Failure

```text
Review/Test
 |
 v
FAIL
 |
 v
Building
 |
 v
test
 |
 v
Review
```

## Repeated Failure

If retries exceed 3 (default):

```text
FAILED/BLOCKED
  |
  v
Manager notifies the user
```

Never retry forever.

---

# 19. Completion Criteria

A task counts as `DONE` when (evidence-based completion gate — see §26.3, severity §26.20, VERIFYING §26.10):

- implementation complete per requirements
- relevant tests pass (test result as evidence)
- no unresolved blockers
- Review passes with standardized severity — no outstanding `CRITICAL`/`HIGH` (see §26.20); rule: `CRITICAL/HIGH outstanding → NOT DONE`
- checkpoint updated (or reconstructed with evidence source recorded if missing)
- manager state updated (state_version/event_sequence bumped via §26.5 protocol)
- queue marked completed
- git diff inspected with baseline + checkpoint.files_changed separation (see §26.19 — never assume `all diff = worker changes`)
- recovery evidence recorded if recovery occurred (see §26.23)
- work touching indexed projects: equivalence proof passes + graph numbers within tolerance (see §25); OpenVisio is optional provider (see §26.25) — Manager core works without it

Prohibitions:

- never mark DONE from a worker's "Done" message alone — every item needs supporting evidence
- never count worker claims as verification

Not sufficient as the sole completion signal:

```text
"Done"
```

from Building.

---

# 20. Git Safety

The Manager must:

- inspect the working tree before starting work
- never reset unnecessarily
- never overwrite unrelated user changes
- inspect the diff before completion
- never auto-commit in the MVP unless the user allows it

Git commit/PR automation can come in a later phase.

---

# 21. MVP Scope

V1 should focus only on:

```text
1. Manager receives task
2. Manager creates Building session
3. Building works
4. Manager detects session end/tool limit
5. Manager reads checkpoint
6. Manager opens new Building session
7. Building resumes
8. Repeat until done
9. Send to Review
10. Finish
```

Do not start with parallel agents, distributed execution, or multiple autonomous coders at once.

---

# 22. Implementation Phases

## Phase 1 — Proof of Concept

Goal:

Fix the Building tool limit problem first.

Tasks:

1. Survey the OpenCode CLI/API for session control
2. Build a small Manager controller
3. Create a Building session
4. Send the task
5. Monitor the session
6. Detect termination/tool limit
7. Read the checkpoint
8. Create a new Building session
9. Resume
10. Log transitions

## Phase 2 — State Machine

Add states:

```text
IDLE
PLANNING
ERROR_DEBUG
BUILDING
BUILDING_RESUME
REVIEWING
BLOCKED
FAILED
DONE
```

## Phase 3 — Task Queue

Add:

- priority
- dependency
- status
- retry count
- change request

## Phase 4 — Change Requests

Add automatic routing:

```text
minor
  -> Building

design change
  -> Planning -> Building

architecture change
  -> Planning -> Design Review -> Building -> Review
```

## Phase 5 — Reliability

Add:

- crash recovery
- stale session detection
- retry limits
- structured logs
- cancellation
- safe recovery
- manual approval gate

## Phase 6 — Advanced

Consider later:

- parallel workers
- automatic git checkpoints
- automatic rollback
- token/tool budget tracking
- benchmark automation
- automatic commit
- PR creation
- Discord remote control
- scheduled/background execution

## Phase U1-U7 — Manager Upgrade (see §26)

Implementation order for the reliable orchestration layer:

```text
U1 Watchdog          — heartbeat, progress timestamp, stale detection, recovery trigger
U2 Recovery          — idempotency, active-session discovery, duplicate prevention, restart recovery
U3 Event System      — events.jsonl, lifecycle events, structured logging, replay
U4 Context Manager   — resume context generator, compaction, validation, resume prompt
U5 Policy            — configurable limits, risk classification, approval gate, destructive protection
U6 Queue Intelligence— dependency resolver, ready-state, blocked dependency, scheduling
U7 Decision Trace    — decision logs, routing/recovery explanation, final summary
```

Priority: P0 (U1-U3) must be stable first → P1 (U4-U7) → P2 (parallel/automation) must not start before P0/P1 are stable.

---

# 23. Example End-to-End

User:

```text
Add a Graph Export API
```

Manager:

```text
1. Analyze requirements
2. Check design.md
3. Check impact
4. Found a medium change
5. Call Planning
6. Planning updates design.md
7. Create TASK-001
8. Send to Building
```

Building Session #1:

```text
Implement database changes
Implement repository changes
Implement API partially
tool limit reached
```

Manager:

```text
1. Read checkpoint
2. Check git diff
3. Create Building Session #2
4. Send checkpoint
```

Building Session #2:

```text
Continue API implementation
Add tests
Run tests
```

Manager:

```text
Implementation complete
-> Review
```

Review:

```text
FAIL:
missing validation
```

Manager:

```text
-> Building Session #3
```

Building:

```text
fix validation
run tests
```

Review:

```text
PASS
```

Manager:

```text
TASK-001 = DONE
```

The user never has to open a new session manually.

---

# 24. System Philosophy

This system should hold that:

```text
Conversation = temporary
State = persistent
Agent session = disposable
Task = persistent
Checkpoint = recovery mechanism
Manager = orchestrator
```

Therefore a Building context/tool limit exhaustion must not mean the work stops.

It must mean:

```text
worker session ends
        |
        v
manager recovers
        |
        v
new worker session
        |
        v
same work continues
```

This is the single most important principle of this Manager architecture.

---

# 25. OpenVisio Skill + E2E (TASK-OV-A/B/C/D/E/F/G/H)

## Shared skill

- Name: `openvisio-graph` (openvisio 0.3.1, docs match the real CLI since TASK-OV-C, project-neutral since TASK-OV-G)
- Single home (global, no mirrors since TASK-OV-H): `~/.config/opencode/skills/openvisio-graph/SKILL.md` — every project calls it by skill name
- Usage: `skeleton [path]` before reading code → `find_symbol` via MCP → implement → equivalence proof (re-export and compare numbers) → `export-verify`
- Things that don't exist (never use): `lookup / prove / watch-check`, `--json`, `--project`, `-o`, `--version` (read the version from package.json)
- Iron rule: every command takes an explicit path, never run bare inside a work folder (bare triggers init+index)

### Cloudflare global skill (TASK-CF-005)
- Single home (global, no mirrors): `~/.config/opencode/skills/cloudflare-security-audit/SKILL.md`, validator รายรีโป `scripts/cloudflare-audit-validate.cjs`.
- Trigger แยกขาด: `cloudflare audit` (full 6-phase audit) vs `security review` (default fast SAST, never call cloudflare).
- Guidance default: ถ้ากำกวมถาม 1 คำถามก่อนรัน full audit.

## 6-file agent matrix

| project | planner | builder | reviewer |
|---|---|---|---|
| Dashboard (Python/FastAPI) | `.opencode/agent/openvisio-planner.md` | `.opencode/agent/openvisio-builder.md` | `.opencode/agent/openvisio-reviewer.md` (+pytest) |
| mcp (TS/Node) | `.opencode/agent/openvisio-planner.md` | `.opencode/agent/openvisio-builder.md` | `.opencode/agent/openvisio-reviewer.md` (+npm test) |

Every file declares `skills: [openvisio-graph]` and references its own project's baseline.

## Graph baselines (E2E tolerance: files ±3, symbols/edges ±5)

| project | files | symbols | edges |
|---|---|---|---|
| Dashboard | 69 | 227 | 169 |
| mcp | 140 | 431 | 433 |

## E2E commands

```text
powershell -ExecutionPolicy Bypass -File "D:\Coding_Project\scopeboard\scripts\e2e-openvisio.ps1"
```

Results: `D:\Coding_Project\scopeboard\.agent\building\e2e-ov-b.json` (must pass 5/5: 2-project skeleton + export/prove equivalence + watch freshness + token-proxy)

---

# 26. Manager Upgrade Specification (Reliable Orchestration Layer)

> Origin: the upgrade spec was fully merged into design.md (TASK-UP-0); the original
> `upgrade.md` file was deleted on 2026-09-14 — this document is the single source of truth
> (the original spec is still kept at `.agent/manager/upgrade-spec.md` under runtime, out of git)

As a reliable orchestration layer, the Manager supports 8 core capabilities plus supporting structures:

## Target Architecture
```text
                          USER
                            |
                            v
                     +-------------+
                     |   MANAGER   |
                     +------+------+
                            |
           +----------------+----------------+
           |                |                |
           v                v                v
        ROUTER          SCHEDULER          POLICY
           |                |                |
           +----------------+----------------+
                           |
                           v
                    +-------------+
                    | TASK QUEUE  |
                    +------+------+
                           |
                           v
                    +-------------+
                    |   WORKER    |
                    | CONTROLLER  |
                    +------+------+
                           |
           +----------------+----------------+
           |                |                |
           v                v                v
      PLANNING          BUILDING        ERROR_DEBUG
                           |
                           v
                      CHECKPOINT
                           |
                           v
                       WATCHDOG
                           |
                 +---------+---------+
                 |                   |
                 v                   v
             HEALTHY             PROBLEM
                 |                   |
                 v                   v
             CONTINUE             RECOVERY
                                     |
                                     v
                               NEW SESSION
                                     |
                                     v
                                  RESUME
                                     |
                                     v
                                  REVIEW
                                     |
                                     v
                               VERIFICATION
                                     |
                              +------+------+
                              |             |
                              v             v
                            DONE         BLOCKED
```

## Persistent Runtime State
```text
.agent/
├── manager/
│   ├── state.json
│   ├── queue.json
│   ├── events.jsonl
│   ├── decisions/
│   ├── context/
│   └── changes/
│
└── building/
    └── checkpoint.json
```

## 8 Core Capabilities

1. **Worker Watchdog**: monitors worker health, heartbeat, progress timestamps, stale detection (heartbeat / progress / checkpoint timeouts) and triggers recovery when a worker hangs or dies
2. **Progress / Heartbeat Protocol**: records milestones, timestamps, evidence and status lifecycle (`running`, `stopped_limit`, `done`, `failed`, `blocked`)
3. **Evidence-Based Completion**: never trust a bare worker "Done" message — must pass an Evidence Check (changed files, git diff, relevant tests, test results, checkpoint, verification)
4. **Idempotent Recovery**: supports retry and restart while preventing duplicate workers (operation ID / task ID + attempt) and recovers from checkpoint / working tree / active session
5. **Event Log**: append-only history at `.agent/manager/events.jsonl`, separate from state.json
6. **Context Manager**: builds a just-in-time resume context file (`.agent/manager/context/<task_id>.resume.md`) sending only necessary data to a new Building session, cutting token noise
7. **Lease / Lock**: prevents concurrent controllers with a lease on state.json (`owner`, `acquired_at`, `expires_at`)
8. **Policy / Budget / Human Approval**: enforces limits (max sessions, review cycles, runtime), classifies risk per-case via `assess_risk` (LOW, MEDIUM, HIGH) and mandates a Human Approval Gate only for HIGH-risk operations (destructive + irreversible / security-sensitive / data-loss combinations, not every database/architecture touch)

## Additional supporting structures
- Truly dependency-aware Task Queue (`queue.json` schema v2)
- Manager Decision Log recording Routing / Recovery / Review-failure rationale (`.agent/manager/decisions/`)
- Separate configuration (`.agent/manager/config.json`)

## Key design principles
- **Principle 1 — Worker is disposable**: a worker session can be dropped and recreated
- **Principle 2 — State is the source of truth**: never rely on conversation
- **Principle 3 — Evidence over claims**: agent says done ≠ done
- **Principle 4 — Recovery over restart**: a restart must be a recovery with context, not just a fresh session
- **Principle 5 — Safe failure**: when unsure, BLOCKED beats guessing ahead
- **Principle 6 — No infinite automation**: every retry and loop needs a limit
- **Principle 7 — Preserve user work**: the Manager must not destroy user work to fix its own problems
- **Principle 8 — Reliability before intelligence**: a plain-deciding Manager that recovers well beats a smart one with broken state

## Success Definition
The Manager Upgrade counts as successful when:
> **A user can hand a long-running task to the Manager while worker sessions die, hit tool-call limits, crash or get replaced, yet the work safely continues from persistent state — and the Manager never declares success without supporting evidence and verification.**

## 26.1 Watchdog Data + Stale Detection (Hardened — fix.md §3 + §14)

The Manager tracks per task:

```json
{
  "last_output_at": "ISO-8601",
  "last_progress_at": "ISO-8601",
  "last_checkpoint_at": "ISO-8601",
  "heartbeat_at": "ISO-8601",
  "stuck_count": 0
}
```

All timeouts must come from `config.json`, never hard-coded: `heartbeat_timeout_seconds`, `progress_timeout_seconds`, `checkpoint_timeout_seconds`, `worker_start_timeout_seconds`

> An open session with no progress may count as a failure condition.

**Hardened (fix.md §3):** heartbeat is only one signal — `heartbeat ≠ alive`. Manager must use external observation hierarchy:

```text
OpenCode session/process status → session activity → event activity → checkpoint freshness → heartbeat
```

Health classification (see §26.16), operation-aware (fix-v2 §8):

```text
HEALTHY   — heartbeat fresh + progress fresh
SLOW      — heartbeat fresh + progress delayed
IN_FLIGHT — process alive + active operation + operation deadline unexceeded
            → NO auto-recovery even if progress/checkpoint timestamps are old
STUCK     — session alive + no progress beyond threshold, OR active operation
            deadline exceeded
DEAD      — session/process unavailable
UNKNOWN   — evidence contradictory or insufficient → must go to error_debug classify, no blind resume
```

Worker state exposes the in-flight operation (checkpoint carries worker state):

```json
{
  "active_operation": "RUN_TESTS",
  "operation_started_at": "ISO-8601",
  "operation_deadline_at": "ISO-8601"
}
```

Rule: alive + active op + deadline unexceeded → NO auto-recovery. Stale
progress/checkpoint alone cannot produce STUCK/DEAD while the operation is
alive. Four timeouts are distinct:

```text
heartbeat timeout ≠ progress timeout ≠ operation timeout ≠ process death
```

Manager must detect: worker not sending heartbeat, session disappeared, process hung, session alive but no progress, state not changing within threshold.

## 26.2 Progress / Heartbeat Protocol

Workers report lifecycle: `STARTED | PROGRESS | CHECKPOINT | BLOCKED | DONE | FAILED`

- `status` = lifecycle (`running | stopped_limit | done | failed | blocked`)
- `progress` = health (milestone + timestamp + evidence — never use percentage as the source of truth)

```json
{
  "status": "running",
  "progress": {
    "milestone": "repository-layer",
    "last_progress_at": "ISO-8601",
    "files_changed": 5
  }
}
```

## 26.3 Evidence-Based Completion Gate (Hardened — fix.md §9 + §19)

The Manager checks before marking DONE:

```text
implementation complete? tests pass? blockers empty? review pass (no CRITICAL/HIGH)?
checkpoint updated (or reconstructed with source recorded)? manager state updated? queue state updated?
git diff inspected with baseline separation? recovery evidence recorded?
```

Severity vocabulary is unified `CRITICAL/HIGH/MEDIUM/LOW` across Review/Policy/Dashboard/Evidence/Alerts (see §26.20). Rule: `CRITICAL/HIGH outstanding → NOT DONE`

Minimum evidence: changed files, git diff (with baseline separation, see §26.19), relevant tests + test results, checkpoint (or reconstructed evidence source), requirement coverage, review results, recovery evidence if any. For indexed projects, verification via optional provider (see §26.25) must pass when enabled.

## 26.4 Idempotent Recovery (Hardened — fix.md §6 + §26.17)

- Idempotency key: `task_id:operation:attempt` (e.g. `TASK-001:BUILD:003`)
- **Hardened:** every side-effecting operation must have a persistent operation record:

```json
{
  "operation_id": "op-001",
  "task_id": "TASK-001",
  "operation": "CREATE_WORKER",
  "attempt": 3,
  "status": "committed",
  "session_id": "session-123",
  "created_at": "ISO-8601",
  "completed_at": "ISO-8601",
  "result": {}
}
```

Flow before side effect:

```text
check operation → already committed? → YES: reuse result | NO: execute → persist committed
```

Coverage: `CREATE_SESSION`, `RESUME`, `RECOVER`, `REVIEW`, `DISPATCH` must all be idempotent. Handles `Manager crash after session creation but before persisting` — on restart discover existing session instead of duplicating.

- Before creating a new session, discover active sessions first: found → recover existing, not found → create new
- Never blindly create duplicate sessions after a Manager restart
- Every lifecycle operation must be retryable without duplicate side effects

## 26.5 Event Log (Hardened — fix.md §4)

The `.agent/manager/events.jsonl` file is append-only:

```json
{"event":"TASK_CREATED","task":"TASK-001","time":"...","state_version":12,"event_sequence":45}
{"event":"WORKER_STARTED","task":"TASK-001","session":"..."}
{"event":"PROGRESS","task":"TASK-001","milestone":"repository"}
{"event":"WORKER_LIMIT","task":"TASK-001","session":"..."}
{"event":"WORKER_RESUMED","task":"TASK-001","session":"..."}
{"event":"REVIEW_FAILED","task":"TASK-001"}
{"event":"TASK_DONE","task":"TASK-001"}
```

Required events: task created/queued/started, worker created/stopped/limit, checkpoint, recovery, resume, review started/failed/passed, blocked, failed, cancelled, done — every event carries a timestamp + task ID + `event_sequence`

**Hardened:** state transition protocol:

```text
1. validate transition 2. create event 3. persist event 4. persist state 5. update timestamps (state_version, event_sequence, updated_at)
```

On Manager startup (crash/restart contract, see §26.29):

```text
LOAD STATE → LOAD RECENT EVENTS → LOAD OPERATIONS → VALIDATE CONSISTENCY (state_version/event_sequence/updated_at) → RECONCILE (incl. reconcile_started_operations: STARTED ops must discover external side effects before retry, never blind re-execute) → DISCOVER ACTIVE SESSIONS (before creating any session, I9) → RECONSTRUCT → RESUME/RECOVER
```

Inconsistent state → `RECOVERING` or `BLOCKED`, never duplicate worker. Lease fencing holds across restart: only the current valid lease mutates state (see §26.7).

**FIX-V2-09 reconciliation repair (P0):** not every state/event mismatch is unrecoverable. Reconcile follows `STATE + EVENT LOG → RECONCILIATION → repairable? → YES: REPLAY / NO: BLOCK-RECOVER`. State tracks both `event_sequence` and `last_applied_event_sequence`; events after the last applied sequence are replayed in log order before any block decision. Sequence gap / ordering drift (state behind the log, ordinary events pending) is repairable → `REPLAY`, then continue. Only genuine conflicts — nothing replayable while validation still fails, or a `BLOCK`-family terminal event (`WORKER_BLOCKED`, `TASK_BLOCKED`, `QUEUE_BLOCKED`, `STARTUP_BUDGET_EXCEEDED`) pending in the unapplied window — go to `BLOCK` / `RECOVER`. Rule: never blanket-classify a mismatch as unrecoverable; mixed windows replay the repairable prefix and block only on the true conflict.

## 26.6 Context Manager

Build `.agent/manager/context/TASK-xxx.resume.md` instead of sending the whole conversation:

```markdown
# Resume Context
Task: / Current phase: / Completed: / Current: / Remaining:
Important decisions: / Files changed: / Known issues:
Next action: / Do not redo:
```

Context send priority: Resume Context > Checkpoint > Current State > Relevant design section > Working tree > Relevant files. Never send whole conversations/logs/the entire repo source unless necessary.

## 26.7 Lease / Lock (Hardened — fix.md §5 + §26.18)

```text
.agent/manager/state.json  # application data
.agent/manager/manager.lock # synchronization primitive (atomic, separate)
```

```json
{
  "owner": "manager-01",
  "lease_id": "lease-uuid",
  "fencing_token": 42,
  "acquired_at": "ISO-8601",
  "expires_at": "ISO-8601"
}
```

Use leases instead of permanent locks — an expired lease lets a new Manager recover. Must acquire atomically (OS-level atomic or compare-and-swap). Prevents: a task executed twice, a worker controlled by two Manager instances, duplicate recovery. Manager crash must not leave permanent lock — expired lease can be taken over.

FIX-V2-06 fencing (stale-owner protection): a lease can expire while the old Manager is still executing (`A gets lease → A stalls → lease expires → B gets lease → A resumes`). `lease_id` (uuid4, unique per acquisition) plus monotonic `fencing_token` (prev+1 on every acquisition, including takeover) distinguish the current valid lease from a stale one. Every state mutation must verify current ownership (`owner` + `lease_id` + `fencing_token` match the lock file) before mutating; if the token is stale → `ABORT MUTATION` (log `LEASE_STALE_ABORT`, perform no write). Invariant: only the current valid Manager lease may mutate persistent Manager state.

## 26.8 Policy / Budget / Human Approval

```json
{
  "limits": { "max_worker_sessions": 5, "max_review_cycles": 3, "max_retries": 3, "max_runtime_minutes": 120 },
  "watchdog": { "heartbeat_timeout_seconds": 120, "progress_timeout_seconds": 600, "checkpoint_timeout_seconds": 900, "worker_start_timeout_seconds": 60 },
  "approval": { "require_for_destructive": true, "require_for_architecture_change": true },
  "git": { "auto_commit": false }
}
```

Risk: LOW → automatic, MEDIUM → per policy, HIGH → APPROVAL_REQUIRED. Risk is scored per-case by `assess_risk(ctx)`: +2 destructive, +2 irreversible, +2 security-sensitive/auth, +2 data-loss, +1 broad scope, +1 database scope, +1 architecture/api scope; 0-1 → LOW, 2-3 → MEDIUM, 4+ → HIGH. Database or API changes alone are LOW/MEDIUM and require approval only when combined with destructive, irreversible, security-sensitive or data-loss factors. The Manager must never bypass the approval gate when HIGH is assessed.

Counter semantics are disambiguated (see §26.21): `worker_attempt` / `recovery_attempt` / `task_retry` / `review_cycle`.

Budget hierarchy (FIX-V2-14): `session_count` (session, cap 5) / `recovery_count` (recovery, cap 5) / `task_retries` (retry, cap 3) / `review_count` (review, cap 3) — each scope burns exactly one counter via `check_budget`/`consume_budget`; no-conflation across scopes.

FIX-V2-18 7-stage arch-change pipeline: `Arch change → Impact → Planning → Design Review → Policy → Approval → Building`. Each stage has a gate; no-skip rule — `request_approval` returns `BLOCKED` with no impact record, and `BLOCKED` with impact but no design-review `PASS`. `Design Review` is technical validation, `Human Approval` is authorization; both required before `Building`.

## 26.9 Dependency-Aware Queue

Queue states: `pending | ready | running | blocked | failed | completed | cancelled` — the Manager checks `dependency complete?` before dispatch. Tasks whose dependencies haven't passed must not be dispatched.

## 26.10 Enhanced State Machine (Canonical — fix.md §19 / fix-v2 §1)

```text
IDLE → PLANNING → BUILDING
BUILDING → LIMIT/STUCK → RECOVERING → BUILDING
BUILDING → REVIEWING → FAIL → BUILDING
REVIEWING → PASS → VERIFYING → DONE | BLOCKED
Additional: FAILED, CANCELLED, APPROVAL_REQUIRED, RECOVERING
```

`RECOVERING` separates the recovery process from normal building. `VERIFYING` is mandatory — `REVIEWING → DONE` bypass is forbidden (see §17). `ERROR_DEBUG` is an **agent/task routing target**, not a Manager lifecycle state (see fix-v2 §1). `BUILDING_RESUME` is not a state — resume is `RECOVERING → BUILDING` transition. No other section may define an alternative Manager state machine. Definitions: `REVIEWING` = quality/bugs/regression/security, `VERIFYING` = requirement + evidence + state + tests + diff + final acceptance.

## 26.11 Recovery Protocol (Canonical — fix.md §20 + §26.11)

```text
1. STOP 2. DETECT 3. ACQUIRE LEASE 4. READ STATE 5. READ CHECKPOINT 6. READ EVENTS
7. DISCOVER ACTIVE SESSION 8. INSPECT WORKING TREE 9. CHECK OPERATION RECORD 10. CLASSIFY
11. RECONSTRUCT CONTEXT 12. APPLY POLICY 13. RECOVER/RESUME 14. VERIFY 15. WRITE EVENT 16. UPDATE STATE
```

Classification: `LIMIT | CRASH | STUCK | FAILED | DONE | BLOCKED | UNKNOWN`
- LIMIT/CRASH/STUCK → RECOVER → NEW SESSION → RESUME
- UNKNOWN → ERROR_DEBUG → CLASSIFY (no blind resume)

The Manager must never: blind git reset/checkout/delete/overwrite, blind retry forever, blind create duplicate sessions, blind mark DONE. Destructive operations must pass policy first. Recovery must run `git status` + `git diff` + baseline comparison to separate worker changes from user changes — never assume every diff belongs to the worker (see §26.19).

Never:

```text
blind resume, blind retry, blind new session, blind reset, blind checkout, blind delete, blind DONE
```

## 26.12 Review Loop + Observability (Hardened — fix.md §9)

Reviews must produce structured findings with unified severity:

```json
{ "status": "failed", "findings": [ { "severity": "CRITICAL", "file": "src/example.ts", "issue": "missing validation", "required_action": "add input validation" } ] }
```

Severity vocabulary everywhere: `CRITICAL/HIGH/MEDIUM/LOW` (never `critical/major/minor` vs `CRITICAL/HIGH/...` mixed). The Manager feeds findings as input to the next Building round.

Log levels: `INFO | WARN | ERROR | RECOVERY | SECURITY` — every log carries timestamp, task_id, agent, session_id, event, message. Goal: reconstruct past events.

## 26.13 Decision Log

`.agent/manager/decisions/TASK-xxx.md` records: Routing (selected + reason), Recovery (decision + source + evidence), Review Failure (finding + decision), Final — goal: retrace what the Manager decided and why. Every recovery must record evidence-based decision (see §26.23).

## 26.14 Acceptance Criteria (summary)

| Area | Criteria |
|---|---|
| Recovery | detect limit/stop/stuck, recover from checkpoint/hierarchy, no duplicates, resumable, recover after restart, re-entrant |
| State | state/checkpoint/event log persistent, lease atomic, transitions correct, state_version/event_sequence validated, inconsistent → RECOVERING/BLOCKED |
| Verification | never mark DONE from a single claim, check tests/diff/review/blockers with CRITICAL/HIGH gate, indexed projects via optional provider, VERIFYING mandatory |
| Reliability | bounded retries (worker_attempt/recovery_attempt/task_retry/review_cycle disambiguated), no infinite loops, preserve user changes via baseline, idempotent recovery with operation records |
| Context | build + use resume context, never rely on old conversations |
| Safety | destructive ops pass policy, architecture passes approval, no auto git reset/commit, user changes never overwritten |
| Traceability | every recovery decision recorded with evidence, operation records, event/state consistency |

Required test scenarios: T01 normal, T02 tool limit, T03 multiple limits, T04 crash, T05 stuck, T06 review fail, T07 repeated failure, T08 duplicate protection, T09 user changes, T10 approval, **plus T11-T22 (see §27)** — P0 `T02 T03 T04 T05 T08 T11 T12 T13 T14 T15 T16 T17` must pass before aggressive autonomous recovery.

## 26.15 Cloudflare Security Audit — Light Adapt (6-Phase) (fix.md §15 + skill `cloudflare-security-audit` v1.0.0-light)

*ทริกเกอร์แยกเด็ดขาด:* `cloudflare audit` → สกิลนี้เท่านั้น (ใช้ `scripts/cloudflare-audit-validate.cjs` + checklist `.agent/manager/context/cloudflare-adapt.checklist.md`) — `security review` → `security-reviewer` เท่านั้น ห้ามเรียกสลับกัน (ดู `decisions/TASK-CF-002.md`)

**โหมดค่าเริ่มต้น (Guidance):** โหลดสกิล ≠ สั่งออดิตเต็มรูป — ตอบเป็นไกด์ไลน์ ไม่สร้าง output dir ไม่รัน 6 phases — ถ้ากำกวมให้ถาม 1 คำถามก่อน — สั่งออดิตเต็มรูปต้องมีคำชัด `audit` / `pen-test` / `full` / `comprehensive` / `end-to-end` หรือขอ report artifact

**กฎความปลอดภัย (Safety):** อ่านซอร์สอย่างเดียว ไม่แก้ไฟล์ — รันโค้ด target ได้เฉพาะใน OS sandbox (no external network + empty env allowlist + target/toolchain read-only + เขียนได้แค่ `scratch/` ของตัวเอง) — ต้องมี CPU/mem/time limits ถ้าไม่มีให้ตอบเป็น `needs_validation` blocker แทน — output dir อยู่นอก target (`~/security-audit-skill/<repo>/run-<N>`) ยกเว้นผู้ใช้ยืนยันพาธ git-ignored ใน target — `agent-id` ต้อง lowercase (เลี่ยง `con`/`prn`/`aux` บน Windows)

**Severity mapping เดียวกับ Manager (FIX-11):** `critical` → `CRITICAL`, `high` → `HIGH`, `medium` → `MEDIUM`, `low`/`informational`/`info` → `LOW` (ห้ามตัด `informational` ทิ้ง) — `needs_validation` ห้ามมี severity ส่งเป็น blocker (`blockers` + `validation_plan` อย่างน้อย 1 entry) — `confirmed` ต้องมี `likelihood`/`impact`/`overall_severity` ครบ และ `overall_severity <= impact` — ทุก record ต้องมี fingerprint + trace ตั้งแต่ entrypoint → sink

**Validator ก่อน `save_review`:** ถ้ามี `scripts/cloudflare-audit-validate.cjs` ในโปรเจกต์ ให้รันก่อนเสมอ:
```bash
node scripts/cloudflare-audit-validate.cjs findings.json
```
ถ้าไม่มีไฟล์ (นอก repo Agent) ให้ตรวจมือตามกฎเดียวกัน หรือตอบเป็น `needs_validation` blocker แล้วบอกให้ copy validator จาก repo Agent

**6-Phase ที่ Manager ต้องออร์เคสตรา (เมื่อผู้ใช้พิมพ์ `cloudflare audit` ชัด):**
```text
1. เตรียม sandbox — สร้าง scratch/ นอก target, ตั้ง limits, ตรวจ agent-id lowercase
2. เก็บหลักฐาน — อ่านซอร์ส, สร้าง checklist, รันสแกน read-only ใน sandbox เท่านั้น
3. วิเคราะห์ช่องโหว่ — classify ตาม OWASP Top 10 + trace entry→sink + ให้ severity ตาม mapping ข้างบน
4. ตรวจ severity/validator — รัน validator .cjs, แยก confirmed vs needs_validation, ตรวจ needs_validation มี blockers+validation_plan
5. สร้างรายงาน — เขียน findings.json + รายงานภาษาไทย (สรุป, รายการปัญหา, วิธีแก้, ลำดับความสำคัญ) ลง output dir นอก target
6. ส่ง Review → Manager — save_review ด้วย severity CRITICAL/HIGH/MEDIUM/LOW, กฎ CRITICAL/HIGH ค้าง = NOT DONE ต้องส่งกลับ BUILDING/VERIFYING จนกว่าจะเคลียร์หรือมี approval
```
*Manager ไม่รัน audit เอง — ส่งให้ Review Agent ที่โหลดสกิล `cloudflare-security-audit` เท่านั้น*

## 26.16 Checkpoint Hierarchy (fix.md §2 — P0)

Checkpoint is **preferred**, not absolute. Recovery hierarchy:

```text
1. Checkpoint 2. Event Log 3. Manager State 4. Active Session 5. Working Tree/Git 6. Timestamps
```

When checkpointหาย/stale: reconstruct via hierarchy and record `reconstructed_from` (which evidence). `missing checkpoint ≠ no progress`. Every recovery logs its evidence source.

## 26.17 State/Event Consistency (fix.md §4 — P0)

Transition protocol: `validate → create event → persist event → persist state → update timestamps (state_version, event_sequence, updated_at)`. On startup: `LOAD STATE → LOAD EVENTS → VALIDATE → RECONCILE → RESUME`. `state_version` + `event_sequence` detect stale writes/ordering. Mismatch → `RECOVERING`/`BLOCKED`, never duplicate worker.

## 26.18 Lease Separation (fix.md §5 — P0)

`state.json` = application data, `manager.lock` = synchronization primitive (atomic). Lease `{owner, lease_id, fencing_token, acquired_at, expires_at}`; expired → takeover allowed with `fencing_token = prev + 1` and a fresh `lease_id`; crash must not leave permanent lock. Guard: `ManagerOrchestrator._verify_lease()` checks `owner` + `lease_id` + `fencing_token` against the lock file; every mutation path (`transition_state`, `execute_recovery`, `startup_recovery`, `evaluate_dependencies`, `check_approval_required`, `verify_completion`, `complete_task`, dispatch) calls `_abort_if_stale(op)` first — stale token → `ABORT MUTATION`, no write. Invariant: only the current valid lease mutates state.

## 26.19 Baseline & User-Change Protection (fix.md §8 — P0)

At `TASK START`: capture `git status` + file hashes baseline. Decide with `baseline + checkpoint.files_changed + current tree + git diff`. Never assume `all diff = worker changes`. Tests: `T09` user changes, `T18` same file as worker — must not reset/overwrite/clean user changes and must notify when ownership unclear.

Baseline is captured at TASK START (git status + file hashes). checkpoint.files_changed is a hint, never authority. Ground truth order: baseline + current git diff > checkpoint list. Missing baseline → conservative BLOCKED.

FIX-V2-13 Same-File Policy table (code: `resolve_file_policy`, hooks: `log_dispatch`, `execute_recovery` log `FILE_POLICY`, never silent overwrite):

| Case | Condition | Action | Note |
|---|---|---|---|
| A | Different file (no user/unknown files) | CONTINUE | CONTINUE iff user file list empty |
| B | User files present, no overlap proven | WARNING | continue with warning, non-overlap only if reliably identified |
| C | Same file overlapping region | BLOCKED | human review, must not overwrite user change |
| D | Ownership undetermined (no baseline / unknown files) | BLOCKED | do not guess |

Intent priority: conflicting user intent decided by priority — user HIGH beats worker LOW → BLOCKED / worker yields; equal or higher user priority blocks.

## 26.20 Severity Standardization (fix.md §9 — P1)

Single vocabulary `CRITICAL/HIGH/MEDIUM/LOW` for Review, Policy, Dashboard, Evidence gate, Alerts, Decision log. Completion: `CRITICAL/HIGH outstanding → NOT DONE`.

## 26.21 Attempt Semantics (fix.md §10 — P1)

Disambiguate:

```text
worker_attempt   = number of worker sessions
recovery_attempt = number of recovery operations
task_retry       = number of task retries due to failure
review_cycle     = number of Review→Building→Review loops
```

Example `Worker #1 LIMIT, #2 LIMIT, #3 LIMIT` → `worker_attempt=3, recovery_attempt=2` ≠ `task_retry`. Every counter has one definition used in both code and dashboard.

## 26.22 Task Manifest (fix.md §13 — P1)

Conceptual `Task Manifest` — task identity without reading conversation:

```text
.agent/manager/tasks/TASK-001/
  manifest.json  # id, requirement, type, owner, phase, attempts, evidence, recovery context, completion status
  resume.md
  decisions.md
```

Global `state.json` remains the global manager state; manifest is per-task.

## 26.23 Recovery Evidence Record (fix.md §15 — P1)

Every recovery records:

```json
{
  "task_id": "TASK-001",
  "classification": "STUCK",
  "evidence": ["session_active","heartbeat_stale","no_progress_720s","checkpoint_stale"],
  "decision": "RECOVER",
  "source": "watchdog"
}
```

Makes Decision Log + Event Log auditable.

## 26.24 Re-entrant Recovery (fix.md §16 — P1)

Recovery must be resumable after Manager crash mid-RECOVERING:

```text
READ recovery operation → already started? → already committed? → resume recovery
```

Recovery itself is stateful and idempotent via operation records.

## 26.29 Manager Crash / Restart Contract (fix-v2 §9)

Guarantee stated explicitly — automatic process resurrection is provided
only by an external supervisor/launcher, never by the Manager itself.

With supervisor (automatic restart allowed):

```text
Supervisor
    ↓
Manager
    ↓
OpenCode workers
```

Manager crash recovery can be automatic: the supervisor resurrects the
Manager process, then the Manager reconciles state on startup.

Without supervisor (manual restart required):

```text
Manager crashes
    ↓
user / launcher restarts Manager
    ↓
Manager reconciles
```

No supervisor means no automatic resurrection. The user or launcher
restarts the Manager process; the Manager only reconciles after restart.
This spec never claims Manager self-resurrection.

Required 8-step startup protocol (fix-v2 §9):

```text
LOAD STATE
 ↓
LOAD RECENT EVENTS
 ↓
LOAD OPERATIONS
 ↓
VALIDATE
 ↓
RECONCILE
 ↓
DISCOVER ACTIVE SESSIONS
 ↓
RECONSTRUCT
 ↓
RESUME / RECOVER
```

`startup_recovery()` implements this order: fencing lease is acquired
before any mutation, STARTED operations are reconciled via
`reconcile_started_operations(task_id)` after DISCOVER/LOAD OPERATIONS,
active sessions are discovered before RECONSTRUCT, and recovery resumes
re-entrantly without duplicating sessions.

## 26.30 UNKNOWN Hard Boundary (fix-v2 §10)

UNKNOWN/CLASSIFY/ERROR_DEBUG never transitions to DONE, destructive recovery, or blind building. The only allowed transition is route to classify with `CLASSIFY_ONLY` and forbid `["destructive", "building", "done"]`.

ERROR_DEBUG-first: any UNKNOWN health, watchdog UNKNOWN, or evidence UNKNOWN must route to `ERROR_DEBUG` classify before any other action. No recovery and no done is permitted on this path.

## 26.25 OpenVisio Optional Provider (fix.md §12 — P1)

```text
Manager Core → Generic Verification → Verification Provider → OpenVisio / pytest / npm test / custom
```

Project config:

```json
{ "verification": { "enabled": true, "provider": "openvisio" } }
```

Manager core never hard-codes Dashboard/mcp/OpenVisio assumptions; works with projects without OpenVisio.

### Verification Provider Interface (FIX-V2-21)

Core talks only to this contract. Provider specifics live in provider
implementations behind a registry — never in core call-sites.

```text
can_verify(task) -> bool
verify(task, evidence) -> { status: pass|fail|unavailable|skipped, provider, detail }
```

Rules:

- Core-agnostic: `get_verification_provider` / `verify_with_provider` contain
  no provider-specific command or config literals. All such knowledge lives
  in `VerificationProvider` subclasses registered by name via
  `register_verification_provider` (openvisio / pytest / npm / custom).
- Graceful degradation: provider unknown, `can_verify` false, `verify`
  returns non-pass status, or provider raises — all collapse to an
  `unavailable` verdict. Core proceeds on the evidence gate and never crashes.
- Swap-ability: tests and custom setups register fake providers by name
  without touching core code.

## 26.26 Core vs Observability Separation (fix.md §17 — P2)

```text
MANAGER CORE: orchestration, state, recovery, policy, verification
OBSERVABILITY: tool calls, metrics, dashboard, history, inventory, trends
```

Manager emits events; Observability reads events — no dashboard logic inside orchestration.
Boundary is events-only: core writes events.jsonl/tool-calls.jsonl; observability reads them; core never imports observability modules nor reads/writes metrics.json.
Failure-isolation: observability failure never blocks core orchestration; core log_dispatch/verify_completion/build_recovery_plan succeed with observability modules unimportable.

## 26.27 Test Matrix T11-T22 (fix.md §21) — plus Implementation Order

### Test Matrix

```text
T01 normal, T02 limit, T03 multiple limits, T04 crash, T05 stuck, T06 review fail, T07 repeated fail,
T08 duplicate protection, T09 user changes, T10 approval (baseline)
T11 Manager crash during Building
T12 Manager crash during session creation
T13 Manager crash during recovery
T14 checkpoint missing after tool limit
T15 state/event inconsistency
T16 duplicate operation after restart
T17 concurrent Manager startup
T18 user edits same file as worker
T19 verification provider unavailable
T20 stale heartbeat but active progress
T21 active heartbeat but no actual progress
T22 recovery reaches budget limit
```

Priority:

```text
P0: T02 T03 T04 T05 T08 T11 T12 T13 T14 T15 T16 T17
P1: T09 T10 T18 T19 T20 T21 T22
Baseline: T01 T06 T07
```

All P0 must pass before aggressive autonomous recovery.

### Implementation Order (fix.md §22)

```text
FIX-1 Recovery Foundation: 1.checkpoint hierarchy 2.state/event consistency 3.self-recovery 4.external watchdog 5.lock separation 6.idempotent ops
FIX-2 Safety: 7.baseline snapshot 8.user-change protection 9.approval integration 10.recovery evidence
FIX-3 Cleanup: 11.severity 12.attempt semantics 13.VERIFYING canonical 14.re-entrant 15.manifest
FIX-4 Architecture: 16.OpenVisio provider 17.core vs observability
FIX-5 Validation: run T01-T22, require P0 pass
```

Do not implement all fixes simultaneously — sequence as above.

## 26.28 Tool-call Logging + Metrics + Dashboard (TASK-LOG-001 → 002 → 003) — legacy preserved, now after hardening sections

Evidence-based observability over worker tool calls / dispatches / results
without touching existing `manager.py` logic (new functions only + mirrored events).

### Schema `tool-calls.jsonl` (append-only, 1 line = 1 record, UTF-8)

File: `.agent/manager/tool-calls.jsonl` (runtime — gitignored, never enters git)

| Field | Type | Notes |
|---|---|---|
| `time` | string ISO-8601 UTC | event time |
| `task` | string | task id (e.g. TASK-LOG-001) |
| `attempt` | integer | session attempt (matches `state.json` attempt) |
| `session_id` | string | worker session id |
| `agent` | enum | `planning` \| `building` \| `review` \| `error_debug` \| `manager` |
| `operation` | enum | `DISPATCH` \| `CALL` \| `RESULT` (`ATTEMPT` for denied attempts) |
| `tool` | string | tool name (e.g. read/edit/test) — may be empty for DISPATCH |
| `duration_ms` | integer | elapsed time (required for RESULT, 0 for CALL/DISPATCH) |
| `status` | enum | `ok` \| `error` \| `blocked` \| `limit` \| `denied` |
| `error` | string/`null` | error text (`null` when none) |
| `prompt_hash` | string | first 12 hex chars of prompt sha256 — never store secrets/full prompts |
| `tokens_in` / `tokens_out` | integer/`null` | filled by the caller when known (e.g. from an API response), no default |

### Tool-call lifecycle (FIX-V2-23)

States (4): `STARTED`, `COMPLETED`, `FAILED`, `UNKNOWN`.
- Transitions: `STARTED→COMPLETED` on normal finish via `record_lifecycle`; `STARTED→FAILED` only on explicit error evidence; `STARTED→UNKNOWN` only via `reconcile_tool_calls` (restart-reconcile) when no matching completion/result found.
- Every tool-call row carries `tool_call_id` (uuid4 hex) + `lifecycle` (one of the 4 states); `status` retained for compat.
- Restart-reconcile: on restart, scan `tool-calls.jsonl` for rows with `lifecycle=STARTED`; with matching completion event → close as `COMPLETED` (or `FAILED` if error evidence), without match → close as `UNKNOWN`.
- Missing result → `UNKNOWN`, never `FAILED` (FAILED requires explicit error).

### 6 Metrics + formulas (`scripts/metrics.py --rebuild`)

Reads `events.jsonl` + `tool-calls.jsonl` and computes per task (re-runnable with identical results = idempotent):

| Metric | Formula |
|---|---|
| `sessions_per_task` | count `WORKER_STARTED` per task (legacy alias of `worker_sessions_created`) |
| `tool_calls_per_task` | count non-denied `tool-calls.jsonl` rows per task |
| `recovery_count` | count `RECOVERY_STARTED` per task |
| `review_loop` | count `REVIEW_FAILED` per task — over `max_review_cycles=3` in `config.json` → `BLOCKED` |
| `time_to_DONE` | `TASK_DONE.time − TASK_CREATED.time` (seconds, `null` when absent) |
| `success_rate` | `DONE / (DONE + failed + cancelled)` overall (0–1) |

Session/recovery/review split semantics (FIX-V2-24): `worker_sessions_created` counts
`WORKER_STARTED` only; `worker_session_resumes` counts `WORKER_RESUMED` only;
`worker_recoveries` counts `RECOVERY_STARTED` only; `review_cycles` counts
`REVIEW_FAILED` only. Counting rules: one event row = one count, per task, `TEST-*`/`DEP-*`
excluded; a resume never creates a session (`WORKER_RESUMED` never increments
`worker_sessions_created`/`sessions_per_task`) — a resume is NEVER a new session.

Plus (added later, same rebuild): `denied_attempts`, `recovery_tokens`, `review_passed`,
efficiency block (token/tools per success, recovery cost, session efficiency, review
rework + per-agent comparison with low-sample flag), daily `history/` snapshots.

### Interception: 3+ functions (`scripts/manager.py` — additive only)

1. `log_tool_call(...)` — writes 1 record to `tool-calls.jsonl` **plus**
   a mirrored `log_event("TOOL_CALL", ...)` in `events.jsonl` (both files every time)
2. `log_dispatch(task_id, agent, session_id, attempt)` — records `operation=DISPATCH`
   whenever the Manager hands work to a worker, **plus** a paired `WORKER_STARTED`
3. `wrap_task(task_id, agent, session_id, tool, prompt_text, fn, ...)` — timing wrapper:
   measures `duration_ms` + computes `prompt_hash` (first 12 hex of sha256) + sets
   `status` (`ok`/`error`/`blocked`/`limit`) + `error`, then calls `log_tool_call()` at the end
4. `log_denied_attempt(...)` — records `status="denied"` attempts (never counted as
   successful calls) + `TOOL_DENIED` event for the permission audit

### How to run

```text
python scripts/metrics.py --rebuild        # compute metrics → .agent/manager/metrics.json
python scripts/export_json.py              # export → dashboard/data.json
```

Open the dashboard: double-click `dashboard/index.html`, or run
`python -m http.server` then open `dashboard/index.html`
(5 charts: bar tool calls, line timeline, pie success_rate, bar sessions/recovery/review, line trend).

One-click: double-click `run_dashboard.bat` (repo root, Windows) or `sh run_dashboard.sh`
(macOS/Linux) — rebuilds + exports + cross-project aggregate + serves
`http://localhost:8080/dashboard/all.html` (combined Agent+mcp+Dashboard view,
auto-discovers sibling projects) + opens the browser itself
(uses `http.server` because opening via `file://` gets `fetch(data.json)` blocked).

Run tests:

```text
python -m unittest discover -s scripts -p test_logging.py -v   # 22 tests
python -m unittest discover -s scripts -p test_upgrade.py -v   # original U1-U7, 7/7
```

Tests in `scripts/test_logging.py` use a tmp dir / `TEST-*` task ids and clean up after
themselves. Never overwrite real `queue.json` / `checkpoint.json` / events (no `TEST-*`
leftovers in real files).

### Runtime files in `.gitignore`

`.agent/` (including `tool-calls.jsonl`, `events.jsonl`, `metrics.json`, `state.json`,
`queue.json`, `checkpoint.json`, `history/`, `reviews/`, `observability.json`) and `.openvisio/`
never enter git. Dashboard runtime JSON (`dashboard/data.json`, `dashboard/all-projects.json`)
is gitignored too; only the `.html` viewers are committed.

### Rotation guard 1MB

Past 1MB (`1048576` bytes) of `tool-calls.jsonl`, a `TOOLCALLS_GROWING` event is written
recommending rotation (e.g. split per task) — never auto-deletes data.

### No-secret policy

Store only `prompt_hash` (first 12 hex of sha256). Never store full prompts or secrets
(api keys / passwords / tokens) in logs — `prompt_hash` allows after-the-fact correlation
with nothing sensitive on disk.

### Observability extensions (phase 2 — all 9 areas)

1. **Activity feed + errors**: `export_json.build_extra()` pulls the latest 20 rows from
   `events.jsonl` and `status=error` rows from `tool-calls.jsonl` (messages cut at 300 chars)
2. **Queue health**: reads `queue.json`, counts `by_status` + lists `blocked/failed` ids
3. **Timing**: `duration_ms` stats per tool (avg/P95, top 10) from tool-calls
4. **Per-agent split**: sessions (from WORKER_STARTED/WORKER_RESUMED) + calls/tokens/errors per agent
5. **Tokens**: `log_tool_call(..., tokens_in, tokens_out)` (13-field schema), totals per task/overall
   — callers fill them in when known (e.g. from an API response), no default
6. **Touched files**: merges `files_changed` from building checkpoints cross-project (top 10)
7. **Review results**: reviews must call `save_review(task_id, status, findings)` every time,
   writing `reviews/<task>.json` (`severity: critical|major|minor`); dashboards count by severity
8. **Alerts**: `STUCK` (no events for 6h+ on unfinished work), `REVIEW_AT_RISK`
   (review_loop near `max_review_cycles`), `QUEUE_BLOCKED/FAILED`
9. **Trend**: `metrics.py --rebuild` writes `history/YYYY-MM-DD.json` snapshots
   (90 days kept); dashboards draw daily done/tool_calls lines (combined + per project)
10. **Combined view**: `aggregate.py` merges all areas cross-project (default auto-scans
    sibling folders with `.agent/manager/metrics.json`, overridable with `--projects`) →
    `dashboard/all-projects.json`, viewed at `dashboard/all.html` — projects not yet rebuilt
    show SKIP without breaking the page
11. **Fine-grained tool inventory** (`scripts/tools_inventory.py`): 24-tool catalog
    (file/exec/search/orchestration/web/memory/graph) + reads `permission:` from all 6
    agent definitions + merges real call counts from `tool-calls.jsonl` into a
    "tool × agent (allowed/denied/called/failed/last-used)" matrix — off-catalog tools fall
    under `observed`. Principle: **tools ship with the harness, not the model**
    (the model is the engine); what differs is per-agent permission. See the
    tbl-tools/tbl-perm tables on both dashboards.

### v0.2.0 roadmap completion (Agent Observatory)

Roadmap P0+P1+P2 is done (only overnight 1M/10M bench runs remain):
- P0: `graph.py` (Execution Graph, no schema change), 6 efficiency formulas + agent
  comparison table (low_sample flag), `risk.py` (deterministic, observation/recommendation split)
- P1: `observability.py` (`observability.json` v1 contract), `failure.py`
  (7-category taxonomy + recovery analytics + repeated patterns), audit states
  (`log_denied_attempt` + 4 states + SECURITY box), all 5 views
  (Overview/Task/Agent/Session/Graph)
- P2: `bench.py` + `docs/BENCHMARKS.md` — 500K results: rebuild 23s / export 46s /
  aggregate 33s / peak 347MB (O(T×N) → single-pass + compact JSON, evidence-based)
- `test_logging.py` 22 tests + `test_upgrade.py` 7 tests green in all 3 projects
  (Agent/mcp/Dashboard hash-synced)

## 26.31 Routing Boundary (fix-v2 §18 — FIX-V2-17)

Building owns production code. Ownership is exclusive per task type.

### Ownership matrix

| Task type | Owner | Others |
|---|---|---|
| design (architecture planning, design changes, design structure) | planning | building / error_debug / review must not own |
| code (production implementation, normal coding, implementation fixes) | building | planning / error_debug / review must not own |
| classify (root-cause analysis, investigation, complex diagnosis) | error_debug | must not become second production implementation unless task policy explicitly authorizes |
| review (implementation / regression / security review, quality findings) | review | reviews only, never edits files |

### Violation examples

- V1: design task dispatched to building — BLOCKED (design structure owned by planning).
- V2: code task dispatched to error_debug — BLOCKED (error_debug is root-cause only, never second implementation).
- V3: code task dispatched to review — BLOCKED (review owns quality, never edits).
- V4: review task dispatched to building — BLOCKED (quality owned by review).
- V5: classify task dispatched to building — BLOCKED (diagnosis owned by error_debug).

### Routing table reference

- Source of truth for enforcement: `ROUTING_OWNERSHIP` in `scripts/manager.py`.
- Check function: `check_routing(task_type, agent)` returns ALLOW on owner match,
  BLOCKED with reason on misroute, WARNING on unknown task type (compat).
- Dispatch path: `log_dispatch(..., task_type=None)` resolves task type from the
  explicit argument or the task manifest, calls `check_routing`, logs a `ROUTING`
  event on misroute and proceeds (warn, not hard-fail) to preserve compat.

## 26.32 P0 Tests Gate (FIX-V2-27)

- P0 suite: `T02/T03/T04/T05/T08/T11-T17` — must be green before any recovery proceeds.
- Gate-first: `execute_recovery` calls `check_p0_gate()` as the first step, before policy/classify/building.
- Record source: `state["p0_last_result"]` (`{passed: bool, at: now-UTC-ISO}`), fallback to `STATE_FILE` on disk.
- Red / missing / stale / invalid-timestamp → return `{"status": "blocked", "reason": "P0_RED", "detail": ...}` (BLOCKED) + manual path (human must re-run P0 suite and refresh the record, never real lock, never auto-unblock).

## 26.33 Reliability Metrics (FIX-V2-25)

- Reliability first: the 6 reliability metrics outrank efficiency metrics
  (`task_efficiency`, `token_per_success`, `tools_per_success`,
  `recovery_cost_ratio`, `session_efficiency`, `review_rework_rate`).
  A green efficiency number never overrides a red reliability signal.
- All 6 are computed from the event log only (`events.jsonl`), never from
  tool-call contents: per-task booleans roll up into `totals`, and
  `rebuild()` exposes the same 6 under the `reliability` payload key.
  `compute_metrics` arity is unchanged (still returns 4 values).
- Definitions:
  1. `recovery_success_rate` = tasks with `recovered_then_done` /
     tasks with recovery (`recovery_count > 0`), else `None` on no recovery.
     Per-task `recovered_then_done` = `RECOVERY_STARTED` seen and `TASK_DONE` seen.
  2. `false_done_count` = tasks where `TASK_DONE` exists without
     `VERIFYING_SUCCESS` (per-task `false_done`).
  3. `duplicate_worker_count` = tasks with more than one `WORKER_STARTED`
     (per-task `duplicate_worker` = `sessions_created > 1`).
  4. `data_loss_count` = tasks where a `TASK_BLOCKED` carrying
     `files_touched` has no later `TASK_DONE` / `TASK_UNBLOCKED` /
     `RECOVERY_RESOLVED` (per-task `data_loss`).
  5. `unresolved_count` = tasks whose last `TASK_BLOCKED` / `TASK_FAILED`
     sits after the last `TASK_DONE` / `TASK_UNBLOCKED` /
     `RECOVERY_RESOLVED` (per-task `unresolved`).
  6. `completion_rate` = `done` / `len(per_task)` (`0.0` on empty).


## 26.34 Capability Matrix (FIX-V2-20)

Scope: fix-v2 §21 — verify what the session lifecycle can actually do
before building the full controller. Rows are lifecycle capabilities;
columns are Available / Interface / Reliable. Status is honest:
cells marked with gaps were observed during this fix-v2 run.

| Capability | Available | Interface | Reliable | Evidence + status |
|---|---|---|---|---|
| create | yes | `log_dispatch` | partial | WORKER_STARTED event + DISPATCH tool-call record per dispatch. Gap: record-only, no live process-handle check, worker death is inferred later via watchdog, not at create time. Status: USABLE_WITH_GAP |
| discover | no | none | no | No session-listing API exists in this repo; controller sees only queue/state/checkpoint files. Live OpenCode session enumeration was never demonstrated in this run. Status: GAP — controller must not assume it can find sessions |
| resume | yes | `generate_resume_context` | partial | Resume context builder + checkpoint file round-trip. Gap: quality depends on worker-written checkpoint; stale checkpoints fall back to classify. Status: USABLE_WITH_GAP |
| recover | yes | `recover_from_hierarchy` | partial | Recovery hierarchy + `build_recovery_plan`; unknown modes degrade to CLASSIFY_ONLY. Gap: novel failure modes still need human triage. Status: USABLE_WITH_GAP |
| verify | yes | `verify_with_provider` | partial | VerificationProvider abstraction + `verify_completion` evidence gate. Gap: provider-dependent; TASK_DONE without VERIFYING_SUCCESS is counted as false_done rather than prevented. Status: USABLE_WITH_GAP |
| review | yes | `save_review` | partial | Review record persists status + findings. Gap: quality judgment is owned by the review agent; controller only records, never grades. Status: USABLE_WITH_GAP |
| lease | yes | `verify_lease_fencing` | yes (single-host) | Fenced dispatch aborts stale holders with StaleLeaseError; file-lease round-trips green in suite. Limit: single-host file lease, no distributed fencing. Status: OK |
| reconcile | yes | `reconcile_state` | partial | `reconcile_state` + `reconcile_tool_calls` map STARTED-only calls to UNKNOWN instead of assuming failure. Gap: UNKNOWN outcomes still need triangulation or human call. Status: USABLE_WITH_GAP |

Mapping to fix-v2 §21 original rows: Create session -> create; Send prompt / Read output -> create (prompt_text + tool-call record, same gap as create); Get session status -> discover (GAP); Detect tool limit -> reconcile (partial, via lifecycle + threshold); Detect process death -> create/watchdog (partial, inferred not detected); Resume -> resume; Terminate session -> GAP (no terminate interface exists; stale leases are fenced, sessions are never killed by the controller).

Enforcement: `CAPABILITY_MATRIX` in `scripts/manager.py` mirrors this table; `get_capability(name)` reads one cell; `guard_capability(name)` is the consult gate — unknown or unavailable returns SKIP with reason, never raises. `log_dispatch` consults `create` first and returns a skipped dict instead of dispatching when unavailable.


## 26.35 Invariant-Structure for Hardening Cases (FIX-V2-26)

Scope: all existing test_Txx_ methods in scripts/test_hardening.py.
Every T-case docstring MUST carry the same 4-heading block so intent
and invariants are reviewable without reading the body.

Template (1-2 lines per heading, docstrings only, no logic change):

    GIVEN: <isolated precondition: tmp queue/state/checkpoint/operations/events/lock>
    WHEN: <action under test: startup_recovery / hierarchy / reconcile / lock / classify / verify / watchdog>
    THEN: <assertion: expected status string / flag / bounded plan>
    EXPECTED INVARIANTS: <tmp-only; real files and real lock untouched; deterministic/idempotent>

Every-T-case rule: each test_Txx_ method keeps its original first-line
summary, then adds the 4 headings above; a meta-test using
inspect.getdoc over all test_Txx_ methods asserts all 4 headings are
present and fails listing offenders. Suite stays green via repo-root
`python -m unittest discover -s scripts -p "test_*.py"`. Never take a
real lock; never touch real queue/state/checkpoint files.

## FIX-V2-28 MVP boundary (dispatch focus)

Advanced-out (never in manager core): parallel / distributed / discord /
routing / rollback / dashboard / benchmark / planning.
Core allowlist (stdlib-only): os, json, time, hashlib, subprocess, uuid,
datetime — locked by `assert_mvp_boundary()` scanning own import lines at
import time and raising `ImportError` on violation.
Focus: tool-limit recovery loop only — dispatch + recover + verify survive
stub-raise; tmp-isolated tests; never real lock.

## Phase0-5 Roadmap (FIX-V2-29)

Scope: fix-v2 §30 — one primary roadmap (Phase 0-5). Legacy sets below
are DONE checklists only, not competing roadmaps; their content is not
duplicated here.

### Phase 0 — OpenCode Capability Validation

Verify session lifecycle capabilities (fix-v2 §21 capability matrix).

### Phase 1 — Recovery Foundation

Watchdog, Recovery, Event System, Operation records, Lease/lock,
State reconciliation.

### Phase 2 — Safe Orchestration

Queue, Context Manager, Policy, Budget, Approval.

### Phase 3 — Verification

Review, VERIFYING, Evidence, Verification Providers.

### Phase 4 — Observability

Logging, Metrics, Dashboard.

### Phase 5 — Advanced Automation

Parallel workers, Git automation, Discord, Scheduled/background work.
Out of MVP core (see FIX-V2-28 boundary).

### Legacy mapping (checklists — DONE, no duplication)

- [x] Phase 1-6 / Phase1-6 DONE → folded into Phase 0-5 above.
- [x] U1-U7 DONE → U1-U3 in Phase 1, U4-U7 in Phase 2.
- [x] P0-P2 / P0/P1/P2 DONE → P0 in Phase 1, P1 in Phase 2-3, P2 in Phase 5.
- [x] FIX-1–FIX-5 / FIX-1-5 DONE → implementation checklists under Phase 1-3.
- [x] FIX-V2-01–29 / FIX-V2-01-29 DONE → Phase 0 (FIX-V2-20), Phase 1 (FIX-V2-12/13/14/18/19), Phase 2 (FIX-V2-28), Phase 3 (FIX-V2-11/17/21/27), Phase 4 (FIX-V2-22/23/24/25).

---

# ภาคผนวก DOC-02 — รวมเอกสาร New-Architecture + Roadmap + สถานะบิลด์ (คีย์ DOC-02:PLAN:001)

> วิธีรวม: integrate+dedupe — คงโครง §1–§26 เดิมทุกประการ ส่วนใดมีใน design.md แล้วให้อ้างเลข § เดิม ไม่เขียนซ้ำ
> ต้นฉบับ (อ่านอย่างเดียว ห้ามแตะ): `New-Architecture-v1.md` (719 บรรทัด 17 ส่วน) + `Implementation-Roadmap-v1.md` (612 บรรทัด Phase 0–9+MVP) + `implementation-plan.md` (84 บรรทัด รีเฟรชแล้ว)
> หลัก: ไม่ผูกโมเดลเฉพาะรุ่น (model-agnostic) — เครื่องมือมากับฮาร์เนส ไม่ใช่โมเดล

## ผนวก ก. New Architecture v1 — สรุปย่อ (อ้างเลขส่วนต้นฉบับ)

วิชัน (Vision): ใช้ LLM reasoning เฉพาะจุดจำเป็น ย้ายงานดีเทอร์มินิสติกให้ระบบทำแทน — ลดโทเค็น/calls เพิ่ม correctness/reliability รองรับ parallel อย่างปลอดภัย วัดผลจากข้อมูลจริง
แบ่งอำนาจ (§2): `Manager = Decision Authority` (เข้าใจ intent วิเคราะห์ วางแผน สร้าง Task Contract เลือกความซับซ้อน ประสาน execution รับมือ failure) / `System = Rule Authority` (ตรวจ dependency/conflict/completeness/safety จัด DAG/wave/lock เกต verification/recovery — Manager บายพาสไม่ได้)
Confidence 4 สถานะ (§4): KNOWN (รู้จาก context) / ASSUMED (คาดเดา ใช้เป็น fact ไม่ได้) / UNKNOWN (ต้อง investigate) / VERIFIED (ตรวจแล้ว) — กฎเหล็ก `ASSUMED != VERIFIED` กัน hallucination
Task Contract (§5): ทุกงาน non-LOW ต้องมี Task ID/Objective/Inputs/Outputs/Dependencies/Files To Read-Modifying/Constraints/Acceptance/Verification/Risk — worker รันจาก contract ไม่ใช่คำสั่ง vague
รูเตอร์ 3 ระดับ (§6): LOW (typo/config/ฟังก์ชันเล็ก → Context→Manager→Building→Test ข้าม validator) / MEDIUM (ฟีเจอร์ → Contract→Validator→Scheduler→Building→Review→Verification) / HIGH (เปลี่ยนสถาปัตยกรรม → เพิ่ม Risk Check + Additional Review + Parallel)
Validator (§7, deterministic ไม่ใช้ LLM): ตรวจ Dependency (missing/circular/wrong-order) + Scope (ownership-conflict/duplicate/unclear) + Completeness (reject งานไม่มี verification) + Safety (protected-file/risky/migration)
Scheduler + Ownership + Parallelism (§8–§10): สร้าง DAG แบ่ง wave assign worker; ทุกทาสก์ประกาศ owns เมื่อชนทางเลือก serialize/split/exclusive-lock; parallel แบบ adaptive (task count + independence + context cost + conflict risk — more agents != always faster)
Verification/Recovery (§11–§12 — มีแล้วใน design.md ไม่เขียนซ้ำ): ดู §19 + §26.3 (Evidence gate) + §26.10 (VERIFYING บังคับ) + §11 + §18 + §26.4/§26.11/§26.16/§26.23–§26.24 (recovery hierarchy + idempotent + re-entrant)
Metrics/Feedback (§13–§15 — มีแล้ว ไม่เขียนซ้ำ): ดู §26.5 (events) + §26.28 (tool-calls/metrics/dashboard) + §26.33 (reliability) — วัด LLM Tokens-Calls/Time ต่อ successful task + success/recovery rate; ผลลัพธ์วนปรับ context (project-specific intelligence ไม่ใช่เทรนโมเดล)
หลัก §17 (6 ข้อ): 1) อย่าเพิ่มเอเจนต์ถ้าไม่มี measurable benefit 2) อย่าใช้ LLM แก้ปัญหาที่โค้ดแก้ได้ 3) อย่าบังคับทุกทาสก์ผ่าน workflow ใหญ่ 4) progressive planning 5) วัดก่อนเพิ่มฟีเจอร์ 6) reliability > autonomy — อนึ่ง §16 ถอด OpenVisio ออกจาก core แล้ว (ใช้ event/state ต่อภาพภายหลังได้)

## ผนวก ข. Roadmap Phase 0–9 + MVP (อ้าง implementation-plan.md §3–§4)

| เฟส Roadmap | ประเด็น | สถานะบิลด์ (อ้าง implementation-plan.md) |
|---|---|---|
| Phase 0 Baseline | `baseline-report.json` (LLM calls/tokens/latency + time/success/retry/failure-type) ทำก่อนทุกเฟส | สร้างเสร็จ (`collect_baseline` DONE) |
| Phase 1 Context | compiler/cache เดิม (ลดโทเค็น 59.1%) | ข้าม — มีแล้ว คงเดิม |
| Phase 2 Contract | เติม confidence-state + บังคับ verification (non-LOW) | สร้างเสร็จ (`confidence_tags` P2-01 + `task_contract` P2-02) |
| Phase 3 Router | rule-based classifier LOW/MEDIUM/HIGH + เหตุผลทุกครั้ง | สร้างเสร็จ (`task_levels` P3-01) |
| Phase 4 Validator | completeness gate ปฏิเสธงานไม่มี verification | สร้างเสร็จ (`require_verification` P4-01) |
| Phase 5 Scheduler | เอกสาร serialize/split/exclusive-lock (`docs/ownership-policy.md`) | สร้างเสร็จ เวฟ `[A,B,D]→[C]→[E]` คงเดิม |
| Phase 6 Verification | `verification-report.json` (tests/build/files/acceptance/review) | สร้างเสร็จ (`verify_report` P6-01) |
| Phase 7 Recovery | failure classification 6 แบบ + retry budget (ใช้ checkpoint/lease/events เดิม — มีแล้วดู §26.4/§26.11 ไม่เขียนซ้ำ) | สร้างเสร็จ (`failure_kinds` P7-01) |
| Phase 8 Parallelism | ตัวเลือก worker 1/3/5 บน `capacity_hint` เดิม | สร้างเสร็จ (`worker_choice` P8-01) |
| Phase 9 Feedback | feedback เต็มรูป Task/Plan/Execution/Result/Failure → context improvement (ไม่เทรนโมเดล) | สร้างเสร็จ (`feedback_loop` P9-01) |
MVP YES (v1.0 ต้องมี — มีแล้วครบ): Context Cache / Task Contract / Validator / Scheduler / Verification / Metrics (+baseline ใหม่)
MVP NO (ไม่ทำใน v1.0): Multi Manager / Swarm Intelligence / Auto Learning / Complex AI Critic / Visualization dependency (รวม OpenVisio ที่ถอดตาม §16)

## ผนวก ค. Build-status appendix — ตารางเฟสที่สร้างเสร็จพร้อมหลักฐาน

| เฟส | สิ่งที่ส่งมอบ | หลักฐาน (อ้าง implementation-plan.md §2 + ผลรันจริง) |
|---|---|---|
| Phase 0 | `baseline-report.json` + `collect_baseline` | ไฟล์ baseline 1 ชุด รันซ้ำดีเทอร์มินิสติก + batch_seq 3 เวลา (queue/compile/persist) |
| Phase 2 | `confidence_tags` (P2-01) + `task_contract` (P2-02) | `validate_completeness` ตรวจฟิลด์ confidence + verification ผ่าน |
| Phase 3 | `task_levels` (P3-01) classifier rule-based | ผ่านเคส LOW/MEDIUM/HIGH + คืนเหตุผลทุกครั้ง |
| Phase 4 | `require_verification` (P4-01) | reject งานไม่มี verification อัตโนมัติ |
| Phase 5 | `docs/ownership-policy.md` | เวฟ `[A,B,D]→[C]→[E]` ชน 0 คู่ `capacity_hint=5` + `capacity_skips` นับถูก |
| Phase 6 | `verify_report` (P6-01) | ทุก DONE อ้าง `verification-report.json` 5 ช่อง |
| Phase 7 | `failure_kinds` (P7-01) | จำแนก 6 แบบ TOKEN_LIMIT/MODEL_ERROR/CODE_ERROR/TEST_FAIL/CONFLICT + retry budget ลงไฟล์ |
| Phase 8 | `worker_choice` (P8-01) | เลือก 1/3/5 จาก task count + conflict risk + token budget; เบนช์ CU-09B เคสหนักสุดรวม ~16ms |
| Phase 9 | `feedback_loop` (P9-01) | pattern report จากรันจริง → ปรับ context (ไม่เทรนโมเดล) |
ภาพรวม: คิวจบหมด สวีตเทส **127 → 161 เทสเขียวทั้งหมด** (ชุด `test_lean_merge.py` ตรวจ 7 ฟิลด์ + topology + capacity-bound ผ่าน) เหลือข้อมูลโทเค็นจริงสถานะ UNKNOWN อย่างซื่อสัตย์รอวัดจากรันจริง — จุด dedupe ที่ทำ: recovery/locks/events/review/verification ทั้งหมดอ้าง §19/§26 เดิม ไม่เขียนตรรกะซ้ำในภาคผนวกนี้
