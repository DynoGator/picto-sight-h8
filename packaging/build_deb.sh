#!/usr/bin/env bash
# ==============================================================================
# packaging/build_deb.sh — Debian 13 (Trixie) Native .deb Package Builder
# Builds: dist/picto-sight-h8_3.0.0-1+trixie_<arch>.deb
#
# Install resulting package on Raspberry Pi OS / Debian 13 Trixie with:
#   sudo apt update
#   sudo apt install -y ./dist/picto-sight-h8_3.0.0-1+trixie_arm64.deb
# ==============================================================================

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_DIR}"

PKG_NAME="picto-sight-h8"
PKG_VERSION="3.0.0"
PKG_RELEASE="1+trixie"
ARCH="$(dpkg --print-architecture 2>/dev/null || echo "arm64")"
DEB_FILENAME="${PKG_NAME}_${PKG_VERSION}-${PKG_RELEASE}_${ARCH}.deb"

STAGE_ROOT="$(mktemp -d "/tmp/${PKG_NAME}-deb-stage.XXXXXX")"
trap 'rm -rf "${STAGE_ROOT}"' EXIT

OPT_DIR="${STAGE_ROOT}/opt/${PKG_NAME}"
BIN_DIR="${STAGE_ROOT}/usr/bin"
APP_DIR="${STAGE_ROOT}/usr/share/applications"
ICON_DIR="${STAGE_ROOT}/usr/share/icons/hicolor/scalable/apps"
UDEV_DIR="${STAGE_ROOT}/etc/udev/rules.d"
DOC_DIR="${STAGE_ROOT}/usr/share/doc/${PKG_NAME}"
DEBIAN_DIR="${STAGE_ROOT}/DEBIAN"

echo "================================================================================"
echo "  Building Debian 13 (Trixie) Package: ${DEB_FILENAME}"
echo "================================================================================"

mkdir -p \
    "${OPT_DIR}/core" \
    "${OPT_DIR}/ui" \
    "${OPT_DIR}/scripts" \
    "${OPT_DIR}/models" \
    "${OPT_DIR}/icons" \
    "${OPT_DIR}/desktop" \
    "${OPT_DIR}/docs" \
    "${OPT_DIR}/packaging/udev" \
    "${OPT_DIR}/tests" \
    "${BIN_DIR}" \
    "${APP_DIR}" \
    "${ICON_DIR}" \
    "${UDEV_DIR}" \
    "${DOC_DIR}" \
    "${DEBIAN_DIR}"

# 1. Copy clean application tree (no .venv, no __pycache__, no private GPS profiles)
cp -f \
    "${REPO_DIR}/smart_scanner_cli.py" \
    "${REPO_DIR}/launch_scanner.sh" \
    "${REPO_DIR}/autostart_scanner.sh" \
    "${REPO_DIR}/install.sh" \
    "${REPO_DIR}/pyproject.toml" \
    "${REPO_DIR}/requirements.txt" \
    "${REPO_DIR}/README.md" \
    "${REPO_DIR}/LICENSE" \
    "${OPT_DIR}/"

[ -f "${REPO_DIR}/CHANGELOG.md" ] && cp -f "${REPO_DIR}/CHANGELOG.md" "${OPT_DIR}/"

cp -f "${REPO_DIR}/core/"*.py "${OPT_DIR}/core/"
cp -f "${REPO_DIR}/ui/"*.py "${OPT_DIR}/ui/"
cp -f "${REPO_DIR}/scripts/"*.py "${OPT_DIR}/scripts/"
cp -f "${REPO_DIR}/models/hailo_pictogram_head_weights.npz" "${OPT_DIR}/models/"
cp -f "${REPO_DIR}/icons/"*.svg "${OPT_DIR}/icons/"
cp -f "${REPO_DIR}/desktop/"*.in "${OPT_DIR}/desktop/"
cp -f "${REPO_DIR}/packaging/udev/53-hackrf-picto-sight.rules" "${OPT_DIR}/packaging/udev/"
cp -f "${REPO_DIR}/tests/"*.py "${OPT_DIR}/tests/"
if compgen -G "${REPO_DIR}/docs/*.md" >/dev/null; then
    cp -f "${REPO_DIR}/docs/"*.md "${OPT_DIR}/docs/"
    cp -f "${REPO_DIR}/docs/"*.md "${DOC_DIR}/"
fi

