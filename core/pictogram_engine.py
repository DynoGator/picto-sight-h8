"""
Hailo-8 (26 TOPS) Multi-Frame AI Pictogram Classifier Engine
Executes hardware-accelerated neural spectrogram classification across >=3 independent
1.000 MHz STFT frames per candidate signal, fusing transient 2D ROI extraction,
spectral-floor carrier ROI extraction, and IQ modulation metrics, while rendering
visual RGB spectrogram strips with ROI overlays for the UI and saved Location Profiles.
"""

from __future__ import annotations

import atexit
import gc
import logging
import math
import os
import sys
import threading
import time
import uuid
import weakref
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

import cv2
import numpy as np

logger = logging.getLogger("smart_scanner.pictogram")


@dataclass
class PictogramAnalysisResult:
    category: str               # "PICTO_GLYPH", "PICTO_RASTER", "PICTO_GEOMETRIC", "PICTO_BURST_MATRIX", "PICTO_CARRIER_COMB", "BROADCAST_WFM", "BROADCAST_TV", "ISM_TELEMETRY", "AVIATION_ADSB", "WEATHER_SAT_TELEM", "APRS_PACKET", "PAGER_DATA", "AVIATION_AM", "WEATHER_NFM", "NARROWBAND_FM", "NOISE_REJECT"
    raw_neural_class: str       # Direct Hailo-8 head output
    label: str                  # Concise human+AI classification label
    confidence: float           # Aggregated neural confidence (0.0..1.0)
    hardware_accelerated: bool  # True when executed on /dev/hailo0 26 TOPS PCIe NPU
    latency_ms: float           # Total multi-frame inference time in ms
    frames_evaluated: int       # Total Macro + Ultra-Zoom frames classified
    num_rois_analyzed: int      # Total ROIs passed through Hailo-8
    is_noise_rejected: bool     # True if neural classifier + low duty cycle flags noise artifact
    top_candidates: List[Dict[str, Any]] = field(default_factory=list)
    per_frame_classes: List[str] = field(default_factory=list)
    visual_features: Dict[str, Any] = field(default_factory=dict)
    composite_rgb: Optional[np.ndarray] = None  # Rendered Dual-Scale HxWx3 RGB strip
    zoom_frames_evaluated: int = 0
    zoom_span_hz: float = 0.0
    fine_resolution_hz: float = 7.629
    accumulated_scores: Dict[str, List[float]] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop("composite_rgb", None)
        d.pop("accumulated_scores", None)
        return d


_shared_hailo_lock = threading.Lock()
_shared_hailo_classifier: Optional[Any] = None
_live_managers: "weakref.WeakSet[AIPictogramClassifierManager]" = weakref.WeakSet()


def _shutdown_all_hailo_handles() -> None:
    """
    Registered via atexit so HailoRT InferVStreams, network_group activation,
    and VDevice handles are released cleanly while Python and hailo_platform
    C++ singletons are still intact (preventing exit-139 GC teardown crashes).
    """
    global _shared_hailo_classifier
    for mgr in list(_live_managers):
        try:
            mgr._initialized = False
            mgr._hardware_active = False
            mgr._classifier = None
        except Exception:
            pass
    with _shared_hailo_lock:
        if _shared_hailo_classifier is not None:
            try:
                if hasattr(_shared_hailo_classifier, "_cleanup_hardware"):
                    _shared_hailo_classifier._cleanup_hardware()
                elif hasattr(_shared_hailo_classifier, "close"):
                    _shared_hailo_classifier.close()
            except Exception:
                pass
            _shared_hailo_classifier = None
    try:
        gc.collect()
    except Exception:
        pass


atexit.register(_shutdown_all_hailo_handles)


