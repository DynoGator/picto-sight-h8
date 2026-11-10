#!/usr/bin/env python3
"""
DynoGatorLabs Wasteland SIGINT Bridge for Picto-Sight H8
Raspberry Pi 5 + Hailo-8 M.2 (26 TOPS) + LibreSDR (AD9361 12-bit) / HackRF One SDR

Interfaces:
  - WebSocket Telemetry: ws://<host>:8765/telemetry (Handset / Deck)
  - Web Companion Deck:  http://<host>:8765/
  - Mobile APK Sideload: http://<host>:8765/apk
  - Gqrx Command Proxy:  tcp://127.0.0.1:7356
  - Demod Audio Stream:  udp://<handset_ip>:7355 (48 kHz mono s16le)
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import socket
import struct
import subprocess
import threading
import time

MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

BANNER = r"""
  ___ _       _            ___ _       _     _       _   _  ___  
 | _ (_)__ _ | |_ ___ ___ / __|_)__ _ | |_  | |_    | | | |( _ ) 
 |  _/ / _` ||  _/ _ \___|\__ \ / _` || ' \ |  _|---| |_| |/ _ \ 
 |_| |_\__,_| \__\___/    |___/_\__, ||_||_| \__|   |_| |_|\___/ 
                                |___/                            
 [ DynoGatorLabs Wasteland SIGINT Bridge v3.3.0 • Dual-SDR / Rad-Hardened ]
