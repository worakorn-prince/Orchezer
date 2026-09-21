# ดีไซน์เทเลเมทรี LLM — INV-01 (PLAN:001 ครั้งที่ 1)

> สถานะ: งานออกแบบก่อนโค้ด | ภาษาไทย UTF-8 | ไฟล์เดียว ไม่แตะไฟล์อื่น
> อ้างอิง: `New-Architecture-v1.md §13-§14`, `baseline-report.json` ส่วน `llm`, `scripts/metrics.py`, `scripts/observability.py`, `scripts/manager.py`, `.agent/manager/tool-calls.jsonl`, `opencode.json`, `.agent/manager/config.json`

## 1. ผล investigate: แหล่งข้อมูลที่เป็นไปได้ในรีโพ (จุดตัด + ข้อมูลที่มี/ไม่มี)

### 1.1 ภาพรวม (Overview)

เป้าหมาย §13-§14 คือวัด `model / calls / input / output / context tokens / latency` แล้วคำนวณ `tokens-calls-time per successful task` แต่ปัจจุบัน `baseline-report.json` ส่วน `llm` เป็น `UNKNOWN` อย่างถูกต้อง เพราะไม่มีแหล่งข้อมูลไลฟ์ที่ให้ค่าเหล่านี้ได้จริง

### 1.2 จุดตัดที่สำรวจ (7 จุด)

| # | จุดตัด | ที่มีอยู่ | ที่ไม่มี | ประเมิน |
|---|---|---|---|---|
| P1 | `opencode.json` (รูท) | มีแค่คอนฟิก MCP `memory` (โลคัล) | ไม่มี `provider` / `model` / `telemetry` / ข้อมูลโทเค็น | ใช้เป็นแหล่งโมเดลไม่ได้ |
| P2 | `.opencode/` | มีแค่ `.gitignore`, `package.json`, `skill/` | ไม่มีคอนฟิกโมเดล/โพรไวเดอร์ | ใช้เป็นแหล่งโมเดลไม่ได้ |
| P3 | `.agent/manager/config.json` | มี `limits`, `watchdog`, `approval`, `git` | ไม่มี `provider` / `model` / `telemetry` | ใช้เป็นแหล่งโมเดลไม่ได้ |
| P4 | `.agent/manager/tool-calls.jsonl` (มีไฟล์จริง) | มี `time/task/attempt/session_id/agent/operation/tool/duration_ms/status/prompt_hash/tokens_in/tokens_out` | `tokens_in/tokens_out` เป็น `null` ทุกแถวที่สุ่มตรวจ, `duration_ms=0`, ไม่มีฟิลด์ `model`, ไม่มี `latency` แยก, ไม่มี `context tokens` | โครงพร้อม แต่ข้อมูลว่าง — จุดตัดหลักสำหรับเฟส A/B |
| P5 | `scripts/manager.py : log_tool_call / wrap_task` | ฟังก์ชันรับ `tokens_in/tokens_out/duration_ms/prompt_text→prompt_hash` แล้วเขียน `tool-calls.jsonl` + อีเวนต์ `TOOL_CALL` | ผู้เรียก (`wrap_task` ดีฟอลต์ `None`) ไม่เคยส่งค่าโทเค็นจริง, ไม่รับ `model`, ไม่วัด `latency` | จุดเติมข้อมูลดีที่สุดโดยไม่ต้องรื้อแกน |
| P6 | `scripts/metrics.py` + `scripts/observability.py` (ส่วนหัว) | `metrics.py` รวม `tokens_in+tokens_out` ต่อทาสก์, คำนวณ `token_per_success/tools_per_success` ได้แล้ว; `observability.py` อ่าน `events.jsonl+tool-calls.jsonl` สร้างคอนแทร็ก | ผลรวมเป็น `0` เพราะต้นทางเป็น `null`; `observability.py` ยังไม่มีเมตริก LLM โดยเฉพาะ | จุดอ่าน/รวมผลพร้อม ใช้ต่อได้เลยเมื่อมีข้อมูล |
| P7 | `scripts/collect_baseline.py` + `scripts/bench.py` + `scripts/bootstrap.py` | `collect_baseline.py` เขียนส่วน `llm` เป็น `UNKNOWN` อย่างตั้งใจ; `bench/bootstrap` มี `tokens_in/out` แค่ค่าจำลอง/เดโม | ไม่มีไลฟ์เทเลเมทรีจากโพรไวเดอร์จริง | ยืนยันว่า UNKNOWN ไม่ใช่บั๊ก แต่คือสถานะที่ต้อง investigate |