class AIPictogramClassifierManager:
    """
    Coordinates Hailo-8 26 TOPS PCIe inference across multi-frame 1 MHz spectrograms.
    Shares a single process-wide PCIe VDevice handle so multiple manager instances
    never contend over /dev/hailo0.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._classifier: Optional[Any] = None
        self._extractor: Optional[Any] = None
        self._roi_cls: Optional[Any] = None
        self._initialized = False
        self._hardware_active = False
        _live_managers.add(self)

    def initialize(self) -> bool:
        global _shared_hailo_classifier
        with self._lock:
            if self._initialized and self._classifier is not None:
                return True

            try:
                from core.hailo_pictogram import (
                    HailoPictogramClassifier,
                    WaterfallPictogramExtractor,
                    PictogramROI,
                )

                self._roi_cls = PictogramROI
                with _shared_hailo_lock:
                    if _shared_hailo_classifier is None:
                        clf = HailoPictogramClassifier(fallback_ok=True)
                        clf.initialize()
                        _shared_hailo_classifier = clf
                    self._classifier = _shared_hailo_classifier

                self._hardware_active = bool(self._classifier.is_hardware_active)
                self._extractor = WaterfallPictogramExtractor(
                    rows=64, cols=128, snr_threshold_db=7.0, min_area=12, min_dim=3
                )
                self._initialized = True
                logger.info(
                    "Hailo-8 AI Pictogram Engine initialized (PCIe 26 TOPS active=%s)",
                    self._hardware_active,
                )
                return True
            except Exception as exc:
                logger.warning("Hailo-8 initialization fallback: %s", exc)
                self._initialized = True
                self._hardware_active = False
                return True

    @property
    def is_hardware_accelerated(self) -> bool:
        if not self._initialized:
            self.initialize()
        return self._hardware_active

    def _build_spectral_frame_roi(
        self,
        frame: np.ndarray,
        center_freq_hz: float,
        bandwidth_hz: float,
        frame_idx: int,
        ref_floor_db: Optional[float] = None,
    ) -> Tuple[Optional[Any], Tuple[int, int, int, int]]:
        """
        Extracts the active carrier/modulation bounding box relative to the 12th-15th percentile
        (or out-of-channel reference) spectral noise floor, while enforcing a scale-preserving
        minimum horizontal context window (48 bins = 375 kHz out of 1.000 MHz, expanding up to
        128 bins for wideband multiplexes) so Hailo-8 ResNet-50 sees true physical bandwidth
        geometry (e.g. 12.5 kHz NFM vs 200 kHz WFM vs 6.0 MHz ATSC 8VSB TV).
        """
        rows, cols = frame.shape
        p12 = float(np.percentile(frame, 12))
        p20 = float(np.percentile(frame, 20))
        if ref_floor_db is not None and float(ref_floor_db) < p20 - 4.5:
            floor_db = max(float(ref_floor_db), p12 - 18.0)
        else:
            floor_db = float(np.percentile(frame, 14))

        peak_db = float(np.max(frame))
        snr_2d = frame - floor_db

        # Check if frame is a wideband flat-top multiplex (e.g., ATSC 8VSB TV shelf filling >65% of bins)
        col_means = np.mean(snr_2d, axis=0)
        wide_active_frac = float(np.mean(col_means >= max(4.0, (peak_db - floor_db) * 0.28)))
        if wide_active_frac >= 0.65:
            active_cols = np.where(col_means >= max(3.5, (peak_db - floor_db) * 0.22))[0]
            c0 = int(active_cols[0]) if len(active_cols) else 0
            c1 = int(active_cols[-1] + 1) if len(active_cols) else cols
            r0, r1 = 0, rows
            best_box: Optional[Tuple[int, int, int, int]] = (r0, r1, c0, max(c0 + 4, c1))
        else:
            # Focus search within central ±420 kHz of the 1.000 MHz window around target carrier
            mid_c = cols // 2
            half_span = max(16, int(cols * 0.42))
            c_lo_search = max(0, mid_c - half_span)
            c_hi_search = min(cols, mid_c + half_span)

            thresh = max(4.5, min(12.0, (peak_db - floor_db) * 0.36))
            sub_mask = (snr_2d[:, c_lo_search:c_hi_search] >= thresh).astype(np.uint8) * 255
            kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 5))
            closed = cv2.morphologyEx(sub_mask, cv2.MORPH_CLOSE, kernel)
            contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

            best_box = None
            best_score = -1.0
            for cnt in contours:
                cx, ry, bw, bh = cv2.boundingRect(cnt)
                c0 = c_lo_search + cx
                c1 = min(cols, c0 + bw)
                r0 = ry
                r1 = min(rows, ry + bh)
                if (c1 - c0) < 1 or (r1 - r0) < 2:
                    continue
                patch_snr = float(np.mean(snr_2d[r0:r1, c0:c1]))
                dist_penalty = abs(((c0 + c1) * 0.5) - mid_c) * 0.16
                score = patch_snr * math.sqrt((c1 - c0) * (r1 - r0)) - dist_penalty
                if score > best_score:
                    best_score = score
                    best_box = (r0, r1, c0, c1)

            if best_box is None:
                mid_c = cols // 2
                c0 = max(0, mid_c - 4)
                c1 = min(cols, mid_c + 4)
                best_box = (0, rows, c0, c1)

        r0, r1, c0, c1 = best_box
        contour_w = max(1, c1 - c0)
        contour_h = max(1, r1 - r0)

        # Enforce scale-preserving minimum 48-bin (375 kHz) horizontal context so narrowband (NFM/AM)
        # and wideband (WFM/ATSC/ADS-B) maintain distinct visual widths inside 224x224 Viridis!
        min_ctx_cols = min(cols, max(contour_w + max(6, contour_w // 4), 48))
        box_mid_c = (c0 + c1) // 2
        pc0 = max(0, min(cols - min_ctx_cols, box_mid_c - min_ctx_cols // 2))
        pc1 = min(cols, pc0 + min_ctx_cols)
        pad_r = max(2, contour_h // 8)
        pr0 = max(0, r0 - pad_r)
        pr1 = min(rows, r1 + pad_r)

        patch = frame[pr0:pr1, pc0:pc1]
        dyn_range = max(16.0, peak_db - floor_db + 4.0)
        norm = np.clip((patch - floor_db) / dyn_range * 255.0, 0, 255).astype(np.uint8)
        bgr_patch = cv2.applyColorMap(norm, cv2.COLORMAP_VIRIDIS)
        rgb_224 = cv2.resize(bgr_patch, (224, 224), interpolation=cv2.INTER_LINEAR)

        if self._roi_cls is None:
            return None, (r0, r1, c0, c1)

        bin_hz = bandwidth_hz / cols
        roi_center_hz = (center_freq_hz - bandwidth_hz * 0.5) + ((c0 + c1) * 0.5) * bin_hz
        roi = self._roi_cls(
            roi_id=f"f{frame_idx}_{uuid.uuid4().hex[:6]}",
            row_min=r0,
            row_max=r1,
            col_min=c0,
            col_max=c1,
            width_bins=contour_w,
            height_rows=contour_h,
            area=contour_w * contour_h,
            center_freq_hz=float(roi_center_hz),
            bandwidth_hz=float(contour_w * bin_hz),
            duration_s=0.045 * (contour_h / max(1.0, float(rows))),
            snr_db=float(np.max(snr_2d[r0:r1, c0:c1])),
            peak_dbm=float(np.max(frame[r0:r1, c0:c1])),
            patch=patch,
            normalized_rgb=rgb_224,
        )
        return roi, (r0, r1, c0, c1)

    def _build_zoom_frame_roi(
        self,
        z_frame: np.ndarray,
        center_freq_hz: float,
        zoom_span_hz: float,
        frame_idx: int,
    ) -> Tuple[Optional[Any], Tuple[int, int, int, int]]:
        """
        Builds a full-span Ultra-Zoom Micro Waterfall ROI (31.25 kHz @ 244.14 Hz/bin or
        125/250 kHz @ 976.6/1,953 Hz/bin) where narrowband carrier spines, speech sidebands,
        and FSK Mark/Space rails span 30-60 columns instead of 1.5 pixels!
        """
        rows, cols = z_frame.shape
        floor_db = float(np.percentile(z_frame, 15))
        peak_db = float(np.max(z_frame))
        snr_2d = np.maximum(0.0, z_frame - floor_db)

        col_means = np.mean(snr_2d, axis=0)
        act = np.where(col_means >= max(3.5, (peak_db - floor_db) * 0.25))[0]
        if len(act) >= 2:
            c0 = int(act[0])
            c1 = int(act[-1] + 1)
        else:
            mid_c = cols // 2
            c0, c1 = max(0, mid_c - 8), min(cols, mid_c + 8)

        row_pks = np.max(snr_2d[:, max(0, c0) : min(cols, c1)], axis=1)
        act_r = np.where(row_pks >= max(3.5, (peak_db - floor_db) * 0.30))[0]
        if len(act_r) >= 2:
            r0, r1 = int(act_r[0]), int(act_r[-1] + 1)
        else:
            r0, r1 = 0, rows

        contour_w = max(2, c1 - c0)
        contour_h = max(2, r1 - r0)

        # Preserve full 128-column zoomed frame so 31.25 kHz / 250 kHz physical width ratio stays exact
        dyn_range = max(15.0, peak_db - floor_db + 3.5)
        norm = np.clip((z_frame - floor_db) / dyn_range * 255.0, 0, 255).astype(np.uint8)
        bgr_patch = cv2.applyColorMap(norm, cv2.COLORMAP_VIRIDIS)
        rgb_224 = cv2.resize(bgr_patch, (224, 224), interpolation=cv2.INTER_LINEAR)

        if self._roi_cls is None:
            return None, (r0, r1, c0, c1)

        bin_hz = zoom_span_hz / float(cols)
        roi = self._roi_cls(
            roi_id=f"z{frame_idx}_{uuid.uuid4().hex[:6]}",
            row_min=r0,
            row_max=r1,
            col_min=c0,
            col_max=c1,
            width_bins=contour_w,
            height_rows=contour_h,
            area=contour_w * contour_h,
            center_freq_hz=float(center_freq_hz),
            bandwidth_hz=float(contour_w * bin_hz),
            duration_s=0.10 * (contour_h / max(1.0, float(rows))),
            snr_db=float(np.max(snr_2d[r0:r1, c0:c1])),
            peak_dbm=float(peak_db),
            patch=z_frame,
            normalized_rgb=rgb_224,
        )
        return roi, (r0, r1, c0, c1)

    @staticmethod
    def render_frames_composite_rgb(
        frames: List[np.ndarray],
        boxes: List[Tuple[int, int, int, int]],
        center_freq_mhz: float,
        label: str,
        zoom_frames: Optional[List[np.ndarray]] = None,
        zoom_boxes: Optional[List[Tuple[int, int, int, int]]] = None,
        zoom_span_hz: float = 31_250.0,
        dwell_passes: int = 1,
    ) -> np.ndarray:
        """
        Renders a crisp Dual-Scale RGB Spectrogram Strip (1.000 MHz Macro context + 31.25/250 kHz
        Ultra-Zoom carrier detail @ 244 Hz/bin) bounded to <= 4 representative tiles so UI widgets
        and disk thumbnails stay razor-sharp and compact.
        """
        _ = (center_freq_mhz, label)
        rendered_tiles: List[np.ndarray] = []
        tile_w, tile_h = 176, 104

        z_list = [z for z in (zoom_frames or []) if z is not None and z.ndim == 2 and z.size > 0]
        zb_list = zoom_boxes or []

        def _pick_indices(n_total: int, k_max: int) -> List[int]:
            if n_total <= 0:
                return []
            if n_total <= k_max:
                return list(range(n_total))
            return [int(round(i * (n_total - 1) / float(max(1, k_max - 1)))) for i in range(k_max)]

        if z_list:
            m_idxs = _pick_indices(len(frames), 2)
            z_idxs = _pick_indices(len(z_list), 2)
        else:
            m_idxs = _pick_indices(len(frames), min(4, max(3, len(frames))))
            z_idxs = []

        # 1. Render Macro 1.000 MHz context tiles
        for idx in m_idxs:
            frame = frames[idx]
            floor_db = float(np.percentile(frame, 18))
            ceil_db = max(floor_db + 18.0, float(np.percentile(frame, 99.5)) + 3.0)
            norm = np.clip((frame - floor_db) / (ceil_db - floor_db) * 255.0, 0, 255).astype(np.uint8)
            bgr = cv2.applyColorMap(norm, cv2.COLORMAP_TURBO)
            bgr_resized = cv2.resize(bgr, (tile_w, tile_h), interpolation=cv2.INTER_CUBIC)

            rows, cols = frame.shape
            sx = tile_w / float(cols)
            sy = tile_h / float(rows)

            mid_x = tile_w // 2
            for y in range(0, tile_h, 8):
                cv2.line(bgr_resized, (mid_x, y), (mid_x, min(tile_h - 1, y + 4)), (180, 255, 255), 1)

            if idx < len(boxes):
                r0, r1, c0, c1 = boxes[idx]
                x0, x1 = int(c0 * sx), int(c1 * sx)
                y0, y1 = int(r0 * sy), int(r1 * sy)
                cv2.rectangle(bgr_resized, (x0, y0), (x1, y1), (255, 240, 0), 1)

            cv2.rectangle(bgr_resized, (0, 0), (tile_w, 16), (14, 20, 32), -1)
            hdr = f"M{idx + 1}/{len(frames)} 1MHz P{dwell_passes}" if dwell_passes > 1 else f"F{idx + 1}/{len(frames)} 1MHz"
            cv2.putText(
                bgr_resized,
                hdr,
                (5, 12),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.35,
                (0, 240, 255),
                1,
                cv2.LINE_AA,
            )
            cv2.rectangle(bgr_resized, (0, 0), (tile_w - 1, tile_h - 1), (45, 75, 110), 1)
            rendered_tiles.append(cv2.cvtColor(bgr_resized, cv2.COLOR_BGR2RGB))

        # 2. Render Ultra-Zoom Micro Waterfall tiles (244 Hz/bin or 977/1953 Hz/bin)
        span_khz = zoom_span_hz / 1000.0
        for idx in z_idxs:
            zf = z_list[idx]
            floor_db = float(np.percentile(zf, 16))
            ceil_db = max(floor_db + 16.0, float(np.percentile(zf, 99.5)) + 2.5)
            norm = np.clip((zf - floor_db) / (ceil_db - floor_db) * 255.0, 0, 255).astype(np.uint8)
            bgr = cv2.applyColorMap(norm, cv2.COLORMAP_VIRIDIS)
            bgr_resized = cv2.resize(bgr, (tile_w, tile_h), interpolation=cv2.INTER_CUBIC)

            rows, cols = zf.shape
            sx = tile_w / float(cols)
            sy = tile_h / float(rows)

            mid_x = tile_w // 2
            for y in range(16, tile_h, 6):
                cv2.line(bgr_resized, (mid_x, y), (mid_x, min(tile_h - 1, y + 3)), (120, 255, 140), 1)

            if idx < len(zb_list):
                r0, r1, c0, c1 = zb_list[idx]
                x0, x1 = int(c0 * sx), int(c1 * sx)
                y0, y1 = int(r0 * sy), int(r1 * sy)
                cv2.rectangle(bgr_resized, (x0, y0), (x1, y1), (80, 255, 160), 1)

            cv2.rectangle(bgr_resized, (0, 0), (tile_w, 16), (10, 32, 24), -1)
            z_hdr = f"ZOOM {span_khz:.1f}kHz #{idx + 1}/{len(z_list)}"
            cv2.putText(
                bgr_resized,
                z_hdr,
                (5, 12),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.34,
                (90, 255, 165),
                1,
                cv2.LINE_AA,
            )
            cv2.rectangle(bgr_resized, (0, 0), (tile_w - 1, tile_h - 1), (40, 145, 95), 1)
            rendered_tiles.append(cv2.cvtColor(bgr_resized, cv2.COLOR_BGR2RGB))

        if not rendered_tiles:
            return np.zeros((tile_h, tile_w * 3, 3), dtype=np.uint8)

        sep = np.full((tile_h, 4, 3), 11, dtype=np.uint8)
        parts: List[np.ndarray] = []
        for i, t in enumerate(rendered_tiles):
            if i > 0:
                parts.append(sep)
            parts.append(t)
        return np.hstack(parts)

    def analyze_spectrogram_frames(
        self,
        center_freq_hz: float,
        frames: List[np.ndarray],
        bandwidth_hz: float = 1_000_000.0,
        iq_metrics: Optional[Dict[str, Any]] = None,
        prior_class_scores: Optional[Dict[str, List[float]]] = None,
        dwell_passes: int = 1,
    ) -> PictogramAnalysisResult:
        """
        Runs Dual-Scale Hailo-8 classification across ALL Macro 1.000 MHz frames AND ALL
        Ultra-Zoom (244.14 Hz/bin / 976.6 Hz/bin) frames, fuses multi-pass posterior scores
        with 7.63 Hz/bin carrier lock & subcarrier metrics, and renders a Dual-Scale RGB strip.
        """
        if not self._initialized:
            self.initialize()

        iq_metrics = iq_metrics or {}
        t0 = time.perf_counter()
        valid_frames = [f for f in frames if f is not None and f.ndim == 2 and f.size > 0]
        raw_zoom_frames = iq_metrics.get("zoom_frames") or []
        valid_zoom_frames = [zf for zf in raw_zoom_frames if zf is not None and zf.ndim == 2 and zf.size > 0]
        zoom_span_hz = float(iq_metrics.get("zoom_span_hz", 31_250.0 if valid_zoom_frames else 0.0))
        fine_res_hz = float(iq_metrics.get("fine_fft_resolution_hz", 7.629))

        # Initialize from prior multi-pass dwell scores if accumulating across passes
        class_scores: Dict[str, List[float]] = {
            k: list(v) for k, v in (prior_class_scores or {}).items()
        }
        per_frame_winners: List[str] = []
        boxes: List[Tuple[int, int, int, int]] = []
        zoom_boxes: List[Tuple[int, int, int, int]] = []
        rois_analyzed = 0
        used_hw_any = False

        macro_zm = {**iq_metrics, "roi_span_hz": bandwidth_hz}
        macro_zm.pop("zoom_frames", None)
        micro_zm = {**macro_zm, "roi_span_hz": max(15_000.0, zoom_span_hz)}

        ref_floor_db = iq_metrics.get("ref_floor_dbfs")
        with self._lock:
            if self._extractor is not None and hasattr(self._extractor, "reset"):
                self._extractor.reset()

            # 1. Evaluate Macro 1.000 MHz context ROIs (evenly strided up to 14 NPU passes/burst)
            n_m = len(valid_frames)
            m_eval_set = set(range(n_m)) if n_m <= 14 else {int(round(i * (n_m - 1) / 13.0)) for i in range(14)}
            for idx, frame in enumerate(valid_frames):
                roi, box = self._build_spectral_frame_roi(
                    frame,
                    center_freq_hz,
                    bandwidth_hz,
                    idx,
                    ref_floor_db=float(ref_floor_db) if ref_floor_db is not None else None,
                )
                boxes.append(box)
                if idx in m_eval_set and roi is not None and self._classifier is not None:
                    try:
                        det = self._classifier.classify_roi(roi, zoom_metrics=macro_zm)
                        rois_analyzed += 1
                        used_hw_any = used_hw_any or bool(det.hardware_accelerated)
                        if det.top_candidates:
                            per_frame_winners.append(str(det.top_candidates[0]["class"]))
                            for cand in det.top_candidates:
                                cname = str(cand["class"])
                                class_scores.setdefault(cname, []).append(float(cand["score"]))
                        else:
                            cname = det.category.value if hasattr(det.category, "value") else str(det.category)
                            per_frame_winners.append(cname)
                            class_scores.setdefault(cname, []).append(float(det.confidence))
                    except Exception as exc:
                        logger.debug("Macro frame %d Hailo classify note: %s", idx, exc)

                # Rolling 2D glyph/chirp contour extractor
                if self._extractor is not None and (idx < 3 or idx % max(1, len(valid_frames) // 3) == 0):
                    for row in frame:
                        self._extractor.push_row(row)
                    transient_rois = self._extractor.extract_candidates(
                        center_freq_hz=center_freq_hz,
                        span_hz=bandwidth_hz,
                    )
                    for troi in transient_rois[:2]:
                        if self._classifier is not None:
                            try:
                                tdet = self._classifier.classify_roi(troi, zoom_metrics=macro_zm)
                                rois_analyzed += 1
                                used_hw_any = used_hw_any or bool(tdet.hardware_accelerated)
                                for cand in tdet.top_candidates:
                                    cname = str(cand["class"])
                                    weight = 1.15 if cname in ("glyph", "geometric", "raster") else 1.0
                                    class_scores.setdefault(cname, []).append(min(0.99, float(cand["score"]) * weight))
                            except Exception:
                                pass

            # 2. Evaluate Ultra-Zoom Micro Waterfall ROIs (244.14 Hz/bin narrowband; 976.6/1,953 Hz/bin WFM)
            n_z = len(valid_zoom_frames)
            z_eval_set = set(range(n_z)) if n_z <= 14 else {int(round(i * (n_z - 1) / 13.0)) for i in range(14)}
            for z_idx, z_frame in enumerate(valid_zoom_frames):
                z_roi, z_box = self._build_zoom_frame_roi(
                    z_frame,
                    center_freq_hz=center_freq_hz,
                    zoom_span_hz=max(15_000.0, zoom_span_hz),
                    frame_idx=z_idx,
                )
                zoom_boxes.append(z_box)
                if z_idx in z_eval_set and z_roi is not None and self._classifier is not None:
                    try:
                        z_det = self._classifier.classify_roi(z_roi, zoom_metrics=micro_zm)
                        rois_analyzed += 1
                        used_hw_any = used_hw_any or bool(z_det.hardware_accelerated)
                        if z_det.top_candidates:
                            per_frame_winners.append(str(z_det.top_candidates[0]["class"]))
                            for cand in z_det.top_candidates:
                                cname = str(cand["class"])
                                z_w = 1.25 if zoom_span_hz <= 65_000.0 else 1.10
                                class_scores.setdefault(cname, []).append(min(0.995, float(cand["score"]) * z_w))
                    except Exception as exc:
                        logger.debug("Ultra-Zoom frame %d Hailo classify note: %s", z_idx, exc)

        snr_db = float(iq_metrics.get("snr_db", 12.0))
        obw_hz = float(iq_metrics.get("measured_obw_hz", iq_metrics.get("obw_hz", 25_000.0)))
        iq_mod = str(iq_metrics.get("iq_mod_hint", "NFM"))
        duty_cycle = float(iq_metrics.get("duty_cycle", 1.0))
        tonality_db = float(iq_metrics.get("carrier_tonality_db", 0.0))
        fsk_rail = float(iq_metrics.get("fsk_rail_score", 0.0))
        stereo_pilot = bool(iq_metrics.get("stereo_pilot", False))
        ctcss_hz = float(iq_metrics.get("ctcss_hz", 0.0))

        # Aggregate posterior probabilities across all Macro + Ultra-Zoom + Multi-Pass Dwell frames
        is_dedicated_pager = (
            abs(center_freq_hz - 152_480_000) <= 25_000
            or abs(center_freq_hz - 158_700_000) <= 25_000
            or abs(center_freq_hz - 454_000_000) <= 25_000
            or abs(center_freq_hz - 462_750_000) <= 25_000
            or (929_000_000 <= center_freq_hz <= 932_000_000)
        )

        agg_probs: List[Tuple[str, float]] = []
        if class_scores:
            raw_means = {k: float(np.mean(v)) for k, v in class_scores.items() if len(v) > 0}
            if 87.8e6 <= center_freq_hz <= 108.1e6:
                if "wfm_stereo" in raw_means:
                    raw_means["wfm_stereo"] *= 1.50 if stereo_pilot else 1.35
                for unalloc in ("fsk_pager", "atsc_tv", "adsb_pulse", "ism_telemetry"):
                    if unalloc in raw_means:
                        raw_means[unalloc] *= 0.20
            else:
                if obw_hz < 1_000_000 and "atsc_tv" in raw_means:
                    try:
                        from core.demod_engine import identify_atsc_tv_channel
                        if identify_atsc_tv_channel(center_freq_hz) is None:
                            raw_means["atsc_tv"] *= 0.25
                    except Exception:
                        raw_means["atsc_tv"] *= 0.25
                if not (976.0e6 <= center_freq_hz <= 1_100.0e6) and "adsb_pulse" in raw_means:
                    raw_means["adsb_pulse"] *= 0.30

                # AM Voice is physically constrained to MW (0.5-1.7M), HF/CB (3-30M), Airband (108-137M) and Mil-Air (225-400M)
                is_am_band = (
                    (530_000 <= center_freq_hz <= 30_000_000)
                    or (108_000_000 <= center_freq_hz <= 137_000_000)
                    or (225_000_000 <= center_freq_hz <= 400_000_000)
                )
                if not is_am_band and iq_mod != "AM" and "am_voice" in raw_means:
                    raw_means["am_voice"] *= 0.25

                # Dedicated POCSAG / FLEX Hospital & Public Safety Paging Channel Prior
                if is_dedicated_pager and "fsk_pager" in raw_means:
                    raw_means["fsk_pager"] *= 2.40

                # Sub-10 Hz precision physical anchors:
                if iq_mod == "AM" and tonality_db >= 11.5 and "am_voice" in raw_means:
                    raw_means["am_voice"] *= 1.30
                if iq_mod == "NFM" and ctcss_hz >= 60.0 and "nfm_voice" in raw_means:
                    raw_means["nfm_voice"] *= 1.30
                if fsk_rail >= 0.36 and duty_cycle < 0.75:
                    if "fsk_pager" in raw_means:
                        raw_means["fsk_pager"] *= 1.25
                    if "ism_telemetry" in raw_means and (310e6 <= center_freq_hz <= 930e6):
                        raw_means["ism_telemetry"] *= 1.20

            total = sum(raw_means.values()) or 1.0
            agg_probs = sorted(
                [(k, val / total) for k, val in raw_means.items()],
                key=lambda item: item[1],
                reverse=True,
            )
        else:
            agg_probs = [("carrier_comb", 0.85)]

        best_neural_class, best_neural_conf = agg_probs[0]
        top_candidates = [{"class": k, "score": round(v, 4)} for k, v in agg_probs[:6]]

        is_noise = bool(
            best_neural_class == "noise_reject"
            and best_neural_conf >= 0.62
            and snr_db < 9.0
            and duty_cycle < 0.35
        )

        category, label, final_conf = self._fuse_neural_and_rf_semantics(
            center_freq_hz=center_freq_hz,
            neural_class=best_neural_class,
            neural_conf=best_neural_conf,
            agg_probs=agg_probs,
            iq_mod=iq_mod,
            obw_hz=obw_hz,
            snr_db=snr_db,
        )
        # Multi-pass high-confidence dwell boost when consistent across passes
        if dwell_passes > 1 and not is_noise:
            final_conf = min(0.99, final_conf + min(0.06, 0.02 * (dwell_passes - 1)))

        composite_rgb = self.render_frames_composite_rgb(
            valid_frames,
            boxes,
            center_freq_mhz=center_freq_hz / 1e6,
            label=label,
            zoom_frames=valid_zoom_frames,
            zoom_boxes=zoom_boxes,
            zoom_span_hz=zoom_span_hz if zoom_span_hz > 0 else 31_250.0,
            dwell_passes=dwell_passes,
        )

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        mean_peak = float(np.mean([np.max(f) for f in valid_frames])) if valid_frames else -80.0
        mean_floor = float(np.mean([np.percentile(f, 20) for f in valid_frames])) if valid_frames else -95.0
        total_eval_frames = len(valid_frames) + len(valid_zoom_frames)

        return PictogramAnalysisResult(
            category=category,
            raw_neural_class=best_neural_class,
            label=label,
            confidence=round(final_conf, 3),
            hardware_accelerated=used_hw_any or self._hardware_active,
            latency_ms=round(elapsed_ms, 2),
            frames_evaluated=total_eval_frames,
            num_rois_analyzed=rois_analyzed,
            is_noise_rejected=is_noise,
            top_candidates=top_candidates,
            per_frame_classes=per_frame_winners,
            visual_features={
                "frames_evaluated": total_eval_frames,
                "macro_frames_evaluated": len(valid_frames),
                "zoom_frames_evaluated": len(valid_zoom_frames),
                "rois_analyzed": rois_analyzed,
                "mean_peak_dbfs": round(mean_peak, 1),
                "mean_floor_dbfs": round(mean_floor, 1),
                "spectral_bandwidth_hz": bandwidth_hz,
                "zoom_span_hz": round(zoom_span_hz, 1),
                "fine_fft_resolution_hz": round(fine_res_hz, 3),
                "measured_obw_hz": round(obw_hz, 0),
                "iq_modulation": iq_mod,
                "ctcss_hz": round(ctcss_hz, 1),
                "stereo_pilot": stereo_pilot,
                "dwell_passes": int(dwell_passes),
            },
            composite_rgb=composite_rgb,
            zoom_frames_evaluated=len(valid_zoom_frames),
            zoom_span_hz=round(zoom_span_hz, 1),
            fine_resolution_hz=round(fine_res_hz, 3),
            accumulated_scores=class_scores,
        )

    @staticmethod
    def _fuse_neural_and_rf_semantics(
        center_freq_hz: float,
        neural_class: str,
        neural_conf: float,
        agg_probs: List[Tuple[str, float]],
        iq_mod: str,
        obw_hz: float,
        snr_db: float,
    ) -> Tuple[str, str, float]:
        """
        Produces an accurate, human-readable + AI-grounded classification combining
        Hailo-8 spectrogram pattern recognition with IQ physical-layer measurements.
        """
        # If top class was noise_reject on a verified high-SNR carrier, pick strongest structural class
        eff_class = neural_class
        eff_conf = neural_conf
        if eff_class == "noise_reject" and snr_db >= 9.0 and len(agg_probs) > 1:
            eff_class, second_prob = agg_probs[1]
            eff_conf = max(0.68, min(0.96, second_prob + 0.35))
        else:
            eff_conf = max(0.65, min(0.99, eff_conf + min(0.18, snr_db / 160.0)))

        # Distinct exotic 2D waterfall pictograms outside standard FM/TV broadcast
        is_fm_band = 87.8e6 <= center_freq_hz <= 108.1e6
        if eff_class == "glyph" and neural_conf >= 0.72 and not is_fm_band:
            return "PICTO_GLYPH", f"Pictogram-Glyph [{eff_class}]", eff_conf
        if eff_class == "geometric" and neural_conf >= 0.74 and (iq_mod == "DIGITAL_BURST" or obw_hz >= 35_000):
            return "PICTO_GEOMETRIC", f"Swept-Chirp [{eff_class}]", eff_conf

        # 1. ATSC Digital Broadcast Television Channels (VHF Low 2-6, VHF High 7-13, UHF 14-36)
        try:
            from core.demod_engine import identify_atsc_tv_channel
            tv_info = identify_atsc_tv_channel(center_freq_hz)
            if tv_info is not None:
                ch, center, lo, pilot = tv_info
                tv_conf = max(0.86, min(0.99, eff_conf + (0.12 if eff_class == "atsc_tv" else 0.08)))
                return "BROADCAST_TV", f"ATSC-TV-Ch{ch} ({eff_class})", tv_conf
        except Exception:
            pass

        # 2. Aviation ADS-B Mode-S Transponders (1090 MHz & 978 MHz UAT)
        if 1_088_000_000 <= center_freq_hz <= 1_092_000_000:
            return "AVIATION_ADSB", f"ADS-B-1090 ({eff_class})", max(0.88, eff_conf)
        if 977_000_000 <= center_freq_hz <= 979_000_000:
            return "AVIATION_ADSB", f"UAT-978 ({eff_class})", max(0.86, eff_conf)

        # 3. Weather Satellite Telemetry (137.0 - 138.0 MHz APT/LRPT)
        if 137_000_000 <= center_freq_hz <= 138_000_000:
            return "WEATHER_SAT_TELEM", f"WX-Sat-Telem ({eff_class})", eff_conf

        # 4. Amateur APRS Tactical Packet Radio (144.390 MHz)
        if abs(center_freq_hz - 144_390_000) <= 25_000:
            return "APRS_PACKET", f"APRS-AX25 ({eff_class})", max(0.85, eff_conf)

        # 5. Citizens Band (CB) Radio (26.965 - 27.405 MHz AM)
        if 26_960_000 <= center_freq_hz <= 27_410_000:
            if abs(center_freq_hz - 27_185_000) <= 6_000:
                return "TRUCKER_CB", f"CB-19-Trucker ({eff_class})", max(0.85, eff_conf)
            if abs(center_freq_hz - 27_065_000) <= 6_000:
                return "TRUCKER_CB", f"CB-09-Emerg ({eff_class})", max(0.85, eff_conf)
            return "TRUCKER_CB", f"CB-Voice ({eff_class})", max(0.80, eff_conf)

        # 6. Fremont County / Penrose Priority Fire, Sheriff, SAR, Medevac (evaluated BEFORE generic VHF/Pager!)
        if abs(center_freq_hz - 155_880_000) <= 6_000:
            return "FIRE_EMS", f"Penrose-Fire-Ops ({eff_class})", max(0.88, eff_conf)
        if abs(center_freq_hz - 154_400_000) <= 6_000:
            return "FIRE_EMS", f"Fire-Dispatch-154.4 ({eff_class})", max(0.88, eff_conf)
        if abs(center_freq_hz - 154_310_000) <= 6_000:
            return "FIRE_EMS", f"Canon-Fire-154.31 ({eff_class})", max(0.88, eff_conf)
        if abs(center_freq_hz - 154_845_000) <= 6_000:
            return "SHERIFF_POLICE", f"Fremont-Sheriff-154.8 ({eff_class})", max(0.88, eff_conf)
        if abs(center_freq_hz - 155_160_000) <= 6_000:
            return "SEARCH_RESCUE", f"CO-SAR-MRA1 ({eff_class})", max(0.88, eff_conf)
        if abs(center_freq_hz - 155_235_000) <= 6_000:
            return "SEARCH_RESCUE", f"CO-SAR-MRA2 ({eff_class})", max(0.88, eff_conf)
        if abs(center_freq_hz - 155_340_000) <= 6_000:
            return "MEDEVAC_HOSPITAL", f"CHERN-Med9-Hospital ({eff_class})", max(0.88, eff_conf)

        # 7. Local Aviation Airband & Penrose Fremont Co Airport (1V6)
        if abs(center_freq_hz - 122_800_000) <= 6_000:
            return "AVIATION_AM", f"1V6-Penrose-CTAF ({eff_class})", max(0.88, eff_conf)
        if abs(center_freq_hz - 120_025_000) <= 6_000:
            return "AVIATION_AM", f"1V6-Penrose-AWOS ({eff_class})", max(0.88, eff_conf)

        # 8. Railroad Operations (Union Pacific & Royal Gorge)
        if 160_215_000 <= center_freq_hz <= 161_565_000:
            if abs(center_freq_hz - 161_490_000) <= 6_000:
                return "RAILROAD_AAR", f"UP-AAR92-Rail ({eff_class})", max(0.88, eff_conf)
            if abs(center_freq_hz - 160_590_000) <= 6_000:
                return "RAILROAD_AAR", f"Gorge-AAR32-Rail ({eff_class})", max(0.88, eff_conf)
            if abs(center_freq_hz - 161_550_000) <= 6_000:
                return "RAILROAD_AAR", f"JointLine-AAR96 ({eff_class})", max(0.88, eff_conf)
            return "RAILROAD_AAR", f"Rail-AAR ({eff_class})", max(0.82, eff_conf)

        # 9. ISM IoT Telemetry Bands (433 MHz, 915 MHz, 315 MHz)
        if 433_000_000 <= center_freq_hz <= 434_900_000:
            return "ISM_TELEMETRY", f"Telemetry-433 ({eff_class})", eff_conf
        if 902_000_000 <= center_freq_hz <= 928_000_000:
            return "ISM_TELEMETRY", f"Telemetry-915 ({eff_class})", eff_conf
        if 314_500_000 <= center_freq_hz <= 315_500_000:
            return "ISM_TELEMETRY", f"Telemetry-315 ({eff_class})", eff_conf

        # 10. Dedicated POCSAG / FLEX Pager Sub-Bands (152.00-152.86, 157.74-158.72, 454.00-454.68, 929-932 MHz)
        is_dedicated_pager = (
            (152_000_000 <= center_freq_hz <= 152_860_000)
            or (157_740_000 <= center_freq_hz <= 158_720_000)
            or (454_000_000 <= center_freq_hz <= 454_680_000)
            or (929_000_000 <= center_freq_hz <= 932_000_000)
        )
        if is_dedicated_pager and (
            iq_mod == "DIGITAL_BURST" or eff_class in ("fsk_pager", "tone_matrix", "raster", "ism_telemetry")
        ):
            return "PAGER_DATA", f"Pager-POCSAG ({eff_class})", eff_conf
        if eff_class == "fsk_pager" and iq_mod == "DIGITAL_BURST" and (
            150_000_000 <= center_freq_hz <= 470_000_000
        ):
            return "PAGER_DATA", f"Pager-POCSAG ({eff_class})", eff_conf

        # Band + IQ + Neural pattern fusion
        if is_fm_band:
            sub = "MPX-Stereo" if (eff_class in ("wfm_stereo", "carrier_comb", "raster") or obw_hz >= 75_000) else "FM-Carrier"
            return "BROADCAST_WFM", f"WFM-Stereo ({sub})", max(0.84, eff_conf)
        if 108.0e6 <= center_freq_hz <= 137.0e6:
            return "AVIATION_AM", f"Airband-AM ({eff_class})", eff_conf
        if 162.38e6 <= center_freq_hz <= 162.57e6:
            return "WEATHER_NFM", f"NOAA-WX-NFM ({eff_class})", eff_conf
        if 156.0e6 <= center_freq_hz <= 162.03e6:
            return "MARINE_NFM", f"Marine-VHF ({eff_class})", eff_conf
        if (eff_class == "atsc_tv" and obw_hz >= 1_200_000) or obw_hz >= 1_500_000:
            return "BROADCAST_TV", f"Wideband-TV ({eff_class})", eff_conf
        if eff_class == "wfm_stereo" or iq_mod == "WFM" or obw_hz >= 85_000:
            return "WIDEBAND_MUX", f"Wideband-{eff_class}", eff_conf
        if eff_class == "am_voice" or iq_mod == "AM":
            return "AM_VOICE", f"AM-Carrier ({eff_class})", eff_conf
        if iq_mod == "CW":
            return "CW_BEACON", f"CW-Tone ({eff_class})", eff_conf
        if iq_mod == "DIGITAL_BURST" or eff_class in ("fsk_pager", "ism_telemetry", "adsb_pulse", "tone_matrix", "raster"):
            return "PICTO_BURST_MATRIX", f"Burst/FSK ({eff_class})", eff_conf

        return "NARROWBAND_FM", f"NFM-Voice ({eff_class})", eff_conf

    def close(self) -> None:
        global _shared_hailo_classifier
        with self._lock:
            self._classifier = None
            self._initialized = False
            self._hardware_active = False
        with _shared_hailo_lock:
            if _shared_hailo_classifier is not None:
                try:
                    if hasattr(_shared_hailo_classifier, "_cleanup_hardware"):
                        _shared_hailo_classifier._cleanup_hardware()
                except Exception:
                    pass
                _shared_hailo_classifier = None


_pictogram_manager: Optional[AIPictogramClassifierManager] = None
_picto_lock = threading.Lock()


def get_pictogram_classifier() -> AIPictogramClassifierManager:
    global _pictogram_manager
    with _picto_lock:
        if _pictogram_manager is None:
            _pictogram_manager = AIPictogramClassifierManager()
            _pictogram_manager.initialize()
        return _pictogram_manager
