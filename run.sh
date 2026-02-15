#!/usr/bin/env bash
# run.sh — Start LivePhotoSort in the background
#
# Usage:
#   ./run.sh           # full run
#   ./run.sh --dry-run # scan only, no file moves
#
# Watch:
#   tail -f logs/run_*.log   (or use watch_log.sh)
#
# Stop:
#   kill $(cat logs/live_photo_sort.pid)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

mkdir -p logs

VENV_PYTHON="$SCRIPT_DIR/venv/bin/python3"
if [ ! -f "$VENV_PYTHON" ]; then
    echo "venv not found — run: python3 -m venv venv"
    exit 1
fi

LOG_FILE="logs/run_$(date +%Y%m%d_%H%M%S).log"
echo "Starting LivePhotoSort…"
echo "Log: $SCRIPT_DIR/$LOG_FILE"
echo "Pass --dry-run to preview without moving files."

nohup "$VENV_PYTHON" live_photo_sort.py "$@" >> "$LOG_FILE" 2>&1 &
echo "PID: $!"
echo "$!" > logs/live_photo_sort.pid
echo ""
echo "Watch with:  tail -f $LOG_FILE"
echo "Stop with:   kill \$(cat logs/live_photo_sort.pid)"
