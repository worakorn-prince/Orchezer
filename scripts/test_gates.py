"""test_gates.py — GATE-01 enforcing gates (moved from test_hardening.py).

Covers: misroute blocked / unknown warning (+ end-to-end passthrough) /
complete blocked / superseded FIXV217 gate.
Uses isolated tmp for queue/state/checkpoint/operations to avoid polluting real files.
"""
import os
import json
import tempfile
import shutil
import unittest
from datetime import datetime, timezone

import manager as mgr_mod
from manager import (
    ManagerOrchestrator, load_json, save_json, log_event,
    log_dispatch,
)


class IsolatedGateTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="test_gates_")
        self._backups = {}
        for key, path in [
            ("queue", mgr_mod.QUEUE_FILE),
            ("state", mgr_mod.STATE_FILE),
            ("checkpoint", mgr_mod.CHECKPOINT_FILE),
            ("operations", mgr_mod.OPERATIONS_FILE),
            ("events", mgr_mod.EVENTS_FILE),
            ("lock", mgr_mod.LOCK_FILE),
        ]:
            if os.path.exists(path):
                bak = os.path.join(self.tmpdir, os.path.basename(path) + ".bak")
                shutil.copy2(path, bak)
                self._backups[key] = (path, bak)
        self._orig = {
            "queue": mgr_mod.QUEUE_FILE,
            "state": mgr_mod.STATE_FILE,
            "checkpoint": mgr_mod.CHECKPOINT_FILE,
            "operations": mgr_mod.OPERATIONS_FILE,
            "events": mgr_mod.EVENTS_FILE,
            "lock": mgr_mod.LOCK_FILE,
        }
        self.tmp_queue = os.path.join(self.tmpdir, "queue.json")
        self.tmp_state = os.path.join(self.tmpdir, "state.json")
        self.tmp_checkpoint = os.path.join(self.tmpdir, "checkpoint.json")
        self.tmp_operations = os.path.join(self.tmpdir, "operations.jsonl")
        self.tmp_events = os.path.join(self.tmpdir, "events.jsonl")
        self.tmp_lock = os.path.join(self.tmpdir, "manager.lock")
        mgr_mod.QUEUE_FILE = self.tmp_queue
        mgr_mod.STATE_FILE = self.tmp_state
        mgr_mod.CHECKPOINT_FILE = self.tmp_checkpoint
        mgr_mod.OPERATIONS_FILE = self.tmp_operations
        mgr_mod.EVENTS_FILE = self.tmp_events
        mgr_mod.LOCK_FILE = self.tmp_lock
        import scripts.manager as _m
        save_json(self.tmp_state, {
            "schema_version": 2,
            "state_version": 1,
            "event_sequence": 1,
            "project": "test",
            "status": "running",
            "current_task_id": "TEST-GATE",
            "phase": "building",
            "worker_attempt": 1,
            "recovery_attempt": 0,
            "task_retry": 0,
            "review_cycle": 0,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "p0_last_result": {"passed": True, "at": datetime.now(timezone.utc).isoformat()},
        })
        save_json(self.tmp_queue, {"tasks": []})
        save_json(self.tmp_checkpoint, {})

    def tearDown(self):
        for key, orig in self._orig.items():
            try:
                setattr(mgr_mod, f"{key.upper()}_FILE" if key != 'queue' else 'QUEUE_FILE', orig)
                if key == "queue":
                    mgr_mod.QUEUE_FILE = orig
                elif key == "state":
                    mgr_mod.STATE_FILE = orig
                elif key == "checkpoint":
                    mgr_mod.CHECKPOINT_FILE = orig
                elif key == "operations":
                    mgr_mod.OPERATIONS_FILE = orig
                elif key == "events":
                    mgr_mod.EVENTS_FILE = orig
                elif key == "lock":
                    mgr_mod.LOCK_FILE = orig
            except Exception:
                pass
        for key, (orig, bak) in self._backups.items():
            try:
                shutil.copy2(bak, orig)
            except Exception:
                pass
        try:
            shutil.rmtree(self.tmpdir)
        except Exception:
            pass

    def test_FIXV217_dispatch_misroute_warns_not_blocks(self):
        # superseded by GATE-01 (user-approved enforcing gates)
        res = log_dispatch("TEST-FIXV217", "review", "sess-rt-1", attempt=1, task_type="code")
        blocked = isinstance(res, dict) and res.get("blocked") is True
        self.assertTrue(blocked)

    def test_GATE01_complete_without_verifying_success_blocked(self):
        """GATE-01: fresh task without VERIFYING_SUCCESS must block complete with zero mutation."""
        mgr = ManagerOrchestrator(owner="test-gate01-a")
        task_id = "TEST-GATE01-NOVERIFY"
        self.assertFalse(mgr_mod._has_verifying_success(task_id))
        ev = {k: True for k in ["requirements", "tests", "review", "blockers", "checkpoint", "state", "queue", "diff", "recovery"]}
        with open(self.tmp_queue, "rb") as f:
            q_before = f.read()
        with open(self.tmp_state, "rb") as f:
            s_before = f.read()
        ev_before = open(self.tmp_events, encoding="utf-8").read() if os.path.exists(self.tmp_events) else ""
        ok = mgr.complete_task(task_id, ev, "gate01 decision")
        self.assertFalse(ok)
        with open(self.tmp_queue, "rb") as f:
            self.assertEqual(f.read(), q_before)
        with open(self.tmp_state, "rb") as f:
            self.assertEqual(f.read(), s_before)
        ev_after = open(self.tmp_events, encoding="utf-8").read()[len(ev_before):]
        self.assertIn("COMPLETE_BLOCKED", ev_after)
        self.assertNotIn("TASK_DONE", ev_after)

    def test_GATE01_unknown_type_warning_passthrough(self):
        from manager import check_routing
        res = check_routing("BOGUS-XYZ-UNKNOWN-TYPE", "building")
        self.assertTrue(res.get("allowed"))
        self.assertEqual(res.get("action"), "WARNING")
        res_e2e = log_dispatch("TEST-GATE01-UNKNOWN-E2E", "building", "sess-unknown-e2e", attempt=1, task_type="BOGUS-XYZ-UNKNOWN-TYPE")
        blocked = isinstance(res_e2e, dict) and res_e2e.get("blocked") is True
        self.assertFalse(blocked)


if __name__ == "__main__":
    unittest.main()
