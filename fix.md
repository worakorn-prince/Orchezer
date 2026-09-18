# Manager Agent — Fix & Hardening Plan

> Purpose: รวบรวมปัญหาที่พบจากการ review `design.md` และกำหนดแนวทางแก้ไขก่อนยกระดับ Manager ให้เป็น reliable orchestration runtime
>
> Source of review: `design.md` ตัวจริง 1,472 บรรทัด
>
> Priority:
> - P0 = ต้องแก้ก่อนพึ่งพา autonomous recovery
> - P1 = ควรแก้ก่อนใช้งานจริงระยะยาว
> - P2 = ปรับปรุงภายหลัง / ไม่ควรขยาย scope ตอนนี้

---

# 1. Executive Summary

Architecture ปัจจุบันแข็งแรงมากในด้าน orchestration, checkpoint/recovery, evidence-based completion, watchdog, policy และ observability

แต่มีจุดที่ควร harden ก่อน โดยเฉพาะ:

1. Checkpoint อาจหายก่อน worker มีโอกาสเขียนก่อน tool-call limit
2. Watchdog พึ่ง heartbeat จาก worker มากเกินไป
3. `state.json` และ `events.jsonl` ยังไม่มี consistency/reconciliation contract ที่ละเอียดพอ
4. Lease/lock ถูกวางรวมกับ state มากเกินไป
5. Idempotency ยังไม่ครอบคลุมกรณี Manager crash ระหว่าง side effect
6. การแยก user changes กับ worker changes ด้วย `git diff` อย่างเดียวไม่เพียงพอ
7. Review severity ใช้หลาย vocabulary (`HIGH`, `MAJOR`, `critical`) ต้องทำให้เป็นมาตรฐานเดียว
8. `max_worker_sessions` / `max_retries` / review cycles ยังต้องนิยาม semantics ให้ชัด
9. `VERIFYING` ควรเป็น state จริง ไม่ใช่แค่ขั้นตอนในบางส่วนของ design
10. OpenVisio ควรเป็น optional verification provider ไม่ใช่ dependency ของ Manager core
11. Manager self-recovery ต้องเป็น first-class scenario
12. Task ควรมี identity/manifest ที่ชัดขึ้น
13. Observability เริ่มขยาย scope จนเสี่ยงทำให้ Manager core บวม

หลักการแกนกลางที่ต้องรักษา:

```text
Conversation = temporary
State = persistent
Agent session = disposable
Task = persistent
Checkpoint = recovery mechanism
Manager = orchestrator
```

---

# 2. P0 — Checkpoint Reliability

## Problem

ปัจจุบัน design กำหนดให้ Building เขียน checkpoint ก่อน session จบ และ Manager ใช้ checkpoint เป็น source of truth

ปัญหาคือ tool-call limit หรือ crash สามารถเกิดก่อน Building จะมีโอกาสเขียน checkpoint ได้

ตัวอย่าง:

```text
Building
  ↓
edit
  ↓
test
  ↓
read
  ↓
edit
  ↓
TOOL LIMIT
  ↓
checkpoint ไม่ถูกเขียน
```

แม้ design จะมี fallback ด้วย `git diff` + timestamps แต่ hierarchy ยังควรชัดกว่านี้

## Fix

เปลี่ยน concept จาก:

```text
checkpoint = absolute source of truth
```

เป็น:

```text
checkpoint = preferred recovery state
```

Recovery evidence hierarchy:

```text
1. Checkpoint
2. Event Log
3. Manager State
4. Active Session State
5. Working Tree / Git Evidence
6. Filesystem timestamps
```

เมื่อ checkpoint หาย/stale:

```text
Checkpoint
    ↓
Events
    ↓
State
    ↓
Active session
    ↓
git status / git diff
    ↓
timestamps
    ↓
reconstruct state
```

## Acceptance

- Tool-limit ก่อน checkpoint ต้อง recover ได้
- Crash ก่อน checkpoint ต้อง recover ได้
- Manager ห้าม assume ว่า missing checkpoint = no progress
- Recovery ต้องบันทึกว่า state ถูก reconstruct จาก evidence ใด

---

# 3. P0 — External Watchdog

## Problem

