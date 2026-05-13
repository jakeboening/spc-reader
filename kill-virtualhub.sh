#!/usr/bin/env bash
# Kill any running VirtualHub / VirtualHub-v2 processes

set -euo pipefail

PIDS=$(pgrep -f 'VirtualHub' 2>/dev/null || true)

if [[ -z "$PIDS" ]]; then
    echo "VirtualHub is not running."
    exit 0
fi

echo "Found VirtualHub process(es): $PIDS"
kill $PIDS
echo "Sent SIGTERM to VirtualHub."

# Wait up to 5 seconds for clean exit, then force-kill
for i in $(seq 1 5); do
    sleep 1
    REMAINING=$(pgrep -f 'VirtualHub' 2>/dev/null || true)
    if [[ -z "$REMAINING" ]]; then
        echo "VirtualHub stopped cleanly."
        exit 0
    fi
done

echo "Process still running after 5s — sending SIGKILL."
pkill -9 -f 'VirtualHub' || true
echo "VirtualHub force-killed."
