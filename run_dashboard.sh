#!/bin/sh
# run_dashboard.sh — one-click for macOS/Linux: rebuild + export + aggregate + serve + open browser
# Usage:  sh run_dashboard.sh   (or: chmod +x run_dashboard.sh && ./run_dashboard.sh)
set -e
cd "$(dirname "$0")"

echo "[1/4] Rebuilding metrics..."
python3 scripts/metrics.py --rebuild

echo "[2/4] Exporting dashboard data..."
python3 scripts/export_json.py

echo "[3/4] Aggregating all projects..."
python3 scripts/aggregate.py || echo "(warn) aggregate failed - single-project dashboard still works"

PORT="${PORT:-8080}"
echo "[4/4] Serving dashboard at http://localhost:$PORT/all.html ..."
if command -v xdg-open >/dev/null 2>&1; then
  (xdg-open "http://localhost:$PORT/all.html" >/dev/null 2>&1 &)
elif command -v open >/dev/null 2>&1; then
  (open "http://localhost:$PORT/all.html" >/dev/null 2>&1 &)
else
  echo "(info) open the URL above in your browser manually"
fi
python3 -m http.server --bind 127.0.0.1 "$PORT" --directory dashboard
