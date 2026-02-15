#!/usr/bin/env bash
# watch_log.sh — Tail the most recent log file

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

LATEST=$(ls -t logs/run_*.log 2>/dev/null | head -1)
if [ -z "$LATEST" ]; then
    echo "No log files found yet. Start a run first with ./run.sh"
    exit 1
fi
echo "Watching: $LATEST"
echo "Press Ctrl+C to stop watching (the process keeps running)."
echo ""
tail -f "$LATEST"
