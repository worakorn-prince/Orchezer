# Manager Agent — Upgrade v2
## Context-Efficient Orchestration Architecture

> Status: Architecture proposal
> Purpose: Upgrade the existing Manager Agent without replacing the current reliability/recovery foundation.
> Primary constraint: The system relies mainly on free/low-cost coding models where latency and token usage are significant constraints.

---

## 1. Executive Direction

The Manager Agent should evolve from a task controller into a **context-efficient coding-agent orchestrator**.

The core objective is not to maximize the number of agents or maximize parallelism.

The objective is:

> Complete each coding task with the minimum necessary LLM calls, minimum sufficient context, safe parallelism, and reliable recovery.

The architecture should therefore combine:

1. Manager / Supervisor
2. Context Compiler
3. Context Cache
4. Dependency Graph
5. Deterministic Scheduler
6. Adaptive Parallelism
7. Resource Ownership / Conflict Detection
8. Existing State, Checkpoint, Recovery and Verification systems
9. Observability through Scopeboard / visualization tooling

---

> Architecture decision: visualization tooling is removed from this architecture. The execution core must remain visualization-independent.

# 2. Architectural Principles

## 2.1 LLM calls are expensive resources

The Manager must treat LLM calls as a constrained resource.

Prefer:

```text
LLM → reasoning / decomposition / ambiguity resolution
Code → deterministic state management / graph / scheduling / validation
```

Do not use an LLM when deterministic code can produce the same result reliably.

Examples of deterministic operations:

- dependency graph construction after dependencies are known
- topological sorting
- execution waves
- task state transitions
- lock/lease handling
- cache lookup
- cache invalidation
- file ownership checks
- retry counters
- timeout handling
- event recording
- worker counting
- conflict detection

---

## 2.2 Minimal Sufficient Context

Do not send all available project context to every agent.

For every task:

```text
Required Context = minimum information needed for the assigned agent
                   to perform the task correctly.
```

The Manager should prefer:

```text
Global Context
+
Task Context
+
Relevant File Context
+
Relevant Dependency Context
```

over:

```text
Entire project context
+
Entire repository
+
All previous agent output
```

---

## 2.3 Parallelism is conditional

Parallelism must never be enabled merely because multiple tasks exist.

A task is parallel-safe only when:

- dependencies are satisfied
- file/resource ownership does not conflict
- required context versions are valid
- task outputs do not create an unresolved ordering requirement
- model/provider capacity allows additional workers
- expected latency benefit is greater than coordination overhead

---

## 2.4 Manager should be model-agnostic

Do not hard-code architecture around a specific OpenCode Zen free model.

The system must tolerate:

- fast models
- slow models
- temporary unavailable models
- different context windows
- different rate limits
- different quality levels

Model/provider information should be runtime configuration.

---

# 3. Target Architecture

```text
                           USER
                             |
                             v
                         MANAGER
                             |
                 +-----------+-----------+
                 |                       |
                 v                       v
        Context Compiler          Existing State
                 |                 / Recovery
                 v
          Context Cache
                 |
                 v
        Dependency / DAG Engine
                 |
                 v
             Scheduler
                 |
       +---------+---------+
       |         |         |
       v         v         v
   Building   Building   Review/Debug
       |         |         |
       +---------+---------+
                 |
                 v
            Integration
                 |
                 v
             Verification
                 |
                 v
             State Update
                 |
        +--------+--------+
        |                 |
        v                 v
   Context Update    Event Stream
                          |
                  +-------+-------+
                  |               |
                  v               v
              Scopeboard      visualization tooling
```

---

# 4. Context Compiler

## 4.1 Purpose

The Context Compiler converts broad project knowledge into task-specific context packages.

Input:

```text
design.md
architecture
requirements
repository structure
task history
current state
dependency information
known constraints
```

Output:

```text
Project Context
Task Context
Relevant Files
Relevant Requirements
Relevant Dependencies
Constraints
Known Risks
```

---

## 4.2 Context Layers

### Layer A — Global Context

Small and reusable.

Examples:

- project identity
- architecture
- language/runtime
- coding conventions
- important constraints
- global safety rules

### Layer B — Task Context

Specific to one task.

Examples:

- objective
- acceptance criteria
- expected outputs
- assigned files
- dependencies
- relevant requirements
- known risks

### Layer C — File/Artifact Context

Only files and artifacts relevant to the task.

### Layer D — Dependency Context

Only upstream outputs required by the task.

---

# 5. Context Cache

## 5.1 Purpose

Prevent every agent from independently rediscovering the same project information.

Suggested conceptual structure:

```text
.agent/
  manager/
    context/
      project/
        global.json
        architecture.json
        requirements.json

      tasks/
        TASK-001.json
        TASK-002.json

      files/
        src_models.json
        src_validator.json

      dependencies/
        TASK-001_to_TASK-003.json
```

The exact storage format is implementation-dependent.

---

## 5.2 Context Versioning

Every reusable context artifact should have a version or source fingerprint.

Example:

```json
{
  "artifact": "src/models.py",
  "version": 18,
  "source_hash": "...",
  "generated_at": "...",
  "depends_on": []
}
```

If the source changes:

```text
version 18 → version 19
```

dependent cached context becomes stale.

---

# 6. Context Invalidation

When an agent changes a file:

```text
Agent A
  |
  +-- modifies models.py
  |
  v
Context version changes
  |
  v
Invalidate affected cached artifacts
  |
  v
Recompute only affected downstream context
```

Do not invalidate the entire project cache unless required.

Goal:

> Incremental context refresh instead of full re-analysis.

---

# 7. Dependency Graph

Tasks should be represented as a DAG whenever possible.

Example:

```text
        A --------+
                  |
        B --------+----> C ----> E
                  |
        D --------------------> E
```

The scheduler should derive executable waves:

```text
Wave 0:
A, B, D

Wave 1:
C

Wave 2:
E
```

Tasks in the same wave may run concurrently if resource/conflict checks pass.

---

# 8. Deterministic Scheduler

Once task dependencies are known, scheduling should primarily be code-driven.

Responsibilities:

- topological ordering
- ready-task detection
- execution wave creation
- dependency completion checks
- worker assignment
- retry scheduling
- blocked-task handling
- lock/lease checks
- conflict detection

The Scheduler should not repeatedly ask an LLM to decide things that can be calculated.

---

# 9. Adaptive Parallelism

Do not use a fixed worker count.

Conceptually:

```text
parallel_capacity =
    model_capacity
    × task_independence
    × context_cost
    × conflict_risk
    × provider_limits
```

Examples:

### Slow free model

```text
workers = 1–2
```

### Fast provider

```text
workers = 2–4+
```

### High-conflict task set

```text
workers = 1
```

### Independent read-heavy tasks

```text
workers can increase
```

The actual formula should be benchmark-driven rather than hard-coded initially.

---

# 10. Resource Ownership

Parallel agents must not unknowingly edit the same resource.

Each task should expose an estimated ownership set:

```text
TASK-A
owns:
  src/auth/*
  src/user.py

TASK-B
owns:
  src/payment/*
```

If:

```text
TASK-A → src/shared/types.py
TASK-B → src/shared/types.py
```

the scheduler should treat this as a conflict.

Possible decisions:

1. serialize
2. split the task
3. acquire an exclusive lock
4. allow parallel work only when the operation is provably non-conflicting

The existing lease/lock/recovery mechanisms should remain the source of truth for runtime safety.

---

# 11. Manager Decision Policy

Manager should use this decision sequence:

```text
1. Understand user objective
2. Check existing state
3. Determine whether planning/reasoning is required
4. Compile or refresh relevant context
5. Decompose into tasks
6. Determine dependencies
7. Detect resource conflicts
8. Build execution waves
9. Estimate context and model cost
10. Choose parallelism level
11. Dispatch agents
12. Monitor execution
13. Update state/context
14. Verify outputs
15. Recover or retry when necessary
16. Produce final result
```

---

# 12. LLM Call Budget

Every workflow should conceptually have a call budget.

Example:

### Simple task

```text
Manager → Building → Test
```

### Medium task

```text
Manager/Context → Building A + Building B → Review
```

### Complex task

```text
Planning
→ Context compilation
→ Parallel implementation
→ Integration
→ Review
→ Recovery if required
```

Do not automatically introduce Planner, Researcher, Reviewer, Debugger and other agents for every task.

