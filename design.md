# Manager Agent Architecture

## 1. เป้าหมาย

สร้าง **Manager Agent** สำหรับ OpenCode เพื่อเป็น orchestrator ของทีม agent โดยผู้ใช้สั่งงานกับ Manager เป็นหลัก และ Manager รับผิดชอบการควบคุม workflow, task queue, agent routing และ lifecycle ของ worker sessions

เป้าหมายสำคัญที่สุดของ version แรกคือแก้ปัญหา:

> Building agent ทำงานไปจนถึง tool-call limit แล้วหยุด ทำให้ผู้ใช้ต้องเปิด session ใหม่และสั่ง "ทำต่อ" เอง

Manager ต้องสามารถเปิด Building session ใหม่และ resume งานจาก persistent checkpoint ได้โดยอัตโนมัติ

---

## 2. Agent ที่มีอยู่

ปัจจุบันมี agent:

1. **Planning** — วางแผนและวางโครงสร้างงานลง `design.md`
2. **Building** — อ่าน `design.md` และลงมือเขียน/แก้โค้ด
3. **Generic** — งานทั่วไปหรือ task ที่ไม่เข้ากับ role เฉพาะ
4. **Review** — ตรวจ code, test, regression และ security
5. **error_debug** — ดีบั๊ก วิเคราะห์ root cause และแก้ปัญหาตรรกะซับซ้อน
6. **Manager** — orchestrator (อัปเกรดเป็น reliable orchestration layer ดู §26): รองรับ Worker Watchdog, Progress/Heartbeat, Evidence-based Completion, Idempotent Recovery, Event Log, Context Manager, Lease/Lock และ Policy/Human Approval
7. **openvisio-planner / openvisio-builder / openvisio-reviewer** — thin wrapper ต่อโปรเจกต์ (3 โรล × 2 โปรเจกต์ = 6 ไฟล์ใต้ `.opencode/agent/`) เรียกสกิลกลาง `openvisio-graph` ก่อนค้นหา/วางแผน/รีวิวเสมอ ไม่ใช่เอเจนต์ใหม่ทั้งระบบ (ดู §25)

---

## 3. หลักการสำคัญ

Manager **ไม่ควรเป็น coding agent ตัวที่สอง**

Manager มีหน้าที่:

- รับ requirement จากผู้ใช้
- วิเคราะห์ requirement
- แตก task
- จัดลำดับ task
- เลือก agent ที่เหมาะสม
- สร้างและควบคุม OpenCode sessions
- ตรวจสถานะของ worker
- ตรวจการหยุดจาก tool limit
- เปิด session ใหม่เมื่อจำเป็น
- resume งานจาก checkpoint
- จัดการ failure/retry
- ส่ง implementation ให้ Review
- วน workflow จน task ผ่าน completion criteria

Building เป็นผู้รับผิดชอบ production code โดยตรง

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

ผู้ใช้ควรคุยกับ Manager เป็นหลัก

ตัวอย่าง:

```text
ทำตาม design.md ให้เสร็จ
```

หรือ:

```text
เพิ่ม Graph Export API
```

หรือ:

```text
แก้ปัญหา search ที่ช้าเมื่อมี memory จำนวนมาก
```

ผู้ใช้ไม่จำเป็นต้องตัดสินใจเองว่าต้องเรียก Planning, error_debug, Building หรือ Review

Manager เป็นคน routing ให้

---

# 6. Workflow หลัก

## 6.1 งานใหม่จาก design.md

```text
USER
 |
 v
MANAGER
 |
 v
อ่าน design.md
 |
 v
ตรวจ current state
 |
 v
BUILDING
 |
 v
checkpoint
 |
 v
ถึง tool limit?
 |
 +-- NO --> ทำต่อ
 |
 +-- YES
       |
       v
   Manager ตรวจ checkpoint
       |
       v
   เปิด Building session ใหม่
       |
       v
   resume
       |
       v
   ทำต่อ
```

ทำซ้ำจน implementation เสร็จ

จากนั้น:

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

Review FAIL เกิน 3 รอบ → เปลี่ยนเป็น BLOCKED + แจ้งผู้ใช้ | สูงสุด 5 worker session ต่อ 1 task

---

# 7. Persistent State

ไม่ควรพึ่ง conversation context ของ Building เป็นหลัก

ทุก session ต้องสามารถเริ่มใหม่ได้จาก state บน disk

แนะนำโครงสร้าง:

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

ไฟล์ runtime ใต้ `.agent/` (state.json, queue.json, checkpoint.json, events.jsonl) ต้องอยู่ใน `.gitignore` ไม่เข้า git

หลักการ (ดู §26):

- `state.json` = current truth
- `events.jsonl` = historical truth (append-only)
- `config.json` = policy/limits (ห้าม hard-code ใน logic)
- `context/` = resume context ต่อ task
- `decisions/` = decision log ต่อ task

---

# 8. Manager State

ไฟล์:

```text
.agent/manager/state.json
```

ตัวอย่าง:

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

ควรเก็บอย่างน้อย:

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

ไฟล์:

```text
.agent/manager/queue.json
```

ตัวอย่าง:

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

Task ควรมี:

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

ไฟล์:

```text
.agent/building/checkpoint.json
```