Watchdog ปัจจุบันมี heartbeat/progress/checkpoint timestamps แต่ worker อาจค้างและหยุดส่ง heartbeat

ดังนั้น:

```text
worker heartbeat = evidence
```

ไม่ควรเท่ากับ:

```text
worker is alive = fact
```

## Fix

Manager ต้องมี external observation:

```text
Manager
  ↓
OpenCode session/process status
  ↓
session activity
  ↓
event activity
  ↓
checkpoint freshness
  ↓
worker heartbeat
```

แล้ว classify:

```text
HEALTHY
SLOW
STUCK
DEAD
UNKNOWN
```

Heartbeat เป็นเพียงหนึ่ง signal

## Acceptance

Manager สามารถ detect:

- worker ไม่ส่ง heartbeat
- worker session หาย
- process/session ค้าง
- worker ยังเปิดแต่ไม่มี progress
- state ไม่เปลี่ยนตามเวลาที่กำหนด

---

# 4. P0 — State/Event Consistency

## Problem

ปัจจุบัน:

```text
state.json = current truth
events.jsonl = historical truth
```

แต่ยังไม่มี protocol ที่ชัดเจนเมื่อสองแหล่งไม่ตรงกัน

ตัวอย่าง:

```text
state.json → BUILDING

events.jsonl ล่าสุด → WORKER_STOPPED
```

หรือ Manager crash ระหว่างเขียน state

## Fix

กำหนด state transition protocol:

```text
1. validate transition
2. create event
3. persist event
4. persist state
5. update timestamps
```

และเมื่อ Manager startup:

```text
LOAD STATE
   ↓
LOAD RECENT EVENTS
   ↓
VALIDATE CONSISTENCY
   ↓
RECONCILE IF NEEDED
   ↓
RESUME / RECOVER
```

ควรมี:

```text
state_version
event_sequence
updated_at
```

เพื่อช่วยตรวจ stale write / ordering

## Acceptance

- Manager restart แล้ว reconstruct state ได้
- state/event mismatch ไม่ทำให้เกิด duplicate worker
- inconsistent state → `RECOVERING` หรือ `BLOCKED`
- ทุก transition สำคัญมี event

---

# 5. P0 — Separate Lock From State

## Problem

ปัจจุบัน lease/lock ถูกอธิบายไว้ใน state structure

แนวคิด lease ถูกต้อง แต่ state และ synchronization เป็นคนละ responsibility

## Fix

แยก:

```text
.agent/manager/state.json
.agent/manager/manager.lock
```

หรือใช้ OS-level atomic lock

Concept:

```text
state = application data
lock = synchronization primitive
```

Lease ต้องมี:

```text
owner
acquired_at
expires_at
```

และต้อง acquire แบบ atomic

## Acceptance

- Manager สองตัวไม่สามารถควบคุม task เดียวกันพร้อมกัน
- expired lease สามารถถูก takeover ได้
- Manager crash ไม่ทำให้ lock ค้างถาวร
- duplicate recovery ไม่เกิดจาก concurrent managers

---

# 6. P0 — Stronger Idempotent Operations

## Problem

ปัจจุบันมี:

```text
task_id:operation:attempt
```

แต่ยังไม่ครอบคลุมกรณี:

```text
Manager
  ↓
CREATE_SESSION
  ↓
session ถูกสร้างสำเร็จ
  ↓
Manager crash ก่อนบันทึกผล
```

เมื่อ Manager กลับมาอาจสร้าง session ซ้ำ

## Fix

สร้าง operation record สำหรับ side effects สำคัญ:

```text
operation_id
task_id
operation_type
attempt
status
result
session_id
created_at
completed_at
```

ตัวอย่าง:

```json
{
  "operation_id": "op-001",
  "task_id": "TASK-001",
  "operation": "CREATE_WORKER",
  "attempt": 3,
  "status": "committed",
  "session_id": "session-123"
}
```

ก่อนทำ side effect:

```text
check operation
    ↓
already committed?
    ├─ YES → reuse result
    └─ NO  → execute
```

## Acceptance

ทุก lifecycle operation สำคัญต้องเป็น idempotent:

- CREATE_SESSION
- RESUME
- RECOVER
- REVIEW
- DISPATCH

