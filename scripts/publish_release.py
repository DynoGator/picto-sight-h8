#!/usr/bin/env python3
"""
Publish v3.3.0 release to GitHub and upload Debian 13 package & Android APK assets.
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

    body_text = """# Picto-Sight H8 v3.3.0 — "Libre-Pluto-Dual-SDR"

<p align="center">
  <img src="https://raw.githubusercontent.com/DynoGator/picto-sight-h8/main/docs/images/cover.jpg" alt="Picto-Sight H8 — DynoGatorLabs Wasteland SIGINT Edition" width="760"/>
</p>

Picto-Sight H8 v3.3.0 expands the **Dual-Node Mobile Remote SIGINT Platform** with native hardware support for the **LibreSDR HamGeek AD9363 rev.5 / PlutoSDR+ (Tezuka firmware on SD card)** alongside the **HackRF One**, bringing 12-bit ADC dynamic range (>70 dB) and Gigabit Ethernet SDR connectivity to the Raspberry Pi 5 + Hailo-8 NPU base station and Google Pixel 9 Pro XL handset running GrapheneOS.

### 🌟 What's New in v3.3.0

1. **Native LibreSDR PlutoSDR+ (AD9363 rev.5) Acquisition Engine (`core/pluto_engine.py`)**:
   - 12-bit complex IQ acquisition via `libiio` over Gigabit Ethernet (port `30431`) and USB gadget.
   - Calibrated hardware logarithmic RSSI readouts from internal AD9361 power detectors.
   - Fast frequency hopping wideband sweeps (47 MHz – 6000 MHz) and Layer 3 1.000 MHz STFT spectrogram generation for Hailo-8 26 TOPS AI classification.
   - Fixed AD9361 PLL lock to onboard 40.000 MHz TCXO reference clock.

2. **Multi-SDR Resource Arbiter & Hardware Autodetection (`core/sdr_arbiter.py`)**:
   - Zero-collision mutual exclusion between LibreSDR Pluto (12-bit) and HackRF One (8-bit).
   - Compiled and packaged native `libPlutoSDRSupport.so` for Gqrx and SoapySDR.
   - Adaptive sample rate configuration and crash sanitization.

3. **Mobile Remote Handset APK v2.2.0 (`handset/apk/PictoSight-H8-2.2.0.apk`)**:
   - Multi-IP auto-discovery connecting seamlessly across `10.83.29.90`, `10.67.191.8`, `10.67.191.90`, and `192.168.2.10`.
   - Live SDR toggling (LibreSDR 12-bit vs HackRF One) and live hardware RSSI readouts.
   - Clean v3 APK signing and AAPT2 build for hardened GrapheneOS / Pixel 9 Pro XL.

4. **Automated Verification**:
   - 52/52 pytest tests passing (100% test pass rate).
   - Tested live over Gigabit Ethernet with 0.26 ms RTT.

### 📦 Installation

**Debian 13 (Trixie) / Raspberry Pi OS ARM64:**
```bash
sudo apt update
sudo apt install -y ./picto-sight-h8_3.2.0-1.trixie_arm64.deb
```

**Mobile Handset Sideload (Pixel 9 Pro XL / Android):**
```bash
./handset/install_handset.sh
# Or download directly from your Pi in browser:
# http://<pi_ip>:8765/apk
```
"""

    payload = {
        'tag_name': 'v3.3.0',
        'target_commitish': 'main',
        'name': 'Picto-Sight H8 v3.3.0 (LibreSDR Pluto Dual-SDR & Handset v2.2.0)',
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
        req_get = urllib.request.Request(
            'https://api.github.com/repos/DynoGator/picto-sight-h8/releases/tags/v3.3.0',
            headers={
                'Authorization': f'token {pat}',
                'Accept': 'application/vnd.github.v3+json',
                'User-Agent': 'picto-sight-releaser'
            }
        )
        with urllib.request.urlopen(req_get) as resp:
            release = json.loads(resp.read().decode('utf-8'))
            upload_url_base = release['upload_url'].split('{')[0]

    deb_path = '/home/dynogator/ai-rf-smart-scanner/dist/picto-sight-h8_3.2.0-1.trixie_arm64.deb'
    deb_sha = deb_path + '.sha256'
    apk_path = '/home/dynogator/ai-rf-smart-scanner/handset/apk/PictoSight-H8-2.2.0.apk'
    apk_sha = apk_path + '.sha256'

    for asset_file in [deb_path, deb_sha, apk_path, apk_sha]:
        if not os.path.isfile(asset_file):
            print(f"Asset file not found: {asset_file}")
            continue
        name = os.path.basename(asset_file)
        with open(asset_file, 'rb') as f:
            data = f.read()
        if name.endswith('.deb'):
            content_type = 'application/vnd.debian.binary-package'
        elif name.endswith('.apk'):
            content_type = 'application/vnd.android.package-archive'
        else:
            content_type = 'text/plain'
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