ตัวอย่าง:

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

Building ควรอัปเดต checkpoint หลัง milestone สำคัญ และก่อนจบ sessionถ้าเป็นไปได้

---

# 11. Automatic Session Resume

เมื่อ Building session ติด tool-call limit:

### Manager ต้องทำ

1. ตรวจว่า session หยุดแล้ว
2. อ่าน `state.json`
3. อ่าน `checkpoint.json`
4. ตรวจ working tree
5. ตรวจว่ามีงานที่ทำค้างอยู่หรือไม่
6. สร้าง Building session ใหม่
7. ส่ง task context ที่จำเป็น
8. ส่ง `design.md`
9. ส่ง `checkpoint.json`
10. สั่งให้ทำต่อจาก `next_action`
11. ห้ามทำ completed work ซ้ำโดยไม่จำเป็น
12. monitor session ใหม่
13. ทำซ้ำจน task complete

### สัญญาณดีเทกต์ (status protocol)

- Building ต้องเขียน `checkpoint.json` พร้อมฟิลด์ `status` ทุกครั้งก่อนจบ session ค่าที่ใช้ได้: `running` | `stopped_limit` | `done` | `failed` | `blocked`
- Manager ถือไฟล์นี้เป็น source of truth ห้ามเดาจากความเงียบ
- ถ้าไฟล์หายหรือเก่าเกิน 1 session: กู้จาก `git diff` + ไทม์สแตมป์ไฟล์เป็นหลัก checkpoint เป็นตัวเสริม

---

# 12. Resume Prompt Contract

ทุก Building session ใหม่ควรได้รับ instruction ที่มีลักษณะดังนี้:

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

Call the `openvisio-graph` skill (`graph-skeleton` + prove เทียบเท่า) before every resume when the task touches indexed projects (see §25).
```

---

# 13. การเพิ่ม Feature ใหม่ที่ไม่มีใน design.md

ผู้ใช้ไม่ต้องรู้ว่าต้องส่งให้ agent ตัวไหน

ผู้ใช้เพียงบอก Manager:

```text
เพิ่ม Graph Export API
```

Manager วิเคราะห์ impact

## Case A — Small Change

ไม่กระทบ architecture และ design หลัก:

```text
USER
 |
 v
MANAGER
 |
 v
วิเคราะห์
 |
 v
เพิ่ม task
 |
 v
BUILDING
```

ไม่ต้องเรียก Planning ใหม่

---

## Case B — Design Change

feature ต้องเพิ่มรายละเอียดใน design แต่ไม่เปลี่ยน architecture:

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

ตัวอย่าง:

```text
เปลี่ยนจาก SQLite เป็น PostgreSQL
```

หรือ:

```text
เปลี่ยน architecture ของระบบ memory
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

Manager ไม่ควรส่ง architectural change ตรงเข้า Building

---

# 14. Change Request

แนะนำให้ Manager บันทึก feature/requirement ใหม่เป็น change request

โครงสร้าง:

```text
.agent/manager/changes/
├── CR-001.md
├── CR-002.md
└── CR-003.md
```

ตัวอย่าง:

```markdown
# CR-001 — Graph Export API

## Request

เพิ่ม API สำหรับ export memory graph เป็น JSON

## Source

User request

## Impact

Medium

## Decision

ต้อง update design.md ก่อน implementation

## Status

planning
```

ข้อดีคือสามารถ trace ได้ว่า requirement ใหม่มาจากไหน และ Manager ตัดสินใจอย่างไร

---

# 15. Agent Routing Rules

| Situation | Agent |
|---|---|
| วาง architecture | Planning |
| แก้/เพิ่ม design | Planning |
| ไม่รู้ว่า code อยู่ตรงไหน | error_debug |
| วิเคราะห์ root cause | error_debug |
| เขียน code | Building |
| แก้บั๊กทั่วไป | Building |
| แก้บั๊กตรรกะซับซ้อน | error_debug |
| ตรวจ implementation | Review |
| ตรวจ regression | Review |
| ตรวจ security | Review |
| ต้องหากราฟ/ซิมโบลก่อนลงมือ | openvisio-planner + สกิล openvisio-graph |
| ตรวจกราฟก่อนรีวิว | openvisio-reviewer (export-verify + เทียบเบสไลน์) |
| worker stuck / state UNKNOWN | error_debug (classify ก่อน resume — ห้าม blind resume) |
| HIGH risk operation | approval จากผู้ใช้ก่อน (APPROVAL_REQUIRED) |
| งานทั่วไป | Generic |
| คุม workflow | Manager (reliable orchestration layer — ดู §26) |

---

# 16. Manager Decision Rules

1. Manager ห้ามแก้ production code โดยตรงใน version แรก
2. Architectural change ต้องผ่าน Planning
3. Unknown codebase/root-cause ให้ error_debug ก่อน
4. Implementation ให้ Building
5. Meaningful implementation ต้องผ่าน Review
6. Building หยุดก่อนเสร็จ ให้ resume จาก checkpoint
7. Building ถึง tool limit ให้เปิด session ใหม่อัตโนมัติ
8. ห้ามถือว่า task เสร็จเพียงเพราะ Building บอกว่าเสร็จ
9. ต้องตรวจ test/state/review ก่อน mark DONE
10. ห้ามลบหรือ overwrite user changes ที่ไม่เกี่ยวข้อง
11. ห้าม reset git แบบไม่ตรวจสอบ
12. ต้องรักษา unfinished tasks ไว้ใน queue
13. จำกัด retry เมื่อเกิด failure ซ้ำ
14. ถ้าความต้องการคลุมเครือและมีผลต่อ implementation อย่างมีนัยสำคัญ ให้ถามผู้ใช้

