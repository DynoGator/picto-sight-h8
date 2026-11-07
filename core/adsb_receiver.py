#!/usr/bin/env python3
"""
core/adsb_receiver.py - Live 1090 MHz ADS-B Mode-S Receiver & readsb Bridge
===========================================================================
Streams 2.400 MSPS 1090 MHz IQ from HackRF One, performs real-time I/Q DC-offset
cancellation and signed-int8 -> unsigned-UC8 conversion, pipes into readsb, and
parses decoded Mode-S / ADS-B DF17 aircraft positions, altitudes, callsigns,
groundspeeds, headings, vertical rates, and squawks relative to Home Base.
Arbitrated cleanly via SDRResourceArbiter ("ADSB_LIVE").
"""

from __future__ import annotations

import json
import logging
import math
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from core.sdr_arbiter import get_sdr_arbiter

logger = logging.getLogger("smart_scanner.adsb")

ADSB_JSON_DIR = "/tmp/picto_adsb"
EARTH_RADIUS_NM = 3440.065


def _haversine_nm_brg(lat1: float, lon1: float, lat2: float, lon2: float) -> Tuple[float, float]:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi * 0.5) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam * 0.5) ** 2
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))
    dist = EARTH_RADIUS_NM * c
    y = math.sin(dlam) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlam)
    brg = (math.degrees(math.atan2(y, x)) + 360.0) % 360.0
    return dist, brg


