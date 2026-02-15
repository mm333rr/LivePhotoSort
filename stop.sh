#!/usr/bin/env bash
# stop.sh — Gracefully stop LivePhotoSort

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$SCRIPT_DIR/logs/live_photo_sort.pid"

if [ ! -f "$PID_FILE" ]; then
    echo "No PID file found — process may not be running."
    exit 0
fi

PID=$(cat "$PID_FILE")
if kill -0 "$PID" 2>/dev/null; then
    echo "Sending SIGTERM to PID $PID…"
    kill "$PID"
    echo "Process will finish current file then stop. Watch the log to confirm."
else
    echo "Process $PID not running. Cleaning up PID file."
    rm -f "$PID_FILE"
fi
