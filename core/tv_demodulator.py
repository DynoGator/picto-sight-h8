#!/usr/bin/env python3
"""
core/tv_demodulator.py - Real-Time Broadcast Television Demodulator & RF Analyzer
=================================================================================
Provides crash-free terrestrial Broadcast TV demodulation on HackRF One:
  1. Digital ATSC 1.0 (8VSB) Receiver:
     - Isolated GNU Radio subprocess: osmosdr.source at native 11,838,462 Hz
       (atsc_sym_rate * 1.1) with analog AGC + 50-tap direct FFT RRC filter
       (filter.fft_filter_ccc) -> zero polyphase resampler CPU overhead!
     - atsc_fpll -> dc_blocker -> atsc_sync -> atsc_fs_checker -> atsc_equalizer
       -> atsc_viterbi_decoder -> atsc_deinterleaver -> atsc_rs_decoder -> atsc_derandomizer
       -> atsc_depad -> 188-byte MPEG2-TS UDP stream (udp://127.0.0.1:17355).
     - Live 8-level ATSC constellation / eye-diagram soft symbols for GUI display.
     - Low-latency mpv / ffplay video+audio window with --idle=yes & --force-window=yes
       so it never exits as defunct while waiting for packets.
  2. Analog NTSC / Fast-Scan ATV Envelope Raster & FM Audio Receiver:
     - 8.0 MSPS HackRF capture centered on channel.
     - AM visual carrier demodulation (-1.75 MHz offset from center), 15,734.26 Hz
       horizontal sync tip alignment, live 320x240 CRT raster frame output.
     - Integrated +4.5 MHz FM aural audio carrier demodulation (+2.75 MHz offset from center),
       quadrature FM discrimination, 75 us de-emphasis, 48 kHz S16LE PCM stream
       directly to PipeWire via paplay (with 1-tap live mute/unmute control).
  3. Southern Colorado / Penrose Wideband TV Channel Scanner:
     - Fast single-shot (-1) VHF-Hi + UHF sweep ranking active 6.0 MHz TV channels & pilot SNR.
  4. Strict 1-at-a-time SDRResourceArbiter ownership ("TV_DEMOD").
"""

from __future__ import annotations

import argparse
import atexit
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
TV_CONSTELLATION_NPY_PATH = "/tmp/picto_tv_constellation.npy"
TV_AUDIO_MUTE_PATH = "/tmp/picto_tv_audio_mute"
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


