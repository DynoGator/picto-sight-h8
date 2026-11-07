# Picto-Sight H8 — Operator & Cyberdeck Field Manual

Welcome to **Picto-Sight H8 v3.0.0 (`Trixie-Ghost-Spectrum`)**.  
Whether you are perched on a tailgate in Fremont County watching Mode-S transponders cross the Sangre de Cristos on a **5-inch `800×480` DSI touchscreen**, or running a **1080p+ 4-pane HDMI SIGINT desk**, every control is engineered for zero-lag, 1-at-a-time USB arbitration.

---

## 1. Launching the Cockpit

### From the Raspberry Pi Desktop (1-Tap Icons)
The installer places four launchers on your Desktop and Application Menu:
- **Picto-Sight H8 RF Scanner** — Opens the adaptive GUI in your last saved state (`profiles/ui_settings.json`).
- **Quick RF Location Scan** — Opens (or raises) the GUI and immediately runs a fast 3-frame/MHz scan.
- **Deep AI RF Location Scan** — Runs a high-precision 5-frame/MHz Hailo-8 scan and saves a GPS Location Profile.
- **Custom RF Profile Scan** — Opens custom threshold/demod controls and triggers a tailored scan.

> [!TIP]
> Single-instance UNIX flock + atomic JSON IPC (`/tmp/smart_rf_scanner_gui.lock`) guarantees that tapping Desktop icons while the app is already open **raises and commands the existing window** instead of spawning evil twin processes fighting over your HackRF One.

### From Any Terminal
```bash
picto-sight --gui
```

---

## 2. Adaptive Display Topologies (`800×480` DSI vs. `1080p+` HDMI)

Picto-Sight H8 inspects screen geometry on startup and whenever displays change:
- **5-Inch DSI Field Deck (`800×480` / `800×444` usable)**: Uses a zero-scroll **5-Tab Deck** (`1:Scan`, `2:RF+AI`, `3:Signals`, `4:Profiles`, `5:Radar`) with oversized touch targets and compact telemetry badges.
- **HDMI SIGINT Studio (`≥ 1024×640`)**: Expands into a simultaneous **4-Pane Split Cockpit** showing Scan Controls, Live Wideband RF + 3-Frame AI Waterfall, Signals Table, and Tactical Radar/Profiles side-by-side.
- Manual override anytime via the **`Layout: Auto / Deck / Studio`** selector in Tab 1.

---

## 3. Step-by-Step Field Workflow

### Tab 1 — `1:Scan` (Sweep Mode, Thresholds & Hardware Gains)
1. **Pick a Band Preset or Custom Span** (`1 MHz – 6000 MHz`):
   - Presets include *FM Broadcast (`88–108 MHz`)*, *VHF Airband (`118–137 MHz`)*, *Fremont/Penrose Public Safety & Rail (`150–163 MHz`)*, *Trucker CB (`26–28 MHz`)*, *UHF Broadcast TV (`470–608 MHz`)*, *ISM 433 / 915*, and *Wideband V/UHF*.
2. **Choose Scan Depth**:
   - **QUICK**: Fast sweep + `3 × 1.000 MHz` STFT frames per candidate on Hailo-8.
   - **DEEP**: Fine integration + `5 × 1.000 MHz` STFT frames per candidate for weak/intermittent LMR & SAR traffic.
   - **CUSTOM**: Full manual control over Demod override, **Min SNR (`dB`)**, **Min Peak (`dBFS`)**, **LNA (`0–40 dB`)**, and **VGA (`0–62 dB`)**.
3. **Tap `▶ START SCAN`**:
   - If Gqrx, Broadcast TV, or ADS-B Radar is currently holding the HackRF One, the **SDR Resource Arbiter** cleanly parks it first, verifies bulk endpoint health, and locks the signal table (`🔒 SCANNING`) until Stage 4 completes.
4. **Need an Instant USB Exorcism?**
   - Tap **`⟳ Reset SDR`** in the top status bar to fire a kernel `USBDEVFS_RESET` (`0x5514`) ioctl directly at the HackRF One without unplugging the cable or typing `sudo`.

