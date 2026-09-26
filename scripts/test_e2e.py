"""test_e2e.py — Real Harness E2E, Fix v1 P0 section 3 (TASK-FIX-007).

Four scenarios through REAL manager boundaries (no mocks of manager
internals; only the external agent side is scripted):
  A. happy path: dispatch -> events -> verify -> COMPLETE
  B. session limit: recovery -> resume attempt 2 -> COMPLETE, single TASK_DONE
  C. verification failure: complete blocked, failure event, zero mutation
  D. provider unavailable: UNAVAILABLE maps to not-passed (never PASS)
Isolated tmp: patches FILE constants + MANAGER_DIR + BASELINE_DIR
+ DECISIONS_DIR + CONTEXT_DIR.
"""
import os
import json
import sqlite3
import tempfile
import shutil
import unittest
from datetime import datetime, timezone

import manager as mgr_mod
from manager import (
    ManagerOrchestrator, load_json, save_json, log_event,
    log_dispatch,
)


def _now():
    return datetime.now(timezone.utc).isoformat()


class E2EBase(unittest.TestCase):
    DIR_KEYS = ("QUEUE_FILE", "STATE_FILE", "CHECKPOINT_FILE",
                "OPERATIONS_FILE", "EVENTS_FILE", "LOCK_FILE",
                "CONFIG_FILE", "TOOLCALLS_FILE", "DECISIONS_DIR",
                "CONTEXT_DIR")

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="test_e2e_")
        self._orig = {"MANAGER_DIR": mgr_mod.MANAGER_DIR,
                      "BASELINE_DIR": mgr_mod.BASELINE_DIR}
        for key in self.DIR_KEYS:
            self._orig[key] = getattr(mgr_mod, key)
            setattr(mgr_mod, key, os.path.join(self.tmpdir, key.lower()))
        mgr_mod.MANAGER_DIR = self.tmpdir
        mgr_mod.BASELINE_DIR = os.path.join(self.tmpdir, "baselines")
        mgr_mod.DECISIONS_DIR = os.path.join(self.tmpdir, "decisions")
        mgr_mod.CONTEXT_DIR = os.path.join(self.tmpdir, "context")
        save_json(mgr_mod.STATE_FILE, {
            "schema_version": 2, "state_version": 1, "project": "test-e2e",
            "status": "running", "current_task_id": "TEST-E2E",
            "phase": "building", "worker_attempt": 1, "recovery_attempt": 0,
            "task_retry": 0, "review_cycle": 0,
            "updated_at": _now(),
            "p0_last_result": {"passed": True, "at": _now()},
        })
        save_json(mgr_mod.QUEUE_FILE, {"tasks": []})
        save_json(mgr_mod.CHECKPOINT_FILE, {})
        save_json(mgr_mod.CONFIG_FILE, {})

    def tearDown(self):
        for key, val in self._orig.items():
            try:
                setattr(mgr_mod, key, val)
            except Exception:
                pass
        try:
            shutil.rmtree(self.tmpdir)
        except Exception:
            pass

    def _add_task(self, task_id, status="pending"):
        q = load_json(mgr_mod.QUEUE_FILE, {}) or {}
        q.setdefault("tasks", []).append({"id": task_id, "status": status,
                                          "dependencies": [], "type": "build"})
        save_json(mgr_mod.QUEUE_FILE, q)

    def _events(self):
        if not os.path.exists(mgr_mod.EVENTS_FILE):
            return ""
        with open(mgr_mod.EVENTS_FILE, "r", encoding="utf-8-sig") as f:
            return f.read()

    def _full_evidence(self):
        return {k: True for k in ["requirements", "tests", "review",
                                  "blockers", "checkpoint", "state",
                                  "queue", "diff", "recovery"]}


