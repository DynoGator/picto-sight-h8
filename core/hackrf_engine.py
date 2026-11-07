"""
Multi-Layer HackRF One RF Acquisition, True 1 MHz IQ STFT & Signal Refinement Engine
Implements:
  - ScanConfig presets & custom controls (QUICK, DEEP, CUSTOM)
  - Layer 1: Multi-sweep wideband spectrum scan with 20th-percentile wide-span floor & prominence clustering
  - Layer 2: Merged-window fine-bin zoom refinement, LO spur rejection & channel raster snapping
  - Layer 3: True 1.000 MHz complex IQ capture via hackrf_transfer (2 MSPS, +250 kHz LO offset to eliminate
             0 Hz DC spike), producing >=3 independent 64x128 dBFS STFT waterfall frames plus physical
             OBW and AM/FM/CW/Burst IQ modulation classification.
"""

from __future__ import annotations

import csv
import logging
import math
import os
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field, asdict
from typing import Callable, Dict, List, Optional, Tuple, Any

import numpy as np
from scipy.ndimage import percentile_filter, uniform_filter1d
from scipy.signal import find_peaks, firwin

from core.bandplans import refine_precision_channel_frequency, snap_to_channel_raster
from core.sdr_arbiter import get_sdr_arbiter

logger = logging.getLogger("smart_scanner.hackrf")

# Standard TIA-603 50-Tone CTCSS Subaudible PL Frequencies (Hz)
STANDARD_CTCSS_TONES_HZ: Tuple[float, ...] = (
    67.0, 69.3, 71.9, 74.4, 77.0, 79.7, 82.5, 85.4, 88.5, 91.5,
    94.8, 97.4, 100.0, 103.5, 107.2, 110.9, 114.8, 118.8, 123.0, 127.3,
    131.8, 136.5, 141.3, 146.2, 151.4, 156.7, 159.8, 162.2, 165.5, 167.9,
    171.3, 173.8, 177.3, 179.9, 183.5, 186.2, 189.9, 192.8, 196.6, 199.5,
    203.5, 206.5, 210.7, 218.1, 225.7, 229.1, 233.6, 241.8, 250.3, 254.1,
)

# Precomputed anti-alias FIR taps for clean 8x decimation stages (2.0 MSPS -> 250 kSPS -> 31.25 kSPS)
_FIR_DECIM8_TAPS: np.ndarray = firwin(33, cutoff=0.105, window="hamming").astype(np.float32)


def is_likely_internal_clock_spur(
    freq_hz: float,
    obw_hz: float,
    iq_mod_hint: str = "",
    freq_dev_hz: float = 0.0,
) -> bool:
    """
    Identifies unmodulated internal HackRF Si5351 clock harmonics (exact N * 20.000 MHz)
    and Pi 5 RP1/PCIe reference spurs (250.000, 350.000, 500.000 MHz) so wide multi-GHz
    scans don't get crowded by internal board clock needles. Never rejects modulated signals,
    wideband carriers, commercial FM broadcast, NOAA weather, or ADS-B.
    """
    f = float(freq_hz)
    if (87_800_000.0 <= f <= 108_100_000.0) or (162_380_000.0 <= f <= 162_570_000.0) or (1_089_000_000.0 <= f <= 1_091_000_000.0):
        return False
    if obw_hz > 16_000.0 or iq_mod_hint in ("WFM", "AM", "DIGITAL_BURST") or freq_dev_hz >= 2_400.0:
        return False

    rem20 = f % 20_000_000.0
    on_20m_harmonic = min(rem20, 20_000_000.0 - rem20) <= 7_500.0
    on_rp1_clock = any(abs(f - clk) <= 6_000.0 for clk in (250_000_000.0, 350_000_000.0, 500_000_000.0))
    return on_20m_harmonic or on_rp1_clock


@dataclass
class ScanConfig:
    mode: str = "QUICK"                         # "QUICK", "DEEP", "CUSTOM"
    freq_min_mhz: int = 1                       # Defaults to Full Spectrum (1 - 6000 MHz)
    freq_max_mhz: int = 6000                    # Defaults to Full Spectrum (1 - 6000 MHz)
    min_snr_db: float = 9.0                     # Signal strength adjuster 1: Minimum SNR (dB)
    min_peak_dbfs: float = -72.0                # Signal strength adjuster 2: Minimum Peak Power (dBFS)
    demod_mode: str = "AUTO (Band + AI)"        # Demodulation mode override or AUTO
    squelch_mode: str = "OPEN"                  # "OPEN", "AUTO", or "CUSTOM:<db>"
    l1_bin_width_hz: int = 50_000
    l1_sweeps: int = 3
    l1_passes: int = 3                          # Multi-pass wideband temporal sweeps (Mean + Max-Hold)
    l2_bin_width_hz: int = 4_000
    l2_sweeps: int = 3
    l2_passes: int = 2                          # Multi-pass fine-zoom verification sweeps
    l3_frames_per_mhz: int = 24                 # 24 Macro + 24 Ultra-Zoom = 48 frames/pass (>= 3)
    dwell_passes: int = 3                       # Multi-pass high-confidence real-time IQ dwell passes
    iq_capture_s: float = 1.25                  # Seconds of 2.0 MSPS IQ per pass (>= 2.5M samples -> 7.63 Hz/bin!)
    min_scan_duration_s: float = 128.0          # Full-spectrum baseline (>= 2 whole minutes QUICK; 5-10 min DEEP)
    lna_gain_db: int = 24                       # 0..40 step 8
    vga_gain_db: int = 22                       # 0..62 step 2
    amp_enable: int = 0                         # 0 or 1
    only_legal_demod: bool = True
    max_candidates: int = 26
    simulate: bool = False                      # NEVER True on real scans; reserved strictly for unit tests

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mode(
        cls,
        mode: str = "QUICK",
        freq_min_mhz: int = 1,
        freq_max_mhz: int = 6000,
        min_snr_db: float = 9.0,
        min_peak_dbfs: float = -72.0,
        demod_mode: str = "AUTO (Band + AI)",
        squelch_mode: str = "OPEN",
        l3_frames_per_mhz: int = 24,
        lna_gain_db: int = 24,
        vga_gain_db: int = 22,
        amp_enable: int = 0,
        only_legal_demod: bool = True,
        dwell_passes: Optional[int] = None,
        min_scan_duration_s: Optional[float] = None,
    ) -> "ScanConfig":
        norm_mode = (mode or "QUICK").upper().strip()
        f_min = max(1, int(freq_min_mhz))
        f_max = max(f_min + 2, int(freq_max_mhz))
        span = f_max - f_min

        # Quantize HackRF hardware gains to valid register steps
        lna = max(0, min(40, int(round(lna_gain_db / 8.0) * 8)))
        vga = max(0, min(62, int(round(vga_gain_db / 2.0) * 2)))
        amp = 1 if int(amp_enable) > 0 else 0

        if norm_mode == "DEEP":
            l1_bw = 100_000 if span > 1500 else (50_000 if span > 600 else (25_000 if span > 220 else 15_000))
            l1_sw = 4 if span > 1500 else 5
            d_passes = max(1, int(dwell_passes)) if dwell_passes is not None else 5
            if min_scan_duration_s is not None:
                min_dur = float(min_scan_duration_s)
            elif span >= 2000:
                min_dur = 340.0  # 5.7 - 8.5 minutes Deep Full-Spectrum thorough survey
            elif span >= 400:
                min_dur = 150.0
            else:
                min_dur = 0.0
            return cls(
                mode="DEEP",
                freq_min_mhz=f_min,
                freq_max_mhz=f_max,
                min_snr_db=float(min_snr_db),
                min_peak_dbfs=float(min_peak_dbfs),
                demod_mode=demod_mode,
                squelch_mode=squelch_mode,
                l1_bin_width_hz=l1_bw,
                l1_sweeps=l1_sw,
                l1_passes=5 if span >= 400 else 3,
                l2_bin_width_hz=3_000,
                l2_sweeps=4,
                l2_passes=3,
                l3_frames_per_mhz=max(5, int(l3_frames_per_mhz) if l3_frames_per_mhz != 24 else 36),
                dwell_passes=d_passes,
                iq_capture_s=2.00,
                min_scan_duration_s=min_dur,
                lna_gain_db=lna,
                vga_gain_db=vga,
                amp_enable=amp,
                only_legal_demod=only_legal_demod,
                max_candidates=36,
            )
        if norm_mode == "CUSTOM":
            l1_bw = 200_000 if span > 1500 else (100_000 if span > 500 else (50_000 if span > 180 else 25_000))
            l1_sw = 3 if span > 1500 else 4
            d_passes = max(1, int(dwell_passes)) if dwell_passes is not None else 3
            if min_scan_duration_s is not None:
                min_dur = float(min_scan_duration_s)
            elif span >= 2000 and d_passes >= 2:
                min_dur = 128.0
            else:
                min_dur = 0.0
            return cls(
                mode="CUSTOM",
                freq_min_mhz=f_min,
                freq_max_mhz=f_max,
                min_snr_db=float(min_snr_db),
                min_peak_dbfs=float(min_peak_dbfs),
                demod_mode=demod_mode,
                squelch_mode=squelch_mode,
                l1_bin_width_hz=l1_bw,
                l1_sweeps=l1_sw,
                l1_passes=max(2, min(5, d_passes)),
                l2_bin_width_hz=4_000,
                l2_sweeps=3,
                l2_passes=2,
                l3_frames_per_mhz=max(3, int(l3_frames_per_mhz)),
                dwell_passes=d_passes,
                iq_capture_s=1.30,
                min_scan_duration_s=min_dur,
                lna_gain_db=lna,
                vga_gain_db=vga,
                amp_enable=amp,
                only_legal_demod=only_legal_demod,
                max_candidates=30,
            )
        # Default: QUICK (Standard Thorough Full-Spectrum >= 2 Whole Minutes!)
        l1_bw_q = 150_000 if span > 1200 else (75_000 if span > 220 else 25_000)
        d_passes_q = max(1, int(dwell_passes)) if dwell_passes is not None else 3
        if min_scan_duration_s is not None:
            min_dur_q = float(min_scan_duration_s)
        elif span >= 2000:
            min_dur_q = 130.0  # >= 2 minutes 10 seconds full-spectrum multi-pass + dwell
        elif span >= 400:
            min_dur_q = 65.0
        else:
            min_dur_q = 0.0
        return cls(
            mode="QUICK",
            freq_min_mhz=f_min,
            freq_max_mhz=f_max,
            min_snr_db=float(min_snr_db),
            min_peak_dbfs=float(min_peak_dbfs),
            demod_mode=demod_mode,
            squelch_mode=squelch_mode,
            l1_bin_width_hz=l1_bw_q,
            l1_sweeps=3,
            l1_passes=3 if span >= 200 else 2,
            l2_bin_width_hz=4_000,
            l2_sweeps=3,
            l2_passes=2,
            l3_frames_per_mhz=max(3, int(l3_frames_per_mhz) if l3_frames_per_mhz >= 3 else 24),
            dwell_passes=d_passes_q,
            iq_capture_s=1.25,
            min_scan_duration_s=min_dur_q,
            lna_gain_db=lna,
            vga_gain_db=vga,
            amp_enable=amp,
            only_legal_demod=only_legal_demod,
            max_candidates=26,
        )


