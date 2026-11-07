# Changelog

All notable changes to **Picto-Sight H8 (`picto-sight-h8`)** are documented in this file.  
Format inspired by [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), seasoned lightly with the quiet existential dread of anyone who has ever debugged `libusb` bulk endpoints at 03:00 MST.

---

## [3.0.0] — 2026-11-07 — *"Trixie-Ghost-Spectrum"*

### Added
- **Full-Stack Seamless Installer (`install.sh`)**:
  - Supports both local clone execution (`./install.sh`) and 1-line zero-friction GitHub bootstrap (`curl -fsSL https://raw.githubusercontent.com/DynoGator/picto-sight-h8/main/install.sh | bash`).
  - Auto-detects **Raspberry Pi OS / Debian GNU/Linux 13 (Trixie)** and resolves all 30+ APT packages across SDR (`hackrf`, `gqrx-sdr`, `gnuradio`, `gr-osmosdr`, `soapysdr0.8-module-hackrf`), digital decoders (`readsb`, `rtl-433`, `multimon-ng`, `direwolf`, `mpv`, `w-scan-cpp`), GNSS (`gpsd`), GUI (`python3-pyqt6`, `python3-opencv`), and NPU (`hailo-all`, `hailo-models`, `python3-hailort`).
  - De-conflicts background SDR-grabbing systemd units (`readsb.service`) automatically so your HackRF One isn't held hostage at boot.
  - Installs `/etc/udev/rules.d/53-hackrf-picto-sight.rules` for passwordless non-root `USBDEVFS_RESET` recovery and links `/usr/local/bin/picto-sight` globally.
- **Native Debian 13 (Trixie) `.deb` Package Builder (`packaging/build_deb.sh`)**:
  - Builds `dist/picto-sight-h8_3.0.0-1+trixie_arm64.deb` with strict Debian 13 `Depends`/`Recommends`, `/opt/picto-sight-h8` payload, `/usr/bin/picto-sight` launcher, system XDG desktop icon, udev rules, and automated `postinst` `.venv --system-site-packages` + Gqrx provisioning.
  - Added `core.get_runtime_data_dir()` (`PICTOSIGHT_DATA_DIR` / `~/.local/share/picto-sight-h8` fallback) so system-wide read-only `/opt` installations store user profiles, exports, and logs cleanly without permission errors.
- **System Doctor & Out-of-the-Box Gqrx Provisioner (`scripts/doctor_check.py`)**:
  - `picto-sight --doctor` (`--doctor-json`) audits 25 OS, Python, C++ PyBind11 (`gnuradio.dtv`, `osmosdr`, `hailo_platform`), RF binary, systemd conflict, `plugdev`, USB `sysfs`, Hailo-8 `/dev/hailo0`, model weight, and Gqrx config checks in < 1 second.
  - `picto-sight --provision-gqrx` synthesizes crash-free `default.conf`, `SmartScanner_Active.conf`, PipeWire `gain=0` audio sink config, and starter `bookmarks.csv` bound to the host's exact `hackrf=<serial_suffix>` before the user ever opens Gqrx.
- **Broadcast Television Video/Audio & Raster Receiver (`core/tv_demodulator.py`, `ui/tv_dialog.py`)**:
  - **ATSC 1.0 Digital `8VSB` Mode**: Full `8.0 MSPS` GNU Radio `dtv.atsc_rx` hardware demodulation pipeline streaming MPEG-TS over localhost UDP (`127.0.0.1:17355`) to low-latency `mpv` / `ffplay` video + AC-3 audio, paired with live `6.0 MHz` channel FFT, `+309.441 kHz` pilot carrier lock detector, MER gauge, Southern Colorado TV presets (`Ch 25 KOAA`, `Ch 24 KKTV`, `Ch 22 KXRM`, `Ch 19 KRDO`, `Ch 8 KTSC`), and VHF/UHF TV channel scanner.
  - **Analog NTSC `525-Line` CRT Mode**: Real-time `4.0 MSPS` IQ envelope detection and horizontal sync correlation rendering a live `525-line / 29.97 fps` monochrome CRT raster right inside the `800×480` DSI dialog.
  - **8.0 MSPS Wideband Gqrx TV Handoff**: Added `TV_WIDE` (`8,000,000 Hz`, `decim=1`) tier to `GqrxController` so inspecting a `6 MHz` TV channel in Gqrx shows the entire channel and pilot spike without clipping.
- **Upgraded 10-Class Physics-Grounded Hailo-8 RF Classifier (`97.4%` Validation Accuracy)**:
  - Retrained `models/hailo_pictogram_head_weights.npz` directly on real `/dev/hailo0` ResNet-50 embeddings (`1000-D`) concatenated with a 16-dimensional physical RF spectrogram morphology vector (`1016-D` total) + standalone CPU MLP fallback (`96.0%` val accuracy when running without an NPU).
  - Expanded taxonomy to 10 RF classes: `wfm_stereo`, `nfm_voice`, `am_voice`, `atsc_tv`, `fsk_pager`, `ism_telemetry`, `adsb_pulse`, `glyph`, `geometric`, and `noise_reject`.
