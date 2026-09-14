# Scopeboard Upgrade Roadmap

> เป้าหมายของเอกสารนี้คือยกระดับ Scopeboard จาก **file-based observability dashboard** ไปสู่ **Agent Observatory** ที่สามารถอธิบายพฤติกรรมของ AI agent, วิเคราะห์ประสิทธิภาพและความเสี่ยง และเป็นข้อมูลให้ Manager Agent ใช้ตัดสินใจได้

---

## 1. Vision

Scopeboard ไม่ควรหยุดอยู่ที่การแสดง log หรือ chart

เป้าหมายระยะกลางคือ:

```text
Agent
  |
  | events / tool calls / results
  v
Scopeboard Collector
  |
  v
Execution Model
  |
  +---- Metrics
  +---- Execution Graph
  +---- Efficiency
  +---- Recovery / Failure
  +---- Tool / Permission Audit
  +---- Risk / Limit Signals
  |
  v
Agent Observatory
  |
  v
Manager / Automation
```

หลักสำคัญ:

- ยังคง file-based เป็นค่าเริ่มต้น
- ยังคง stdlib-only ถ้าไม่จำเป็นต้องเพิ่ม dependency
- harness-agnostic
- local-first
- ไม่ผูกกับ OpenCode หรือ coding agent รายใดรายหนึ่ง
- schema ต้อง backward-compatible เมื่อเป็นไปได้
- observability ต้องไม่กลายเป็น control layer โดยไม่ตั้งใจ

---

# 2. Upgrade Principles

## 2.1 Observe before control

Scopeboard ต้องสามารถเก็บและอธิบายสิ่งที่เกิดขึ้นได้ก่อนที่จะให้ระบบอื่นนำข้อมูลไปควบคุม agent

```text
Observe -> Understand -> Recommend -> Control
```

Scopeboard ในระยะแรกเน้นสองขั้นแรก และเปิด API/ไฟล์ contract สำหรับขั้นต่อไป

## 2.2 Evidence over inference

Dashboard ห้ามสรุปว่า agent ทำสำเร็จเพียงเพราะ session จบ

สถานะ completion ต้องอิง evidence เช่น:

- explicit TASK_DONE event
- test result
- review result
- checkpoint status
- git/worktree evidence เมื่อมี integration

## 2.3 Event history is immutable

`events.jsonl` และ raw tool-call logs เป็น historical record

การคำนวณ metric สามารถ rebuild ใหม่ได้เสมอโดยไม่แก้ historical events

## 2.4 Derived data must be rebuildable

ข้อมูลเช่น metrics, summaries, graph indexes และ dashboard JSON ต้องถือเป็น derived state

ห้ามใช้ derived state เป็น source of truth หากสามารถกลับไปอ่าน raw events ได้

---

# 3. Phase 1 — Execution Graph

**Priority: P0**

เป้าหมาย: เปลี่ยนจากการมอง event แบบแบน ๆ เป็นการมอง workflow ของ task

## 3.1 Execution entities

เพิ่ม conceptual model:

```text
Task
  |
  +-- Attempt
        |
        +-- Session
              |
              +-- Agent
                    |
                    +-- Tool Calls
                    +-- Events
                    +-- Results
```

อย่างน้อยต้องสามารถเชื่อม:

- task_id
- attempt
- session_id
- agent
- event
- tool call
- parent/related event เมื่อมีข้อมูล

## 3.2 Execution graph

Dashboard ใหม่ควรแสดงตัวอย่าง flow:

```text
TASK-42
  |
  v
PLANNER
  |
  v
BUILDER / session-1
  |
  +--> tool calls
  |
  +--> stopped_limit
  |
  v
BUILDER / session-2
  |
  v
REVIEWER
  |
  +--> REVIEW_FAILED
  |
  v
BUILDER / session-3
  |
  v
REVIEWER
  |
  +--> REVIEW_PASSED
  |
  v
TASK_DONE
```

