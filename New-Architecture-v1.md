# New-Architecture-v1.md

# Manager Agent New Architecture v1

## Vision

เป้าหมายของ architecture นี้ไม่ใช่การสร้าง multi-agent system ที่มี agent เยอะที่สุด

แต่คือ:

> ใช้ LLM reasoning เฉพาะจุดที่จำเป็น และย้ายงานที่เป็น deterministic logic ไปให้ระบบจัดการแทน

เป้าหมายหลัก:

- ลด LLM token usage
- ลด LLM calls ที่ไม่จำเป็น
- เพิ่ม correctness
- เพิ่ม reliability
- รองรับ parallel execution อย่างปลอดภัย
- สามารถวัดผลและปรับปรุงได้จากข้อมูลจริง


---

# 1. Core Architecture
                     USER REQUEST
                          |
                          v
                     MANAGER
                          |
          +---------------+---------------+
          |                               |
          v                               v
   CONTEXT SYSTEM                  TASK ANALYSIS
          |
          v
  Context Compiler
          |
          v
    Context Cache
          |
          v
    Knowledge State


                     MANAGER
                        |
                        v
                Task Contract
                        |
                        v
              Complexity Router
                        |
      +-----------------+-----------------+
      |                 |                 |
      v                 v                 v
     LOW             MEDIUM             HIGH

      |                 |                 |
      v                 v                 v

  Fast Path       Validator Path    Full Validation


                        |
                        v

                   PLAN VALIDATOR

                        |
         +--------------+--------------+
         |                             |
        FAIL                          PASS
         |                             |
         v                             v

    Re-plan Manager              SCHEDULER


                                     |
                       +-------------+-------------+
                       |             |             |
                       v             v             v

                   Building      Building      Review


                       |
                       v

                  VERIFICATION


                       |
          +------------+------------+
          |                         |
         FAIL                      PASS
          |                         |
          v                         v

      Recovery                    DONE


                       |
                       v

              Feedback / Metrics
                       |
                       v

              Context Improvement

---

# 2. Manager Responsibility

Manager มีหน้าที่:

- เข้าใจ user intent
- วิเคราะห์งาน
- สร้างแผน
- สร้าง Task Contract
- เลือกระดับความซับซ้อน
- ประสานงาน execution
- รับมือ failure

Manager ไม่ควรรับผิดชอบ:

- คำนวณ dependency graph เอง
- ตรวจ conflict ด้วยตัวเองทั้งหมด
- จัดการ lock
- ทำ deterministic validation


หลักการ:
Manager = Decision Authority

System = Rule Authority

Manager สามารถตัดสินใจได้

แต่ไม่สามารถ bypass กฎของระบบได้


---

# 3. Context System

## Purpose

ป้องกันไม่ให้ทุก agent ต้องอ่าน project ทั้งหมดซ้ำ

Architecture:
Project Knowledge
|
v
Context Compiler
|
v
Context Cache
|
v
Task Specific Context


Context ประกอบด้วย:

- project architecture
- file structure
- module relationship
- coding rules
- previous decisions
- constraints
- relevant files


เป้าหมาย:

ให้ worker ได้เฉพาะ context ที่จำเป็น

ไม่ใช่ full project context ทุกครั้ง


---

# 4. Information Confidence State

ทุกข้อมูลควรมีสถานะ:
KNOWN
ASSUMED
UNKNOWN
VERIFIED


ความหมาย:

## KNOWN

ข้อมูลที่ระบบรู้จาก context

## ASSUMED

ข้อมูลที่คาดเดา

ไม่สามารถใช้เป็น fact ได้

## UNKNOWN

ข้อมูลที่ยังไม่มี

ต้อง investigate

## VERIFIED

ข้อมูลที่ผ่านการตรวจสอบแล้ว


กฎ:
ASSUMED != VERIFIED

เพื่อป้องกัน architecture hallucination


---

# 5. Task Contract

ทุกงานที่ไม่ใช่งานเล็กควรสร้าง contract


Structure:
Task ID

Objective

Inputs

Expected Outputs

Dependencies

Files To Read

Files To Modify

Constraints

Acceptance Criteria

Verification Method

Risk Level



Worker ไม่ควรได้รับ instruction แบบ vague

แต่ควร execute จาก contract


---

# 6. Complexity Router

ไม่ใช่ทุก task ต้องผ่าน architecture เต็ม


## LOW

งานเล็ก:

ตัวอย่าง:

- typo
- config
- small function


Flow:


Context
|
Manager
|
Building
|
Test



---

## MEDIUM

Feature level:

ตัวอย่าง:

- เพิ่ม module
- เพิ่ม API
- เพิ่ม component


Flow:


Context
|
Task Contract
|
Plan Validator
|
Scheduler
|
Building
|
Review
|
Verification



---

## HIGH

Architecture change:

ตัวอย่าง:

- database redesign
- core system change
- major refactor


