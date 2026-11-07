#!/bin/bash
# Picto-Sight H8 v3.0.0 | Production Launcher for AI-Powered Smart RF Scanner & Gqrx Studio
set -e

SOURCE_PATH="${BASH_SOURCE[0]}"
while [ -L "${SOURCE_PATH}" ]; do
    LINK_DIR="$(cd -P "$(dirname "${SOURCE_PATH}")" && pwd)"
    SOURCE_PATH="$(readlink "${SOURCE_PATH}")"
    [[ "${SOURCE_PATH}" != /* ]] && SOURCE_PATH="${LINK_DIR}/${SOURCE_PATH}"
done
SCRIPT_DIR="$(cd -P "$(dirname "${SOURCE_PATH}")" && pwd)"

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

# Determine writable data/log directory (local git checkout vs /opt/picto-sight-h8 .deb install)
if [ -n "${PICTOSIGHT_DATA_DIR:-}" ]; then
    DATA_ROOT="${PICTOSIGHT_DATA_DIR}"
elif [ -w "${SCRIPT_DIR}" ]; then
    DATA_ROOT="${SCRIPT_DIR}"
else
    DATA_ROOT="${HOME}/.local/share/picto-sight-h8"
    export PICTOSIGHT_DATA_DIR="${DATA_ROOT}"
fi

mkdir -p "${DATA_ROOT}/logs" "${DATA_ROOT}/profiles" "${DATA_ROOT}/export"
export HAILORT_LOGGER_PATH="${DATA_ROOT}/logs"
export PYTHONPATH="${SCRIPT_DIR}:${PYTHONPATH:-}"

cd "${DATA_ROOT}"

if [ -x "${SCRIPT_DIR}/.venv/bin/python" ]; then
    PYTHON_BIN="${SCRIPT_DIR}/.venv/bin/python"
else
    PYTHON_BIN="$(command -v python3)"
fi

exec "${PYTHON_BIN}" "${SCRIPT_DIR}/smart_scanner_cli.py" "$@"