## 3.3 Required outputs

- task timeline
- attempt timeline
- session chain
- recovery chain
- review loop
- terminal state

## 3.4 Acceptance criteria

- สามารถ trace task หนึ่ง task ตั้งแต่ creation ถึง terminal state
- สามารถเห็นว่า task ใช้กี่ session
- สามารถเห็น recovery/review loop
- ข้อมูลเดิมที่ไม่มี relationship ยังต้องแสดงผลได้
- unknown event ต้องไม่ทำให้ graph พัง

---

# 4. Phase 2 — Efficiency & Cost Intelligence

**Priority: P0**

ใช้ข้อมูลที่มีอยู่แล้ว เช่น duration, tokens, tool calls และ task/session state เพื่อสร้าง metric ที่ตอบคำถามเชิงปฏิบัติ

## 4.1 Core metrics

### Task efficiency

```text
successful_tasks / total_tasks
```

### Token efficiency

```text
tokens_used / successful_task
```

### Tool efficiency

```text
tool_calls / successful_task
```

### Recovery cost

```text
tokens_after_failure / total_tokens
```

### Session efficiency

```text
tasks_completed / sessions_used
```

### Review rework rate

```text
review_failed_attempts / total_review_attempts
```

## 4.2 Agent comparison

แสดง aggregate ต่อ agent:

```text
Agent       Success   Avg Tokens   Avg Tools   Avg Time   Recovery
builder     91%       42K          87          11m        18%
planner     98%       9K           14          2m         3%
reviewer    86%       18K          31          4m         12%
```

ตัวเลขทั้งหมดต้องเป็น derived metrics และระบุ sample size เมื่อมีจำนวนข้อมูลน้อย

## 4.3 Acceptance criteria

- metric ทุกตัวสามารถ rebuild จาก raw logs
- metric ระบุกรณีข้อมูลไม่เพียงพอ
- ห้ามสร้าง false precision จาก sample เล็ก
- cross-project aggregation ต้องใช้ metric definition เดียวกัน

---

# 5. Phase 3 — Tool Limit & Session Risk Signals

**Priority: P0**

นี่เป็น feature ที่เชื่อม Scopeboard กับ pain point ของ Manager Agent โดยตรง

## 5.1 Goal

ตรวจจับว่า worker session กำลังเข้าใกล้ข้อจำกัดหรือมี pattern ที่บ่งบอกว่าควร checkpoint/resume

## 5.2 Initial signals

ไม่ต้องใช้ ML ในเวอร์ชันแรก

ใช้ deterministic heuristics เช่น:

```text
current tool calls
historical tool calls per session
recent error rate
checkpoint age
session duration
```

ตัวอย่าง:

```text
Risk: HIGH

Tool calls: 84
Historical session median: 92
Recent errors: 4
Checkpoint age: 17 min

Recommendation:
Create checkpoint and prepare a new session.
```

## 5.3 Important distinction

Scopeboard ควรแยก:

```text
OBSERVATION
RECOMMENDATION
ACTION
```

ตัวอย่าง:

```text
OBSERVATION:
Worker has consumed 84 tool calls.

RECOMMENDATION:
Prepare session rollover.

ACTION:
Manager may create a new session.
```

Scopeboard ไม่ควรเปิด/หยุด agent เองใน phase นี้

## 5.4 Acceptance criteria

- มี risk score หรือ severity ที่อธิบายเหตุผลได้
- ไม่มี opaque prediction ที่ผู้ใช้ตรวจสอบไม่ได้
- threshold configurable
- output สามารถอ่านโดย Manager ได้

---

# 6. Phase 4 — Manager Integration Contract

**Priority: P1**

เมื่อ observability data มีความเสถียร ให้สร้าง machine-readable contract สำหรับ Manager

## 6.1 Recommended file

```text
.agent/manager/observability.json
```

ตัวอย่าง:

