#!/bin/bash
# Picto-Sight H8 | Automated Installer for Raspberry Pi 5 (Raspberry Pi OS Trixie / Bookworm)
# Installs system SDR/Hailo/Qt6 packages, builds a --system-site-packages virtualenv,
# and optionally installs Desktop 1-tap launchers + Wayland boot autostart.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${REPO_DIR}"

INSTALL_SYSTEM_PKGS=1
INSTALL_DESKTOP=1
INSTALL_AUTOSTART=1

for arg in "$@"; do
    case "$arg" in
        --no-apt) INSTALL_SYSTEM_PKGS=0 ;;
        --no-desktop) INSTALL_DESKTOP=0 ;;
        --no-autostart) INSTALL_AUTOSTART=0 ;;
        -h|--help)
            echo "Usage: ./install.sh [--no-apt] [--no-desktop] [--no-autostart]"
            exit 0
            ;;
    esac
done

echo "================================================================"
echo "  Picto-Sight H8 v2.1 Installer"
echo "  Install Directory: ${REPO_DIR}"
echo "================================================================"

if [ "${INSTALL_SYSTEM_PKGS}" -eq 1 ] && command -v apt-get >/dev/null 2>&1; then
    echo "[1/4] Checking system packages via apt..."
    sudo apt-get update
    sudo apt-get install -y \
        hackrf \
        gqrx-sdr \
        gpsd \
        gpsd-clients \
        python3-venv \
        python3-pip \
        python3-numpy \
        python3-opencv \
        python3-pyqt6 \
        python3-pyqt6.qtsvg \
        python3-serial \
        python3-pytest \
        pulseaudio-utils

    # Install HailoRT & Hailo Model Zoo if available in apt repositories (Raspberry Pi AI HAT+ 26 TOPS)
    if apt-cache show hailo-all >/dev/null 2>&1; then
        sudo apt-get install -y hailo-all || true
    fi
else
    echo "[1/4] Skipping apt package installation."
fi

echo "[2/4] Creating Python virtual environment (.venv with --system-site-packages)..."
python3 -m venv --system-site-packages "${REPO_DIR}/.venv"
chmod +x "${REPO_DIR}/launch_scanner.sh" "${REPO_DIR}/autostart_scanner.sh"

# Ensure user belongs to plugdev and dialout for non-root HackRF USB reset & u-blox GPS serial access
if command -v usermod >/dev/null 2>&1; then
    for grp in plugdev dialout; do
        if getent group "$grp" >/dev/null 2>&1 && ! id -nG "$USER" | grep -qw "$grp"; then
            echo "  Adding $USER to '$grp' group..."
            sudo usermod -aG "$grp" "$USER" || true
        fi
    done
fi

if [ "${INSTALL_DESKTOP}" -eq 1 ]; then
    echo "[3/4] Installing Desktop & Application Menu launchers..."
    mkdir -p "${HOME}/Desktop" "${HOME}/.local/share/applications"
    for tmpl in \
        Smart-AI-RF-Scanner \
        Quick-Location-RF-Scan \
        Deep-Location-RF-Scan \
        Custom-RF-Profile-Scan; do
        sed "s|@INSTALL_DIR@|${REPO_DIR}|g" "${REPO_DIR}/desktop/${tmpl}.desktop.in" \
            > "${HOME}/.local/share/applications/${tmpl}.desktop"
        cp -f "${HOME}/.local/share/applications/${tmpl}.desktop" "${HOME}/Desktop/${tmpl}.desktop"
        chmod +x "${HOME}/.local/share/applications/${tmpl}.desktop" "${HOME}/Desktop/${tmpl}.desktop"
    done
else
    echo "[3/4] Skipping Desktop launchers."
fi

if [ "${INSTALL_AUTOSTART}" -eq 1 ]; then
    echo "[4/4] Installing Wayland / XDG boot autostart entry..."
    mkdir -p "${HOME}/.config/autostart"
    sed "s|@INSTALL_DIR@|${REPO_DIR}|g" \
        "${REPO_DIR}/desktop/smart-ai-rf-scanner-autostart.desktop.in" \
        > "${HOME}/.config/autostart/smart-ai-rf-scanner.desktop"
    chmod +x "${HOME}/.config/autostart/smart-ai-rf-scanner.desktop"
else
    echo "[4/4] Skipping boot autostart entry."
fi

echo ""
echo "[✓] Picto-Sight H8 installation complete!"
echo "    Launch GUI : ${REPO_DIR}/launch_scanner.sh --gui"
echo "    Quick CLI  : ${REPO_DIR}/launch_scanner.sh --scan --mode quick"
echo "    Run Tests  : ${REPO_DIR}/.venv/bin/python -m pytest"
