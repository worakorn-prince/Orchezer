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
.agent/manager/state.json
```

Example:

```json
{
  "schema_version": 1,
  "project": "example",
  "status": "running",
  "current_task_id": "TASK-001",
  "phase": "building",
  "active_agent": "building",
  "active_session_id": "session-id",
  "retry_count": 0,
  "lock_owner": null,
  "updated_at": "ISO-8601"
}
```

Should store at minimum:

- schema_version
- project
- overall status
- current task
- current phase
- active agent
- active session
- retry count
- lock_owner
- timestamps
- last known result

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
  "status": "in_progress",
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
- The Manager treats this file as the source of truth — never guess from silence
- If the file is missing or older than 1 session: recover primarily from `git diff` + file timestamps, with the checkpoint as a supplement

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

# 17. State Machine

The Manager should use an explicit state machine:

```text
IDLE
 |
 v
PLANNING
 |
 v
ERROR_DEBUG
 |
 v
BUILDING
 |
 +--> BUILDING_RESUME
 |        |
 |        v
 |      BUILDING
 |
 v
REVIEWING
 |
 +--> BUILDING
 |
 v
DONE

Additional states:

BLOCKED
FAILED
CANCELLED
```

Not every state is needed in the MVP, but the design should accommodate them.

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

A task counts as `DONE` when (evidence-based completion gate — see §26):

- implementation complete per requirements
- relevant tests pass (test result as evidence)
- no unresolved blockers
- Review passes (structured findings with no outstanding CRITICAL/HIGH)
- checkpoint updated
- manager state updated
- queue marked completed
- git diff inspected (worker changes separated from user changes)
- work touching indexed projects: equivalence proof passes + graph numbers within tolerance (see §25)

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
powershell -ExecutionPolicy Bypass -File "D:\Coding_Project\Agent\scripts\e2e-openvisio.ps1"
```

Results: `D:\Coding_Project\Agent\.agent\building\e2e-ov-b.json` (must pass 5/5: 2-project skeleton + export/prove equivalence + watch freshness + token-proxy)

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
8. **Policy / Budget / Human Approval**: enforces limits (max sessions, review cycles, runtime), classifies risk (LOW, MEDIUM, HIGH) and mandates a Human Approval Gate for high-risk operations (e.g. destructive changes, database/architecture changes)

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

## 26.1 Watchdog Data + Stale Detection

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

## 26.3 Evidence-Based Completion Gate

The Manager checks before marking DONE:

```text
implementation complete? tests pass? blockers empty? review pass?
checkpoint updated? manager state updated? queue state updated?
```

Minimum evidence: changed files, git diff, relevant tests + test results, checkpoint, requirement coverage, review results. For indexed projects, OpenVisio verification must pass (see §25).

## 26.4 Idempotent Recovery

- Idempotency key: `task_id:operation:attempt` (e.g. `TASK-001:BUILD:003`)
- Before creating a new session, discover active sessions first: found → recover existing, not found → create new
- Never blindly create duplicate sessions after a Manager restart
- Every lifecycle operation must be retryable without duplicate side effects

## 26.5 Event Log

The `.agent/manager/events.jsonl` file is append-only:

```json
{"event":"TASK_CREATED","task":"TASK-001","time":"..."}
{"event":"WORKER_STARTED","task":"TASK-001","session":"..."}
{"event":"PROGRESS","task":"TASK-001","milestone":"repository"}
{"event":"WORKER_LIMIT","task":"TASK-001","session":"..."}
{"event":"WORKER_RESUMED","task":"TASK-001","session":"..."}
{"event":"REVIEW_FAILED","task":"TASK-001"}
{"event":"TASK_DONE","task":"TASK-001"}
```

Required events: task created/queued/started, worker created/stopped/limit, checkpoint, recovery, resume, review started/failed/passed, blocked, failed, cancelled, done — every event carries a timestamp + task ID

## 26.6 Context Manager

Build `.agent/manager/context/TASK-xxx.resume.md` instead of sending the whole conversation:

```markdown
# Resume Context
Task: / Current phase: / Completed: / Current: / Remaining:
Important decisions: / Files changed: / Known issues:
Next action: / Do not redo:
```

Context send priority: Resume Context > Checkpoint > Current State > Relevant design section > Working tree > Relevant files. Never send whole conversations/logs/the entire repo source unless necessary.

## 26.7 Lease / Lock

```json
{ "lock": { "owner": "manager-01", "acquired_at": "ISO-8601", "expires_at": "ISO-8601" } }
```

Use leases instead of permanent locks — an expired lease lets a new Manager recover. Prevents: a task executed twice, a worker controlled by two Manager instances, duplicate recovery.

## 26.8 Policy / Budget / Human Approval

