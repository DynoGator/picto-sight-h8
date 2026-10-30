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
from scipy.signal import find_peaks

from core.bandplans import snap_to_channel_raster
from core.sdr_arbiter import get_sdr_arbiter

logger = logging.getLogger("smart_scanner.hackrf")


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
    freq_min_mhz: int = 88
    freq_max_mhz: int = 137
    min_snr_db: float = 9.0                     # Signal strength adjuster 1: Minimum SNR (dB)
    min_peak_dbfs: float = -72.0                # Signal strength adjuster 2: Minimum Peak Power (dBFS)
    demod_mode: str = "AUTO (Band + AI)"        # Demodulation mode override or AUTO
    squelch_mode: str = "OPEN"                  # "OPEN", "AUTO", or "CUSTOM:<db>"
    l1_bin_width_hz: int = 50_000
    l1_sweeps: int = 2
    l2_bin_width_hz: int = 12_500
    l2_sweeps: int = 2
    l3_frames_per_mhz: int = 3                  # Guaranteed >= 3 frames per 1 MHz
    lna_gain_db: int = 24                       # 0..40 step 8
    vga_gain_db: int = 22                       # 0..62 step 2
    amp_enable: int = 0                         # 0 or 1
    only_legal_demod: bool = True
    max_candidates: int = 20
    simulate: bool = False                      # NEVER True on real scans; reserved strictly for unit tests

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mode(
        cls,
        mode: str = "QUICK",
        freq_min_mhz: int = 88,
        freq_max_mhz: int = 137,
        min_snr_db: float = 9.0,
        min_peak_dbfs: float = -72.0,
        demod_mode: str = "AUTO (Band + AI)",
        squelch_mode: str = "OPEN",
        l3_frames_per_mhz: int = 3,
        lna_gain_db: int = 24,
        vga_gain_db: int = 22,
        amp_enable: int = 0,
        only_legal_demod: bool = True,
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
            l1_bw = 125_000 if span > 1500 else (75_000 if span > 600 else (50_000 if span > 220 else 25_000))
            l1_sw = 3 if span > 1500 else 5
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
                l2_bin_width_hz=5_000,
                l2_sweeps=4,
                l3_frames_per_mhz=max(5, int(l3_frames_per_mhz)),
                lna_gain_db=lna,
                vga_gain_db=vga,
                amp_enable=amp,
                only_legal_demod=only_legal_demod,
                max_candidates=30,
            )
        if norm_mode == "CUSTOM":
            l1_bw = 200_000 if span > 1500 else (100_000 if span > 500 else (50_000 if span > 180 else 25_000))
            l1_sw = 2 if span > 1500 else 3
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
                l2_bin_width_hz=10_000,
                l2_sweeps=3,
                l3_frames_per_mhz=max(3, int(l3_frames_per_mhz)),
                lna_gain_db=lna,
                vga_gain_db=vga,
                amp_enable=amp,
                only_legal_demod=only_legal_demod,
                max_candidates=28,
            )
        # Default: QUICK
        l1_bw_q = 250_000 if span > 1200 else (100_000 if span > 220 else 50_000)
        return cls(
            mode="QUICK",
            freq_min_mhz=f_min,
            freq_max_mhz=f_max,
            min_snr_db=float(min_snr_db),
            min_peak_dbfs=float(min_peak_dbfs),
            demod_mode=demod_mode,
            squelch_mode=squelch_mode,
            l1_bin_width_hz=l1_bw_q,
            l1_sweeps=2,
            l2_bin_width_hz=12_500,
            l2_sweeps=2,
            l3_frames_per_mhz=3,
            lna_gain_db=lna,
            vga_gain_db=vga,
            amp_enable=amp,
            only_legal_demod=only_legal_demod,
            max_candidates=18,
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
        """Executes hackrf_sweep and averages power bins across num_sweeps with auto-USB recovery."""
        f_min_mhz = max(1, int(f_min_mhz))
        f_max_mhz = max(f_min_mhz + 1, int(f_max_mhz))
        # Valid hackrf_sweep bin width range: 2445 .. 5000000 Hz
        bin_w_clamped = max(3_000, min(1_000_000, int(bin_width_hz)))

        last_err_txt = ""
        for attempt in range(2):
            if stop_check is not None and stop_check():
                return np.array([], dtype=np.float64), np.array([], dtype=np.float32)

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
                    return np.array([], dtype=np.float64), np.array([], dtype=np.float32)

                freq_accum: Dict[int, List[float]] = {}
                if os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 0:
                    with open(tmp_path, "r", encoding="utf-8", errors="ignore") as f:
                        reader = csv.reader(f)
                        for row in reader:
                            if len(row) < 7:
                                continue
                            try:
                                hz_low = float(row[2].strip())
                                hz_high = float(row[3].strip())
                                step_hz = (hz_high - hz_low) / max(1, len(row) - 6)
                                for i, db_str in enumerate(row[6:]):
                                    pwr = float(db_str.strip())
                                    if not math.isfinite(pwr):
                                        continue
                                    cf = int(round(hz_low + (i + 0.5) * step_hz))
                                    if f_min_mhz * 1_000_000 <= cf <= f_max_mhz * 1_000_000:
                                        freq_accum.setdefault(cf, []).append(pwr)
                            except Exception:
                                continue

                if freq_accum:
                    sorted_freqs = np.array(sorted(freq_accum.keys()), dtype=np.float64)
                    powers = np.array([float(np.mean(freq_accum[int(f)])) for f in sorted_freqs], dtype=np.float32)
                    return sorted_freqs, powers

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

    # ------------------------------------------------------------------
    # LAYER 1: Wideband Spectrum Sweep
    # ------------------------------------------------------------------
    def sweep_wideband(
        self,
        config: ScanConfig,
        region: str = "US",
        stop_check: Optional[Callable[[], bool]] = None,
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray, List[DetectedSignalSpike]]:
        """
        Layer 1: Wideband multi-sweep survey.
        Returns (frequencies_hz, powers_dbfs, baseline_floor_dbfs, detected_spikes).
        """
        if config.simulate:
            return self._simulate_wideband(config, region)

        t0 = time.perf_counter()
        span_mhz = max(1, config.freq_max_mhz - config.freq_min_mhz)
        sweep_timeout = max(12.0, min(45.0, span_mhz * 0.015 * config.l1_sweeps + 10.0))
        freqs, powers = self._run_hackrf_sweep_csv(
            f_min_mhz=config.freq_min_mhz,
            f_max_mhz=config.freq_max_mhz,
            bin_width_hz=config.l1_bin_width_hz,
            num_sweeps=config.l1_sweeps,
            lna_gain_db=config.lna_gain_db,
            vga_gain_db=config.vga_gain_db,
            amp_enable=config.amp_enable,
            timeout_s=sweep_timeout,
            stop_check=stop_check,
        )
        if len(freqs) == 0:
            return freqs, powers, np.array([], dtype=np.float32), []
        elapsed = time.perf_counter() - t0
        logger.info(
            "Layer 1 sweep (%d-%d MHz, %d bins, %d sweeps) completed in %.2fs",
            config.freq_min_mhz,
            config.freq_max_mhz,
            len(freqs),
            config.l1_sweeps,
            elapsed,
        )

        noise_floor, spikes = self.extract_prominent_spikes(
            freqs=freqs,
            powers=powers,
            bin_width_hz=float(config.l1_bin_width_hz),
            min_snr_db=config.min_snr_db,
            min_peak_dbfs=config.min_peak_dbfs,
            layer_stage=1,
            region=region,
            f_min_mhz=config.freq_min_mhz,
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
    def _deduplicate_by_band_spacing(
        spikes: List[DetectedSignalSpike],
        region: str = "US",
    ) -> List[DetectedSignalSpike]:
        if not spikes:
            return []

        # Process strongest spikes first so sidebands merge into true main carrier
        by_strength = sorted(spikes, key=lambda s: s.peak_dbm, reverse=True)
        kept: List[DetectedSignalSpike] = []

        for cand in by_strength:
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
    # LAYER 2: Merged-Window Fine Zoom Refinement & Channel Alignment
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
        coincidences), sweeps at fine bin width (5-12.5 kHz), refines center frequency & bandwidth,
        and aligns carriers to regulatory channel rasters.
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

        # Build merged integer-MHz zoom spans (4-8 MHz wide) covering all candidates
        sorted_cands = sorted(candidates, key=lambda c: c.center_freq_hz)
        windows: List[Tuple[int, int, List[DetectedSignalSpike]]] = []

        for cand in sorted_cands:
            c_mhz = cand.center_freq_hz / 1e6
            # Shift start by -1.5 MHz floored so LO steps differ from Layer 1
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

        for w_idx, (w_lo, w_hi, grp_cands) in enumerate(windows):
            if stop_check is not None and stop_check():
                return []
            if progress_cb:
                progress_cb(w_idx + 1, total_wins, grp_cands[0].center_freq_mhz)

            try:
                sub_freqs, sub_powers = self._run_hackrf_sweep_csv(
                    f_min_mhz=w_lo,
                    f_max_mhz=w_hi,
                    bin_width_hz=config.l2_bin_width_hz,
                    num_sweeps=config.l2_sweeps,
                    lna_gain_db=config.lna_gain_db,
                    vga_gain_db=config.vga_gain_db,
                    amp_enable=config.amp_enable,
                    timeout_s=10.0,
                    stop_check=stop_check,
                )
            except Exception as exc:
                logger.warning("Layer 2 zoom window %d-%d MHz note: %s", w_lo, w_hi, exc)
                continue

            if len(sub_freqs) < 8:
                continue

            local_floor = float(np.percentile(sub_powers, 20))
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

                # Reject Layer 1 transients/spurs that fail confirmation in Layer 2
                if ref_snr < max(5.0, config.min_snr_db - 1.8) or ref_peak < (config.min_peak_dbfs - 2.0):
                    continue

                # Energy-weighted centroid within -4 dB of peak for stable FM/AM center estimation
                top_mask = w_pwrs >= (ref_peak - 4.0)
                if np.sum(top_mask) >= 2:
                    lin_weights = 10.0 ** ((w_pwrs[top_mask] - ref_peak) / 10.0)
                    raw_center_hz = float(np.sum(w_freqs[top_mask] * lin_weights) / np.sum(lin_weights))
                else:
                    raw_center_hz = float(w_freqs[p_idx])

                # Measure -6 dB bandwidth
                bw_mask = w_pwrs >= max(local_floor + 4.0, ref_peak - 6.0)
                above_f = w_freqs[bw_mask]
                if len(above_f) >= 2:
                    ref_bw = float(np.max(above_f) - np.min(above_f)) + eff_bin
                else:
                    ref_bw = max(eff_bin, cand.estimated_bandwidth_hz)

                # Reject razor-thin 1-bin unmodulated spur or exact Si5351/RP1 clock needle
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
                        estimated_bandwidth_hz=round(max(8_000.0, min(260_000.0, ref_bw)), 0),
                        layer_stage=2,
                    )
                )

        deduped = self._deduplicate_snapped(refined)
        # Prioritize strongest viable signals if exceeding max_candidates, then re-sort ascending
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
    # LAYER 3: True 1.000 MHz IQ Capture & >=3 STFT Pictogram Frames
    # ------------------------------------------------------------------
    def capture_1mhz_pictogram_frames(
        self,
        center_freq_hz: float,
        num_frames: int = 3,
        rows_per_frame: int = 64,
        cols_per_frame: int = 128,
        lna_gain_db: int = 24,
        vga_gain_db: int = 22,
        amp_enable: int = 0,
        simulate: bool = False,
        stop_check: Optional[Callable[[], bool]] = None,
    ) -> Tuple[List[np.ndarray], Dict[str, Any]]:
        """
        Layer 3: Captures raw 2.000 MSPS complex IQ via hackrf_transfer with a ±250 kHz LO offset
        (placing HackRF's 0 Hz DC spike 250 kHz away from the target signal and safely >= 1.0 MHz LO
        even on low-HF 1.0-1.5 MHz carriers), notches DC, and extracts the exact 1.000 MHz passband
        [center_freq_hz - 500 kHz, center_freq_hz + 500 kHz] across >=3 independent temporal STFT
        waterfall frames (64 x 128 dBFS each).

        Also measures true calibrated dBFS peak/noise floor, occupied bandwidth (OBW),
        duty cycle, and physical IQ modulation type (WFM, NFM, AM, CW, DIGITAL_BURST).
        """
        target_frames = max(3, int(num_frames))
        if simulate:
            return self._simulate_iq_frames(center_freq_hz, target_frames, rows_per_frame, cols_per_frame)

        sample_rate = 2_000_000
        # For low-HF (< 1.5 MHz), place LO +250 kHz above target so LO >= 1.25 MHz (within HackRF 1 MHz min limit)
        if center_freq_hz < 1_500_000.0:
            signed_target_offset_hz = -250_000
            lo_freq_hz = max(1_000_000, int(round(center_freq_hz + 250_000)))
            c_bin_start, c_bin_end = 32, 160
        else:
            signed_target_offset_hz = 250_000
            lo_freq_hz = max(1_000_000, int(round(center_freq_hz - 250_000)))
            c_bin_start, c_bin_end = 96, 224

        # Minimum 87,380 complex samples per frame (~43.7 ms/frame -> >= 131 ms total for 3 frames, 220 ms for 5 frames)
        samples_per_frame = 90_112
        total_samples = max(262_144, target_frames * samples_per_frame)

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
                        timeout_s=6.5,
                        stop_check=stop_check,
                    )
                except TimeoutError as t_exc:
                    out_s, err_s, aborted = "", str(t_exc), False

                # Brief USB bulk endpoint settle between consecutive Layer 3 captures
                time.sleep(0.035)

                if aborted:
                    return [], {}

                if os.path.exists(iq_path) and os.path.getsize(iq_path) >= 32_768:
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

        if raw_bytes is None or len(raw_bytes) < 32_768:
            raise RuntimeError(
                f"Layer 3 IQ capture failed at {center_freq_hz / 1e6:.4f} MHz: {last_err_txt[:180]}"
            )

        n_iq = len(raw_bytes) // 2
        raw_f = raw_bytes[: n_iq * 2].astype(np.float32) / 128.0
        iq = raw_f[0::2] + 1j * raw_f[1::2]

        # Remove global time-domain DC offset (eliminates 0 Hz LO spike)
        iq = iq - np.mean(iq)

        # STFT configuration:
        # At fs = 2.000 MSPS, nfft = 256 gives df = 7,812.5 Hz/bin.
        # LO (0 Hz) is at bin 128; target (+250 kHz -> bin 160 -> [96:224], or -250 kHz -> bin 96 -> [32:160]).
        nfft = 256
        window = np.hanning(nfft).astype(np.float32)
        win_coherent_sum = float(np.sum(window))

        frames: List[np.ndarray] = []
        chunk_len = n_iq // target_frames

        for f_idx in range(target_frames):
            seg = iq[f_idx * chunk_len : (f_idx + 1) * chunk_len]
            if len(seg) < nfft * 4:
                continue

            # Compute 2x oversampled STFT rows and average pairs for low-variance dBFS spectrogram
            sub_rows = rows_per_frame * 2
            starts = np.linspace(0, len(seg) - nfft, sub_rows, dtype=np.int64)
            idx_grid = starts[:, None] + np.arange(nfft, dtype=np.int64)[None, :]
            windowed = seg[idx_grid] * window[None, :]
            spectra = np.fft.fftshift(np.fft.fft(windowed, n=nfft, axis=1), axes=1)
            mag_sq = (np.abs(spectra) / win_coherent_sum) ** 2

            # Interpolate residual LO DC bin (bin 128) cleanly
            mag_sq[:, 128] = 0.5 * (mag_sq[:, 127] + mag_sq[:, 129])

            # Average pairs of adjacent time rows -> (rows_per_frame, 256)
            avg_mag_sq = 0.5 * (mag_sq[0::2, :] + mag_sq[1::2, :])
            dbfs_full = 10.0 * np.log10(avg_mag_sq + 1e-12)

            # Crop exact 1.000 MHz target passband (64 x 128)
            frame_1mhz = dbfs_full[:rows_per_frame, c_bin_start:c_bin_end].astype(np.float32)
            if cols_per_frame != 128:
                x_old = np.linspace(0.0, 1.0, 128)
                x_new = np.linspace(0.0, 1.0, cols_per_frame)
                frame_1mhz = np.vstack([np.interp(x_new, x_old, r) for r in frame_1mhz]).astype(np.float32)
            frames.append(frame_1mhz)

        iq_metrics = self._compute_iq_signal_metrics(
            iq,
            frames,
            sample_rate=sample_rate,
            lo_offset_hz=signed_target_offset_hz,
        )
        return frames, iq_metrics

    @staticmethod
    def _compute_iq_signal_metrics(
        iq: np.ndarray,
        frames: List[np.ndarray],
        sample_rate: int = 2_000_000,
        lo_offset_hz: int = 250_000,
    ) -> Dict[str, Any]:
        """
        Derives true calibrated dBFS peak, noise floor, SNR, occupied bandwidth (OBW),
        duty cycle, and physical modulation signature from the captured IQ & 1 MHz frames.
        """
        stack = np.stack(frames, axis=0)  # (N, 64, 128)
        mean_psd = np.mean(stack, axis=(0, 1))  # 128 bins across 1.000 MHz (7812.5 Hz/bin)
        max_hold_psd = np.percentile(stack.reshape(-1, stack.shape[-1]), 90, axis=0)

        bin_hz = 1_000_000.0 / len(mean_psd)
        mid = len(mean_psd) // 2

        noise_floor_dbfs = float(np.percentile(mean_psd, 20))
        # Evaluate target signal within central ±110 kHz
        rad_bins = max(4, int(round(115_000.0 / bin_hz)))
        c_lo = max(0, mid - rad_bins)
        c_hi = min(len(mean_psd), mid + rad_bins + 1)

        peak_bin = c_lo + int(np.argmax(max_hold_psd[c_lo:c_hi]))
        peak_dbfs = float(max_hold_psd[peak_bin])
        snr_db = max(0.0, peak_dbfs - noise_floor_dbfs)

        # Temporal duty cycle across all N*64 time rows at the peak carrier columns
        carrier_time_trace = np.max(
            stack.reshape(-1, stack.shape[-1])[:, max(0, peak_bin - 2) : min(len(mean_psd), peak_bin + 3)],
            axis=1,
        )
        active_rows = carrier_time_trace >= (noise_floor_dbfs + max(4.0, snr_db * 0.35))
        duty_cycle = float(np.mean(active_rows))

        # Occupied bandwidth (-10 dB skirt relative to peak, bounded above noise_floor + 4.5 dB)
        obw_thresh = max(noise_floor_dbfs + 4.5, peak_dbfs - 10.5)
        b_lo = peak_bin
        while b_lo > max(0, mid - int(150_000 / bin_hz)) and max_hold_psd[b_lo - 1] >= obw_thresh:
            b_lo -= 1
        b_hi = peak_bin
        while b_hi < min(len(mean_psd) - 1, mid + int(150_000 / bin_hz)) and max_hold_psd[b_hi + 1] >= obw_thresh:
            b_hi += 1
        obw_hz = max(bin_hz, float((b_hi - b_lo + 1) * bin_hz))

        # Baseband mix-down of up to 65,520 IQ samples (divisible by 40 and 8) to classify AM vs WFM vs NFM vs CW
        seg_n = (min(65_520, len(iq)) // 40) * 40
        iq_mod_hint = "NFM"
        freq_dev_85 = 0.0
        env_cv = 0.2
        if seg_n >= 4_000:
            t_idx = np.arange(seg_n, dtype=np.float32)
            osc = np.exp(-1j * (2.0 * math.pi * float(lo_offset_hz) / float(sample_rate)) * t_idx)
            bb = iq[:seg_n] * osc

            # Decimate 8x -> 250 kSPS wideband channel
            z_wb = np.mean(bb.reshape(-1, 8), axis=1)
            # Decimate 40x -> 50 kSPS narrowband channel
            z_nb = np.mean(bb.reshape(-1, 40), axis=1)

            env_nb = np.abs(z_nb)
            env_med = float(np.median(env_nb)) + 1e-9
            active_nb = env_nb > (env_med * 0.45)
            env_cv = float(np.std(env_nb[active_nb]) / (np.mean(env_nb[active_nb]) + 1e-9)) if np.any(active_nb) else 0.2

            phase_wb = np.unwrap(np.angle(z_wb))
            inst_freq_wb = np.diff(phase_wb) * (250_000.0 / (2.0 * math.pi))
            freq_dev_85 = float(np.percentile(np.abs(inst_freq_wb), 85))

            if obw_hz >= 75_000.0 or (freq_dev_85 >= 24_000.0 and env_cv < 0.40):
                iq_mod_hint = "WFM"
            elif duty_cycle < 0.55:
                iq_mod_hint = "DIGITAL_BURST"
            elif obw_hz <= 8_500.0 and env_cv < 0.14 and freq_dev_85 < 1_800.0:
                iq_mod_hint = "CW"
            elif env_cv >= 0.25 and obw_hz <= 38_000.0:
                iq_mod_hint = "AM"
            else:
                iq_mod_hint = "NFM"

        return {
            "peak_dbfs": round(peak_dbfs, 1),
            "noise_floor_dbfs": round(noise_floor_dbfs, 1),
            "snr_db": round(snr_db, 1),
            "obw_hz": round(obw_hz, 0),
            "duty_cycle": round(duty_cycle, 2),
            "iq_mod_hint": iq_mod_hint,
            "freq_dev_85_hz": round(freq_dev_85, 1),
            "env_cv": round(env_cv, 3),
            "frames_captured": len(frames),
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
        frames = []
        for idx in range(num_frames):
            f = rng.normal(loc=-82.0, scale=2.0, size=(rows, cols)).astype(np.float32)
            mid = cols // 2
            span = 11 if is_wfm else 2
            f[:, max(0, mid - span) : min(cols, mid + span + 1)] = -42.0 + 2.0 * math.sin(idx)
            frames.append(f)
        return frames, {
            "peak_dbfs": -41.0,
            "noise_floor_dbfs": -82.0,
            "snr_db": 41.0,
            "obw_hz": 180_000.0 if is_wfm else 16_000.0,
            "duty_cycle": 1.0,
            "iq_mod_hint": "WFM" if is_wfm else "NFM",
            "frames_captured": num_frames,
        }