---

# 17. State Machine

Manager ควรใช้ state machine ที่ชัดเจน:

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

ไม่จำเป็นต้องใช้ทุก state ใน MVP แต่ควรออกแบบให้รองรับ

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

หาก retry เกิน 3 ครั้ง (ค่าเริ่มต้น):

```text
FAILED/BLOCKED
 |
 v
Manager แจ้งผู้ใช้
```

ไม่ควรวน retry ไม่สิ้นสุด

---

# 19. Completion Criteria

Task จะถือว่า `DONE` เมื่อ (evidence-based completion gate — ดู §26):

- implementation ครบตาม requirement
- relevant tests ผ่าน (มี test result เป็น evidence)
- ไม่มี blocker ที่ยังไม่แก้
- Review ผ่าน (structured findings ไม่มี CRITICAL/HIGH ค้าง)
- checkpoint ถูก update
- manager state ถูก update
- queue เปลี่ยนเป็น completed
- git diff ถูกตรวจแล้ว (แยก worker changes กับ user changes)
- งานที่แตะโปรเจกต์ที่อินเด็กซ์แล้ว: prove เทียบเท่าผ่าน + เลขกราฟใน tolerance (ดู §25)

ข้อห้าม:

- ห้าม mark DONE จากข้อความ "Done" ของ worker เพียงอย่างเดียว — ต้องมี evidence รองรับทุกข้อ
- ห้ามนับ claim ของ worker เป็น verification

ไม่ใช้เพียงข้อความ:

```text
"Done"
```

จาก Building เป็น completion signal เพียงอย่างเดียว

---

# 20. Git Safety

Manager ต้อง:

- ตรวจ working tree ก่อนเริ่มงาน
- ไม่ reset โดยไม่จำเป็น
- ไม่ overwrite unrelated user changes
- ตรวจ diff ก่อน completion
- ไม่ commit โดยอัตโนมัติใน MVP เว้นแต่ผู้ใช้อนุญาต

Git commit/PR automation สามารถเพิ่มใน phase หลัง

---

# 21. MVP Scope

Version แรกควรโฟกัสเพียง:

```text
1. Manager รับ task
2. Manager สร้าง Building session
3. Building ทำงาน
4. Manager detect session end/tool limit
5. Manager อ่าน checkpoint
6. Manager เปิด Building session ใหม่
7. Building resume
8. ทำซ้ำจนเสร็จ
9. ส่ง Review
10. จบงาน
```

ยังไม่ควรเริ่มด้วย parallel agents, distributed execution หรือ autonomous coding หลายตัวพร้อมกัน

---

# 22. Implementation Phases

## Phase 1 — Proof of Concept

เป้าหมาย:

แก้ปัญหา Building tool limit ให้ได้ก่อน

Tasks:

1. ตรวจ OpenCode CLI/API ที่สามารถควบคุม session ได้
2. สร้าง Manager controller ขนาดเล็ก
3. สร้าง Building session
4. ส่ง task
5. monitor session
6. detect termination/tool limit
7. อ่าน checkpoint
8. สร้าง Building session ใหม่
9. resume
10. log transitions

## Phase 2 — State Machine

เพิ่ม state:

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

เพิ่ม:

- priority
- dependency
- status
- retry count
- change request

## Phase 4 — Change Requests

เพิ่ม automatic routing:

```text
minor
  -> Building

design change
  -> Planning -> Building

architecture change
  -> Planning -> Design Review -> Building -> Review
```

## Phase 5 — Reliability

เพิ่ม:

- crash recovery
- stale session detection
- retry limits
- structured logs
- cancellation
- safe recovery
- manual approval gate

## Phase 6 — Advanced

พิจารณาภายหลัง:

- parallel workers
- automatic git checkpoints
- automatic rollback
- token/tool budget tracking
- benchmark automation
- automatic commit
- PR creation
- Discord remote control
- scheduled/background execution

## Phase U1-U7 — Manager Upgrade (ดู §26)

ลำดับอิมพลีเมนต์ reliable orchestration layer:

```text
U1 Watchdog          — heartbeat, progress timestamp, stale detection, recovery trigger
U2 Recovery          — idempotency, active-session discovery, duplicate prevention, restart recovery
U3 Event System      — events.jsonl, lifecycle events, structured logging, replay
U4 Context Manager   — resume context generator, compaction, validation, resume prompt
U5 Policy            — configurable limits, risk classification, approval gate, destructive protection
U6 Queue Intelligence— dependency resolver, ready-state, blocked dependency, scheduling
U7 Decision Trace    — decision logs, routing/recovery explanation, final summary
```

Priority: P0 (U1-U3) ต้องเสถียรก่อน → P1 (U4-U7) → P2 (parallel/automation) ห้ามเริ่มก่อน P0/P1 เสถียร