```json
{
  "schema_version": 1,
  "generated_at": "ISO-8601",
  "task_id": "TASK-001",
  "status": "running",
  "session": {
    "id": "ses-1",
    "tool_calls": 84,
    "duration_ms": 812000,
    "risk": "high"
  },
  "recommendations": [
    {
      "type": "SESSION_ROLLOVER",
      "priority": "high",
      "reason": "tool-call budget is near historical limit"
    }
  ]
}
```

## 6.2 Contract rules

- Scopeboard writes observations/derived recommendations
- Manager owns actions
- Manager must not rewrite historical events
- Manager decisions should be logged separately
- schema must be versioned

## 6.3 Future flow

```text
Scopeboard
    |
    v
observability.json
    |
    v
Manager
    |
    +-- continue
    +-- checkpoint
    +-- rollover
    +-- review
    +-- block
```

---

# 7. Phase 5 — Tool & Permission Audit

**Priority: P1**

พัฒนาฟีเจอร์ tool × agent ให้เป็น audit layer มากกว่า matrix ธรรมดา

## 7.1 Three states

```text
Allowed
Denied
Actually Used
```

ควรสามารถแสดงกรณีสำคัญ:

```text
allowed + used
allowed + unused
denied + attempted
```

กรณีสุดท้ายควรเป็น security/review signal

## 7.2 Audit events

หากมีข้อมูลเพียงพอ ให้รองรับ:

```text
TOOL_ALLOWED
TOOL_DENIED
TOOL_ATTEMPTED
TOOL_USED
```

ไม่จำเป็นต้องบังคับ producer ทุกตัวใน schema แรก

## 7.3 Acceptance criteria

- permission data กับ actual usage แยกจากกัน
- denied attempt ไม่ถูกนับเป็น successful tool use
- dashboard แสดง mismatch ได้ชัดเจน

---

# 8. Phase 6 — Failure & Recovery Intelligence

**Priority: P1**

เปลี่ยน failure log ให้ตอบคำถามว่า "ระบบแก้ failure อย่างไร"

## 8.1 Failure taxonomy

เริ่มจาก category ที่ deterministic:

```text
TOOL_ERROR
TEST_FAILURE
REVIEW_FAILURE
SESSION_LIMIT
TIMEOUT
BLOCKED
UNKNOWN
```

## 8.2 Recovery metrics

- failures per task
- recovery attempts per failure
- recovery success rate
- time to recovery
- tokens spent on recovery
- repeated failure patterns

## 8.3 Repeated failure detection

ตัวอย่าง:

```text
TASK-12
  REVIEW_FAILED
  BUILDING
  REVIEW_FAILED
  BUILDING
  REVIEW_FAILED

Signal:
Repeated review failure
Recommendation:
Escalate to Manager / human review
```

ไม่ควรให้ Scopeboard ตัดสินใจหยุด task เองใน phase นี้

---

# 9. Phase 7 — Observatory Dashboard

**Priority: P1**

เพิ่มมุมมองจาก dashboard แบบ chart-centric เป็น operation-centric

## 9.1 Overview

แสดง:

- active tasks
- completed tasks
- failed tasks
- blocked tasks
- active sessions
- high-risk sessions
- recovery count

## 9.2 Task view

```text
Task
Status
Duration
Sessions
Attempts
Tokens
Tools
Reviews
Recovery
```

## 9.3 Agent view

```text
Agent
Tasks
Success rate
Avg tokens
Avg tools
Avg duration
Failure rate
Recovery rate
```

## 9.4 Session view

```text
Session
Agent
Task
Started
Duration
Tool calls
Errors
Checkpoint
Terminal state
```

## 9.5 Graph view

แสดง execution graph ที่กำหนดใน Phase 1

---

# 10. Data Model Evolution

อย่ารีบเปลี่ยน JSONL schema ทั้งหมด

ให้ใช้ additive evolution ก่อน

## Existing core