---

### Tab 2 — `2:RF+AI` (Wideband Spectrum & 3-Frame Hailo-8 Waterfall)
- **Top Graph**: Live Layer 1 / Layer 2 wideband power trace (`dBFS`), adaptive 20th-percentile noise floor, and highlighted candidate peaks.
- **Bottom Strip**: Real-time side-by-side **`1.000 MHz` STFT spectrogram frames** (`F1 | F2 | F3`) showing the exact 2D carrier ROIs fed into `/dev/hailo0` along with neural class (`wfm_stereo`, `nfm_voice`, `am_voice`, `atsc_tv`, `fsk_pager`, `ism_telemetry`, `adsb_pulse`, etc.) and confidence.

---

### Tab 3 — `3:Signals` (Filter Pills & 1-Tap Instant Demodulation)
Once Stage 4 finishes, signals are sorted in **strict ascending frequency order** and unlocked (`🔓 TAP ANY ROW`):

- **Quick Filter Pills**:
  - **`📻 Voice`** — Commercial FM/AM, NOAA Weather (`WX`), and ATIS continuous voice broadcasts (`V` shortcut).
  - **`🗣️ All Voice`** — Adds Public Safety (Sheriff/Fire/EMS), Search & Rescue, Aviation CTAF, Railroad AAR, Amateur, and 40-channel Trucker CB 2-way voice.
  - **`📺 TV`** — Terrestrial VHF/UHF ATSC 8VSB & NTSC television channels.
  - **`📡 Data`** — POCSAG/FLEX pagers, `144.390 MHz` APRS, `1090 MHz` ADS-B, and `315/433/915 MHz` IoT telemetry.
  - **`🌐 All`** — Every verified legal carrier in the profile.

- **What Happens When You Tap a Row?**
  - **Voice / Audio Row (`gqrx`)**: Synthesizes a calibrated Gqrx profile (`WIDE 2.4 MSPS` for FM Stereo; `NARROW 2.0 MSPS dec=2` for NFM/AM/CB/SSB), launches Gqrx (or fast-tunes an already-running Gqrx over `127.0.0.1:7356` in `< 80 ms`), sets PipeWire audio to `0 dB`, and verifies live IQ signal strength (`dBFS`).
  - **Broadcast TV Row (`atsc_tv_rx`)**: Releases Gqrx cleanly and opens the **Broadcast Television Receiver** dialog tuned to that exact `6 MHz` TV channel.
  - **ADS-B `1090 MHz` Row (`readsb`)**: Switches to Tab 5 (`5:Radar`) and starts the live `1090 MHz` Mode-S aircraft radar receiver.
  - **Pager / APRS / ISM Telemetry Row (`multimon_ng` / `rtl_433`)**: Switches to Tab 5 (`5:Radar`) and decodes live bursts directly into the Tactical Telemetry Feed.

---

### Broadcast Television Receiver (`📺 TV` Button or Any TV Signal Row)
Accessible anytime from the top header **`📺 TV`** button or by clicking any `[📺 BROADCAST TV]` signal:
1. **Digital ATSC 1.0 (`8VSB`) Mode**:
   - Captures `8.0 MSPS` IQ from HackRF One through GNU Radio's `dtv.atsc_rx` pipeline (`AGC → FPLL → Timing/Equalizer → Viterbi → Reed-Solomon → Deinterleaver`).
   - Renders the live `6.0 MHz` RF channel spectrum, highlights the **`+309.441 kHz` 8VSB pilot spike**, tracks **Pilot SNR (`dB`)**, **Channel SNR**, **Estimated MER**, and **MPEG-TS bitrate (`kbps`)**.
   - Streams decoded MPEG-TS to `udp://127.0.0.1:17355` and launches low-latency **`mpv`** video/AC-3 audio automatically once MPEG-TS sync (`0x47`) locks (or on demand via **`🎬 Video Player`**).