Agent count must be proportional to task complexity.

---

# 13. Existing Manager Features — Keep

The existing reliability foundation should remain.

Keep:

- persistent state
- event log
- checkpointing
- watchdog/heartbeat
- idempotent recovery
- lease/lock
- baseline protection
- task manifest
- review/verification gates
- retry/recovery budget
- observability
- Scopeboard
- visualization-independent event/state interfaces

These are not replaced by the new Context/Scheduler layer.

---

# 14. visualization tooling Policy

visualization tooling should remain outside the execution core.

Correct relationship:

```text
Manager Core
     |
     +---- Execution
     +---- Context
     +---- Scheduler
     +---- Recovery
     |
     +---- Event Stream
                |
                +---- Scopeboard
                +---- visualization tooling
```

Manager must remain fully functional if visualization tooling is removed.

visualization tooling should be used for:

- workflow visualization
- dependency visualization
- agent status
- context routing inspection
- cache/version inspection
- bottleneck investigation
- debugging orchestration behavior

Do not make visualization tooling a required step in normal task execution.

---

# 15. Scopeboard Policy

Scopeboard remains the operational observability layer.

It should consume event/state data rather than become part of execution logic.

Potential future metrics:

```text
LLM calls
tokens
context tokens
cache hits
cache misses
cache invalidations
parallel workers
queue wait time
model latency
task execution time
retries
recovery count
conflicts
blocked tasks
review failures
```

This is especially important for benchmarking the free-model workflow.

---

# 16. Benchmark Requirements

Before declaring the new architecture successful, compare:

### Baseline

Current Manager:

```text
full/large context
+
current task execution
```

### Experiment A

```text
Context Compiler
+
Context Cache
```

### Experiment B

```text
A
+
DAG Scheduler
```

### Experiment C

```text
B
+
Adaptive Parallelism
```

Measure:

- total wall-clock time
- LLM call count
- input tokens
- output tokens
- context tokens
- cache hit rate
- retries
- review failures
- task correctness
- conflict count
- recovery count

Do not optimize for token count alone.

The target is:

> lower total cost and latency without reducing task correctness or reliability.

---

# 17. Failure Modes to Guard Against

## 17.1 Stale Context

Agent receives outdated information.

Mitigation:

- context version
- source hash
- invalidation
- downstream refresh

## 17.2 Over-compression

Context becomes too small and important constraints disappear.

Mitigation:

- required-context checklist
- task acceptance criteria
- context completeness validation

## 17.3 Excessive Manager Reasoning

Manager spends more LLM calls coordinating than the workers spend coding.

Mitigation:

- LLM call budget
- deterministic scheduler
- avoid unnecessary planning layers

## 17.4 Parallel Conflict

Two agents modify the same resource.

Mitigation:

- ownership
- locks
- conflict detection
- dependency serialization

## 17.5 Free-model Latency

Parallel execution creates provider contention instead of speedup.

Mitigation:

- adaptive worker count
- provider-aware scheduling
- benchmark-driven limits

## 17.6 Context Cache Complexity

Cache becomes harder to maintain than the token savings justify.

Mitigation:

- start with coarse-grained cache
- measure cache hit rate
- only add fine-grained invalidation when useful

---

# 18. What NOT to Add

Do not add these merely because another framework supports them:

- autonomous agent-to-agent conversation
- unlimited agent spawning
- multiple manager layers
- LLM-based scheduling when deterministic scheduling is sufficient
- mandatory graph visualization
- mandatory external orchestration framework
- complex distributed infrastructure
- model-specific logic in core architecture

Every new subsystem must justify its complexity through measurable improvement.

---

# 19. Research Patterns Adopted

The architecture should take inspiration from existing systems without cloning them.

### Agent orchestration projects

Useful patterns:

- shared task state
- task dependencies
- resource locking
- context synchronization

### DAG-oriented orchestration

Useful patterns:

- topological sorting
- execution waves
- parallel execution within independent waves
- dependency-aware output forwarding

### Stateful graph frameworks

Useful patterns:

- explicit state
- checkpoints
- graph transitions
- controlled loops/recovery

### Coding-agent orchestration

Useful patterns:

- supervisor/worker separation
- isolated work
- persistent sessions
- review loops
- recovery

### Model-routing systems

