"""
Multi-Layer AI Smart RF Scanner Pipeline Orchestrator
Coordinates:
  - Stage 0: Exclusive 1-at-a-time SDR Mutex Acquisition (SDRResourceArbiter) & GPS Fix Poll
  - Stage 1: Layer 1 Wideband Spectrum Sweep & 20th-Percentile Baseline Detection
  - Stage 2: Layer 2 Merged-Window Fine Zoom Refinement & Channel Raster Snapping
  - Stage 3: Layer 3 True 1.000 MHz IQ Capture (>=3 STFT frames/1MHz) & Hailo-8 26 TOPS AI Classification
  - Stage 4: Locality Profile Refinement, Permanent Reboot-Safe Disk Save & Gqrx Bookmarks Export

Emits rich graphical & alphanumerical progress telemetry (elapsed time, ETA, overall %, stage %,
live spectrum/noise floor arrays, and rendered >=3-frame AI spectrogram RGB strips).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple, Any

import numpy as np

from core.bandplans import find_band_definition, refine_precision_channel_frequency
from core.gps_engine import get_gps_manager, GPSLocalityFix
from core.hackrf_engine import (
    HackRFScannerEngine,
    DetectedSignalSpike,
    ScanConfig,
    is_likely_internal_clock_spur,
)
from core.pluto_engine import PlutoScannerEngine
from core.pictogram_engine import get_pictogram_classifier, PictogramAnalysisResult
from core.profile_organizer import (
    SignalProfileOrganizer,
    ClassifiedSignalProfile,
    SavedLocationProfile,
)
from core.sdr_arbiter import get_sdr_arbiter

logger = logging.getLogger("smart_scanner.pipeline")


@dataclass
class ScanProgressUpdate:
    stage: int                  # 0=Arbiter/GPS, 1=L1 Wideband, 2=L2 Zoom, 3=L3 1MHz IQ+AI, 4=Profile Save/Done
    stage_name: str
    percent_complete: float     # Overall 0..100%
    sub_percent: float          # Current stage 0..100%
    elapsed_s: float
    eta_s: float
    sdr_owner_badge: str
    message: str
    current_freq_mhz: float
    detected_count: int
    spectrum_frequencies: Optional[np.ndarray] = None
    spectrum_powers: Optional[np.ndarray] = None
    spectrum_floor: Optional[np.ndarray] = None
    latest_frame: Optional[np.ndarray] = None
    latest_composite_rgb: Optional[np.ndarray] = None
    latest_pictogram: Optional[PictogramAnalysisResult] = None
    saved_profile: Optional[SavedLocationProfile] = None


class MultiLayerScannerPipeline:
    """
    Executes the multi-layer AI classification loop under strict 1-at-a-time SDR locking.
    """

    def __init__(self, callback: Optional[Callable[[ScanProgressUpdate], None]] = None) -> None:
        self.callback = callback
        self.arbiter = get_sdr_arbiter()
        self.hackrf = HackRFScannerEngine()
        self.pluto = PlutoScannerEngine()
        self.gps_mgr = get_gps_manager()
        self.picto_mgr = get_pictogram_classifier()
        self.organizer = SignalProfileOrganizer()

        self._stop_requested = False
        self._is_scanning = False
        self._lock = threading.Lock()
        self.latest_results: List[ClassifiedSignalProfile] = []
        self.latest_saved_profile: Optional[SavedLocationProfile] = None

    def _active_sdr_engine(self):
        active = self.arbiter.get_active_sdr()
        if active == "LIBRESDR_PLUTO" and self.pluto.is_online():
            return self.pluto
        return self.hackrf

    def is_scanning(self) -> bool:
        with self._lock:
            return self._is_scanning

    def stop_scan(self) -> None:
        self._stop_requested = True

    def _emit(self, update: ScanProgressUpdate) -> None:
        if self.callback:
            try:
                self.callback(update)
            except Exception as exc:
                logger.debug("Progress callback error: %s", exc)

    def run_scan_with_config(
        self,
        config: ScanConfig,
    ) -> Tuple[List[ClassifiedSignalProfile], Optional[SavedLocationProfile]]:
        """
        Executes the complete multi-layer scan & permanent Location Profile creation using ScanConfig.
        """
        with self._lock:
            if self._is_scanning:
                raise RuntimeError("A multi-layer scan is already in progress.")
            self._is_scanning = True
            self._stop_requested = False

        t_start = time.perf_counter()
        sdr_acquired = False

        def _elapsed() -> float:
            return round(time.perf_counter() - t_start, 2)

        target_min_dur = 0.0 if config.simulate else float(getattr(config, "min_scan_duration_s", 0.0))

        def _eta(pct: float) -> float:
            el = time.perf_counter() - t_start
            if pct <= 3.0 or el < 0.2:
                if target_min_dur > 0.0:
                    return round(target_min_dur + (25.0 if config.mode == "QUICK" else 60.0), 1)
                return 18.0 if config.mode == "QUICK" else 40.0
            rem = el * (100.0 - pct) / max(1.0, pct)
            if target_min_dur > 0.0 and pct < 95.0:
                rem = max(rem, target_min_dur - el)
            return round(max(0.0, rem), 1)

        try:
            # ------------------------------------------------------------------
            # STAGE 0: Exclusive SDR Lock Acquisition & GPS Locality Fix
            # ------------------------------------------------------------------
            self._emit(
                ScanProgressUpdate(
                    stage=0,
                    stage_name="Stage 0: SDR Lock & GPS Check",
                    percent_complete=2.0,
                    sub_percent=20.0,
                    elapsed_s=_elapsed(),
                    eta_s=_eta(2.0),
                    sdr_owner_badge="SDR: ACQUIRING LOCK",
                    message="Acquiring exclusive 1-at-a-time HackRF One lock & polling u-blox GPS...",
                    current_freq_mhz=float(config.freq_min_mhz),
                    detected_count=0,
                )
            )

            sdr_engine = self._active_sdr_engine()
            if not config.simulate:
                def _handoff_cb(msg: str, sub_pct: float) -> None:
                    self._emit(
                        ScanProgressUpdate(
                            stage=0,
                            stage_name="Stage 0: SDR Handoff",
                            percent_complete=2.0 + sub_pct * 0.05,
                            sub_percent=sub_pct,
                            elapsed_s=_elapsed(),
                            eta_s=_eta(4.0),
                            sdr_owner_badge="SDR: HANDOFF",
                            message=msg,
                            current_freq_mhz=float(config.freq_min_mhz),
                            detected_count=0,
                        )
                    )

                ok_lock, lock_info = self.arbiter.acquire_for_scanner(progress_cb=_handoff_cb)
                if not ok_lock:
                    raise RuntimeError(lock_info)
                sdr_acquired = True
                if hasattr(sdr_engine, "serial_number"):
                    sdr_engine.serial_number = lock_info

            gps_fix = self.gps_mgr.refresh_now()
            logger.info(
                "Starting %s scan (%d-%d MHz) at locality: %s (%s)",
                config.mode,
                config.freq_min_mhz,
                config.freq_max_mhz,
                gps_fix.locality_name,
                gps_fix.maidenhead_grid,
            )

            if self._stop_requested:
                return [], None

            # ------------------------------------------------------------------
            # STAGE 1: Layer 1 Multi-Pass Band-Segmented Full-Spectrum Sweep
            # ------------------------------------------------------------------
            l1_passes = max(1, int(getattr(config, "l1_passes", 3)))
            self.arbiter.set_owner_state(
                "SCANNER_L1",
                f"Layer 1 Multi-Pass Sweep {config.freq_min_mhz}-{config.freq_max_mhz} MHz ({l1_passes} passes)",
                100.0,
            )
            self._emit(
                ScanProgressUpdate(
                    stage=1,
                    stage_name=f"Layer 1: Multi-Pass Sweep ({config.mode})",
                    percent_complete=6.0,
                    sub_percent=5.0,
                    elapsed_s=_elapsed(),
                    eta_s=_eta(6.0),
                    sdr_owner_badge="SDR: L1 MULTI-PASS",
                    message=(
                        f"Sweeping {config.freq_min_mhz}-{config.freq_max_mhz} MHz "
                        f"({l1_passes} passes, Mean+MaxHold, SNR>={config.min_snr_db:.1f}dB, Peak>={config.min_peak_dbfs:.0f}dBFS)..."
                    ),
                    current_freq_mhz=float(config.freq_min_mhz),
                    detected_count=0,
                )
            )

            def _l1_progress(
                pass_i: int,
                tot_passes: int,
                seg_i: int,
                tot_segs: int,
                s_lo: int,
                s_hi: int,
                disp_f: np.ndarray,
                disp_p: np.ndarray,
                disp_fl: np.ndarray,
                bins_so_far: int,
            ) -> None:
                step_done = ((pass_i - 1) * tot_segs + seg_i) / float(max(1, tot_passes * tot_segs))
                sub_p = round(step_done * 100.0, 1)
                pct_p = round(6.0 + step_done * 19.0, 1)
                self._emit(
                    ScanProgressUpdate(
                        stage=1,
                        stage_name=f"Layer 1: Pass {pass_i}/{tot_passes} ({s_lo}-{s_hi} MHz)",
                        percent_complete=pct_p,
                        sub_percent=sub_p,
                        elapsed_s=_elapsed(),
                        eta_s=_eta(pct_p),
                        sdr_owner_badge=f"SDR: L1 P{pass_i}/{tot_passes} {s_hi}MHz",
                        message=(
                            f"Pass {pass_i}/{tot_passes} [{seg_i}/{tot_segs}]: Swept {s_lo}-{s_hi} MHz "
                            f"({bins_so_far:,} fine spectral bins accumulated)..."
                        ),
                        current_freq_mhz=float(s_hi),
                        detected_count=0,
                        spectrum_frequencies=disp_f,
                        spectrum_powers=disp_p,
                        spectrum_floor=disp_fl,
                    )
                )

            freqs, powers, floor, l1_spikes = sdr_engine.sweep_wideband(
                config=config,
                region=gps_fix.region_code,
                stop_check=lambda: self._stop_requested,
                progress_cb=_l1_progress,
            )

            if self._stop_requested or len(freqs) == 0:
                return [], None

            viable_l1: List[DetectedSignalSpike] = []
            for spk in l1_spikes:
                bdef = find_band_definition(spk.center_freq_hz, gps_fix.region_code)
                if config.only_legal_demod and bdef and not bdef.is_legal_demod:
                    continue
                viable_l1.append(spk)

            self._emit(
                ScanProgressUpdate(
                    stage=1,
                    stage_name="Layer 1: Multi-Pass Wideband Complete",
                    percent_complete=26.0,
                    sub_percent=100.0,
                    elapsed_s=_elapsed(),
                    eta_s=_eta(26.0),
                    sdr_owner_badge="SDR: SCANNER L1 DONE",
                    message=(
                        f"Layer 1 ({l1_passes} passes) mapped {len(freqs):,} spectral bins -> "
                        f"{len(viable_l1)} candidate carriers (floor {float(np.median(floor)):.1f} dBFS)"
                    ),
                    current_freq_mhz=float(config.freq_max_mhz),
                    detected_count=len(viable_l1),
                    spectrum_frequencies=freqs[:: max(1, len(freqs) // 4096)],
                    spectrum_powers=powers[:: max(1, len(powers) // 4096)],
                    spectrum_floor=floor[:: max(1, len(floor) // 4096)],
                )
            )

            # If Full-Spectrum hardware scan initially sees quiet spectrum, perform extra confirmation sweeps
            # across the time budget so intermittent PTT/burst traffic is caught before concluding Quiet Spectrum.
            confirm_round = 0
            while (
                not viable_l1
                and not config.simulate
                and target_min_dur > 0.0
                and _elapsed() < target_min_dur
                and not self._stop_requested
            ):
                confirm_round += 1
                pct_c = min(90.0, 26.0 + (_elapsed() / max(1.0, target_min_dur)) * 64.0)
                self._emit(
                    ScanProgressUpdate(
                        stage=1,
                        stage_name=f"Layer 1: Deep Confirmation Sweep #{confirm_round}",
                        percent_complete=round(pct_c, 1),
                        sub_percent=round(min(99.0, (_elapsed() / max(1.0, target_min_dur)) * 100.0), 1),
                        elapsed_s=_elapsed(),
                        eta_s=_eta(pct_c),
                        sdr_owner_badge=f"SDR: L1 CONFIRM #{confirm_round}",
                        message=(
                            f"Quiet initial pass — running confirmation sweep #{confirm_round} "
                            f"({config.freq_min_mhz}-{config.freq_max_mhz} MHz) watching for intermittent PTT/bursts..."
                        ),
                        current_freq_mhz=float(config.freq_min_mhz),
                        detected_count=0,
                    )
                )
                freqs_c, powers_c, floor_c, spikes_c = sdr_engine.sweep_wideband(
                    config=config,
                    region=gps_fix.region_code,
                    stop_check=lambda: self._stop_requested,
                    progress_cb=None,
                )
                if len(freqs_c) == len(freqs):
                    powers = np.maximum(powers, powers_c)
                    floor = 0.5 * (floor + floor_c)
                for spk in spikes_c:
                    bdef = find_band_definition(spk.center_freq_hz, gps_fix.region_code)
                    if config.only_legal_demod and bdef and not bdef.is_legal_demod:
                        continue
                    viable_l1.append(spk)

            if not viable_l1:
                # Still save an empty/quiet-band Location Profile so field survey record is preserved
                final_profiles, saved_loc = self.organizer.organize_and_save_location_profile(
                    layer3_signals=[],
                    gps_fix=gps_fix,
                    scan_config=config,
                    scan_duration_s=_elapsed(),
                )
                self.latest_results = final_profiles
                self.latest_saved_profile = saved_loc
                self._emit(
                    ScanProgressUpdate(
                        stage=4,
                        stage_name="SCAN COMPLETE — QUIET SPECTRUM",
                        percent_complete=100.0,
                        sub_percent=100.0,
                        elapsed_s=_elapsed(),
                        eta_s=0.0,
                        sdr_owner_badge="SDR: IDLE (RELEASED)",
                        message=(
                            f"No signals exceeded SNR>={config.min_snr_db:.1f} dB & "
                            f"Peak>={config.min_peak_dbfs:.0f} dBFS. Try lowering thresholds or Deep Scan."
                        ),
                        current_freq_mhz=0.0,
                        detected_count=0,
                        spectrum_frequencies=freqs,
                        spectrum_powers=powers,
                        spectrum_floor=floor,
                        saved_profile=saved_loc,
                    )
                )
                return [], saved_loc

            # ------------------------------------------------------------------
            # STAGE 2: Layer 2 Multi-Pass Fine Zoom & Channel Raster Refinement
            # ------------------------------------------------------------------
            l2_passes = max(1, int(getattr(config, "l2_passes", 2)))
            self.arbiter.set_owner_state(
                "SCANNER_L2",
                f"Layer 2 Multi-Pass Zoom ({len(viable_l1)} candidates, {l2_passes} passes @ {config.l2_bin_width_hz / 1000:.1f} kHz)",
                100.0,
            )

            def _l2_progress(win_idx: int, total_wins: int, cf_mhz: float) -> None:
                sub = (win_idx / max(1, total_wins)) * 100.0
                pct = 26.0 + (sub * 0.12)
                self._emit(
                    ScanProgressUpdate(
                        stage=2,
                        stage_name=f"Layer 2: Multi-Pass Zoom ({win_idx}/{total_wins})",
                        percent_complete=round(pct, 1),
                        sub_percent=round(sub, 1),
                        elapsed_s=_elapsed(),
                        eta_s=_eta(pct),
                        sdr_owner_badge=f"SDR: L2 ZOOM {cf_mhz:.2f}MHz",
                        message=(
                            f"Refining zoom cluster {win_idx}/{total_wins} around {cf_mhz:.3f} MHz "
                            f"({l2_passes} passes @ {config.l2_bin_width_hz / 1000:.1f} kHz)..."
                        ),
                        current_freq_mhz=cf_mhz,
                        detected_count=len(viable_l1),
                    )
                )

            refined_l2 = sdr_engine.zoom_refine_candidates(
                candidates=viable_l1,
                config=config,
                region=gps_fix.region_code,
                progress_cb=_l2_progress,
                stop_check=lambda: self._stop_requested,
            )

            if self._stop_requested:
                return [], None

            self._emit(
                ScanProgressUpdate(
                    stage=2,
                    stage_name="Layer 2: Multi-Pass Zoom Complete",
                    percent_complete=38.0,
                    sub_percent=100.0,
                    elapsed_s=_elapsed(),
                    eta_s=_eta(38.0),
                    sdr_owner_badge="SDR: SCANNER L2 DONE",
                    message=f"Layer 2 verified & channel-aligned {len(refined_l2)} clean carriers (spurs rejected).",
                    current_freq_mhz=refined_l2[-1].center_freq_mhz if refined_l2 else 0.0,
                    detected_count=len(refined_l2),
                )
            )

            # ------------------------------------------------------------------
            # STAGE 3A: Pass 1 Dual-Scale 1MHz + Ultra-Zoom (7.63 Hz/bin) IQ & Hailo-8 AI
            # ------------------------------------------------------------------
            layer3_records: List[Dict[str, Any]] = []
            total_cands = len(refined_l2)
            req_frames = max(3, int(config.l3_frames_per_mhz))
            cap_dur_s = float(getattr(config, "iq_capture_s", 1.25))

            def _format_situational_notes(rec_d: Dict[str, Any]) -> str:
                parts: List[str] = [
                    f"{rec_d['dwell_passes']}p/{rec_d['dwell_time_s']:.1f}s ({rec_d['num_frames']}f @{rec_d['zoom_resolution_hz']:.1f}Hz)"
                ]
                if rec_d.get("stereo_pilot"):
                    parts.append("19kHz-Stereo")
                if float(rec_d.get("ctcss_hz", 0.0)) >= 60.0:
                    parts.append(f"CTCSS {float(rec_d['ctcss_hz']):.1f}Hz")
                fsk_sh = float(rec_d.get("_fsk_shift_hz", 0.0))
                if fsk_sh >= 1200.0:
                    parts.append(f"FSK-Shift {fsk_sh / 1000.0:.2f}kHz")
                duty_v = float(rec_d.get("duty_cycle", 1.0))
                if duty_v < 0.82:
                    parts.append(f"Burst {int(round(duty_v * 100))}%-duty")
                off_hz = float(rec_d.get("carrier_offset_hz", 0.0))
                if abs(off_hz) >= 5.0:
                    parts.append(f"Δ{off_hz:+.0f}Hz")
                else:
                    parts.append("0Hz-Lock")
                return " | ".join(parts)

            for idx, cand in enumerate(refined_l2):
                if self._stop_requested:
                    return [], None

                sub_start = (idx / max(1, total_cands)) * 100.0
                pct_start = 38.0 + (idx / max(1, total_cands)) * 28.0
                self.arbiter.set_owner_state(
                    "SCANNER_L3",
                    f"Layer 3 Pass 1 Ultra-Zoom [{idx + 1}/{total_cands}] @ {cand.center_freq_mhz:.4f} MHz",
                    sub_start,
                )
                self._emit(
                    ScanProgressUpdate(
                        stage=3,
                        stage_name=f"Layer 3 Pass 1: Ultra-Zoom IQ ({idx + 1}/{total_cands})",
                        percent_complete=round(pct_start, 1),
                        sub_percent=round(sub_start, 1),
                        elapsed_s=_elapsed(),
                        eta_s=_eta(pct_start),
                        sdr_owner_badge=f"SDR: L3 P1 {cand.center_freq_mhz:.3f}MHz",
                        message=(
                            f"[Pass 1 | {idx + 1}/{total_cands}] Capturing {cap_dur_s:.2f}s @ 2MSPS "
                            f"(7.6Hz FFT + {req_frames * 2} Dual-Scale frames) at {cand.center_freq_mhz:.4f} MHz..."
                        ),
                        current_freq_mhz=cand.center_freq_mhz,
                        detected_count=len(layer3_records),
                    )
                )

                t_cap0 = time.perf_counter()
                frames, iq_metrics = sdr_engine.capture_1mhz_pictogram_frames(
                    center_freq_hz=cand.center_freq_hz,
                    num_frames=req_frames,
                    rows_per_frame=64,
                    cols_per_frame=128,
                    lna_gain_db=config.lna_gain_db,
                    vga_gain_db=config.vga_gain_db,
                    amp_enable=config.amp_enable,
                    simulate=config.simulate,
                    stop_check=lambda: self._stop_requested,
                    capture_duration_s=cap_dur_s,
                )
                cap_elapsed = max(0.05, time.perf_counter() - t_cap0)

                if self._stop_requested or not frames:
                    return [], None

                true_obw_check = float(iq_metrics.get("obw_hz", cand.estimated_bandwidth_hz))
                if not config.simulate and is_likely_internal_clock_spur(
                    freq_hz=cand.raw_measured_hz,
                    obw_hz=true_obw_check,
                    iq_mod_hint=str(iq_metrics.get("iq_mod_hint", "")),
                    freq_dev_hz=float(iq_metrics.get("freq_dev_85_hz", 0.0)),
                ):
                    logger.info(
                        "Layer 3 filtered unmodulated internal clock harmonic spur at %.4f MHz (OBW=%.0f Hz)",
                        cand.center_freq_mhz,
                        true_obw_check,
                    )
                    continue

                iq_metrics["ref_floor_dbfs"] = cand.noise_floor_dbm
                picto_result = self.picto_mgr.analyze_spectrogram_frames(
                    center_freq_hz=cand.center_freq_hz,
                    frames=frames,
                    bandwidth_hz=1_000_000.0,
                    iq_metrics=iq_metrics,
                    prior_class_scores=None,
                    dwell_passes=1,
                )

                if picto_result.is_noise_rejected:
                    logger.info(
                        "Layer 3 Hailo-8 noise_reject filtered transient artifact at %.4f MHz",
                        cand.center_freq_mhz,
                    )
                    continue

                true_peak_dbfs = float(iq_metrics.get("peak_dbfs", cand.peak_dbm))
                true_floor_dbfs = float(iq_metrics.get("noise_floor_dbfs", cand.noise_floor_dbm))
                true_snr_db = max(
                    cand.snr_db * 0.35 + float(iq_metrics.get("snr_db", cand.snr_db)) * 0.65,
                    float(iq_metrics.get("snr_db", cand.snr_db)),
                )
                true_obw_hz = float(iq_metrics.get("obw_hz", cand.estimated_bandwidth_hz))
                prec_carrier_hz = float(iq_metrics.get("precision_carrier_hz", cand.raw_measured_hz))
                snapped_hz, off_hz = refine_precision_channel_frequency(
                    measured_carrier_hz=prec_carrier_hz,
                    obw_hz=true_obw_hz,
                    iq_mod_hint=str(iq_metrics.get("iq_mod_hint", "NFM")),
                    region=gps_fix.region_code,
                )
                fine_res_hz = float(iq_metrics.get("fine_fft_resolution_hz", 7.629))

                rec: Dict[str, Any] = {
                    "center_freq_hz": snapped_hz,
                    "center_freq_mhz": round(snapped_hz / 1e6, 6),
                    "raw_measured_hz": round(prec_carrier_hz, 1),
                    "carrier_offset_hz": round(off_hz, 1),
                    "peak_dbm": round(true_peak_dbfs, 1),
                    "noise_floor_dbm": round(true_floor_dbfs, 1),
                    "snr_db": round(true_snr_db, 1),
                    "estimated_bandwidth_hz": round(true_obw_hz, 0),
                    "iq_mod_hint": str(iq_metrics.get("iq_mod_hint", "NFM")),
                    "freq_dev_hz": float(iq_metrics.get("freq_dev_85_hz", 0.0)),
                    "duty_cycle": float(iq_metrics.get("duty_cycle", 1.0)),
                    "picto_category": picto_result.category,
                    "picto_neural_class": picto_result.raw_neural_class,
                    "picto_label": picto_result.label,
                    "picto_confidence": picto_result.confidence,
                    "hailo_accelerated": picto_result.hardware_accelerated,
                    "num_frames": int(picto_result.frames_evaluated),
                    "dwell_passes": 1,
                    "dwell_time_s": round(cap_elapsed if not config.simulate else cap_dur_s, 2),
                    "zoom_resolution_hz": round(fine_res_hz, 2),
                    "ctcss_hz": float(iq_metrics.get("ctcss_hz", 0.0)),
                    "stereo_pilot": bool(iq_metrics.get("stereo_pilot", False)),
                    "composite_rgb": picto_result.composite_rgb,
                    "timestamp_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "_accum_scores": picto_result.accumulated_scores,
                    "_carrier_samples": [(prec_carrier_hz, max(1.0, true_snr_db))],
                    "_fsk_shift_hz": float(iq_metrics.get("fsk_shift_hz", 0.0)),
                }
                rec["situational_notes"] = _format_situational_notes(rec)
                layer3_records.append(rec)

                sub_done = ((idx + 1) / max(1, total_cands)) * 100.0
                pct_done = 38.0 + ((idx + 1) / max(1, total_cands)) * 28.0
                self._emit(
                    ScanProgressUpdate(
                        stage=3,
                        stage_name=f"Layer 3 Pass 1: AI Classified ({idx + 1}/{total_cands})",
                        percent_complete=round(pct_done, 1),
                        sub_percent=round(sub_done, 1),
                        elapsed_s=_elapsed(),
                        eta_s=_eta(pct_done),
                        sdr_owner_badge=f"SDR: L3 P1 {rec['center_freq_mhz']:.4f}MHz",
                        message=(
                            f"{rec['center_freq_mhz']:.4f} MHz ({rec['num_frames']} frames @ {fine_res_hz:.1f}Hz, "
                            f"+{true_snr_db:.1f}dB) -> {picto_result.label} [{int(picto_result.confidence * 100)}%]"
                        ),
                        current_freq_mhz=float(rec["center_freq_mhz"]),
                        detected_count=len(layer3_records),
                        latest_frame=frames[-1] if frames else None,
                        latest_composite_rgb=picto_result.composite_rgb,
                        latest_pictogram=picto_result,
                    )
                )

            # ------------------------------------------------------------------
            # STAGE 3B: Multi-Pass High-Confidence Real-Time Dwell & Sub-10 Hz Lock
            # ------------------------------------------------------------------
            base_dwell_passes = max(1, int(getattr(config, "dwell_passes", 3)))
            high_conf_records = [
                r for r in layer3_records
                if float(r.get("picto_confidence", 0.0)) >= 0.50 or float(r.get("snr_db", 0.0)) >= 8.0
            ]
            if not high_conf_records and layer3_records:
                high_conf_records = list(layer3_records)

            def _run_single_dwell_pass(rec_item: Dict[str, Any], pass_no: int, step_idx: int, step_tot: int) -> bool:
                if self._stop_requested:
                    return False
                cf_hz = float(rec_item["center_freq_hz"])
                cf_mhz = cf_hz / 1e6
                sub_p = (step_idx / max(1, step_tot)) * 100.0
                if target_min_dur > 0.0:
                    pct_p = min(94.0, max(66.0 + sub_p * 0.28, min(94.0, (_elapsed() / target_min_dur) * 94.0)))
                else:
                    pct_p = 66.0 + sub_p * 0.28

                self.arbiter.set_owner_state(
                    "SCANNER_L3",
                    f"Layer 3 Dwell Pass #{pass_no} @ {cf_mhz:.4f} MHz ({rec_item['picto_label']})",
                    sub_p,
                )
                self._emit(
                    ScanProgressUpdate(
                        stage=3,
                        stage_name=f"Layer 3: High-Conf Dwell Pass #{pass_no}",
                        percent_complete=round(pct_p, 1),
                        sub_percent=round(min(99.9, sub_p), 1),
                        elapsed_s=_elapsed(),
                        eta_s=_eta(pct_p),
                        sdr_owner_badge=f"SDR: DWELL P{pass_no} {cf_mhz:.3f}MHz",
                        message=(
                            f"[Dwell Pass #{pass_no}] Watching {cf_mhz:.4f} MHz ({rec_item['picto_label']}) "
                            f"at 7.6Hz/244Hz zoom to lock carrier & subcarriers..."
                        ),
                        current_freq_mhz=cf_mhz,
                        detected_count=len(layer3_records),
                    )
                )

                t_dw0 = time.perf_counter()
                dw_frames, dw_metrics = sdr_engine.capture_1mhz_pictogram_frames(
                    center_freq_hz=cf_hz,
                    num_frames=req_frames,
                    rows_per_frame=64,
                    cols_per_frame=128,
                    lna_gain_db=config.lna_gain_db,
                    vga_gain_db=config.vga_gain_db,
                    amp_enable=config.amp_enable,
                    simulate=config.simulate,
                    stop_check=lambda: self._stop_requested,
                    capture_duration_s=cap_dur_s,
                )
                dw_elapsed = max(0.05, time.perf_counter() - t_dw0)
                if self._stop_requested or not dw_frames:
                    return False

                dw_metrics["ref_floor_dbfs"] = rec_item["noise_floor_dbm"]
                new_pass_count = int(rec_item["dwell_passes"]) + 1
                dw_picto = self.picto_mgr.analyze_spectrogram_frames(
                    center_freq_hz=cf_hz,
                    frames=dw_frames,
                    bandwidth_hz=1_000_000.0,
                    iq_metrics=dw_metrics,
                    prior_class_scores=rec_item.get("_accum_scores"),
                    dwell_passes=new_pass_count,
                )

                dw_snr = float(dw_metrics.get("snr_db", rec_item["snr_db"]))
                dw_peak = float(dw_metrics.get("peak_dbfs", rec_item["peak_dbm"]))
                dw_prec_hz = float(dw_metrics.get("precision_carrier_hz", rec_item["raw_measured_hz"]))

                rec_item["_accum_scores"] = dw_picto.accumulated_scores
                rec_item["dwell_passes"] = new_pass_count
                rec_item["dwell_time_s"] = round(
                    float(rec_item["dwell_time_s"]) + (dw_elapsed if not config.simulate else cap_dur_s),
                    2,
                )
                rec_item["num_frames"] = int(rec_item["num_frames"]) + int(dw_picto.frames_evaluated)

                # SNR-weighted multi-pass sub-10 Hz carrier frequency fusion
                samples: List[Tuple[float, float]] = rec_item.setdefault("_carrier_samples", [])
                if dw_snr >= 4.5:
                    samples.append((dw_prec_hz, max(1.0, dw_snr)))
                w_sum = sum(w for _, w in samples) or 1.0
                fused_carrier_hz = sum(f_v * w for f_v, w in samples) / w_sum

                # Update peak/SNR/OBW with max-hold + weighted average so burst traffic keeps active SNR
                rec_item["peak_dbm"] = round(max(float(rec_item["peak_dbm"]), dw_peak), 1)
                rec_item["snr_db"] = round(max(float(rec_item["snr_db"]), dw_snr), 1)
                if dw_snr >= float(rec_item["snr_db"]) - 3.0:
                    rec_item["estimated_bandwidth_hz"] = round(
                        0.5 * float(rec_item["estimated_bandwidth_hz"]) + 0.5 * float(dw_metrics.get("obw_hz", rec_item["estimated_bandwidth_hz"])),
                        0,
                    )
                    rec_item["iq_mod_hint"] = str(dw_metrics.get("iq_mod_hint", rec_item["iq_mod_hint"]))
                    rec_item["freq_dev_hz"] = max(float(rec_item.get("freq_dev_hz", 0.0)), float(dw_metrics.get("freq_dev_85_hz", 0.0)))

                rec_item["duty_cycle"] = round(
                    0.5 * float(rec_item["duty_cycle"]) + 0.5 * float(dw_metrics.get("duty_cycle", rec_item["duty_cycle"])),
                    2,
                )
                if float(dw_metrics.get("ctcss_hz", 0.0)) >= 60.0:
                    rec_item["ctcss_hz"] = float(dw_metrics["ctcss_hz"])
                if bool(dw_metrics.get("stereo_pilot", False)):
                    rec_item["stereo_pilot"] = True
                if float(dw_metrics.get("fsk_shift_hz", 0.0)) >= 1200.0:
                    rec_item["_fsk_shift_hz"] = float(dw_metrics["fsk_shift_hz"])

                snapped_dw_hz, off_dw_hz = refine_precision_channel_frequency(
                    measured_carrier_hz=fused_carrier_hz,
                    obw_hz=float(rec_item["estimated_bandwidth_hz"]),
                    iq_mod_hint=str(rec_item["iq_mod_hint"]),
                    region=gps_fix.region_code,
                )
                rec_item["center_freq_hz"] = snapped_dw_hz
                rec_item["center_freq_mhz"] = round(snapped_dw_hz / 1e6, 6)
                rec_item["raw_measured_hz"] = round(fused_carrier_hz, 1)
                rec_item["carrier_offset_hz"] = round(off_dw_hz, 1)

                if not dw_picto.is_noise_rejected:
                    rec_item["picto_category"] = dw_picto.category
                    rec_item["picto_neural_class"] = dw_picto.raw_neural_class
                    rec_item["picto_label"] = dw_picto.label
                    rec_item["picto_confidence"] = dw_picto.confidence
                    rec_item["hailo_accelerated"] = bool(rec_item["hailo_accelerated"] or dw_picto.hardware_accelerated)
                    rec_item["composite_rgb"] = dw_picto.composite_rgb

                rec_item["situational_notes"] = _format_situational_notes(rec_item)

                self._emit(
                    ScanProgressUpdate(
                        stage=3,
                        stage_name=f"Layer 3: Dwell Pass #{new_pass_count} Locked",
                        percent_complete=round(min(94.5, pct_p + 0.5), 1),
                        sub_percent=round(min(100.0, sub_p), 1),
                        elapsed_s=_elapsed(),
                        eta_s=_eta(min(94.5, pct_p + 0.5)),
                        sdr_owner_badge=f"SDR: DWELL P{new_pass_count} {rec_item['center_freq_mhz']:.4f}MHz",
                        message=(
                            f"[Pass #{new_pass_count}] {rec_item['center_freq_mhz']:.4f} MHz -> {rec_item['picto_label']} "
                            f"[{int(rec_item['picto_confidence'] * 100)}%] ({rec_item['situational_notes']})"
                        ),
                        current_freq_mhz=float(rec_item["center_freq_mhz"]),
                        detected_count=len(layer3_records),
                        latest_frame=dw_frames[-1] if dw_frames else None,
                        latest_composite_rgb=rec_item["composite_rgb"],
                        latest_pictogram=dw_picto,
                    )
                )
                return True

            # Scheduled multi-pass dwell (Passes 2 .. base_dwell_passes)
            max_total_scan_s = 540.0  # Keep even massive environments cleanly under 9 minutes
            if high_conf_records and base_dwell_passes >= 2:
                planned_steps = (base_dwell_passes - 1) * len(high_conf_records)
                step_counter = 0
                for p_no in range(2, base_dwell_passes + 1):
                    for rec_item in high_conf_records:
                        if self._stop_requested or (not config.simulate and _elapsed() >= max_total_scan_s):
                            break
                        step_counter += 1
                        if not _run_single_dwell_pass(rec_item, p_no, step_counter, planned_steps):
                            return [], None

            # Adaptive High-Confidence Deep Dwell Budget: if Full-Spectrum scan finished scheduled passes
            # earlier than target_min_dur (>=130s QUICK / >=340s DEEP), keep dwelling round-robin on the
            # highest-confidence real-time carriers to maximize frame count & situational awareness!
            if (
                high_conf_records
                and not config.simulate
                and target_min_dur > 0.0
                and _elapsed() < target_min_dur
                and not self._stop_requested
            ):
                priority_dwell = sorted(
                    high_conf_records,
                    key=lambda r: (float(r.get("picto_confidence", 0.0)), float(r.get("snr_db", 0.0))),
                    reverse=True,
                )
                extra_idx = 0
                while _elapsed() < target_min_dur and _elapsed() < max_total_scan_s and not self._stop_requested:
                    rec_item = priority_dwell[extra_idx % len(priority_dwell)]
                    extra_idx += 1
                    next_p = int(rec_item["dwell_passes"]) + 1
                    prog_frac = min(1.0, _elapsed() / max(1.0, target_min_dur))
                    if not _run_single_dwell_pass(rec_item, next_p, int(prog_frac * 100), 100):
                        if self._stop_requested:
                            return [], None
                        break

            # Deduplicate any carriers that converged onto the exact same snapped channel raster during multi-pass lock
            dedup_by_chan: Dict[int, Dict[str, Any]] = {}
            for r in layer3_records:
                r.pop("_accum_scores", None)
                r.pop("_carrier_samples", None)
                r.pop("_fsk_shift_hz", None)
                ch_key = int(round(float(r["center_freq_hz"]) / 100.0))
                if ch_key not in dedup_by_chan:
                    dedup_by_chan[ch_key] = r
                else:
                    prev = dedup_by_chan[ch_key]
                    prev["num_frames"] = int(prev["num_frames"]) + int(r["num_frames"])
                    prev["dwell_passes"] = max(int(prev["dwell_passes"]), int(r["dwell_passes"]))
                    prev["dwell_time_s"] = round(float(prev["dwell_time_s"]) + float(r["dwell_time_s"]), 2)
                    if float(r["snr_db"]) > float(prev["snr_db"]):
                        r["num_frames"] = prev["num_frames"]
                        r["dwell_passes"] = prev["dwell_passes"]
                        r["dwell_time_s"] = prev["dwell_time_s"]
                        dedup_by_chan[ch_key] = r
                    dedup_by_chan[ch_key]["situational_notes"] = _format_situational_notes(dedup_by_chan[ch_key])

            layer3_records = sorted(dedup_by_chan.values(), key=lambda x: float(x["center_freq_hz"]))

            # Release HackRF BEFORE unlocking clickable UI so Gqrx has instant exclusive access!
            if sdr_acquired:
                self.arbiter.release_from_scanner()
                sdr_acquired = False

            # ------------------------------------------------------------------
            # STAGE 4: Locality Profile Organizer & Permanent Disk Persistence
            # ------------------------------------------------------------------
            self._emit(
                ScanProgressUpdate(
                    stage=4,
                    stage_name="Stage 4: Saving Location Profile",
                    percent_complete=96.0,
                    sub_percent=60.0,
                    elapsed_s=_elapsed(),
                    eta_s=0.4,
                    sdr_owner_badge="SDR: RELEASED (READY FOR GQRX)",
                    message="Saving permanent timestamped Location Profile & precision Gqrx clickable links...",
                    current_freq_mhz=0.0,
                    detected_count=len(layer3_records),
                )
            )

            final_profiles, saved_loc = self.organizer.organize_and_save_location_profile(
                layer3_signals=layer3_records,
                gps_fix=gps_fix,
                scan_config=config,
                scan_duration_s=_elapsed(),
            )
            self.latest_results = final_profiles
            self.latest_saved_profile = saved_loc

            total_frames_all = sum(int(getattr(p, "frames_evaluated", getattr(p, "num_frames", 3))) for p in final_profiles)
            self._emit(
                ScanProgressUpdate(
                    stage=4,
                    stage_name="SCAN COMPLETE — SIGNALS UNLOCKED FOR GQRX",
                    percent_complete=100.0,
                    sub_percent=100.0,
                    elapsed_s=_elapsed(),
                    eta_s=0.0,
                    sdr_owner_badge="SDR: READY FOR GQRX",
                    message=(
                        f"Saved Profile [{saved_loc.approx_datetime_local}]: {len(final_profiles)} signals "
                        f"({total_frames_all:,} zoom frames in {_elapsed():.1f}s). Click any frequency to tune Gqrx!"
                    ),
                    current_freq_mhz=0.0,
                    detected_count=len(final_profiles),
                    spectrum_frequencies=freqs,
                    spectrum_powers=powers,
                    spectrum_floor=floor,
                    saved_profile=saved_loc,
                )
            )
            return final_profiles, saved_loc

        finally:
            if sdr_acquired:
                self.arbiter.release_from_scanner()
            with self._lock:
                self._is_scanning = False

    # Convenience method (defaults to Full-Spectrum 1 - 6000 MHz!)
    def run_scan(
        self,
        freq_min_mhz: int = 1,
        freq_max_mhz: int = 6000,
        snr_threshold_db: float = 8.5,
        only_legal_demod: bool = True,
        mode: str = "QUICK",
    ) -> List[ClassifiedSignalProfile]:
        cfg = ScanConfig.from_mode(
            mode=mode,
            freq_min_mhz=freq_min_mhz,
            freq_max_mhz=freq_max_mhz,
            min_snr_db=snr_threshold_db,
            only_legal_demod=only_legal_demod,
        )
        # Keep narrow sub-band programmatic calls snappy while Full-Spectrum runs full multi-pass budget
        if (freq_max_mhz - freq_min_mhz) <= 60:
            cfg.dwell_passes = min(cfg.dwell_passes, 2)
            cfg.iq_capture_s = min(cfg.iq_capture_s, 0.75)
        profiles, _ = self.run_scan_with_config(cfg)
        return profiles
