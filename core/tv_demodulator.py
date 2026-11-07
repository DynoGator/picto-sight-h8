#!/usr/bin/env python3
"""
core/tv_demodulator.py - Real-Time Broadcast Television Demodulator & RF Analyzer
=================================================================================
Provides crash-free terrestrial Broadcast TV demodulation on HackRF One:
  1. Digital ATSC 1.0 (8VSB) Receiver:
     - Isolated GNU Radio subprocess: osmosdr.source (8.0 MSPS, 6 MHz BW)
       -> gnuradio.dtv.atsc_rx(8_000_000.0, 1.1) -> 188-byte MPEG2-TS UDP stream
       (udp://127.0.0.1:17355) + live 1024-pt Hann FFT spectrum & +309.441 kHz
       8VSB pilot carrier lock/SNR telemetry.
     - Low-latency mpv / ffplay video+audio window sized cleanly for 800x480 DSI.
  2. Analog NTSC / Fast-Scan ATV Envelope Raster Receiver:
     - 4.0 MSPS HackRF capture -> AM visual carrier demodulation (+1.25 MHz above
       lower band edge), horizontal sync alignment (15,734.26 Hz), live 320x240
       grayscale/CRT raster frame output + live RF spectrum.
  3. Southern Colorado / Penrose Wideband TV Channel Scanner:
     - Fast VHF-Hi + UHF sweep ranking active 6.0 MHz TV channels & pilot SNR.
  4. Strict 1-at-a-time SDRResourceArbiter ownership ("TV_DEMOD").
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from core.demod_engine import identify_atsc_tv_channel
from core.sdr_arbiter import detect_hackrf_sysfs, format_osmosdr_hackrf_device_arg, get_sdr_arbiter

logger = logging.getLogger("smart_scanner.tv_demod")

TV_STATUS_JSON_PATH = "/tmp/picto_tv_status.json"
TV_RASTER_NPY_PATH = "/tmp/picto_tv_raster.npy"
TV_UDP_HOST = "127.0.0.1"
TV_UDP_PORT = 17355

# Southern Colorado (Pueblo / Cheyenne Mtn / Penrose) Broadcast TV Station Guide
SOUTHERN_CO_TV_PRESETS: List[Dict[str, Any]] = [
    {"channel": 25, "center_hz": 539_000_000, "call": "KOAA-TV (NBC 5)", "site": "Baculite Mesa / Pueblo"},
    {"channel": 24, "center_hz": 533_000_000, "call": "KKTV (CBS 11)", "site": "Cheyenne Mtn"},
    {"channel": 22, "center_hz": 521_000_000, "call": "KXRM-TV (FOX 21)", "site": "Cheyenne Mtn"},
    {"channel": 19, "center_hz": 503_000_000, "call": "KRDO-TV (ABC 13)", "site": "Cheyenne Mtn"},
    {"channel": 8,  "center_hz": 183_000_000, "call": "KTSC (Rocky Mtn PBS)", "site": "Baculite Mesa / Pueblo"},
    {"channel": 26, "center_hz": 545_000_000, "call": "KZCS-LD (Azteca)", "site": "Colorado Springs"},
    {"channel": 27, "center_hz": 551_000_000, "call": "KVSN-DT (Univision)", "site": "Pueblo / Cheyenne Mtn"},
    {"channel": 14, "center_hz": 473_000_000, "call": "UHF Ch 14", "site": "470-476 MHz"},
    {"channel": 7,  "center_hz": 177_000_000, "call": "VHF-Hi Ch 7", "site": "174-180 MHz"},
]


def get_all_fcc_tv_channels() -> List[Dict[str, Any]]:
    """Returns full FCC North American Broadcast TV table (Ch 2 - 36)."""
    by_ch = {p["channel"]: p for p in SOUTHERN_CO_TV_PRESETS}
    table: List[Dict[str, Any]] = []
    for ch in range(2, 37):
        if ch == 2:
            lo = 54_000_000
        elif ch == 3:
            lo = 60_000_000
        elif ch == 4:
            lo = 66_000_000
        elif ch == 5:
            lo = 76_000_000
        elif ch == 6:
            lo = 82_000_000
        elif 7 <= ch <= 13:
            lo = 174_000_000 + (ch - 7) * 6_000_000
        else:
            lo = 470_000_000 + (ch - 14) * 6_000_000
        center = lo + 3_000_000
        pilot = lo + 309_441
        preset = by_ch.get(ch)
        call = preset["call"] if preset else f"ATSC Ch {ch}"
        site = preset["site"] if preset else f"{lo / 1e6:.0f}-{(lo + 6_000_000) / 1e6:.0f} MHz"
        table.append(
            {
                "channel": ch,
                "lo_hz": lo,
                "hi_hz": lo + 6_000_000,
                "center_hz": center,
                "pilot_hz": pilot,
                "call": call,
                "site": site,
            }
        )
    return table


@dataclass
class TVReceiverTelemetry:
    running: bool = False
    mode: str = "ATSC_8VSB"
    channel: int = 25
    callsign: str = "KOAA-TV (NBC 5)"
    center_freq_hz: int = 539_000_000
    pilot_freq_hz: float = 536_309_441.0
    pilot_dbfs: float = -90.0
    shelf_dbfs: float = -92.0
    noise_floor_dbfs: float = -95.0
    pilot_snr_db: float = 0.0
    channel_snr_db: float = 0.0
    mer_est_db: float = 0.0
    pilot_locked: bool = False
    ts_sync_locked: bool = False
    ts_packets_total: int = 0
    ts_bitrate_kbps: float = 0.0
    spectrum_freqs_mhz: List[float] = field(default_factory=list)
    spectrum_dbfs: List[float] = field(default_factory=list)
    status_text: str = "Idle"
    updated_ts: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _compute_8mhz_tv_metrics(
    iq: np.ndarray,
    center_freq_hz: float,
    sample_rate: float = 8_000_000.0,
) -> Dict[str, Any]:
    """
    Computes calibrated 1024-pt Hann FFT across 8.0 MHz (7.8125 kHz/bin), extracting:
      - Out-of-channel guard-band noise floor (lower -4.0..-3.15 MHz & upper +3.15..+4.0 MHz)
      - 6.0 MHz in-channel ATSC shelf median power
      - Exact +309.441 kHz 8VSB pilot carrier power (-2.69056 MHz offset from channel center)
      - 160-point display spectrum spanning [center - 3.6 MHz, center + 3.6 MHz]
    """
    nfft = 1024
    if len(iq) < nfft:
        return {}

    n_blocks = min(24, max(1, len(iq) // nfft))
    seg = iq[: n_blocks * nfft].reshape(n_blocks, nfft)
    seg = seg - np.mean(seg, axis=1, keepdims=True)
    win = np.hanning(nfft).astype(np.float32)
    win_sum = float(np.sum(win))
    spec = np.fft.fftshift(np.fft.fft(seg * win[None, :], n=nfft, axis=1), axes=1)
    psd_lin = np.mean((np.abs(spec) / win_sum) ** 2, axis=0)
    # Remove residual 0 Hz DC bin
    mid = nfft // 2
    psd_lin[mid] = 0.5 * (psd_lin[mid - 1] + psd_lin[mid + 1])
    psd_db = 10.0 * np.log10(psd_lin + 1e-13)

    freqs_hz = center_freq_hz + np.linspace(-sample_rate * 0.5, sample_rate * 0.5, nfft, endpoint=False)
    lo_edge = center_freq_hz - 3_000_000.0
    hi_edge = center_freq_hz + 3_000_000.0
    pilot_hz = lo_edge + 309_441.0

    guard_mask = (freqs_hz <= lo_edge - 160_000.0) | (freqs_hz >= hi_edge + 160_000.0)
    noise_floor = float(np.percentile(psd_db[guard_mask], 25)) if np.any(guard_mask) else float(np.percentile(psd_db, 15))

    in_chan_mask = (freqs_hz >= lo_edge + 120_000.0) & (freqs_hz <= hi_edge - 120_000.0)
    shelf_db = float(np.median(psd_db[in_chan_mask])) if np.any(in_chan_mask) else noise_floor

    pilot_mask = np.abs(freqs_hz - pilot_hz) <= 32_000.0
    pilot_db = float(np.max(psd_db[pilot_mask])) if np.any(pilot_mask) else shelf_db

    pilot_snr = max(0.0, pilot_db - noise_floor)
    chan_snr = max(0.0, shelf_db - noise_floor)
    pilot_prominence = pilot_db - shelf_db
    pilot_locked = bool(pilot_snr >= 8.5 and pilot_prominence >= 3.2)
    mer_est = round(chan_snr + (2.5 if pilot_locked else 0.0), 1)

    # Downsample central 7.2 MHz span to 160 points for smooth UI rendering
    disp_mask = (freqs_hz >= center_freq_hz - 3_600_000.0) & (freqs_hz <= center_freq_hz + 3_600_000.0)
    d_f = freqs_hz[disp_mask] / 1e6
    d_p = psd_db[disp_mask]
    if len(d_f) > 160:
        x_new = np.linspace(float(d_f[0]), float(d_f[-1]), 160)
        d_p = np.interp(x_new, d_f, d_p)
        d_f = x_new

    return {
        "pilot_freq_hz": pilot_hz,
        "pilot_dbfs": round(pilot_db, 1),
        "shelf_dbfs": round(shelf_db, 1),
        "noise_floor_dbfs": round(noise_floor, 1),
        "pilot_snr_db": round(pilot_snr, 1),
        "channel_snr_db": round(chan_snr, 1),
        "mer_est_db": mer_est,
        "pilot_locked": pilot_locked,
        "spectrum_freqs_mhz": [round(float(x), 3) for x in d_f],
        "spectrum_dbfs": [round(float(y), 1) for y in d_p],
    }


def measure_tv_channel_rf_snapshot(
    center_freq_hz: float,
    lna_gain: int = 32,
    vga_gain: int = 28,
    amp_enable: int = 1,
) -> Dict[str, Any]:
    """One-shot 8.0 MSPS HackRF snapshot measuring 6 MHz ATSC TV channel & 8VSB pilot."""
    arbiter = get_sdr_arbiter()
    ok, msg = arbiter.prepare_for_aux_receiver("TELEM_DECODE", f"TV RF Probe @ {center_freq_hz / 1e6:.1f} MHz")
    if not ok:
        return {"measured": False, "error": msg}

    try:
        cmd = [
            "hackrf_transfer",
            "-r", "-",
            "-f", str(int(center_freq_hz)),
            "-s", "8000000",
            "-b", "6000000",
            "-l", str(lna_gain),
            "-g", str(vga_gain),
            "-a", str(1 if amp_enable else 0),
            "-n", "262144",
        ]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=3.5)
        raw = proc.stdout or b""
        if len(raw) < 16384:
            return {"measured": False, "error": "Short IQ buffer from HackRF"}
        arr = np.frombuffer(raw, dtype=np.int8).astype(np.float32) / 128.0
        n2 = (len(arr) // 2) * 2
        iq = arr[0:n2:2] + 1j * arr[1:n2:2]
        metrics = _compute_8mhz_tv_metrics(iq, float(center_freq_hz), 8_000_000.0)
        metrics["measured"] = True
        return metrics
    except Exception as exc:
        return {"measured": False, "error": str(exc)}
    finally:
        arbiter.release_aux_receiver("TELEM_DECODE")


def scan_active_tv_channels(
    lna_gain: int = 32,
    vga_gain: int = 30,
    amp_enable: int = 1,
) -> Dict[str, Any]:
    """
    Fast wideband VHF-Hi (174-216 MHz) + UHF (470-608 MHz) Broadcast TV channel scan
    via hackrf_sweep. Ranks active 6 MHz FCC channels by pilot/shelf SNR cleanly!
    """
    arbiter = get_sdr_arbiter()
    ok, msg = arbiter.prepare_for_aux_receiver("TELEM_DECODE", "Scanning VHF/UHF Broadcast TV...")
    if not ok:
        return {"success": False, "error": msg, "channels": []}

    try:
        freqs_all: List[float] = []
        pwrs_all: List[float] = []
        for f_range in ("174:216", "470:608"):
            cmd = [
                "hackrf_sweep",
                "-f", f_range,
                "-w", "50000",
                "-N", "3",
                "-l", str(lna_gain),
                "-g", str(vga_gain),
                "-a", str(1 if amp_enable else 0),
            ]
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=6.0)
            bucket: Dict[int, List[float]] = {}
            for line in (proc.stdout or "").splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) < 7:
                    continue
                try:
                    hz_low = float(parts[2])
                    bin_w = float(parts[4])
                    vals = [float(x) for x in parts[6:]]
                except Exception:
                    continue
                for idx, db_val in enumerate(vals):
                    cf = int(round(hz_low + (idx + 0.5) * bin_w))
                    bucket.setdefault(cf, []).append(db_val)
            for cf in sorted(bucket.keys()):
                freqs_all.append(float(cf))
                pwrs_all.append(float(np.mean(bucket[cf])))

        if len(freqs_all) < 16:
            return {"success": False, "error": "No sweep bins returned from HackRF", "channels": []}

        f_arr = np.array(freqs_all, dtype=np.float64)
        p_arr = np.array(pwrs_all, dtype=np.float32)
        global_floor = float(np.percentile(p_arr, 20))

        results: List[Dict[str, Any]] = []
        for ch_entry in get_all_fcc_tv_channels():
            ch = ch_entry["channel"]
            if ch < 7:
                continue
            lo = float(ch_entry["lo_hz"])
            hi = float(ch_entry["hi_hz"])
            pilot = float(ch_entry["pilot_hz"])
            m_chan = (f_arr >= lo + 100_000) & (f_arr <= hi - 100_000)
            if not np.any(m_chan):
                continue
            shelf_db = float(np.median(p_arr[m_chan]))
            m_pilot = np.abs(f_arr - pilot) <= 85_000
            pilot_db = float(np.max(p_arr[m_pilot])) if np.any(m_pilot) else float(np.max(p_arr[m_chan]))
            pilot_snr = round(max(0.0, pilot_db - global_floor), 1)
            chan_snr = round(max(0.0, shelf_db - global_floor), 1)
            pilot_lock = bool(pilot_snr >= 7.5 and (pilot_db - shelf_db) >= 2.2)
            results.append(
                {
                    "channel": ch,
                    "call": ch_entry["call"],
                    "site": ch_entry["site"],
                    "center_hz": ch_entry["center_hz"],
                    "center_mhz": round(ch_entry["center_hz"] / 1e6, 1),
                    "pilot_mhz": round(pilot / 1e6, 4),
                    "pilot_dbfs": round(pilot_db, 1),
                    "shelf_dbfs": round(shelf_db, 1),
                    "pilot_snr_db": pilot_snr,
                    "channel_snr_db": chan_snr,
                    "pilot_locked": pilot_lock,
                }
            )

        results.sort(key=lambda r: (r["pilot_locked"], r["pilot_snr_db"], r["channel_snr_db"]), reverse=True)
        return {"success": True, "noise_floor_dbfs": round(global_floor, 1), "channels": results}
    except Exception as exc:
        return {"success": False, "error": str(exc), "channels": []}
    finally:
        arbiter.release_aux_receiver("TELEM_DECODE")


def _write_atomic_json(path: str, payload: Dict[str, Any]) -> None:
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, path)
    except Exception:
        pass


# ======================================================================
# Subprocess Worker Entrypoints (Isolated GNU Radio ATSC & Analog Raster)
# ======================================================================
def _run_atsc_gnuradio_worker(
    center_freq_hz: int,
    channel: int,
    callsign: str,
    lna_gain: int,
    vga_gain: int,
    amp_enable: int,
    udp_port: int,
) -> int:
    """
    Isolated OS process executing GNU Radio ATSC 1.0 8VSB demodulation + live 8 MHz FFT probe.
    Forwards decoded 188-byte MPEG-TS packets over localhost UDP to mpv/ffplay.
    """
    from gnuradio import blocks, dtv, gr
    import osmosdr

    stop_flag = False

    def _sig_handler(_signum: int, _frame: Any) -> None:
        nonlocal stop_flag
        stop_flag = True

    signal.signal(signal.SIGINT, _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)

    present, ser, _, _ = detect_hackrf_sysfs()
    dev_arg = format_osmosdr_hackrf_device_arg(ser) if present else "hackrf=0"

    udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    state_lock = threading.Lock()
    shared: Dict[str, Any] = {
        "ts_packets": 0,
        "ts_bytes_window": 0,
        "last_rate_ts": time.time(),
        "ts_bitrate_kbps": 0.0,
        "rf": {},
    }

    class MPEGTSUDPSink(gr.sync_block):
        def __init__(self) -> None:
            super().__init__(name="MPEGTSUDPSink", in_sig=[np.uint8], out_sig=None)
            self._rem = bytearray()

        def work(self, input_items: List[np.ndarray], _output_items: List[np.ndarray]) -> int:
            chunk = input_items[0].tobytes()
            if not chunk:
                return 0
            self._rem.extend(chunk)
            # Send in 7 * 188 = 1316-byte standard MPEG-TS UDP datagrams
            pkt_size = 1316
            valid_syncs = 0
            while len(self._rem) >= pkt_size:
                dg = bytes(self._rem[:pkt_size])
                del self._rem[:pkt_size]
                for off in range(0, pkt_size, 188):
                    if dg[off] == 0x47:
                        valid_syncs += 1
                try:
                    udp_sock.sendto(dg, (TV_UDP_HOST, udp_port))
                except Exception:
                    pass
            if valid_syncs > 0:
                with state_lock:
                    shared["ts_packets"] += valid_syncs
                    shared["ts_bytes_window"] += valid_syncs * 188
            return len(input_items[0])

    class RFSpectrumProbeSink(gr.sync_block):
        def __init__(self) -> None:
            super().__init__(
                name="RFSpectrumProbeSink",
                in_sig=[(np.complex64, 1024)],
                out_sig=None,
            )
            self._buf: List[np.ndarray] = []

        def work(self, input_items: List[np.ndarray], _output_items: List[np.ndarray]) -> int:
            vecs = input_items[0]
            for v in vecs:
                self._buf.append(v.copy())
                if len(self._buf) >= 8:
                    iq_cat = np.concatenate(self._buf, axis=0)
                    self._buf.clear()
                    rf_m = _compute_8mhz_tv_metrics(iq_cat, float(center_freq_hz), 8_000_000.0)
                    with state_lock:
                        shared["rf"] = rf_m
            return len(vecs)

    tb = gr.top_block("PictoSight_ATSC_8VSB_RX")
    src = osmosdr.source(args=dev_arg)
    src.set_sample_rate(8_000_000.0)
    src.set_center_freq(float(center_freq_hz), 0)
    src.set_bandwidth(6_000_000.0, 0)
    src.set_dc_offset_mode(2, 0)
    src.set_gain(14.0 if amp_enable else 0.0, "RF", 0)
    src.set_gain(float(lna_gain), "IF", 0)
    src.set_gain(float(vga_gain), "BB", 0)

    # Branch 1: ATSC 1.0 8VSB Digital Television Receiver -> UDP MPEG-TS
    atsc_rx = dtv.atsc_rx(8_000_000.0, 1.1)
    ts_sink = MPEGTSUDPSink()
    tb.connect(src, atsc_rx, ts_sink)

    # Branch 2: Low-overhead 1024-pt RF Spectrum & +309.441 kHz Pilot Lock Probe
    s2v = blocks.stream_to_vector(gr.sizeof_gr_complex, 1024)
    keep = blocks.keep_one_in_n(gr.sizeof_gr_complex * 1024, 320)
    rf_probe = RFSpectrumProbeSink()
    tb.connect(src, s2v, keep, rf_probe)

    tb.start()
    try:
        while not stop_flag:
            time.sleep(0.35)
            now = time.time()
            with state_lock:
                dt = max(0.05, now - shared["last_rate_ts"])
                kbps = (shared["ts_bytes_window"] * 8.0 / 1000.0) / dt
                shared["ts_bytes_window"] = 0
                shared["last_rate_ts"] = now
                shared["ts_bitrate_kbps"] = round(kbps, 1)
                ts_pkts = int(shared["ts_packets"])
                rf_m = dict(shared["rf"])

            ts_locked = kbps >= 50.0
            pilot_locked = bool(rf_m.get("pilot_locked", False))
            if ts_locked:
                st = f"LOCKED ATSC MPEG-TS ({kbps:.0f} kbps) • Video Ready!"
            elif pilot_locked:
                st = f"8VSB Pilot Locked (+{rf_m.get('pilot_snr_db', 0):.1f} dB) • Equalizing VSB..."
            else:
                st = f"Monitoring Ch {channel} ({center_freq_hz / 1e6:.1f} MHz) • Adjust antenna/gain for >15 dB SNR"

            payload = {
                "running": True,
                "mode": "ATSC_8VSB",
                "channel": channel,
                "callsign": callsign,
                "center_freq_hz": center_freq_hz,
                "ts_sync_locked": ts_locked,
                "ts_packets_total": ts_pkts,
                "ts_bitrate_kbps": round(kbps, 1),
                "status_text": st,
                "updated_ts": now,
                **rf_m,
            }
            _write_atomic_json(TV_STATUS_JSON_PATH, payload)
    finally:
        try:
            tb.stop()
            tb.wait()
        except Exception:
            pass
        udp_sock.close()
    return 0


def _run_analog_ntsc_worker(
    center_freq_hz: int,
    channel: int,
    callsign: str,
    lna_gain: int,
    vga_gain: int,
    amp_enable: int,
) -> int:
    """
    Isolated OS process capturing HackRF 4.0 MSPS IQ around the visual carrier
    (lo + 1.25 MHz), envelope-demodulating NTSC/ATV 525-line video, aligning horizontal
    sync pulses (15,734.26 Hz -> 254.22 samples/line), and writing 240x320 uint8 raster
    frames + live RF spectrum.
    """
    stop_flag = False

    def _sig_handler(_signum: int, _frame: Any) -> None:
        nonlocal stop_flag
        stop_flag = True

    signal.signal(signal.SIGINT, _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)

    lo_edge = center_freq_hz - 3_000_000
    vis_carrier_hz = lo_edge + 1_250_000
    fs = 4_000_000

    cmd = [
        "hackrf_transfer",
        "-r", "-",
        "-f", str(int(vis_carrier_hz)),
        "-s", str(fs),
        "-b", "3500000",
        "-l", str(lna_gain),
        "-g", str(vga_gain),
        "-a", str(1 if amp_enable else 0),
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    frame_samples = int(fs / 29.97)  # ~133,467 complex samples per NTSC frame
    bytes_needed = frame_samples * 2

    try:
        while not stop_flag and proc.poll() is None and proc.stdout is not None:
            buf = proc.stdout.read(bytes_needed)
            if not buf or len(buf) < bytes_needed:
                break

            arr = np.frombuffer(buf, dtype=np.int8).astype(np.float32) / 128.0
            iq = arr[0::2] + 1j * arr[1::2]
            iq = iq - np.mean(iq)

            # Negative-modulation AM video envelope (sync tip = max RF, white = min RF)
            env = np.abs(iq)
            p5 = float(np.percentile(env, 5))
            p98 = float(np.percentile(env, 98))
            inv_luma = 1.0 - np.clip((env - p5) / max(0.02, p98 - p5), 0.0, 1.0)

            # Find strongest horizontal sync alignment offset across 254-sample line stride
            samples_per_line = fs / 15_734.264  # 254.222 samples/line
            n_lines = 240
            line_starts = (np.arange(n_lines, dtype=np.float32) * samples_per_line).astype(np.int32)
            # Coarse sync search across 0..253
            test_grid = env[: min(len(env), 254 * 64)].reshape(-1, 254)
            sync_col = int(np.argmax(np.mean(test_grid, axis=0)))

            col_offsets = np.linspace(0, samples_per_line - 1, 320, dtype=np.float32).astype(np.int32)
            idx_2d = np.clip(sync_col + line_starts[:, None] + col_offsets[None, :], 0, len(inv_luma) - 1)
            raster_u8 = (inv_luma[idx_2d] * 255.0).astype(np.uint8)

            tmp_npy = f"{TV_RASTER_NPY_PATH}.tmp.npy"
            np.save(tmp_npy, raster_u8)
            os.replace(tmp_npy, TV_RASTER_NPY_PATH)

            rf_m = _compute_8mhz_tv_metrics(iq[:32768], float(vis_carrier_hz), float(fs))
            now = time.time()
            payload = {
                "running": True,
                "mode": "ANALOG_NTSC",
                "channel": channel,
                "callsign": callsign,
                "center_freq_hz": center_freq_hz,
                "ts_sync_locked": False,
                "ts_packets_total": 0,
                "ts_bitrate_kbps": 0.0,
                "status_text": (
                    f"Analog NTSC/ATV Live Raster @ {vis_carrier_hz / 1e6:.2f} MHz Visual Carrier "
                    f"(Peak {rf_m.get('pilot_dbfs', -80):.1f} dBFS)"
                ),
                "updated_ts": now,
                **rf_m,
            }
            _write_atomic_json(TV_STATUS_JSON_PATH, payload)
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=1.0)
            except Exception:
                proc.kill()
    return 0


# ======================================================================
# GUI-Safe BroadcastTVController
# ======================================================================
class BroadcastTVController:
    """
    Manages the isolated Broadcast TV receiver worker process + optional mpv/ffplay
    video player window, registered with SDRResourceArbiter for 1-at-a-time HackRF safety.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._worker_proc: Optional[subprocess.Popen] = None
        self._player_proc: Optional[subprocess.Popen] = None
        self._mode: str = "ATSC_8VSB"
        self._center_freq_hz: int = 539_000_000
        self._channel: int = 25
        self._callsign: str = "KOAA-TV (NBC 5)"
        self.arbiter = get_sdr_arbiter()
        self.arbiter.register_aux_stopper("TV_DEMOD", self.stop)

    def is_running(self) -> bool:
        with self._lock:
            return self._worker_proc is not None and self._worker_proc.poll() is None

    def is_player_running(self) -> bool:
        with self._lock:
            return self._player_proc is not None and self._player_proc.poll() is None

    def start(
        self,
        center_freq_hz: float,
        mode: str = "ATSC_8VSB",
        lna_gain: int = 32,
        vga_gain: int = 28,
        amp_enable: int = 1,
        auto_open_player: bool = False,
    ) -> Tuple[bool, str]:
        """Starts ATSC 8VSB or Analog NTSC TV demodulation cleanly under SDRResourceArbiter."""
        self.stop()

        tv_info = identify_atsc_tv_channel(center_freq_hz)
        if tv_info is not None:
            ch, exact_center, _, _ = tv_info
            c_hz = int(exact_center)
        else:
            ch = 25
            c_hz = int(center_freq_hz)

        callsign = f"ATSC Ch {ch}"
        for p in get_all_fcc_tv_channels():
            if p["channel"] == ch:
                callsign = p["call"]
                break

        ok_sdr, sdr_msg = self.arbiter.prepare_for_aux_receiver(
            "TV_DEMOD",
            f"TV Ch {ch} ({c_hz / 1e6:.1f} MHz • {mode})",
        )
        if not ok_sdr:
            return False, sdr_msg

        for p_old in (TV_STATUS_JSON_PATH, TV_RASTER_NPY_PATH):
            try:
                if os.path.exists(p_old):
                    os.remove(p_old)
            except Exception:
                pass

        cmd = [
            sys.executable,
            "-m",
            "core.tv_demodulator",
            "--worker",
            "--mode", mode,
            "--freq", str(c_hz),
            "--channel", str(ch),
            "--callsign", callsign,
            "--lna", str(int(lna_gain)),
            "--vga", str(int(vga_gain)),
            "--amp", str(1 if amp_enable else 0),
        ]

        try:
            with self._lock:
                self._mode = mode
                self._center_freq_hz = c_hz
                self._channel = ch
                self._callsign = callsign
                self._worker_proc = subprocess.Popen(
                    cmd,
                    cwd=str(Path(__file__).resolve().parent.parent),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            if auto_open_player and mode == "ATSC_8VSB":
                self.launch_video_player()
            return True, f"Started {mode} Receiver on Ch {ch} ({c_hz / 1e6:.1f} MHz)"
        except Exception as exc:
            self.arbiter.release_aux_receiver("TV_DEMOD")
            return False, f"Failed to start TV receiver: {exc}"

    def launch_video_player(self) -> Tuple[bool, str]:
        """Launches compact 640x360 mpv or ffplay window attached to UDP MPEG-TS output."""
        with self._lock:
            if self._player_proc is not None and self._player_proc.poll() is None:
                return True, "Video player window is already open."
            title = f"Picto-Sight TV Ch {self._channel} - {self._callsign} ({self._center_freq_hz / 1e6:.1f} MHz)"

        env = os.environ.copy()
        env.setdefault("DISPLAY", ":0")
        env.setdefault("WAYLAND_DISPLAY", "wayland-0")
        env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")

        udp_url = f"udp://{TV_UDP_HOST}:{TV_UDP_PORT}"
        mpv_bin = shutil.which("mpv")
        ffplay_bin = shutil.which("ffplay")
        if mpv_bin:
            args = [
                mpv_bin,
                f"--title={title}",
                "--geometry=640x360",
                "--profile=low-latency",
                "--no-cache",
                "--demuxer-lavf-o=fflags=+nobuffer",
                "--force-window=immediate",
                udp_url,
            ]
        elif ffplay_bin:
            args = [
                ffplay_bin,
                "-window_title", title,
                "-x", "640",
                "-y", "360",
                "-fflags", "nobuffer",
                "-flags", "low_delay",
                "-f", "mpegts",
                udp_url,
            ]
        else:
            return False, "Neither mpv nor ffplay is installed."

        try:
            with self._lock:
                self._player_proc = subprocess.Popen(
                    args,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=env,
                    start_new_session=True,
                )
            return True, f"Opened live MPEG-TS video player ({os.path.basename(args[0])})."
        except Exception as exc:
            return False, str(exc)

    def read_telemetry(self) -> TVReceiverTelemetry:
        """Non-blocking telemetry read safe for 250ms GUI QTimer polling."""
        running = self.is_running()
        telem = TVReceiverTelemetry(
            running=running,
            mode=self._mode,
            channel=self._channel,
            callsign=self._callsign,
            center_freq_hz=self._center_freq_hz,
        )
        if os.path.exists(TV_STATUS_JSON_PATH):
            try:
                with open(TV_STATUS_JSON_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for k, v in data.items():
                    if hasattr(telem, k):
                        setattr(telem, k, v)
                telem.running = running
            except Exception:
                pass
        return telem

    def read_analog_raster(self) -> Optional[np.ndarray]:
        """Reads latest 240x320 uint8 analog raster frame if available."""
        if not os.path.exists(TV_RASTER_NPY_PATH):
            return None
        try:
            return np.load(TV_RASTER_NPY_PATH)
        except Exception:
            return None

    def stop(self) -> None:
        """Stops GNU Radio / HackRF TV worker and video player, releasing SDRResourceArbiter."""
        with self._lock:
            w_proc = self._worker_proc
            p_proc = self._player_proc
            self._worker_proc = None
            self._player_proc = None

        for proc in (p_proc, w_proc):
            if proc is not None and proc.poll() is None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGINT)
                except Exception:
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                try:
                    proc.wait(timeout=1.2)
                except Exception:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass

        if w_proc is not None:
            time.sleep(0.20)
        self.arbiter.release_aux_receiver("TV_DEMOD")


_tv_controller_singleton: Optional[BroadcastTVController] = None
_tv_ctrl_lock = threading.Lock()


def get_tv_controller() -> BroadcastTVController:
    global _tv_controller_singleton
    with _tv_ctrl_lock:
        if _tv_controller_singleton is None:
            _tv_controller_singleton = BroadcastTVController()
        return _tv_controller_singleton


def main() -> int:
    parser = argparse.ArgumentParser(description="Picto-Sight H8 Broadcast TV Receiver")
    parser.add_argument("--worker", action="store_true", help="Internal worker subprocess mode")
    parser.add_argument("--mode", default="ATSC_8VSB", choices=["ATSC_8VSB", "ANALOG_NTSC"])
    parser.add_argument("--freq", type=int, default=539_000_000)
    parser.add_argument("--channel", type=int, default=25)
    parser.add_argument("--callsign", type=str, default="KOAA-TV (NBC 5)")
    parser.add_argument("--lna", type=int, default=32)
    parser.add_argument("--vga", type=int, default=28)
    parser.add_argument("--amp", type=int, default=1)
    parser.add_argument("--udp-port", type=int, default=TV_UDP_PORT)
    args = parser.parse_args()

    if args.worker:
        if args.mode == "ANALOG_NTSC":
            return _run_analog_ntsc_worker(
                args.freq, args.channel, args.callsign, args.lna, args.vga, args.amp
            )
        return _run_atsc_gnuradio_worker(
            args.freq, args.channel, args.callsign, args.lna, args.vga, args.amp, args.udp_port
        )

    # Interactive CLI launch
    print(f"Starting Picto-Sight Broadcast TV Receiver on Ch {args.channel} ({args.freq / 1e6:.1f} MHz)...")
    ctrl = get_tv_controller()
    ok, msg = ctrl.start(
        center_freq_hz=args.freq,
        mode=args.mode,
        lna_gain=args.lna,
        vga_gain=args.vga,
        amp_enable=args.amp,
        auto_open_player=(args.mode == "ATSC_8VSB"),
    )
    print(msg)
    if not ok:
        return 1
    try:
        while ctrl.is_running():
            time.sleep(1.0)
            t = ctrl.read_telemetry()
            print(
                f"[Ch {t.channel}] Pilot: {t.pilot_dbfs:.1f} dBFS (SNR +{t.pilot_snr_db:.1f} dB, Lock={t.pilot_locked}) | "
                f"Shelf: {t.shelf_dbfs:.1f} dBFS | TS: {t.ts_bitrate_kbps:.1f} kbps ({t.ts_packets_total} pkts)"
            )
    except KeyboardInterrupt:
        pass
    finally:
        ctrl.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
