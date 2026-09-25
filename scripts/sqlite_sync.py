import argparse
import datetime
import json
import os
import sqlite3
import sys

DB_SCHEMA_VERSION = "1"
NOTE_MAX_LEN = 2000

DDL = """
CREATE TABLE IF NOT EXISTS events (
  seq      INTEGER PRIMARY KEY,
  time     TEXT,
  event    TEXT NOT NULL,
  task     TEXT,
  session  TEXT,
  note     TEXT,
  raw_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS queue_snapshot (
  task_id    TEXT PRIMARY KEY,
  status     TEXT,
  priority   TEXT,
  title      TEXT,
  updated_at TEXT,
  raw_json   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checkpoints (
  task          TEXT PRIMARY KEY,
  status        TEXT,
  updated_at    TEXT,
  files_changed TEXT,
  raw_json      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sync_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task);
CREATE INDEX IF NOT EXISTS idx_events_time ON events(time);
CREATE INDEX IF NOT EXISTS idx_events_event ON events(event);
CREATE INDEX IF NOT EXISTS idx_events_task_event ON events(task, event);
CREATE INDEX IF NOT EXISTS idx_queue_status ON queue_snapshot(status);
CREATE INDEX IF NOT EXISTS idx_checkpoints_status ON checkpoints(status);
"""


def parse_args(argv):
    p = argparse.ArgumentParser(description="Sync JSON source-of-truth into SQLite read-model.")
    p.add_argument("--rebuild", action="store_true")
    p.add_argument("--events", default=".agent/manager/events.jsonl")
    p.add_argument("--queue", default=".agent/manager/queue.json")
    p.add_argument("--checkpoint", default=".agent/building/checkpoint.json")
    p.add_argument("--db", default=".agent/manager/manager_index.db")
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args(argv)


def connect_db(db_path):
    parent = os.path.dirname(os.path.abspath(db_path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=OFF;")
    return conn


def init_schema(conn):
    with conn:
        conn.executescript(DDL)


def read_jsonl_safe(path):
    rows = []
    corrupt = 0
    try:
        fh = open(path, encoding="utf-8-sig", errors="strict")
    except FileNotFoundError:
        return rows, corrupt
    with fh:
        for lineno, line in enumerate(fh, start=1):
            if line.strip() == "":
                continue
            raw = line.rstrip("\r\n")
            if raw.strip() == "":
                continue
            try:
                obj = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                corrupt += 1
                print("warn: corrupt line %d in %s skipped" % (lineno, path), file=sys.stderr)
                continue
            if not isinstance(obj, dict):
                corrupt += 1
                print("warn: corrupt line %d in %s skipped (not an object)" % (lineno, path), file=sys.stderr)
                continue
            rows.append((lineno, obj, raw))
    return rows, corrupt


def _get_last_seq(conn):
    try:
        cur = conn.execute("SELECT value FROM sync_meta WHERE key='last_seq'")
        row = cur.fetchone()
        return int(row[0]) if row else 0
    except (sqlite3.Error, ValueError, TypeError):
        return 0


def sync_events(conn, events_path, full, start_seq):
    rows, corrupt = read_jsonl_safe(events_path)
    if full:
        with conn:
            conn.execute("DELETE FROM events")
            data = []
            for seq, obj, raw in rows:
                note = obj.get("note")
                if isinstance(note, str) and len(note) > NOTE_MAX_LEN:
                    note = note[:NOTE_MAX_LEN]
                elif note is not None and not isinstance(note, str):
                    note = str(note)
                    if len(note) > NOTE_MAX_LEN:
                        note = note[:NOTE_MAX_LEN]
                data.append((
                    seq,
                    obj.get("time"),
                    obj.get("event") or "",
                    obj.get("task"),
                    obj.get("session"),
                    note,
                    raw,
                ))
            conn.executemany(
                "INSERT OR REPLACE INTO events(seq,time,event,task,session,note,raw_json)"
                " VALUES(?,?,?,?,?,?,?)",
                data,
            )
        return len(data), corrupt
    with conn:
        data = []
        for seq, obj, raw in rows:
            if seq <= start_seq:
                continue
            note = obj.get("note")
            if isinstance(note, str) and len(note) > NOTE_MAX_LEN:
                note = note[:NOTE_MAX_LEN]
            elif note is not None and not isinstance(note, str):
                note = str(note)
                if len(note) > NOTE_MAX_LEN:
                    note = note[:NOTE_MAX_LEN]
            data.append((
                seq,
                obj.get("time"),
                obj.get("event") or "",
                obj.get("task"),
                obj.get("session"),
                note,
                raw,
            ))
        conn.executemany(
            "INSERT OR REPLACE INTO events(seq,time,event,task,session,note,raw_json)"
            " VALUES(?,?,?,?,?,?,?)",
            data,
        )
    return len(data), corrupt


def sync_queue(conn, queue_path):
    try:
        with open(queue_path, encoding="utf-8-sig", errors="strict") as fh:
            payload = json.load(fh)
    except FileNotFoundError:
        return 0, 0
    tasks = payload.get("tasks", []) if isinstance(payload, dict) else []
    with conn:
        cur = conn.execute("SELECT COUNT(*) FROM queue_snapshot")
        # NOTE: prev_count is the row count before DELETE, not a diff of this sync.
        prev_count = cur.fetchone()[0]
        conn.execute("DELETE FROM queue_snapshot")
        data = []
        for t in tasks:
            if not isinstance(t, dict):
                continue
            data.append((
                t.get("id"),
                t.get("status"),
                t.get("priority"),
                t.get("title"),
                t.get("updated_at"),
                json.dumps(t, ensure_ascii=False),
            ))
        conn.executemany(
            "INSERT OR REPLACE INTO queue_snapshot(task_id,status,priority,title,updated_at,raw_json)"
            " VALUES(?,?,?,?,?,?)",
            data,
        )
    return len(data), prev_count


def sync_checkpoint(conn, checkpoint_path):
    try:
        with open(checkpoint_path, encoding="utf-8-sig", errors="strict") as fh:
            raw_text = fh.read()
    except FileNotFoundError:
        return False
    try:
        cp = json.loads(raw_text)
    except (json.JSONDecodeError, ValueError):
        print("warn: checkpoint file corrupt, skipped", file=sys.stderr)
        return False
    if not isinstance(cp, dict):
        return False
    task = cp.get("task") or "default"
    status = cp.get("status")
    progress = cp.get("progress") if isinstance(cp.get("progress"), dict) else {}
    updated_at = progress.get("last_progress_at")
    if updated_at is None:
        try:
            mtime = os.path.getmtime(checkpoint_path)
            updated_at = datetime.datetime.fromtimestamp(mtime, tz=datetime.timezone.utc).isoformat()
        except OSError:
            updated_at = None
    files_changed = progress.get("files_changed", [])
    files_changed_json = json.dumps(files_changed, ensure_ascii=False)
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO checkpoints(task,status,updated_at,files_changed,raw_json)"
            " VALUES(?,?,?,?,?)",
            (task, status, updated_at, files_changed_json, raw_text),
        )
    return True


def update_sync_meta(conn, stats):
    now = datetime.datetime.now(tz=datetime.timezone.utc).isoformat()
    kv = {
        "last_seq": str(stats.get("last_seq", 0)),
        "last_sync_at": now,
        "corrupt_lines": str(stats.get("corrupt_lines", 0)),
        "db_schema_version": DB_SCHEMA_VERSION,
    }
    for name, fpath in (
        ("source_events_mtime", stats.get("events_path")),
        ("source_queue_mtime", stats.get("queue_path")),
    ):
        if fpath and os.path.exists(fpath):
            try:
                kv[name] = str(os.path.getmtime(fpath))
            except OSError:
                pass
    if stats.get("events_path") and os.path.exists(stats["events_path"]):
        try:
            kv["source_events_bytes"] = str(os.path.getsize(stats["events_path"]))
        except OSError:
            pass
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO sync_meta(key,value) VALUES(?,?)",
            list(kv.items()),
        )


