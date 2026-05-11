#!/usr/bin/env bash
# Launch VirtualHub (if not already running) then start the Python live plotter.
set -euo pipefail

ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
VHUB_DIR="$ROOT/VirtualHubV2.linux.69177/x86_64"
VHUB_BIN="$VHUB_DIR/VirtualHub-v2"
VHUB_HTTP_PORT="${VHUB_HTTP_PORT:-4444}"   # plain HTTP (redirects to HTTPS)
VHUB_PORT="${VHUB_PORT:-4443}"            # HTTPS port used by YAPI
VHUB_PID_FILE="${TMPDIR:-/tmp}/yoctopuce-vhub.pid"

# ── VirtualHub ────────────────────────────────────────────────────────────────

vhub_running() {
  ss -tlnH "sport = :$VHUB_PORT" 2>/dev/null | grep -q ":$VHUB_PORT" || \
  nc -z 127.0.0.1 "$VHUB_PORT" 2>/dev/null
}

start_vhub() {
  if [[ ! -x "$VHUB_BIN" ]]; then
    echo "ERROR: VirtualHub binary not found or not executable at $VHUB_BIN" >&2
    exit 1
  fi

  echo "Starting VirtualHub on port $VHUB_PORT …"
  "$VHUB_BIN" -d &           # -d = run as daemon (stays in background)
  VHUB_SPAWNED_PID=$!
  echo "$VHUB_SPAWNED_PID" > "$VHUB_PID_FILE"

  # Wait up to 5 s for VirtualHub to be ready.
  for i in $(seq 1 10); do
    sleep 0.5
    if vhub_running; then
      echo "VirtualHub ready."
      return
    fi
  done
  echo "ERROR: VirtualHub did not start within 5 s." >&2
  exit 1
}

stop_vhub_if_spawned() {
  if [[ -f "$VHUB_PID_FILE" ]]; then
    pid="$(cat "$VHUB_PID_FILE")"
    rm -f "$VHUB_PID_FILE"
    if kill -0 "$pid" 2>/dev/null; then
      echo "Stopping VirtualHub (pid $pid) …"
      kill "$pid" 2>/dev/null || true
    fi
  fi
}

VHUB_SPAWNED_PID=""
if vhub_running; then
  echo "VirtualHub already running on port $VHUB_PORT."
else
  trap stop_vhub_if_spawned EXIT
  start_vhub
fi

# ── Python plotter ────────────────────────────────────────────────────────────

# Use TkAgg so matplotlib doesn't initialise the Qt/GTK Wayland Vulkan path,
# which emits harmless but noisy MESA-INTEL "FINISHME" warnings.
export MPLBACKEND="${MPLBACKEND:-TkAgg}"

PYTHON="${PYTHON:-python3}"
SCRIPT="$ROOT/scripts/plot_temperature.py"

# Pass through any extra arguments (e.g. --ymin 20 --ymax 500 --window 300).
exec "$PYTHON" "$SCRIPT" --hub "127.0.0.1:$VHUB_PORT" "$@"