Useful patterns:

- task/model matching
- fallback
- provider-aware routing
- cost/latency awareness

Do not adopt a framework solely because it provides a feature.

---

# 20. Implementation Phases

## Phase 1 — Architecture only

Define:

- Context Compiler interface
- Context Cache interface
- task/dependency schema
- scheduler interface
- ownership metadata
- context versioning

No major runtime changes yet.

## Phase 2 — Context Compiler

Implement:

```text
project context
task context
relevant files
constraints
```

Measure token reduction.

## Phase 3 — Context Cache

Implement:

- reusable context
- cache lookup
- versioning
- basic invalidation

## Phase 4 — DAG Scheduler

Implement:

- dependency graph
- ready queue
- execution waves
- deterministic scheduling

## Phase 5 — Parallel Execution

Connect scheduler to existing Building/Review agents.

## Phase 6 — Adaptive Parallelism

Add:

- latency tracking
- provider capacity
- context cost
- conflict risk
- worker adaptation

## Phase 7 — Observability

Expose metrics to Scopeboard.

## Phase 8 — Benchmark and Hardening

Compare against the current architecture.

Only keep changes that demonstrate measurable benefit.

---

# 21. Acceptance Criteria

The upgrade should not be considered complete until:

- [ ] Manager can compile task-specific context
- [ ] Agents no longer need full project context by default
- [ ] Context artifacts can be reused
- [ ] Cached context has freshness/version information
- [ ] Dependency graph is explicit
- [ ] Independent tasks can execute in parallel
- [ ] Dependent tasks are automatically ordered
- [ ] Resource conflicts are detected
- [ ] Existing lock/recovery mechanisms remain compatible
- [ ] Parallelism adapts to provider/model conditions
- [ ] visualization tooling is optional
- [ ] Scopeboard remains operational
- [ ] LLM calls are measurable
- [ ] Context tokens are measurable
- [ ] Correctness is not reduced
- [ ] Recovery behavior remains reliable
- [ ] Benchmark demonstrates a meaningful improvement before the feature is retained

---

# 22. Final Architecture Principle

The Manager should not become a bigger collection of agents.

It should become a better **orchestrator of information and work**.

The key optimization loop is:

```text
Understand once
      ↓
Cache
      ↓
Route only what is needed
      ↓
Schedule deterministically
      ↓
Parallelize when safe
      ↓
Update only affected context
      ↓
Verify
      ↓
Measure
      ↓
Improve
```

The system should optimize for:

```text
Correctness
   +
Reliability
   +
Minimum sufficient context
   +
Minimum necessary LLM calls
   +
Safe parallelism
```

not for maximum agent count.

---

# 23. Decision Summary

| Area | Decision |
|---|---|
| Manager | Keep and extend |
| Context Compiler | Add |
| Context Cache | Add |
| DAG | Add |
| Deterministic Scheduler | Add |
| Adaptive Parallelism | Add |
| Resource Ownership | Add |
| Context Versioning | Add |
| Existing Recovery | Keep |
| Existing Locks/Leases | Keep |
| Scopeboard | Keep as observability layer |
| visualization tooling | Keep as optional observability/provider layer |
| External orchestration framework | Do not adopt wholesale |
| Multi-agent swarm behavior | Do not add unless a concrete use case requires it |
| More agents | Do not add by default |
| LLM-based scheduling | Avoid where deterministic code is sufficient |
| Benchmarking | Required before finalizing |

---

## End State

The intended system is not simply:

```text
Multi-Agent System
```

It is:

```text
Context-Efficient
Coding-Agent
Orchestration System
```

with:

```text
Manager
+
Context Compiler
+
Context Cache
+
Dependency Graph
+
Deterministic Scheduler
+
Adaptive Parallelism
+
Resource Ownership
+
Persistent State / Recovery
+
Verification
+
Observability
```

The architecture must remain modular so that each subsystem can be removed or replaced without requiring a rewrite of the entire Manager.

---

## Lean Parallel Update (CU-01..CU-06L, ผู้ใช้อนุมัติ)