def convert_hackrf_int8_to_uc8(raw_int8_bytes: bytes, remove_dc: bool = True) -> bytes:
    """
    Converts HackRF signed int8 interleaved I/Q bytes (-128..127) to unsigned 8-bit
    UC8 bytes (0..255, zero=128) expected by readsb --iformat=UC8, optionally removing
    per-block ADC I/Q DC offset so weak Mode-S preambles aren't masked by LO leakage.
    """
    if not raw_int8_bytes:
        return b""
    arr = np.frombuffer(raw_int8_bytes, dtype=np.int8)
    if remove_dc and len(arr) >= 64:
        n2 = (len(arr) // 2) * 2
        iq_i16 = arr[:n2].astype(np.int16)
        dc_i = int(np.round(np.mean(iq_i16[0::2])))
        dc_q = int(np.round(np.mean(iq_i16[1::2])))
        if dc_i != 0 or dc_q != 0:
            iq_i16[0::2] -= dc_i
            iq_i16[1::2] -= dc_q
            uc8 = np.clip(iq_i16 + 128, 0, 255).astype(np.uint8)
            return uc8.tobytes()
    return (arr.view(np.uint8) ^ 0x80).tobytes()


def parse_readsb_aircraft_json(
    json_dir: str = ADSB_JSON_DIR,
    home_lat: float = 38.4112,
    home_lon: float = -105.0366,
) -> Tuple[List[Dict[str, Any]], int]:
    """
    Reads readsb's aircraft.json and returns (parsed_aircraft_list, total_messages).
    Includes both full ADS-B lat/lon targets and Mode-S altitude-only targets.
    """
    ac_file = os.path.join(json_dir, "aircraft.json")
    if not os.path.exists(ac_file):
        return [], 0

    try:
        with open(ac_file, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception:
        return [], 0

    total_msgs = int(payload.get("messages", 0))
    now_ts = float(payload.get("now", time.time()))
    raw_list = payload.get("aircraft", [])
    parsed: List[Dict[str, Any]] = []

    for item in raw_list:
        seen = float(item.get("seen", 999.0))
        if seen > 65.0:
            continue
        icao = str(item.get("hex", "")).strip().upper()
        if not icao:
            continue
        call = str(item.get("flight", "")).strip() or f"HEX-{icao}"
        alt_raw = item.get("alt_baro", item.get("alt_geom", 0))
        alt_ft = 0 if alt_raw == "ground" else int(alt_raw or 0)
        gs_kt = int(round(float(item.get("gs", 0.0) or 0.0)))
        hdg = float(item.get("track", item.get("mag_heading", 0.0)) or 0.0)
        vr_fpm = int(item.get("baro_rate", item.get("geom_rate", 0)) or 0)
        squawk = str(item.get("squawk", "----"))
        ac_type = str(item.get("category", item.get("t", "ADSB")))

        has_pos = ("lat" in item) and ("lon" in item)
        if has_pos:
            lat = float(item["lat"])
            lon = float(item["lon"])
            dist_nm, brg_deg = _haversine_nm_brg(home_lat, home_lon, lat, lon)
        else:
            lat = home_lat
            lon = home_lon
            dist_nm = 0.0
            brg_deg = 0.0

        parsed.append(
            {
                "icao": icao,
                "callsign": call,
                "has_position": has_pos,
                "latitude": lat,
                "longitude": lon,
                "altitude_ft": alt_ft,
                "ground_speed_kt": gs_kt,
                "heading_deg": hdg,
                "vertical_rate_fpm": vr_fpm,
                "squawk": squawk,
                "aircraft_type": ac_type,
                "distance_nm": round(dist_nm, 1),
                "bearing_deg": round(brg_deg, 1),
                "rssi_dbfs": float(item.get("rssi", -49.5)),
                "messages": int(item.get("messages", 1)),
                "seen_s": round(seen, 1),
                "updated_ts": now_ts,
            }
        )

    parsed.sort(key=lambda a: (not a["has_position"], a["distance_nm"]))
    return parsed, total_msgs


def decode_uc8_buffer_with_readsb(
    uc8_bytes: bytes,
    home_lat: float = 38.4112,
    home_lon: float = -105.0366,
) -> Tuple[List[Dict[str, Any]], int]:
    """
    Runs readsb over an in-memory 2.4 MSPS UC8 IQ buffer (padded to >= 6 MB so readsb's
    internal 256 KB block reader flushes all frames cleanly) and returns decoded aircraft.
    """
    readsb_bin = shutil.which("readsb")
    if not readsb_bin:
        return [], 0

    min_len = 6_400_000  # ~1.33s at 2.4 MSPS (2 bytes/sample)
    if len(uc8_bytes) < min_len:
        pad = bytes([128]) * (min_len - len(uc8_bytes))
        payload = uc8_bytes + pad
    else:
        payload = uc8_bytes

    with tempfile.TemporaryDirectory(prefix="picto_adsb_test_") as tmpdir:
        iq_file = os.path.join(tmpdir, "capture.uc8")
        with open(iq_file, "wb") as f:
            f.write(payload)

        cmd = [
            readsb_bin,
            "--device-type=ifile",
            f"--ifile={iq_file}",
            "--iformat=UC8",
            f"--lat={home_lat:.5f}",
            f"--lon={home_lon:.5f}",
            f"--write-json={tmpdir}",
            "--write-json-every=1",
            "--quiet",
        ]
        subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=6.0)
        return parse_readsb_aircraft_json(tmpdir, home_lat=home_lat, home_lon=home_lon)


class LiveADSBReceiver:
    """
    Continuous background 1090 MHz ADS-B receiver streaming HackRF One -> readsb
    with strict 1-at-a-time SDRResourceArbiter coordination.
    """

    def __init__(self, json_dir: str = ADSB_JSON_DIR) -> None:
        self.json_dir = json_dir
        self._lock = threading.RLock()
        self._running = False
        self._hrf_proc: Optional[subprocess.Popen] = None
        self._readsb_proc: Optional[subprocess.Popen] = None
        self._bridge_thread: Optional[threading.Thread] = None
        self._home_lat = 38.4112
        self._home_lon = -105.0366
        self._chunks_piped = 0
        self.arbiter = get_sdr_arbiter()
        self.arbiter.register_aux_stopper("ADSB_LIVE", self.stop)

    def is_running(self) -> bool:
        with self._lock:
            return (
                self._running
                and self._hrf_proc is not None
                and self._hrf_proc.poll() is None
                and self._readsb_proc is not None
                and self._readsb_proc.poll() is None
            )

    def start(
        self,
        home_lat: float = 38.4112,
        home_lon: float = -105.0366,
        lna_gain: int = 40,
        vga_gain: int = 48,
        amp_enable: int = 1,
    ) -> Tuple[bool, str]:
        self.stop()

        if not shutil.which("hackrf_transfer") or not shutil.which("readsb"):
            return False, "hackrf_transfer or readsb binary not installed."

        ok_sdr, sdr_msg = self.arbiter.prepare_for_aux_receiver(
            "ADSB_LIVE",
            "Live 1090 MHz ADS-B Mode-S Radar (readsb)",
        )
        if not ok_sdr:
            return False, sdr_msg

        os.makedirs(self.json_dir, exist_ok=True)
        for old_f in Path(self.json_dir).glob("*.json"):
            try:
                old_f.unlink()
            except Exception:
                pass

        readsb_cmd = [
            "readsb",
            "--device-type=ifile",
            "--ifile=-",
            "--iformat=UC8",
            f"--lat={home_lat:.5f}",
            f"--lon={home_lon:.5f}",
            f"--write-json={self.json_dir}",
            "--write-json-every=1",
            "--quiet",
        ]
        hrf_cmd = [
            "hackrf_transfer",
            "-r", "-",
            "-f", "1090000000",
            "-s", "2400000",
            "-b", "2500000",
            "-l", str(int(lna_gain)),
            "-g", str(int(vga_gain)),
            "-a", str(1 if amp_enable else 0),
        ]
        ser = self.arbiter.get_hackrf_serial()
        if ser:
            hrf_cmd.extend(["-d", ser])

        try:
            with self._lock:
                self._home_lat = home_lat
                self._home_lon = home_lon
                self._chunks_piped = 0
                self._readsb_proc = subprocess.Popen(
                    readsb_cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                self._hrf_proc = subprocess.Popen(
                    hrf_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                self._running = True
                self._bridge_thread = threading.Thread(
                    target=self._iq_bridge_loop,
                    daemon=True,
                    name="ADSB_IQ_Bridge",
                )
                self._bridge_thread.start()
            return True, "Live 1090 MHz ADS-B Receiver active (2.4 MSPS HackRF -> readsb)."
        except Exception as exc:
            self.stop()
            return False, f"Failed to start Live ADS-B Receiver: {exc}"

    def _iq_bridge_loop(self) -> None:
        chunk_bytes = 262_144
        while True:
            with self._lock:
                if not self._running or self._hrf_proc is None or self._readsb_proc is None:
                    break
                h_out = self._hrf_proc.stdout
                r_in = self._readsb_proc.stdin
            if h_out is None or r_in is None:
                break
            try:
                raw = h_out.read(chunk_bytes)
                if not raw:
                    break
                uc8 = convert_hackrf_int8_to_uc8(raw, remove_dc=True)
                r_in.write(uc8)
                r_in.flush()
                with self._lock:
                    self._chunks_piped += 1
            except Exception:
                break

    def poll_targets(self) -> Tuple[List[Dict[str, Any]], int, bool]:
        """Returns (aircraft_dicts, total_mode_s_messages, is_running)."""
        running = self.is_running()
        targets, msgs = parse_readsb_aircraft_json(
            self.json_dir,
            home_lat=self._home_lat,
            home_lon=self._home_lon,
        )
        return targets, msgs, running

    def stop(self) -> None:
        with self._lock:
            self._running = False
            hrf = self._hrf_proc
            rsb = self._readsb_proc
            self._hrf_proc = None
            self._readsb_proc = None

        for proc in (hrf, rsb):
            if proc is not None:
                try:
                    if proc.stdin:
                        proc.stdin.close()
                except Exception:
                    pass
                if proc.poll() is None:
                    try:
                        proc.send_signal(signal.SIGINT)
                        proc.wait(timeout=1.0)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass

        self.arbiter.release_aux_receiver("ADSB_LIVE")


_adsb_receiver_singleton: Optional[LiveADSBReceiver] = None
_adsb_lock = threading.Lock()


def get_adsb_receiver() -> LiveADSBReceiver:
    global _adsb_receiver_singleton
    with _adsb_lock:
        if _adsb_receiver_singleton is None:
            _adsb_receiver_singleton = LiveADSBReceiver()
        return _adsb_receiver_singleton