---

# 23. Example End-to-End

User:

```text
เพิ่ม Graph Export API
```

Manager:

```text
1. วิเคราะห์ requirement
2. ตรวจ design.md
3. ตรวจ impact
4. พบว่าเป็น medium change
5. เรียก Planning
6. Planning update design.md
7. สร้าง TASK-001
8. ส่ง Building
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
1. อ่าน checkpoint
2. ตรวจ git diff
3. สร้าง Building Session #2
4. ส่ง checkpoint
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

ผู้ใช้ไม่ต้องเปิด session ใหม่เองเลย

---

# 24. หลักคิดของระบบ

ระบบนี้ควรถือว่า:

```text
Conversation = temporary
State = persistent
Agent session = disposable
Task = persistent
Checkpoint = recovery mechanism
Manager = orchestrator
```

ดังนั้นการหมด context/tool limit ของ Building ไม่ควรหมายถึงงานหยุด

มันควรหมายถึง:

```text
worker session จบ
        |
        v
manager recover
        |
        v
worker session ใหม่
        |
        v
งานเดิมดำเนินต่อ
```

นี่คือหลักสำคัญที่สุดของ Manager Architecture นี้

---

# 25. OpenVisio Skill + E2E (TASK-OV-A/B/C/D/E/F/G/H)

## สกิลกลาง

- ชื่อ: `openvisio-graph` (openvisio 0.3.1, เอกสารตรง CLI จริงหลัง TASK-OV-C, เป็นกลางไม่ผูกโปรเจกต์หลัง TASK-OV-G)
- ที่อยู่เดียว (global, ไม่มี mirror ตั้งแต่ TASK-OV-H): `~/.config/opencode/skills/openvisio-graph/SKILL.md` — ทุกโปรเจกต์เรียกผ่านชื่อสกิลได้เลย
- วิธีใช้: `skeleton [path]` ก่อนอ่านโค้ด → `find_symbol` ผ่าน MCP → implement → prove เทียบเท่า (export ซ้ำเทียบตัวเลข) → `export-verify`
- ของไม่มีจริง (ห้ามใช้): `lookup / prove / watch-check`, `--json`, `--project`, `-o`, `--version` (ดูเวอร์ชันจาก package.json)
- กฎเหล็ก: ทุกคำสั่งใส่ path ชัด ห้ามรันเปล่าในโฟลเดอร์งาน (bare จะทริกเกอร์ init+index)

## เมทริกซ์ 6 ไฟล์เอเจนต์

| โปรเจกต์ | planner | builder | reviewer |
|---|---|---|---|
| Dashboard (Python/FastAPI) | `.opencode/agent/openvisio-planner.md` | `.opencode/agent/openvisio-builder.md` | `.opencode/agent/openvisio-reviewer.md` (+pytest) |
| mcp (TS/Node) | `.opencode/agent/openvisio-planner.md` | `.opencode/agent/openvisio-builder.md` | `.opencode/agent/openvisio-reviewer.md` (+npm test) |

ทุกไฟล์ประกาศ `skills: [openvisio-graph]` และอ้างเบสไลน์ของโปรเจกต์ตัวเอง

## เบสไลน์กราฟ (E2E tolerance: files ±3, symbols/edges ±5)

| โปรเจกต์ | files | symbols | edges |
|---|---|---|---|
| Dashboard | 69 | 227 | 169 |
| mcp | 140 | 431 | 433 |

## คำสั่ง E2E

```text
powershell -ExecutionPolicy Bypass -File "D:\Coding_Project\Agent\scripts\e2e-openvisio.ps1"
```

ผลลัพธ์: `D:\Coding_Project\Agent\.agent\building\e2e-ov-b.json` (ต้อง passed 5/5: skeleton 2 โปรเจกต์ + export/prove เทียบเท่า + watch freshness + token-proxy)

---

# 26. Manager Upgrade Specification (Reliable Orchestration Layer)

> ที่มา: สเปกอัปเกรดถูกผสานเข้า design.md ครบแล้ว (TASK-UP-0) ไฟล์ `upgrade.md`
> ต้นฉบับถูกลบออกเมื่อ 2026-09-14 — เอกสารนี้คือ single source of truth ฉบับเดียว
> (สเปกต้นฉบับยังเก็บไว้ที่ `.agent/manager/upgrade-spec.md` ใต้ runtime ซึ่งไม่เข้า git)

Manager ในฐานะ reliable orchestration layer รองรับ 8 ความสามารถหลักและโครงสร้างสนับสนุน:

## สถาปัตยกรรมเป้าหมาย (Target Architecture)
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

## สถานะรันไทม์ที่คงอยู่ (Persistent Runtime State)
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

## ความสามารถหลัก 8 ประการ

1. **Worker Watchdog**: ตรวจสอบสุขภาพ worker, heartbeat, progress timestamp, stale detection (heartbeat / progress / checkpoint timeout) และกระตุ้น recovery เมื่อ worker ค้างหรือตาย
2. **Progress / Heartbeat Protocol**: บันทึก milestone, timestamp, evidence และ status lifecycle (`running`, `stopped_limit`, `done`, `failed`, `blocked`)
3. **Evidence-Based Completion**: ห้ามเชื่อข้อความ "Done" ของ worker ลอยๆ ต้องผ่าน Evidence Check (changed files, git diff, relevant tests, test results, checkpoint, verification)
4. **Idempotent Recovery**: รองรับ retry และ restart โดยป้องกัน duplicate worker (ใช้ operation ID / task ID + attempt) และกู้คืนจาก checkpoint / working tree / active session
5. **Event Log**: บันทึกประวัติแบบ append-only ไว้ที่ `.agent/manager/events.jsonl` แยกจาก state.json
6. **Context Manager**: สร้างไฟล์ resume context เฉพาะกิจ (`.agent/manager/context/<task_id>.resume.md`) เพื่อส่งเฉพาะข้อมูลที่จำเป็นไปยัง Building session ใหม่ ลด token noise
7. **Lease / Lock**: ป้องกัน concurrent controller ด้วย lease บน state.json (`owner`, `acquired_at`, `expires_at`)
8. **Policy / Budget / Human Approval**: ควบคุมขีดจำกัด (max sessions, review cycles, runtime), จำแนกความเสี่ยง (LOW, MEDIUM, HIGH) และบังคับ Human Approval Gate สำหรับ High-risk operations (เช่น destructive changes, database/architecture changes)

## โครงสร้างสนับสนุนเพิ่มเติม
- Task Queue แบบรองรับ dependencies จริง (`queue.json` schema v2)
- Manager Decision Log บันทึกเหตุผลการตัดสินใจ Routing / Recovery / Review failures (`.agent/manager/decisions/`)
- Configuration แยกต่างหาก (`.agent/manager/config.json`)

## หลักการออกแบบสำคัญ
- **Principle 1 — Worker is disposable**: Worker session สามารถถูกทิ้งและสร้างใหม่ได้
- **Principle 2 — State is the source of truth**: อย่าพึ่ง conversation
- **Principle 3 — Evidence over claims**: Agent บอกว่าเสร็จ ≠ งานเสร็จ
- **Principle 4 — Recovery over restart**: การ restart ต้องเป็น recovery ที่มี context ไม่ใช่แค่เปิด session ใหม่
- **Principle 5 — Safe failure**: เมื่อไม่แน่ใจ ให้ BLOCKED ดีกว่าทำต่อแบบเดา
- **Principle 6 — No infinite automation**: ทุก retry และ loop ต้องมี limit
- **Principle 7 — Preserve user work**: Manager ต้องไม่ทำลายงานของ user เพื่อแก้ปัญหาของตัวเอง
- **Principle 8 — Reliability before intelligence**: Manager ที่ตัดสินใจธรรมดาแต่ recover ได้ดีกว่า Manager ที่ฉลาดแต่ state พัง

## นิยามความสำเร็จ (Success Definition)
Manager Upgrade จะถือว่าประสบความสำเร็จเมื่อ:
> **ผู้ใช้สามารถมอบหมาย task ระยะยาวให้ Manager แล้ว worker session สามารถตาย, ถูกจำกัด tool-call, crash หรือถูกเปลี่ยน session ได้ โดยงานยังสามารถดำเนินต่อจาก persistent state ได้อย่างปลอดภัย และ Manager ไม่ประกาศความสำเร็จจนกว่าจะมี evidence และ verification รองรับ**

## 26.1 Watchdog Data + Stale Detection

Manager ติดตามต่อ task:

```json
{
  "last_output_at": "ISO-8601",
  "last_progress_at": "ISO-8601",
  "last_checkpoint_at": "ISO-8601",
  "heartbeat_at": "ISO-8601",
  "stuck_count": 0
}
```

Timeout ทั้งหมดต้องมาจาก `config.json` ห้าม hard-code: `heartbeat_timeout_seconds`, `progress_timeout_seconds`, `checkpoint_timeout_seconds`, `worker_start_timeout_seconds`

> Session ที่ยังเปิดอยู่แต่ไม่มี progress ถือเป็น failure condition ได้

## 26.2 Progress / Heartbeat Protocol

Worker รายงาน lifecycle: `STARTED | PROGRESS | CHECKPOINT | BLOCKED | DONE | FAILED`

- `status` = lifecycle (`running | stopped_limit | done | failed | blocked`)
- `progress` = health (milestone + timestamp + evidence ไม่ใช้ percentage เป็น source of truth)

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

Manager ตรวจก่อน mark DONE:

```text
implementation complete? tests pass? blockers empty? review pass?
checkpoint updated? manager state updated? queue state updated?
```

Evidence ขั้นต่ำ: changed files, git diff, relevant tests + test result, checkpoint, requirement coverage, review result สำหรับ indexed project ต้องผ่าน OpenVisio verification (ดู §25)

## 26.4 Idempotent Recovery

- Idempotency key: `task_id:operation:attempt` (เช่น `TASK-001:BUILD:003`)
- ก่อนสร้าง session ใหม่ต้อง discover active session ก่อน: พบ → recover existing, ไม่พบ → create new
- ห้าม blindly create duplicate session หลัง Manager restart
- ทุก lifecycle operation ต้อง retry ได้โดยไม่สร้างผลซ้ำ

## 26.5 Event Log

ไฟล์ `.agent/manager/events.jsonl` append-only:

```json
{"event":"TASK_CREATED","task":"TASK-001","time":"..."}
{"event":"WORKER_STARTED","task":"TASK-001","session":"..."}
{"event":"PROGRESS","task":"TASK-001","milestone":"repository"}
{"event":"WORKER_LIMIT","task":"TASK-001","session":"..."}
{"event":"WORKER_RESUMED","task":"TASK-001","session":"..."}
{"event":"REVIEW_FAILED","task":"TASK-001"}
{"event":"TASK_DONE","task":"TASK-001"}
```

Event ที่ต้องมี: task created/queued/started, worker created/stopped/limit, checkpoint, recovery, resume, review started/failed/passed, blocked, failed, cancelled, done — ทุก event มี timestamp + task ID

## 26.6 Context Manager

สร้าง `.agent/manager/context/TASK-xxx.resume.md` แทนการส่ง conversation ทั้งหมด:

```markdown
# Resume Context
Task: / Current phase: / Completed: / Current: / Remaining:
Important decisions: / Files changed: / Known issues:
Next action: / Do not redo:
```

Priority การส่ง context: Resume Context > Checkpoint > Current State > Relevant design section > Working tree > Relevant files ห้ามส่ง conversation/log/source ทั้ง repo โดยไม่จำเป็น

## 26.7 Lease / Lock

```json
{ "lock": { "owner": "manager-01", "acquired_at": "ISO-8601", "expires_at": "ISO-8601" } }
```

ใช้ lease แทน permanent lock — lease expired → Manager ใหม่ recover ได้ ป้องกัน: task ถูก execute สองครั้ง, worker ถูกควบคุมโดย Manager สอง instance, duplicate recovery

## 26.8 Policy / Budget / Human Approval

```json
{
  "limits": { "max_worker_sessions": 5, "max_review_cycles": 3, "max_retries": 3, "max_runtime_minutes": 120 },
  "watchdog": { "heartbeat_timeout_seconds": 120, "progress_timeout_seconds": 600, "checkpoint_timeout_seconds": 900 },
  "approval": { "require_for_destructive": true, "require_for_architecture_change": true },
  "git": { "auto_commit": false }
}
```

Risk: LOW → automatic, MEDIUM → ตาม policy, HIGH → APPROVAL_REQUIRED (เช่น เปลี่ยน database, ลบ API, เปลี่ยน auth, ลบไฟล์จำนวนมาก, major dependency upgrade, git reset, destructive operation) Manager ห้าม bypass approval gate

## 26.9 Dependency-Aware Queue

Queue states: `pending | ready | running | blocked | failed | completed | cancelled` — Manager ตรวจ `dependency complete?` ก่อน dispatch task ที่ dependency ยังไม่ผ่านต้องไม่ถูก dispatch

## 26.10 Enhanced State Machine

```text
IDLE → PLANNING → ERROR_DEBUG → BUILDING
BUILDING → LIMIT/STUCK → RECOVERING → BUILDING
BUILDING → REVIEWING → FAIL → BUILDING
REVIEWING → PASS → VERIFYING → DONE | BLOCKED
Additional: FAILED, CANCELLED, APPROVAL_REQUIRED, RECOVERING
```

`RECOVERING` แยก recovery process ออกจาก normal building

## 26.11 Recovery Protocol

```text
WORKER STOP → MANAGER DETECT → ACQUIRE LOCK → READ STATE → READ CHECKPOINT
→ INSPECT WORKING TREE → INSPECT ACTIVE SESSION → INSPECT EVENTS → CLASSIFY
```

Classification: `LIMIT | CRASH | STUCK | FAILED | DONE | BLOCKED | UNKNOWN`
- LIMIT/CRASH/STUCK → RECOVER → NEW SESSION → RESUME
- UNKNOWN → ERROR_DEBUG → CLASSIFY (ห้าม blind resume)

Manager ห้าม: blind git reset/checkout/delete/overwrite, blind retry forever, blind create duplicate session, blind mark DONE ก่อน destructive operation ต้องผ่าน policy ก่อน recovery ต้องรัน `git status` + `git diff` เพื่อแยก worker changes กับ user changes — ห้าม assume ว่าทุก diff เป็นของ worker

## 26.12 Review Loop + Observability

Review ต้อง produce structured findings:

```json
{ "status": "failed", "findings": [ { "severity": "high", "file": "src/example.ts", "issue": "missing validation", "required_action": "add input validation" } ] }
```

Manager ใช้ findings เป็น input ให้ Building รอบถัดไป

Log levels: `INFO | WARN | ERROR | RECOVERY | SECURITY` — ทุก log มี timestamp, task_id, agent, session_id, event, message เป้าหมาย: reconstruct เหตุการณ์ย้อนหลังได้

## 26.13 Decision Log

ไฟล์ `.agent/manager/decisions/TASK-xxx.md` บันทึก: Routing (selected + reason), Recovery (decision + ที่มา), Review Failure (finding + decision), Final — เป้าหมาย: ย้อนดูได้ว่า Manager ตัดสินใจอะไรและเพราะอะไร

## 26.14 Acceptance Criteria (ย่อ)

| หมวด | เกณฑ์ |
|---|---|
| Recovery | detect limit/stop/stuck, recover จาก checkpoint, ไม่ duplicate, resume ได้, recover หลัง restart |
| State | state/checkpoint/event log persistent, lease ทำงาน, transition ถูกต้อง |
| Verification | ไม่ mark DONE จาก claim เดียว, ตรวจ tests/diff/review/blockers, indexed ผ่าน verification |
| Reliability | retry จำกัด, ไม่มี infinite loop, preserve user changes, recovery idempotent |
| Context | สร้าง+ใช้ resume context, ไม่พึ่ง conversation เก่า |
| Safety | destructive ผ่าน policy, architecture ผ่าน approval, ไม่ auto git reset/commit |

Test scenarios ต้องมี: T01 normal, T02 tool limit, T03 multiple limits, T04 crash, T05 stuck, T06 review fail, T07 repeated failure, T08 duplicate protection, T09 user changes, T10 approval

## 26.15 Tool-call Logging + Metrics + Dashboard (TASK-LOG-001 → 002 → 003)

ชั้น observability แบบ evidence-based ครอบ tool call / dispatch / result ของ worker
โดยไม่แตะ logic เดิมของ `manager.py` (เพิ่มเฉพาะฟังก์ชันใหม่ + mirror event คู่กัน)

### Schema `tool-calls.jsonl` (append-only, 1 บรรทัด = 1 record, UTF-8)

ไฟล์: `.agent/manager/tool-calls.jsonl` (runtime — อยู่ใน `.gitignore` ไม่เข้า git)

| ฟิลด์ | ชนิด | หมายเหตุ |
|---|---|---|
| `time` | string ISO-8601 UTC | เวลาเกิด event |
| `task` | string | task id (เช่น TASK-LOG-001) |
| `attempt` | integer | session attempt (สอดคล้อง `state.json` attempt) |
| `session_id` | string | worker session id |
| `agent` | enum | `planning` \| `building` \| `review` \| `error_debug` \| `manager` |
| `operation` | enum | `DISPATCH` \| `CALL` \| `RESULT` |
| `tool` | string | ชื่อ tool (เช่น read/edit/test) — ว่างได้สำหรับ DISPATCH |
| `duration_ms` | integer | เวลาที่ใช้ (RESULT บังคับ, CALL/DISPATCH ให้ 0) |
| `status` | enum | `ok` \| `error` \| `blocked` \| `limit` |
| `error` | string/`null` | ข้อความ error (ไม่มี = `null`) |
| `prompt_hash` | string | sha256 ของ prompt 12 หลักแรก ห้ามเก็บ secret/prompt เต็ม |

### 6 Metrics + สูตร (`scripts/metrics.py --rebuild`)

อ่าน `events.jsonl` + `tool-calls.jsonl` แล้วคำนวณต่อ task (รันซ้ำได้ผลเท่าเดิม = idempotent):

| Metric | สูตร |
|---|---|
| `sessions_per_task` | นับ `WORKER_STARTED` / `WORKER_RESUMED` ต่อ task |
| `tool_calls_per_task` | นับแถว `tool-calls.jsonl` ต่อ task |
| `recovery_count` | นับ `RECOVERY_STARTED` ต่อ task |
| `review_loop` | นับ `REVIEW_FAILED` ต่อ task — เกิน `max_review_cycles=3` ใน `config.json` → `BLOCKED` |
| `time_to_DONE` | `TASK_DONE.time − TASK_CREATED.time` (วินาที, ไม่มี = `null`) |
| `success_rate` | `DONE / (DONE + failed + cancelled)` ระดับภาพรวม (0–1) |

### Interception points 3 ฟังก์ชัน (`scripts/manager.py` — เพิ่มเท่านั้น)

1. `log_tool_call(...)` — เขียน 1 record ลง `tool-calls.jsonl` **พร้อม**
   `log_event("TOOL_CALL", ...)` คู่กันใน `events.jsonl` (mirror 2 ไฟล์ทุกครั้ง)
2. `log_dispatch(task_id, agent, session_id, attempt)` — บันทึก `operation=DISPATCH`
   ตอน Manager ส่งงานให้ worker ทุกครั้ง **พร้อม** `WORKER_STARTED` คู่กัน
3. `wrap_task(task_id, agent, session_id, tool, prompt_text, fn, ...)` — wrapper จับเวลา
   `duration_ms` + คำนวณ `prompt_hash` (sha256 12 หลักแรก) + กำหนด
   `status` (`ok`/`error`/`blocked`/`limit`) + `error` แล้วเรียก `log_tool_call()` ตอนจบ

### วิธีรัน

```text
python scripts/metrics.py --rebuild        # คำนวณ 6 metrics → .agent/manager/metrics.json
python scripts/export_json.py              # export → dashboard/data.json
```

เปิดแดชบอร์ด: ดับเบิลคลิก `dashboard/index.html` หรือ
`python -m http.server` แล้วเปิด `dashboard/index.html`
(กราฟ 4 ชุด: bar tool calls, line timeline, pie success_rate, bar sessions/recovery/review)

กดทีเดียวจบ: ดับเบิลคลิก `run_dashboard.bat` (repo root) — รัน rebuild +
export + aggregate ข้ามโปรเจกต์ + เสิร์ฟ `http://localhost:8080/dashboard/all.html`
(จอรวม Agent+mcp+Dashboard) + เปิดเบราว์เซอร์ให้เอง
(ใช้ `http.server` เพราะเปิดผ่าน `file://` แล้ว `fetch(data.json)` โดนบล็อก)