> ส่วนเสริมท้ายไฟล์ (CU-07D, คีย์ `CU-07D:PLAN:001` ครั้งที่ 1) — เพิ่มอย่างเดียว ไม่ลบ/ไม่ย่อเนื้อหาเดิม (§1–§23 คงเดิมทุกบรรทัด)
> ที่มา: สเปก CU-01 (6 อินเทอร์เฟซ) + หลักฐานวัดจริง CU-02/CU-03/CU-04 + ทางวิกฤตลีน CU-05L + แบทช์/เวลา CU-06L
> หลักการลีน: ตัดทุกอย่างที่ได้ไม่คุ้มเสีย คงเฉพาะสิ่งที่วัดแล้วว่าคุ้ม ไม่ผูกโมเดลเฉพาะรุ่น ไม่เพิ่มเฟรมเวิร์ก/ดีเพนเดนซี

### 1. สิ่งที่ลีนลง 4 ข้อ (ทำแล้ว วัดแล้ว)

1. **เวิร์กเกอร์คอมไพล์ แมเนเจอร์ตรวจเกตอย่างเดียว**
   คอมไพล์คอนเท็กซ์ย้ายไปฝั่งเวิร์กเกอร์ (`worker_compile` เรียก `compile_task` ของ CU-02 แบบเลซี่ ไม่ก๊อปโค้ด)
   เวฟ 0 (A,B,D) คอมไพล์ขนานกันด้วยลูปเรียงไอดี แมเนเจอร์ทำแค่ `manager_validate` เรียก
   `validate_completeness` อย่างเดียว เกตเป็นแบบ events-only (คืนค่าดิบให้คอร์ตัดสินใจ ไม่เขียนอีเวนต์เองในโปรโตไทป์)

2. **ล็อกเหลือแค่ตอนดิสแพตช์ ไม่รื้อลีส**
   ไม่มีล็อกยาวรอบคอมไพล์ เหลือจุดดิสแพตช์เวฟเดียว (`dispatch_wave` เรียก `assign` ของ CU-04 ตรง)
   ลีส/เบสไลน์/รีวิวเกต/ตาราง FILE_POLICY คงเดิม ไม่แตะ ไม่เพิ่มอินฟราใหม่

3. **อีเวนต์แบทช์ช่วงลำดับเดียว สแนปช็อตทุก N=10**
   เขียนแบทช์ (`batch_append`) จองช่วงลำดับ (`sequence`) ครั้งเดียว วัดจริงได้ `seq 1-5` (5 อีเวนต์ start=1 end=5)
   สแนปช็อตห่างด้วย `snapshot_policy` คืนจริงทุก N=10 (ชุดทดสอบ 9,10,11,20 ได้ hits=[10,20]) ไม่สแนปช็อตทุกงาน ลดเขียนดิสก์

4. **แคชขี้เกียจรีเฟรชตอนใช้ + ฮาร์ตบีตแบบพุช + เมตริกแค่ 3 ตัว**
   แคชขี้เกียจ (lazy refresh): แปะ `stale=True` แล้วรีเฟรชเฉพาะสายที่ถูกใช้ (วัดจริง stale=True v1 → ใช้ → stale=False v2 refreshed=True)
   สอดคล้อง IF-6 (version/source_hash/generated_at/depends_on) และ `refresh_stale` ของ CU-03
   ฮาร์ตบีตแบบพุช: เวิร์กเกอร์ส่งเอง 3 ครั้ง แมเนเจอร์แค่นับ (`pushed=3 counted=3`) ไม่โพลล์ ไม่เพิ่มวอตช์ด็อกเธรด
   เมตริกมีแค่ 3 ตัวผ่าน `time_it`: `queue_ms / compile_ms / persist_ms` (วัดจริง `queue ~2.7ms / compile ~3.3ms / persist ~0.02ms`)
   ไม่เพิ่มอินฟราออบเซอร์วาบิลิตี คอร์แค่ปล่อยอีเวนต์เหมือนเดิม

### 2. ยืนยันขนานคงเดิม (ไม่เปลี่ยนสเปก CU-01/CU-04)