---

# 7. P0 — Manager Self-Recovery

## Problem

Design แข็งแรงสำหรับ Building crash แต่ Manager เองยังควรถูกกำหนดเป็น failure scenario อย่างชัดเจน

ถ้า:

```text
Building running
      ↓
Manager crashes
```

ระบบต้องไม่เสีย task

## Fix

Startup recovery protocol:

```text
MANAGER START
    ↓
ACQUIRE LEASE
    ↓
READ STATE
    ↓
READ EVENTS
    ↓
DISCOVER ACTIVE WORKERS
    ↓
RECONCILE
    ↓
CLASSIFY
    ↓
RECOVER / RESUME
```

Manager restart ต้องเป็น normal recovery path ไม่ใช่ exceptional workaround

## Acceptance

สร้าง test:

```text
T11 Manager crash/restart
```

Manager restart ระหว่าง:

- Building
- Review
- Recovery
- session creation

แล้วต้องไม่ duplicate task/worker

---

# 8. P0 — User Change Protection

## Problem

`git diff` อย่างเดียวไม่สามารถแยก user change กับ worker change ได้เสมอ

กรณี:

```text
ก่อน task: A
Building → B
User     → C
```

current diff อาจเห็นเพียง:

```text
A → C
```

จึงไม่รู้ว่าใครเป็นเจ้าของแต่ละ change

## Fix

เมื่อเริ่ม task ให้ capture baseline:

```text
TASK START
  ↓
git status
  ↓
file hashes / relevant baseline
  ↓
worker starts
```

จากนั้นใช้:

```text
baseline
+
checkpoint.files_changed
+
current tree
+
git diff
```

เพื่อประกอบการตัดสินใจ

ไม่ควร assume:

```text
all current diff = worker changes
```

## Acceptance

สร้าง test:

```text
T09 user changes
```

ต้องพิสูจน์ว่า Manager:

- ไม่ reset user change
- ไม่ overwrite user change
- ไม่ auto-clean user change
- แจ้ง user เมื่อ ownership ของ change ไม่ชัด

---

# 9. P1 — Standardize Review Severity

## Problem

Design ใช้ severity หลายแบบ:

```text
CRITICAL / HIGH / MEDIUM / LOW
```

และบางส่วน:

```text
critical / major / minor
```

## Fix

เลือก vocabulary เดียวทั้งระบบ

แนะนำ:

```text
CRITICAL
HIGH
MEDIUM
LOW
```

ใช้เหมือนกันใน:

- Review
- Policy
- Dashboard
- Evidence gate
- Alerts
- Decision log

Completion rule:

```text
CRITICAL/HIGH outstanding → NOT DONE
```

---

# 10. P1 — Clarify Retry / Attempt Semantics

## Problem

มีหลาย counter:

```text
max_worker_sessions
max_review_cycles
max_retries
```

แต่ semantics อาจ overlap

## Fix

แยกความหมายชัดเจน:

```text
worker_attempt
= จำนวน session ของ worker

recovery_attempt
= จำนวน recovery operation

task_retry
= จำนวนครั้งที่ task ถูก retry เพราะ failure

review_cycle
= จำนวนรอบ Review → Building → Review
```

ตัวอย่าง:

```text
Worker #1 → LIMIT
Worker #2 → LIMIT
Worker #3 → LIMIT
```

คือ:

```text
worker_attempt = 3
recovery_attempt = 2
```

ไม่จำเป็นต้องเท่ากับ task_retry

## Acceptance

ทุก counter มี definition เดียว และ dashboard ใช้ definition เดียวกัน

---

# 11. P1 — Make VERIFYING a Real State

## Problem

บางส่วนของ design มี:

```text
REVIEWING → DONE
```

แต่ Upgrade Architecture มี:

```text
REVIEWING → VERIFYING → DONE
```

## Fix

ใช้ state machine canonical:

```text
IDLE
  ↓
PLANNING
  ↓
BUILDING
  ↓
REVIEWING
  ↓
VERIFYING
  ↓
DONE
```

Recovery:

```text
BUILDING
  ↓
RECOVERING
  ↓
BUILDING
```

Definitions:

```text
REVIEWING
= ตรวจ implementation quality / bugs / regression / security

VERIFYING
= ตรวจ requirement + evidence + state + tests + diff + final acceptance
```

## Acceptance

ห้าม transition:

```text
REVIEWING → DONE
```

โดย bypass `VERIFYING`

---

# 12. P1 — OpenVisio as Optional Verification Provider

## Problem

OpenVisio มีประโยชน์มากสำหรับ projects ที่ index อยู่ แต่ไม่ควรกลายเป็น dependency ของ Manager core

## Fix

Architecture:

```text
Manager Core
    |
    +-- Generic Verification
    |
    +-- Verification Provider
           |
           +-- OpenVisio
           +-- pytest
           +-- npm test
           +-- custom verifier
```

Project config ระบุ provider:

```json
{
  "verification": {
    "enabled": true,
    "provider": "openvisio"
  }
}
```

Manager core ไม่ควร hard-code Dashboard/mcp/OpenVisio assumptions

## Acceptance

Manager ทำงานได้กับ project ที่ไม่มี OpenVisio

---

# 13. P1 — Task Manifest

## Problem

ข้อมูลของ task กระจายอยู่หลายไฟล์:

```text
state.json
queue.json
checkpoint.json
resume.md
decisions/
changes/
events.jsonl
```

## Fix

เพิ่ม conceptual `Task Manifest`

ไม่จำเป็นต้องทำเป็นระบบใหญ่ทันที

Task ต้องมี identity:

```text
Task ID
Requirement
Type
Owner
Current phase
Attempts
Evidence
Recovery context
Completion status
```

อาจใช้:

```text
.agent/manager/tasks/TASK-001/
    manifest.json
    resume.md
    decisions.md
```

โดยยังให้ `state.json` เป็น global manager state

## Acceptance

เปิด task แล้วสามารถ reconstruct task identity และ lifecycle ได้โดยไม่ต้องอ่านข้อความสนทนา

---

# 14. P1 — External Worker Health Model

เพิ่ม health model ที่รวมหลาย signals:

```text
heartbeat
progress
checkpoint
session status
event activity
filesystem activity
```

ตัวอย่าง:

```text
HEALTHY
  heartbeat fresh
  progress fresh

SLOW
  heartbeat fresh
  progress delayed

STUCK
  session alive
  no progress beyond threshold

DEAD
  session/process unavailable

UNKNOWN
  evidence contradictory or insufficient
```

`UNKNOWN` ต้องเข้าสู่ classification/recovery ไม่ใช่ blind resume

---

# 15. P1 — Recovery Evidence Record

ทุก recovery ควรบันทึกว่า Manager ตัดสินใจจากอะไร

ตัวอย่าง:

```json
{
  "task_id": "TASK-001",
  "classification": "STUCK",
  "evidence": [
    "session_active",
    "heartbeat_stale",
    "no_progress_720s",
    "checkpoint_stale"
  ],
  "decision": "RECOVER",
  "source": "watchdog"
}
```

สิ่งนี้จะทำให้ Decision Log และ Event Log ตรวจสอบย้อนหลังได้จริง

---

# 16. P1 — Recovery Must Be Re-entrant

Recovery ไม่ควร assume ว่า Manager จะทำจนจบในครั้งเดียว

ตัวอย่าง:

```text
RECOVERING
  ↓
Manager crash
```

เมื่อกลับมา:

```text
READ recovery operation
    ↓
already started?
    ↓
already committed?
    ↓
resume recovery
```

Recovery เองต้องเป็น stateful และ idempotent

---

# 17. P2 — Prevent Manager Scope Creep

ปัจจุบัน Manager design เริ่มรวม:

```text
orchestration
recovery
metrics
dashboard
tool inventory
history
alerts
cross-project aggregation
```

ทั้งหมดมีประโยชน์ แต่ core ควรแบ่ง layer:

```text
MANAGER CORE
- orchestration
- state
- recovery
- policy
- verification

OBSERVABILITY
- tool calls
- metrics
- dashboard
- history
- inventory
- trends
```

Manager ส่ง event ออกไป แล้ว Observability อ่าน event

ไม่ควรให้ dashboard logic เข้าไปปนกับ orchestration logic

