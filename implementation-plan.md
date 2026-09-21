# implementation-plan.md — IP-01 (คีย์ `IP-01:PLAN:001` ครั้งที่ 1/5)

> ไฟล์แผนอย่างเดียว ไม่แตะโค้ด ไม่แก้ไฟล์อื่น ภาษาไทย UTF-8
> อ้างอิง: `New-Architecture-v1.md` (719 บรรทัด, 17 ส่วน) + `Implementation-Roadmap-v1.md` (612 บรรทัด, Phase 0–9 + MVP) + `core-upgrade.md` § Lean Parallel Update (บรรทัด 1035–1095) + `scripts/test_lean_merge.py` + `.agent/manager/tasks/CU-09B/results.md`

---

## 1. สรุปสถาปัตยกรรมใหม่ย่อ (New-Arch)

- **หลักแบ่งอำนาจ:** `Manager = Decision Authority` (เข้าใจ intent, วิเคราะห์, วางแผน, สร้าง Task Contract, เลือกความซับซ้อน, ประสาน execution, รับมือ failure) / `System = Rule Authority` (ตรวจ dependency/conflict/completeness/safety ด้วยโค้ดดีเทอร์มินิสติก, จัด DAG/wave/lock, เกต verification/recovery — Manager บายพาสกฎไม่ได้)
- **Confidence 4 สถานะ (§4):** `KNOWN` (รู้จาก context) / `ASSUMED` (คาดเดา ใช้เป็น fact ไม่ได้) / `UNKNOWN` (ต้อง investigate) / `VERIFIED` (ตรวจแล้ว) — กฎเหล็ก `ASSUMED != VERIFIED` กัน architecture hallucination
- **รูเตอร์ 3 ระดับ (§6):** `LOW` (งานเล็ก: typo/config/ฟังก์ชันเล็ก → Context → Manager → Building → Test) / `MEDIUM` (ฟีเจอร์: Contract → Validator → Scheduler → Building → Review → Verification) / `HIGH` (เปลี่ยนสถาปัตยกรรม: เพิ่ม Risk Check + Additional Review + Parallel Execution)
- **ไปป์ไลน์เต็ม:** Context System (compiler/cache) → Task Contract → Complexity Router → Plan Validator → Scheduler (DAG/waves) → Workers → Verification (evidence ไม่ใช่คำอ้าง) → Recovery → Metrics → Feedback Loop → Context Improvement
- **หลักประหยัด:** ย้ายงานดีเทอร์มินิสติกออกจาก LLM (§17: อย่าใช้ LLM แก้ปัญหาที่โค้ดแก้ได้, อย่าบังคับทุกทาสก์ผ่าน workflow ใหญ่, วัดก่อนเพิ่มฟีเจอร์)

---

## 2. ตาราง gap-based: ของที่มีจริง (พร้อมหลักฐาน) เทียบ New-Arch §1–17

