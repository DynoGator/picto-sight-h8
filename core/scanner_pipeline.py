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

from core.bandplans import find_band_definition
from core.gps_engine import get_gps_manager, GPSLocalityFix
from core.hackrf_engine import (
    HackRFScannerEngine,
    DetectedSignalSpike,
    ScanConfig,
    is_likely_internal_clock_spur,
)
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
        self.gps_mgr = get_gps_manager()
        self.picto_mgr = get_pictogram_classifier()
        self.organizer = SignalProfileOrganizer()

        self._stop_requested = False
        self._is_scanning = False
        self._lock = threading.Lock()
        self.latest_results: List[ClassifiedSignalProfile] = []
        self.latest_saved_profile: Optional[SavedLocationProfile] = None

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

        def _eta(pct: float) -> float:
            el = time.perf_counter() - t_start
            if pct <= 3.0 or el < 0.2:
                return 8.0 if config.mode == "QUICK" else 16.0
            rem = el * (100.0 - pct) / max(1.0, pct)
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
                self.hackrf.serial_number = lock_info

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
            # STAGE 1: Layer 1 Full Spectrum Wideband Sweep
            # ------------------------------------------------------------------
            self.arbiter.set_owner_state(
                "SCANNER_L1",
                f"Layer 1 Sweep {config.freq_min_mhz}-{config.freq_max_mhz} MHz ({config.mode})",
                100.0,
            )
            self._emit(
                ScanProgressUpdate(
                    stage=1,
                    stage_name=f"Layer 1: Wideband Sweep ({config.mode})",
                    percent_complete=9.0,
                    sub_percent=15.0,
                    elapsed_s=_elapsed(),
                    eta_s=_eta(9.0),
                    sdr_owner_badge="SDR: SCANNER L1 SWEEP",
                    message=(
                        f"Sweeping {config.freq_min_mhz}-{config.freq_max_mhz} MHz "
                        f"({config.l1_bin_width_hz // 1000} kHz bins, {config.l1_sweeps}x avg, "
                        f"SNR>={config.min_snr_db:.1f}dB, Peak>={config.min_peak_dbfs:.0f}dBFS)..."
                    ),
                    current_freq_mhz=float(config.freq_min_mhz),
                    detected_count=0,
                )
            )

            freqs, powers, floor, l1_spikes = self.hackrf.sweep_wideband(
                config=config,
                region=gps_fix.region_code,
                stop_check=lambda: self._stop_requested,
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
                    stage_name="Layer 1: Wideband Complete",
                    percent_complete=28.0,
                    sub_percent=100.0,
                    elapsed_s=_elapsed(),
                    eta_s=_eta(28.0),
                    sdr_owner_badge="SDR: SCANNER L1 DONE",
                    message=(
                        f"Layer 1 mapped {len(freqs)} spectral bins -> {len(viable_l1)} candidate carriers "
                        f"(median floor {float(np.median(floor)):.1f} dBFS)"
                    ),
                    current_freq_mhz=float(config.freq_max_mhz),
                    detected_count=len(viable_l1),
                    spectrum_frequencies=freqs,
                    spectrum_powers=powers,
                    spectrum_floor=floor,
                )
            )

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
            # STAGE 2: Layer 2 Incremental Zoom & Channel Raster Refinement
            # ------------------------------------------------------------------
            self.arbiter.set_owner_state(
                "SCANNER_L2",
                f"Layer 2 Fine Zoom ({len(viable_l1)} candidates @ {config.l2_bin_width_hz / 1000:.1f} kHz)",
                100.0,
            )

            def _l2_progress(win_idx: int, total_wins: int, cf_mhz: float) -> None:
                sub = (win_idx / max(1, total_wins)) * 100.0
                pct = 28.0 + (sub * 0.22)
                self._emit(
                    ScanProgressUpdate(
                        stage=2,
                        stage_name=f"Layer 2: Fine Zoom ({win_idx}/{total_wins})",
                        percent_complete=pct,
                        sub_percent=sub,
                        elapsed_s=_elapsed(),
                        eta_s=_eta(pct),
                        sdr_owner_badge=f"SDR: L2 ZOOM {cf_mhz:.2f}MHz",
                        message=(
                            f"Refining zoom cluster {win_idx}/{total_wins} around {cf_mhz:.3f} MHz "
                            f"({config.l2_bin_width_hz / 1000:.1f} kHz resolution)..."
                        ),
                        current_freq_mhz=cf_mhz,
                        detected_count=len(viable_l1),
                    )
                )

            refined_l2 = self.hackrf.zoom_refine_candidates(
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
                    stage_name="Layer 2: Zoom Refinement Complete",
                    percent_complete=52.0,
                    sub_percent=100.0,
                    elapsed_s=_elapsed(),
                    eta_s=_eta(52.0),
                    sdr_owner_badge="SDR: SCANNER L2 DONE",
                    message=f"Layer 2 verified & channel-aligned {len(refined_l2)} clean carriers (spurs rejected).",
                    current_freq_mhz=refined_l2[-1].center_freq_mhz if refined_l2 else 0.0,
                    detected_count=len(refined_l2),
                )
            )

            # ------------------------------------------------------------------
            # STAGE 3: Layer 3 True 1.000 MHz IQ Capture (>=3 Frames) & Hailo-8 AI
            # ------------------------------------------------------------------
            layer3_records: List[Dict[str, Any]] = []
            total_cands = len(refined_l2)
            req_frames = max(3, int(config.l3_frames_per_mhz))

            for idx, cand in enumerate(refined_l2):
                if self._stop_requested:
                    return [], None

                sub_start = (idx / max(1, total_cands)) * 100.0
                pct_start = 52.0 + (idx / max(1, total_cands)) * 40.0
                self.arbiter.set_owner_state(
                    "SCANNER_L3",
                    f"Layer 3 1MHz IQ + Hailo-8 [{idx + 1}/{total_cands}] @ {cand.center_freq_mhz:.4f} MHz",
                    sub_start,
                )
                self._emit(
                    ScanProgressUpdate(
                        stage=3,
                        stage_name=f"Layer 3: 1MHz IQ + AI ({idx + 1}/{total_cands})",
                        percent_complete=pct_start,
                        sub_percent=sub_start,
                        elapsed_s=_elapsed(),
                        eta_s=_eta(pct_start),
                        sdr_owner_badge=f"SDR: L3 IQ {cand.center_freq_mhz:.3f}MHz",
                        message=(
                            f"[{idx + 1}/{total_cands}] Capturing {req_frames}x 1.000 MHz IQ STFT frames "
                            f"at {cand.center_freq_mhz:.4f} MHz..."
                        ),
                        current_freq_mhz=cand.center_freq_mhz,
                        detected_count=len(layer3_records),
                    )
                )

                frames, iq_metrics = self.hackrf.capture_1mhz_pictogram_frames(
                    center_freq_hz=cand.center_freq_hz,
                    num_frames=req_frames,
                    rows_per_frame=64,
                    cols_per_frame=128,
                    lna_gain_db=config.lna_gain_db,
                    vga_gain_db=config.vga_gain_db,
                    amp_enable=config.amp_enable,
                    simulate=config.simulate,
                    stop_check=lambda: self._stop_requested,
                )

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

                picto_result = self.picto_mgr.analyze_spectrogram_frames(
                    center_freq_hz=cand.center_freq_hz,
                    frames=frames,
                    bandwidth_hz=1_000_000.0,
                    iq_metrics=iq_metrics,
                )

                if picto_result.is_noise_rejected:
                    logger.info(
                        "Layer 3 Hailo-8 noise_reject filtered transient artifact at %.4f MHz",
                        cand.center_freq_mhz,
                    )
                    continue

                # Fuse calibrated Layer 3 IQ dBFS/OBW measurements with Layer 2 carrier info
                true_peak_dbfs = float(iq_metrics.get("peak_dbfs", cand.peak_dbm))
                true_floor_dbfs = float(iq_metrics.get("noise_floor_dbfs", cand.noise_floor_dbm))
                true_snr_db = max(cand.snr_db * 0.4 + float(iq_metrics.get("snr_db", cand.snr_db)) * 0.6, float(iq_metrics.get("snr_db", cand.snr_db)))
                true_obw_hz = float(iq_metrics.get("obw_hz", cand.estimated_bandwidth_hz))

                rec = {
                    "center_freq_hz": cand.center_freq_hz,
                    "center_freq_mhz": cand.center_freq_mhz,
                    "raw_measured_hz": cand.raw_measured_hz,
                    "carrier_offset_hz": cand.carrier_offset_hz,
                    "peak_dbm": round(true_peak_dbfs, 1),
                    "noise_floor_dbm": round(true_floor_dbfs, 1),
                    "snr_db": round(true_snr_db, 1),
                    "estimated_bandwidth_hz": round(true_obw_hz, 0),
                    "iq_mod_hint": iq_metrics.get("iq_mod_hint", "NFM"),
                    "duty_cycle": iq_metrics.get("duty_cycle", 1.0),
                    "picto_category": picto_result.category,
                    "picto_neural_class": picto_result.raw_neural_class,
                    "picto_label": picto_result.label,
                    "picto_confidence": picto_result.confidence,
                    "hailo_accelerated": picto_result.hardware_accelerated,
                    "num_frames": len(frames),
                    "composite_rgb": picto_result.composite_rgb,
                    "timestamp_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
                layer3_records.append(rec)

                sub_done = ((idx + 1) / max(1, total_cands)) * 100.0
                pct_done = 52.0 + ((idx + 1) / max(1, total_cands)) * 40.0
                self._emit(
                    ScanProgressUpdate(
                        stage=3,
                        stage_name=f"Layer 3: AI Classified ({idx + 1}/{total_cands})",
                        percent_complete=pct_done,
                        sub_percent=sub_done,
                        elapsed_s=_elapsed(),
                        eta_s=_eta(pct_done),
                        sdr_owner_badge=f"SDR: L3 AI {cand.center_freq_mhz:.3f}MHz",
                        message=(
                            f"{cand.center_freq_mhz:.4f} MHz ({len(frames)}/{req_frames} frames, "
                            f"+{true_snr_db:.1f}dB, {true_peak_dbfs:.1f}dBFS) -> "
                            f"{picto_result.label} [{int(picto_result.confidence * 100)}%]"
                        ),
                        current_freq_mhz=cand.center_freq_mhz,
                        detected_count=len(layer3_records),
                        latest_frame=frames[-1] if frames else None,
                        latest_composite_rgb=picto_result.composite_rgb,
                        latest_pictogram=picto_result,
                    )
                )

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
                    percent_complete=95.0,
                    sub_percent=60.0,
                    elapsed_s=_elapsed(),
                    eta_s=0.4,
                    sdr_owner_badge="SDR: RELEASED (READY FOR GQRX)",
                    message="Saving permanent timestamped Location Profile & Gqrx classification bookmarks...",
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
                        f"Saved Location Profile [{saved_loc.approx_datetime_local}] with "
                        f"{len(final_profiles)} viable signals. Tap/click any signal to demodulate live in Gqrx!"
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

    # Backwards-compatible convenience method
    def run_scan(
        self,
        freq_min_mhz: int = 88,
        freq_max_mhz: int = 137,
        snr_threshold_db: float = 9.0,
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
        profiles, _ = self.run_scan_with_config(cfg)
        return profiles