รันเทสต์:

```text
python -m unittest discover -s scripts -p test_logging.py -v   # 14 ข้อ (a-k + f2)
python -m unittest discover -s scripts -p test_upgrade.py -v   # U1-U7 เดิม 7/7
```

เทสต์ใน `scripts/test_logging.py` ใช้ tmp dir / task id `TEST-*` แล้วลบข้อมูลทดสอบทิ้งเอง
ห้ามเขียนทับ `queue.json` / `checkpoint.json` / event จริง (ห้ามเหลือ `TEST-*` ในไฟล์จริง)

### ไฟล์ runtime ใน `.gitignore`

`.agent/` (รวม `tool-calls.jsonl`, `events.jsonl`, `metrics.json`, `state.json`,
`queue.json`, `checkpoint.json`) และ `.openvisio/` ไม่เข้า git

### Rotation guard 1MB

เมื่อ `tool-calls.jsonl` เกิน 1MB (`1048576` bytes) จะเขียน event `TOOLCALLS_GROWING`
เตือนให้ทำ rotation (เช่น แยกไฟล์ราย task) — ไม่ลบข้อมูลอัตโนมัติ

### นโยบายไม่เก็บ secret

เก็บเฉพาะ `prompt_hash` (12 หลักแรกของ sha256) ห้ามเก็บ prompt เต็ม ห้ามเก็บ secret
(api key / password / token) ลง log เด็ดขาด — ตรวจย้อนหลังได้จาก `prompt_hash`
โดยไม่มีข้อมูลอ่อนไหวหลุดลง disk