| # | ส่วน New-Arch | ของที่มีจริง + หลักฐาน | สถานะ | แกปที่เหลือ |
|---|---|---|---|---|
| 1 | Context (§3: compiler/cache/state) | `scripts/ctx_compiler.py` (`compile_task` แพ็กเกจลีน 7 ฟิลด์) + `scripts/ctx_cache.py` (lazy refresh, `stale` flag) — หลักฐาน `core-upgrade.md:1044–1061`: ลดโทเค็น **59.1% (587→240)**, ฮิตเรต **0.8 (`hits=8 misses=2`)**, อินวาลิเดต 3 สาย โกลบอลรอด; `test_lean_merge.py:38–69` ตรวจ 7 ฟิลด์ + `manager_validate` ผ่าน | มีแล้วส่วนใหญ่ | ขาด confidence tagging บน context (ยังไม่แปะ KNOWN/ASSUMED/UNKNOWN/VERIFIED) (อัปเดต: มีแล้ว — Phase 2 DONE: `confidence_tags` P2-01) |
| 2 | Task Contract (§5) | `schema-task` + `manifest` (โครง Task ID/Objective/Inputs/Outputs/Dependencies/Files/Constraints/Acceptance/Verification/Risk ครบตามสเปก CU-01) — อ้าง `test_lean_merge.py:62,127–131` (`validate_completeness` ตรวจ `Task/Task.id/Task.acceptance`) | มีแล้วพื้นฐาน | ขาดฟิลด์ confidence-state ใน contract + การบังคับ `verification method` ทุกทาสก์ที่ไม่ใช่ LOW (อัปเดต: มีแล้ว — Phase 2 DONE: `confidence_tags` P2-01 + `task_contract` P2-02) |
| 3 | Complexity Router (§6 + รูตติ้ง §15) | มีสเปกรูตติ้ง LOW/MEDIUM/HIGH ตาม §15 ของ Roadmap (เกณฑ์ `<3 files` = LOW; database/core/dependency = HIGH) แต่**ยังไม่มี classifier โค้ด** | ยังไม่มีโค้ด (อัปเดต: มีแล้ว — Phase 3 DONE: `task_levels` P3-01) | **สร้างใหม่: rule-based complexity classifier** (stdlib-only, รับ Task+Context+Changed Files → LOW/MEDIUM/HIGH) (อัปเดต: มีแล้ว — Phase 3 DONE) |
| 4 | Plan Validator (§7) | `validate_completeness` (ctx_compiler) + `validate_dag` (dag_waves: missing/circular/wrong-order) + review gate (CRITICAL/HIGH outstanding → NOT DONE ห้ามบายพาส — `core-upgrade.md:1084`) | มีแล้วพื้นฐาน | ขาด completeness gate แบบไฟล์ (reject ไม่มี verification) + รายงานผลรวมศูนย์ (อัปเดต: มีแล้ว — Phase 4 DONE: `require_verification` P4-01) |
| 5 | Scheduler + DAG (§8–§9) | `scripts/dag_waves.py` (`build_waves` + `assign` + `check_conflict` สองทิศ + `capacity_skips`) — หลักฐาน `core-upgrade.md:1065–1069` (เวฟ `[A,B,D]→[C]→[E]`, ชน 0 คู่, `capacity_hint=5`) และ `test_lean_merge.py:85–126` (topology + same-zone แยก 3 แบทช์ + capacity-bound นับ skips) | มีแล้ว | ขาด serialize/split-task policy เป็นลายลักษณ์อักษรเมื่อชน owns เดียวกัน (ตอนนี้แยกแบทช์เงียบ) (อัปเดต: มีแล้ว — Phase 5 DONE: `docs/ownership-policy.md`) |
| 6 | Verification (§11) | ชุดเทส **161 เทส** (ชุดปัจจุบัน; เดิม 127 เทส) + เกต `VERIFYING` (ตรวจ tests/build/changed-files/acceptance/review ก่อน DONE) | มีแล้วพื้นฐาน | **สร้างใหม่: `verification-report.json`** ต่อรัน (ยังไม่มีไฟล์รวมศูนย์) (อัปเดต: มีแล้ว — Phase 6 DONE: `verify_report` P6-01) |
| 7 | Recovery (§12) | `checkpoint` + `execution state` + `lease` + `events` (idempotent recovery; ตัด optimistic-rollback แล้วเพราะซับซ้อน — `core-upgrade.md:1090–1092`) | มีแล้วพื้นฐาน | ขาด failure classification ครบ 6 แบบ (TOKEN_LIMIT/MODEL_ERROR/CODE_ERROR/TEST_FAIL/CONFLICT + retry budget เป็นไฟล์) (อัปเดต: มีแล้ว — Phase 7 DONE: `failure_kinds` P7-01) |
| 8 | Adaptive Parallelism (§10) | `capacity_hint` พื้นฐาน (`min(5,len)`, ไม่แตกแบทช์เกินจำเป็น — `core-upgrade.md:1069`) + เบนช์สเกล CU-09B ชี้ว่าคอขวดคือ `assign` (~75% ที่ N=1000) | มีแล้วพื้นฐาน | ขาดตัวเลือกจำนวน worker ตามสถานการณ์ (1/3/5 จาก task count + conflict risk + token budget) (อัปเดต: มีแล้ว — Phase 8 DONE worker_choice P8-01) |
| 9 | Metrics/Observability (§13–§14) | `batch_seq` 3 เวลา (`queue_ms ~2.7 / compile_ms ~3.3 / persist_ms ~0.02` — `core-upgrade.md:1060–1081`) + เบนช์ 9 เคส (100/300/1000 × worker 1/3/5 — `CU-09B/results.md:14–24`, เคสหนักสุดรวม ~16ms) + `seq 1-5` + `heartbeat 3/3` + `snapshot N=10` | มีแล้วพื้นฐาน | **สร้างใหม่: `baseline-report.json`** (Phase 0) + ขยายเป็น LLM tokens/calls + success/retry ภายหลัง (อัปเดต: มีแล้ว — Phase 0 DONE: `baseline-report.json` + `collect_baseline`) |
| 10 | Feedback Loop (§15) | events-driven พื้นฐาน (`batch_append` + snapshot + heartbeat → ปรับ context ภายหลังได้) แต่ยังไม่เต็มรูป | เริ่มต้นเท่านั้น (อัปเดต: มีแล้ว — Phase 9 DONE feedback_loop P9-01) | **สร้างใหม่: feedback loop เต็มรูป** (เก็บ Task/Plan/Execution/Result/Failure → ปรับ context จริง) |
| 11 | Confidence States (§4) | **ยังไม่มีในโค้ด** (ไม่มีการแปะป้าย KNOWN/ASSUMED/UNKNOWN/VERIFIED ที่ใด) | ไม่มี (อัปเดต: มีแล้ว — Phase 2 DONE confidence_tags P2-01) | **สร้างใหม่: confidence-state tagging** (สคีมา + กฎ `ASSUMED != VERIFIED` ใน validator) |