- เวฟ: `[A,B,D] → [C] → [E]` (3 ชั้น ตรงกับ CU-04/CU-05L เพราะใช้ `build_waves` ตัวเดิม ไม่เขียนใหม่)
- แบทช์ปลอดชน: `[[A,B,D]]` (เวฟ 0 แตก 1 แบทช์ ด้วย `capacity_hint=5`)
- ชน 0 คู่ (`conflicts=[]`, `reason=wave of 3 split into 1 conflict-free batches; 0 conflict pairs`)
- `check_conflict` ตรวจสองทิศ (ไฟล์เดียวกัน=True, คนละไฟล์=False)
- `capacity_hint` เคารพด้วย `min(5,len)` ไม่แตกแบทช์เกินจำเป็น
- ดีเทอร์มินิสติกเรียงไอดีเสมอ ไม่ใช้มัลติโพรเซส ไม่ใช้โมเดลตัดสินใจ (model-agnostic ตาม §2.4)

### 3. ตัวเลขหลักฐานจริง (รันจริง stdlib-only)

- ลดโทเค็น 59.1% (587 → 240): `token_full=587` (ส่ง 2 สคีมา CU-01 เต็ม) เทียบ `token_min=240` (แพ็กเกจลีน 7 ฟิลด์)
  สูตร `(587-240)/587*100` เกต `validate_completeness` ผ่าน ไม่บีบอัดเกิน (กัน §17.2)
- ฮิตเรต 0.800 อินวาลิเดต 3 โกลบอลรอด: `stats_final: hits=8 misses=2 hit_rate=0.800 invalidations=3`
  รายการที่ลาม `[context/files/manager-py, context/tasks/CU-03, context/dependencies/CU-01_to_CU-03]`
  โกลบอล `context/project/global` รอดยังฮิต v1 (ลามเฉพาะสาย incremental ไม่ล้างทั้งแคช)
- `seq 1-5` (start=1 end=5 count=5 จองครั้งเดียว)
- `queue ~2.7ms / compile ~3.3ms / persist ~0.02ms` (ค่าจริงรอบอ้างอิง `queue_ms=2.475 compile_ms=3.963 persist_ms=0.017`
  วัดด้วย `perf_counter*1000` รันซ้ำต่างเล็กน้อย แต่โครงต้องเหมือนเดิม)
- `heartbeat 3/3` (`pushed=3 counted=3`)
- `demo_ok True` (CU-05L `validate_ok=True missing={} dag=(True,ok)` + CU-06L ครบ `seq/stale/heartbeat/snapshot`)
- รีวิวผ่านทุกงาน 0 CRITICAL/HIGH/MEDIUM (คงรีวิวเกตเดิม: CRITICAL/HIGH outstanding → NOT DONE, ห้ามบายพาส)

### 4. สิ่งที่ตัดทิ้งเพราะซับซ้อน (ได้ไม่คุ้ม)

| สิ่งที่ตัด | เหตุผล: ได้ไม่คุ้ม |
|---|---|
| ล็อกแยกพาร์ติชัน | ล็อกจุดเดียวตอนดิสแพตช์พอแล้ว แยกพาร์ติชันเพิ่มโค้ดล็อก/เดดล็อกโดยไม่ลดเลเทนซีที่วัดได้ |
| รีดเรพลิกา | โปรโตไทป์ in-memory + อีเวนต์แบทช์เร็วพอ (persist ~0.02ms) เรพลิกาเพิ่มความซับซ้อนความสดโดยไม่มีหลักฐานว่าต้องใช้ |
| ออปติมิสติกโรลแบ็ก | ลีส/เช็กพอยต์/รีคัฟเวอรีเดิม (idempotent) ครอบคลุมแล้ว โรลแบ็กเพิ่มเส้นทางการกู้ที่ต้องทดสอบอีกชุด |
| เมตริกชุดใหญ่ (LLM calls/tokens/cache-misses/workers/retries/conflicts เต็ม Scopeboard §15) | ช่วงลีนใช้แค่ 3 เวลา (`queue/compile/persist ms`) พอพิสูจน์ทางวิกฤต ชุดใหญ่ค่อยเปิดตอน Observability เฟส 7 (§20) เมื่อมีเบนช์มาร์กรองรับ |

คง UTF-8 ภาษาไทยอ่านได้ วิธีตรวจซ้ำ: อ่านไฟล์ทวน + รันเดโม CU-05L/CU-06L ต้องได้โครงเดิม (`waves/batches/conflicts`, `seq 1-5`, `stale→refreshed`, `heartbeat 3/3`)
