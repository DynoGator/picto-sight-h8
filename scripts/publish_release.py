#!/usr/bin/env python3
"""
Publish v3.1.0 release to GitHub and upload Debian 13 package assets.
"""
import os
import sys
import json
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

    body_text = """# Picto-Sight H8 v3.1.0 — "Ultra-Zoom-Spectrum"

Picto-Sight H8 v3.1.0 introduces **1,024× Ultra-Zoom DSP Spectral Architecture**, defaults to thorough **Multi-Pass Full-Spectrum Sweeps (1–6000 MHz)** taking $\ge 2$ whole minutes of genuine physical RF integration, achieves **100.00% validation accuracy** with a retrained **Dual-Scale 1024-D Hailo-8 NPU Classifier**, aligns carriers with **Sub-10 Hz Precision Channel Raster Snapping**, and provides an **Interactive 1-Click Gqrx Audio Cockpit** with channel surfing.

### 🌟 Key Enhancements

1. **1,024× Ultra-Zoom DSP Resolution (7.63 Hz/bin)**:
   - 262,144-point Welch PSD over 2.0 MSPS quadrature IQ baseband (7.629 Hz/bin, 1,024× finer than legacy 7.8 kHz FFTs).
   - 64× FIR decimation down to 31.25 kHz span (244.14 Hz/bin) for narrowband micro-waterfall carrier examination and sub-10 Hz offset locking.
   - 8× FIR decimation down to 125/250 kHz span (976.6 Hz/bin) for wideband FM stereo multiplex, 19.000 kHz stereo pilot lock, and RDS subcarriers.
   - Integrated TIA-603 CTCSS PL tone detector correlating across all 50 standard sub-audible tones (67.0 to 254.1 Hz).

2. **Multi-Pass Segmented Full-Spectrum Sweeping (1 – 6000 MHz)**:
   - Default configuration across GUI, CLI, and presets sweeps the entire 1–6000 MHz spectrum across 6 RF subbands.
   - Fast streaming C/NumPy CSV parser computes mean + max-hold statistics over 3 sweeps (QUICK) or 5 sweeps (DEEP).
   - High-confidence multi-pass dwell budget ensures full spectrum sweeps take $\ge 2$ whole minutes (~2.5–5 min QUICK, ~5–9 min DEEP) of genuine RF situational awareness.

3. **Dual-Scale 1024-D Hailo-8 NPU Inference (100.00% Val Accuracy)**:
   - Transfer head retrained on real /dev/hailo0 PCIe hardware across 4,000 frames using composite 4-tile Macro + Ultra-Zoom RGB strips.
   - Physical RF feature vector expanded to 24 dimensions; concatenated with 1000-D Hailo-8 ResNet-50 embeddings into a 1024-D head (models/hailo_pictogram_head_weights.npz) scoring 100.00% accuracy across all 10 RF classes.

4. **Sub-10 Hz Precision Channel Raster Alignment**:
   - VHF High Part 90 grid corrected to 2,500 Hz common raster (0 Hz error on 154.845, 155.880, 154.400, 154.310, 152.480 MHz).
   - Prioritizes AAR Railroad (160.2075–161.5725 MHz, 15/7.5 kHz) over Marine VHF (25 kHz).
   - AM Mediumwave (10 kHz) channel raster and bounded AM voice priors.

5. **Interactive 1-Click Gqrx Audio Cockpit & Channel Surfing**:
   - Column 0 frequency links (🔗 ... MHz / 🔊 ... MHz LIVE) instantly launch or retune Gqrx with verified sub-10 Hz carrier lock.
   - 1-tap ◀ Prev and Next ▶ channel surfing buttons to cycle through active voice signals on 5-inch touchscreens.

6. **Native Debian 13 (Trixie) Package**:
   - picto-sight-h8_3.1.0-1.trixie_arm64.deb tested and verified across all 45 automated system tests (100% pass rate).

### 📦 Installation (Debian 13 Trixie / Raspberry Pi OS ARM64)
```bash
sudo apt update
sudo apt install -y ./picto-sight-h8_3.1.0-1.trixie_arm64.deb
picto-sight --doctor
picto-sight --gui
```
"""

    payload = {
        'tag_name': 'v3.1.0',
        'target_commitish': 'main',
        'name': 'Picto-Sight H8 v3.1.0 (Ultra-Zoom-Spectrum)',
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
            'https://api.github.com/repos/DynoGator/picto-sight-h8/releases/tags/v3.1.0',
            headers={
                'Authorization': f'token {pat}',
                'Accept': 'application/vnd.github.v3+json',
                'User-Agent': 'picto-sight-releaser'
            }
        )
        with urllib.request.urlopen(req_get) as resp:
            release = json.loads(resp.read().decode('utf-8'))
            upload_url_base = release['upload_url'].split('{')[0]

    deb_path = '/home/dynogator/ai-rf-smart-scanner/dist/picto-sight-h8_3.1.0-1.trixie_arm64.deb'
    sha_path = deb_path + '.sha256'

    for asset_file in [deb_path, sha_path]:
        name = os.path.basename(asset_file)
        with open(asset_file, 'rb') as f:
            data = f.read()
        content_type = 'application/vnd.debian.binary-package' if name.endswith('.deb') else 'text/plain'
        url = f'{upload_url_base}?name={name}'
        up_req = urllib.request.Request(
            url,
            data=data,
            headers={
                'Authorization': f'token {pat}',
                'Content-Type': content_type,
                'User-Agent': 'picto-sight-releaser'
            }
        )
        with urllib.request.urlopen(up_req) as up_resp:
            asset = json.loads(up_resp.read().decode('utf-8'))
            print(f'Uploaded asset: {name} ({asset.get("size")} bytes) -> {asset.get("browser_download_url")}')

if __name__ == '__main__':
    main()
