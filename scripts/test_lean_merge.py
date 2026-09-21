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
import confidence_tags


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

    def test_tag_empty_defaults_assumed(self):
        self.assertEqual(confidence_tags.tag({}), "ASSUMED")

    def test_tag_verified_without_verifier_falls_back(self):
        self.assertEqual(confidence_tags.tag({"confidence": "VERIFIED"}), "ASSUMED")

    def test_verify_package_unknown_flagged(self):
        self.assertEqual(
            confidence_tags.verify_package({"Requirements": [{"confidence": "UNKNOWN"}]}),
            (False, [0]),
        )

    def test_levels_typo_single_file_low(self):
        import task_levels
        level, reasons = task_levels.classify(
            {"id": "TYPO", "title": "fix typo", "files": ["docs/guide.md"], "dependencies": []}
        )
        self.assertEqual(level, "LOW")
        self.assertTrue(reasons)

    def test_levels_db_touch_high(self):
        import task_levels
        level, reasons = task_levels.classify(
            {"id": "DB1", "title": "add column", "files": ["db/migrate/001_add_col.sql"], "dependencies": []}
        )
        self.assertEqual(level, "HIGH")
        self.assertTrue(reasons)

    def test_levels_five_files_medium(self):
        import task_levels
        level, reasons = task_levels.classify(
            {"id": "M5", "title": "general update",
             "files": ["src/m%d.py" % i for i in range(5)], "dependencies": []}
        )
        self.assertEqual(level, "MEDIUM")
        self.assertTrue(reasons)

    def test_levels_empty_task_medium_no_files(self):
        import task_levels
        level, reasons = task_levels.classify({})
        self.assertEqual(level, "MEDIUM")
        self.assertTrue(any("no files" in str(r).lower() for r in reasons))

    def test_levels_dependencies_list_not_high(self):
        import task_levels
        level, reasons = task_levels.classify(
            {"id": "DEP1", "title": "dag only", "files": ["src/a.py"], "dependencies": ["A"]}
        )
        self.assertEqual(level, "LOW")
        self.assertTrue(reasons)


    def test_verify_report_all_green_pass(self):
        import verify_report
        rep = verify_report.build_report(
            {"passed": 10, "failed": 0},
            True,
            ["scripts/verify_report.py"],
            {"all_met": True},
            {"status": "PASS"},
        )
        self.assertEqual(rep["verdict"], "PASS")
        self.assertTrue(verify_report.valid_report(rep))

    def test_verify_report_failed_tests_fail(self):
        import verify_report
        rep = verify_report.build_report(
            {"passed": 9, "failed": 1},
            True,
            ["scripts/verify_report.py"],
            {"all_met": True},
            {"status": "PASS"},
        )
        self.assertEqual(rep["verdict"], "FAIL")
        self.assertTrue(rep["reasons"])

    def test_verify_report_broken_invalid(self):
        import verify_report
        rep = verify_report.build_report(
            {"passed": 10, "failed": 0},
            True,
            ["scripts/verify_report.py"],
            {"all_met": True},
            {"status": "PASS"},
        )
        broken = dict(rep)
        broken.pop("tests")
        self.assertFalse(verify_report.valid_report(broken))

    def test_choice_single_task_single(self):
        import worker_choice
        n, reasons = worker_choice.choose_workers(1)
        self.assertEqual(n, 1)
        self.assertTrue(reasons)

    def test_choice_many_tasks_scales_five(self):
        import worker_choice
        n, reasons = worker_choice.choose_workers(
            20, conflict_pairs=0, token_budget={"remaining": 80, "total": 100})
        self.assertEqual(n, 5)
        self.assertTrue(reasons)

    def test_choice_many_tasks_clash_not_five(self):
        import worker_choice
        n, reasons = worker_choice.choose_workers(
            20, conflict_pairs=3, token_budget={"remaining": 80, "total": 100})
        self.assertIn(n, (1, 3))
        self.assertNotEqual(n, 5)
        self.assertTrue(reasons)