Flow:


Context
|
Task Contract
|
Plan Validator
|
Risk Check
|
Additional Review
|
Scheduler
|
Parallel Execution
|
Verification



---

# 7. Plan Validator

หน้าที่:

ตรวจสอบ plan ก่อน execution


ตรวจ:

## Dependency

- missing dependency
- circular dependency
- wrong order


## Scope

- file ownership conflict
- duplicate responsibility
- unclear modification


## Completeness

- missing output
- missing acceptance criteria
- missing verification


## Safety

- protected file
- risky change
- migration impact


หลักการ:

สิ่งที่ deterministic ควรใช้ code ตรวจ

ไม่ควรเสีย LLM token


---

# 8. Scheduler

หน้าที่:

- สร้าง DAG
- จัด execution order
- แบ่ง execution wave
- assign worker
- ป้องกัน conflict
- retry scheduling


ตัวอย่าง:


Wave 1:
Task A
Task B
Task D

Wave 2:
Task C

Wave 3:
Task E



---

# 9. Resource Ownership

ทุก task ต้องประกาศ resource ที่แก้ไข


ตัวอย่าง:


Task A

owns:
src/auth/

Task B

owns:
src/payment/



ถ้า conflict:


Task A:
src/shared/

Task B:
src/shared/



Scheduler ต้องเลือก:

- serialize
- split task
- exclusive lock


---

# 10. Adaptive Parallelism

Parallel ไม่ใช่เปิดทุกครั้ง


ปัจจัย:

- model availability
- latency
- task independence
- context cost
- conflict risk


หลักการ:


More agents != Always faster



---

# 11. Verification System

Agent บอกเสร็จ ไม่ใช่หลักฐาน


ต้องตรวจ:

- tests
- build
- changed files
- acceptance criteria
- review result


สถานะ DONE ต้องมี evidence


---

# 12. Recovery System

เก็บ:

- checkpoint
- execution state
- retry budget
- rollback information
- failure classification


Recovery ควรใช้ deterministic logic ก่อน


---

# 13. Observability

ใช้ Scopeboard เป็น observability layer


## System Metrics

วัด:

- validation time
- scheduler time
- cache lookup time
- queue time


## LLM Metrics

วัด:

- model
- LLM calls
- input tokens
- output tokens
- context tokens
- latency


## Outcome Metrics

วัด:

- success
- failure
- retry
- recovery
- regression


---

# 14. Efficiency Measurement

ไม่วัดจากจำนวนขั้นตอน

เพราะ system step ไม่เท่ากับ LLM cost


วัด:


LLM Tokens / Successful Task

LLM Calls / Successful Task

Execution Time / Successful Task

Success Rate

Recovery Rate



ระบบที่มี system steps มากกว่า อาจดีกว่า

ถ้ามัน:

- ลด LLM usage
- เพิ่ม reliability
- ลด failure


---

# 15. Feedback Loop

ผลลัพธ์จาก execution กลับมาปรับปรุง context



Plan
|
Execute
|
Verify
|
Metrics
|
Historical Knowledge
|
Context Improvement
|
Future Planning



ไม่ใช่ training model

แต่เป็น project-specific intelligence


---

# 16. Removed Components

## OpenVisio

Removed.


เหตุผล:

- ไม่ใช่ core execution component
- เพิ่ม complexity
- visualization ไม่ใช่ requirement หลัก
- event/state data สามารถนำไปสร้าง visualization ภายหลังได้


Architecture จึงไม่ผูกกับ visualization tool


---

# 17. Design Principles

1. อย่าเพิ่ม agent ถ้าไม่มี measurable benefit

2. อย่าใช้ LLM แก้ปัญหาที่ deterministic code แก้ได้

3. อย่าบังคับทุก task ผ่าน workflow ใหญ่

4. ใช้ progressive planning แทน complete planning ทุกครั้ง

5. วัดผลก่อนเพิ่ม feature

6. Reliability สำคัญกว่า autonomy สูงสุด


---

# Final Architecture


Manager
|
+-- Context System
|
+-- Task Contract
|
+-- Complexity Router
|
+-- Plan Validator
|
+-- Scheduler
|
+-- Workers
|
+-- Verification
|
+-- Recovery
|
+-- Metrics
|
+-- Feedback Loop



Target:

ระบบ orchestration สำหรับ coding agent ที่:

- ใช้ context อย่างมีประสิทธิภาพ
- ลด LLM cost
- ทำงาน parallel ได้อย่างปลอดภัย
- ตรวจสอบตัวเองได้
- ปรับปรุงจากข้อมูลจริง


ไม่ใช่ระบบที่มี agent เยอะที่สุด

แต่เป็นระบบที่ใช้ intelligence ในจุดที่เหมาะสมที่สุด

แนะนำให้บันทึกไฟล์นี้ไว้คู่กับ:

design.md
core-upgrade.md
New-Architecture-v1.md
