# Picto-Sight H8 Mobile Remote Handset (Pixel 9 Pro XL / GrapheneOS)

![DynoGatorLabs Wasteland Cover](../docs/images/cover.jpg)

The **Picto-Sight H8 Handset Remote** (`llc.resgen.pictosight` v2.1.0) is a dedicated, native touch interface engineered for the **Google Pixel 9 Pro XL** (and modern Android / GrapheneOS devices) to interface with the Raspberry Pi 5 + Hailo-8 + HackRF One node.

---

## Capabilities & Architecture
- **Zero Cloud / Local-First**: Operates strictly peer-to-peer over local Wi-Fi, hotspot, or Tailscale WireGuard mesh (`10.67.191.90:8765`).
- **GrapheneOS Optimized**: Unrooted, no Google Play Services required, strictly sandboxed.
- **Bi-directional WebSocket Telemetry**: Real-time signal waterfall, 26 TOPS Hailo-8 thermal & load monitoring, and HackRF hardware serial validation.
- **Multi-Stage RF Sweep Controller**: Controls L1 Sweep $\rightarrow$ L2 Zoom $\rightarrow$ 1 MHz IQ+AI $\rightarrow$ Profile Lock directly from touch glass.
- **Instant RF Presets**: Instant push-to-tune for CB19 (27.185 MHz AM), Fire (154.280 MHz FM), Sheriff, SAR, Rail, and 2m Calling (+1V6, 146.520 MHz FM).
- **Tactical Airspace Radar**: Live ADS-B / SIM radar with 25/50 NM range rings and Colorado waypoints (CHEY, COS, PUB).
- **Gqrx Demod Audio Link**: Receives 48 kHz mono PCM demod audio streaming over UDP port `7355`.

---

## Quick Install (One-Click)

Connect your Pixel via USB-C or Wireless Debugging and run:

```bash
./install_handset.sh
```

Or manually sideload via ADB:

```bash
adb install -r --no-incremental apk/PictoSight-H8-2.1.0.apk
```

---

## Network Pairing & Port Tunnels
1. **Direct Wi-Fi**: Put the phone on the same local subnet as the Pi (e.g. `10.67.191.90`).
2. **Reverse ADB Tunnel** (USB / Wi-Fi ADB):
   ```bash
   adb reverse tcp:8765 tcp:8765
   adb reverse tcp:7356 tcp:7356
   ```
3. Tap **`LINK`** on the top header bar to establish the live telemetry session.

---

*Crafted in the subterranean bunkers of DynoGatorLabs.*
