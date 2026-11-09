#!/usr/bin/env python3
"""
Publish v3.2.0 release to GitHub and upload Debian 13 package & Android APK assets.
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

    body_text = """# Picto-Sight H8 v3.2.0 — "DynoGatorLabs Wasteland SIGINT Edition"

<p align="center">
  <img src="https://raw.githubusercontent.com/DynoGator/picto-sight-h8/main/docs/images/cover.jpg" alt="Picto-Sight H8 — DynoGatorLabs Wasteland SIGINT Edition" width="760"/>
</p>

Picto-Sight H8 v3.2.0 delivers the full **Dual-Node Mobile Remote SIGINT Platform**, pairing the Raspberry Pi 5 + Hailo-8 AI HAT+ + HackRF One base station with a **Google Pixel 9 Pro XL** handset running **GrapheneOS**, featuring the **Vault-Tec SIGINT Overseer Terminal (`Ctrl+Shift+D`)**, and complete **DynoGatorLabs** wasteland branding.

### 🌟 What's New in v3.2.0

1. **Dual-Node Mobile Remote Node (Pixel 9 Pro XL / GrapheneOS)**:
   - Release APK: `PictoSight-H8-2.1.0.apk` (`llc.resgen.pictosight`), free of Google Play Services dependencies for hardened GrapheneOS and AOSP devices.
   - 1-Command sideload installer: `./handset/install_handset.sh` with automated ADB detection and reverse port tunnels.
   - Remote control over Wi-Fi hotspot (`10.67.191.90:8765`) or Tailscale mesh (`100.64.0.1:8765`).
   - Tactile touch interface with 4-stage sweep telemetry, frequency scrubber, fine stepper (`±1k/10k/100k/1M`), volume controls, and tactical radar rings (`25/50 NM`).
   - 48 kHz mono UDP demodulation audio streaming direct from Pi node to handset over port `7355`.

2. **DynoGatorLabs Vault-Tec SIGINT Overseer Terminal (`Ctrl+Shift+D`)**:
   - Hidden RobCo / Pip-Boy green phosphor CRT terminal dialog in desktop GUI.
   - Animated radiation monitor (`0.003 mR/h RAD-SAFE`), ASCII terminal art, and wasteland lore.
   - 1-Touch Quick Tune buttons for DynoGatorLabs frequencies:
     - `146.520 MHz FM` — DGL Emergency Wasteland Calling Net
     - `101.300 MHz WFM` — Vault 101 Wasteland Radio
     - `1240.0 kHz AM` — Conelrad Emergency Broadcast Relay

3. **Wasteland SIGINT Bridge Daemon (`scripts/pi_remote_bridge.py`)**:
   - Multi-threaded WebSocket and HTTP companion server on `0.0.0.0:8765`.
   - Direct in-browser APK downloads (`/apk`), hero cover art delivery (`/cover.jpg`), and companion web GUI.
   - Systemd unit `picto-sight-bridge.service` enabled for automatic boot autostart.

4. **Robust Automated Installers**:
   - Upgraded `install.sh` and `handset/install_handset.sh` with ASCII art headers, bridge service installation, and ADB handset detection.
   - Automated test suite passes 45/45 tests (100% pass rate).

### 📦 Installation

**Debian 13 (Trixie) / Raspberry Pi OS ARM64:**
```bash
sudo apt update
sudo apt install -y ./picto-sight-h8_3.2.0-1.trixie_arm64.deb
picto-sight --doctor
picto-sight --gui
```

**Mobile Handset Sideload (Pixel 9 Pro XL / Android):**
```bash
./handset/install_handset.sh
# Or download directly from your Pi in browser:
# http://<pi_ip>:8765/apk
```
"""

    payload = {
        'tag_name': 'v3.2.0',
        'target_commitish': 'main',
        'name': 'Picto-Sight H8 v3.2.0 (DynoGatorLabs Wasteland SIGINT Edition)',
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
            'https://api.github.com/repos/DynoGator/picto-sight-h8/releases/tags/v3.2.0',
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
    apk_path = '/home/dynogator/ai-rf-smart-scanner/handset/apk/PictoSight-H8-2.1.0.apk'
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