class TestFeedbackLoop(unittest.TestCase):
    def test_feedback_record_three_summarize_counts(self):
        import feedback_loop
        store = []
        store = feedback_loop.record_event(store, {"task": "T1", "result": "PASS", "failure": "", "owns": ["a/x.py"]})
        store = feedback_loop.record_event(store, {"task": "T2", "result": "FAIL", "failure": "TEST_FAIL", "owns": ["a/y.py"]})
        store = feedback_loop.record_event(store, {"task": "T3", "result": "FAIL", "failure": "TEST_FAIL", "owns": ["b/z.py"]})
        summary = feedback_loop.summarize(store)
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["by_result"], {"PASS": 1, "FAIL": 2})
        self.assertEqual(summary["by_failure"], {"TEST_FAIL": 2})
        self.assertEqual(summary["top_zone"], "a")

    def test_feedback_repeat_fail_suggests(self):
        import feedback_loop
        store = []
        store = feedback_loop.record_event(store, {"task": "T1", "result": "FAIL", "failure": "TEST_FAIL", "owns": ["a/x.py"]})
        store = feedback_loop.record_event(store, {"task": "T2", "result": "FAIL", "failure": "TEST_FAIL", "owns": ["a/y.py"]})
        summary = feedback_loop.summarize(store)
        tips = feedback_loop.suggest(summary)
        self.assertTrue(tips)
        self.assertTrue(any("TEST_FAIL" in str(t) for t in tips))

    def test_feedback_empty_store_empty_summary(self):
        import feedback_loop
        summary = feedback_loop.summarize([])
        self.assertEqual(summary["total"], 0)
        self.assertEqual(summary["by_result"], {})
        self.assertEqual(summary["by_failure"], {})
        self.assertEqual(feedback_loop.suggest(summary), [])


class TestFailureKinds(unittest.TestCase):
    def test_failure_limit_hit_token_limit_retryable_budget_two(self):
        import failure_kinds
        kind, retryable = failure_kinds.classify({"limit_hit": True})
        self.assertEqual(kind, "TOKEN_LIMIT")
        self.assertTrue(retryable)
        self.assertEqual(failure_kinds.budget_for(kind), 2)

    def test_failure_empty_signals_unknown_no_retry(self):
        import failure_kinds
        kind, retryable = failure_kinds.classify({})
        self.assertEqual(kind, "UNKNOWN")
        self.assertFalse(retryable)
        self.assertFalse(failure_kinds.should_retry(kind, 0))

    def test_failure_should_retry_exhausted_false(self):
        import failure_kinds
        self.assertFalse(failure_kinds.should_retry("TOKEN_LIMIT", 2))
        self.assertFalse(failure_kinds.should_retry("MODEL_ERROR", 3))
        self.assertFalse(failure_kinds.should_retry("UNKNOWN", 0))


class TestRequireVerification(unittest.TestCase):
    def test_verification_string_ok(self):
        import verify_report
        ok, missing = verify_report.require_verification({"verification": "pytest scripts/ -q"})
        self.assertTrue(ok)
        self.assertEqual(missing, [])

    def test_empty_contract_not_ok(self):
        import verify_report
        ok, missing = verify_report.require_verification({})
        self.assertFalse(ok)
        self.assertTrue(missing)

    def test_acceptance_review_without_verification_ok(self):
        import verify_report
        ok, missing = verify_report.require_verification(
            {"acceptance": ["done"], "review": {"status": "PASS"}}
        )
        self.assertTrue(ok)
        self.assertEqual(missing, [])


class TestTaskContract(unittest.TestCase):
    def test_contract_full_with_verification_ok(self):
        import task_contract
        contract = {
            "id": "C1",
            "objective": "ship feature",
            "acceptance": ["done"],
            "files": ["src/a.py"],
            "verification": "pytest scripts/ -q",
        }
        ok, missing, level = task_contract.validate_contract(contract)
        self.assertTrue(ok, missing)
        self.assertEqual(missing, [])
        self.assertIn(level, ("LOW", "MEDIUM", "HIGH"))

    def test_contract_high_without_verification_not_ok(self):
        import task_contract
        contract = {
            "id": "DBX",
            "objective": "migrate db",
            "acceptance": ["done"],
            "files": ["db/migrate/001.sql"],
        }
        ok, missing, level = task_contract.validate_contract(contract)
        self.assertEqual(level, "HIGH")
        self.assertFalse(ok)
        self.assertIn("verification", missing)

    def test_contract_low_without_verification_ok(self):
        import task_contract
        contract = {
            "id": "LOW1",
            "objective": "fix typo",
            "acceptance": ["done"],
            "files": ["docs/guide.md"],
        }
        ok, missing, level = task_contract.validate_contract(contract)
        self.assertEqual(level, "LOW")
        self.assertTrue(ok, missing)
        self.assertEqual(missing, [])

    def test_build_report_single_string_file(self):
        import verify_report
        rep = verify_report.build_report(
            {"passed": 10, "failed": 0},
            True,
            "scripts/verify_report.py",
            {"all_met": True},
            {"status": "PASS"},
        )
        self.assertEqual(rep["files"], ["scripts/verify_report.py"])
        self.assertEqual(rep["verdict"], "PASS")


class TestFeedbackRecordImmutability(unittest.TestCase):
    def test_record_returns_new_store(self):
        import feedback_loop
        store = []
        result = feedback_loop.record_event(
            store, {"task": "T1", "result": "PASS", "failure": "", "owns": ["a/x.py"]})
        self.assertIsNot(result, store)
        self.assertEqual(len(store), 0)
        self.assertEqual(len(result), 1)


if __name__ == "__main__":
    unittest.main()