---

# 18. P2 — Keep Parallelism Out of Current Scope

ยังไม่ควรรีบเพิ่ม:

```text
parallel workers
distributed execution
multiple autonomous coders
```

จนกว่า P0/P1 จะ stable

ลำดับที่ถูกต้อง:

```text
single worker
    ↓
reliable recovery
    ↓
state consistency
    ↓
idempotency
    ↓
verification
    ↓
observability
    ↓
parallel workers
```

Parallelism ก่อน reliability จะเพิ่ม failure modes แบบทวีคูณ

---

# 19. Recommended State Machine

Canonical state machine:

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

Other terminal/control states:
FAILED
CANCELLED
APPROVAL_REQUIRED
```

---

# 20. Recommended Recovery Protocol

Canonical recovery:

```text
1. STOP
2. DETECT
3. ACQUIRE LEASE
4. READ STATE
5. READ CHECKPOINT
6. READ EVENTS
7. DISCOVER ACTIVE SESSION
8. INSPECT WORKING TREE
9. CHECK OPERATION RECORD
10. CLASSIFY
11. RECONSTRUCT CONTEXT
12. APPLY POLICY
13. RECOVER / RESUME
14. VERIFY
15. WRITE EVENT
16. UPDATE STATE
```

Never:

```text
blind resume
blind retry
blind new session
blind reset
blind checkout
blind delete
blind DONE
```

---

# 21. Recommended Test Matrix

Existing T01–T10 should remain.

Add:

```text
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
P0:
T02 T03 T04 T05 T08 T11 T12 T13 T14 T15 T16 T17

P1:
T09 T10 T18 T19 T20 T21 T22

Baseline:
T01 T06 T07
```

---

# 22. Implementation Order

Do NOT implement all fixes simultaneously.

Recommended order:

## Phase FIX-1 — Recovery Foundation

```text
1. Checkpoint hierarchy
2. State/Event consistency
3. Manager self-recovery
4. External watchdog
5. Lock separation
6. Idempotent operation records
```

## Phase FIX-2 — Safety

```text
7. Baseline snapshot
8. User-change protection
9. Approval integration
10. Recovery evidence
```

## Phase FIX-3 — State/Contract Cleanup

```text
11. Standardize severity
12. Clarify attempts/retries
13. Make VERIFYING canonical
14. Re-entrant recovery
15. Task manifest
```

## Phase FIX-4 — Architecture Cleanup

```text
16. OpenVisio provider abstraction
17. Separate Manager core from observability
```

## Phase FIX-5 — Validation

Run:

```text
T01–T22
```

and require all P0 tests to pass before enabling aggressive autonomous recovery.

---

# 23. Definition of Done for This Fix

The Manager hardening work is complete when:

- [ ] Tool-limit recovery works without checkpoint
- [ ] Crash recovery works
- [ ] Manager restart recovery works
- [ ] Watchdog can detect stuck worker without heartbeat
- [ ] State/Event consistency is validated
- [ ] Lock is atomic and lease-based
- [ ] Side effects are idempotent
- [ ] Duplicate sessions cannot be created by restart
- [ ] User changes are protected
- [ ] Review severity is standardized
- [ ] Retry/attempt semantics are unambiguous
- [ ] `VERIFYING` is a canonical state
- [ ] OpenVisio is optional at Manager-core level
- [ ] Recovery decisions are traceable
- [ ] Recovery is re-entrant
- [ ] T01–T22 are implemented
- [ ] All P0 tests pass
- [ ] No blind reset/checkout/delete exists in recovery path
- [ ] Manager never marks DONE from worker claim alone

---

# 24. Final Recommendation

Do not add more intelligence to the Manager yet.

The next improvement should be:

```text
                RELIABILITY
                     ↑
                     |
       Safety ← Manager → Recovery
                     |
                     ↓
                  Evidence
```

Only after this foundation is stable should the project move toward:

```text
parallel workers
automatic git checkpoints
rollback
token budgets
Discord control
scheduled/background execution
```

The target is not:

> “Manager that is smart enough to do everything.”

The target is:

> **“Manager that can keep work moving safely even when workers, sessions, or the Manager itself fail.”**

That is the core reliability milestone.