### 1.3 ข้อสรุปว่าทำไมปัจจุบันเป็น UNKNOWN

1. **ไม่มีซอร์สไลฟ์:** ไม่มีฮุก/เอเจนต์/สคริปต์ใดดึง `calls/tokens/latency` จากโพรไวเดอร์จริง (OpenCode Zen / OpenRouter / NVIDIA ล้วนอยู่นอกScopeboardรีโพนี้)
2. **ท่อมีแต่ข้อมูลไม่ไหล:** สคีมา `tool-calls.jsonl` มีช่อง `tokens_in/out` แล้ว แต่ผู้เรียกส่ง `null` และไม่มีช่อง `model` ตั้งแต่ต้น
3. **ไม่มีคอนฟิกโมเดล:** `opencode.json` + `.opencode/` + `config.json` ไม่มี `provider/model` จึงระบุ `model` ต่อคอลไม่ได้
4. **ตัวรวมผลคำนวณบนค่าว่าง:** `metrics.py` รวม `null→0` จึงต้องตอบ `null/UNKNOWN` ต่อไป มิฉะนั้นจะละเมิดกฎ `ASSUMED != VERIFIED` (§4)
5. **วิธีเก็บปัจจุบันถูกต้องแล้ว:** `collect_baseline.py` มาร์ก `UNKNOWN` พร้อม `collection_method` อธิบายว่าไม่มีซอร์สไลฟ์ — ตรงตาม §4 (UNKNOWN ต้อง investigate ไม่ใช่เดา)

> สรุปสั้น: โครงท่อ (`log_tool_call → tool-calls.jsonl → metrics.py`) มีครบ แต่ไม่มีน้ำ (ข้อมูลไลฟ์) + ไม่มีป้ายชื่อน้ำ (model/provider) จึงเป็น UNKNOWN โดยชอบ

## 2. ดีไซน์ตัวเก็บแบบมินิมัลดีเทอร์มินิสติก

### 2.1 สถาปัตยกรรม (Architecture)

```text
Worker/Agent (manual log หรือ wrapper ในอนาคต)
  → .agent/manager/llm-telemetry.jsonl (ไฟล์ใหม่ ไฟล์เดียว)
  → collector (อ่านอย่างเดียว รวมเป็นสรุป)
  → baseline-report.json ส่วน llm (UNKNOWN → VERIFIED)
  → scripts/metrics.py / observability.py (อ่านสรุป ไม่แก้สคีมาเดิม)
```

หลักการ: แยกไฟล์ใหม่ ไม่แก้สคีมา `tool-calls.jsonl` เดิม; เขียนแบบ append-only JSONL; คำนวณด้วยสแตนดาร์ดไลบรารีเท่านั้น; ไม่เก็บพรอมต์/ซีเคร็ต

### 2.2 สคีมา (1 บรรทัด = 1 คอล, ตัวอย่างค่าปลอดภัยเท่านั้น)