> ครบ 11 ส่วนตามที่สั่ง (Context / Contract / Router / Validator / Scheduler / Verification / Recovery / Parallelism / Metrics / Feedback / Confidence)

---

## 3. แผนเฟสตาม Roadmap 0–9 (ข้ามของที่มีแล้ว — สร้างใหม่เฉพาะแกปจริง)

| เฟส | สถานะ | งานสร้างใหม่ (เฉพาะแกป) | เวฟ/ดีเพนเดนซี | รูตติ้งเอเจนต์ | เกณฑ์ผ่าน |
|---|---|---|---|---|---|
| Phase 0 Baseline | **สร้างใหม่** | `baseline-report.json` (LLM calls/tokens/latency + time/success/retry/failure-type) เก็บก่อนอัปเกรด | Wave 1 (ทำก่อนทุกเฟส) | Manager | มีไฟล์ baseline 1 ไฟล์, รันซ้ำได้ดีเทอร์มินิสติก |
| Phase 1 Context | ข้าม (มีแล้ว) | — (คง `ctx_compiler/ctx_cache`, ฮิต 0.8) | — | — | คงเดิม: ลด context tokens ≥30% (ทำได้ 59.1% แล้ว) |
| Phase 2 Contract | ต่อยอดเล็กน้อย | เพิ่มฟิลด์ confidence-state + บังคับ verification method (ทาสก์ non-LOW) | ขึ้นกับ Phase 0 | Planning | `validate_completeness` ตรวจฟิลด์ใหม่ผ่าน |
| Phase 3 Router | **สร้างใหม่** | **rule-based complexity classifier** (stdlib-only; LOW `<3 files` + no-arch-impact / HIGH database+core+dependency-change; คืนเหตุผลทุกครั้ง) | ขึ้นกับ Phase 2 | Planning → Building (LOW ข้าม validator) | classifier ผ่านเคสตัวอย่าง LOW/MEDIUM/HIGH + ลด planning time ที่วัดได้ |
| Phase 4 Validator | ข้าม (มีแล้ว) | เพิ่ม completeness gate ปฏิเสธงานไม่มี verification (โค้ด 1 ฟังก์ชัน) | ขึ้นกับ Phase 3 | Review | ลด failed execution จาก bad plan |
| Phase 5 Scheduler | ข้าม (มีแล้ว) | เอกสาร serialize/split/exclusive-lock เมื่อ owns ชน (ไม่เพิ่มโค้ดล็อกใหม่) | ขึ้นกับ Phase 4 | Manager | เวฟ `[A,B,D]→[C]→[E]` คงเดิม + `capacity_skips` นับถูก |
| Phase 6 Verification | **สร้างใหม่บางส่วน** | **`verification-report.json`** (tests/build/files/acceptance/review + evidence) | ขึ้นกับ Phase 5 | Review → Building (FAIL วนกลับ) | ลด false completion; ทุก DONE มีไฟล์หลักฐาน |
| Phase 7 Recovery | ข้าม (มีแล้ว) | เพิ่ม failure classification 6 แบบ + retry budget ลงไฟล์ (ใช้ checkpoint/lease/events เดิม) | ขึ้นกับ Phase 6 | Manager | กู้ได้ตาม retry budget โดยไม่เพิ่ม rollback ใหม่ |
| Phase 8 Parallelism | ต่อยอดเล็กน้อย | ตัวเลือก worker 1/3/5 จาก (task count + conflict risk + token budget) บน `capacity_hint` เดิม | ขึ้นกับ CU-09B bench | Manager | ไม่ช้ากว่า sequential; `assign` ไม่พุ่งที่ N=1000 |
| Phase 9 Feedback | **สร้างใหม่** | **feedback loop เต็มรูป** (Task/Plan/Execution/Result/Failure → context improvement; ไม่เทรนโมเดล) | สุดท้าย (ขึ้นกับ Phase 6+Metrics) | Manager | มี pattern report 1 ชุดจากการรันจริง |

