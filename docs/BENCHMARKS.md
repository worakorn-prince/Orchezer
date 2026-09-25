# Orchezer Benchmarks (roadmap section 12, P2)

All runs use **synthetic logs in a temp dir** (`scripts/bench.py --calls N`) —
real `.agent/` data is never touched. Each scale splits calls across 3
projects to also exercise cross-project aggregation.

## Results (this machine, CPython 3.12, Windows)

| tool calls | rebuild | export | aggregate | parse* | peak RAM | export size |
|---|---:|---:|---:|---:|---:|---:|
| 10,000 | 0.64s | 1.91s | 1.19s | 0.14s | 6.9MB | 0.9MB |
| 100,000 | 11.0s | 14.6s | 11.1s | 0.84s | 69MB | 9.8MB |
| 500,000 (before) | 38.3s | 97.2s | 61.0s | 6.6s | 347MB | 49MB |
| 500,000 (after) | 23.4s | 46.3s | 33.0s | 3.5s | 347MB | 24.8MB |

\* `parse` = `json.load` time of `data.json`, proxy for browser dashboard load.

## Findings (evidence-based, no guessing)

1. **O(T×N) partitioning blowup** — `graph.build_graph`, `metrics.compute_metrics`
   and `failure.analyze` re-scanned full row lists per task. 500K calls × 2500
   tasks never finished (>600s). Fixed with single-pass `task → rows` grouping.
2. **Pretty-print cost** — `json.dump(indent=2)` on a 49MB payload dominated
   export/parse time. `dump_json()` now writes compact JSON above 50K calls
   (~2x faster export, ~2x smaller files, identical content).
3. **Peak RAM is data-dominated** (~0.7KB/call) and unchanged by the fixes —
   expected: rows stay in memory through the pipeline.

## Larger scales (out of scope by decision 2026-09-15)

1M/10M runs were intentionally dropped — 500K numbers already prove the
pipeline scales linearly after the single-pass fixes, and larger runs need
dedicated machine time for little extra signal. Commands kept for reference:

```powershell
python scripts/bench.py --calls 1000000 --out bench-1m.json
python scripts/bench.py --calls 10000000 --out bench-10m.json   # needs ~GBs RAM, run alone
```

Checkpoints are written per scale, so an interrupted run keeps finished rows.
Rule from the roadmap: no storage/indexing optimization without a benchmark
first — JSONL stays canonical until numbers say otherwise.

## Reproduce

```powershell
python scripts/bench.py --calls 10000
python -m unittest discover -s scripts -p "test_logging.py"   # 37 tests incl. scale smoke
```

## Scheduler + telemetry benchmarks

เบนช์ CU-09B: 100/300/1000 ทาสก์ × เวิร์กเกอร์ 1/3/5 เคสหนักสุด ~16ms
คอขวดคือ assign (~75% ที่ N=1000) สเกลตามทาสก์ ไม่ใช่เวิร์กเกอร์
เทเลเมทรี A: 4 แถว manual (เบสไลน์ก่อนมี collector)
เทเลเมทรี B: collector อ่านอย่างเดียว ไม่แตะงานผู้ใช้
เทเลเมทรี C: gate ต่อเบสไลน์ก่อนผ่าน
ดูผลเต็มใน `.agent/manager/tasks/CU-09B/results.md`
หมายเหตุ: เบนช์ ~16ms กับ ~35ms คือคนละรอบรัน เทียบข้ามรอบไม่ได้โดยตรง
ให้ยึดรอบ/คอมมิตเดียวกันเมื่อเทียบเลขเบนช์
