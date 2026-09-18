import os
import json
import unittest
from datetime import datetime, timezone
from manager import (
    ManagerOrchestrator, load_json, save_json, log_event,
    write_decision, generate_resume_context,
    STATE_FILE, QUEUE_FILE, CONFIG_FILE, EVENTS_FILE, CHECKPOINT_FILE,
    DECISIONS_DIR, CONTEXT_DIR
)

class TestManagerUpgrade(unittest.TestCase):
    def setUp(self):
        os.makedirs(".agent/manager", exist_ok=True)
        os.makedirs(".agent/building", exist_ok=True)
        os.makedirs(DECISIONS_DIR, exist_ok=True)
        os.makedirs(CONTEXT_DIR, exist_ok=True)
        # FIX-01: isolate destructive tests — backup real queue/state/checkpoint
        import tempfile, shutil
        self._tmpdir = tempfile.mkdtemp(prefix="test_upgrade_")
        # backup real files if exist
        self._backups = {}
        import manager as _mgr
        for key, path in [("queue", _mgr.QUEUE_FILE), ("state", _mgr.STATE_FILE), ("checkpoint", _mgr.CHECKPOINT_FILE)]:
            if os.path.exists(path):
                bak = os.path.join(self._tmpdir, os.path.basename(path) + ".bak")
                shutil.copy2(path, bak)
                self._backups[key] = (path, bak)
        # redirect queue/state/checkpoint to tmp files for isolation
        self._orig_queue = _mgr.QUEUE_FILE
        self._orig_state = _mgr.STATE_FILE
        self._orig_checkpoint = _mgr.CHECKPOINT_FILE
        self._tmp_queue = os.path.join(self._tmpdir, "queue.json")
        self._tmp_state = os.path.join(self._tmpdir, "state.json")
        self._tmp_checkpoint = os.path.join(self._tmpdir, "checkpoint.json")
        _mgr.QUEUE_FILE = self._tmp_queue
        _mgr.STATE_FILE = self._tmp_state
        _mgr.CHECKPOINT_FILE = self._tmp_checkpoint
        # also patch globals in this module
        global QUEUE_FILE, STATE_FILE, CHECKPOINT_FILE
        QUEUE_FILE = self._tmp_queue
        STATE_FILE = self._tmp_state
        CHECKPOINT_FILE = self._tmp_checkpoint
        # init tmp files with backups or defaults
        for key, path in [("queue", self._orig_queue), ("state", self._orig_state), ("checkpoint", self._orig_checkpoint)]:
            if key in self._backups:
                shutil.copy2(self._backups[key][1], getattr(self, f"_tmp_{key}"))
        # also patch ManagerOrchestrator to use tmp files via reload? Instead, ensure new instances read tmp
        # ensure checkpoint dir exists

    def tearDown(self):
        import manager as _mgr
        # restore globals
        _mgr.QUEUE_FILE = self._orig_queue
        _mgr.STATE_FILE = self._orig_state
        _mgr.CHECKPOINT_FILE = self._orig_checkpoint
        global QUEUE_FILE, STATE_FILE, CHECKPOINT_FILE
        QUEUE_FILE = self._orig_queue
        STATE_FILE = self._orig_state
        CHECKPOINT_FILE = self._orig_checkpoint
        # restore backups if needed (queue already restored by not overwriting real file, but ensure)
        import shutil
        for key, (orig, bak) in self._backups.items():
            try:
                shutil.copy2(bak, orig)
            except Exception:
                pass
        # cleanup tmp
        import shutil as _sh
        try:
            _sh.rmtree(self._tmpdir)
        except Exception:
            pass

    def test_u1_watchdog_detection(self):
        mgr = ManagerOrchestrator(owner="test-runner")
        # Save a stopped_limit checkpoint
        save_json(CHECKPOINT_FILE, {
            "task_id": "TEST-TASK-001",
            "status": "stopped_limit",
            "phase": "implementation",
            "updated_at": datetime.now(timezone.utc).isoformat()
        })
        health = mgr.check_watchdog()
        self.assertEqual(health, "RECOVERY_NEEDED")

    def test_u2_idempotent_recovery(self):
        mgr = ManagerOrchestrator(owner="test-runner")
        initial_attempt = mgr.state.get("attempt", 1)
        save_json(CHECKPOINT_FILE, {
            "task_id": "TEST-TASK-001",
            "status": "stopped_limit",
            "completed": ["schema setup"],
            "remaining": ["api integration"],
            "next_action": "implement api endpoint"
        })
        mgr.execute_recovery("TEST-TASK-001")
        self.assertEqual(mgr.state.get("phase"), "recovering")
        self.assertEqual(mgr.state.get("attempt"), initial_attempt + 1)
        ctx_file = os.path.join(CONTEXT_DIR, "TEST-TASK-001.resume.md")
        self.assertTrue(os.path.exists(ctx_file))

    def test_u3_event_log(self):
        test_event = "TEST_EVENT_LOG"
        log_event(test_event, "TEST-TASK", "Testing append-only event log")
        self.assertTrue(os.path.exists(EVENTS_FILE))
        with open(EVENTS_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        self.assertTrue(any(test_event in line for line in lines))

    def test_u4_context_manager(self):
        chk = {
            "phase": "building",
            "completed": ["db migration"],
            "remaining": ["tests"],
            "next_action": "run pytest"
        }
        path = generate_resume_context("TEST-CTX", "Test Context Task", chk)
        self.assertTrue(os.path.exists(path))
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("Test Context Task", content)
        self.assertIn("db migration", content)

    def test_u5_policy_and_approval(self):
        mgr = ManagerOrchestrator(owner="test-runner")
        # Check high risk approval gate
        is_required = mgr.check_approval_required("TEST-HIGH-RISK", risk_level="HIGH")
        self.assertTrue(is_required)
        self.assertEqual(mgr.state.get("status"), "approval_required")

    def test_u6_dependency_queue(self):
        mgr = ManagerOrchestrator(owner="test-runner")
        test_queue = {
            "tasks": [
                {"id": "DEP-1", "status": "completed", "dependencies": []},
                {"id": "DEP-2", "status": "pending", "dependencies": ["DEP-1"]}
            ]
        }
        save_json(QUEUE_FILE, test_queue)
        mgr.queue = test_queue
        mgr.evaluate_dependencies()
        q = load_json(QUEUE_FILE)
        task2 = next(t for t in q["tasks"] if t["id"] == "DEP-2")
        self.assertEqual(task2["status"], "ready")

    def test_u7_decision_log(self):
        write_decision("TEST-DECISION", "Routing decision: Planning -> Building -> Review")
        dec_file = os.path.join(DECISIONS_DIR, "TEST-DECISION.md")
        self.assertTrue(os.path.exists(dec_file))
        with open(dec_file, "r", encoding="utf-8") as f:
            text = f.read()
        self.assertIn("Routing decision", text)

if __name__ == "__main__":
    unittest.main()