class TestE2EHappy(E2EBase):
    def test_a_happy_path_dispatch_to_complete(self):
        task_id = "TEST-E2E-A"
        self._add_task(task_id)
        mgr = ManagerOrchestrator(owner="test-e2e-a")
        mgr.evaluate_dependencies()
        log_dispatch(task_id, "building", "ses-e2e-a1", attempt=1)
        log_event("PROGRESS", task_id, "half done")
        log_event("VERIFYING_SUCCESS", task_id, "9/9 ALL-PASS")
        ok = mgr.complete_task(task_id, self._full_evidence(), "e2e-a done")
        self.assertTrue(bool(ok))
        blob = self._events()
        seq = [json.loads(line).get("event") for line in blob.splitlines()
               if line.strip()]
        for marker in ("TASK_READY", "VERIFYING_SUCCESS", "TASK_DONE"):
            self.assertIn(marker, seq)
        self.assertLess(seq.index("TASK_READY"), seq.index("TASK_DONE"))
        q = load_json(mgr_mod.QUEUE_FILE, {})
        row = [t for t in q.get("tasks", []) if t.get("id") == task_id][0]
        self.assertEqual(row.get("status"), "completed")
        st = load_json(mgr_mod.STATE_FILE, {})
        self.assertEqual(st.get("phase"), "completed")
        # telemetry boundary: tool-call rows flow through telemetry_collect
        import telemetry_collect as tel_mod
        with open(mgr_mod.TOOLCALLS_FILE, "w", encoding="utf-8") as f:
            f.write(json.dumps({"time": _now(), "task": task_id,
                                "tool": "Read", "status": "ok"}) + "\n")
        rows = tel_mod.load_rows(mgr_mod.TOOLCALLS_FILE)
        self.assertGreaterEqual(len(rows), 1)
        self.assertIn("Read", json.dumps(rows, ensure_ascii=False))
        # SQLite read-model boundary: derived rows match source of truth
        import sqlite_sync as sync_mod
        db = os.path.join(self.tmpdir, "idx.db")
        rc = sync_mod.main(["--events", mgr_mod.EVENTS_FILE,
                            "--queue", mgr_mod.QUEUE_FILE, "--db", db])
        self.assertEqual(rc, 0)
        c = sqlite3.connect(db)
        n_ev = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        c.close()
        n_lines = len([l for l in blob.splitlines() if l.strip()])
        self.assertEqual(n_ev, n_lines)


class TestE2ELimit(E2EBase):
    def test_b_limit_recovery_resume_single_done(self):
        task_id = "TEST-E2E-B"
        self._add_task(task_id)
        mgr = ManagerOrchestrator(owner="test-e2e-b")
        log_dispatch(task_id, "building", "ses-e2e-b1", attempt=1)
        save_json(mgr_mod.CHECKPOINT_FILE,
                  {"task_id": task_id, "status": "stopped_limit"})
        res = mgr.execute_recovery(task_id, classification="LIMIT",
                                   evidence={"attempt": 1})
        self.assertIsInstance(res, dict)
        self.assertNotEqual(res.get("status"), "blocked")
        self.assertNotEqual(res.get("status"), "classify")
        st = load_json(mgr_mod.STATE_FILE, {})
        self.assertGreaterEqual(int(st.get("attempt", 1)), 1)
        self.assertTrue(os.path.exists(
            os.path.join(self.tmpdir, "recovery.jsonl")))
        # resume as attempt 2, then verify + complete
        log_dispatch(task_id, "building", "ses-e2e-b2", attempt=2)
        log_event("VERIFYING_SUCCESS", task_id, "9/9 ALL-PASS retry")
        ok = mgr.complete_task(task_id, self._full_evidence(), "e2e-b done")
        self.assertTrue(bool(ok))
        blob = self._events()
        done = [json.loads(line) for line in blob.splitlines() if line.strip()
                and json.loads(line).get("event") == "TASK_DONE"
                and json.loads(line).get("task") == task_id]
        self.assertEqual(len(done), 1)


class TestE2EVerifyFail(E2EBase):
    def test_c_verify_failure_blocks_complete(self):
        task_id = "TEST-E2E-C"
        self._add_task(task_id)
        mgr = ManagerOrchestrator(owner="test-e2e-c")
        self.assertFalse(mgr_mod._has_verifying_success(task_id))
        with open(mgr_mod.QUEUE_FILE, "rb") as f:
            q_before = f.read()
        with open(mgr_mod.STATE_FILE, "rb") as f:
            s_before = f.read()
        ok = mgr.complete_task(task_id, self._full_evidence(), "e2e-c")
        self.assertFalse(bool(ok))
        with open(mgr_mod.QUEUE_FILE, "rb") as f:
            self.assertEqual(f.read(), q_before)
        with open(mgr_mod.STATE_FILE, "rb") as f:
            self.assertEqual(f.read(), s_before)
        blob = self._events()
        self.assertIn("COMPLETE_BLOCKED", blob)
        self.assertNotIn("TASK_DONE", blob)


class TestE2EUnavailable(E2EBase):
    def test_d_provider_unavailable_never_pass(self):
        save_json(mgr_mod.CONFIG_FILE,
                  {"verification": {"enabled": True,
                                    "provider": "no-such-provider-xyz"}})
        res = mgr_mod.verify_status("TEST-E2E-D", {})
        self.assertEqual(res.status, "UNAVAILABLE")
        self.assertFalse(bool(res.passed))
        ok, _ = mgr_mod.verify_with_provider("TEST-E2E-D", {})
        self.assertFalse(bool(ok))


if __name__ == "__main__":
    unittest.main()
