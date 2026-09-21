"""test_lean_merge.py — CU-08M merge validation (additive only).

Covers: 5 merged modules importable, compile 7 fields, cache
hit_rate/invalidate, waves [A,B,D]->[C]->[E], assign 1 batch,
batch seq 1-5, assert_mvp_boundary still passes.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import manager as manager_mod
import ctx_compiler
import ctx_cache
import dag_waves
import lean_flow
import batch_seq


def _demo_tasks():
    return [
        {"id": "A", "title": "a", "owns": ["a/*.py"], "dependencies": [], "status": "pending"},
        {"id": "B", "title": "b", "owns": ["b/*.py"], "dependencies": [], "status": "pending"},
        {"id": "D", "title": "d", "owns": ["d/*.py"], "dependencies": [], "status": "pending"},
        {"id": "C", "title": "c", "owns": ["c/*.py"], "dependencies": ["A", "B"], "status": "pending"},
        {"id": "E", "title": "e", "owns": ["e/*.py"], "dependencies": ["C"], "status": "pending"},
    ]


class TestLeanMerge(unittest.TestCase):
    def test_modules_importable(self):
        for mod in (ctx_compiler, ctx_cache, dag_waves, lean_flow, batch_seq):
            self.assertTrue(hasattr(mod, "__name__"))

    def test_compile_seven_fields(self):
        task = {"id": "T1", "title": "Do thing", "owns": ["a/*.py"], "dependencies": []}
        global_context = {
            "artifact": "ctx://global",
            "version": 1,
            "data": {
                "project": "P",
                "language_runtime": "py",
                "conventions": ["c1"],
                "safety": ["s1"],
            },
        }
        schemas = {"context": {"artifacts": [
            {"artifact": "ctx://tasks/T1", "data": {
                "objective": "Do thing",
                "acceptance": ["done"],
                "constraints": [],
            }},
            {"layer": "file", "data": {"path": "a/x.py", "scope": "edit"}},
        ]}}
        pkg = ctx_compiler.compile_task(task, schemas=schemas, global_context=global_context)
        self.assertEqual(
            sorted(pkg.keys()),
            ["Constraints", "Dependencies", "Files", "Project", "Requirements", "Risks", "Task"],
        )
        ok, missing = ctx_compiler.validate_completeness(pkg)
        self.assertTrue(ok, missing)
        self.assertGreater(ctx_compiler.estimate_tokens(pkg), 0)
        ok2, missing2 = lean_flow.manager_validate(pkg)
        self.assertTrue(ok2, missing2)
        pkg2 = lean_flow.worker_compile(task, schemas)
        self.assertIn("Project", pkg2)

    def test_cache_hit_rate_invalidate(self):
        ctx_cache.clear()
        ctx_cache.store("a", {"v": 1})
        ctx_cache.store("b", {"v": 2}, depends_on=["a"])
        self.assertIsNotNone(ctx_cache.lookup("a"))
        self.assertIsNotNone(ctx_cache.lookup("a"))
        st = ctx_cache.stats()
        self.assertGreater(st["hit_rate"], 0.0)
        affected = ctx_cache.invalidate("a")
        self.assertIn("a", affected)
        self.assertIn("b", affected)
        self.assertIsNone(ctx_cache.lookup("a"))
        ctx_cache.clear()

    def test_waves_topology(self):
        waves = dag_waves.build_waves(_demo_tasks())
        self.assertEqual(waves, [["A", "B", "D"], ["C"], ["E"]])

    def test_assign_single_batch(self):
        tasks = _demo_tasks()[:3]
        plan = dag_waves.assign(tasks)
        self.assertEqual(len(plan["batches"]), 1)
        self.assertEqual(sorted(plan["batches"][0]), ["A", "B", "D"])
        plan2 = lean_flow.dispatch_wave(tasks)
        self.assertEqual(len(plan2["batches"]), 1)

    def test_batch_seq_one_to_five(self):
        batch_seq.reset()
        res = batch_seq.batch_append([{"e": i} for i in range(1, 6)])
        self.assertEqual(res, {"start": 1, "end": 5, "count": 5})
        self.assertEqual(batch_seq.current_seq(), 5)
        self.assertTrue(batch_seq.snapshot_policy(10))
        batch_seq.reset()

    def test_mvp_boundary_still_passes(self):
        manager_mod.assert_mvp_boundary()

    def test_assign_same_zone_no_capacity_skips(self):
        tasks = [
            {"id": "S%d" % i, "title": "s", "owns": ["src/same/mod.py"], "dependencies": [], "status": "pending"}
            for i in range(3)
        ]
        plan = dag_waves.assign(tasks, capacity_hint=10)
        self.assertEqual(len(plan["batches"]), 3)
        self.assertTrue(plan["conflicts"])
        self.assertEqual(plan["capacity_skips"], 0)

    def test_assign_capacity_bound_counts_skips(self):
        tasks = [
            {"id": "T%02d" % i, "title": "t", "owns": ["src/zone-%02d/mod.py" % (i % 10)], "dependencies": [], "status": "pending"}
            for i in range(20)
        ]
        plan = dag_waves.assign(tasks, capacity_hint=5)
        self.assertEqual(len(plan["batches"]), 4)
        self.assertGreater(plan["capacity_skips"], 0)

    def test_validate_empty_pkg_missing_complete(self):
        ok, missing = ctx_compiler.validate_completeness({})
        self.assertFalse(ok)
        for key in ("Task", "Task.id", "Task.acceptance"):
            self.assertIn(key, missing)


if __name__ == "__main__":
    unittest.main()