chmod 0755 \
    "${OPT_DIR}/launch_scanner.sh" \
    "${OPT_DIR}/autostart_scanner.sh" \
    "${OPT_DIR}/install.sh" \
    "${OPT_DIR}/smart_scanner_cli.py" \
    "${OPT_DIR}/scripts/doctor_check.py" \
    "${OPT_DIR}/scripts/train_hailo_rf_classifier.py"

# 2. System CLI entrypoint (/usr/bin/picto-sight)
cat > "${BIN_DIR}/picto-sight" <<'EOF'
#!/usr/bin/env bash
exec /opt/picto-sight-h8/launch_scanner.sh "$@"
EOF
chmod 0755 "${BIN_DIR}/picto-sight"

# 3. System-wide XDG Desktop Launcher & Scalable SVG Icon
cp -f "${REPO_DIR}/icons/smart-scanner.svg" "${ICON_DIR}/picto-sight-h8.svg"
cat > "${APP_DIR}/picto-sight-h8.desktop" <<'EOF'
[Desktop Entry]
Version=1.0
Type=Application
Name=Picto-Sight H8 RF Scanner
GenericName=AI Pictogram RF Scanner, ATSC TV, ADS-B Radar & Gqrx Studio
Comment=Hailo-8 26 TOPS Multi-Layer RF Scanner, Live Broadcast TV, 1090 MHz ADS-B Radar & Gqrx Demodulator
Exec=/usr/bin/picto-sight --gui
Icon=picto-sight-h8
Terminal=false
Categories=HamRadio;Science;AudioVideo;
StartupNotify=true
EOF
chmod 0644 "${APP_DIR}/picto-sight-h8.desktop" "${ICON_DIR}/picto-sight-h8.svg"

# 4. System udev rule for HackRF One non-root access & USBDEVFS_RESET
cp -f "${REPO_DIR}/packaging/udev/53-hackrf-picto-sight.rules" \
    "${UDEV_DIR}/53-hackrf-picto-sight.rules"
chmod 0644 "${UDEV_DIR}/53-hackrf-picto-sight.rules"

# 5. Documentation & MIT Copyright
cp -f "${REPO_DIR}/LICENSE" "${DOC_DIR}/copyright"
cp -f "${REPO_DIR}/README.md" "${DOC_DIR}/README.md"
[ -f "${REPO_DIR}/CHANGELOG.md" ] && cp -f "${REPO_DIR}/CHANGELOG.md" "${DOC_DIR}/CHANGELOG.md"

# Compute Installed-Size in KiB
INSTALLED_KB="$(du -sk "${STAGE_ROOT}" | awk '{print $1}')"

# 6. DEBIAN/control — Exact Debian 13 (Trixie) Dependency Specification
cat > "${DEBIAN_DIR}/control" <<EOF
Package: ${PKG_NAME}
Version: ${PKG_VERSION}-${PKG_RELEASE}
Section: hamradio
Priority: optional
Architecture: ${ARCH}
Installed-Size: ${INSTALLED_KB}
Maintainer: DynoGator <https://github.com/DynoGator/picto-sight-h8>
Homepage: https://github.com/DynoGator/picto-sight-h8
Depends: python3 (>= 3.11), python3-venv, python3-numpy, python3-opencv, python3-pyqt6, python3-pyqt6.qtsvg, python3-serial, hackrf, libhackrf0, gqrx-sdr, gnuradio, gr-osmosdr, soapysdr0.8-module-hackrf | soapysdr-module-hackrf, rtl-433, readsb, multimon-ng, mpv, gpsd, gpsd-clients, pulseaudio-utils, usbutils
Recommends: hailo-all, hailo-models, python3-hailort, direwolf, w-scan-cpp, ffmpeg, sox, minimodem, lxterminal, grim, python3-pytest
Description: Hailo-8 AI-Powered RF Smart Scanner, ATSC TV, ADS-B Radar & Gqrx Studio
 Picto-Sight H8 is a multi-layer RF spectrum intelligence, 10-class Hailo-8
 (26 TOPS PCIe) spectrogram classifier, live terrestrial ATSC 8VSB / Analog
 NTSC television receiver, 1090 MHz ADS-B Mode-S tactical radar, POCSAG/APRS/ISM
 telemetry decoder, and automated Gqrx 2.17+ controller engineered for
 Raspberry Pi 5 (Debian 13 Trixie), HackRF One, 5-inch DSI touchscreens,
 and 1080p+ HDMI SIGINT workstations.
EOF