```json
{
  "limits": { "max_worker_sessions": 5, "max_review_cycles": 3, "max_retries": 3, "max_runtime_minutes": 120 },
  "watchdog": { "heartbeat_timeout_seconds": 120, "progress_timeout_seconds": 600, "checkpoint_timeout_seconds": 900 },
  "approval": { "require_for_destructive": true, "require_for_architecture_change": true },
  "git": { "auto_commit": false }
}
```

Risk: LOW → automatic, MEDIUM → per policy, HIGH → APPROVAL_REQUIRED (e.g. changing databases, deleting APIs, changing auth, deleting many files, major dependency upgrades, git reset, destructive operations). The Manager must never bypass the approval gate.

## 26.9 Dependency-Aware Queue

Queue states: `pending | ready | running | blocked | failed | completed | cancelled` — the Manager checks `dependency complete?` before dispatch. Tasks whose dependencies haven't passed must not be dispatched.

## 26.10 Enhanced State Machine

```text
IDLE → PLANNING → ERROR_DEBUG → BUILDING
BUILDING → LIMIT/STUCK → RECOVERING → BUILDING
BUILDING → REVIEWING → FAIL → BUILDING
REVIEWING → PASS → VERIFYING → DONE | BLOCKED
Additional: FAILED, CANCELLED, APPROVAL_REQUIRED, RECOVERING
```

`RECOVERING` separates the recovery process from normal building.

## 26.11 Recovery Protocol

```text
WORKER STOP → MANAGER DETECT → ACQUIRE LOCK → READ STATE → READ CHECKPOINT
→ INSPECT WORKING TREE → INSPECT ACTIVE SESSION → INSPECT EVENTS → CLASSIFY
```

Classification: `LIMIT | CRASH | STUCK | FAILED | DONE | BLOCKED | UNKNOWN`
- LIMIT/CRASH/STUCK → RECOVER → NEW SESSION → RESUME
- UNKNOWN → ERROR_DEBUG → CLASSIFY (no blind resume)

The Manager must never: blind git reset/checkout/delete/overwrite, blind retry forever, blind create duplicate sessions, blind mark DONE. Destructive operations must pass policy first. Recovery must run `git status` + `git diff` to separate worker changes from user changes — never assume every diff belongs to the worker.

## 26.12 Review Loop + Observability

Reviews must produce structured findings:

```json
{ "status": "failed", "findings": [ { "severity": "high", "file": "src/example.ts", "issue": "missing validation", "required_action": "add input validation" } ] }
```

The Manager feeds findings as input to the next Building round.

Log levels: `INFO | WARN | ERROR | RECOVERY | SECURITY` — every log carries timestamp, task_id, agent, session_id, event, message. Goal: reconstruct past events.

## 26.13 Decision Log

`.agent/manager/decisions/TASK-xxx.md` records: Routing (selected + reason), Recovery (decision + source), Review Failure (finding + decision), Final — goal: retrace what the Manager decided and why.

## 26.14 Acceptance Criteria (summary)

| Area | Criteria |
|---|---|
| Recovery | detect limit/stop/stuck, recover from checkpoint, no duplicates, resumable, recover after restart |
| State | state/checkpoint/event log persistent, lease works, transitions correct |
| Verification | never mark DONE from a single claim, check tests/diff/review/blockers, indexed projects pass verification |
| Reliability | bounded retries, no infinite loops, preserve user changes, idempotent recovery |
| Context | build + use resume context, never rely on old conversations |
| Safety | destructive ops pass policy, architecture passes approval, no auto git reset/commit |

Required test scenarios: T01 normal, T02 tool limit, T03 multiple limits, T04 crash, T05 stuck, T06 review fail, T07 repeated failure, T08 duplicate protection, T09 user changes, T10 approval

## 26.15 Tool-call Logging + Metrics + Dashboard (TASK-LOG-001 → 002 → 003)

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

### 6 Metrics + formulas (`scripts/metrics.py --rebuild`)

Reads `events.jsonl` + `tool-calls.jsonl` and computes per task (re-runnable with identical results = idempotent):

| Metric | Formula |
|---|---|
| `sessions_per_task` | count `WORKER_STARTED` / `WORKER_RESUMED` per task |
| `tool_calls_per_task` | count non-denied `tool-calls.jsonl` rows per task |
| `recovery_count` | count `RECOVERY_STARTED` per task |
| `review_loop` | count `REVIEW_FAILED` per task — over `max_review_cycles=3` in `config.json` → `BLOCKED` |
| `time_to_DONE` | `TASK_DONE.time − TASK_CREATED.time` (seconds, `null` when absent) |
| `success_rate` | `DONE / (DONE + failed + cancelled)` overall (0–1) |

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

