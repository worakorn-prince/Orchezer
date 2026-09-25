"""bench.py — P2 performance benchmarks (roadmap section 12).

Generates SYNTHETIC logs in a temp dir (never touches real .agent/) and
measures: ingest/write, metrics rebuild, export size/time, aggregation,
dashboard-JSON parse (browser-load proxy) and peak Python memory.

Usage:
  python scripts/bench.py                                   # 100K/500K/1M
  python scripts/bench.py --calls 10000,100000              # custom scales
  python scripts/bench.py --calls 10000000 --out bench.json # 10M scale
"""
import os
import sys
import json
import time
import random
import argparse
import tempfile
import tracemalloc

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
ROOT = os.path.dirname(HERE)

import metrics as metrics_mod
import export_json as export_mod
import aggregate as aggregate_mod

TOOLS = ["Read", "Edit", "Bash", "Grep", "Glob", "Task"]
AGENTS = ["planning", "building", "review"]
STATUSES = ["ok"] * 95 + ["error"] * 4 + ["denied"]


def gen_project(root, n_calls, seed=7, n_tasks=0):
    rnd = random.Random(seed)
    n_tasks = n_tasks or max(10, n_calls // 200)
    mgr = os.path.join(root, ".agent", "manager")
    os.makedirs(mgr, exist_ok=True)
    with open(os.path.join(mgr, "tool-calls.jsonl"), "w", encoding="utf-8") as f:
        for i in range(n_calls):
            t = rnd.randint(0, n_tasks - 1)
            st = rnd.choice(STATUSES)
            f.write(json.dumps({
                "time": f"2026-09-14T{str(i % 24).zfill(2)}:00:00Z",
                "task": f"TASK-{t:05d}", "attempt": 1,
                "session_id": f"ses-{t % max(1, n_tasks // 5)}",
                "agent": rnd.choice(AGENTS), "operation": "CALL",
                "tool": rnd.choice(TOOLS), "duration_ms": rnd.randint(5, 2000),
                "status": st, "error": "boom" if st != "ok" else None,
                "prompt_hash": "bench%08d" % i,
                "tokens_in": rnd.randint(100, 3000),
                "tokens_out": rnd.randint(50, 1500)}, ensure_ascii=False) + "\n")
    with open(os.path.join(mgr, "events.jsonl"), "w", encoding="utf-8") as f:
        for t in range(n_tasks):
            for ev in ("TASK_CREATED", "WORKER_STARTED", "TASK_DONE"):
                f.write(json.dumps({"time": "2026-09-14T08:00:00Z",
                                    "event": ev, "task": f"TASK-{t:05d}",
                                    "session_id": f"ses-{t % max(1, n_tasks // 5)}",
                                    "agent": "building"}) + "\n")
    with open(os.path.join(mgr, "queue.json"), "w", encoding="utf-8") as f:
        json.dump({"schema_version": 2, "tasks": []}, f)
    return mgr


def _patch(mod_paths):
    saved = {}
    for mod, name, value in mod_paths:
        saved[(mod, name)] = getattr(mod, name)
        setattr(mod, name, value)
    return saved


def _restore(saved):
    for (mod, name), value in saved.items():
        setattr(mod, name, value)


def bench_scale(n_calls, tmpbase):
    projs = []
    for p in range(3):
        root = os.path.join(tmpbase, f"proj{p}")
        gen_project(root, n_calls // 3 if p else n_calls - 2 * (n_calls // 3),
                    seed=100 + p)
        projs.append(root)
    mgr0 = os.path.join(projs[0], ".agent", "manager")
    saved = _patch([
        (metrics_mod, "MANAGER_DIR", mgr0),
        (metrics_mod, "EVENTS_FILE", os.path.join(mgr0, "events.jsonl")),
        (metrics_mod, "TOOLCALLS_FILE", os.path.join(mgr0, "tool-calls.jsonl")),
        (metrics_mod, "METRICS_FILE", os.path.join(mgr0, "metrics.json")),
        (metrics_mod, "HISTORY_DIR", os.path.join(mgr0, "history")),
    ])
    tracemalloc.start()
    try:
        t0 = time.perf_counter()
        metrics_mod.rebuild()
        t_rebuild = time.perf_counter() - t0
        out_data = os.path.join(projs[0], "dashboard", "data.json")
        t0 = time.perf_counter()
        export_mod.export_data(
            out_data, metrics_path=os.path.join(mgr0, "metrics.json"),
            events_path=os.path.join(mgr0, "events.jsonl"),
            toolcalls_path=os.path.join(mgr0, "tool-calls.jsonl"),
            queue_path=os.path.join(mgr0, "queue.json"),
            reviews_dir=os.path.join(mgr0, "reviews"),
            history_dir=os.path.join(mgr0, "history"),
            checkpoint_path=os.path.join(projs[0], ".agent", "building", "checkpoint.json"),
            config_path=os.path.join(mgr0, "config.json"))
        t_export = time.perf_counter() - t0
        t0 = time.perf_counter()
        payload = aggregate_mod.build_payload(projs)
        t_aggr = time.perf_counter() - t0
        t0 = time.perf_counter()
        with open(out_data, encoding="utf-8") as f:
            json.load(f)
        t_parse = time.perf_counter() - t0
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
        _restore(saved)
    calls_kb = sum(os.path.getsize(os.path.join(p, ".agent", "manager", "tool-calls.jsonl"))
                   for p in projs) / 1024.0
    return {
        "tool_calls_total": n_calls,
        "tasks": payload["combined"]["total_tasks"],
        "rebuild_s": round(t_rebuild, 2),
        "export_s": round(t_export, 2),
        "export_kb": round(os.path.getsize(out_data) / 1024.0, 1),
        "aggregate_s": round(t_aggr, 2),
        "parse_s": round(t_parse, 3),
        "calls_kb": round(calls_kb, 1),
        "peak_mb": round(peak / 1024.0 / 1024.0, 1),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Orchezer benchmarks (synthetic, tmp-only)")
    parser.add_argument("--calls", default="100000,500000,1000000",
                        help="Comma-separated tool-call scales")
    parser.add_argument("--out", default=None, help="Write results JSON here")
    args = parser.parse_args(argv)
    scales = [int(x.strip()) for x in args.calls.split(",") if x.strip()]
    results = []
    out_path = args.out
    with tempfile.TemporaryDirectory(prefix="scopeboard-bench-") as tmp:
        for n in scales:
            print(f"[bench] scale {n}...", flush=True)
            row = bench_scale(n, os.path.join(tmp, f"s{n}"))
            results.append(row)
            print(f"  rebuild={row['rebuild_s']}s export={row['export_s']}s "
                  f"aggr={row['aggregate_s']}s parse={row['parse_s']}s "
                  f"peak={row['peak_mb']}MB export_size={row['export_kb']}KB", flush=True)
            if out_path:
                with open(out_path, "w", encoding="utf-8") as f:
                    json.dump(results, f, indent=2)
                print(f"  checkpoint -> {out_path}", flush=True)
    if out_path:
        print(f"wrote {out_path}")
    print("\n| calls | rebuild | export | aggr | parse | peak MB | export KB |")
    print("|---|---|---|---|---|---|---|")
    for r in results:
        print(f"| {r['tool_calls_total']} | {r['rebuild_s']}s | {r['export_s']}s | "
              f"{r['aggregate_s']}s | {r['parse_s']}s | {r['peak_mb']} | {r['export_kb']} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