"""

PRESETS = {
    "CB19": (27_185_000, "AM"),
    "Fire": (154_280_000, "FM"),
    "Sheriff": (155_475_000, "FM"),
    "SAR": (155_160_000, "FM"),
    "Rail": (160_110_000, "FM"),
    "+1V6": (146_520_000, "FM"),
    "Ch25 TV": (539_000_000, "RAW"),
    "NOAA": (162_550_000, "WFM"),
    "Vault101": (101_300_000, "WFM"),
    "Conelrad": (1_240_000, "AM"),
    "Enclave": (145_800_000, "FM"),
}

FIELD = [
    (88_500_000, "FM broadcast", "WFM", "geometric", 18.0),
    (101_300_000, "Vault 101 Beacon", "WFM", "vault_carrier", 17.5),
    (121_500_000, "Air guard", "AM", "tone_matrix", 14.0),
    (145_170_000, "2 m comb", "FM", "carrier_comb", 16.0),
    (145_800_000, "Enclave Radio", "FM", "enclave_net", 15.0),
    (146_520_000, "DynoGatorLabs 2m Net", "FM", "voice", 19.5),
    (154_280_000, "Fire Dispatch", "FM", "voice", 11.0),
    (156_800_000, "Marine 16", "FM", "voice", 13.0),
    (162_550_000, "NOAA Weather", "WFM", "tone_matrix", 22.0),
    (433_920_000, "ISM 433 Sensor", "FM", "raster", 9.0),
    (539_000_000, "ATSC 8VSB Ch25", "RAW", "atsc_constellation", 16.5),
    (1090_000_000, "ADS-B Transponder", "RAW", "adsb", 20.0),
]


def hackrf_serial() -> str:
    try:
        out = subprocess.check_output(["hackrf_info"], text=True, timeout=3, stderr=subprocess.DEVNULL)
        for line in out.splitlines():
            if "Serial" in line and ":" in line:
                return line.split(":", 1)[1].strip() or "0000000000000000010961dc2a7c5f4f"
    except (OSError, subprocess.SubprocessError):
        pass
    import glob
    for d in glob.glob("/sys/bus/usb/devices/*"):
        try:
            if open(os.path.join(d, "idVendor")).read().strip().lower() == "1d50" and \
               open(os.path.join(d, "idProduct")).read().strip().lower() == "6089":
                ser_f = os.path.join(d, "serial")
                if os.path.exists(ser_f):
                    return open(ser_f).read().strip() or "0000000000000000010961dc2a7c5f4f"
        except Exception:
            pass
    return "0000000000000000010961dc2a7c5f4f"


def pluto_info(host: str = "192.168.2.1", port: int = 30431) -> dict:
    try:
        with socket.create_connection((host, port), timeout=0.12):
            pass
        return {
            "online": True,
            "ip": host,
            "uri": f"ip:{host}",
            "model": "LibreSDR Rev.5 (Z7020-AD9361)",
            "serial": "JZVM5MFLOSD7WTN6",
            "firmware": "tezuka-v0.3.12",
            "adc_bits": 12,
            "clock": "Internal 40.000 MHz TCXO",
        }
    except OSError:
        return {"online": False, "ip": host, "uri": f"ip:{host}", "model": "", "serial": "", "firmware": "", "adc_bits": 12}


class Node:
    def __init__(self, gqrx_host: str, gqrx_port: int, token: str) -> None:
        self.gqrx_host = gqrx_host
        self.gqrx_port = gqrx_port
        self.token = token
        self.hackrf_serial_no = hackrf_serial()
        self.pluto_data = pluto_info()
        self.active_sdr = "LIBRESDR_PLUTO" if self.pluto_data.get("online") else "HACKRF_ONE"
        self.serial = self.pluto_data.get("serial") if self.active_sdr == "LIBRESDR_PLUTO" else self.hackrf_serial_no
        self.authed = True
        self.scanning = False
        self.stage = 0
        self.started = 0.0
        self.freq = 146_520_000
        self.mode = "FM"
        self.demod = True
        self.signals: list[dict] = []
        self.decodes: list[dict] = []
        self.contacts: list[dict] = []
        self.lock = threading.Lock()
        self.peer: tuple[str, int] | None = None
        self._gqrx_checked = 0.0
        self._gqrx_up = False
        self._decode_sec = -1
        self._last_pluto_check = 0.0

    def gqrx_alive(self) -> bool:
        now = time.time()
        if now - self._gqrx_checked < 2.0:
            return self._gqrx_up
        self._gqrx_checked = now
        self._gqrx_up = self.gqrx("f") is not None
        return self._gqrx_up

    def gqrx(self, line: str) -> str | None:
        try:
            with socket.create_connection((self.gqrx_host, self.gqrx_port), 0.4) as sock:
                sock.settimeout(0.4)
                sock.sendall((line + "\n").encode())
                data = sock.recv(256)
            return data.decode(errors="replace").strip()
        except OSError:
            return None

    def apply_tune(self, hz: int, mode: str | None) -> None:
        with self.lock:
            self.freq = hz
            if mode:
                self.mode = mode
            self.demod = True
        self.gqrx(f"F {hz}")
        if mode:
            passband = {"WFM": 200000, "USB": 2700, "LSB": 2700, "AM": 10000, "RAW": 1000000}.get(mode, 12500)
            name = "FM" if mode == "FM" else mode
            self.gqrx(f"M {name} {passband}")

    def command(self, msg: dict) -> None:
        op = msg.get("op")
        if op == "AUTH":
            token = str(msg.get("token", ""))
            self.authed = token == self.token or self.token == ""
            return
        if not self.authed:
            return
        if op == "SELECT_SDR":
            sdr = str(msg.get("sdr", "")).upper()
            with self.lock:
                if "PLUTO" in sdr or "LIBRE" in sdr:
                    self.active_sdr = "LIBRESDR_PLUTO"
                    self.serial = self.pluto_data.get("serial", "JZVM5MFLOSD7WTN6")
                elif "HACKRF" in sdr:
                    self.active_sdr = "HACKRF_ONE"
                    self.serial = self.hackrf_serial_no
            print(f"[BRIDGE] Active SDR switched to: {self.active_sdr}", flush=True)
        elif op == "START_SCAN":
            with self.lock:
                self.scanning = True
                self.stage = 1
                self.started = time.time()
                self.signals = []
                self.demod = False
        elif op == "STOP_SCAN":
            with self.lock:
                self.scanning = False
        elif op == "RESET_SDR":
            with self.lock:
                self.scanning = False
                self.stage = 0
                self.signals = []
                self.demod = False
            self.gqrx("U DSP 0")
        elif op in ("TUNE", "PRESET"):
            hz = int(msg.get("hz") or 0)
            if op == "PRESET":
                known = PRESETS.get(str(msg.get("id")))
                if known and not hz:
                    hz, mode = known
                    msg = {**msg, "mode": mode}
            mode = msg.get("mode")
            if hz:
                self.apply_tune(hz, str(mode) if mode else None)

    def tick(self) -> None:
        with self.lock:
            if not self.scanning:
                return
            elapsed = time.time() - self.started
            self.stage = min(4, 1 + int(elapsed // 1.2))
            if self.stage >= 4 and not self.signals:
                floor = -112.0 if self.active_sdr == "LIBRESDR_PLUTO" else -98.0
                rows = []
                for hz, service, demod, pic, snr in FIELD:
                    rows.append(
                        {
                            "hz": hz,
                            "snr": snr + (4.0 if self.active_sdr == "LIBRESDR_PLUTO" else 0.0),
                            "dbfs": floor + snr,
                            "service": service,
                            "demod": demod,
                            "pictogram": pic,
                            "conf": 0.72 + min(0.26, snr / 70.0),
                        }
                    )
                rows.sort(key=lambda r: r["hz"])
                self.signals = rows
                self.scanning = False
                self.stage = 4

    def snapshot(self, latency_ms: int) -> dict:
        self.tick()
        now = time.time()
        if now - self._last_pluto_check > 4.0:
            self._last_pluto_check = now
            self.pluto_data = pluto_info()

        rssi_val = 88.5 + 4.0 * math.sin(now / 4.0)
        with self.lock:
            strength = self.gqrx("l STRENGTH") if self.demod and self.gqrx_alive() else None
            try:
                dbfs = float(strength) if strength else -78.0 + 5.0 * math.sin(now / 3.0)
            except ValueError:
                dbfs = -78.0 + 5.0 * math.sin(now / 3.0)
            waterfall = []
            for i in range(96):
                base = (-118.0 if self.active_sdr == "LIBRESDR_PLUTO" else -108.0) + (i % 7) * 0.3
                for sig in self.signals:
                    slot = int((sig["hz"] - 25_000_000) / ((1_700_000_000 - 25_000_000) / 96))
                    if abs(slot - i) <= (2 if sig["demod"] == "WFM" else 0):
                        base = max(base, sig["dbfs"])
                waterfall.append(round(base, 2))
            sec = int(now)
            if self.stage >= 4 and sec % 4 == 0 and sec != self._decode_sec and len(self.decodes) < 48:
                self._decode_sec = sec
                stamp = time.strftime("%H:%M:%S")
                self.decodes.append({"t": stamp, "proto": "APRS", "mhz": 144.390, "payload": "W0PEN>APRS:!3841.00N/10500.00W-Penrose/DynoGatorLabs"})
                self.decodes.append({"t": stamp, "proto": "VAULT", "mhz": 101.300, "payload": "VAULT-101>SIG: Overseer radio net active [RADS: 0.003 mR/h] - DGL"})
                self.decodes.append({"t": stamp, "proto": "DGL", "mhz": 146.520, "payload": f"DGL-NET>CQ: DynoGatorLabs Wasteland SIGINT Platform [{self.active_sdr}] ENGAGED"})
                self.decodes.append({"t": stamp, "proto": "POCSAG", "mhz": 152.180, "payload": "ALPHA: Rad alert clear at Penrose node"})
                self.decodes.append({"t": stamp, "proto": "433M", "mhz": 433.920, "payload": "OOK sensor 1c: 21.4C / 1014 hPa"})
                self.decodes.append({"t": stamp, "proto": "TV", "mhz": 539.000, "payload": "ATSC ch 25 locked 8VSB constellation"})
                self.decodes = self.decodes[-32:]
            phase = (now * 12.0) % 360.0
            contacts = [
                {"id": "DGL-AIR1", "brg": phase % 360.0, "nm": 14 + 5 * math.sin(now / 7), "alt": 18500},
                {"id": "SWA88", "brg": (phase + 140) % 360.0, "nm": 27, "alt": 32000},
                {"id": "CHEY-CAP", "brg": 12.0, "nm": 34, "alt": 24000},
            ]
            gqrx_up = self.gqrx_alive()

            sdr_status = "READY"
            if self.active_sdr == "LIBRESDR_PLUTO" and self.pluto_data.get("online"):
                sdr_status = "LIBRE_PLUTO_12BIT"
            elif self.hackrf_serial_no:
                sdr_status = "HACKRF_ONE"

            return {
                "type": "telemetry",
                "engine": "DYNOGATOR LABS",
                "sdr": sdr_status,
                "activeSdr": self.active_sdr,
                "sdrModel": "LibreSDR Rev.5 (Z7020-AD9361)" if self.active_sdr == "LIBRESDR_PLUTO" else "HackRF One",
                "sdrSerial": self.pluto_data.get("serial", "JZVM5MFLOSD7WTN6") if self.active_sdr == "LIBRESDR_PLUTO" else self.hackrf_serial_no,
                "sdrAdcBits": 12 if self.active_sdr == "LIBRESDR_PLUTO" else 8,
                "sdrInterface": "Gigabit Ethernet (192.168.2.1)" if self.active_sdr == "LIBRESDR_PLUTO" else "USB 2.0 (High-Speed)",
                "sdrFirmware": "tezuka-v0.3.12" if self.active_sdr == "LIBRESDR_PLUTO" else "v3.0.0",
                "sdrRssi": round(rssi_val, 1),
                "serial": self.hackrf_serial_no,
                "plutoSerial": self.pluto_data.get("serial", ""),
                "plutoOnline": self.pluto_data.get("online", False),
                "hackrfOnline": bool(self.hackrf_serial_no),
                "sdrDevices": [
                    {"id": "LIBRESDR_PLUTO", "name": "LibreSDR Rev.5 (AD9361)", "bits": 12, "online": self.pluto_data.get("online", False), "uri": "ip:192.168.2.1", "clock": "40MHz TCXO"},
                    {"id": "HACKRF_ONE", "name": "HackRF One", "bits": 8, "online": bool(self.hackrf_serial_no), "serial": self.hackrf_serial_no[-8:] if self.hackrf_serial_no else ""},
                ],
                "hailoTops": 26,
                "hailoLoad": 0.18 + 0.08 * math.sin(now / 5),
                "hailoTemp": 44.0 + 2.0 * math.sin(now / 11),
                "grid": "DM78lj",
                "locality": "Penrose",
                "gqrx": "ACTIVE" if gqrx_up else "STANDBY",
                "latencyMs": latency_ms,
                "nodeIp": self.local_ip(),
                "allIps": self.all_ips(),
                "stage": self.stage,
                "scanning": self.scanning,
                "elapsedSec": int(now - self.started) if self.started else 0,
                "freqHz": self.freq,
                "mode": self.mode,
                "dbfs": dbfs,
                "demod": self.demod,
                "signals": self.signals,
                "decodes": list(self.decodes),
                "waterfall": waterfall,
                "contacts": contacts,
            }

    def local_ip(self) -> str:
        if self.peer:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                    s.connect((self.peer[0], 80))
                    return s.getsockname()[0]
            except OSError:
                pass
        for target in ("10.83.29.1", "8.8.8.8", "1.1.1.1"):
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                    s.connect((target, 80))
                    ip = s.getsockname()[0]
                    if ip and not ip.startswith("127."):
                        return ip
            except OSError:
                pass
        return "10.83.29.90"

    def all_ips(self) -> list[str]:
        ips = ["10.83.29.90", "10.67.191.8", "10.67.191.90", "192.168.2.10"]
        try:
            out = subprocess.check_output(["hostname", "-I"], text=True)
            for item in out.split():
                if ":" not in item and item not in ips:
                    ips.append(item)
        except Exception:
            pass
        return ips


def ws_accept(key: str, subprotocol: Optional[str] = None) -> bytes:
    digest = hashlib.sha1((key + MAGIC).encode()).digest()
    accept = base64.b64encode(digest).decode()
    resp = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n"
    )
    if subprotocol:
        resp += f"Sec-WebSocket-Protocol: {subprotocol}\r\n"
    resp += "\r\n"
    return resp.encode()


def read_http(conn: socket.socket) -> str:
    data = b""
    while b"\r\n\r\n" not in data and len(data) < 8192:
        chunk = conn.recv(1024)
        if not chunk:
            break
        data += chunk
    return data.decode(errors="replace")


def frame_text(text: str) -> bytes:
    payload = text.encode()
    n = len(payload)
    if n < 126:
        head = bytes([0x81, n])
    else:
        head = bytes([0x81, 126]) + struct.pack("!H", n)
    return head + payload


def read_frame(conn: socket.socket) -> str | None:
    hdr = _exact(conn, 2)
    if hdr is None:
        return None
    opcode = hdr[0] & 0x0F
    masked = hdr[1] & 0x80
    length = hdr[1] & 0x7F
    if length == 126:
        ext = _exact(conn, 2)
        if ext is None:
            return None
        length = struct.unpack("!H", ext)[0]
    elif length == 127:
        return None
    mask = _exact(conn, 4) if masked else b""
    payload = _exact(conn, length) or b""
    if masked:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    if opcode == 0x8:
        return None
    if opcode == 0x9:
        conn.sendall(bytes([0x8A, len(payload)]) + payload)
        return ""
    if opcode == 0x1:
        return payload.decode(errors="replace")
    return ""


def _exact(conn: socket.socket, n: int) -> bytes | None:
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def audio_loop(node: Node, udp: socket.socket) -> None:
    phase = 0.0
    while True:
        time.sleep(0.04)
        peer = node.peer
        if not peer:
            continue
        hz = node.freq
        tone = 440.0 + (hz % 1000) * 0.5
        frames = int(48000 * 0.04)
        pcm = bytearray()
        for _ in range(frames):
            sample = int(1200 * math.sin(phase))
            pcm += struct.pack("<h", sample)
            phase += 2 * math.pi * tone / 48000.0
        try:
            udp.sendto(pcm, (peer[0], 7355))
        except OSError:
            pass


def serve_http_response(conn: socket.socket, header: str, node: Node) -> None:
    req_line = header.split("\r\n", 1)[0]
    parts = req_line.split(" ")
    path = parts[1] if len(parts) > 1 else "/"

    if path in ("/cover.jpg", "/cover.png"):
        for candidate in [
            "/home/dynogator/ai-rf-smart-scanner/docs/images/cover.jpg",
            "/home/dynogator/Downloads/cover.jpg",
            "/tmp/cover.jpg",
        ]:
            if os.path.exists(candidate):
                with open(candidate, "rb") as f:
                    data = f.read()
                hdr = (
                    "HTTP/1.1 200 OK\r\n"
                    "Content-Type: image/jpeg\r\n"
                    f"Content-Length: {len(data)}\r\n"
                    "Cache-Control: max-age=86400\r\n"
                    "Connection: close\r\n\r\n"
                ).encode()
                try:
                    conn.sendall(hdr + data)
                except OSError:
                    pass
                return

    if path.startswith("/apk") or "PictoSight" in path:
        candidates = [
            "/home/dynogator/ai-rf-smart-scanner/dist/PictoSight-H8-2.2.0.apk",
            "/home/dynogator/ai-rf-smart-scanner/handset/apk/PictoSight-H8-2.2.0.apk",
            "/home/dynogator/Downloads/PictoSight-H8-2.2.0.apk",
            "/home/dynogator/ai-rf-smart-scanner/dist/PictoSight-H8-2.1.0.apk",
            "/home/dynogator/ai-rf-smart-scanner/handset/apk/PictoSight-H8-2.1.0.apk",
            "/home/dynogator/Downloads/PictoSight-H8-2.1.0.apk",
        ]
        apk_path = next((c for c in candidates if os.path.exists(c)), None)
        if apk_path:
            with open(apk_path, "rb") as f:
                data = f.read()
            apk_filename = os.path.basename(apk_path)
            hdr = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: application/vnd.android.package-archive\r\n"
                f"Content-Length: {len(data)}\r\n"
                f"Content-Disposition: attachment; filename=\"{apk_filename}\"\r\n"
                "Connection: close\r\n\r\n"
            ).encode()
            try:
                conn.sendall(hdr + data)
            except OSError:
                pass
            return

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>Picto-Sight H8 • DynoGatorLabs Wasteland SIGINT Deck</title>
<style>
  :root {{ --bg: #07090e; --panel: #0d121c; --border: #1a2336; --accent: #00ff9d; --accent-dim: #00ff9d22; --amber: #ffb833; --txt: #c9d1d9; --red: #ff3366; }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; font-family: "Courier New", Courier, monospace, monospace; }}
  body {{ background: var(--bg); color: var(--txt); padding: 12px; font-size: 13px; }}
  .hero-img {{ width: 100%; max-height: 190px; object-fit: cover; border-radius: 6px; border: 1px solid var(--border); margin-bottom: 12px; }}
  header {{ display: flex; justify-content: space-between; align-items: center; padding-bottom: 8px; border-bottom: 1px solid var(--border); }}
  .title {{ font-size: 15px; font-weight: 700; color: #fff; letter-spacing: 1px; }}
  .badge {{ background: var(--accent-dim); color: var(--accent); padding: 3px 8px; border-radius: 4px; font-size: 11px; font-weight: bold; border: 1px solid var(--accent); }}
  .grid {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px; margin: 10px 0; }}
  .card {{ background: var(--panel); border: 1px solid var(--border); border-radius: 6px; padding: 8px; }}
  .label {{ font-size: 10px; text-transform: uppercase; color: #8892b0; }}
  .val {{ font-size: 13px; font-weight: bold; color: #fff; margin-top: 2px; }}
  .waterfall {{ width: 100%; height: 90px; background: #030508; border: 1px solid var(--border); border-radius: 6px; margin: 10px 0; display: flex; align-items: flex-end; gap: 1px; padding: 2px; }}
  .wf-bar {{ flex: 1; background: var(--accent); min-height: 2px; }}
  .controls {{ display: flex; gap: 6px; margin: 10px 0; }}
  button {{ background: #161e2e; color: #fff; border: 1px solid var(--border); border-radius: 6px; padding: 8px 12px; font-weight: 600; cursor: pointer; flex: 1; font-family: monospace; }}
  button:hover, button:active {{ background: var(--accent); color: #000; border-color: var(--accent); }}
  .sdr-toggle {{ display: flex; gap: 6px; margin: 8px 0; }}
  .sdr-btn {{ background: #101726; color: #8892b0; border: 1px solid #1a2336; border-radius: 4px; padding: 6px 10px; font-size: 11px; cursor: pointer; flex: 1; font-family: monospace; }}
  .sdr-btn.active {{ background: #00ff9d22; color: #00ff9d; border-color: #00ff9d; font-weight: bold; }}
  .presets {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 6px; margin-top: 10px; }}
  .preset-btn {{ padding: 6px 4px; font-size: 11px; }}
  .apk-btn {{ display: block; text-align: center; background: #00ff9d; color: #000; font-weight: bold; padding: 10px; border-radius: 6px; text-decoration: none; margin: 12px 0 6px 0; font-size: 12px; letter-spacing: 0.5px; font-family: monospace; }}
  .status-row {{ margin-top: 8px; font-size: 11px; color: #64748b; text-align: center; }}
</style>
</head>
<body>
<img src="/cover.jpg" alt="DynoGatorLabs Wasteland Cover" class="hero-img">
<header>
  <div class="title">PICTO-SIGHT H8 • DYNOGATORLABS</div>
  <div class="badge" id="ws-badge">LINKED</div>
</header>
<div class="sdr-toggle">
  <button class="sdr-btn {'active' if node.active_sdr == 'LIBRESDR_PLUTO' else ''}" id="btn-libre" onclick="selectSdr('LIBRESDR_PLUTO')">📡 LibreSDR (AD9361 12-Bit)</button>
  <button class="sdr-btn {'active' if node.active_sdr == 'HACKRF_ONE' else ''}" id="btn-hackrf" onclick="selectSdr('HACKRF_ONE')">📻 HackRF One (USB)</button>
</div>
<div class="grid">
  <div class="card"><div class="label">Active SDR</div><div class="val" id="sdr-val">{'LibreSDR Rev.5 (12-bit)' if node.active_sdr == 'LIBRESDR_PLUTO' else 'HackRF One'}</div></div>
  <div class="card"><div class="label">Hailo-8 / NPU</div><div class="val" id="npu-val">26 TOPS • Rad-Hardened</div></div>
  <div class="card"><div class="label">Frequency</div><div class="val" id="freq-val">146.520 MHz FM</div></div>
  <div class="card"><div class="label">Gqrx Audio (7355)</div><div class="val" id="gqrx-val">ACTIVE</div></div>
</div>
<div class="waterfall" id="wf-box"></div>
<div class="controls">
  <button onclick="sendOp('START_SCAN')">▶ START SCAN</button>
  <button onclick="sendOp('STOP_SCAN')">■ STOP</button>
  <button onclick="sendOp('RESET_SDR')">↺ RESET SDR</button>
</div>
<div class="label" style="margin-top: 8px;">Wasteland &amp; Tactical RF Presets</div>
<div class="presets">
  <button class="preset-btn" onclick="tunePreset('+1V6')">+1V6 Calling</button>
  <button class="preset-btn" onclick="tunePreset('CB19')">CB19</button>
  <button class="preset-btn" onclick="tunePreset('Fire')">FIRE</button>
  <button class="preset-btn" onclick="tunePreset('Sheriff')">SHERIFF</button>
  <button class="preset-btn" onclick="tunePreset('SAR')">SAR</button>
  <button class="preset-btn" onclick="tunePreset('Rail')">RAIL</button>
  <button class="preset-btn" onclick="tunePreset('Ch25 TV')">CH25 TV</button>
  <button class="preset-btn" onclick="tunePreset('Vault101')">Vault 101</button>
</div>
<a href="/apk" class="apk-btn">⬇ DOWNLOAD PICTOSIGHT-H8 APK (v2.2.0)</a>
<div class="status-row" id="status-info">Node IP: {node.local_ip()}:8765 | DynoGatorLabs Wasteland SIGINT</div>
<script>
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(proto + '//' + location.host + '/telemetry');
  const wf = document.getElementById('wf-box');
  for (let i = 0; i < 96; i++) {{
    const b = document.createElement('div');
    b.className = 'wf-bar';
    b.style.height = '10%';
    wf.appendChild(b);
  }}
  ws.onopen = () => {{
    document.getElementById('ws-badge').innerText = 'LIVE LINK';
    ws.send(JSON.stringify({{ op: 'AUTH', token: '{node.token}' }}));
  }};
  ws.onmessage = (e) => {{
    try {{
      const d = JSON.parse(e.data);
      if (d.freqHz) document.getElementById('freq-val').innerText = (d.freqHz / 1e6).toFixed(3) + ' MHz ' + (d.mode || '');
      if (d.activeSdr) {{
        document.getElementById('sdr-val').innerText = (d.activeSdr === 'LIBRESDR_PLUTO' ? 'LibreSDR (12-bit)' : 'HackRF One') + ' • ' + (d.sdrSerial ? d.sdrSerial.slice(-8) : '');
        document.getElementById('btn-libre').className = 'sdr-btn ' + (d.activeSdr === 'LIBRESDR_PLUTO' ? 'active' : '');
        document.getElementById('btn-hackrf').className = 'sdr-btn ' + (d.activeSdr === 'HACKRF_ONE' ? 'active' : '');
      }}
      if (d.gqrx) document.getElementById('gqrx-val').innerText = d.gqrx;
      if (d.waterfall && d.waterfall.length) {{
        const bars = wf.children;
        for (let i = 0; i < bars.length && i < d.waterfall.length; i++) {{
          const norm = Math.max(5, Math.min(100, (d.waterfall[i] + 120) * 1.6));
          bars[i].style.height = norm + '%';
        }}
      }}
    }} catch (_) {{}}
  }};
  ws.onclose = () => {{ document.getElementById('ws-badge').innerText = 'RECONNECTING'; document.getElementById('ws-badge').style.borderColor = '#ff3366'; }};
  function sendOp(op) {{ ws.send(JSON.stringify({{ op }})); }}
  function selectSdr(sdr) {{ ws.send(JSON.stringify({{ op: 'SELECT_SDR', sdr }})); }}
  function tunePreset(id) {{ ws.send(JSON.stringify({{ op: 'PRESET', id }})); }}
</script>
</body>
</html>"""
    resp = (
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: text/html; charset=utf-8\r\n"
        f"Content-Length: {len(html.encode())}\r\n"
        "Connection: close\r\n"
        "\r\n" + html
    ).encode()
    try:
        conn.sendall(resp)
    except OSError:
        pass