| ฟิลด์ | ไทป์ | ความหมาย | ตัวอย่าง (สมมติ ไม่ใช่ซีเคร็ต) |
|---|---|---|---|
| `time` | string ISO-8601 UTC | เวลาเกิดคอล | `2026-09-21T00:00:00+00:00` |
| `task_id` | string | ผูกกับ Task Contract | `INV-01` |
| `agent` | string | `planning/building/review/manager` | `planning` |
| `model` | string | ชื่อโมเดลแบบอิสระ (model-agnostic, ไม่ฮาร์ดโค้ด) | `zen-free-model-a` |
| `provider` | string | ชื่อโพรไวเดอร์แบบอิสระ | `zen` |
| `calls` | int (=1 ต่อแถว) | นับคอล | `1` |
| `input_tokens` | int \| null | อินพุตโทเค็น (null = ไม่ทราบ ห้ามเดาเป็น 0) | `1200` |
| `output_tokens` | int \| null | เอาต์พุตโทเค็น | `300` |
| `context_tokens` | int \| null | คอนเท็กซ์โทเค็นที่ส่งจริง (ถ้าไม่ทราบให้ null) | `240` |
| `latency_ms` | number \| null | เวลาเรียกลมเดลต่อคอล | `850.5` |
| `status` | enum `ok/error/unknown` | ผลคอล | `ok` |
| `prompt_hash` | string (12 อักขระ) | แฮช SHA-256 แบบย่อของพรอมต์ (เก็บแค่แฮช) | `abc123def456` |
| `session_id` | string | ผูกเซสชัน (ถ้ามี) | `ses-inv01-manual-001` |

กฎดีเทอร์มินิสติก:
- ขาดข้อมูลให้ใช้ `null` ไม่ใช่ `0`; ตัวรวมผลข้าม `null` (ไม่ปนกับค่าจริง)
- `total_tokens = input + output` คำนวณตอนรวมผลเท่านั้น ไม่เก็บซ้ำในไฟล์ดิบ
- เรียงแถวตาม `time`, เขียนครั้งละบรรทัด, ห้ามเขียนทับประวัติ
- `model/provider` เป็นสตริงอิสระ รองรับมัลติโพรไวเดอร์โดยไม่แก้สคีมา

### 2.3 ที่เก็บ (Storage)

- พาธใหม่ไฟล์เดียว: `.agent/manager/llm-telemetry.jsonl` (ใต้ `.agent` ตามนโยบาย ไม่ใช่รูท)
- ฟอร์แมต: JSONL UTF-8, บรรทัดละอ็อบเจกต์ตามสคีมาข้างบน
- อายุข้อมูล: หมุนเวียนแบบเดียวกับ `history/` (เช่น เก็บ 90 วัน) — รายละเอียดให้เฟส B กำหนด ไม่ล็อกในดีไซน์นี้
- ผู้อ่าน: `metrics.py` (รวม `tokens/calls per success`), `observability.py` (แสดงผล Scopeboard), `collect_baseline.py` (เติมส่วน `llm`) — อ่านอย่างเดียว ไม่แก้ไฟล์ดิบ

### 2.4 วิธีคุมไพรเวซี (Privacy — ห้ามบันทึกพรอมต์/ซีเคร็ต/คีย์)

1. **ห้ามฟิลด์พรอมต์ดิบ:** เก็บได้แค่ `prompt_hash` (ย่อ 12 อักขระ) ห้าม `prompt_text` / `prompt` / `messages` / `content`
2. **ห้ามซีเคร็ต:** ห้าม `api_key` / `token_secret` / `password` / `authorization` / `secret` ทุกกรณี แม้ในตัวอย่าง
3. **ห้ามค่าจริงในดีไซน์:** ตัวอย่างในเอกสารนี้เป็นค่าสมมติเท่านั้น ไม่ผูกกับคีย์จริง
4. **ตรวจก่อนรวม:** คอลเล็กเตอร์ต้องดรอปบรรทัดที่มีคีย์ต้องห้าม (deny-list) แล้วนับเป็น `dropped_privacy` โดยไม่ล็อกค่าดิบ
5. **สิทธิ์ไฟล์:** ไฟล์อยู่ใต้ `.agent/manager/` ตามโครงเดิม ไม่เผยแพร่แยก; ไม่คอมมิตค่าจริงขึ้นรีโมตถ้ามีนโยบายห้าม

## 3. แผนอิมพลีเมนต์เป็นเฟส (ไม่ลงโค้ด)

### 3.1 รายการงานย่อย (Work Breakdown)

