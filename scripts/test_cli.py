"""test_cli.py — orchezer CLI dispatcher (TASK-ORCH-005).

Covers: --help (ASCII-only, exit 0), unknown command (exit 2),
init skeleton on isolated tmp root, no-overwrite without --force,
--demo end-to-end (dashboard/data.json built).
Uses isolated tmp dirs to avoid polluting real files.
"""
import io
import os
import sys
import json
import tempfile
import shutil
import unittest
from contextlib import redirect_stdout, redirect_stderr

import cli as cli_mod


class CliDispatchTest(unittest.TestCase):
    def test_help_exit_zero_and_ascii(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli_mod.main(["--help"])
        self.assertEqual(rc, 0)
        text = buf.getvalue()
        self.assertIn("init", text)
        text.encode("ascii")  # raises if non-ASCII slips into help

    def test_unknown_command_exit_two(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli_mod.main(["bogus-cmd"])
        self.assertEqual(rc, 2)
        self.assertIn("init", out.getvalue() + err.getvalue())

    def test_commands_registered(self):
        self.assertEqual(cli_mod.COMMANDS,
                         ("init", "sync", "metrics", "export", "aggregate"))


class CliInitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cli_init_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_init_creates_skeleton(self):
        rc = cli_mod.main(["init", "--root", self.tmp])
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(
            os.path.join(self.tmp, ".agent", "manager", "config.json")))
        self.assertTrue(os.path.exists(
            os.path.join(self.tmp, ".agent", "manager", "queue.json")))

    def test_init_no_overwrite_without_force(self):
        cli_mod.main(["init", "--root", self.tmp])
        cfg = os.path.join(self.tmp, ".agent", "manager", "config.json")
        with open(cfg, "r", encoding="utf-8") as f:
            before = f.read()
        cli_mod.main(["init", "--root", self.tmp])
        with open(cfg, "r", encoding="utf-8") as f:
            after = f.read()
        self.assertEqual(before, after)

    def test_init_demo_builds_dashboard(self):
        rc = cli_mod.main(["init", "--root", self.tmp, "--demo"])
        self.assertEqual(rc, 0)
        data = os.path.join(self.tmp, "dashboard", "data.json")
        self.assertTrue(os.path.exists(data))
        with open(data, "r", encoding="utf-8") as f:
            payload = json.load(f)
        self.assertGreaterEqual(payload["kpis"]["total_tasks"], 3)


if __name__ == "__main__":
    unittest.main()