@dataclass
class DetectedSignalSpike:
    center_freq_hz: float
    center_freq_mhz: float
    raw_measured_hz: float
    carrier_offset_hz: float
    peak_dbm: float             # Calibrated dBFS power
    noise_floor_dbm: float      # Local 20th-percentile baseline floor dBFS
    snr_db: float
    estimated_bandwidth_hz: float
    layer_stage: int            # 1, 2, or 3
    iq_mod_hint: str = "NFM"
    duty_cycle: float = 1.0
    num_pictogram_frames: int = 0
    pictogram_frames: List[np.ndarray] = field(default_factory=list)
    raw_samples_info: Dict[str, Any] = field(default_factory=dict)


class HackRFScannerEngine:
    """
    Hardware RF acquisition & DSP engine for HackRF One.
    Never injects fake/synthetic signals during real hardware scans.
    """

    def __init__(self, serial_number: Optional[str] = None) -> None:
        self.serial_number = serial_number

    @staticmethod
    def _exec_hackrf_cli(
        cmd: List[str],
        timeout_s: float,
        stop_check: Optional[Callable[[], bool]] = None,
    ) -> Tuple[int, str, str, bool]:
        """
        Executes hackrf_sweep / hackrf_transfer with responsive abort checking and graceful
        SIGINT shutdown so libhackrf always runs hackrf_stop_rx() + hackrf_close() cleanly.
        Returns (returncode, stdout, stderr, was_aborted).
        """
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + max(2.0, float(timeout_s))
        aborted = False
        timed_out = False

        while proc.poll() is None:
            if stop_check is not None and stop_check():
                aborted = True
                break
            if time.monotonic() >= deadline:
                timed_out = True
                break
            time.sleep(0.06)

        if proc.poll() is None:
            try:
                proc.send_signal(signal.SIGINT)
                out, err = proc.communicate(timeout=0.9)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    out, err = proc.communicate(timeout=0.4)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    out, err = proc.communicate()
            if timed_out and not aborted:
                raise TimeoutError(f"{cmd[0]} timed out after {timeout_s:.1f}s")
            return proc.returncode or 0, out or "", err or "", aborted

        out, err = proc.communicate()
        return proc.returncode or 0, out or "", err or "", False

    def _run_hackrf_sweep_csv_stats(
        self,
        f_min_mhz: int,
        f_max_mhz: int,
        bin_width_hz: int,
        num_sweeps: int,
        lna_gain_db: int,
        vga_gain_db: int,
        amp_enable: int,
        timeout_s: float = 20.0,
        stop_check: Optional[Callable[[], bool]] = None,
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """
        Executes hackrf_sweep and computes vectorized (frequencies_hz, mean_dbfs, max_hold_dbfs)
        across num_sweeps with automatic USB bulk-endpoint recovery.
        """
        f_min_mhz = max(1, int(f_min_mhz))
        f_max_mhz = max(f_min_mhz + 1, int(f_max_mhz))
        bin_w_clamped = max(3_000, min(1_000_000, int(bin_width_hz)))
        f_min_hz = f_min_mhz * 1_000_000.0
        f_max_hz = f_max_mhz * 1_000_000.0

        last_err_txt = ""
        for attempt in range(2):
            if stop_check is not None and stop_check():
                empty_f = np.array([], dtype=np.float64)
                empty_p = np.array([], dtype=np.float32)
                return empty_f, empty_p, empty_p

            with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
                tmp_path = tmp.name

            try:
                cmd = [
                    "hackrf_sweep",
                    "-f", f"{f_min_mhz}:{f_max_mhz}",
                    "-N", str(max(1, int(num_sweeps))),
                    "-w", str(bin_w_clamped),
                    "-P", "estimate",
                    "-l", str(lna_gain_db),
                    "-g", str(vga_gain_db),
                    "-a", str(1 if amp_enable else 0),
                    "-r", tmp_path,
                ]
                if self.serial_number:
                    cmd.extend(["-d", self.serial_number])

                try:
                    _, out_s, err_s, aborted = self._exec_hackrf_cli(
                        cmd,
                        timeout_s=timeout_s,
                        stop_check=stop_check,
                    )
                except TimeoutError as t_exc:
                    out_s, err_s, aborted = "", str(t_exc), False

                if aborted:
                    empty_f = np.array([], dtype=np.float64)
                    empty_p = np.array([], dtype=np.float32)
                    return empty_f, empty_p, empty_p

                block_accum: Dict[Tuple[int, int, int], List[Any]] = {}
                if os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 0:
                    with open(tmp_path, "r", encoding="utf-8", errors="ignore") as f:
                        for line in f:
                            parts = line.split(",", 6)
                            if len(parts) < 7:
                                continue
                            try:
                                hz_low = int(round(float(parts[2])))
                                hz_high = int(round(float(parts[3])))
                                vals = np.fromstring(parts[6], sep=",", dtype=np.float32)
                                n_bins = len(vals)
                                if n_bins < 1 or not np.all(np.isfinite(vals)):
                                    continue
                                bkey = (hz_low, hz_high, n_bins)
                                entry = block_accum.get(bkey)
                                if entry is None:
                                    block_accum[bkey] = [vals.astype(np.float64), vals.copy(), 1]
                                else:
                                    entry[0] += vals
                                    np.maximum(entry[1], vals, out=entry[1])
                                    entry[2] += 1
                            except Exception:
                                continue

                if block_accum:
                    f_chunks: List[np.ndarray] = []
                    mean_chunks: List[np.ndarray] = []
                    max_chunks: List[np.ndarray] = []
                    for (hz_low, hz_high, n_bins), (sum_v, max_v, cnt) in sorted(block_accum.items()):
                        step_hz = (hz_high - hz_low) / float(n_bins)
                        f_vec = hz_low + (np.arange(n_bins, dtype=np.float64) + 0.5) * step_hz
                        mask = (f_vec >= f_min_hz) & (f_vec <= f_max_hz)
                        if np.any(mask):
                            f_chunks.append(f_vec[mask])
                            mean_chunks.append((sum_v[mask] / float(cnt)).astype(np.float32))
                            max_chunks.append(max_v[mask].astype(np.float32))

                    if f_chunks:
                        all_f = np.concatenate(f_chunks)
                        all_mean = np.concatenate(mean_chunks)
                        all_max = np.concatenate(max_chunks)
                        order = np.argsort(all_f)
                        return all_f[order], all_mean[order], all_max[order]

                last_err_txt = (err_s + " " + out_s).strip()
                if attempt == 0 and (stop_check is None or not stop_check()):
                    logger.warning(
                        "hackrf_sweep (%d-%d MHz) returned empty (%s); running automatic USB port reset & retry!",
                        f_min_mhz,
                        f_max_mhz,
                        last_err_txt[:120],
                    )
                    get_sdr_arbiter().reset_hackrf_usb_port(stop_gqrx=False)
                    time.sleep(0.15)
            finally:
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass

        raise RuntimeError(
            f"HackRF sweep ({f_min_mhz}-{f_max_mhz} MHz) returned no RF data. Hardware diagnostic: {last_err_txt[:180]}"
        )

    def _run_hackrf_sweep_csv(
        self,
        f_min_mhz: int,
        f_max_mhz: int,
        bin_width_hz: int,
        num_sweeps: int,
        lna_gain_db: int,
        vga_gain_db: int,
        amp_enable: int,
        timeout_s: float = 15.0,
        stop_check: Optional[Callable[[], bool]] = None,
    ) -> Tuple[np.ndarray, np.ndarray]:
        """Backwards-compatible wrapper returning (freqs, mean_powers)."""
        freqs, mean_p, _ = self._run_hackrf_sweep_csv_stats(
            f_min_mhz=f_min_mhz,
            f_max_mhz=f_max_mhz,
            bin_width_hz=bin_width_hz,
            num_sweeps=num_sweeps,
            lna_gain_db=lna_gain_db,
            vga_gain_db=vga_gain_db,
            amp_enable=amp_enable,
            timeout_s=timeout_s,
            stop_check=stop_check,
        )
        return freqs, mean_p

    @staticmethod
    def _build_wideband_segments(config: ScanConfig) -> List[Tuple[int, int, int]]:
        """
        Partitions [freq_min_mhz, freq_max_mhz] into high-resolution RF sub-bands so
        narrowband HF/CB/VHF/UHF voice & telemetry bands (1 - 960 MHz) are swept with fine
        15-25 kHz bins even during full 1 - 6000 MHz multi-GHz surveys!
        """
        lo = int(config.freq_min_mhz)
        hi = int(config.freq_max_mhz)
        span = hi - lo
        if span <= 260:
            fine_bw = min(int(config.l1_bin_width_hz), 15_000 if config.mode == "DEEP" else 20_000)
            return [(lo, hi, max(5_000, fine_bw))]

        # Sub-band definitions: (band_lo_mhz, band_hi_mhz, target_bin_hz_quick, target_bin_hz_deep)
        subband_defs = (
            (1, 88, 15_000, 10_000),       # HF / CB 27 MHz / 6m / VHF-Lo TV
            (88, 225, 15_000, 10_000),     # FM Broadcast, Airband, 2m, Sheriff/Fire/SAR/NOAA/Rail, VHF-Hi TV
            (225, 620, 20_000, 15_000),    # MilAir, 315/433 ISM, 70cm, UHF LMR/FRS/GMRS, UHF TV Ch 14-36
            (620, 1300, 35_000, 25_000),   # 700/800 P25, 902-928 ISM/LoRa, 929 Pager, 978 UAT, 1090 ADS-B
            (1300, 3000, 75_000, 50_000),  # L-Band WX-Sat / GPS / 2.4 GHz ISM
            (3000, 6000, 125_000, 80_000), # S/C-Band 3.5 GHz / 5.8 GHz ISM & FPV
        )
        segs: List[Tuple[int, int, int]] = []
        for s_lo, s_hi, bw_q, bw_d in subband_defs:
            c_lo = max(lo, s_lo)
            c_hi = min(hi, s_hi)
            if c_hi - c_lo >= 2:
                bw = bw_d if config.mode == "DEEP" else bw_q
                segs.append((c_lo, c_hi, bw))
        return segs or [(lo, hi, int(config.l1_bin_width_hz))]

    # ------------------------------------------------------------------
    # LAYER 1: Multi-Pass Segmented Wideband Spectrum Sweep
    # ------------------------------------------------------------------
    def sweep_wideband(
        self,
        config: ScanConfig,
        region: str = "US",
        stop_check: Optional[Callable[[], bool]] = None,
        progress_cb: Optional[Callable[[int, int, int, int, int, int, np.ndarray, np.ndarray, np.ndarray, int], None]] = None,
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray, List[DetectedSignalSpike]]:
        """
        Layer 1: Multi-pass band-segmented wideband spectrum survey.
        Executes config.l1_passes full temporal passes across fine-resolution RF sub-bands,
        tracking both Mean thermal baseline and Max-Hold burst/PTT peaks so intermittent
        voice & packet transmissions are reliably captured.
        """
        if config.simulate:
            return self._simulate_wideband(config, region)

        t0 = time.perf_counter()
        segments = self._build_wideband_segments(config)
        total_passes = max(1, int(getattr(config, "l1_passes", 3)))
        total_segs = len(segments)

        seg_freqs: List[Optional[np.ndarray]] = [None] * total_segs
        seg_mean_sum: List[Optional[np.ndarray]] = [None] * total_segs
        seg_max_hold: List[Optional[np.ndarray]] = [None] * total_segs
        seg_pass_cnt: List[int] = [0] * total_segs

        for pass_idx in range(total_passes):
            for seg_idx, (s_lo, s_hi, s_bw) in enumerate(segments):
                if stop_check is not None and stop_check():
                    return np.array([], dtype=np.float64), np.array([], dtype=np.float32), np.array([], dtype=np.float32), []

                seg_span = max(1, s_hi - s_lo)
                seg_timeout = max(10.0, min(35.0, seg_span * 0.014 * config.l1_sweeps + 8.0))
                f_arr, mean_arr, max_arr = self._run_hackrf_sweep_csv_stats(
                    f_min_mhz=s_lo,
                    f_max_mhz=s_hi,
                    bin_width_hz=s_bw,
                    num_sweeps=config.l1_sweeps,
                    lna_gain_db=config.lna_gain_db,
                    vga_gain_db=config.vga_gain_db,
                    amp_enable=config.amp_enable,
                    timeout_s=seg_timeout,
                    stop_check=stop_check,
                )
                if len(f_arr) == 0:
                    continue

                if seg_freqs[seg_idx] is None or len(seg_freqs[seg_idx]) != len(f_arr):
                    seg_freqs[seg_idx] = f_arr
                    seg_mean_sum[seg_idx] = mean_arr.astype(np.float64)
                    seg_max_hold[seg_idx] = max_arr.copy()
                    seg_pass_cnt[seg_idx] = 1
                else:
                    seg_mean_sum[seg_idx] += mean_arr
                    np.maximum(seg_max_hold[seg_idx], max_arr, out=seg_max_hold[seg_idx])
                    seg_pass_cnt[seg_idx] += 1

                if progress_cb is not None:
                    # Emit live stitched display spectrum (downsampled to <= 4096 points for 60 FPS GUI smoothness)
                    disp_f_list = []
                    disp_p_list = []
                    disp_fl_list = []
                    for k in range(total_segs):
                        if seg_freqs[k] is not None and seg_pass_cnt[k] > 0:
                            fk = seg_freqs[k]
                            mk = (seg_mean_sum[k] / float(seg_pass_cnt[k])).astype(np.float32)
                            xk = seg_max_hold[k]
                            pk = (0.35 * mk + 0.65 * xk).astype(np.float32)
                            step_ds = max(1, len(fk) // 800)
                            disp_f_list.append(fk[::step_ds])
                            disp_p_list.append(pk[::step_ds])
                            fl_approx = np.full(len(fk[::step_ds]), float(np.percentile(mk, 20)), dtype=np.float32)
                            disp_fl_list.append(fl_approx)
                    if disp_f_list:
                        progress_cb(
                            pass_idx + 1,
                            total_passes,
                            seg_idx + 1,
                            total_segs,
                            s_lo,
                            s_hi,
                            np.concatenate(disp_f_list),
                            np.concatenate(disp_p_list),
                            np.concatenate(disp_fl_list),
                            sum(len(x) for x in seg_freqs if x is not None),
                        )

        all_freqs_list: List[np.ndarray] = []
        all_powers_list: List[np.ndarray] = []
        all_floors_list: List[np.ndarray] = []
        all_spikes: List[DetectedSignalSpike] = []

        for seg_idx, (s_lo, s_hi, s_bw) in enumerate(segments):
            if seg_freqs[seg_idx] is None or seg_pass_cnt[seg_idx] == 0:
                continue
            f_seg = seg_freqs[seg_idx]
            mean_seg = (seg_mean_sum[seg_idx] / float(seg_pass_cnt[seg_idx])).astype(np.float32)
            max_seg = seg_max_hold[seg_idx]
            # Composite detection envelope: preserves smooth thermal floor while boosting active PTT/burst peaks!
            blended_seg = np.where(
                (max_seg - mean_seg) >= 4.5,
                0.30 * mean_seg + 0.70 * max_seg,
                0.65 * mean_seg + 0.35 * max_seg,
            ).astype(np.float32)

            fl_seg, spk_seg = self.extract_prominent_spikes(
                freqs=f_seg,
                powers=blended_seg,
                bin_width_hz=float(s_bw),
                min_snr_db=config.min_snr_db,
                min_peak_dbfs=config.min_peak_dbfs,
                layer_stage=1,
                region=region,
                f_min_mhz=s_lo,
            )
            all_freqs_list.append(f_seg)
            all_powers_list.append(blended_seg)
            all_floors_list.append(fl_seg)
            all_spikes.extend(spk_seg)

        if not all_freqs_list:
            return np.array([], dtype=np.float64), np.array([], dtype=np.float32), np.array([], dtype=np.float32), []

        freqs = np.concatenate(all_freqs_list)
        powers = np.concatenate(all_powers_list)
        noise_floor = np.concatenate(all_floors_list)
        spikes = self._deduplicate_by_band_spacing(all_spikes, region=region)

        elapsed = time.perf_counter() - t0
        logger.info(
            "Layer 1 multi-pass sweep (%d-%d MHz, %d passes x %d segs, %d total bins) -> %d spikes in %.2fs",
            config.freq_min_mhz,
            config.freq_max_mhz,
            total_passes,
            total_segs,
            len(freqs),
            len(spikes),
            elapsed,
        )
        return freqs, powers, noise_floor, spikes

    @staticmethod
    def extract_prominent_spikes(
        freqs: np.ndarray,
        powers: np.ndarray,
        bin_width_hz: float,
        min_snr_db: float,
        min_peak_dbfs: float,
        layer_stage: int,
        region: str = "US",
        f_min_mhz: int = 88,
    ) -> Tuple[np.ndarray, List[DetectedSignalSpike]]:
        """
        Computes a true wide-window 20th-percentile baseline noise floor (6 MHz span)
        and extracts distinct signal carriers via prominence & channel-aware peak spacing.
        """
        n = len(powers)
        if n < 8:
            return np.full_like(powers, -85.0), []

        eff_bin_hz = float(np.median(np.diff(freqs))) if n > 2 else float(bin_width_hz)
        eff_bin_hz = max(1_000.0, eff_bin_hz)

        # 6 MHz sliding 20th-percentile window tracks unoccupied thermal noise even across dense FM broadcast
        win_bins = max(25, int(round(6_500_000.0 / eff_bin_hz)) | 1)
        win_bins = min(max(15, (n // 2) | 1), win_bins) if n < win_bins else win_bins
        raw_floor = percentile_filter(powers, percentile=20, size=win_bins, mode="nearest")
        noise_floor = uniform_filter1d(raw_floor, size=max(5, win_bins // 6), mode="nearest").astype(np.float32)

        # Light 3-tap smoothing for peak center stability without smearing narrow carriers
        smooth_pwr = np.convolve(powers, np.array([0.22, 0.56, 0.22], dtype=np.float32), mode="same")
        smooth_pwr[0], smooth_pwr[-1] = powers[0], powers[-1]

        snr_trace = smooth_pwr - noise_floor
        min_dist_bins = max(2, int(round(35_000.0 / eff_bin_hz)))
        prominence_req = max(3.2, min_snr_db * 0.42)

        peak_indices, _ = find_peaks(
            snr_trace,
            height=min_snr_db,
            prominence=prominence_req,
            distance=min_dist_bins,
        )

        raw_candidates: List[DetectedSignalSpike] = []
        base_hz = f_min_mhz * 1_000_000.0

        for idx in peak_indices:
            peak_p = float(np.max(powers[max(0, idx - 1) : min(n, idx + 2)]))
            floor_p = float(noise_floor[idx])
            snr_val = peak_p - floor_p

            if snr_val < min_snr_db or peak_p < min_peak_dbfs:
                continue

            # Parabolic 3-point sub-bin frequency interpolation
            cf_hz = float(freqs[idx])
            if 0 < idx < n - 1:
                y0, y1, y2 = float(powers[idx - 1]), float(powers[idx]), float(powers[idx + 1])
                denom = (y0 - 2.0 * y1 + y2)
                if abs(denom) > 1e-4:
                    delta = max(-0.5, min(0.5, 0.5 * (y0 - y2) / denom))
                    cf_hz += delta * eff_bin_hz

            # Estimate -6 dB bandwidth around peak
            half_thresh = max(floor_p + min_snr_db * 0.45, peak_p - 6.5)
            lo_i = idx
            while lo_i > 0 and powers[lo_i - 1] >= half_thresh and (idx - lo_i) < int(350_000 / eff_bin_hz) + 2:
                lo_i -= 1
            hi_i = idx
            while hi_i < n - 1 and powers[hi_i + 1] >= half_thresh and (hi_i - idx) < int(350_000 / eff_bin_hz) + 2:
                hi_i += 1
            width_bins = max(1, hi_i - lo_i + 1)
            est_bw = float(width_bins * eff_bin_hz)

            # Filter out single-bin hackrf_sweep 5 MHz LO interleave ticks and razor-thin Si5351 20 MHz / RP1 clock needles
            rel_mod = (cf_hz - base_hz) % 5_000_000.0
            near_lo_step = min(rel_mod, 5_000_000.0 - rel_mod) <= eff_bin_hz * 1.1
            if near_lo_step and width_bins <= 1 and snr_val < min_snr_db + 7.0:
                continue
            rem20 = cf_hz % 20_000_000.0
            near_20m_clk = min(rem20, 20_000_000.0 - rem20) <= max(12_000.0, eff_bin_hz * 0.85)
            near_rp1_clk = any(abs(cf_hz - clk) <= max(12_000.0, eff_bin_hz * 0.85) for clk in (250_000_000.0, 350_000_000.0, 500_000_000.0))
            if (near_20m_clk or near_rp1_clk) and width_bins <= 1 and not (87.8e6 <= cf_hz <= 108.1e6):
                continue

            raw_candidates.append(
                DetectedSignalSpike(
                    center_freq_hz=cf_hz,
                    center_freq_mhz=round(cf_hz / 1e6, 4),
                    raw_measured_hz=cf_hz,
                    carrier_offset_hz=0.0,
                    peak_dbm=round(peak_p, 1),
                    noise_floor_dbm=round(floor_p, 1),
                    snr_db=round(snr_val, 1),
                    estimated_bandwidth_hz=round(est_bw, 0),
                    layer_stage=layer_stage,
                )
            )

        # Band-aware deduplication (e.g., prevent stereo sidebands on FM 88-108 MHz from creating twin spikes)
        deduped = HackRFScannerEngine._deduplicate_by_band_spacing(raw_candidates, region=region)
        return noise_floor, deduped

    @staticmethod
    def _atsc_channel_key(freq_hz: float, region: str = "US") -> Optional[int]:
        """Returns integer ATSC 6 MHz channel number if freq_hz lies inside a US broadcast TV band."""
        if region not in ("US", "CA", "MX"):
            return None
        f = float(freq_hz)
        if 54_000_000 <= f < 72_000_000:
            return 2 + int((f - 54_000_000) // 6_000_000)
        if 76_000_000 <= f < 87_800_000:
            return 5 + int((f - 76_000_000) // 6_000_000)
        if 174_000_000 <= f < 216_000_000:
            return 7 + int((f - 174_000_000) // 6_000_000)
        if 470_000_000 <= f < 608_000_000:
            return 14 + int((f - 470_000_000) // 6_000_000)
        return None

    @staticmethod
    def _deduplicate_by_band_spacing(
        spikes: List[DetectedSignalSpike],
        region: str = "US",
    ) -> List[DetectedSignalSpike]:
        if not spikes:
            return []

        # Process strongest spikes first so sidebands/shelf ripples merge into true main carrier/pilot
        by_strength = sorted(spikes, key=lambda s: s.peak_dbm, reverse=True)
        kept: List[DetectedSignalSpike] = []
        kept_tv_channels: set[int] = set()

        for cand in by_strength:
            tv_ch = HackRFScannerEngine._atsc_channel_key(cand.center_freq_hz, region=region)
            if tv_ch is not None:
                if tv_ch in kept_tv_channels:
                    continue
                kept_tv_channels.add(tv_ch)
                kept.append(cand)
                continue

            if 87_800_000 <= cand.center_freq_hz <= 108_100_000:
                min_guard_hz = 165_000.0 if region in ("US", "CA", "MX") else 85_000.0
            elif 118_000_000 <= cand.center_freq_hz <= 137_000_000:
                min_guard_hz = 20_000.0
            else:
                min_guard_hz = 11_000.0

            if any(abs(cand.center_freq_hz - k.center_freq_hz) < min_guard_hz for k in kept):
                continue
            kept.append(cand)

        kept.sort(key=lambda s: s.center_freq_hz)
        return kept

    # ------------------------------------------------------------------
    # LAYER 2: Multi-Pass Merged-Window Fine Zoom Refinement & Channel Alignment
    # ------------------------------------------------------------------
    def zoom_refine_candidates(
        self,
        candidates: List[DetectedSignalSpike],
        config: ScanConfig,
        region: str = "US",
        progress_cb: Optional[Callable[[int, int, float], None]] = None,
        stop_check: Optional[Callable[[], bool]] = None,
    ) -> List[DetectedSignalSpike]:
        """
        Layer 2: Groups candidates into offset 4 MHz zoom windows (eliminating Layer 1 LO step boundary
        coincidences), sweeps across config.l2_passes fine-bin passes (3-4 kHz bins) combining Mean +
        Max-Hold power so intermittent PTT/burst signals are retained, refines center frequency &
        bandwidth, and aligns carriers to regulatory channel rasters.
        """
        if not candidates:
            return []
        if config.simulate:
            refined_sim = []
            for c in candidates:
                snapped_hz, off_hz = snap_to_channel_raster(c.center_freq_hz, region)
                c.raw_measured_hz = c.center_freq_hz
                c.center_freq_hz = float(snapped_hz)
                c.center_freq_mhz = round(snapped_hz / 1e6, 4)
                c.carrier_offset_hz = round(off_hz, 1)
                c.layer_stage = 2
                refined_sim.append(c)
            return self._deduplicate_snapped(refined_sim)[: config.max_candidates]

        sorted_cands = sorted(candidates, key=lambda c: c.center_freq_hz)
        windows: List[Tuple[int, int, List[DetectedSignalSpike]]] = []

        for cand in sorted_cands:
            c_mhz = cand.center_freq_hz / 1e6
            w_lo = max(1, int(math.floor(c_mhz - 1.5)))
            w_hi = max(w_lo + 3, int(math.ceil(c_mhz + 1.5)))
            if windows and w_lo <= windows[-1][1] and (w_hi - windows[-1][0]) <= 12:
                prev_lo, prev_hi, grp = windows[-1]
                grp.append(cand)
                windows[-1] = (prev_lo, max(prev_hi, w_hi), grp)
            else:
                windows.append((w_lo, w_hi, [cand]))

        refined: List[DetectedSignalSpike] = []
        total_wins = len(windows)
        l2_passes = max(1, int(getattr(config, "l2_passes", 2)))

        for w_idx, (w_lo, w_hi, grp_cands) in enumerate(windows):
            if stop_check is not None and stop_check():
                return []
            if progress_cb:
                progress_cb(w_idx + 1, total_wins, grp_cands[0].center_freq_mhz)

            sub_freqs: np.ndarray = np.array([], dtype=np.float64)
            mean_acc: Optional[np.ndarray] = None
            max_acc: Optional[np.ndarray] = None
            valid_passes = 0

            for _ in range(l2_passes):
                if stop_check is not None and stop_check():
                    return []
                try:
                    f_p, m_p, x_p = self._run_hackrf_sweep_csv_stats(
                        f_min_mhz=w_lo,
                        f_max_mhz=w_hi,
                        bin_width_hz=config.l2_bin_width_hz,
                        num_sweeps=config.l2_sweeps,
                        lna_gain_db=config.lna_gain_db,
                        vga_gain_db=config.vga_gain_db,
                        amp_enable=config.amp_enable,
                        timeout_s=12.0,
                        stop_check=stop_check,
                    )
                    if len(f_p) >= 8:
                        if mean_acc is None or len(f_p) != len(sub_freqs):
                            sub_freqs = f_p
                            mean_acc = m_p.astype(np.float64)
                            max_acc = x_p.copy()
                            valid_passes = 1
                        else:
                            mean_acc += m_p
                            np.maximum(max_acc, x_p, out=max_acc)
                            valid_passes += 1
                except Exception as exc:
                    logger.warning("Layer 2 zoom window %d-%d MHz note: %s", w_lo, w_hi, exc)

            if valid_passes == 0 or mean_acc is None or max_acc is None or len(sub_freqs) < 8:
                continue

            mean_pwrs = (mean_acc / float(valid_passes)).astype(np.float32)
            sub_powers = np.where(
                (max_acc - mean_pwrs) >= 4.0,
                0.30 * mean_pwrs + 0.70 * max_acc,
                0.65 * mean_pwrs + 0.35 * max_acc,
            ).astype(np.float32)

            local_floor = float(np.percentile(mean_pwrs, 20))
            eff_bin = float(np.median(np.diff(sub_freqs))) if len(sub_freqs) > 2 else float(config.l2_bin_width_hz)

            for cand in grp_cands:
                search_rad = 110_000.0 if (87.8e6 <= cand.center_freq_hz <= 108.1e6) else 65_000.0
                mask = (sub_freqs >= cand.center_freq_hz - search_rad) & (sub_freqs <= cand.center_freq_hz + search_rad)
                if not np.any(mask):
                    continue

                w_freqs = sub_freqs[mask]
                w_pwrs = sub_powers[mask]
                p_idx = int(np.argmax(w_pwrs))
                ref_peak = float(w_pwrs[p_idx])
                ref_snr = ref_peak - local_floor

                # Retain strong Layer 1 burst detections if intermittent during Layer 2, while rejecting noise spurs
                if ref_snr < max(4.8, config.min_snr_db - 2.2) or ref_peak < (config.min_peak_dbfs - 2.5):
                    if cand.snr_db >= config.min_snr_db + 6.0 and cand.estimated_bandwidth_hz >= 15_000.0:
                        ref_peak = cand.peak_dbm
                        ref_snr = cand.snr_db
                        raw_center_hz = cand.center_freq_hz
                        ref_bw = cand.estimated_bandwidth_hz
                    else:
                        continue
                else:
                    top_mask = w_pwrs >= (ref_peak - 4.0)
                    if np.sum(top_mask) >= 2:
                        lin_weights = 10.0 ** ((w_pwrs[top_mask] - ref_peak) / 10.0)
                        raw_center_hz = float(np.sum(w_freqs[top_mask] * lin_weights) / np.sum(lin_weights))
                    else:
                        raw_center_hz = float(w_freqs[p_idx])

                    bw_mask = w_pwrs >= max(local_floor + 4.0, ref_peak - 6.0)
                    above_f = w_freqs[bw_mask]
                    if len(above_f) >= 2:
                        ref_bw = float(np.max(above_f) - np.min(above_f)) + eff_bin
                    else:
                        ref_bw = max(eff_bin, cand.estimated_bandwidth_hz)

                    if len(above_f) <= 1 and ref_snr < config.min_snr_db + 3.0:
                        continue
                    if len(above_f) <= 1 and is_likely_internal_clock_spur(raw_center_hz, ref_bw, "CW", 0.0):
                        continue

                snapped_hz, offset_hz = snap_to_channel_raster(raw_center_hz, region=region)
                refined.append(
                    DetectedSignalSpike(
                        center_freq_hz=float(snapped_hz),
                        center_freq_mhz=round(snapped_hz / 1e6, 4),
                        raw_measured_hz=round(raw_center_hz, 1),
                        carrier_offset_hz=round(offset_hz, 1),
                        peak_dbm=round(ref_peak, 1),
                        noise_floor_dbm=round(local_floor, 1),
                        snr_db=round(ref_snr, 1),
                        estimated_bandwidth_hz=round(max(6_000.0, min(260_000.0, ref_bw)), 0),
                        layer_stage=2,
                    )
                )

        deduped = self._deduplicate_snapped(refined)
        if len(deduped) > config.max_candidates:
            deduped = sorted(deduped, key=lambda s: s.snr_db, reverse=True)[: config.max_candidates]
            deduped.sort(key=lambda s: s.center_freq_hz)
        return deduped

    @staticmethod
    def _deduplicate_snapped(spikes: List[DetectedSignalSpike]) -> List[DetectedSignalSpike]:
        by_freq: Dict[int, DetectedSignalSpike] = {}
        for s in spikes:
            key = int(round(s.center_freq_hz))
            if key not in by_freq or s.snr_db > by_freq[key].snr_db:
                by_freq[key] = s
        ordered = sorted(by_freq.values(), key=lambda x: x.center_freq_hz)
        return HackRFScannerEngine._deduplicate_by_band_spacing(ordered)

    # ------------------------------------------------------------------
    # LAYER 3 & DWELL: 1,024x Ultra-Zoom IQ Capture (7.63 Hz/bin + Dual-Scale Waterfalls)
    # ------------------------------------------------------------------
    def capture_1mhz_pictogram_frames(
        self,
        center_freq_hz: float,
        num_frames: int = 24,
        rows_per_frame: int = 64,
        cols_per_frame: int = 128,
        lna_gain_db: int = 24,
        vga_gain_db: int = 22,
        amp_enable: int = 0,
        simulate: bool = False,
        stop_check: Optional[Callable[[], bool]] = None,
        capture_duration_s: Optional[float] = None,
    ) -> Tuple[List[np.ndarray], Dict[str, Any]]:
        """
        Layer 3 & Multi-Pass Dwell Acquisition Engine:
          1. Captures continuous 2.000 MSPS complex IQ via hackrf_transfer with ±250 kHz DC-avoidance LO offset.
          2. Computes a 262,144-pt Hann Welch Precision PSD (7.629 Hz/bin — 1,024x finer spectral resolution
             than standard 7,812.5 Hz/bin!), locking true carrier center to ±3.8 Hz, measuring carrier spine
             tonality, sideband symmetry, FSK Mark/Space rail spacing, 19.000 kHz WFM Stereo Pilot SNR, and
             TIA-603 CTCSS subaudible PL tones (67.0 - 254.1 Hz).
          3. Extracts High-Definition 1.000 MHz Macro STFT frames (1024-pt FFT = 1,953 Hz/bin pooled to 64x128)
             PLUS Ultra-Zoom Micro STFT frames (64x FIR decimated 31.25 kHz span @ 244.14 Hz/bin for narrowband;
             8x FIR decimated 125/250 kHz span @ 976.6/1,953 Hz/bin for WFM Stereo MPX) in iq_metrics["zoom_frames"].
        """
        target_frames = max(3, int(num_frames))
        if simulate:
            return self._simulate_iq_frames(center_freq_hz, target_frames, rows_per_frame, cols_per_frame)

        sample_rate = 2_000_000
        capture_center_hz = float(center_freq_hz)
        tv_ch = self._atsc_channel_key(center_freq_hz)
        tv_lo = 0.0
        if tv_ch is not None:
            if 2 <= tv_ch <= 4:
                tv_lo = 54_000_000.0 + (tv_ch - 2) * 6_000_000.0
            elif 5 <= tv_ch <= 6:
                tv_lo = 76_000_000.0 + (tv_ch - 5) * 6_000_000.0
            elif 7 <= tv_ch <= 13:
                tv_lo = 174_000_000.0 + (tv_ch - 7) * 6_000_000.0
            else:
                tv_lo = 470_000_000.0 + (tv_ch - 14) * 6_000_000.0
            capture_center_hz = tv_lo + 350_000.0

        # For low-HF (< 1.5 MHz), place LO +250 kHz above target so LO >= 1.25 MHz (within HackRF 1 MHz min limit)
        if capture_center_hz < 1_500_000.0:
            signed_target_offset_hz = -250_000
            lo_freq_hz = max(1_000_000, int(round(capture_center_hz + 250_000)))
            c_bin_start_1024, c_bin_end_1024 = 128, 640
        else:
            signed_target_offset_hz = 250_000
            lo_freq_hz = max(1_000_000, int(round(capture_center_hz - 250_000)))
            c_bin_start_1024, c_bin_end_1024 = 384, 896

        if capture_duration_s is not None and capture_duration_s > 0.0:
            total_samples = max(524_288, int(round(float(capture_duration_s) * sample_rate / 8192.0)) * 8192)
        elif target_frames >= 12:
            # Multi-frame deep/standard inspection: >= 1.25s (2,500,000 complex samples) for 7.63 Hz/bin + 24+ zoom frames
            total_samples = max(2_490_368, target_frames * 102_400)
        else:
            # Fast single-shot mode (still >= 524,288 samples so 262,144-pt 7.63 Hz/bin FFT always executes!)
            total_samples = max(524_288, target_frames * 131_072)

        xfer_timeout = max(7.0, (total_samples / float(sample_rate)) * 2.5 + 5.0)
        raw_bytes: Optional[np.ndarray] = None
        last_err_txt = ""
        for attempt in range(2):
            if stop_check is not None and stop_check():
                return [], {}

            with tempfile.NamedTemporaryFile(suffix=".iq8", delete=False) as tmp:
                iq_path = tmp.name

            try:
                cmd = [
                    "hackrf_transfer",
                    "-r", iq_path,
                    "-f", str(lo_freq_hz),
                    "-s", str(sample_rate),
                    "-b", "1750000",
                    "-l", str(lna_gain_db),
                    "-g", str(vga_gain_db),
                    "-a", str(1 if amp_enable else 0),
                    "-n", str(total_samples),
                ]
                if self.serial_number:
                    cmd.extend(["-d", self.serial_number])

                try:
                    _, out_s, err_s, aborted = self._exec_hackrf_cli(
                        cmd,
                        timeout_s=xfer_timeout,
                        stop_check=stop_check,
                    )
                except TimeoutError as t_exc:
                    out_s, err_s, aborted = "", str(t_exc), False

                time.sleep(0.035)

                if aborted:
                    return [], {}

                if os.path.exists(iq_path) and os.path.getsize(iq_path) >= 65_536:
                    raw_bytes = np.fromfile(iq_path, dtype=np.int8)
                    break

                last_err_txt = (err_s + " " + out_s).strip()
                if attempt == 0 and (stop_check is None or not stop_check()):
                    logger.warning(
                        "hackrf_transfer at %.4f MHz failed (%s); running automatic USB port reset & retry!",
                        center_freq_hz / 1e6,
                        last_err_txt[:120],
                    )
                    get_sdr_arbiter().reset_hackrf_usb_port(stop_gqrx=False)
                    time.sleep(0.15)
            finally:
                if os.path.exists(iq_path):
                    try:
                        os.remove(iq_path)
                    except Exception:
                        pass

        if raw_bytes is None or len(raw_bytes) < 65_536:
            raise RuntimeError(
                f"Layer 3 IQ capture failed at {center_freq_hz / 1e6:.4f} MHz: {last_err_txt[:180]}"
            )

        n_iq = len(raw_bytes) // 2
        raw_f = raw_bytes[: n_iq * 2].astype(np.float32) / 128.0
        iq = raw_f[0::2] + 1j * raw_f[1::2]
        iq = iq - np.mean(iq)

        # High-Definition 1.000 MHz Macro STFT (nfft = 1024 -> 1,953.1 Hz/bin, 4x finer than legacy 256-pt!)
        nfft_macro = 1024
        win_macro = np.hanning(nfft_macro).astype(np.float32)
        win_macro_sum = float(np.sum(win_macro))

        frames: List[np.ndarray] = []
        seg_len = max(nfft_macro * 8, min(n_iq, int(n_iq / max(1.0, target_frames * 0.55))))
        seg_starts = (
            np.linspace(0, max(0, n_iq - seg_len), target_frames, dtype=np.int64)
            if n_iq > seg_len
            else np.zeros(target_frames, dtype=np.int64)
        )

        for f_idx in range(target_frames):
            s0 = int(seg_starts[f_idx])
            seg = iq[s0 : min(n_iq, s0 + seg_len)]
            if len(seg) < nfft_macro * 2:
                continue

            starts = np.linspace(0, len(seg) - nfft_macro, rows_per_frame, dtype=np.int64)
            idx_grid = starts[:, None] + np.arange(nfft_macro, dtype=np.int64)[None, :]
            windowed = seg[idx_grid] * win_macro[None, :]
            spectra = np.fft.fftshift(np.fft.fft(windowed, n=nfft_macro, axis=1), axes=1)
            mag_sq = (np.abs(spectra) / win_macro_sum) ** 2
            mag_sq[:, 512] = 0.5 * (mag_sq[:, 511] + mag_sq[:, 513])

            passband_512 = mag_sq[:, c_bin_start_1024:c_bin_end_1024].reshape(rows_per_frame, 128, 4)
            # Peak-preserving 4->1 spectral pooling keeps narrowband carriers razor-sharp while smoothing thermal floor
            pooled_mag_sq = 0.55 * np.max(passband_512, axis=2) + 0.45 * np.mean(passband_512, axis=2)
            frame_1mhz = (10.0 * np.log10(pooled_mag_sq + 1e-12)).astype(np.float32)

            if cols_per_frame != 128:
                x_old = np.linspace(0.0, 1.0, 128)
                x_new = np.linspace(0.0, 1.0, cols_per_frame)
                frame_1mhz = np.vstack([np.interp(x_new, x_old, r) for r in frame_1mhz]).astype(np.float32)
            frames.append(frame_1mhz)

        iq_metrics = self._compute_iq_signal_metrics(
            iq=iq,
            frames=frames,
            sample_rate=sample_rate,
            lo_offset_hz=signed_target_offset_hz,
            is_atsc_tv=(tv_ch is not None),
            nominal_center_hz=float(center_freq_hz),
            capture_center_hz=capture_center_hz,
            target_zoom_frames=target_frames,
            rows_per_frame=rows_per_frame,
            cols_per_frame=cols_per_frame,
        )
        return frames, iq_metrics

    @staticmethod
    def _fir_decimate_8x(sig: np.ndarray) -> np.ndarray:
        """Clean Hamming-sinc anti-aliased 8x complex IQ decimator."""
        if len(sig) < 64:
            return sig[::8]
        re_f = np.convolve(sig.real, _FIR_DECIM8_TAPS, mode="same")[::8]
        im_f = np.convolve(sig.imag, _FIR_DECIM8_TAPS, mode="same")[::8]
        return (re_f + 1j * im_f).astype(np.complex64)

    @staticmethod
    def _compute_iq_signal_metrics(
        iq: np.ndarray,
        frames: List[np.ndarray],
        sample_rate: int = 2_000_000,
        lo_offset_hz: int = 250_000,
        is_atsc_tv: bool = False,
        nominal_center_hz: float = 100_000_000.0,
        capture_center_hz: Optional[float] = None,
        target_zoom_frames: int = 24,
        rows_per_frame: int = 64,
        cols_per_frame: int = 128,
    ) -> Dict[str, Any]:
        """
        1,024x Ultra-Zoom Precision DSP & Modulation Analyzer:
          - 262,144-pt Hann Welch PSD (7.629 Hz/bin = 1,024x finer resolution!) for sub-10 Hz carrier lock,
            coherent spine tonality, double-sideband symmetry, and FSK Mark/Space twin-rail shift.
          - 64x / 8x FIR-decimated Ultra-Zoom Micro Waterfall frames (244.14 Hz/bin narrowband;
            976.56 / 1,953.1 Hz/bin wideband) centered on the locked carrier.
          - Subcarrier demodulator detecting 19.000 kHz WFM Stereo Pilot & 67.0-254.1 Hz TIA-603 CTCSS tones.
        """
        cap_cf_hz = float(capture_center_hz if capture_center_hz is not None else nominal_center_hz)
        stack = np.stack(frames, axis=0)  # (N, 64, 128)
        mean_psd = np.mean(stack, axis=(0, 1))
        max_hold_psd = np.percentile(stack.reshape(-1, stack.shape[-1]), 90, axis=0)

        bin_hz = 1_000_000.0 / len(mean_psd)
        mid = len(mean_psd) // 2

        noise_floor_dbfs = float(np.percentile(mean_psd, 12 if is_atsc_tv else 20))
        rad_bins = max(4, int(round((220_000.0 if is_atsc_tv else 115_000.0) / bin_hz)))
        c_lo = max(0, mid - rad_bins)
        c_hi = min(len(mean_psd), mid + rad_bins + 1)

        peak_bin = c_lo + int(np.argmax(max_hold_psd[c_lo:c_hi]))
        peak_dbfs = float(max_hold_psd[peak_bin])
        snr_db = max(0.0, peak_dbfs - noise_floor_dbfs)

        carrier_time_trace = np.max(
            stack.reshape(-1, stack.shape[-1])[:, max(0, peak_bin - 2) : min(len(mean_psd), peak_bin + 3)],
            axis=1,
        )
        active_rows = carrier_time_trace >= (noise_floor_dbfs + max(4.0, snr_db * 0.35))
        duty_cycle = float(np.mean(active_rows))

        obw_thresh = max(noise_floor_dbfs + 4.5, peak_dbfs - 10.5)
        max_rad = len(mean_psd) if is_atsc_tv else int(150_000 / bin_hz)
        b_lo = peak_bin
        while b_lo > max(0, mid - max_rad) and max_hold_psd[b_lo - 1] >= obw_thresh:
            b_lo -= 1
        b_hi = peak_bin
        while b_hi < min(len(mean_psd) - 1, mid + max_rad) and max_hold_psd[b_hi + 1] >= obw_thresh:
            b_hi += 1
        coarse_obw_hz = max(bin_hz, float((b_hi - b_lo + 1) * bin_hz))
        if is_atsc_tv and float(np.mean(mean_psd[20:] >= noise_floor_dbfs + 3.5)) >= 0.55:
            coarse_obw_hz = 5_380_000.0

        # ------------------------------------------------------------------
        # 1. Sub-10 Hz Precision Welch PSD (262,144-pt FFT @ 2.0 MSPS = 7.62939 Hz/bin = 1,024x detail!)
        # ------------------------------------------------------------------
        nfft_fine = 262_144 if len(iq) >= 262_144 else (65_536 if len(iq) >= 65_536 else max(4096, 1 << int(math.log2(max(4096, len(iq))))))
        df_fine = float(sample_rate) / float(nfft_fine)
        precision_delta_hz = 0.0
        carrier_3db_width_hz = 1200.0
        carrier_tonality_db = 6.0
        sideband_symmetry = 0.5
        fsk_rail_score = 0.0
        fsk_shift_hz = 0.0
        fine_obw_hz = coarse_obw_hz

        if len(iq) >= nfft_fine:
            win_fine = np.hanning(nfft_fine).astype(np.float32)
            win_fine_sum = float(np.sum(win_fine))
            n_blocks = min(10, max(1, (len(iq) - nfft_fine) // max(1, nfft_fine // 2) + 1))
            b_starts = (
                np.linspace(0, len(iq) - nfft_fine, n_blocks, dtype=np.int64)
                if len(iq) > nfft_fine
                else np.array([0], dtype=np.int64)
            )
            psd_accum = np.zeros(nfft_fine, dtype=np.float64)
            psd_max = np.zeros(nfft_fine, dtype=np.float32)
            for bs in b_starts:
                spec_f = np.fft.fftshift(np.fft.fft(iq[int(bs) : int(bs) + nfft_fine] * win_fine))
                p_lin = ((np.abs(spec_f) / win_fine_sum) ** 2).astype(np.float32)
                psd_accum += p_lin
                np.maximum(psd_max, p_lin, out=psd_max)
            psd_mean_lin = (psd_accum / float(n_blocks)).astype(np.float32)
            # Blend mean and max-hold so burst/PTT carriers lock with full 7.63 Hz precision
            psd_fine_db = 10.0 * np.log10(0.45 * psd_mean_lin + 0.55 * psd_max + 1e-15)
            dc_idx = nfft_fine // 2
            psd_fine_db[dc_idx] = 0.5 * (psd_fine_db[dc_idx - 1] + psd_fine_db[dc_idx + 1])

            nom_offset_from_lo = float(lo_offset_hz) + (-40_559.0 if is_atsc_tv else 0.0)
            nom_bin = dc_idx + int(round(nom_offset_from_lo / df_fine))
            search_hz = 70_000.0 if coarse_obw_hz >= 68_000.0 else 32_000.0
            s_rad = max(16, int(round(search_hz / df_fine)))
            s0 = max(4, nom_bin - s_rad)
            s1 = min(nfft_fine - 4, nom_bin + s_rad + 1)

            if s1 > s0 + 16:
                local_fine = psd_fine_db[s0:s1]
                fine_floor_db = float(np.percentile(local_fine, 18))
                pk_rel = int(np.argmax(local_fine))
                pk_abs = s0 + pk_rel
                pk_val_db = float(local_fine[pk_rel])

                # Parabolic sub-bin interpolation (±3.8 Hz resolution!)
                sub_bin = 0.0
                if 0 < pk_rel < len(local_fine) - 1:
                    y0, y1, y2 = float(local_fine[pk_rel - 1]), pk_val_db, float(local_fine[pk_rel + 1])
                    denom = y0 - 2.0 * y1 + y2
                    if abs(denom) > 1e-5:
                        sub_bin = max(-0.5, min(0.5, 0.5 * (y0 - y2) / denom))
                spine_delta_hz = ((float(pk_abs) + sub_bin) - float(nom_bin)) * df_fine

                # Coherent carrier spine tonality & -3 dB line width (separates AM/CW/Pilot from FM/FSK!)
                w_lo_b = pk_rel
                while w_lo_b > 0 and local_fine[w_lo_b - 1] >= pk_val_db - 3.0:
                    w_lo_b -= 1
                w_hi_b = pk_rel
                while w_hi_b < len(local_fine) - 1 and local_fine[w_hi_b + 1] >= pk_val_db - 3.0:
                    w_hi_b += 1
                carrier_3db_width_hz = float(max(1, w_hi_b - w_lo_b + 1)) * df_fine

                sb_rad = max(8, int(round(7_500.0 / df_fine)))
                guard_rad = max(3, int(round(180.0 / df_fine)))
                lsb = local_fine[max(0, pk_rel - sb_rad) : max(0, pk_rel - guard_rad)]
                usb = local_fine[min(len(local_fine), pk_rel + guard_rad + 1) : min(len(local_fine), pk_rel + sb_rad + 1)]
                if len(lsb) >= 8 and len(usb) >= 8:
                    sb_med = 0.5 * (float(np.median(lsb)) + float(np.median(usb)))
                    carrier_tonality_db = max(0.0, pk_val_db - sb_med)
                    m_len = min(len(lsb), len(usb))
                    lsb_sm = uniform_filter1d(lsb[::-1][:m_len], size=max(3, int(round(200.0 / df_fine))))
                    usb_sm = uniform_filter1d(usb[:m_len], size=max(3, int(round(200.0 / df_fine))))
                    if float(np.std(lsb_sm)) > 0.15 and float(np.std(usb_sm)) > 0.15:
                        corr = float(np.corrcoef(lsb_sm, usb_sm)[0, 1])
                        if math.isfinite(corr):
                            sideband_symmetry = float(np.clip(0.5 * (corr + 1.0), 0.0, 1.0))

                # FSK bimodal Mark/Space rail detection & symmetric midpoint lock
                sm_bins = max(5, int(round(250.0 / df_fine)))
                sm_fine = uniform_filter1d(local_fine, size=sm_bins)
                fsk_pks, _ = find_peaks(
                    sm_fine - fine_floor_db,
                    height=max(6.0, (pk_val_db - fine_floor_db) * 0.45),
                    distance=max(4, int(round(1_400.0 / df_fine))),
                    prominence=3.0,
                )
                if len(fsk_pks) >= 2 and coarse_obw_hz < 55_000.0:
                    top2 = sorted(fsk_pks, key=lambda idx_p: float(sm_fine[idx_p]), reverse=True)[:2]
                    p_left, p_right = min(top2), max(top2)
                    sep_hz = float(p_right - p_left) * df_fine
                    if 1_600.0 <= sep_hz <= 26_000.0 and abs(float(sm_fine[p_left]) - float(sm_fine[p_right])) <= 7.5:
                        valley_db = float(np.min(sm_fine[p_left : p_right + 1]))
                        rail_min_db = min(float(sm_fine[p_left]), float(sm_fine[p_right]))
                        fsk_rail_score = float(np.clip((rail_min_db - valley_db) / max(4.0, pk_val_db - fine_floor_db), 0.0, 1.0))
                        if fsk_rail_score >= 0.28:
                            fsk_shift_hz = sep_hz

                # Compute high-precision OBW from smoothed fine PSD (-12 dB / floor+5 dB) unless ATSC 6MHz shelf
                if not is_atsc_tv:
                    obw_sm = uniform_filter1d(local_fine, size=max(5, int(round((1_800.0 if coarse_obw_hz >= 68_000 else 300.0) / df_fine))))
                    sm_pk_idx = int(np.argmax(obw_sm))
                    sm_pk_db = float(obw_sm[sm_pk_idx])
                    f_thresh = max(fine_floor_db + 5.0, sm_pk_db - 11.5)
                    act_bins = np.where(obw_sm >= f_thresh)[0]
                    if len(act_bins) >= 2:
                        measured_fine_bw = float(act_bins[-1] - act_bins[0] + 1) * df_fine
                        fine_obw_hz = float(np.clip(measured_fine_bw, 400.0, 260_000.0))

                # Select optimal carrier lock point:
                #   - Coherent AM/CW/ATSC pilot spine -> 7.63 Hz parabolic spine peak (±3.8 Hz!)
                #   - Symmetric 2-FSK rails -> exact Mark/Space midpoint!
                #   - Angle-modulated FM/WFM -> linear-power-weighted -6 dB spectral centroid!
                if is_atsc_tv or (carrier_tonality_db >= 11.5 and carrier_3db_width_hz <= 90.0):
                    precision_delta_hz = spine_delta_hz
                elif fsk_rail_score >= 0.34 and len(fsk_pks) >= 2:
                    top2 = sorted(fsk_pks, key=lambda idx_p: float(sm_fine[idx_p]), reverse=True)[:2]
                    mid_rel = 0.5 * (float(top2[0]) + float(top2[1]))
                    precision_delta_hz = ((float(s0) + mid_rel) - float(nom_bin)) * df_fine
                else:
                    sm_env = uniform_filter1d(local_fine, size=max(5, int(round((2_500.0 if coarse_obw_hz >= 68_000 else 350.0) / df_fine))))
                    sm_pk = float(np.max(sm_env))
                    c_mask = sm_env >= max(fine_floor_db + 4.5, sm_pk - 6.0)
                    if np.sum(c_mask) >= 3:
                        idxs = np.where(c_mask)[0]
                        w_lin = 10.0 ** ((sm_env[c_mask] - sm_pk) / 10.0)
                        cent_rel = float(np.sum(idxs.astype(np.float64) * w_lin) / np.sum(w_lin))
                        precision_delta_hz = ((float(s0) + cent_rel) - float(nom_bin)) * df_fine
                    else:
                        precision_delta_hz = spine_delta_hz

        if is_atsc_tv:
            precision_carrier_hz = float(nominal_center_hz) + precision_delta_hz
            ddc_shift_hz = float(lo_offset_hz) - 40_559.0 + precision_delta_hz
            obw_hz = 5_380_000.0
        else:
            precision_carrier_hz = cap_cf_hz + precision_delta_hz
            ddc_shift_hz = float(lo_offset_hz) + precision_delta_hz
            obw_hz = fine_obw_hz if fine_obw_hz > 0.0 else coarse_obw_hz

        # ------------------------------------------------------------------
        # 2. Precision DDC Baseband Mixdown + 64x / 8x FIR Ultra-Zoom Waterfalls + Subcarrier Decode
        # ------------------------------------------------------------------
        max_ddc_n = min(len(iq), 2_621_440)  # Up to 1.31s continuous stream for fast FIR decimation
        max_ddc_n = (max_ddc_n // 64) * 64
        iq_mod_hint = "NFM"
        freq_dev_85 = 0.0
        env_cv = 0.2
        stereo_pilot = False
        stereo_pilot_snr_db = 0.0
        ctcss_hz = 0.0
        zoom_frames: List[np.ndarray] = []
        zoom_span_hz = 31_250.0
        zoom_bin_hz = 244.14

        if max_ddc_n >= 8_192:
            # Chunked phase-continuous oscillator avoids giant float64 allocation while keeping exact phase
            t_vec = np.arange(max_ddc_n, dtype=np.float32)
            phase_step = -2.0 * math.pi * float(ddc_shift_hz) / float(sample_rate)
            bb_full = iq[:max_ddc_n] * np.exp(1j * (phase_step * t_vec)).astype(np.complex64)

            # Stage 1: Clean FIR 8x decimation -> 250.0 kSPS wideband baseband
            z_250k = HackRFScannerEngine._fir_decimate_8x(bb_full)
            # Stage 2: Clean FIR 8x decimation -> 31.25 kSPS narrowband ultra-zoom baseband (64x total!)
            z_31k = HackRFScannerEngine._fir_decimate_8x(z_250k)

            # Physical envelope & instantaneous FM deviation analysis
            env_nb = np.abs(z_31k)
            env_med = float(np.median(env_nb)) + 1e-9
            active_nb = env_nb > (env_med * 0.45)
            env_cv = float(np.std(env_nb[active_nb]) / (np.mean(env_nb[active_nb]) + 1e-9)) if np.any(active_nb) else 0.2

            phase_250k = np.unwrap(np.angle(z_250k))
            inst_freq_250k = np.diff(phase_250k) * (250_000.0 / (2.0 * math.pi))
            freq_dev_85 = float(np.percentile(np.abs(inst_freq_250k), 85)) if len(inst_freq_250k) > 16 else 0.0

            # Detect 19.000 kHz WFM Stereo Pilot in 250 kSPS MPX baseband (3.81 Hz/bin!)
            if len(inst_freq_250k) >= 16_384:
                n_mpx = 65_536 if len(inst_freq_250k) >= 65_536 else 16_384
                mpx_seg = inst_freq_250k[:n_mpx] - float(np.mean(inst_freq_250k[:n_mpx]))
                mpx_spec = np.abs(np.fft.rfft(mpx_seg * np.hanning(n_mpx)))
                df_mpx = 250_000.0 / float(n_mpx)
                b_19k_lo = max(1, int(round(18_955.0 / df_mpx)))
                b_19k_hi = min(len(mpx_spec) - 1, int(round(19_045.0 / df_mpx)) + 1)
                b_g_lo = max(1, int(round(17_600.0 / df_mpx)))
                b_g_hi = min(len(mpx_spec) - 1, int(round(20_400.0 / df_mpx)))
                if b_19k_hi > b_19k_lo and b_g_hi > b_g_lo + 12:
                    p19_db = 20.0 * math.log10(float(np.max(mpx_spec[b_19k_lo:b_19k_hi])) + 1e-9)
                    guard_vals = np.concatenate([mpx_spec[b_g_lo:b_19k_lo], mpx_spec[b_19k_hi:b_g_hi]])
                    g_db = 20.0 * math.log10(float(np.median(guard_vals)) + 1e-9) if len(guard_vals) > 4 else p19_db
                    stereo_pilot_snr_db = max(0.0, p19_db - g_db)
                    stereo_pilot = bool(stereo_pilot_snr_db >= 7.5)

            # Classify physical IQ modulation using fused 7.63 Hz spine + envelope + FM deviation + stereo pilot
            if obw_hz >= 75_000.0 or stereo_pilot or (freq_dev_85 >= 24_000.0 and env_cv < 0.42):
                iq_mod_hint = "WFM"
            elif duty_cycle < 0.55 or fsk_rail_score >= 0.36:
                iq_mod_hint = "DIGITAL_BURST"
            elif obw_hz <= 4_500.0 and carrier_3db_width_hz <= 65.0 and env_cv < 0.16 and freq_dev_85 < 1_400.0:
                iq_mod_hint = "CW"
            elif (
                (carrier_tonality_db >= 11.5 and carrier_3db_width_hz <= 85.0 and env_cv >= 0.16 and sideband_symmetry >= 0.52)
                or (env_cv >= 0.27 and obw_hz <= 36_000.0 and fsk_rail_score < 0.25)
            ):
                iq_mod_hint = "AM"
            else:
                iq_mod_hint = "NFM"

            # Detect TIA-603 CTCSS Subaudible PL Tone (67.0 - 254.1 Hz @ 1.91 Hz/bin) on NFM channels!
            if iq_mod_hint == "NFM" and len(z_31k) >= 8_192:
                phase_31k = np.unwrap(np.angle(z_31k))
                inst_freq_31k = np.diff(phase_31k) * (31_250.0 / (2.0 * math.pi))
                n_pl = 16_384 if len(inst_freq_31k) >= 16_384 else 8_192
                pl_seg = inst_freq_31k[:n_pl] - float(np.mean(inst_freq_31k[:n_pl]))
                pl_spec = np.abs(np.fft.rfft(pl_seg * np.hanning(n_pl)))
                df_pl = 31_250.0 / float(n_pl)
                pl_lo = max(2, int(round(65.0 / df_pl)))
                pl_hi = min(len(pl_spec) - 2, int(round(258.0 / df_pl)) + 1)
                if pl_hi > pl_lo + 8:
                    sub_band = pl_spec[pl_lo:pl_hi]
                    pl_pk_rel = int(np.argmax(sub_band))
                    pl_pk_db = 20.0 * math.log10(float(sub_band[pl_pk_rel]) + 1e-9)
                    pl_med_db = 20.0 * math.log10(float(np.median(sub_band)) + 1e-9)
                    if pl_pk_db - pl_med_db >= 9.5:
                        # Parabolic sub-bin interpolation on 1.91 Hz bins -> ±0.4 Hz accuracy!
                        pl_sub = 0.0
                        if 0 < pl_pk_rel < len(sub_band) - 1:
                            a0, a1, a2 = float(sub_band[pl_pk_rel - 1]), float(sub_band[pl_pk_rel]), float(sub_band[pl_pk_rel + 1])
                            den = a0 - 2.0 * a1 + a2
                            if abs(den) > 1e-6:
                                pl_sub = max(-0.5, min(0.5, 0.5 * (a0 - a2) / den))
                        raw_pl_hz = (float(pl_lo + pl_pk_rel) + pl_sub) * df_pl
                        nearest_pl = min(STANDARD_CTCSS_TONES_HZ, key=lambda t_hz: abs(t_hz - raw_pl_hz))
                        if abs(nearest_pl - raw_pl_hz) <= 1.8:
                            ctcss_hz = float(nearest_pl)

            # Build Ultra-Zoom Micro Waterfall Frames (64 x 128 dBFS)
            if iq_mod_hint == "WFM" or (obw_hz >= 68_000.0 and not is_atsc_tv):
                # Wideband Zoom: 250 kHz span @ 1,953.1 Hz/bin (or 125 kHz @ 976.6 Hz/bin)
                z_src = z_250k
                nfft_z = 128 if obw_hz >= 115_000.0 else 256
                zoom_span_hz = 250_000.0 if nfft_z == 128 else 125_000.0
                zoom_bin_hz = zoom_span_hz / 128.0
            else:
                # Narrowband Ultra-Zoom: 31.25 kHz span @ 244.14 Hz/bin (32x zoomed-in waterfall!)
                z_src = z_31k
                nfft_z = 128
                zoom_span_hz = 31_250.0
                zoom_bin_hz = 244.140625

            if len(z_src) >= nfft_z + rows_per_frame * 2:
                win_z = np.hanning(nfft_z).astype(np.float32)
                win_z_sum = float(np.sum(win_z))
                z_win_len = max(nfft_z + rows_per_frame * 2, min(len(z_src), max(nfft_z * 12, int(len(z_src) / max(1.0, target_zoom_frames * 0.45)))))
                z_starts = (
                    np.linspace(0, max(0, len(z_src) - z_win_len), target_zoom_frames, dtype=np.int64)
                    if len(z_src) > z_win_len
                    else np.zeros(target_zoom_frames, dtype=np.int64)
                )
                c_z0 = (nfft_z - 128) // 2
                c_z1 = c_z0 + 128
                for z_i in range(target_zoom_frames):
                    zs0 = int(z_starts[z_i])
                    z_seg = z_src[zs0 : min(len(z_src), zs0 + z_win_len)]
                    if len(z_seg) < nfft_z + rows_per_frame:
                        continue
                    r_starts = np.linspace(0, len(z_seg) - nfft_z, rows_per_frame, dtype=np.int64)
                    r_grid = r_starts[:, None] + np.arange(nfft_z, dtype=np.int64)[None, :]
                    z_spec = np.fft.fftshift(np.fft.fft(z_seg[r_grid] * win_z[None, :], n=nfft_z, axis=1), axes=1)
                    z_mag_sq = ((np.abs(z_spec[:, c_z0:c_z1]) / win_z_sum) ** 2).astype(np.float32)
                    z_db = (10.0 * np.log10(z_mag_sq + 1e-13)).astype(np.float32)
                    if cols_per_frame != 128:
                        xo = np.linspace(0.0, 1.0, 128)
                        xn = np.linspace(0.0, 1.0, cols_per_frame)
                        z_db = np.vstack([np.interp(xn, xo, rw) for rw in z_db]).astype(np.float32)
                    zoom_frames.append(z_db)

        return {
            "peak_dbfs": round(peak_dbfs, 1),
            "noise_floor_dbfs": round(noise_floor_dbfs, 1),
            "snr_db": round(snr_db, 1),
            "obw_hz": round(obw_hz, 0),
            "measured_obw_hz": round(obw_hz, 0),
            "duty_cycle": round(duty_cycle, 2),
            "iq_mod_hint": iq_mod_hint,
            "freq_dev_85_hz": round(freq_dev_85, 1),
            "env_cv": round(env_cv, 3),
            "frames_captured": len(frames),
            "precision_carrier_hz": round(precision_carrier_hz, 1),
            "precision_delta_hz": round(precision_delta_hz, 1),
            "fine_fft_resolution_hz": round(df_fine, 3),
            "carrier_3db_width_hz": round(carrier_3db_width_hz, 1),
            "carrier_tonality_db": round(carrier_tonality_db, 2),
            "sideband_symmetry": round(sideband_symmetry, 3),
            "fsk_rail_score": round(fsk_rail_score, 3),
            "fsk_shift_hz": round(fsk_shift_hz, 1),
            "stereo_pilot": bool(stereo_pilot),
            "stereo_pilot_snr_db": round(stereo_pilot_snr_db, 1),
            "ctcss_hz": round(ctcss_hz, 1),
            "zoom_span_hz": round(zoom_span_hz, 1),
            "zoom_bin_hz": round(zoom_bin_hz, 2),
            "zoom_frames": zoom_frames,
        }

    # ------------------------------------------------------------------
    # Explicit Unit-Test Simulation Helpers (NEVER called unless config.simulate=True)
    # ------------------------------------------------------------------
    def _simulate_wideband(
        self,
        config: ScanConfig,
        region: str,
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray, List[DetectedSignalSpike]]:
        rng = np.random.default_rng(42)
        start_hz = config.freq_min_mhz * 1e6
        end_hz = config.freq_max_mhz * 1e6
        num_bins = int(max(64, (end_hz - start_hz) / config.l1_bin_width_hz))
        freqs = np.linspace(start_hz, end_hz, num_bins, dtype=np.float64)
        powers = rng.normal(loc=-79.0, scale=1.6, size=num_bins).astype(np.float32)

        test_carriers = [
            (93.3e6, 180e3, -44.0),
            (97.3e6, 180e3, -41.0),
            (107.9e6, 190e3, -38.0),
            (119.5e6, 12e3, -55.0),
            (162.55e6, 16e3, -49.0),
        ]
        for cf, bw, pwr in test_carriers:
            if start_hz <= cf <= end_hz:
                sigma = max(config.l1_bin_width_hz * 0.7, bw * 0.35)
                bump = pwr + 79.0
                shape = bump * np.exp(-0.5 * ((freqs - cf) / sigma) ** 2)
                powers = np.maximum(powers, -79.0 + shape.astype(np.float32))

        floor, spikes = self.extract_prominent_spikes(
            freqs, powers, float(config.l1_bin_width_hz), config.min_snr_db, config.min_peak_dbfs, 1, region, config.freq_min_mhz
        )
        return freqs, powers, floor, spikes

    def _simulate_iq_frames(
        self,
        center_freq_hz: float,
        num_frames: int,
        rows: int,
        cols: int,
    ) -> Tuple[List[np.ndarray], Dict[str, Any]]:
        rng = np.random.default_rng(int(center_freq_hz) % 100_000)
        is_wfm = 87.8e6 <= center_freq_hz <= 108.1e6
        is_air = 108.0e6 < center_freq_hz <= 137.0e6
        frames = []
        zoom_frames = []
        for idx in range(num_frames):
            f = rng.normal(loc=-82.0, scale=2.0, size=(rows, cols)).astype(np.float32)
            zf = rng.normal(loc=-88.0, scale=1.8, size=(rows, cols)).astype(np.float32)
            mid = cols // 2
            span = 11 if is_wfm else 2
            z_span = 36 if is_wfm else 24
            f[:, max(0, mid - span) : min(cols, mid + span + 1)] = -42.0 + 2.0 * math.sin(idx)
            zf[:, max(0, mid - z_span) : min(cols, mid + z_span + 1)] = -40.0 + 2.0 * math.cos(idx)
            frames.append(f)
            zoom_frames.append(zf)
        return frames, {
            "peak_dbfs": -41.0,
            "noise_floor_dbfs": -82.0,
            "snr_db": 41.0,
            "obw_hz": 180_000.0 if is_wfm else (9_000.0 if is_air else 16_000.0),
            "measured_obw_hz": 180_000.0 if is_wfm else (9_000.0 if is_air else 16_000.0),
            "duty_cycle": 1.0,
            "iq_mod_hint": "WFM" if is_wfm else ("AM" if is_air else "NFM"),
            "frames_captured": num_frames,
            "precision_carrier_hz": float(center_freq_hz),
            "precision_delta_hz": 0.0,
            "fine_fft_resolution_hz": 7.629,
            "carrier_3db_width_hz": 24.0 if is_air else (45_000.0 if is_wfm else 2_400.0),
            "carrier_tonality_db": 18.0 if is_air else 5.5,
            "sideband_symmetry": 0.88 if is_air else 0.72,
            "fsk_rail_score": 0.0,
            "fsk_shift_hz": 0.0,
            "stereo_pilot": bool(is_wfm),
            "stereo_pilot_snr_db": 16.5 if is_wfm else 0.0,
            "ctcss_hz": 0.0 if (is_wfm or is_air) else 123.0,
            "zoom_span_hz": 250_000.0 if is_wfm else 31_250.0,
            "zoom_bin_hz": 1_953.12 if is_wfm else 244.14,
            "zoom_frames": zoom_frames,
        }