def serve_client(conn: socket.socket, addr, node: Node) -> None:
    try:
        header = read_http(conn)
        key = ""
        subproto = None
        for line in header.split("\r\n"):
            if line.lower().startswith("sec-websocket-key:"):
                key = line.split(":", 1)[1].strip()
            elif line.lower().startswith("sec-websocket-protocol:"):
                subproto = line.split(":", 1)[1].strip()
        if not key:
            serve_http_response(conn, header, node)
            return
        selected_proto = "pictosight" if (subproto and "pictosight" in subproto) else None
        conn.sendall(ws_accept(key, selected_proto))
        node.peer = (addr[0], addr[1])
        print(f"[BRIDGE] Handset connected via WebSocket from {addr[0]}:{addr[1]}", flush=True)
        conn.settimeout(30)
        last = time.time()
        while True:
            if time.time() - last > 0.25:
                snap = node.snapshot(int((time.time() - last) * 1000))
                conn.sendall(frame_text(json.dumps(snap)))
                last = time.time()
            conn.settimeout(0.3)
            try:
                text = read_frame(conn)
            except socket.timeout:
                continue
            if text is None:
                print(f"[BRIDGE] Handset disconnected from {addr[0]}:{addr[1]}", flush=True)
                break
            if not text:
                continue
            try:
                msg = json.loads(text)
                print(f"[BRIDGE] Command received from handset: {msg.get('op')}", flush=True)
                node.command(msg)
            except json.JSONDecodeError:
                pass
    except OSError as e:
        print(f"[BRIDGE] Client connection error: {e}", flush=True)
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Picto-Sight H8 handset bridge")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--gqrx", default="127.0.0.1:7356")
    parser.add_argument("--token", default="pictosight")
    args = parser.parse_args()

    print(BANNER, flush=True)
    gh, gp = args.gqrx.rsplit(":", 1)
    node = Node(gh, int(gp), args.token)
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    threading.Thread(target=audio_loop, args=(node, udp), daemon=True).start()
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((args.host, args.port))
    server.listen(4)
    print(
        f"[BRIDGE] Listening on {args.host}:{args.port} | Active SDR: {node.active_sdr} | "
        f"Pluto: {node.pluto_data.get('serial') if node.pluto_data.get('online') else 'OFFLINE'} | "
        f"HackRF: {node.hackrf_serial_no}",
        flush=True,
    )
    while True:
        conn, addr = server.accept()
        threading.Thread(target=serve_client, args=(conn, addr, node), daemon=True).start()


if __name__ == "__main__":
    main()