**เฟสที่ต้องสร้างใหม่/ต่อยอดจริง: 6 เฟส** (0, 2, 3, 6, 8, 9) — ที่เหลือ (1, 4, 5, 7) ข้ามเพราะของมีแล้วคงเดิม (อัปเดต: สร้างครบแล้วทั้ง 6 เฟส)

---

## 4. MVP Scope (ตาม Roadmap § MVP)

**YES (ต้องมีใน v1.0):** Context Cache ✅มีแล้ว / Task Contract ✅มีแล้ว (+confidence tagging ใหม่) / Validator ✅มีแล้ว / Scheduler ✅มีแล้ว / Verification ✅มีแล้ว (+`verification-report.json` ใหม่) / Metrics ✅พื้นฐานมีแล้ว (+`baseline-report.json` ใหม่)

**NO (ไม่ทำใน v1.0):** Multi Manager / Swarm Intelligence / Auto Learning / Complex AI Critic / Visualization dependency (รวม OpenVisio ที่ถูกถอดแล้วตาม §16 — ใช้ event/state data ภายหลังได้)

**สิ่งที่ไม่ทำ (กันขอบเขตบาน):** ไม่แก้ `scripts/*`, `design.md`, `core-upgrade.md`, `state/queue`; ไม่เพิ่มเฟรมเวิร์ก/ดีเพนเดนซี; ไม่ผูกโมเดลเฉพาะรุ่น (model-agnostic); ไม่ทำ AI summarization ซับซ้อน / training model / autonomous evolution; ไม่รื้อลีส/ล็อก/สแนปช็อตที่วัดแล้วว่าคุ้ม; ไม่เพิ่มเมตริกชุดใหญ่ก่อนมีเบนช์รองรับ

---

## 5. ความเสี่ยง + หลักไม่เพิ่มความซับซ้อนโดยไม่มีข้อมูล (New-Arch §17)

1. **เพิ่ม classifier แล้วรูตติ้งผิด (LOW หลุดไป HIGH หรือกลับกัน)** — รับมือ: rule-based ก่อน + คืนเหตุผลทุกครั้ง + วัด planning time จริงก่อนจูนเกณฑ์
2. **confidence tagging เป็นพิธีกรรม (แปะป้ายแต่ไม่มีใครตรวจ)** — รับมือ: ผูกกฎ `ASSUMED != VERIFIED` เข้า validator ให้ reject อัตโนมัติ
3. **`verification-report.json` กลายเป็นไฟล์ขยะ** — รับมือ: สคีมาตายตัว 5 ช่อง (tests/build/files/acceptance/review) + DONE ต้องอ้างไฟล์นี้เท่านั้น
4. **feedback loop เก็บข้อมูลแต่ไม่ปรับปรุง** — รับมือ: ทำหลัง Phase 6+Metrics มีข้อมูลจริงเท่านั้น (ตามหลัก §17 ข้อ 5: วัดผลก่อนเพิ่มฟีเจอร์)
5. **ขอบเขตบาน (เพิ่ม worker/เมตริก/ล็อกใหม่โดยไม่มีหลักฐาน)** — รับมือ: หลัก §17 ทั้ง 6 ข้อ (อย่าเพิ่มเอเจนต์ถ้าไม่มี measurable benefit / อย่าใช้ LLM แก้ปัญหาที่โค้ดแก้ได้ / อย่าบังคับทุกทาสก์ผ่าน workflow ใหญ่ / progressive planning / วัดก่อนเพิ่ม / reliability > autonomy) + บทเรียน Lean ที่ตัดทิ้งแล้ว 4 อย่าง (ล็อกแยกพาร์ติชัน/รีดเรพลิกา/optimistic-rollback/เมตริกชุดใหญ่ — `core-upgrade.md:1086–1094`)

---

*ท้ายไฟล์ — พร้อมส่งรีวิว (Building Agent รับช่วงต่อเฉพาะงานสร้างใหม่ 6 เฟสข้างต้น)*

---

## สถานะปัจจุบัน

คิวจบหมดแล้ว สวีต 161 เทสเขียวทั้งหมด เหลือแค่ข้อมูลโทเค็นจริงที่ยังเป็น llm UNKNOWN อย่างซื่อสัตย์ (รอวัดจากการรันจริงก่อนเคลมตัวเลข)
