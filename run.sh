#!/usr/bin/env bash
# run.sh — Start LivePhotoSort in the background
#
# Usage:
#   ./run.sh           # full run
#   ./run.sh --dry-run # scan only, no file moves
#
# Watch:
#   tail -f $(ls -t logs/run_*.log | head -1)
#
# Stop:
#   kill $(cat logs/live_photo_sort.pid)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

mkdir -p logs

VENV_PYTHON="$SCRIPT_DIR/venv/bin/python3"
if [ ! -f "$VENV_PYTHON" ]; then
    echo "venv not found — run: python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt"
    exit 1
fi

echo "Starting LivePhotoSort…"
echo "Pass --dry-run to preview without moving files."
echo "Log files: $SCRIPT_DIR/logs/"

# Script writes its own timestamped log file. Don't redirect stdout here
# or log lines will be duplicated. Suppress nohup output entirely.
nohup "$VENV_PYTHON" live_photo_sort.py "$@" > /dev/null 2>&1 &
PID=$!
echo "$PID" > logs/live_photo_sort.pid

echo "PID: $PID"
echo ""
echo "Watch:  tail -f \$(ls -t $SCRIPT_DIR/logs/run_*.log | head -1)"
echo "Stop:   kill \$(cat logs/live_photo_sort.pid)"
