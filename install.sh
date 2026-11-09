#!/usr/bin/env bash
# ==============================================================================
# Picto-Sight H8 v3.2.0 (DynoGatorLabs Wasteland SIGINT Edition)
# Full-Stack Seamless Installer & Dual-Node Provisioner
# Target OS : Raspberry Pi OS / Debian GNU/Linux 13 (Trixie) & 12 (Bookworm)
# Hardware  : Raspberry Pi 5 + Hailo-8 (26 TOPS PCIe) + HackRF One + 5" DSI / HDMI
# Handset   : Google Pixel 9 Pro XL / Android 15 / GrapheneOS (llc.resgen.pictosight)
#
# Supports BOTH:
#   1. Local clone install:
#        git clone https://github.com/DynoGator/picto-sight-h8.git
#        cd picto-sight-h8 && ./install.sh
#   2. Zero-friction 1-line GitHub bootstrap:
#        curl -fsSL https://raw.githubusercontent.com/DynoGator/picto-sight-h8/main/install.sh | bash
# ==============================================================================

set -euo pipefail

APP_VERSION="3.2.0"
GITHUB_REPO_URL="https://github.com/DynoGator/picto-sight-h8.git"

INSTALL_SYSTEM_PKGS=1
INSTALL_UDEV=1
INSTALL_CLI_LINK=1
INSTALL_DESKTOP=1
INSTALL_AUTOSTART=1
INSTALL_BRIDGE_SERVICE=1
INSTALL_HANDSET=1
PROVISION_GQRX=1
RUN_TESTS=0

for arg in "$@"; do
    case "$arg" in
        --no-apt)       INSTALL_SYSTEM_PKGS=0 ;;
        --no-udev)      INSTALL_UDEV=0 ;;
        --no-cli-link)  INSTALL_CLI_LINK=0 ;;
        --no-desktop)   INSTALL_DESKTOP=0 ;;
        --no-autostart) INSTALL_AUTOSTART=0 ;;
        --no-bridge)    INSTALL_BRIDGE_SERVICE=0 ;;
        --no-handset)   INSTALL_HANDSET=0 ;;
        --no-gqrx)      PROVISION_GQRX=0 ;;
        --run-tests)    RUN_TESTS=1 ;;
        -h|--help)
            cat <<'EOF'
Picto-Sight H8 v3.2.0 DynoGatorLabs Wasteland SIGINT Installer (Debian 13 Trixie / Pi OS)

Usage:
  ./install.sh [options]

Options:
  --no-apt        Skip APT system package installation
  --no-udev       Skip /etc/udev/rules.d/53-hackrf-picto-sight.rules installation
  --no-cli-link   Skip /usr/local/bin/picto-sight global symlink creation
  --no-desktop    Skip ~/Desktop & application menu .desktop launchers
  --no-autostart  Skip ~/.config/autostart Wayland/labwc boot autostart entry
  --no-bridge     Skip picto-sight-bridge.service systemd installation
  --no-handset    Skip ADB mobile remote handset sideload check
  --no-gqrx       Skip out-of-the-box Gqrx 2.17+ profile & bookmark provisioning
  --run-tests     Execute the automated pytest verification suite after installation
  -h, --help      Show this help message
EOF
            exit 0
            ;;
    esac
done

# Detect sudo helper (if running as non-root)
SUDO=""
if [ "$(id -u)" -ne 0 ]; then
    if command -v sudo >/dev/null 2>&1; then
        SUDO="sudo"
    else
        echo "[!] Warning: 'sudo' not found and not running as root; system-level steps (--no-apt/--no-udev) may be skipped."
    fi
fi

TARGET_USER="${SUDO_USER:-${USER:-$(id -un)}}"
TARGET_HOME="$(getent passwd "${TARGET_USER}" | cut -d: -f6 || echo "${HOME}")"

run_as_target_user() {
    if [ "$(id -u)" -eq 0 ] && [ -n "${TARGET_USER}" ] && [ "${TARGET_USER}" != "root" ]; then
        sudo -u "${TARGET_USER}" HOME="${TARGET_HOME}" "$@"
    else
        "$@"
    fi
}

# ------------------------------------------------------------------------------
# Stage 0: Bootstrap Repository Checkout (if piped via `curl ... | bash`)
# ------------------------------------------------------------------------------
SCRIPT_SOURCE="${BASH_SOURCE[0]:-}"
if [ -n "${SCRIPT_SOURCE}" ] && [ -f "${SCRIPT_SOURCE}" ]; then
    CANDIDATE_DIR="$(cd "$(dirname "${SCRIPT_SOURCE}")" && pwd)"