# 7. DEBIAN/postinst — Builds --system-site-packages venv, de-conflicts readsb, provisions Gqrx
cat > "${DEBIAN_DIR}/postinst" <<'EOF'
#!/usr/bin/env bash
set -e

OPT_DIR="/opt/picto-sight-h8"

if [ "$1" = "configure" ]; then
    # 1. Create / refresh --system-site-packages venv so gnuradio, osmosdr & hailo_platform are linked
    python3 -m venv --system-site-packages "${OPT_DIR}/.venv"

    # 2. Prevent readsb systemd service from monopolizing HackRF One at boot
    if command -v systemctl >/dev/null 2>&1; then
        systemctl disable --now readsb 2>/dev/null || true
    fi

    # 3. Reload udev rules for immediate non-root HackRF One access
    if command -v udevadm >/dev/null 2>&1; then
        udevadm control --reload-rules 2>/dev/null || true
        udevadm trigger --subsystem-match=usb --attr-match=idVendor=1d50 2>/dev/null || true
    fi

    # 4. Provision target desktop user (groups, Gqrx config, Desktop icons)
    TARGET_USER="${SUDO_USER:-}"
    if [ -z "${TARGET_USER}" ] || [ "${TARGET_USER}" = "root" ]; then
        TARGET_USER="$(awk -F: '$3 >= 1000 && $3 < 60000 {print $1; exit}' /etc/passwd || true)"
    fi

    if [ -n "${TARGET_USER}" ] && id "${TARGET_USER}" >/dev/null 2>&1; then
        for grp in plugdev dialout audio video render gpio; do
            if getent group "$grp" >/dev/null 2>&1; then
                usermod -aG "$grp" "${TARGET_USER}" 2>/dev/null || true
            fi
        done

        TARGET_HOME="$(getent passwd "${TARGET_USER}" | cut -d: -f6 || true)"
        if [ -n "${TARGET_HOME}" ] && [ -d "${TARGET_HOME}" ]; then
            su - "${TARGET_USER}" -c "/opt/picto-sight-h8/launch_scanner.sh --provision-gqrx >/dev/null 2>&1" || true

            mkdir -p "${TARGET_HOME}/Desktop" "${TARGET_HOME}/.local/share/applications" "${TARGET_HOME}/.config/autostart"
            for tmpl in Smart-AI-RF-Scanner Quick-Location-RF-Scan Deep-Location-RF-Scan Custom-RF-Profile-Scan; do
                sed "s|@INSTALL_DIR@|${OPT_DIR}|g" "${OPT_DIR}/desktop/${tmpl}.desktop.in" \
                    > "${TARGET_HOME}/.local/share/applications/${tmpl}.desktop"
                cp -f "${TARGET_HOME}/.local/share/applications/${tmpl}.desktop" \
                    "${TARGET_HOME}/Desktop/${tmpl}.desktop"
                chmod +x "${TARGET_HOME}/.local/share/applications/${tmpl}.desktop" \
                    "${TARGET_HOME}/Desktop/${tmpl}.desktop"
                chown "${TARGET_USER}:${TARGET_USER}" \
                    "${TARGET_HOME}/.local/share/applications/${tmpl}.desktop" \
                    "${TARGET_HOME}/Desktop/${tmpl}.desktop" || true
            done
        fi
    fi

    echo "[✓] picto-sight-h8 configured! Run 'picto-sight --doctor' or launch 'Picto-Sight H8 RF Scanner' from Desktop."
fi

exit 0
EOF

# 8. DEBIAN/prerm — Clean up generated .venv & __pycache__ on package removal
cat > "${DEBIAN_DIR}/prerm" <<'EOF'
#!/usr/bin/env bash
set -e

if [ "$1" = "remove" ] || [ "$1" = "purge" ]; then
    rm -rf /opt/picto-sight-h8/.venv
    find /opt/picto-sight-h8 -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
fi

exit 0
EOF

chmod 0755 "${DEBIAN_DIR}/postinst" "${DEBIAN_DIR}/prerm"
chmod 0644 "${DEBIAN_DIR}/control"

mkdir -p "${REPO_DIR}/dist"
OUT_DEB="${REPO_DIR}/dist/${DEB_FILENAME}"
dpkg-deb --root-owner-group --build "${STAGE_ROOT}" "${OUT_DEB}"

echo ""
echo "[✓] Built Debian 13 (Trixie) package: ${OUT_DEB}"
ls -lh "${OUT_DEB}"
dpkg-deb --info "${OUT_DEB}"