#### เฟส A — Manual Logging (มือก่อน เก็บเกี่ยวเร็วสุด)

- **A1:** กำหนดสคีมา+ตัวอย่างแถว (จาก §2.2) เป็นคู่มือ 1 หน้าให้เอเจนต์กรอกมือ
- **A2:** เอเจนต์ที่เรียกโมเดล (planning/building/review) เขียนแถวลง `.agent/manager/llm-telemetry.jsonl` ด้วยมือหลังจบคอล (1 คอล = 1 บรรทัด, ไม่ทราบให้ `null`)
- **A3:** ตรวจด้วยตา + นับบรรทัด/ตรวจ JSON ว่าแยกบรรทัดถูกต้อง (ไม่ต้องมีสคริปต์ใหม่)
- **เกณฑ์จบเฟส:** มีแถวจริง ≥ 3 ทาสก์, ทุกแถวผ่านสคีมา §2.2, ไม่มีพรอมต์/ซีเคร็ตหลุด

#### เฟส B — Collector (ตัวรวมแบบอ่านอย่างเดียว ดีเทอร์มินิสติก)

- **B1:** สคริปต์อ่าน `llm-telemetry.jsonl` อย่างเดียว (ไม่แก้ไฟล์ดิบ) รวมเป็น `total_calls/input/output/total_tokens/avg_latency/per_task`
- **B2:** กฎ `null`: ข้าม `null`, นับ `known_vs_unknown`, คำนวณ `tokens/calls/time per successful task` ร่วมกับ `events.jsonl` (สถานะ DONE)
- **B3:** กรองไพรเวซี (deny-list) + รายงาน `dropped_privacy`; เอาต์พุตเป็นสรุป JSON ชั่วคราวให้คนตรวจก่อน
- **เกณฑ์จบเฟส:** รันซ้ำได้ผลเดิม (ดีเทอร์มินิสติก), ไฟล์ดิบไม่ถูกแตะ, ตัวเลขตรงกับนับมือ

#### เฟส C — ต่อ `baseline-report.json` ให้ `llm` เป็น VERIFIED

- **C1:** ขยาย `collect_baseline.py` ให้อ่านสรุปจาก B แล้วเติม `llm.metrics` (`total_calls/input/output/total_tokens/latency`) แทน `null`
- **C2:** เปลี่ยน `llm.status` เป็น `VERIFIED` เฉพาะเมื่อมีข้อมูลไลฟ์จริง + `collection_method` ระบุแหล่งไฟล์/ช่วงเวลา; ถ้าไม่มีข้อมูลให้คง `UNKNOWN` (ห้าม ASSUMED)
- **C3:** เติม `confidence.llm = VERIFIED` พร้อมโน้ตช่วงเวลาวัด; รันเบสไลน์ซ้ำแล้วตรวจว่า `execution=VERIFIED` ยังคงเดิม
- **เกณฑ์จบเฟส:** `baseline-report.json` ส่วน `llm` มีค่าจริง + `collection_method` ตรวจซ้ำได้ + ไม่ทำลายส่วน `execution`

### 3.2 การพึ่งพา (Dependencies)

- เฟส A พึ่ง: ไฟล์ดีไซน์นี้ (สคีมา §2.2) + สิทธิ์เขียน `.agent/manager/` + ความร่วมมือเอเจนต์ในการกรอกมือ
- เฟส B พึ่ง: ข้อมูลดิบจาก A (≥3 ทาสก์) + `events.jsonl` สำหรับสถานะ DONE
- เฟส C พึ่ง: สรุปที่ผ่านเกณฑ์ B + `collect_baseline.py` เวอร์ชันปัจจุบัน
- ไม่พึ่ง: การแก้ `manager.py` แกน, การเพิ่มดีเพนเดนซี, การผูกโมเดลเฉพาะรุ่น

## 4. เกณฑ์ผ่านของดีไซน์ + ความเสี่ยง

### 4.1 เกณฑ์ผ่าน (Acceptance Criteria)