### ส่วนขยาย observability (เฟส 2 — ทั้ง 9 หัวข้อ)

1. **ฟีดกิจกรรม + errors**: `export_json.build_extra()` ดึง 20 รายการล่าสุดจาก
   `events.jsonl` และแถว `status=error` จาก `tool-calls.jsonl` (ตัดข้อความ 300 ตัวอักษร)
2. **สุขภาพคิว**: อ่าน `queue.json` นับ `by_status` + ลิสต์ id ที่ `blocked/failed`
3. **เวลา**: สถิติ `duration_ms` ต่อทูล (avg/P95, top 10) จาก tool-calls
4. **แยกตามเอเจนต์**: sessions (จาก WORKER_STARTED/WORKER_RESUMED) + calls/tokens/errors ต่อ agent
5. **Tokens**: `log_tool_call(..., tokens_in, tokens_out)` (สคีมา 13 ฟิลด์) รวมต่อ task/รวม
   — ผู้เรียกกรอกเองเมื่อทราบ (เช่น จาก API response) ไม่มีค่า default
6. **ไฟล์ที่แตะบ่อย**: รวม `files_changed` ใน building checkpoint ข้ามโปรเจกต์ (top 10)
7. **ผลรีวิว**: รีวิวต้องเรียก `save_review(task_id, status, findings)` ทุกครั้ง
   เขียน `reviews/<task>.json` (`severity: critical|major|minor`) แดชบอร์ดนับตาม severity