- **In-App Non-Blocking Telemetry Decoding & Reboot-Persistent UI State**:
  - Clicking POCSAG/FLEX pager, APRS (`144.390 MHz`), or ISM (`315/433/915 MHz`) signals dispatches directly into Tab 5 (`5:Radar` Tactical Feed) via background `QThread` workers instead of spawning blind terminal popups.
  - Added atomic `profiles/ui_settings.json` saving/restoring scan mode, frequency bounds, SNR/Peak gates, LNA/VGA/AMP gains, squelch, layout mode, and filter pill across reboots.

### Fixed
- **Broadcast TV Click Crash (`ui/main_window.py`)**: Eliminated the legacy `lxterminal -e w_scan_cpp` subprocess collision that fought over USB bulk endpoints and crashed/hung the session when clicking Broadcast TV rows; both `atsc_tv_rx` and legacy saved `w_scan_cpp` profiles now route cleanly through `SDRResourceArbiter` into `BroadcastTVReceiverDialog`.
- **Narrowband FM Discriminator Aliasing (`core/demod_engine.py`)**: Added `+250 kHz` digital LO rotation (`exp(-j·2π·f_off·t)`), `10×` boxcar channel decimation (`2.0 MSPS → 200 kSPS`), and `2×` audio anti-alias filtering prior to `atan2` FM discrimination so `multimon-ng` decodes POCSAG/FLEX/APRS cleanly without wideband noise folding or HackRF zero-IF DC spikes.
- **ATSC Layer 3 Pilot-Edge Framing (`core/scanner_pipeline.py`)**: Shifts Layer 3 `2.0 MSPS` IQ capture to `channel_lower_edge + 350 kHz` on `470–608 MHz` / VHF TV allocations so the `+309.441 kHz` 8VSB pilot spike lands squarely inside the `1.000 MHz` Hailo-8 spectrogram window.
- **5-Inch `800×480` DSI Geometry Budget**: Enforced zero horizontal/vertical overflow (`<= 800×444` beneath Wayland top bar) across all 5 Deck tabs and modal dialogs.

---

## [2.2.0] — 2026-11-03 — *"Fremont-VHF-Interceptor"*

### Added
- **Live 1090 MHz ADS-B Mode-S Tactical Radar (`core/adsb_receiver.py`, `ui/map_widget.py`)**:
  - Continuous `2.4 MSPS` HackRF One IQ capture with two's-complement signed `int8 → UC8` DC-bias cancellation piped into `readsb` with live aircraft vectors, callsigns, altitude, groundspeed, and range rings (`10–150 NM`).
- **Fremont County & Penrose, CO (`DM78vj`) Regional Priors**:
  - Curated frequency priors for Fremont County Sheriff, Penrose Fire/EMS, Colorado State Patrol, Cañon City Search & Rescue, Union Pacific Royal Gorge Railroad, 40-channel US-50 Trucker CB (`26.965–27.405 MHz`), Fremont County Airport (`1V6`), and Cheyenne Mountain ATSC TV towers.
- **Voice Broadcast Quick-Finder**:
  - High-visibility `[📻 VOICE BROADCAST]`, `[🎙️ WX VOICE BROADCAST]`, and `[🎙️ ATIS VOICE BROADCAST]` badges plus 1-tap filter pills (`Voice`, `All Voice`, `TV`, `Data`, `All`).

---

## [2.1.0] — 2026-10-29 — *"Zero-Crash-Gqrx"*

### Added
- **Strict Cross-Process SDR Arbiter (`core/sdr_arbiter.py`)**:
  - `fcntl.flock` hardware mutex, zero-USB-traffic `sysfs` polling (`/sys/bus/usb/devices/*/idVendor=1d50`), and passwordless `USBDEVFS_RESET` (`0x5514`) endpoint recovery.
- **Gqrx 2.17+ Crash-Flag & Serial Suffix Healer (`core/gqrx_controller.py`)**:
  - Strips leading `00000000` padding from 32-char HackRF serials (`hackrf=<suffix>`), clears stale `crash=true` flags across `~/.config/gqrx/*.conf`, prunes dead entries in `recentconfig.cfg`, and verifies live IQ demodulation over TCP `127.0.0.1:7356`.

---

## [1.0.0] — 2026-10-25 — *"Initial Hailo-8 Spectrogram Engine"*

### Added
- Initial 4-stage multi-layer HackRF One zoom-refinement scanner (`Layer 1 Wideband → Layer 2 4 MHz Zoom → Layer 3 1.000 MHz Multi-Frame Hailo-8 Pictogram Inference → Stage 4 Atomic GPS Location Profiles`), adaptive `800×480` DSI / `1080p` HDMI PyQt6 GUI, and u-blox GNSS Maidenhead grid integration.
