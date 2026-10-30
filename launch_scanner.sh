#!/bin/bash
# Picto-Sight H8 | Production Launcher for AI-Powered Smart RF Scanner & Gqrx Controller
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UID_NUM="$(id -u)"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/${UID_NUM}}"
export DISPLAY="${DISPLAY:-:0}"

# Auto-detect active Wayland socket inside XDG_RUNTIME_DIR if WAYLAND_DISPLAY is unset
if [ -z "${WAYLAND_DISPLAY:-}" ]; then
    for sock in "${XDG_RUNTIME_DIR}"/wayland-*; do
        if [ -S "$sock" ]; then
            export WAYLAND_DISPLAY="$(basename "$sock")"
            break
        fi
    done
    export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"
fi

export PYTHONPATH="${SCRIPT_DIR}:${PYTHONPATH:-}"
cd "${SCRIPT_DIR}"

if [ -x "${SCRIPT_DIR}/.venv/bin/python" ]; then
    PYTHON_BIN="${SCRIPT_DIR}/.venv/bin/python"
else
    PYTHON_BIN="$(command -v python3)"
fi

exec "${PYTHON_BIN}" "${SCRIPT_DIR}/smart_scanner_cli.py" "$@"