```text
 time
 task
 attempt
 session_id
 agent
 operation
 tool
 duration_ms
 status
 error
 prompt_hash
 tokens_in
 tokens_out
```

## Candidate additions

```text
 event_id
 parent_event_id
 run_id
 task_type
 phase
 source
 result_ref
 checkpoint_id
 error_category
 metadata
 schema_version
```

## Rules

- field ใหม่ควร optional ในช่วง migration
- producer เก่าต้องยังอ่านได้
- consumer ต้อง tolerate unknown fields
- schema version ต้องเพิ่มเมื่อ semantics เปลี่ยน ไม่ใช่ทุกครั้งที่เพิ่ม optional field

---

# 11. Storage Strategy

**ยังไม่เปลี่ยนจาก JSONL เป็น database ใน roadmap นี้**

JSONL ยังคงเป็น canonical event store เพราะ:

- portable
- inspect ง่าย
- append-only
- dependency ต่ำ
- backup ง่าย
- เหมาะกับ local agent workflow

หากข้อมูลโตขึ้น ให้เพิ่ม index/derived store แบบ optional:

```text
Raw JSONL
   |
   +--> Metrics cache
   +--> Graph index
   +--> Dashboard JSON
```

Database จะพิจารณาเมื่อมี evidence ว่า JSONL เริ่มเป็น bottleneck จริง

---

# 12. Performance Requirements

Scopeboard ต้องไม่ทำให้ agent workflow ช้าลงอย่างมีนัยสำคัญ

## Requirements

- logging เป็น append-only
- dashboard generation แยกจาก agent execution เมื่อทำได้
- metrics rebuild ต้องสามารถทำซ้ำได้
- aggregation ต้องรองรับหลาย project
- large logs ต้องไม่ทำให้ dashboard โหลด raw events ทั้งหมดโดยไม่จำเป็น

## Future benchmark targets

ทดสอบอย่างน้อย:

```text
100K tool calls
500K tool calls
1M tool calls
10M tool calls
```

วัด:

- ingest/write cost
- metrics rebuild time
- aggregation time
- export size
- dashboard load time
- memory usage

ห้าม optimize โดยเดาจากความรู้สึก ให้ benchmark ก่อน

---

# 13. Testing Strategy

เพิ่ม test layers ตาม feature

## Unit

- event parsing
- schema validation
- metric calculation
- graph construction
- risk calculation
- aggregation

## Integration

```text
JSONL
  -> metrics
  -> export
  -> graph
  -> dashboard data
```

## Compatibility

ทดสอบ:

- old schema
- missing optional fields
- unknown event
- malformed line
- empty log
- partial session
- incomplete checkpoint

## Regression

ทุก upgrade ต้องรักษา:

- existing dashboard
- existing CLI
- demo bootstrap
- cross-project aggregation
- existing tests

---

# 14. Security & Privacy

Scopeboard เป็น local-first tool ดังนั้นต้องรักษาหลักการ:

- หลีกเลี่ยงการเก็บ raw prompts โดย default
- ใช้ `prompt_hash` หากต้อง correlate prompt
- อย่า log secrets/API keys
- dashboard ไม่ควร expose filesystem นอก project โดยไม่ตั้งใจ
- path handling ต้องป้องกัน traversal
- generated data ควรอยู่ใน project-local scope

หากเพิ่ม export/share feature ในอนาคต ต้องมี explicit redaction layer

---

# 15. What NOT to Build Yet

เพื่อป้องกัน scope creep ยังไม่ทำ:

- cloud SaaS
- authentication
- multi-user backend
- PostgreSQL
- distributed tracing infrastructure
- real-time WebSocket infrastructure
- ML-based prediction
- automatic agent control
- vendor-specific deep integration
- LLM-based analysis ใน core pipeline

สิ่งเหล่านี้อาจเกิดในอนาคต แต่ไม่ใช่ prerequisite ของ Observatory

---

# 16. Recommended Priority

