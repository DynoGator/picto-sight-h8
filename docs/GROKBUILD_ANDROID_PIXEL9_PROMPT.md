# GrokBuild Prompt: Native Android Port of Picto-Sight H8
## Target: Google Pixel 9 Pro XL running GrapheneOS

**Build a production-grade, privacy-first native Android application** that adapts the full Picto-Sight H8 architecture (https://github.com/DynoGator/picto-sight-h8) for on-device use on a Google Pixel 9 Pro XL running GrapheneOS.

### Primary Goals
- Utilize the **Tensor G4 Edge TPU / NPU** for real-time visual spectrogram pictogram classification (equivalent to the original Hailo-8 26 TOPS path).
- Support **OTG-connected SDRs**:
  - Great Scott Gadgets **HackRF One** (1 MHz – 6 GHz)
  - **LibreSDR rev.5** (AD9363-based PlutoSDR+ equivalent) with custom-compiled Tazuka firmware (USB Ethernet/ECM or bulk IQ streaming).
- Support **onboard Pixel sensors** as a fallback or complementary source (usable with or without external SDR):
  - Magnetometer (for EMI / near-field magnetic anomalies)
  - Accelerometer / Gyroscope
  - Barometer / Pressure
  - Microphone (for demodulated audio or acoustic coupling)
  - Ambient light / proximity (optional context)
- Visually classify **video streams**:
  - Local cameras (rear + front + ultrawide)
  - RTSP streams from a **WyseCam** (or compatible IP camera)
- Produce the same core outputs as the Pi version: numerically ordered viable signal table, Location Profiles, annotated spectrogram strips, and one-tap live demod / tuning.

### Hardware & OS Constraints
- **Device**: Google Pixel 9 Pro XL (Tensor G4, 16 GB RAM, hardware memory tagging).
- **OS**: GrapheneOS (official production support). Avoid any Google Play Services dependency. Use only AOSP-compatible APIs, Android USB Host API, Camera2/CameraX, SensorManager, and open-source ML runtimes (LiteRT / TensorFlow Lite with NPU delegate, or NNAPI where available on Tensor).
- **Power**: OTG SDRs must be powered (use powered USB-C hub or SDR with external power). App must detect and gracefully handle USB disconnects / reconnects.
- **Privacy**: All inference and processing stays on-device. No telemetry, no cloud. GrapheneOS-hardened networking and storage.

### Core Functional Requirements (Port from picto-sight-h8)

#### 1. Multi-Stage Zoom-Refinement RF Pipeline
Reimplement the 4-stage loop:
1. **Wideband overview** — sweep or scan selected span (default 88–163 MHz or user custom 1–6000 MHz). Compute robust noise floor (20th percentile). Apply Min SNR and Min Peak Power gates. Cross-reference regional bandplans (FCC_US, ITU-R1, ITU-R3).
2. **Fine-zoom verification** — 4.0 MHz windows at ~100 kHz resolution. Channel raster snapping (200 kHz FM, 25 kHz airband/marine/NOAA, 12.5 kHz LMR/amateur, etc.). Adjacent sidelobe deduplication.
3. **Deep 1.000 MHz multi-frame classification**:
   - Capture IQ (target 2.0 MSPS 8-bit or equivalent from HackRF / AD9363).
   - Generate ≥3 (Quick) or 5 (Deep) independent STFT spectrogram frames (64×128 Hann or equivalent).
   - Extract spectral-floor carrier ROIs and waterfall transient ROIs.
   - Normalize to 224×224×3 Viridis (or equivalent) tensors.
   - Run inference on **Tensor Edge TPU / NPU** using a converted or re-trained head equivalent to `models/hailo_pictogram_head_weights.npz`.
   - Classes: `carrier_comb`, `tone_matrix`, `geometric`, `glyph`, `raster`, `noise_reject`.
   - Fuse with physical IQ metrics (99 % OBW, FM phase deviation vs AM envelope depth, duty cycle).
   - Reject unmodulated internal clock-harmonic spurs (10/20/25/32/40/48/50/80/100/125 MHz).
4. **Refinement & persistence** — strict ascending frequency sort, atomic JSON Location Profiles (locality + Maidenhead + timestamp + mode), annotated multi-frame PNG strips, unlock of the signal table.

#### 2. SDR Sources (OTG)
- **HackRF One**: Use Android USB Host API (libusb-style). Prefer pure-Kotlin drivers (e.g. libhackrfk style) or native via NDK if necessary. Support sweep + IQ transfer. Handle LPC43xx bulk endpoint stalls with reset if possible.
- **LibreSDR / AD9363 (Tazuka firmware)**: Detect as USB Ethernet (ECM) or bulk device. Implement IIO / custom protocol for frequency, sample rate, gain control and IQ streaming. Support the custom Tazuka build.
- Strict single-owner arbiter equivalent (no concurrent open of the same USB device).
- Passive detection of connected SDRs without constant control traffic.

#### 3. Onboard Sensor Mode (No SDR Required)
- Continuous or triggered sampling of magnetometer, accelerometer, gyro, barometer, mic.
- Map sensor streams into “spectrogram-like” or feature tensors suitable for the same pictogram classifier (or a dedicated sensor-fusion head).
- Useful for near-field EMI detection, vibration-coupled RF, acoustic demod, or multi-modal anomaly detection.

#### 4. Video Classification
- Local Camera2/CameraX streams (rear main, ultrawide, front).
- RTSP client for WyseCam (or generic RTSP URL).
- Extract frames or short clips → same Tensor NPU pipeline for visual classification (object, anomaly, optical artifact, or joint RF+video correlation).
- Overlay or side-by-side display with RF spectrograms when both are active.

#### 5. UI / UX (Adaptive)
- Dark, tactical, high-contrast theme optimized for outdoor / low-light use on the Pixel 9 Pro XL OLED.
- Dual layout:
  - Portrait field mode (compact controls, large touch targets).
  - Landscape or multi-pane studio mode when external display or fold is available.
- Live wideband trace, multi-frame AI spectrogram strip, clickable/tappable signal table, GPS/location profile manager.
- One-tap “tune” action that either:
  - Launches a local demod path (audio via AudioTrack / Oboe), or
  - Exports a Gqrx-compatible bookmark / frequency list for later use.
- SDR reset button, gain sliders (LNA/VGA equivalent), squelch, waterfall floor/ceiling.

#### 6. Persistence & Profiles
- Reboot-persistent Location Profiles (JSON) stored in app-private storage (GrapheneOS-friendly).
- Maidenhead grid, lat/lon from Android LocationManager / GNSS (or offline cache).
- Export of signal tables and annotated spectrograms.

#### 7. Additional Engineering Requirements
- All ML inference via LiteRT (TensorFlow Lite) with NPU/NNAPI delegate targeting Tensor G4. Provide a conversion path or quantized model that approximates the original Hailo pictogram head.
- Background scanning with proper foreground service + notification (GrapheneOS battery optimization compatible).
- Full USB permission handling and reconnect logic.
- Unit-testable core pipeline (Kotlin).
- No proprietary Google ML Kit dependency unless it can run fully offline without Play Services; prefer open LiteRT.
- MIT-compatible license, clear receive-only legal notice.

### Deliverables Expected from GrokBuild
1. Complete Android Studio project (Kotlin, Jetpack Compose preferred) that builds and runs on Pixel 9 Pro XL / GrapheneOS.
2. Working OTG HackRF and LibreSDR IQ capture + spectrogram generation.
3. Tensor NPU inference path for the pictogram classifier (with CPU fallback).
4. Onboard sensor mode.
5. Local camera + RTSP video classification.
6. Adaptive UI with signal table, live traces, and profile management.
7. README with build instructions, OTG setup notes for GrapheneOS, and model conversion steps.

### Reference Architecture (from original repo)
- Core modules to conceptually port: `scanner_pipeline.py`, `hailo_pictogram.py`, `pictogram_engine.py`, `hackrf_engine.py`, `sdr_arbiter.py`, `gps_engine.py`, `bandplans.py`, `profile_organizer.py`, UI widgets.
- Model: `models/hailo_pictogram_head_weights.npz` (convert or re-implement the BatchNorm + MLP head for LiteRT).

Build this as a self-contained, GrapheneOS-native, Tensor-accelerated successor to Picto-Sight H8. Prioritize reliability of the OTG SDR path and the quality of the multi-frame spectrogram classification.

---
*Generated for DynoGator / Resonant Genesis — Pixel 9 Pro XL + GrapheneOS + Tensor edge AI port.*
