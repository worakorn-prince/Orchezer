"""test_sync.py — JSONL integrity matrix, Fix v1 P1 section 16 (TASK-FIX-005).

Covers: append 1 / append many -> incremental; rewrite row /
truncate / replace file / same-count changed content -> detect + rebuild;
--rebuild matches JSONL. All in isolated tmp dirs (never touches live .db).
"""
import json
import os
import sqlite3
import tempfile
import shutil
import unittest

import sqlite_sync as sync_mod


def _ev(i, tag="E"):
    return {"seq": i, "event": "TASK_%s_%d" % (tag, i), "task": "T-%d" % i,
            "time": "2026-01-01T00:00:00+00:00"}


class SyncIntegrityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sync_matrix_")
        self.ev_path = os.path.join(self.tmp, "events.jsonl")
        self.q_path = os.path.join(self.tmp, "queue.json")
        self.db_path = os.path.join(self.tmp, "idx.db")
        with open(self.q_path, "w", encoding="utf-8") as f:
            json.dump({"tasks": []}, f)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_events(self, rows):
        with open(self.ev_path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    def _sync(self, *extra):
        rc = sync_mod.main(["--events", self.ev_path, "--queue", self.q_path,
                            "--db", self.db_path] + list(extra))
        self.assertEqual(rc, 0)

    def _db_rows(self):
        c = sqlite3.connect(self.db_path)
        rows = c.execute("SELECT seq, raw_json FROM events ORDER BY seq").fetchall()
        meta = {k: v for k, v in c.execute("SELECT key, value FROM sync_meta")}
        c.close()
        return rows, meta

    def test_append_one_is_incremental(self):
        self._write_events([_ev(1), _ev(2)])
        self._sync()
        self._write_events([_ev(1), _ev(2), _ev(3)])
        self._sync()
        rows, meta = self._db_rows()
        self.assertEqual(len(rows), 3)
        self.assertEqual([r[0] for r in rows], [1, 2, 3])
        self.assertIn("content_hash", meta)
        self.assertEqual(meta["synced_lines"], "3")

    def test_append_many_is_incremental(self):
        self._write_events([_ev(i) for i in range(1, 4)])
        self._sync()
        self._write_events([_ev(i) for i in range(1, 10)])
        self._sync()
        rows, meta = self._db_rows()
        self.assertEqual(len(rows), 9)
        self.assertEqual(meta["synced_lines"], "9")

    def test_rewrite_row_same_count_rebuilds(self):
        self._write_events([_ev(1), _ev(2), _ev(3)])
        self._sync()
        rows2 = [_ev(1), _ev(2), _ev(3)]
        rows2[0] = _ev(1, tag="CHANGED")
        self._write_events(rows2)
        self._sync()
        rows, _ = self._db_rows()
        self.assertEqual(len(rows), 3)
        first = json.loads(rows[0][1])
        self.assertIn("CHANGED", first["event"])

    def test_truncate_rebuilds(self):
        self._write_events([_ev(i) for i in range(1, 6)])
        self._sync()
        self._write_events([_ev(1)])
        self._sync()
        rows, meta = self._db_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(meta["synced_lines"], "1")

    def test_replace_file_rebuilds(self):
        self._write_events([_ev(i) for i in range(1, 4)])
        self._sync()
        self._write_events([_ev(i, tag="NEW") for i in range(1, 5)])
        self._sync()
        rows, _ = self._db_rows()
        self.assertEqual(len(rows), 4)
        self.assertTrue(all("NEW" in json.loads(r[1])["event"] for r in rows))

    def test_same_count_changed_content_rebuilds(self):
        self._write_events([_ev(1), _ev(2)])
        self._sync()
        self._write_events([_ev(1, tag="X"), _ev(2, tag="X")])
        self._sync()
        rows, _ = self._db_rows()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all("_X_" in json.loads(r[1])["event"] for r in rows))

    def test_rebuild_matches_jsonl(self):
        want = [_ev(i, tag="R") for i in range(1, 8)]
        self._write_events(want)
        self._sync("--rebuild")
        rows, meta = self._db_rows()
        self.assertEqual(len(rows), len(want))
        self.assertIn("content_hash", meta)
        for (seq, raw), exp in zip(rows, want):
            self.assertEqual(json.loads(raw)["event"], exp["event"])


if __name__ == "__main__":
    unittest.main()
