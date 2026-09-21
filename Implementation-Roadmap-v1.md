หลักการของ roadmap นี้คือ ไม่สร้างทุกอย่างพร้อมกัน เพราะจะทำให้ Manager ซับซ้อนก่อนที่จะมี feedback จากการใช้งานจริง

โครงสร้างแบ่งเป็น Phase ดังนี้:

# Implementation-Roadmap-v1.md

# Manager Agent Implementation Roadmap v1


## Goal

นำ New Architecture v1 ไปพัฒนาแบบ incremental

หลักการ:

- ทำ Core ที่จำเป็นก่อน
- วัดผลก่อนเพิ่ม complexity
- ทุก feature ต้องมี measurable benefit


---

# Phase 0: Baseline Measurement

## Objective

สร้าง baseline ก่อน upgrade

ต้องรู้ว่า architecture เดิมมี performance เท่าไร


## Metrics

เก็บ:

### LLM

- total calls
- input tokens
- output tokens
- total tokens
- latency


### Execution

- total time
- success rate
- retry count
- failure type


Output:


baseline-report.json



---

# Phase 1: Context System

Priority: CRITICAL


## Goal

ลดการอ่าน project ซ้ำของ agent


Implement:


Project
|
Context Scanner
|
Context Index
|
Context Cache



Features:

- project structure scan
- file metadata
- module relationship
- important files
- coding rules


ไม่ทำ:

- AI summarization ซับซ้อน
- automatic learning


Success Metric:


ลด context tokens >= 30%



---

# Phase 2: Task Contract

Priority: CRITICAL


## Goal

เปลี่ยนจาก instruction แบบ free-form เป็น structured task


เพิ่ม:


Task ID

Goal

Input

Output

Files

Dependencies

Acceptance Criteria

Verification



Success Metric:

- ลด task misunderstanding
- ลด retry


---

# Phase 3: Complexity Router

Priority: HIGH


## Goal

ไม่ให้ทุก task ผ่าน workflow ใหญ่


สร้าง classifier:


Input:


Task
Context
Changed Files



Output:


LOW
MEDIUM
HIGH



Initial version:

ใช้ rule-based ก่อน


ตัวอย่าง:


LOW:

- < 3 files
- no architecture impact


HIGH:

- database
- core module
- dependency change


Success Metric:


ลด unnecessary planning time



---

# Phase 4: Plan Validator

Priority: HIGH


## Goal

ตรวจ plan ก่อน execute


Implement:


Dependency check


A -> B -> C



Conflict check


Task A owns file X
Task B owns file X



Completeness check


No verification
=> reject



ไม่ใช้ LLM


Success Metric:


ลด failed execution จาก bad planning



---

# Phase 5: Scheduler + DAG

Priority: HIGH


## Goal

รองรับ parallel execution


Implement:



Task Graph

A
|
B C
|
D



Features:

- dependency ordering
- execution waves
- worker assignment
- lock


Success Metric:

Compare:


Sequential:


A+B+C+D



Parallel:


A
|
B+C
|
D



---

# Phase 6: Verification System

Priority: CRITICAL


## Goal

เปลี่ยนจาก agent claim เป็น evidence


ตรวจ:

- tests
- build
- files changed
- lint
- acceptance criteria


Output:


verification-report.json



Success Metric:

ลด false completion


---

# Phase 7: Recovery System

Priority: MEDIUM


## Goal

ทำให้ระบบ recover ได้


Implement:

- checkpoint
- retry policy
- failure classification


Failure:



TOKEN_LIMIT

MODEL_ERROR

CODE_ERROR

TEST_FAIL

CONFLICT



---

# Phase 8: Adaptive Parallelism

Priority: MEDIUM


## Goal

เลือกจำนวน agent ตามสถานการณ์


Input:


Task count

Dependency

Model speed

Token budget

Conflict risk



Output:


1 worker

or

3 workers

or

5 workers



---

# Phase 9: Feedback Loop

Priority: LOW


## Goal

เพิ่ม intelligence จาก history


เก็บ:


Task

Plan

Execution

Result

Failure



ใช้เพื่อ:

- improve planning
- detect pattern
- improve context


ไม่ทำ:

- training model
- autonomous evolution


---

# Phase Order Summary



Phase 0
Baseline

|
v

Phase 1
Context System

|
v

Phase 2
Task Contract

|
v

Phase 3
Complexity Router

|
v

Phase 4
Validator

|
v

Phase 5
Scheduler

|
v

Phase 6
Verification

|
v

Phase 7
Recovery

|
v

Phase 8
Adaptive Parallel

|
v

Phase 9
Feedback Loop



---

# MVP Target


Version:


Manager Architecture v1.0



ต้องมี:

YES:

- Context Cache
- Task Contract
- Validator
- Scheduler
- Verification
- Metrics


NO:

- Multi Manager
- Swarm Intelligence
- Auto Learning
- Complex AI Critic
- Visualization dependency


---

# Success Criteria


Architecture ถือว่าสำเร็จเมื่อ:


## Efficiency


ลด LLM tokens/task



## Reliability


ลด failed execution



## Speed


เวลารวมลดลง



## Maintainability


ระบบไม่ซับซ้อนเกินทีมพัฒนา



---

# Final Principle


Build the smallest system that proves the architecture.

Do not build the perfect architecture before collecting data.

ข้อสังเกตจากแผนนี้: ผมตั้งใจให้ Phase 1-6 เป็นแกน v1 จริง เพราะเป็นส่วนที่มีผลกับปัญหาหลักของคุณโดยตรง:

token เยอะ → Context System
manager ผิดพลาด → Task Contract + Validator
parallel ไม่มีประสิทธิภาพ → Scheduler
agent บอกเสร็จแต่พัง → Verification

ส่วน Phase 7-9 เป็นสิ่งที่เพิ่มภายหลังจากมีข้อมูลจริง