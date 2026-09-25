"""orchezer CLI - zero-install entry point (stdlib only, Python 3.10+).

Usage:
  python scripts/cli.py <command> [options]
  orchezer init --demo              (via orchezer.bat / orchezer wrapper)

Commands:
  init       Init project skeleton (.agent/ + config). Options are passed
             through to bootstrap.main: --root/--demo/--config/--set/--force/--sync
  sync       Forward args to sqlite_sync.main (derived read-model cache)
  metrics    Forward args to metrics.main (rebuild metrics.json)
  export     Forward args to export_json.main (write dashboard/data.json)
  aggregate  Forward args to aggregate.main (write dashboard/all-projects.json)

This dispatcher holds no product logic: every command calls main() of the
existing module only. Run from anywhere; --root selects the target project.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

COMMANDS = ("init", "sync", "metrics", "export", "aggregate")

HELP_TEXT = """usage: orchezer <command> [options]

orchezer CLI - zero-install entry point (stdlib only).

commands:
  init       init project skeleton (.agent/ + config, optional demo data)
             options: --root DIR --demo --config --set KEY=VALUE --force --sync
  sync       sync JSONL source of truth into SQLite read-model cache
  metrics    rebuild metrics.json from events + tool calls
  export     export metrics + events to dashboard/data.json
  aggregate  merge cross-project metrics to dashboard/all-projects.json

examples:
  orchezer init --demo
  orchezer init --root D:/proj --config --set MEMORY_MCP_DIR=D:/tools/memory-mcp --sync
  orchezer sync --rebuild
  orchezer metrics --rebuild
  orchezer export
  orchezer aggregate

run 'orchezer <command> --help' for command options.
"""


def _dispatch(command, rest):
    if command == "init":
        import bootstrap
        return bootstrap.main(rest)
    if command == "sync":
        import sqlite_sync
        return sqlite_sync.main(rest)
    if command == "metrics":
        import metrics
        return metrics.main(rest)
    if command == "export":
        import export_json
        return export_json.main(rest)
    if command == "aggregate":
        import aggregate
        return aggregate.main(rest)
    return 2


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        sys.stdout.write(HELP_TEXT)
        return 0
    command, rest = args[0], args[1:]
    if command not in COMMANDS:
        sys.stderr.write("error: unknown command %r\n" % (command,))
        sys.stderr.write("valid commands: init sync metrics export aggregate\n")
        sys.stdout.write(HELP_TEXT)
        return 2
    return _dispatch(command, rest)


if __name__ == "__main__":
    sys.exit(main())