1. **ครบสคีมา:** มี `model/calls/input/output/context tokens/latency/task_id` ทุกแถว (ไม่ทราบให้ `null` อย่างชัดแจ้ง)
2. **แยกที่เก็บ:** ไฟล์ใหม่ใต้ `.agent` ไฟล์เดียว, ไม่แก้สคีมา `tool-calls.jsonl` เดิม
3. **ไพรเวซี:** ไม่มี `prompt_text` / `api_key` / `secret` ในไฟล์ดิบ+ตัวอย่าง+สรุป (ตรวจด้วย deny-list ผ่าน)
4. **ดีเทอร์มินิสติก:** อ่าน+รวมซ้ำได้ผลเดิม, ไม่ใช้มัลติโพรเซส/โมเดลตัดสินใจ, `null ≠ 0`
5. **วัดประสิทธิภาพได้:** คำนวณ `tokens/calls/time per successful task` (§14) ได้จากข้อมูลจริง
6. **พร้อม VERIFIED:** `baseline-report.json` ส่วน `llm` เปลี่ยนเป็น `VERIFIED` ได้พร้อม `collection_method` ที่ตรวจซ้ำได้ ไม่ละเมิด `ASSUMED != VERIFIED`

### 4.2 ความเสี่ยง (Risks) + วิธีบรรเทา

| ความเสี่ยง | ผลกระทบ | บรรเทา |
|---|---|---|
| **โอเวอร์เฮด (Overhead)** — กรอกมือ/ไฟล์โตทำให้ช้า | เสียเวลาทาสก์หลัก, ไฟล์ JSONL บวม | เริ่มมือเฉพาะทาสก์ตัวอย่าง (A), จำกัดเฟส B ให้อ่านอย่างเดียว, หมุนเวียน 90 วัน, เกิน 1MB ให้เตือนแบบเดียวกับ `tool-calls.jsonl` |
| **ความแม่นยำโทเค็น (Token accuracy)** — แต่ละโพรไวเดอร์นับไม่ตรง, กรอกมือผิด, `null` ปน `0` | ตัวเลข `per success` เพี้ยน, เทียบข้ามรุ่นไม่ได้ | ใช้ `null` เมื่อไม่ทราบ, แยก `known_vs_unknown`, ระบุ `model/provider` ทุกแถว, ไม่เปรียบเทียบข้ามโมเดลโดยไม่มีป้ายกำกับ |
| **มัลติโพรไวเดอร์ (Multi-provider)** — ชื่อโมเดล/หน่วย latency/นิยาม context ไม่ตรงกัน | รวมผลผิด, สคีมาแตก | สคีมา model-agnostic (สตริงอิสระ), เก็บ `provider` แยก, คำนวณ `total` ตอนรวมเท่านั้น, ล็อกนิยาม `context_tokens` ในคู่มือเฟส A |
| **ไพรเวซีหลุด (Privacy leak)** — เผลอวางพรอมต์/คีย์ในไฟล์ดิบ | ซีเคร็ตรั่วในรีโพ | deny-list + ตัวอย่างปลอดซีเคร็ต + ตรวจก่อนรวม (B3), ห้ามฟิลด์ดิบตั้งแต่สคีมา |
| **ข้อมูลว่างซ้ำ (Still UNKNOWN)** — เอเจนต์ไม่กรอก, คอลเล็กเตอร์ไม่มีอินพุต | เฟส C ไปต่อไม่ได้ | เกณฑ์จบ A บังคับ ≥3 ทาสก์ก่อนขึ้น B; ถ้าไม่มีข้อมูลให้คง `UNKNOWN` อย่างถูกต้อง ไม่ฝืนเป็น VERIFIED |

---

**พร้อมรีวิว:** พร้อม — จุดตัดครบ 7 จุด, สคีมา+ที่เก็บ+ไพรเวซีชัด, แผน 3 เฟสรอ Building เอเจนต์รับไปอิมพลีเมนต์ (ห้ามเขียนโค้ด/ฮุกจริงในงานนี้)