2. **Analog NTSC (`525-Line`) CRT Mode**:
   - Select **`Analog NTSC (525-line)`** to demodulate AM visual vestigial-sideband video at `4.0 MSPS` into a live `525-line / 29.97 fps` monochrome CRT raster monitor.
3. **Channel Presets & Band Scan**:
   - Includes 1-tap Southern Colorado presets (**Ch 25 KOAA NBC**, **Ch 24 KKTV CBS**, **Ch 22 KXRM FOX**, **Ch 19 KRDO ABC**, **Ch 8 KTSC PBS**) plus **`🔍 Scan TV`** across VHF/UHF channels `2–36`.
4. **Wideband Gqrx Inspection**:
   - Tap **`📻 8M Gqrx`** to hand off the channel to Gqrx in `8.0 MSPS` wideband mode (`decim=1`) so the full `6 MHz` pedestal is visible.

---

### Tab 4 — `4:Profiles` (GPS Location Profiles & Gqrx Bookmarks)
- Every completed scan writes an atomic, fsynced JSON archive + spectrogram strip + two-table Gqrx `bookmarks.csv` tagged with your live GPS fix or offline Maidenhead grid (e.g., `Penrose_CO_DM78vj_...`).
- Reload any historical Location Profile with 1 tap—instantly restoring its entire clickable signal table and updating `~/.config/gqrx/bookmarks.csv`.

---

### Tab 5 — `5:Radar` (Live `1090 MHz` ADS-B Scope & Decoded Telemetry Feed)
- Tap **`✈ Live 1090 ADS-B`** to stream `2.4 MSPS` HackRF IQ (`int8 → UC8` DC-cancelled) into `readsb`.
- Displays concentric range rings (`10 / 25 / 50 / 100 / 150 NM`), local landmarks (`1V6` Fremont County Airport, `PUB`, `COS`, Cheyenne Mtn TV), aircraft callsigns, ICAO hex, altitude (`ft`), groundspeed (`kt`), heading vectors, and live message rate (`msg/s`).
- Also hosts quick-decode buttons and the scrolling **Tactical Telemetry Feed** for **POCSAG/FLEX pagers**, **`144.390 MHz` APRS**, and **`433.92 / 915 MHz` `rtl_433` weather/TPMS/IoT sensors**.

---

## 4. Complete CLI Cheat-Sheet

```bash
# Check version & release codename
picto-sight --version

# Run the 25-point full-stack hardware/dependency doctor
picto-sight --doctor
picto-sight --doctor-json

# Provision / heal Gqrx 2.17+ configuration & starter bookmarks
picto-sight --provision-gqrx

# Launch adaptive GUI (or trigger immediate scan via single-instance IPC)
picto-sight --gui
picto-sight --gui --auto-scan quick
picto-sight --gui --auto-scan deep

# Headless CLI scans (saves permanent GPS Location Profile + Gqrx bookmarks)
picto-sight --scan --mode quick --min 88 --max 108
picto-sight --scan --mode deep --min 150 --max 163 --snr 8.0
picto-sight --scan --mode quick --min 88 --max 163 --voice-only
picto-sight --scan --mode quick --min 88 --max 108 --tune-top

# Direct verified Gqrx tuning from CLI
picto-sight --tune-freq 104.5
picto-sight --tune-freq 155.880

# Standalone live telemetry, pager/APRS & ATSC TV analysis
picto-sight --telemetry 433.92
picto-sight --decode-data 155.880
picto-sight --decode-data 144.390
picto-sight --analyze-tv 539.0

# Print the built-in Fremont County / Penrose, CO & 40-Ch CB frequency guide
picto-sight --fremont-guide

# List all saved Location Profiles
picto-sight --list-profiles
```

---

## 5. Regulatory & ECPA Notice

Picto-Sight H8 operates **strictly in receive-only (`RX`) mode**. Built-in regional bandplans (`FCC_US`, `ITU_R1`, `ITU_R3`) automatically flag and gate cellular allocations per the U.S. Electronic Communications Privacy Act (ECPA) / FCC Part 15. Know your local spectrum laws, don't transmit with tools that aren't transmitters, and enjoy the ether responsibly.
