import hashlib
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
ROOT = os.path.dirname(HERE)

import manager as manager_mod
from manager import log_tool_call, log_dispatch, wrap_task, save_review, log_denied_attempt
import metrics as metrics_mod
import export_json as export_mod
import aggregate as aggregate_mod
import tools_inventory as tools_mod


REQUIRED_TOOLCALL_FIELDS = {
    "time", "task", "attempt", "session_id", "agent",
    "operation", "tool", "duration_ms", "status", "error",
    "prompt_hash", "tokens_in", "tokens_out",
}


def read_jsonl(path):
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


class TestLoggingIsolated(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.toolcalls = os.path.join(self.tmp.name, "tool-calls.jsonl")
        self.events = os.path.join(self.tmp.name, "events.jsonl")
        self.metrics_file = os.path.join(self.tmp.name, "metrics.json")
        self._orig = {
            "manager_dir": manager_mod.MANAGER_DIR,
            "toolcalls": manager_mod.TOOLCALLS_FILE,
            "events": manager_mod.EVENTS_FILE,
            "m_metrics_dir": metrics_mod.MANAGER_DIR,
            "m_metrics_events": metrics_mod.EVENTS_FILE,
            "m_metrics_calls": metrics_mod.TOOLCALLS_FILE,
            "m_metrics_out": metrics_mod.METRICS_FILE,
            "m_metrics_hist": metrics_mod.HISTORY_DIR,
        }
        manager_mod.MANAGER_DIR = self.tmp.name
        manager_mod.TOOLCALLS_FILE = self.toolcalls
        manager_mod.EVENTS_FILE = self.events
        metrics_mod.MANAGER_DIR = self.tmp.name
        metrics_mod.EVENTS_FILE = self.events
        metrics_mod.TOOLCALLS_FILE = self.toolcalls
        metrics_mod.METRICS_FILE = self.metrics_file
        metrics_mod.HISTORY_DIR = os.path.join(self.tmp.name, "history")

    def tearDown(self):
        manager_mod.MANAGER_DIR = self._orig["manager_dir"]
        manager_mod.TOOLCALLS_FILE = self._orig["toolcalls"]
        manager_mod.EVENTS_FILE = self._orig["events"]
        metrics_mod.MANAGER_DIR = self._orig["m_metrics_dir"]
        metrics_mod.EVENTS_FILE = self._orig["m_metrics_events"]
        metrics_mod.TOOLCALLS_FILE = self._orig["m_metrics_calls"]
        metrics_mod.METRICS_FILE = self._orig["m_metrics_out"]
        metrics_mod.HISTORY_DIR = self._orig["m_metrics_hist"]
        self.tmp.cleanup()

    def test_a_log_tool_call_schema(self):
        prompt = "TEST-LOG-A read design section"
        entry = log_tool_call(
            "TEST-LOG-A", "ses_test_a", "building", "CALL", "read",
            duration_ms=0, status="ok", error=None,
            prompt_text=prompt, attempt=1,
        )
        self.assertEqual(set(entry.keys()), REQUIRED_TOOLCALL_FIELDS)
        self.assertEqual(len(REQUIRED_TOOLCALL_FIELDS), 13)
        expected_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]
        self.assertEqual(entry["prompt_hash"], expected_hash)
        self.assertEqual(len(entry["prompt_hash"]), 12)
        with open(self.toolcalls, "r", encoding="utf-8") as f:
            raw = f.read()
        self.assertNotIn(prompt, raw)
        secret_prompt = "TEST-LOG-A edit file with sk-super-secret-999"
        log_tool_call(
            "TEST-LOG-A", "ses_test_a", "building", "CALL", "edit",
            duration_ms=10, status="ok", error=None,
            prompt_text=secret_prompt, attempt=1,
        )
        with open(self.toolcalls, "r", encoding="utf-8") as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["prompt_hash"], expected_hash)
        full_body = "\n".join(lines)
        self.assertNotIn(secret_prompt, full_body)
        self.assertNotIn("sk-super-secret-999", full_body)
        second = json.loads(lines[1])
        self.assertEqual(len(second["prompt_hash"]), 12)

    def test_b_log_dispatch_pair(self):
        log_dispatch("TEST-LOG-B", "building", "ses_test_b", attempt=2,
                     prompt_text="TEST-LOG-B dispatch")
        calls = read_jsonl(self.toolcalls)
        self.assertTrue(any(
            r.get("task") == "TEST-LOG-B" and r.get("operation") == "DISPATCH"
            for r in calls
        ))
        events = read_jsonl(self.events)
        kinds = {(e.get("event"), e.get("task")) for e in events}
        self.assertIn(("TOOL_CALL", "TEST-LOG-B"), kinds)
        self.assertIn(("WORKER_STARTED", "TEST-LOG-B"), kinds)
        started = [e for e in events if e.get("event") == "WORKER_STARTED"
                   and e.get("task") == "TEST-LOG-B"]
        self.assertEqual(started[0].get("session_id"), "ses_test_b")

    def test_c_wrap_task_ok_and_error(self):
        result = wrap_task("TEST-LOG-C", "building", "ses_test_c", "read",
                           "TEST-LOG-C ok prompt", lambda: 42, attempt=1)
        self.assertEqual(result, 42)
        calls = read_jsonl(self.toolcalls)
        ok_rows = [r for r in calls if r.get("task") == "TEST-LOG-C"]
        self.assertEqual(len(ok_rows), 1)
        self.assertEqual(ok_rows[0]["operation"], "RESULT")
        self.assertEqual(ok_rows[0]["status"], "ok")
        self.assertIsNone(ok_rows[0]["error"])
        self.assertIsInstance(ok_rows[0]["duration_ms"], int)
        self.assertGreaterEqual(ok_rows[0]["duration_ms"], 0)
        with self.assertRaises(ValueError):
            wrap_task("TEST-LOG-C", "building", "ses_test_c", "test",
                      "TEST-LOG-C err prompt",
                      lambda: (_ for _ in ()).throw(ValueError("boom-test-xyz")),
                      attempt=1)
        calls = read_jsonl(self.toolcalls)
        err_rows = [r for r in calls if r.get("task") == "TEST-LOG-C"
                    and r.get("status") == "error"]
        self.assertEqual(len(err_rows), 1)
        self.assertIn("boom-test-xyz", err_rows[0]["error"])

    def test_d_metrics_rebuild_formulas(self):
        t0 = "2026-09-14T07:00:00Z"
        t1 = "2026-09-14T08:00:00Z"
        events = [
            {"event": "TASK_CREATED", "task": "TASK-M1", "time": t0},
            {"event": "WORKER_STARTED", "task": "TASK-M1", "time": t0},
            {"event": "WORKER_RESUMED", "task": "TASK-M1", "time": t0},
            {"event": "RECOVERY_STARTED", "task": "TASK-M1", "time": t0},
            {"event": "REVIEW_FAILED", "task": "TASK-M1", "time": t0},
            {"event": "REVIEW_FAILED", "task": "TASK-M1", "time": t0},
            {"event": "TASK_DONE", "task": "TASK-M1", "time": t1},
            {"event": "TASK_CREATED", "task": "TASK-M2", "time": t0},
            {"event": "WORKER_STARTED", "task": "TASK-M2", "time": t0},
            {"event": "TASK_FAILED", "task": "TASK-M2", "time": t1},
            {"event": "TASK_CANCELLED", "task": "TASK-M3", "time": t1},
        ]
        toolcalls = [
            {"task": "TASK-M1", "operation": "CALL", "tokens_in": 100, "tokens_out": 50},
            {"task": "TASK-M1", "operation": "CALL", "tokens_in": 10, "tokens_out": 5},
            {"task": "TASK-M1", "operation": "CALL"},
            {"task": "TASK-M2", "operation": "CALL", "tokens_in": 7, "tokens_out": 3},
        ]
        per_task, totals, efficiency, agent_efficiency = metrics_mod.compute_metrics(events, toolcalls)
        m1 = per_task["TASK-M1"]
        self.assertEqual(m1["tokens_total"], 165)
        self.assertEqual(totals["tokens_total"], 175)
        self.assertIn("token_per_success", efficiency)
        self.assertIn("review_rework_rate", efficiency)
        self.assertIsInstance(agent_efficiency, list)
        m1 = per_task["TASK-M1"]
        self.assertEqual(m1["sessions_per_task"], 2)
        self.assertEqual(m1["tool_calls_per_task"], 3)
        self.assertEqual(m1["recovery_count"], 1)
        self.assertEqual(m1["review_loop"], 2)
        self.assertEqual(m1["time_to_DONE"], 3600.0)
        self.assertTrue(m1["done"])
        m2 = per_task["TASK-M2"]
        self.assertEqual(m2["sessions_per_task"], 1)
        self.assertEqual(m2["tool_calls_per_task"], 1)
        self.assertIsNone(m2["time_to_DONE"])
        self.assertFalse(m2["done"])
        self.assertAlmostEqual(totals["success_rate"], 1 / 3)
        with open(self.events, "w", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")
        with open(self.toolcalls, "w", encoding="utf-8") as f:
            for r in toolcalls:
                f.write(json.dumps(r) + "\n")
        first = metrics_mod.rebuild()
        second = metrics_mod.rebuild()
        self.assertEqual(first["per_task"], second["per_task"])
        self.assertEqual(first["totals"], second["totals"])
        self.assertTrue(os.path.exists(self.metrics_file))

    def test_d2_metrics_excludes_test_dep_prefix(self):
        t0 = "2026-09-14T07:00:00Z"
        t1 = "2026-09-14T08:00:00Z"
        events = [
            {"event": "TASK_CREATED", "task": "TASK-REAL", "time": t0},
            {"event": "WORKER_STARTED", "task": "TASK-REAL", "time": t0},
            {"event": "TASK_DONE", "task": "TASK-REAL", "time": t1},
            {"event": "TASK_CREATED", "task": "TEST-FAKE", "time": t0},
            {"event": "TASK_DONE", "task": "TEST-FAKE", "time": t1},
            {"event": "TASK_CREATED", "task": "DEP-9", "time": t0},
            {"event": "TASK_DONE", "task": "DEP-9", "time": t1},
        ]
        toolcalls = [
            {"task": "TASK-REAL", "operation": "CALL"},
            {"task": "TEST-FAKE", "operation": "CALL"},
            {"task": "DEP-9", "operation": "CALL"},
        ]
        per_task, totals, _, _ = metrics_mod.compute_metrics(events, toolcalls)
        self.assertEqual(sorted(per_task.keys()), ["TASK-REAL"])
        self.assertEqual(totals["tasks"], 1)
        self.assertNotIn("TEST-FAKE", per_task)
        self.assertNotIn("DEP-9", per_task)

    def test_d3_metrics_excludes_toolcall_only_prefix(self):
        events = [
            {"event": "TASK_CREATED", "task": "TASK-REAL", "time": "2026-09-14T07:00:00Z"},
        ]
        toolcalls = [
            {"task": "TASK-REAL", "operation": "CALL"},
            {"task": "TEST-ONLY", "operation": "CALL"},
            {"task": "DEP-ONLY", "operation": "CALL"},
        ]
        per_task, totals, _, _ = metrics_mod.compute_metrics(events, toolcalls)
        self.assertEqual(sorted(per_task.keys()), ["TASK-REAL"])
        self.assertEqual(totals["tasks"], 1)

    def test_e_export_json_payload(self):
        metrics = {
            "totals": {"tasks": 2, "done": 1, "failed": 1, "cancelled": 0,
                       "success_rate": 0.5, "tool_calls": 4,
                       "recoveries": 1, "review_loops": 2},
            "efficiency": {"task_efficiency": 0.5, "token_per_success": 100.0,
                           "tools_per_success": 4.0, "recovery_cost_ratio": 0.25,
                           "session_efficiency": 0.5, "review_rework_rate": 1.0},
            "agent_efficiency": [{"agent": "building", "tasks": 2, "success_rate": 0.5,
                                  "avg_tokens": 50.0, "avg_tools": 2.0,
                                  "avg_time_s": 30.0, "recovery_rate": 0.5,
                                  "low_sample": True}],
            "per_task": {
                "TEST-E1": {"sessions_per_task": 2, "tool_calls_per_task": 3,
                            "recovery_count": 1, "review_loop": 2,
                            "time_to_DONE": 3600.0, "done": True},
                "TEST-E2": {"sessions_per_task": 1, "tool_calls_per_task": 1,
                            "recovery_count": 0, "review_loop": 0,
                            "time_to_DONE": None, "done": False},
            },
        }
        events = [
            {"time": "2026-09-14T07:00:00Z", "event": "TASK_CREATED", "task": "TEST-E1"},
            {"time": "2026-09-14T08:00:00Z", "event": "TASK_DONE", "task": "TEST-E1"},
        ]
        payload = export_mod.build_payload(metrics, events)
        self.assertEqual(set(payload.keys()),
                         {"generated_at", "kpis", "per_task", "timeline",
                          "recent_activity", "errors", "durations", "agents",
                          "slowest_tasks", "queue", "files_changed", "findings",
                          "alerts", "trend", "execution", "efficiency",
                          "agent_efficiency", "risk", "failure", "contract", "tools"})
        for key in ("total_tasks", "done", "failed", "cancelled",
                    "success_rate", "total_tool_calls", "recoveries", "review_fails",
                    "tokens_total", "open_alerts"):
            self.assertIn(key, payload["kpis"])
        self.assertEqual(payload["kpis"]["total_tasks"], 2)
        self.assertAlmostEqual(payload["kpis"]["success_rate"], 0.5)
        self.assertEqual(len(payload["per_task"]), 2)
        for row in payload["per_task"]:
            self.assertEqual(
                set(row.keys()),
                {"task", "done", "sessions", "tool_calls",
                 "recovery", "review_loop", "time_to_DONE", "tokens",
                 "recovery_tokens", "review_passed", "denied"},
            )
        self.assertIn("token_per_success", payload["efficiency"])
        self.assertIsInstance(payload["agent_efficiency"], list)
        self.assertEqual(len(payload["timeline"]), 2)
        self.assertEqual(
            set(payload["timeline"][0].keys()), {"time", "event", "task"})
        out_path = os.path.join(self.tmp.name, "data.json")
        export_mod.export_data(out_path,
                               metrics_path=self.metrics_file,
                               events_path=self.events)
        with open(self.metrics_file, "w", encoding="utf-8") as f:
            json.dump(metrics, f)
        with open(self.events, "w", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")
        exported = export_mod.export_data(out_path,
                                          metrics_path=self.metrics_file,
                                          events_path=self.events)
        self.assertTrue(os.path.exists(out_path))
        with open(out_path, "r", encoding="utf-8") as f:
            on_disk = json.load(f)
        self.assertEqual(set(on_disk.keys()), set(payload.keys()))
        self.assertEqual(exported["kpis"], on_disk["kpis"])
        self.assertIn("tools", on_disk)
        self.assertIn("matrix", on_disk["tools"])

    def test_f_dashboard_static(self):
        html_path = os.path.join(ROOT, "dashboard", "index.html")
        self.assertTrue(os.path.exists(html_path))
        with open(html_path, "r", encoding="utf-8") as f:
            html = f.read()
        for canvas_id in ("ch-calls", "ch-timeline", "ch-pie", "ch-multi", "ch-trend"):
            self.assertIn(canvas_id, html)
        self.assertIn("data.json", html)
        self.assertTrue('fetch("data.json"' in html or "fetch('data.json'" in html)
        self.assertIn("typeof Chart", html)
        self.assertTrue("cdn-warning" in html or "load-error" in html)
        for fn in ("function renderKpis", "function renderTable",
                   "function renderExtra", "function renderCharts"):
            self.assertIn(fn, html)
        for table_id in ("tbl-act", "tbl-err", "tbl-queue", "tbl-dur",
                         "tbl-agent", "tbl-slow", "tbl-find",
                         "tbl-tools", "tbl-perm", "tbl-exec",
                         "tbl-eff", "tbl-agent-eff", "tbl-risk", "tbl-fail",
                         "tbl-audit", "tbl-session"):
            self.assertIn(table_id, html)
        for div_id in ("overview", "contract-box", "alerts-list", "gen", "kpis"):
            self.assertIn(div_id, html)
        self.assertIn("contract-box", html)

    def test_f2_all_projects_static(self):
        html_path = os.path.join(ROOT, "dashboard", "all.html")
        self.assertTrue(os.path.exists(html_path))
        with open(html_path, "r", encoding="utf-8") as f:
            html = f.read()
        for canvas_id in ("ch-proj-calls", "ch-proj-done", "ch-pie",
                          "ch-timeline", "ch-trend", "ch-tokens"):
            self.assertIn(canvas_id, html)
        self.assertIn("all-projects.json", html)
        for fn in ("function renderKpis", "function renderTables",
                   "function renderExtra", "function renderCharts"):
            self.assertIn(fn, html)
        for table_id in ("tbl-proj", "tbl-task", "tbl-act", "tbl-err",
                         "tbl-agent", "tbl-dur", "tbl-slow", "tbl-files",
                         "tbl-find", "tbl-tools", "tbl-perm", "tbl-exec",
                         "tbl-eff", "tbl-eff-proj", "tbl-agent-eff", "tbl-risk",
                         "tbl-contract", "tbl-fail", "tbl-audit", "tbl-session"):
            self.assertIn(table_id, html)
        for div_id in ("overview", "alerts-list", "gen", "kpis"):
            self.assertIn(div_id, html)


    def test_g_save_review(self):
        rec = save_review("TASK-LOG-G", "failed", findings=[
            {"severity": "critical", "file": "a.py", "issue": "hole",
             "required_action": "fix"},
            {"severity": "minor", "file": "b.py", "issue": "typo",
             "required_action": "fix"},
        ])
        self.assertEqual(rec["status"], "failed")
        path = os.path.join(self.tmp.name, "reviews", "TASK-LOG-G.json")
        self.assertTrue(os.path.exists(path))
        with open(path, "r", encoding="utf-8") as f:
            on_disk = json.load(f)
        self.assertEqual(len(on_disk["findings"]), 2)
        events = read_jsonl(self.events)
        kinds = {(e.get("event"), e.get("task")) for e in events}
        self.assertIn(("REVIEW_FAILED", "TASK-LOG-G"), kinds)
        with self.assertRaises(ValueError):
            save_review("TASK-LOG-G", "failed",
                        findings=[{"severity": "nope"}])
        rec2 = save_review("TASK-LOG-G2", "passed", findings=[])
        self.assertEqual(rec2["findings"], [])
        events = read_jsonl(self.events)
        kinds = {(e.get("event"), e.get("task")) for e in events}
        self.assertIn(("REVIEW_PASSED", "TASK-LOG-G2"), kinds)

    def test_h_snapshot_history(self):
        with open(self.events, "w", encoding="utf-8") as f:
            f.write(json.dumps({"event": "TASK_DONE", "task": "TASK-H1",
                                "time": "2026-09-14T08:00:00Z"}) + "\n")
        with open(self.toolcalls, "w", encoding="utf-8") as f:
            f.write("")
        metrics_mod.rebuild()
        hist_dir = os.path.join(self.tmp.name, "history")
        self.assertTrue(os.path.isdir(hist_dir))
        files = [fn for fn in os.listdir(hist_dir) if fn.endswith(".json")]
        self.assertEqual(len(files), 1)
        with open(os.path.join(hist_dir, files[0]), "r", encoding="utf-8") as f:
            snap = json.load(f)
        self.assertEqual(snap["totals"]["done"], 1)

    def test_i_extra_sections_and_alerts(self):
        events = [
            {"time": "2020-01-01T00:00:00Z", "event": "TASK_CREATED", "task": "TASK-STUCK"},
            {"time": "2020-01-01T01:00:00Z", "event": "WORKER_STARTED",
             "task": "TASK-STUCK", "agent": "building"},
        ]
        toolcalls = [
            {"time": "2020-01-01T02:00:00Z", "task": "TASK-STUCK",
             "operation": "CALL", "tool": "Bash", "duration_ms": 100,
             "status": "error", "error": "boom", "agent": "building",
             "tokens_in": 5, "tokens_out": 5},
            {"time": "2020-01-01T03:00:00Z", "task": "TASK-STUCK",
             "operation": "CALL", "tool": "Bash", "duration_ms": 300,
             "status": "ok", "error": None, "agent": "building"},
        ]
        queue = {"tasks": [
            {"id": "TASK-STUCK", "status": "running"},
            {"id": "TASK-B2", "status": "blocked"},
        ]}
        reviews = [{"task": "TASK-STUCK", "time": "2020-01-02T00:00:00Z",
                    "status": "failed",
                    "findings": [{"severity": "major", "file": "x.py",
                                  "issue": "bad"}]}]
        per_task = [{"task": "TASK-STUCK", "done": False, "sessions": 1,
                     "tool_calls": 2, "recovery": 0, "review_loop": 2,
                     "time_to_DONE": None, "tokens": 10}]
        extra = export_mod.build_extra(events, toolcalls, queue, reviews, [],
                                       ["x.py", "y.py"], {}, per_task)
        codes = {a["code"] for a in extra["alerts"]}
        self.assertIn("STUCK", codes)
        self.assertIn("REVIEW_AT_RISK", codes)
        self.assertIn("QUEUE_BLOCKED", codes)
        self.assertEqual(extra["errors"]["count"], 1)
        self.assertEqual(extra["queue"]["by_status"]["blocked"], 1)
        self.assertEqual(extra["findings"]["by_severity"]["major"], 1)
        self.assertEqual(extra["tokens_total"], 10)
        tools = {d["tool"]: d for d in extra["durations"]["per_tool"]}
        self.assertEqual(tools["Bash"]["calls"], 2)
        self.assertEqual(tools["Bash"]["avg_ms"], 200.0)
        ag = {a["agent"]: a for a in extra["agents"]}
        self.assertEqual(ag["building"]["sessions"], 1)
        self.assertEqual(ag["building"]["errors"], 1)

    def test_j_aggregate_two_projects(self):
        projs = []
        for name, tasks in (("PA", ["TASK-A1"]), ("PB", ["TASK-B1", "TASK-B2"])):
            root = os.path.join(self.tmp.name, name)
            mgr = os.path.join(root, ".agent", "manager")
            os.makedirs(mgr)
            metrics = {"totals": {"tasks": len(tasks),
                                  "done": len(tasks), "failed": 0,
                                  "cancelled": 0, "success_rate": 1.0,
                                  "tool_calls": 1, "recoveries": 0,
                                  "review_loops": 0, "tokens_total": 3},
                       "per_task": {t: {"sessions_per_task": 1,
                                        "tool_calls_per_task": 1,
                                        "recovery_count": 0, "review_loop": 0,
                                        "time_to_DONE": 5.0, "done": True,
                                        "tokens_total": 3} for t in tasks}}
            with open(os.path.join(mgr, "metrics.json"), "w", encoding="utf-8") as f:
                json.dump(metrics, f)
            with open(os.path.join(mgr, "events.jsonl"), "w", encoding="utf-8") as f:
                f.write(json.dumps({"time": "2026-09-14T08:00:00Z",
                                    "event": "TASK_DONE",
                                    "task": tasks[0]}) + "\n")
            projs.append(root)
        payload = aggregate_mod.build_payload(projs)
        self.assertEqual(payload["combined"]["total_tasks"], 3)
        self.assertEqual(payload["combined"]["done"], 3)
        self.assertEqual(len(payload["per_task"]), 3)
        self.assertEqual({p["project"] for p in payload["per_project"]}, {"PA", "PB"})
        self.assertIn("token_per_success", payload["efficiency"])
        self.assertEqual(len(payload["efficiency_by_project"]), 2)
        self.assertIn("risk", payload)
        self.assertIsInstance(payload["risk"], list)


    def test_k_tools_inventory(self):
        agents_dir = os.path.join(self.tmp.name, "agents")
        os.makedirs(agents_dir)
        with open(os.path.join(agents_dir, "aa.md"), "w", encoding="utf-8") as f:
            f.write("---\nmodel: m1\nsteps: 5\npermission:\n"
                    "  read: allow\n  bash: deny\n---\n")
        perms = tools_mod.parse_agent_permissions(agents_dir)
        self.assertEqual(perms["aa"]["model"], "m1")
        self.assertEqual(perms["aa"]["tools"]["read"], "allow")
        self.assertEqual(perms["aa"]["tools"]["bash"], "deny")
        calls = [
            {"tool": "Read", "status": "ok", "agent": "aa",
             "time": "2026-09-14T08:00:00Z"},
            {"tool": "Bash", "status": "error", "agent": "aa",
             "time": "2026-09-14T09:00:00Z"},
            {"tool": "mystery_xyz", "status": "ok", "agent": "aa",
             "time": "2026-09-14T10:00:00Z"},
        ]
        sec = tools_mod.build_tools_section(calls, perms)
        by_tool = {m["tool"]: m for m in sec["matrix"]}
        self.assertEqual(by_tool["read"]["calls"], 1)
        self.assertEqual(by_tool["read"]["allowed_by"], ["aa"])
        self.assertEqual(by_tool["bash"]["errors"], 1)
        self.assertEqual(by_tool["bash"]["denied_by"], ["aa"])
        self.assertEqual(by_tool["mystery_xyz"]["category"], "observed")
        self.assertEqual(by_tool["mystery_xyz"]["last_used"], "2026-09-14T10:00:00Z")
        self.assertEqual(sec["agents"]["aa"]["allowed"], ["read"])

    def test_k2_tools_no_agents_dir(self):
        calls = [{"tool": "Read", "status": "ok", "agent": "my-bot",
                  "time": "2026-09-14T08:00:00Z"}]
        sec = tools_mod.build_tools_section(
            calls, None, agents_dir=os.path.join(self.tmp.name, "nope"))
        self.assertEqual(sec["agents"], {})
        by_tool = {m["tool"]: m for m in sec["matrix"]}
        self.assertEqual(by_tool["read"]["calls"], 1)
        self.assertEqual(by_tool["read"]["allowed_by"], [])
        self.assertEqual(by_tool["read"]["agents"], {"my-bot": 1})


    def test_k3_default_projects_discovery(self):
        base = os.path.join(self.tmp.name, "base")
        os.makedirs(os.path.join(base, "proj-a", ".agent", "manager"))
        os.makedirs(os.path.join(base, "proj-b"))
        os.makedirs(os.path.join(base, ".hidden", ".agent", "manager"))
        with open(os.path.join(base, "proj-a", ".agent", "manager",
                               "metrics.json"), "w", encoding="utf-8") as f:
            json.dump({"totals": {}, "per_task": {}}, f)
        with open(os.path.join(base, ".hidden", ".agent", "manager",
                               "metrics.json"), "w", encoding="utf-8") as f:
            json.dump({"totals": {}, "per_task": {}}, f)
        found = aggregate_mod.default_projects(base)
        self.assertEqual(found, [os.path.join(base, "proj-a")])
        self.assertEqual(aggregate_mod.default_projects(
            os.path.join(self.tmp.name, "empty-missing")), [aggregate_mod.ROOT])


    def test_l_execution_graph(self):
        events = [
            {"time": "2026-09-14T07:00:00Z", "event": "TASK_CREATED", "task": "TASK-G1"},
            {"time": "2026-09-14T07:01:00Z", "event": "WORKER_STARTED", "task": "TASK-G1",
             "session_id": "s1", "agent": "planning"},
            {"time": "2026-09-14T07:02:00Z", "event": "MYSTERY_EVENT", "task": "TASK-G1"},
            {"time": "2026-09-14T07:05:00Z", "event": "WORKER_STARTED", "task": "TASK-G1",
             "session_id": "s2", "agent": "building"},
            {"time": "2026-09-14T07:06:00Z", "event": "RECOVERY_STARTED", "task": "TASK-G1"},
            {"time": "2026-09-14T07:07:00Z", "event": "REVIEW_FAILED", "task": "TASK-G1"},
            {"time": "2026-09-14T07:08:00Z", "event": "REVIEW_PASSED", "task": "TASK-G1"},
            {"time": "2026-09-14T07:09:00Z", "event": "TASK_DONE", "task": "TASK-G1"},
        ]
        calls = [
            {"time": "2026-09-14T07:01:30Z", "task": "TASK-G1", "attempt": 1,
             "session_id": "s1", "agent": "planning", "operation": "CALL",
             "tool": "Read", "duration_ms": 10, "status": "ok"},
            {"time": "2026-09-14T07:05:30Z", "task": "TASK-G1", "attempt": 2,
             "session_id": "s2", "agent": "building", "operation": "CALL",
             "tool": "Bash", "duration_ms": 20, "status": "error", "error": "x"},
        ]
        import graph as graph_mod
        g = graph_mod.build_graph(events, calls)
        self.assertEqual(g["summary"]["tasks"], 1)
        node = g["tasks"][0]
        self.assertEqual(node["attempts"], [1, 2])
        self.assertEqual(node["n_sessions"], 2)
        self.assertEqual(node["agents"], ["building", "planning"])
        self.assertEqual(node["review_loop"], 1)
        self.assertEqual(len(node["review_chain"]), 2)
        self.assertEqual(len(node["recovery_chain"]), 1)
        self.assertEqual(node["terminal"]["state"], "done")
        kinds = {x["kind"] for x in node["timeline"]}
        self.assertIn("event", kinds)
        self.assertIn("tool", kinds)
        s2 = next(s for s in node["sessions"] if s["session_id"] == "s2")
        self.assertEqual(s2["errors"], 1)
        ascii_tree = graph_mod.render_ascii(node)
        self.assertIn("TASK-G1", ascii_tree)
        self.assertIn("DONE", ascii_tree)
        empty = graph_mod.build_graph([], [])
        self.assertEqual(empty["tasks"], [])
        self.assertEqual(empty["summary"]["tasks"], 0)


    def test_m_session_risk(self):
        import risk as risk_mod
        now = "2026-09-14T08:00:00Z"
        calls = []
        for i in range(70):
            calls.append({"time": now, "task": "TASK-R1", "session_id": "s-big",
                          "agent": "building", "operation": "CALL", "tool": "Bash",
                          "duration_ms": 100,
                          "status": "error" if i < 30 else "ok",
                          "error": "boom" if i < 30 else None})
        calls.append({"time": now, "task": "TASK-R1", "session_id": "s-small",
                      "agent": "planning", "operation": "CALL", "tool": "Read",
                      "duration_ms": 10, "status": "ok", "error": None})
        result = risk_mod.assess(calls, [], {}, {})
        big = next(s for s in result["sessions"] if s["session_id"] == "s-big")
        self.assertEqual(big["level"], "high")
        self.assertTrue(any("OBSERVATION" in o or "calls" in o for o in big["observation"]))
        self.assertTrue(len(big["recommendation"]) >= 2)
        small = next(s for s in result["sessions"] if s["session_id"] == "s-small")
        self.assertEqual(small["level"], "ok")
        self.assertEqual(result["overall"], "high")
        self.assertIsNone(result["median_calls"])
        th = dict(risk_mod.DEFAULT_THRESHOLDS)
        th["high_calls"] = 1000
        th["error_rate_high"] = 1.0
        calm = risk_mod.assess_session("s", {"agent": "a", "task": "t", "calls": 5,
                                             "errors": 0, "tokens": 0,
                                             "error_rate": 0.0}, 50.0, 5.0, th)
        self.assertEqual(calm["level"], "ok")


    def test_n_observability_contract(self):
        import observability as obs_mod
        self.assertEqual(obs_mod.SCHEMA_VERSION, 1)
        events = [
            {"time": "2026-09-14T07:00:00Z", "event": "TASK_CREATED", "task": "TASK-O1"},
            {"time": "2026-09-14T07:01:00Z", "event": "WORKER_STARTED", "task": "TASK-O1",
             "session_id": "s1", "agent": "building"},
            {"time": "2026-09-14T07:02:00Z", "event": "REVIEW_FAILED", "task": "TASK-O1"},
            {"time": "2026-09-14T07:03:00Z", "event": "REVIEW_FAILED", "task": "TASK-O1"},
        ]
        calls = [{"time": "2026-09-14T07:01:30Z", "task": "TASK-O1",
                  "session_id": "s1", "agent": "building", "operation": "CALL",
                  "tool": "Bash", "duration_ms": 100, "status": "ok"}]
        contract = obs_mod.build_contract(task_id="TASK-O1", events=events,
                                          toolcalls=calls, checkpoint={}, config={},
                                          max_review_cycles=3)
        self.assertEqual(contract["schema_version"], 1)
        self.assertEqual(contract["task_id"], "TASK-O1")
        self.assertEqual(contract["status"], "running")
        self.assertEqual(contract["session"]["id"], "s1")
        types = {r["type"] for r in contract["recommendations"]}
        self.assertIn("REVIEW", types)
        empty = obs_mod.build_contract(events=[], toolcalls=[])
        self.assertIsNone(empty["task_id"])
        self.assertEqual(empty["recommendations"], [])

    def test_o_failure_taxonomy(self):
        import failure as failure_mod
        self.assertEqual(failure_mod.classify("pytest failed x", None), "TEST_FAILURE")
        self.assertEqual(failure_mod.classify("boom", None), "TOOL_ERROR")
        self.assertEqual(failure_mod.classify("x", "REVIEW_FAILED"), "REVIEW_FAILURE")
        self.assertEqual(failure_mod.classify("stopped_limit hit", None), "SESSION_LIMIT")
        self.assertEqual(failure_mod.classify("", None), "UNKNOWN")
        events = [
            {"time": "2026-09-14T07:00:00Z", "event": "TASK_CREATED", "task": "TASK-F1"},
            {"time": "2026-09-14T07:01:00Z", "event": "REVIEW_FAILED", "task": "TASK-F1",
             "message": "bad"},
            {"time": "2026-09-14T07:02:00Z", "event": "REVIEW_FAILED", "task": "TASK-F1",
             "message": "bad"},
            {"time": "2026-09-14T07:03:00Z", "event": "REVIEW_FAILED", "task": "TASK-F1",
             "message": "bad"},
            {"time": "2026-09-14T07:04:00Z", "event": "RECOVERY_STARTED", "task": "TASK-F1"},
            {"time": "2026-09-14T07:05:00Z", "event": "TASK_DONE", "task": "TASK-F1"},
        ]
        result = failure_mod.analyze(events, [])
        t = result["tasks"]["TASK-F1"]
        self.assertEqual(t["failures"], 3)
        self.assertEqual(t["by_category"], {"REVIEW_FAILURE": 3})
        self.assertTrue(t["recovered"])
        self.assertEqual(len(t["repeated"]), 1)
        self.assertIn("Escalate", t["repeated"][0]["recommendation"])
        self.assertEqual(result["summary"]["tasks_with_failures"], 1)


    def test_p_denied_audit(self):
        import tools_inventory as tools_mod
        events = [{"time": "2026-09-14T07:00:00Z", "event": "TASK_CREATED", "task": "TASK-P1"}]
        calls = [
            {"task": "TASK-P1", "operation": "CALL", "tool": "Read", "status": "ok"},
            {"task": "TASK-P1", "operation": "ATTEMPT", "tool": "Bash", "status": "denied"},
            {"task": "TASK-P1", "operation": "ATTEMPT", "tool": "Bash", "status": "denied"},
        ]
        per_task, totals, _, _ = metrics_mod.compute_metrics(events, calls)
        self.assertEqual(per_task["TASK-P1"]["tool_calls_per_task"], 1)
        self.assertEqual(per_task["TASK-P1"]["denied_attempts"], 2)
        self.assertEqual(totals["denied_attempts"], 2)
        perms = {"aa": {"model": "m", "steps": 1,
                        "tools": {"read": "allow", "bash": "deny", "edit": "allow"}}}
        sec = tools_mod.build_tools_section(calls, perms)
        by_tool = {m["tool"]: m for m in sec["matrix"]}
        self.assertEqual(by_tool["bash"]["audit_state"], "denied_attempted")
        self.assertEqual(by_tool["bash"]["denied_attempts"], 2)
        self.assertEqual(by_tool["bash"]["calls"], 0)
        self.assertEqual(by_tool["read"]["audit_state"], "allowed_used")
        self.assertEqual(by_tool["edit"]["audit_state"], "allowed_unused")
        entry = log_denied_attempt("TASK-P1", "s1", "aa", "Bash", reason="no bash")
        self.assertEqual(entry["status"], "denied")
        self.assertEqual(entry["operation"], "ATTEMPT")
        tool_events = [e for e in read_jsonl(self.events) if e.get("event") == "TOOL_DENIED"]
        self.assertEqual(len(tool_events), 1)


    def test_q_bench_smoke(self):
        import tempfile
        import bench as bench_mod
        with tempfile.TemporaryDirectory(prefix="bench-test-") as tmp:
            row = bench_mod.bench_scale(1200, os.path.join(tmp, "s"))
        self.assertEqual(row["tool_calls_total"], 1200)
        for key in ("rebuild_s", "export_s", "aggregate_s", "parse_s",
                    "peak_mb", "export_kb", "tasks"):
            self.assertIn(key, row)
        self.assertGreater(row["tasks"], 0)


if __name__ == "__main__":
    unittest.main()
