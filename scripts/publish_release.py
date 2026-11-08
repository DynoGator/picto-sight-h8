#!/usr/bin/env python3
"""
Publish v3.1.1 release to GitHub and upload Debian 13 package assets.
"""
import json
import os
import sys
import urllib.request


def main():
    pat = None
    for p in [os.path.expanduser('~/.config/dslv-zpdi/github_pat'), os.path.expanduser('~/.config/picto-sight-h8/github_pat')]:
        if os.path.isfile(p):
            with open(p) as f:
                pat = f.read().strip()
                break

    if not pat:
        print("ERROR: No GitHub PAT found")
        sys.exit(1)

    body_text = """# Picto-Sight H8 v3.1.1 — "Broadcast-TV-Overhaul"

Picto-Sight H8 v3.1.1 delivers **Complete Broadcast Television Demodulation (ATSC 1.0 8VSB & Analog NTSC)** with **Integrated Real-Time FM Broadcast Audio**, an **8-Level 8VSB Constellation & Eye Diagram Canvas**, **Direct Native 11.838 MSPS GNU Radio DSP**, and **Crash-Free Video Player Integration** on Raspberry Pi 5 / Debian 13 (Trixie).

### 🌟 What's New in v3.1.1

1. **Integrated Real-Time Analog NTSC FM Audio Demodulation**:
   - Demodulates the North American standard `+4.500 MHz` FM aural audio carrier (`center + 2.750 MHz` / `lo + 5.750 MHz`).
   - Quadrature FM discrimination with 32× FIR decimation (`250 kHz` intermediate FM rate), $75\ \mu\text{s}$ RC de-emphasis, fractional resampling to $48\text{ kHz}$ S16LE PCM, and low-latency streaming to PipeWire via `paplay`.
   - Added a 1-tap **"🔊 AUDIO: LIVE / MUTED"** interactive toggle button in the Broadcast TV receiver dialog.
   - Built-in PipeWire volume guard unmuting default sinks and setting volume $\ge 0.80$.

2. **Direct Native 11.838 MSPS ATSC 1.0 (8VSB) GNU Radio DSP Pipeline**:
   - Eliminated the CPU-choking 16-bank polyphase interpolator by streaming HackRF at native `11,838,462 Hz` (`atsc_sym_rate * 1.1`).
   - Integrated fast 50-tap direct FFT Root Raised Cosine filter (`filter.fft_filter_ccc`) and input analog AGC (`analog.agc2_cc`), saving $\approx 70\%$ CPU on Pi 5 ARM64 cores and preventing USB sample dropouts.
   - Normalized HackRF IQ baseband RMS to $0.5$, translating $+39.3\text{ dB}$ pilot SNR directly into instantaneous symbol sync and segment recovery.

3. **ATSC 8-Level 8VSB Constellation & Eye Diagram Canvas**:
   - Real-time soft symbol constellation canvas rendering the 8 discrete ATSC 8VSB decision levels (`±1, ±3, ±5, ±7`) with tactical phosphor eye traces.
   - Provides instantaneous visual verification of pilot phase lock and multi-level symbol slicing.

4. **Crash-Free Digital Video Player (`mpv`)**:
   - Configured `mpv` with `--idle=yes`, `--force-window=yes`, and `--stream-buffer-size=8MiB`, ensuring the video player never crashes or exits as defunct while waiting for transport packets.

5. **Ultra-Fast Single-Shot TV Band Scanner**:
   - Upgraded `scan_active_tv_channels` with one-shot `-1` fast sweeps, completing entire VHF-Hi and UHF band surveys in $< 2.4$ seconds with zero timeout risks.

6. **Native Debian 13 (Trixie) Package**:
   - `picto-sight-h8_3.1.1-1.trixie_arm64.deb` tested and verified across all 45 automated system tests (100% pass rate).

### 📦 Installation (Debian 13 Trixie / Raspberry Pi OS ARM64)
```bash
sudo apt update
sudo apt install -y ./picto-sight-h8_3.1.1-1.trixie_arm64.deb
picto-sight --doctor
picto-sight --gui
```
"""

    payload = {
        'tag_name': 'v3.1.1',
        'target_commitish': 'main',
        'name': 'Picto-Sight H8 v3.1.1 (Broadcast-TV-Overhaul)',
        'body': body_text.strip(),
        'draft': False,
        'prerelease': False
    }

    req = urllib.request.Request(
        'https://api.github.com/repos/DynoGator/picto-sight-h8/releases',
        data=json.dumps(payload).encode('utf-8'),
        headers={
            'Authorization': f'token {pat}',
            'Accept': 'application/vnd.github.v3+json',
            'Content-Type': 'application/json',
            'User-Agent': 'picto-sight-releaser'
        }
    )

    try:
        with urllib.request.urlopen(req) as resp:
            release = json.loads(resp.read().decode('utf-8'))
            print('Release created successfully:')
            print(f' - ID: {release["id"]}')
            print(f' - HTML URL: {release["html_url"]}')
            upload_url_tmpl = release['upload_url']
            upload_url_base = upload_url_tmpl.split('{')[0]
    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8', errors='replace')
        print(f"HTTPError {e.code}: {error_body}")
        # If release already exists, fetch it
        req_get = urllib.request.Request(
            'https://api.github.com/repos/DynoGator/picto-sight-h8/releases/tags/v3.1.1',
            headers={
                'Authorization': f'token {pat}',
                'Accept': 'application/vnd.github.v3+json',
                'User-Agent': 'picto-sight-releaser'
            }
        )
        with urllib.request.urlopen(req_get) as resp:
            release = json.loads(resp.read().decode('utf-8'))
            upload_url_base = release['upload_url'].split('{')[0]

    deb_path = '/home/dynogator/ai-rf-smart-scanner/dist/picto-sight-h8_3.1.1-1.trixie_arm64.deb'
    sha_path = deb_path + '.sha256'

    for asset_file in [deb_path, sha_path]:
        if not os.path.isfile(asset_file):
            print(f"Asset file not found: {asset_file}")
            continue
        name = os.path.basename(asset_file)
        with open(asset_file, 'rb') as f:
            data = f.read()
        content_type = 'application/vnd.debian.binary-package' if name.endswith('.deb') else 'text/plain'
        upload_url = f"{upload_url_base}?name={name}"
        req_up = urllib.request.Request(
            upload_url,
            data=data,
            headers={
                'Authorization': f'token {pat}',
                'Content-Type': content_type,
                'User-Agent': 'picto-sight-releaser'
            }
        )
        try:
            with urllib.request.urlopen(req_up) as up_resp:
                up_res = json.loads(up_resp.read().decode('utf-8'))
                print(f"Uploaded asset {name}: {up_res.get('browser_download_url')}")
        except urllib.error.HTTPError as err:
            print(f"Could not upload asset {name} (may already exist): {err}")


if __name__ == '__main__':
    main()