8. **Alerts**: `STUCK` (ไม่มีอีเวนต์เกิน 6 ชม. งานยังไม่จบ), `REVIEW_AT_RISK`
   (review_loop ใกล้ `max_review_cycles`), `QUEUE_BLOCKED/FAILED`
9. **เทรนด์**: `metrics.py --rebuild` เขียน snapshot `history/YYYY-MM-DD.json`
   (เก็บ 90 วัน) แดชบอร์ดวาดเส้น done/tool_calls รายวัน (รวม + แยกโปรเจกต์)
10. **จอรวม**: `aggregate.py` รวมทุกหัวข้อข้ามโปรเจกต์ (default สแกนโฟลเดอร์พี่น้อง
    ที่มี `.agent/manager/metrics.json` อัตโนมัติ ปรับได้ด้วย `--projects`) → `dashboard/all-projects.json`
    ดูที่ `dashboard/all.html` — โปรเจกต์ที่ยังไม่ rebuild จะขึ้น SKIP ไม่พังทั้งจอ
11. **Tool inventory ละเอียด** (`scripts/tools_inventory.py`): catalog ทูล 24 ตัว
    (file/exec/search/orchestration/web/memory/graph) + อ่าน `permission:` จากนิยาม
    เอเจนต์ทั้ง 6 ตัว + ผสานยอดเรียกจริงจาก `tool-calls.jsonl` ได้เมทริกซ์
    "ทูล × เอเจนต์ (อนุญาต/ห้าม/เรียกจริง/พัง/ใช้ล่าสุด)" — ทูลนอก catalog ขึ้นหมวด
    `observed` ให้เอง หลักการ: **ทูลมากับ harness ไม่ได้มากับโมเดล**
    (โมเดลเป็น engine) สิ่งที่ต่างกันคือสิทธิ์รายเอเจนต์ ดูได้ที่ตาราง
    tbl-tools/tbl-perm ทั้งสองจอ

