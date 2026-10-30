#!/bin/bash
# Picto-Sight H8 | Boot Autostart Supervisor for Wayland / labwc Desktop
# Waits for Wayland compositor & PipeWire audio readiness, sanitizes Gqrx crash flags,
# and launches a single clean instance of the Picto-Sight H8 SIGINT Studio.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UID_NUM="$(id -u)"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/${UID_NUM}}"
export DISPLAY="${DISPLAY:-:0}"

LOG_DIR="${SCRIPT_DIR}/logs"
mkdir -p "$LOG_DIR"
AUTOSTART_LOG="${LOG_DIR}/autostart.log"

echo "[$(date -Is)] Boot autostart triggered (PID $$)" >> "$AUTOSTART_LOG"

# Wait up to 25s for Wayland compositor socket (wayland-0 / wayland-1)
for i in $(seq 1 50); do
    for sock in "${XDG_RUNTIME_DIR}"/wayland-*; do
        if [ -S "$sock" ]; then
            export WAYLAND_DISPLAY="$(basename "$sock")"
            break 2
        fi
    done
    sleep 0.5
done

# Brief settle delay so wf-panel-pi / kanshi display geometry (800x480 DSI or HDMI) is finalized
sleep 2.0

echo "[$(date -Is)] Launching Picto-Sight H8 on WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-unset} DISPLAY=${DISPLAY}" >> "$AUTOSTART_LOG"
exec "${SCRIPT_DIR}/launch_scanner.sh" --gui --single-instance >> "$AUTOSTART_LOG" 2>&1