def ensure_system_audio_unmuted(min_volume: float = 0.80) -> bool:
    """Ensures PipeWire / PulseAudio default audio sink is not muted and has audible volume."""
    try:
        wpctl_bin = shutil.which("wpctl")
        if wpctl_bin:
            res = subprocess.run(
                [wpctl_bin, "get-volume", "@DEFAULT_AUDIO_SINK@"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=1.0,
            )
            out = res.stdout.strip()
            parts = out.split()
            if len(parts) >= 2:
                try:
                    vol = float(parts[1])
                except ValueError:
                    vol = 0.0
                muted = "[MUTED]" in out
                if muted or vol < 0.15:
                    subprocess.run([wpctl_bin, "set-mute", "@DEFAULT_AUDIO_SINK@", "0"], timeout=1.0)
                    subprocess.run(
                        [wpctl_bin, "set-volume", "@DEFAULT_AUDIO_SINK@", f"{min_volume:.2f}"],
                        timeout=1.0,
                    )
                    return True
        return True
    except Exception:
        return False


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
    audio_enabled: bool = True
    audio_active: bool = False
    constellation_samples: List[float] = field(default_factory=list)
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
    Computes calibrated 1024-pt Hann FFT across captured bandwidth, extracting:
      - Out-of-channel guard-band noise floor
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
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=4.0)
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
    via hackrf_sweep. Uses one-shot '-1' flag for instant <1.5s completion!
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
                "-1",
                "-f", f_range,
                "-w", "100000",
                "-l", str(lna_gain),
                "-g", str(vga_gain),
                "-a", str(1 if amp_enable else 0),
            ]
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=12.0)
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
    parent_pid: int = 0,
) -> int:
    """
    Isolated OS process executing GNU Radio ATSC 1.0 8VSB demodulation + live 8 MHz FFT probe.
    Uses native 11,838,462 Hz sample rate with direct 50-tap FFT RRC filter and AGC, eliminating
    polyphase resampler overhead entirely. Forwards decoded 188-byte MPEG-TS packets over UDP.
    """
    from gnuradio import analog, blocks, dtv, filter, gr
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
        "constellation": [],
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
            pkt_size = 1316  # 7 * 188 bytes
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
            super().__init__(name="RFSpectrumProbeSink", in_sig=[(np.complex64, 1024)], out_sig=None)
            self._buf: List[np.ndarray] = []

        def work(self, input_items: List[np.ndarray], _output_items: List[np.ndarray]) -> int:
            vecs = input_items[0]
            for v in vecs:
                self._buf.append(v.copy())
                if len(self._buf) >= 8:
                    iq_cat = np.concatenate(self._buf, axis=0)
                    self._buf.clear()
                    rf_m = _compute_8mhz_tv_metrics(iq_cat, float(center_freq_hz), sample_rate)
                    with state_lock:
                        shared["rf"] = rf_m
            return len(vecs)

    class ConstellationProbeSink(gr.sync_block):
        def __init__(self) -> None:
            super().__init__(name="ConstellationProbeSink", in_sig=[(np.float32, 256)], out_sig=None)
            self._last_save = 0.0

        def work(self, input_items: List[np.ndarray], _output_items: List[np.ndarray]) -> int:
            now = time.time()
            if now - self._last_save >= 0.20:
                self._last_save = now
                vec = input_items[0][-1] if len(input_items[0]) > 0 else np.array([])
                if len(vec) == 256:
                    c_list = [round(float(x), 2) for x in vec[:128]]
                    with state_lock:
                        shared["constellation"] = c_list
                    try:
                        tmp_c = f"{TV_CONSTELLATION_NPY_PATH}.tmp.npy"
                        np.save(tmp_c, vec.astype(np.float32))
                        os.replace(tmp_c, TV_CONSTELLATION_NPY_PATH)
                    except Exception:
                        pass
            return len(input_items[0])

    atsc_sym_rate = 4.5e6 / 286.0 * 684.0  # 10.762238 MHz
    sps = 1.1
    sample_rate = atsc_sym_rate * sps       # 11,838,462 Hz

    tb = gr.top_block("PictoSight_ATSC_8VSB_DirectRX")
    src = osmosdr.source(args=dev_arg)
    src.set_sample_rate(sample_rate)
    src.set_center_freq(float(center_freq_hz), 0)
    src.set_bandwidth(6_000_000.0, 0)
    src.set_dc_offset_mode(2, 0)
    src.set_gain(14.0 if amp_enable else 0.0, "RF", 0)
    src.set_gain(float(lna_gain), "IF", 0)
    src.set_gain(float(vga_gain), "BB", 0)

    # Input AGC ensuring optimal RMS for FPLL acquisition
    agc_in = analog.agc2_cc(1e-4, 1e-3, 0.5, 1.0)
    agc_in.set_max_gain(65536.0)

    # Direct 50-tap FFT Root Raised Cosine filter (eliminated polyphase resampler CPU bottleneck)
    rrc_taps = filter.firdes.root_raised_cosine(1.0, sample_rate, atsc_sym_rate / 2.0, 0.1152, 50)
    rrc = filter.fft_filter_ccc(1, rrc_taps, 1)

    # Digital ATSC 1.0 demodulation chain
    fpll = dtv.atsc_fpll(sample_rate)
    dcr = filter.dc_blocker_ff(4096)
    agc_const = analog.agc_ff(1e-5, 4.0)
    sync = dtv.atsc_sync(sample_rate)
    fsc = dtv.atsc_fs_checker()
    equ = dtv.atsc_equalizer()
    vit = dtv.atsc_viterbi_decoder()
    dei = dtv.atsc_deinterleaver()
    rsd = dtv.atsc_rs_decoder()
    der = dtv.atsc_derandomizer()
    dep = dtv.atsc_depad()
    ts_sink = MPEGTSUDPSink()

    tb.connect(src, agc_in, rrc, fpll, dcr, agc_const, sync, fsc)
    tb.connect((fsc, 0), (equ, 0))
    tb.connect((fsc, 1), (equ, 1))
    tb.connect((equ, 0), (vit, 0))
    tb.connect((equ, 1), (vit, 1))
    tb.connect((vit, 0), (dei, 0))
    tb.connect((vit, 1), (dei, 1))
    tb.connect((dei, 0), (rsd, 0))
    tb.connect((dei, 1), (rsd, 1))
    tb.connect((rsd, 0), (der, 0))
    tb.connect((rsd, 1), (der, 1))
    tb.connect((der, 0), (dep, 0))
    tb.connect((dep, 0), ts_sink)

    # Low-overhead RF spectrum probe
    s2v_rf = blocks.stream_to_vector(gr.sizeof_gr_complex, 1024)
    keep_rf = blocks.keep_one_in_n(gr.sizeof_gr_complex * 1024, 384)
    rf_probe = RFSpectrumProbeSink()
    tb.connect(src, s2v_rf, keep_rf, rf_probe)

    # Soft symbol constellation probe
    s2v_c = blocks.stream_to_vector(gr.sizeof_float, 256)
    keep_c = blocks.keep_one_in_n(gr.sizeof_float * 256, 160)
    const_probe = ConstellationProbeSink()
    tb.connect(agc_const, s2v_c, keep_c, const_probe)

    tb.start()
    try:
        while not stop_flag:
            if parent_pid > 1 and not os.path.exists(f"/proc/{parent_pid}"):
                break
            time.sleep(0.30)
            now = time.time()
            with state_lock:
                dt = max(0.05, now - shared["last_rate_ts"])
                kbps = (shared["ts_bytes_window"] * 8.0 / 1000.0) / dt
                shared["ts_bytes_window"] = 0
                shared["last_rate_ts"] = now
                shared["ts_bitrate_kbps"] = round(kbps, 1)
                ts_pkts = int(shared["ts_packets"])
                rf_m = dict(shared["rf"])
                c_samples = list(shared["constellation"])

            ts_locked = kbps >= 50.0
            pilot_locked = bool(rf_m.get("pilot_locked", False))
            pilot_snr = rf_m.get("pilot_snr_db", 0.0)
            if ts_locked:
                st = f"LOCKED ATSC MPEG-TS ({kbps:.0f} kbps) • Video Player Ready!"
            elif pilot_locked:
                st = f"8VSB Pilot Locked (+{pilot_snr:.1f} dB) • Symbol Timing & Equalizer Sync Active"
            else:
                st = f"Monitoring Ch {channel} ({center_freq_hz / 1e6:.1f} MHz) • Peak Pilot: +{pilot_snr:.1f} dB"

            payload = {
                "running": True,
                "mode": "ATSC_8VSB",
                "channel": channel,
                "callsign": callsign,
                "center_freq_hz": center_freq_hz,
                "ts_sync_locked": ts_locked,
                "ts_packets_total": ts_pkts,
                "ts_bitrate_kbps": round(kbps, 1),
                "audio_enabled": True,
                "audio_active": ts_locked,
                "constellation_samples": c_samples,
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
    parent_pid: int = 0,
) -> int:
    """
    Isolated OS process capturing HackRF 8.0 MSPS IQ across the 6.0 MHz channel.
    Simultaneously produces:
      1. Negative-modulation AM visual carrier video (offset -1.75 MHz) -> 320x240 CRT raster
      2. Integrated +4.5 MHz FM aural audio carrier (offset +2.75 MHz) -> 48 kHz S16LE PCM
         streamed directly to PipeWire via paplay (with 1-tap live mute/unmute control)
      3. Live 7.2 MHz RF spectrum probe
    """
    stop_flag = False

    def _sig_handler(_signum: int, _frame: Any) -> None:
        nonlocal stop_flag
        stop_flag = True

    signal.signal(signal.SIGINT, _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)

    fs = 8_000_000
    vis_offset_hz = -1_750_000.0   # Visual carrier is 1.25 MHz above lower edge
    audio_offset_hz = +2_750_000.0 # Audio subcarrier is +4.5 MHz above visual carrier

    ensure_system_audio_unmuted(0.85)

    cmd = [
        "hackrf_transfer",
        "-r", "-",
        "-f", str(int(center_freq_hz)),
        "-s", str(fs),
        "-b", "6000000",
        "-l", str(lna_gain),
        "-g", str(vga_gain),
        "-a", str(1 if amp_enable else 0),
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    # Launch paplay low-latency PCM audio sink to PipeWire default sink
    paplay_bin = shutil.which("paplay")
    audio_proc: Optional[subprocess.Popen] = None
    if paplay_bin:
        try:
            audio_proc = subprocess.Popen(
                [paplay_bin, "--raw", "--rate=48000", "--channels=1", "--format=s16le"],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            audio_proc = None

    # Process in ~0.25-second chunks (2,000,000 complex samples = 4,000,000 bytes)
    chunk_samples = 2_000_000
    chunk_bytes = chunk_samples * 2

    # Precalculate mixing LO arrays for visual and audio downconversion
    t_chunk = np.arange(chunk_samples, dtype=np.float32) / float(fs)
    lo_vis = np.exp(-1j * 2.0 * np.pi * vis_offset_hz * t_chunk).astype(np.complex64)
    lo_audio = np.exp(-1j * 2.0 * np.pi * audio_offset_hz * t_chunk).astype(np.complex64)

    # 75 us FM de-emphasis IIR filter state
    out_audio_rate = 48000
    alpha_deemp = float(np.exp(-1.0 / (out_audio_rate * 75e-6)))
    deemp_state = 0.0

    frame_counter = 0

    try:
        while not stop_flag and proc.poll() is None and proc.stdout is not None:
            if parent_pid > 1 and not os.path.exists(f"/proc/{parent_pid}"):
                break
            buf = proc.stdout.read(chunk_bytes)
            if not buf or len(buf) < chunk_bytes:
                break

            arr = np.frombuffer(buf, dtype=np.int8).astype(np.float32) / 128.0
            iq = arr[0::2] + 1j * arr[1::2]
            iq = iq - np.mean(iq)

            frame_counter += 1
            audio_muted = os.path.exists(TV_AUDIO_MUTE_PATH)

            # -------------------------------------------------------------
            # Branch 1: +4.5 MHz FM Aural Audio Subcarrier Demodulation
            # -------------------------------------------------------------
            audio_active = False
            try:
                # Downconvert audio carrier from +2.75 MHz to DC
                iq_aud = iq * lo_audio
                # Decimate by 32: 8,000,000 Hz / 32 = 250,000 Hz intermediate FM rate
                decim_aud = 32
                n_b = len(iq_aud) // decim_aud
                iq_aud_dec = np.mean(iq_aud[: n_b * decim_aud].reshape(n_b, decim_aud), axis=1)

                # FM discriminator (quadrature phase derivative)
                dphi = np.angle(iq_aud_dec[1:] * np.conj(iq_aud_dec[:-1])).astype(np.float32)
                dphi = dphi - float(np.mean(dphi))

                # Exact fractional resample: 250 kHz -> 48 kHz
                in_aud_rate = fs / decim_aud
                n_out_pcm = int(round(len(dphi) * out_audio_rate / in_aud_rate))
                x_in = np.linspace(0.0, 1.0, len(dphi), dtype=np.float32)
                x_out = np.linspace(0.0, 1.0, n_out_pcm, dtype=np.float32)
                pcm_raw = np.interp(x_out, x_in, dphi).astype(np.float32)

                # 75 us de-emphasis IIR
                pcm_de = np.empty_like(pcm_raw)
                curr_y = deemp_state
                for i in range(len(pcm_raw)):
                    curr_y = (1.0 - alpha_deemp) * pcm_raw[i] + alpha_deemp * curr_y
                    pcm_de[i] = curr_y
                deemp_state = curr_y

                # Calibrate audio level to S16LE range
                peak_aud = float(np.percentile(np.abs(pcm_de), 98))
                scale_aud = 15000.0 / max(1e-4, peak_aud)
                pcm_s16 = np.clip(pcm_de * scale_aud, -32767.0, 32767.0).astype(np.int16)

                if audio_proc is not None and audio_proc.stdin is not None and not audio_muted:
                    audio_proc.stdin.write(pcm_s16.tobytes())
                    audio_proc.stdin.flush()
                    audio_active = True
            except Exception:
                audio_active = False

            # -------------------------------------------------------------
            # Branch 2: AM Video Envelope Demodulation & Horizontal Sync Raster
            # -------------------------------------------------------------
            # Downconvert visual carrier from -1.75 MHz to DC and decimate by 2 -> 4.0 MSPS
            iq_v = iq * lo_vis
            iq_v_4m = 0.5 * (iq_v[0::2] + iq_v[1::2])
            fs_vis = 4_000_000.0
            samples_per_line = fs_vis / 15_734.264  # 254.222 samples per NTSC line

            env = np.abs(iq_v_4m)
            p5 = float(np.percentile(env, 5))
            p98 = float(np.percentile(env, 98))
            inv_luma = 1.0 - np.clip((env - p5) / max(0.02, p98 - p5), 0.0, 1.0)

            n_lines = 240
            line_starts = (np.arange(n_lines, dtype=np.float32) * samples_per_line).astype(np.int32)
            test_grid = env[: min(len(env), 254 * 64)].reshape(-1, 254)
            sync_col = int(np.argmax(np.mean(test_grid, axis=0)))

            col_offsets = np.linspace(0, samples_per_line - 1, 320, dtype=np.float32).astype(np.int32)
            idx_2d = np.clip(sync_col + line_starts[:, None] + col_offsets[None, :], 0, len(inv_luma) - 1)
            raster_u8 = (inv_luma[idx_2d] * 255.0).astype(np.uint8)

            tmp_npy = f"{TV_RASTER_NPY_PATH}.tmp.npy"
            np.save(tmp_npy, raster_u8)
            os.replace(tmp_npy, TV_RASTER_NPY_PATH)

            # -------------------------------------------------------------
            # Branch 3: RF Spectrum Metrics & Telemetry JSON
            # -------------------------------------------------------------
            rf_m = _compute_8mhz_tv_metrics(iq[:32768], float(center_freq_hz), float(fs))
            now = time.time()
            vis_mhz = (center_freq_hz + vis_offset_hz) / 1e6
            aud_mhz = (center_freq_hz + audio_offset_hz) / 1e6
            aud_status = "MUTED" if audio_muted else "48kHz PipeWire"

            payload = {
                "running": True,
                "mode": "ANALOG_NTSC",
                "channel": channel,
                "callsign": callsign,
                "center_freq_hz": center_freq_hz,
                "ts_sync_locked": False,
                "ts_packets_total": 0,
                "ts_bitrate_kbps": 0.0,
                "audio_enabled": not audio_muted,
                "audio_active": audio_active,
                "status_text": (
                    f"NTSC/ATV Raster ({vis_mhz:.2f}M Pic) + Live Sound ({aud_mhz:.2f}M FM • {aud_status})"
                ),
                "updated_ts": now,
                **rf_m,
            }
            _write_atomic_json(TV_STATUS_JSON_PATH, payload)
    finally:
        if audio_proc is not None:
            try:
                audio_proc.terminate()
                audio_proc.wait(timeout=0.6)
            except Exception:
                try:
                    audio_proc.kill()
                except Exception:
                    pass
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
        atexit.register(self.stop)

    def is_running(self) -> bool:
        with self._lock:
            return self._worker_proc is not None and self._worker_proc.poll() is None

    def is_player_running(self) -> bool:
        with self._lock:
            return self._player_proc is not None and self._player_proc.poll() is None

    def is_audio_muted(self) -> bool:
        return os.path.exists(TV_AUDIO_MUTE_PATH)

    def set_audio_muted(self, muted: bool) -> None:
        try:
            if muted:
                Path(TV_AUDIO_MUTE_PATH).touch()
            else:
                if os.path.exists(TV_AUDIO_MUTE_PATH):
                    os.remove(TV_AUDIO_MUTE_PATH)
        except Exception:
            pass

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

        for p_old in (TV_STATUS_JSON_PATH, TV_RASTER_NPY_PATH, TV_CONSTELLATION_NPY_PATH):
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
            "--parent-pid", str(os.getpid()),
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
        """Launches compact mpv / ffplay video window with --idle=yes so it never exits as defunct."""
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
                "--idle=yes",
                "--force-window=yes",
                "--demuxer-lavf-probesize=2000000",
                "--demuxer-lavf-analyzeduration=2.0",
                "--stream-buffer-size=8MiB",
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
            audio_enabled=not self.is_audio_muted(),
        )
        if os.path.exists(TV_STATUS_JSON_PATH):
            try:
                with open(TV_STATUS_JSON_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for k, v in data.items():
                    if hasattr(telem, k):
                        setattr(telem, k, v)
                telem.running = running
                telem.audio_muted = self.is_audio_muted()
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

    def read_constellation(self) -> Optional[np.ndarray]:
        """Reads latest 256 soft-symbol constellation samples if available."""
        if not os.path.exists(TV_CONSTELLATION_NPY_PATH):
            return None
        try:
            return np.load(TV_CONSTELLATION_NPY_PATH)
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
    parser.add_argument("--parent-pid", type=int, default=0)
    args = parser.parse_args()

    if args.worker:
        if args.mode == "ANALOG_NTSC":
            return _run_analog_ntsc_worker(
                args.freq, args.channel, args.callsign, args.lna, args.vga, args.amp, args.parent_pid
            )
        return _run_atsc_gnuradio_worker(
            args.freq, args.channel, args.callsign, args.lna, args.vga, args.amp, args.udp_port, args.parent_pid
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