def _count_lines(path):
    try:
        with open(path, encoding="utf-8-sig", errors="strict") as fh:
            return sum(1 for _ in fh)
    except FileNotFoundError:
        return 0


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    lowered = args.db.lower()
    if lowered.endswith(".json") or lowered.endswith(".jsonl"):
        print("error: --db must not point at a JSON file", file=sys.stderr)
        return 2
    if args.dry_run:
        rows, corrupt = read_jsonl_safe(args.events)
        try:
            with open(args.queue, encoding="utf-8-sig", errors="strict") as fh:
                payload = json.load(fh)
            qtasks = payload.get("tasks", []) if isinstance(payload, dict) else []
            qcount = len(qtasks)
        except FileNotFoundError:
            qcount = 0
        cp_exists = os.path.exists(args.checkpoint)
        print("dry-run: %d event lines (+%d corrupt skipped), %d queue tasks, checkpoint=%s, db=%s (untouched)"
              % (len(rows), corrupt, qcount, "found" if cp_exists else "missing", args.db))
        return 0
    conn = connect_db(args.db)
    try:
        init_schema(conn)
        full = bool(args.rebuild)
        if full:
            # NOTE: DELETE FROM events lives only in sync_events(full=True); kept here
            # only queue_snapshot/checkpoints reset + last_seq reset to avoid double DELETE.
            with conn:
                conn.execute("DELETE FROM queue_snapshot")
                conn.execute("DELETE FROM checkpoints")
                conn.execute("INSERT OR REPLACE INTO sync_meta(key,value) VALUES('last_seq','0')")
            start_seq = 0
        else:
            start_seq = _get_last_seq(conn)
            current_lines = _count_lines(args.events)
            if current_lines < start_seq:
                with conn:
                    conn.execute("DELETE FROM events")
                    conn.execute("DELETE FROM queue_snapshot")
                    conn.execute("DELETE FROM checkpoints")
                start_seq = 0
                full = True
        inserted, corrupt = sync_events(conn, args.events, full, start_seq)
        qrows, _removed = sync_queue(conn, args.queue)
        cp_ok = sync_checkpoint(conn, args.checkpoint)
        cur = conn.execute("SELECT COALESCE(MAX(seq),0) FROM events")
        last_seq = cur.fetchone()[0]
        update_sync_meta(conn, {
            "last_seq": last_seq,
            "corrupt_lines": corrupt,
            "events_path": args.events,
            "queue_path": args.queue,
        })
        cur = conn.execute("SELECT task FROM checkpoints LIMIT 1")
        row = cur.fetchone()
        lock = row[0] if row else "none"
        print("Synced: %d events (+%d corrupt skipped), %d queue rows, lock=%s" % (inserted, corrupt, qrows, lock))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