else
    CANDIDATE_DIR=""
fi

if [ -n "${CANDIDATE_DIR}" ] && [ -f "${CANDIDATE_DIR}/smart_scanner_cli.py" ]; then
    REPO_DIR="${CANDIDATE_DIR}"
else
    REPO_DIR="${PICTOSIGHT_INSTALL_DIR:-${TARGET_HOME}/picto-sight-h8}"
    echo "[0/7] Bootstrapping Picto-Sight H8 repository into ${REPO_DIR}..."
    if ! command -v git >/dev/null 2>&1 && [ "${INSTALL_SYSTEM_PKGS}" -eq 1 ]; then
        ${SUDO} apt-get update -qq && ${SUDO} apt-get install -y git ca-certificates curl
    fi
    if [ -d "${REPO_DIR}/.git" ]; then
        run_as_target_user git -C "${REPO_DIR}" fetch origin main && run_as_target_user git -C "${REPO_DIR}" pull --ff-only origin main
    else
        run_as_target_user git clone "${GITHUB_REPO_URL}" "${REPO_DIR}"
    fi
fi

cd "${REPO_DIR}"

OS_PRETTY="Linux"
OS_CODENAME="unknown"
if [ -f /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    OS_PRETTY="${PRETTY_NAME:-Linux}"
    OS_CODENAME="${VERSION_CODENAME:-unknown}"
fi

echo -e "\033[0;32m"
cat << 'EOF'
  ____  _      _             ____  _       _     _     _   _  ___  
 |  _ \(_) ___| |_ ___      / ___|(_) __ _| |__ | |_  | | | |( _ ) 
 | |_) | |/ __| __/ _ \ ____\___ \| |/ _` | '_ \| __| | |_| |/ _ \ 
 |  __/| | (__| || (_) |_____|__) | | (_| | | | | |_  |  _  | (_) |
 |_|   |_|\___|\__\___/     |____/|_|\__, |_| |_|\__| |_| |_|\___/ 
                                     |___/                         
       [ DynoGatorLabs Wasteland SIGINT Platform v3.2.0 ]
       [ Hailo-8 AI + HackRF One + Pixel 9 Pro XL Mobile Remote ]
EOF
echo -e "\033[0m"
echo "================================================================================"
echo "  PICTO-SIGHT H8 v${APP_VERSION} — DUAL-NODE FULL-STACK INSTALLER"
echo "  Detected OS   : ${OS_PRETTY} ($(uname -m), codename: ${OS_CODENAME})"
echo "  Install Path  : ${REPO_DIR}"
echo "  Target User   : ${TARGET_USER} (${TARGET_HOME})"
echo "================================================================================"

# Check Raspberry Pi 5 PCIe status for Hailo-8 AI HAT+
if [ -f /proc/device-tree/model ] && tr -d '\0' </proc/device-tree/model | grep -qi "Raspberry Pi 5"; then
    if [ ! -e /dev/hailo0 ]; then
        echo "[i] Note: Raspberry Pi 5 detected without /dev/hailo0 active yet."
        echo "    If you have a Hailo-8 26 TOPS AI HAT+ installed, ensure 'dtparam=pciex1' (and"
        echo "    optionally 'dtparam=pciex1_gen=3') is enabled in /boot/firmware/config.txt."
    fi
fi

# ------------------------------------------------------------------------------
# Stage 1: Debian 13 Trixie / Raspberry Pi OS Full-Stack APT Dependencies
# ------------------------------------------------------------------------------
apt_pkg_available() {
    apt-cache show "$1" >/dev/null 2>&1
}

if [ "${INSTALL_SYSTEM_PKGS}" -eq 1 ] && command -v apt-get >/dev/null 2>&1; then
    echo ""
    echo "[1/7] Updating APT index & resolving Debian 13 (Trixie) RF/AI/GUI package stack..."
    ${SUDO} apt-get update

    # Core required & recommended packages across SDR, ATSC TV, ADS-B, Pager, GPS, Wayland & Qt6
    CANDIDATE_PKGS=(
        git curl jq ca-certificates usbutils pciutils
        hackrf hackrf-firmware libhackrf0 libhackrf-dev
        gqrx-sdr
        gnuradio gr-osmosdr
        soapysdr-tools soapysdr0.8-module-hackrf soapysdr-module-hackrf python3-soapysdr
        rtl-433 readsb multimon-ng direwolf minimodem w-scan-cpp
        mpv ffmpeg sox pulseaudio-utils
        gpsd gpsd-clients
        lxterminal grim
        python3 python3-venv python3-pip python3-dev
        python3-numpy python3-opencv
        python3-pyqt6 python3-pyqt6.qtsvg
        python3-serial python3-pytest
    )

    RESOLVED_PKGS=()
    for pkg in "${CANDIDATE_PKGS[@]}"; do
        if apt_pkg_available "$pkg"; then
            RESOLVED_PKGS+=("$pkg")
        fi
    done

    echo "      Installing ${#RESOLVED_PKGS[@]} verified system packages..."
    DEBIAN_FRONTEND=noninteractive ${SUDO} apt-get install -y "${RESOLVED_PKGS[@]}"

    # Install Hailo-8 NPU runtime & model zoo if available in Raspberry Pi OS Trixie repos
    for hpkg in hailo-all hailo-models python3-hailort; do
        if apt_pkg_available "$hpkg"; then
            echo "      Checking Hailo NPU package: $hpkg..."
            DEBIAN_FRONTEND=noninteractive ${SUDO} apt-get install -y "$hpkg" || true
        fi
    done

    # Prevent background daemons from hijacking HackRF One at boot
    if command -v systemctl >/dev/null 2>&1; then
        echo "      De-conflicting background SDR daemons (disabling auto-grabbing readsb.service)..."
        ${SUDO} systemctl disable --now readsb 2>/dev/null || true
    fi
else
    echo "[1/7] Skipping APT system package installation (--no-apt)."
fi

# ------------------------------------------------------------------------------
# Stage 2: Non-Root HackRF One udev Rules & Hardware Group Permissions
# ------------------------------------------------------------------------------
if [ "${INSTALL_UDEV}" -eq 1 ]; then
    echo ""
    echo "[2/7] Configuring HackRF One udev rules & hardware group permissions..."
    if [ -f "${REPO_DIR}/packaging/udev/53-hackrf-picto-sight.rules" ] && [ -d /etc/udev/rules.d ]; then
        ${SUDO} cp -f "${REPO_DIR}/packaging/udev/53-hackrf-picto-sight.rules" \
            /etc/udev/rules.d/53-hackrf-picto-sight.rules
        ${SUDO} chmod 0644 /etc/udev/rules.d/53-hackrf-picto-sight.rules
        if command -v udevadm >/dev/null 2>&1; then
            ${SUDO} udevadm control --reload-rules || true
            ${SUDO} udevadm trigger --subsystem-match=usb --attr-match=idVendor=1d50 || true
        fi
    fi

    if command -v usermod >/dev/null 2>&1; then
        for grp in plugdev dialout audio video render gpio; do
            if getent group "$grp" >/dev/null 2>&1 && ! id -nG "${TARGET_USER}" | grep -qw "$grp"; then
                echo "      Adding '${TARGET_USER}' to group '${grp}'..."
                ${SUDO} usermod -aG "$grp" "${TARGET_USER}" || true
            fi
        done
    fi
else
    echo "[2/7] Skipping udev & group setup (--no-udev)."
fi

# ------------------------------------------------------------------------------
# Stage 3: Python Virtual Environment (--system-site-packages) & CLI Wrapper
# ------------------------------------------------------------------------------
echo ""
echo "[3/7] Building Python virtual environment (.venv with --system-site-packages)..."
# --system-site-packages is essential on Debian 13 Trixie so compiled C++ bindings
# (`gnuradio.dtv`, `osmosdr`, `hailo_platform`, `PyQt6`, `cv2`) are inherited cleanly.
run_as_target_user python3 -m venv --system-site-packages "${REPO_DIR}/.venv"

# Install any missing pure-Python / fallback wheels only if not already satisfied by system packages
run_as_target_user "${REPO_DIR}/.venv/bin/python" -c "import numpy, serial, pytest" 2>/dev/null || \
    run_as_target_user "${REPO_DIR}/.venv/bin/pip" install --upgrade "numpy>=1.26.0" "pyserial>=3.5" "pytest>=8.0.0"

run_as_target_user "${REPO_DIR}/.venv/bin/python" -c "import cv2" 2>/dev/null || \
    run_as_target_user "${REPO_DIR}/.venv/bin/pip" install "opencv-python>=4.8.0"

run_as_target_user "${REPO_DIR}/.venv/bin/python" -c "import PyQt6.QtWidgets" 2>/dev/null || \
    run_as_target_user "${REPO_DIR}/.venv/bin/pip" install "PyQt6>=6.6.0"

chmod +x \
    "${REPO_DIR}/launch_scanner.sh" \
    "${REPO_DIR}/autostart_scanner.sh" \
    "${REPO_DIR}/smart_scanner_cli.py" \
    "${REPO_DIR}/scripts/doctor_check.py" \
    "${REPO_DIR}/scripts/train_hailo_rf_classifier.py" \
    "${REPO_DIR}/scripts/pi_remote_bridge.py" \
    "${REPO_DIR}/pi_remote_bridge.py" \
    "${REPO_DIR}/handset/install_handset.sh" \
    "${REPO_DIR}/packaging/build_deb.sh"

run_as_target_user mkdir -p "${REPO_DIR}/profiles" "${REPO_DIR}/export" "${REPO_DIR}/logs"

if [ "${INSTALL_CLI_LINK}" -eq 1 ] && [ -d /usr/local/bin ]; then
    echo "      Linking global CLI command: /usr/local/bin/picto-sight -> ${REPO_DIR}/launch_scanner.sh"
    ${SUDO} ln -sf "${REPO_DIR}/launch_scanner.sh" /usr/local/bin/picto-sight || true
fi

# ------------------------------------------------------------------------------
# Stage 4: Out-of-the-Box Gqrx 2.17+ Provisioning & Crash-Flag Sanitization
# ------------------------------------------------------------------------------
if [ "${PROVISION_GQRX}" -eq 1 ]; then
    echo ""
    echo "[4/7] Provisioning out-of-the-box Gqrx 2.17+ HackRF profile, PipeWire audio & bookmarks..."
    run_as_target_user "${REPO_DIR}/launch_scanner.sh" --provision-gqrx || true
else
    echo "[4/7] Skipping Gqrx provisioning (--no-gqrx)."
fi

# ------------------------------------------------------------------------------
# Stage 5: Desktop 1-Tap Launchers & Application Menu Icons
# ------------------------------------------------------------------------------
if [ "${INSTALL_DESKTOP}" -eq 1 ]; then
    echo ""
    echo "[5/7] Installing Desktop & XDG Application Menu launchers..."
    run_as_target_user mkdir -p "${TARGET_HOME}/Desktop" "${TARGET_HOME}/.local/share/applications"
    for tmpl in \
        Smart-AI-RF-Scanner \
        Quick-Location-RF-Scan \
        Deep-Location-RF-Scan \
        Custom-RF-Profile-Scan; do
        sed "s|@INSTALL_DIR@|${REPO_DIR}|g" "${REPO_DIR}/desktop/${tmpl}.desktop.in" \
            > "${TARGET_HOME}/.local/share/applications/${tmpl}.desktop"
        cp -f "${TARGET_HOME}/.local/share/applications/${tmpl}.desktop" \
            "${TARGET_HOME}/Desktop/${tmpl}.desktop"
        chmod +x \
            "${TARGET_HOME}/.local/share/applications/${tmpl}.desktop" \
            "${TARGET_HOME}/Desktop/${tmpl}.desktop"
        if [ "$(id -u)" -eq 0 ] && [ "${TARGET_USER}" != "root" ]; then
            chown "${TARGET_USER}:${TARGET_USER}" \
                "${TARGET_HOME}/.local/share/applications/${tmpl}.desktop" \
                "${TARGET_HOME}/Desktop/${tmpl}.desktop" || true
        fi
    done
else
    echo "[5/7] Skipping Desktop launchers (--no-desktop)."
fi

# ------------------------------------------------------------------------------
# Stage 6: Wayland / labwc Reboot-Persistent Boot Autostart
# ------------------------------------------------------------------------------
if [ "${INSTALL_AUTOSTART}" -eq 1 ]; then
    echo ""
    echo "[6/8] Installing Wayland / labwc boot autostart entry..."
    run_as_target_user mkdir -p "${TARGET_HOME}/.config/autostart"
    sed "s|@INSTALL_DIR@|${REPO_DIR}|g" \
        "${REPO_DIR}/desktop/smart-ai-rf-scanner-autostart.desktop.in" \
        > "${TARGET_HOME}/.config/autostart/smart-ai-rf-scanner.desktop"
    chmod +x "${TARGET_HOME}/.config/autostart/smart-ai-rf-scanner.desktop"
    if [ "$(id -u)" -eq 0 ] && [ "${TARGET_USER}" != "root" ]; then
        chown "${TARGET_USER}:${TARGET_USER}" \
            "${TARGET_HOME}/.config/autostart/smart-ai-rf-scanner.desktop" || true
    fi
else
    echo "[6/8] Skipping boot autostart (--no-autostart)."
fi

# ------------------------------------------------------------------------------
# Stage 6b: Mobile Remote Bridge Daemon & systemd Service
# ------------------------------------------------------------------------------
if [ "${INSTALL_BRIDGE_SERVICE}" -eq 1 ]; then
    echo ""
    echo "[6b/8] Installing Picto-Sight Mobile Remote Bridge systemd service..."
    if [ -f "${REPO_DIR}/packaging/picto-sight-bridge.service" ] && [ -d /etc/systemd/system ]; then
        ${SUDO} sed -e "s|User=dynogator|User=${TARGET_USER}|g" \
            -e "s|WorkingDirectory=/home/dynogator/ai-rf-smart-scanner|WorkingDirectory=${REPO_DIR}|g" \
            -e "s|/home/dynogator/ai-rf-smart-scanner|${REPO_DIR}|g" \
            "${REPO_DIR}/packaging/picto-sight-bridge.service" \
            > /etc/systemd/system/picto-sight-bridge.service
        ${SUDO} chmod 0644 /etc/systemd/system/picto-sight-bridge.service
        if command -v systemctl >/dev/null 2>&1; then
            ${SUDO} systemctl daemon-reload || true
            ${SUDO} systemctl enable --now picto-sight-bridge.service || true
            echo "       picto-sight-bridge.service installed & active on port 8765."
        fi
    fi
else
    echo "[6b/8] Skipping bridge systemd service (--no-bridge)."
fi

# ------------------------------------------------------------------------------
# Stage 7: Full-Stack System Doctor Verification (& Optional Test Suite)
# ------------------------------------------------------------------------------
echo ""
echo "[7/8] Running Picto-Sight H8 Full-Stack System Doctor..."
run_as_target_user "${REPO_DIR}/launch_scanner.sh" --doctor

if [ "${RUN_TESTS}" -eq 1 ]; then
    echo "Running automated pytest verification suite..."
    run_as_target_user env QT_QPA_PLATFORM=offscreen "${REPO_DIR}/.venv/bin/python" -m pytest "${REPO_DIR}/tests" -v
fi

# ------------------------------------------------------------------------------
# Stage 8: Mobile Handset Sideload & Link (Google Pixel 9 Pro XL / Android)
# ------------------------------------------------------------------------------
if [ "${INSTALL_HANDSET}" -eq 1 ] && [ -f "${REPO_DIR}/handset/install_handset.sh" ]; then
    echo ""
    echo "[8/8] Checking for connected mobile handset (Google Pixel 9 Pro XL / Android)..."
    chmod +x "${REPO_DIR}/handset/install_handset.sh"
    if command -v adb >/dev/null 2>&1 && adb devices | grep -q "device$"; then
        echo "       Connected ADB device detected! Sideloading Picto-Sight v2.1.0 handset..."
        bash "${REPO_DIR}/handset/install_handset.sh" || true
    else
        HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}' || echo "127.0.0.1")"
        echo "       [i] Sideload Android handset anytime with:"
        echo "           ./handset/install_handset.sh"
        echo "           or point phone browser to: http://${HOST_IP}:8765/apk"
    fi
fi

HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}' || echo "127.0.0.1")"
echo "================================================================================"
echo "  [✓] PICTO-SIGHT H8 v${APP_VERSION} (DynoGatorLabs Wasteland SIGINT) READY!"
echo "      Launch Desktop GUI : picto-sight --gui   (or tap Desktop icon)"
echo "      Mobile Remote App  : http://${HOST_IP}:8765/apk  (or ./handset/install_handset.sh)"
echo "      Web Companion Live : http://${HOST_IP}:8765"
echo "      Vault Overseer CRT : Press Ctrl+Shift+D in Desktop GUI"
echo "      Quick RF Scan      : picto-sight --scan --mode quick --min 88 --max 108"
echo "      Live ATSC TV       : ${REPO_DIR}/.venv/bin/python -m core.tv_demodulator --freq 539000000 --channel 25"
echo "      System Doctor      : picto-sight --doctor"
echo "================================================================================"