```text
P0
├── Execution Graph
├── Efficiency / Cost Metrics
└── Tool-limit / Session Risk Signals

P1
├── Manager Integration Contract
├── Failure / Recovery Intelligence
├── Tool Permission Audit
└── Observatory Dashboard

P2
├── Large-scale performance optimization
├── Optional indexing layer
├── Advanced anomaly detection
└── External integrations
```

---

# 17. Definition of Done — Agent Observatory v1

Scopeboard ถือว่าเป็น **Agent Observatory v1** เมื่อสามารถตอบคำถามต่อไปนี้ได้โดยไม่ต้องเปิด raw logs เอง:

1. Agent กำลังทำ task อะไร?
2. Task นี้ผ่านกี่ attempt?
3. ใช้กี่ session?
4. Session ไหนเกิด recovery?
5. Tool ไหนถูกใช้มากที่สุด?
6. Agent ใช้ token และเวลาประมาณเท่าไร?
7. Task สำเร็จเพราะ evidence อะไร?
8. Failure เกิดตรงไหน?
9. Recovery ใช้ต้นทุนเท่าไร?
10. Session มีความเสี่ยงที่จะชน limit หรือไม่?
11. Agent ใช้ tool ที่ได้รับอนุญาตจริงหรือไม่?
12. Manager ควรพิจารณา action อะไรต่อ?

ข้อ 12 ต้องเป็น **recommendation** ไม่ใช่ automatic control ใน v1

---

# 18. Long-term Direction — Agent Operations Layer

หลัง Observatory v1 เสถียร จึงค่อยพิจารณา:

```text
                 +-------------------+
                 |       User        |
                 +---------+---------+
                           |
                           v
                 +-------------------+
                 |      Manager       |
                 +---------+---------+
                           |
                    decisions/actions
                           |
                           v
                 +-------------------+
                 |      Agents       |
                 +---------+---------+
                           |
                         events
                           |
                           v
                 +-------------------+
                 |    Scopeboard      |
                 |    Observatory     |
                 +---------+---------+
                           |
                  observations / risk
                           |
                           +-----------> Manager
```

จุดสำคัญคือ **Scopeboard เป็น observation layer ส่วน Manager เป็น control layer**

การแยกสองบทบาทนี้จะช่วยให้ทั้งสองโปรเจกต์พัฒนาแยกกันได้ และลด coupling

---

# 19. Immediate Next Steps

เริ่ม implementation ตามลำดับนี้:

### Step 1
กำหนด execution graph data model โดยไม่ทำ breaking change กับ schema เดิม

### Step 2
เพิ่ม graph builder + task/session timeline

### Step 3
เพิ่ม efficiency metrics พร้อม sample-size handling

### Step 4
เพิ่ม deterministic session-risk calculation

### Step 5
สร้าง `observability.json` contract สำหรับ Manager

### Step 6
เพิ่ม failure/recovery analytics

### Step 7
ปรับ dashboard ให้มี Overview / Task / Agent / Session / Graph views

### Step 8
ทำ benchmark 100K → 10M events ก่อนพิจารณา storage optimization

---

# 20. Guiding Principle

> **Do not make Scopeboard smarter by adding more charts. Make it smarter by making agent execution explainable.**

เป้าหมายสุดท้ายไม่ใช่การแสดงว่า agent เรียก tool ไปกี่ครั้ง

แต่คือการทำให้ผู้ใช้และ Manager สามารถตอบได้ว่า:

> **เกิดอะไรขึ้น → ทำไมจึงเกิด → ผลคืออะไร → มีความเสี่ยงอะไร → ควรทำอะไรต่อ**

เมื่อ Scopeboard ตอบคำถามเหล่านี้ได้จาก evidence บน disk โดยไม่ผูกกับ harness ใด ๆ จึงถือว่า Scopeboard ก้าวจาก dashboard ไปเป็น Agent Observatory อย่างแท้จริง.
