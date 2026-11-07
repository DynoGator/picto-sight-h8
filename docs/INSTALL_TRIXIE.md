# Debian 13 (Trixie) & Raspberry Pi OS Full-Stack Installation Guide

> **Target Platform**: Raspberry Pi 5 (`aarch64`) running **Raspberry Pi OS / Debian GNU/Linux 13 (Trixie)** (also compatible with Debian 12 Bookworm & Ubuntu 24.04+).  
> **Hardware Stack**: Hailo-8 (26 TOPS PCIe AI HAT+) • Great Scott Gadgets HackRF One (`1d50:6089`) • 5" `800×480` DSI Touchscreen and/or 1080p+ HDMI Monitor • Optional USB u-blox GNSS.

Because life is far too short to spend a Saturday afternoon hand-compiling SDR drivers while `readsb` silently steals your USB bus in the background, **Picto-Sight H8 v3.0.0** ships with **three turnkey installation paths** that configure the OS, kernel `udev` rules, Python/C++ bindings, Hailo-8 NPU runtime, and **Gqrx 2.17+** out of the box.

---

## Option A — 1-Line Zero-Friction GitHub Bootstrap *(Recommended for Fresh Pis)*

Open a terminal on your Raspberry Pi 5 and run:

```bash
curl -fsSL https://raw.githubusercontent.com/DynoGator/picto-sight-h8/main/install.sh | bash
```

What this does while you grab coffee:
1. Clones `DynoGator/picto-sight-h8` into `~/picto-sight-h8` (override with `PICTOSIGHT_INSTALL_DIR=/custom/path`).
2. Updates APT and installs every verified Debian 13 Trixie dependency across `hackrf`, `gqrx-sdr`, `gnuradio`, `gr-osmosdr`, `soapysdr0.8-module-hackrf`, `readsb`, `rtl-433`, `multimon-ng`, `direwolf`, `mpv`, `w-scan-cpp`, `gpsd`, `python3-pyqt6`, `python3-opencv`, and `hailo-all` / `hailo-models` / `python3-hailort`.
3. Disables the background `readsb.service` daemon so it stops hot-wiring your SDR at boot like an unattended racoon.
4. Installs `/etc/udev/rules.d/53-hackrf-picto-sight.rules` (`MODE="0660", GROUP="plugdev", TAG+="uaccess"`) and adds your user to `plugdev`, `dialout`, `audio`, `video`, `render`, and `gpio`.
5. Builds `.venv` with `--system-site-packages` and creates `/usr/local/bin/picto-sight`.
6. Provisions `~/.config/gqrx/default.conf`, `SmartScanner_Active.conf`, and starter `bookmarks.csv` bound to your exact HackRF serial suffix (`hackrf=<suffix>`) with `crash=false` and PipeWire audio at `0 dB`.
7. Installs 4 Desktop 1-tap icons + Wayland/`labwc` boot autostart and runs `picto-sight --doctor`.

---

## Option B — Native Debian 13 (Trixie) `.deb` Package

Prefer clean APT package management under `/opt/picto-sight-h8`? Download the latest `.deb` from [**GitHub Releases (`v3.0.0`)**](https://github.com/DynoGator/picto-sight-h8/releases/latest) (or build it locally) and let `apt` resolve the entire dependency tree automatically:

```bash
# Install directly from downloaded release .deb:
sudo apt update
sudo apt install -y ./picto-sight-h8_3.0.0-1+trixie_arm64.deb

# Verify all 25 hardware & software stack checks:
picto-sight --doctor
```

### Building the `.deb` Yourself from Source
```bash
git clone https://github.com/DynoGator/picto-sight-h8.git
cd picto-sight-h8
./packaging/build_deb.sh
# Output -> dist/picto-sight-h8_3.0.0-1+trixie_arm64.deb
```

> [!NOTE]
> When installed via `.deb` into read-only `/opt/picto-sight-h8`, Picto-Sight H8 automatically routes runtime Location Profiles, Gqrx CSV exports, `ui_settings.json`, and `hailort.log` to `~/.local/share/picto-sight-h8/` (or `$PICTOSIGHT_DATA_DIR`). Zero `PermissionError` drama.

---

## Option C — Developer Git Clone + Automated Test Verification

```bash
git clone https://github.com/DynoGator/picto-sight-h8.git
cd picto-sight-h8
./install.sh --run-tests
```

### Installer CLI Flags (`./install.sh --help`)

| Flag | Effect |
| :--- | :--- |
| `--no-apt` | Skip `apt-get` package installation (fast refresh of `.venv`, udev, Gqrx & launchers) |
| `--no-udev` | Skip `/etc/udev/rules.d/53-hackrf-picto-sight.rules` & group changes |
| `--no-cli-link` | Skip `/usr/local/bin/picto-sight` symlink |
| `--no-desktop` | Skip `~/Desktop` and application menu `.desktop` launchers |
| `--no-autostart` | Skip `~/.config/autostart/smart-ai-rf-scanner.desktop` boot autostart |
| `--no-gqrx` | Skip automatic Gqrx 2.17+ configuration & bookmark seeding |
| `--run-tests` | Execute the full 43-test `pytest` hardware/GUI/DSP verification suite |

---

## Why `--system-site-packages` Is Mandatory on Debian 13 Trixie

On Raspberry Pi OS / Debian 13 (`Python 3.13`), three mission-critical modules are compiled C++/PyBind11 system packages that **do not exist as portable ARM64 wheels on PyPI**:
1. `gnuradio.dtv` — Hardware-rate `8.0 MSPS` ATSC 1.0 `8VSB` Reed-Solomon / Viterbi / equalizer television receiver blocks.
2. `osmosdr` — `gr-osmosdr` C++ HackRF One source block.
3. `hailo_platform` — HailoRT `4.23+` PCIe VDevice inference bindings for `/dev/hailo0`.

Both `install.sh` and the `.deb` `postinst` build `.venv` with `python3 -m venv --system-site-packages` so you get an isolated environment that still inherits Debian's native C++ DSP and NPU bindings cleanly.

---

## Enabling PCIe Gen 3 for the Hailo-8 AI HAT+ on Raspberry Pi 5

If `picto-sight --doctor` reports `/dev/hailo0 not present` (don't panic—Picto-Sight H8 automatically engages its `96.0%`-accurate 16-D RF CPU MLP fallback until the NPU wakes up), make sure PCIe is enabled in `/boot/firmware/config.txt`:

```ini
# /boot/firmware/config.txt
dtparam=pciex1
dtparam=pciex1_gen=3
```

Then install the Raspberry Pi Hailo meta-package and reboot:

```bash
sudo apt update && sudo apt install -y hailo-all hailo-models python3-hailort
sudo reboot
```

After rebooting, verify with:

```bash
hailortcli fw-control identify
picto-sight --doctor
```

---

## Full-Stack Health Audit (`picto-sight --doctor`)

At any time, run:

```bash
picto-sight --doctor
```

Or emit machine-readable JSON for scripts/CI:

```bash
picto-sight --doctor-json
```

And if a rogue plugin or power pull ever corrupts your Gqrx config (`crash=true`), heal it in 200 milliseconds with:

```bash
picto-sight --provision-gqrx
```
