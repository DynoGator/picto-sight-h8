"""
Picto-Sight H8 | Hailo-8 (26 TOPS) RF Waterfall Pictogram Extractor & Neural Classifier
Hardware-accelerated 2D spectrogram ROI segmentation, Viridis 224x224x3 normalization,
HailoRT PCIe VDevice feature extraction, 16-D physical RF spectral morphology fusion,
and calibrated multi-class RF modulation + pictogram transfer-head classification.
"""

from __future__ import annotations

import datetime
import enum
import logging
import math
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import cv2
import numpy as np

# Optional HailoRT imports with graceful fallbacks when running on non-Hailo hosts
try:
    from hailo_platform import (
        HEF,
        ConfigureParams,
        FormatType,
        HailoStreamInterface,
        InferVStreams,
        InputVStreamParams,
        OutputVStreamParams,
        VDevice,
    )
    HAILO_AVAILABLE = True
except (ImportError, OSError):
    HAILO_AVAILABLE = False

logger = logging.getLogger("smart_scanner.hailo_pictogram")

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _resolve_default_hef() -> str:
    """Resolve Hailo Model Zoo ResNet-50 HEF across Hailo-8 / Hailo-8L installations."""
    env_hef = os.environ.get("PICTOSIGHT_HEF_PATH", "").strip()
    candidates = [
        env_hef,
        str(PROJECT_ROOT / "models" / "resnet_v1_50_h8.hef"),
        str(PROJECT_ROOT / "models" / "resnet_v1_50_h8l.hef"),
        "/usr/share/hailo-models/resnet_v1_50_h8.hef",
        "/usr/share/hailo-models/resnet_v1_50_h8l.hef",
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return "/usr/share/hailo-models/resnet_v1_50_h8l.hef"


class PictogramCategory(str, enum.Enum):
    GLYPH = "PICTO_GLYPH"                # Alphanumeric character / spectrogram symbol
    RASTER = "PICTO_RASTER"              # Dense 2D waterfall image / SSTV / ATSC 8VSB / OFDM raster
    GEOMETRIC = "PICTO_GEOMETRIC"        # Swept polygons, chirps, stepped carriers
    BURST_MATRIX = "PICTO_BURST_MATRIX"  # Pulsed multi-tone / FSK / POCSAG / ISM / ADS-B matrix
    CARRIER_COMB = "PICTO_CARRIER_COMB"  # Multi-carrier / WFM MPX / NFM / AM continuous carrier
    ANOMALY = "PICTO_ANOMALY"            # High-SNR unclassified structural energy or noise reject


@dataclass
class PictogramROI:
    """A detected 2D region of interest in the RF waterfall."""
    roi_id: str
    row_min: int
    row_max: int
    col_min: int
    col_max: int
    width_bins: int
    height_rows: int
    area: int
    center_freq_hz: float
    bandwidth_hz: float
    duration_s: float
    snr_db: float
    peak_dbm: float
    patch: np.ndarray          # 2D raw float dB/dBFS patch
    normalized_rgb: np.ndarray # 224x224x3 uint8 image for Hailo-8 neural input


@dataclass
class PictogramDetection:
    """Classified spectrogram ROI event produced by HailoPictogramClassifier."""
    detection_id: str
    timestamp: float
    utc_iso: str
    center_freq_hz: float
    bandwidth_hz: float
    duration_s: float
    snr_db: float
    peak_dbm: float
    category: PictogramCategory
    label: str
    confidence: float
    top_candidates: list[dict[str, Any]]
    bbox: tuple[int, int, int, int]  # (r_min, r_max, c_min, c_max)
    hailo_latency_ms: float
    hardware_accelerated: bool
    model_name: str
    source: str

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["category"] = self.category.value
        return d


def compute_roi_physical_features(roi: PictogramROI, ref_span_hz: float = 1_000_000.0) -> np.ndarray:
    """
    Computes a 16-dimensional scale-aware physical RF spectrogram morphology vector
    from a PictogramROI patch (dBFS) and bandwidth/duration geometry:
      0: norm_bw             - log-normalized occupied bandwidth relative to 1 MHz
      1: aspect_ratio        - tanh-compressed width/height bin ratio
      2: snr_norm            - normalized peak SNR (0..1 over 45 dB)
      3: duty_cycle          - fraction of time rows with active carrier energy
      4: row_burst_rate      - normalized temporal on/off transition rate (burstiness)
      5: spectral_flatness   - geometric/arithmetic mean ratio across frequency PSD (ATSC/OFDM vs CW/NFM)
      6: crest_factor        - peak-to-mean dB concentration of mean PSD (carrier spine vs flat-top)
      7: bw_3db_frac         - fraction of patch columns within -3 dB of peak
      8: bw_10db_frac        - fraction of patch columns within -10 dB of peak
      9: shape_factor        - -3dB to -10dB skirt steepness ratio (rectangular 8VSB/OFDM vs Gaussian FM)
     10: carrier_spine_ratio - central 3-column energy vs sideband energy (AM/CW pilot vs suppressed/FSK)
     11: twin_peak_score     - bimodal FSK / stereo sideband separation score
     12: pilot_edge_score    - asymmetric lower-edge pilot tone prominence (ATSC +309 kHz 8VSB pilot)
     13: freq_centroid_drift - row-to-row spectral centroid drift (swept chirp / geometric vs stationary)
     14: freq_centroid_std   - standard deviation of instantaneous row peak bin (FM deviation / glyph contour)
     15: interior_hole_score - 2D topological interior variance score (2D glyphs/rasters vs 1D carriers)
    """
    patch = np.asarray(roi.patch, dtype=np.float32)
    if patch.ndim != 2 or patch.size < 4:
        return np.zeros(16, dtype=np.float32)

    rows, cols = patch.shape
    floor_db = float(np.percentile(patch, 20))
    peak_db = float(np.max(patch))
    dyn_db = max(1.0, peak_db - floor_db)
    snr_2d = np.maximum(0.0, patch - floor_db)

    # 0..2: Macro bandwidth, contour aspect, SNR
    bw_hz = max(1_000.0, float(roi.bandwidth_hz))
    norm_bw = float(np.clip(np.log10(bw_hz / 1_000.0) / 3.0, 0.0, 1.2))  # 1kHz->0, 1MHz->1.0
    w_ref = float(roi.width_bins) if getattr(roi, "width_bins", 0) > 0 else float(cols)
    h_ref = float(roi.height_rows) if getattr(roi, "height_rows", 0) > 0 else float(rows)
    aspect_ratio = float(np.tanh((w_ref / max(1.0, h_ref)) * 0.35))
    snr_norm = float(np.clip(roi.snr_db / 45.0, 0.0, 1.2))

    # 3..4: Temporal duty cycle & burst transition rate
    row_peaks = np.max(snr_2d, axis=1)
    active_thresh = max(3.0, dyn_db * 0.35)
    active_rows = (row_peaks >= active_thresh).astype(np.float32)
    duty_cycle = float(np.mean(active_rows))
    if rows > 2:
        transitions = float(np.sum(np.abs(np.diff(active_rows)))) / float(rows - 1)
    else:
        transitions = 0.0
    row_burst_rate = float(np.clip(transitions * 2.5, 0.0, 1.0))

    # 5..9: Spectral PSD flatness, crest factor, -3dB / -10dB shape factor
    active_idx = active_rows > 0.5
    mean_psd_db = np.mean(patch[active_idx], axis=0) if np.any(active_idx) else np.mean(patch, axis=0)
    psd_rel_db = np.maximum(0.0, mean_psd_db - float(np.percentile(mean_psd_db, 15)))
    lin_psd = 10.0 ** (np.clip(psd_rel_db, 0.0, 50.0) / 10.0)
    geom_mean = float(np.exp(np.mean(np.log(lin_psd + 1e-6))))
    arith_mean = float(np.mean(lin_psd) + 1e-6)
    spectral_flatness = float(np.clip(geom_mean / arith_mean, 0.0, 1.0))

    psd_max = float(np.max(psd_rel_db))
    crest_factor = float(np.clip((psd_max - float(np.mean(psd_rel_db))) / 25.0, 0.0, 1.2))

    bw_3db_frac = float(np.mean(psd_rel_db >= max(1.5, psd_max - 3.0)))
    bw_10db_frac = float(np.mean(psd_rel_db >= max(1.0, psd_max - 10.0)))
    shape_factor = float(np.clip(bw_3db_frac / max(0.02, bw_10db_frac), 0.0, 1.0))

    # 10: Central carrier spine vs sidebands
    mid = cols // 2
    c0, c1 = max(0, mid - 1), min(cols, mid + 2)
    spine_e = float(np.mean(psd_rel_db[c0:c1])) if c1 > c0 else psd_max
    side_mask = np.ones(cols, dtype=bool)
    side_mask[c0:c1] = False
    side_e = float(np.mean(psd_rel_db[side_mask])) if np.any(side_mask) else 0.0
    carrier_spine_ratio = float(np.clip((spine_e - side_e) / max(3.0, psd_max), -1.0, 1.0))

    # 11: Twin-peak FSK / bimodal score
    if cols >= 6:
        left_pk = float(np.max(psd_rel_db[:mid]))
        right_pk = float(np.max(psd_rel_db[mid:]))
        valley = float(psd_rel_db[mid])
        twin_peak_score = float(np.clip((min(left_pk, right_pk) - valley) / max(3.0, psd_max), 0.0, 1.0))
    else:
        twin_peak_score = 0.0

    # 12: Lower-quarter pilot tone prominence (ATSC 8VSB pilot carrier + flat shelf)
    q1 = max(1, cols // 3)
    pilot_pk = float(np.max(psd_rel_db[:q1]))
    shelf_med = float(np.median(psd_rel_db))
    pilot_edge_score = float(
        np.clip(((pilot_pk - shelf_med) / max(3.0, psd_max)) * (1.0 if bw_10db_frac > 0.45 else 0.25), 0.0, 1.0)
    )

    # 13..14: Row-to-row spectral centroid drift (chirps) & deviation std
    col_coords = np.linspace(-1.0, 1.0, cols, dtype=np.float32)
    weights = np.maximum(0.0, snr_2d - active_thresh * 0.5) + 1e-4
    centroids = np.sum(weights * col_coords[None, :], axis=1) / np.sum(weights, axis=1)
    if np.sum(active_idx) >= 4:
        act_c = centroids[active_idx]
        # Monotonic linear slope vs random jitter
        t_axis = np.linspace(-1.0, 1.0, len(act_c), dtype=np.float32)
        slope = abs(float(np.mean(act_c * t_axis)) / (float(np.mean(t_axis ** 2)) + 1e-6))
        freq_centroid_drift = float(np.clip(slope * 1.8, 0.0, 1.2))
        freq_centroid_std = float(np.clip(np.std(act_c) * 2.5, 0.0, 1.2))
    else:
        freq_centroid_drift = 0.0
        freq_centroid_std = 0.0

    # 15: 2D interior structural variance (distinguishes 2D alphanumeric glyphs/rasters from 1D vertical bars)
    if rows >= 6 and cols >= 6:
        row_norm = snr_2d / (np.max(snr_2d, axis=1, keepdims=True) + 1e-3)
        temporal_mod = float(np.mean(np.std(row_norm, axis=0)))
        interior_hole_score = float(np.clip(temporal_mod * 3.0, 0.0, 1.2))
    else:
        interior_hole_score = 0.0

    return np.array(
        [
            norm_bw,
            aspect_ratio,
            snr_norm,
            duty_cycle,
            row_burst_rate,
            spectral_flatness,
            crest_factor,
            bw_3db_frac,
            bw_10db_frac,
            shape_factor,
            carrier_spine_ratio,
            twin_peak_score,
            pilot_edge_score,
            freq_centroid_drift,
            freq_centroid_std,
            interior_hole_score,
        ],
        dtype=np.float32,
    )


def compute_extended_rf_features(
    roi: PictogramROI,
    zoom_metrics: Optional[Dict[str, Any]] = None,
) -> np.ndarray:
    """
    Computes the 24-dimensional Dual-Scale Ultra-Zoom + Physical RF Feature Vector
    (16-D spectrogram morphology + 8-D sub-10 Hz precision carrier & subcarrier features)
    fused with the 1000-D Hailo-8 ResNet-50 embedding (1024-D total):
      0..15 : compute_roi_physical_features(roi)
      16    : scale_log_span        - 0.0 for 31.25 kHz Ultra-Zoom, 0.60 for 250 kHz Zoom, 1.0 for 1 MHz Macro
      17    : carrier_tonality_norm - 7.63 Hz/bin coherent carrier spine prominence (AM/CW/Pilot vs FM/FSK)
      18    : carrier_narrow_line   - exp(-carrier_3db_width_hz / 180 Hz) sub-100 Hz line indicator
      19    : sideband_symmetry     - 7.63 Hz/bin lower/upper sideband correlation
      20    : fsk_rail_norm         - 7.63 Hz/bin bimodal Mark/Space FSK rail score
      21    : stereo_19k_pilot_norm - 19.000 kHz WFM stereo MPX pilot SNR
      22    : ctcss_pl_detected     - TIA-603 CTCSS subaudible tone lock indicator (67-254.1 Hz)
      23    : fine_obw_log_norm     - log-normalized precision occupied bandwidth (500 Hz .. 6 MHz)
    """
    base16 = compute_roi_physical_features(roi)
    zm = zoom_metrics or {}

    span_hz = float(zm.get("roi_span_hz", 1_000_000.0))
    scale_log_span = float(np.clip(math.log10(max(15_000.0, span_hz) / 31_250.0) / 1.50515, -0.3, 1.2))

    if "carrier_tonality_db" in zm:
        tonality_norm = float(np.clip(float(zm.get("carrier_tonality_db", 6.0)) / 24.0, 0.0, 1.2))
    else:
        tonality_norm = float(np.clip(0.5 + 0.5 * float(base16[10]), 0.0, 1.0))

    if "carrier_3db_width_hz" in zm:
        c_w = max(5.0, float(zm.get("carrier_3db_width_hz", 1200.0)))
        narrow_line = float(np.clip(math.exp(-c_w / 180.0), 0.0, 1.0))
    else:
        narrow_line = float(np.clip((1.0 - float(base16[7])) * max(0.0, float(base16[10])), 0.0, 1.0))

    if "sideband_symmetry" in zm:
        sb_sym = float(np.clip(float(zm.get("sideband_symmetry", 0.5)), 0.0, 1.0))
    else:
        sb_sym = 0.55

    fsk_rail = float(np.clip(float(zm.get("fsk_rail_score", base16[11])), 0.0, 1.0))
    pilot_19k = float(np.clip(float(zm.get("stereo_pilot_snr_db", 0.0)) / 20.0, 0.0, 1.2))
    ctcss_flag = 1.0 if float(zm.get("ctcss_hz", 0.0)) >= 60.0 else 0.0

    obw_val = float(zm.get("measured_obw_hz", zm.get("obw_hz", max(1_000.0, float(roi.bandwidth_hz)))))
    fine_obw_norm = float(np.clip(math.log10(max(500.0, obw_val) / 500.0) / 4.05, 0.0, 1.2))

    ext8 = np.array(
        [
            scale_log_span,
            tonality_norm,
            narrow_line,
            sb_sym,
            fsk_rail,
            pilot_19k,
            ctcss_flag,
            fine_obw_norm,
        ],
        dtype=np.float32,
    )
    return np.concatenate([base16, ext8], axis=0).astype(np.float32)


# Mapping from fine-grained & legacy neural head classes -> (PictogramCategory, Display Label)
NEURAL_CLASS_SEMANTICS: dict[str, tuple[PictogramCategory, str]] = {
    # Upgraded 10-class RF Modulation + Pictogram Head
    "wfm_stereo": (PictogramCategory.CARRIER_COMB, "WFM-Stereo-MPX"),
    "nfm_voice": (PictogramCategory.CARRIER_COMB, "NFM-Voice-Carrier"),
    "am_voice": (PictogramCategory.CARRIER_COMB, "AM-Voice-Sideband"),
    "atsc_tv": (PictogramCategory.RASTER, "ATSC-8VSB-6MHz"),
    "fsk_pager": (PictogramCategory.BURST_MATRIX, "FSK-Pager-Packet"),
    "ism_telemetry": (PictogramCategory.BURST_MATRIX, "ISM-OOK-FSK-Telem"),
    "adsb_pulse": (PictogramCategory.BURST_MATRIX, "ADS-B-ModeS-Pulse"),
    "glyph": (PictogramCategory.GLYPH, "Glyph/Symbol-glyph"),
    "geometric": (PictogramCategory.GEOMETRIC, "Geometric-Sweep-Chirp"),
    "noise_reject": (PictogramCategory.ANOMALY, "Noise-Reject-Cluster"),
    # Legacy 6-class compatibility
    "raster": (PictogramCategory.RASTER, "RasterArt-Pattern-raster"),
    "tone_matrix": (PictogramCategory.BURST_MATRIX, "Tone-Burst-Matrix"),
    "carrier_comb": (PictogramCategory.CARRIER_COMB, "Carrier-Comb"),
}


class WaterfallPictogramExtractor:
    """
    Maintains a rolling 2D waterfall buffer and segments 2D candidate spectrogram ROIs.
    Filters out unmodulated 1-bin CW lines and full-band wideband impulse noise.
    """

    def __init__(
        self,
        rows: int = 64,
        cols: int = 128,
        snr_threshold_db: float = 10.0,
        min_area: int = 16,
        min_dim: int = 4,
    ) -> None:
        self.rows = max(16, rows)
        self.cols = max(32, cols)
        self.snr_threshold_db = snr_threshold_db
        self.min_area = min_area
        self.min_dim = min_dim

        self._lock = threading.Lock()
        self._waterfall = np.full((self.rows, self.cols), -100.0, dtype=np.float32)
        self._row_count = 0
        self._row_interval_s = 0.05

    @property
    def waterfall(self) -> np.ndarray:
        with self._lock:
            return self._waterfall.copy()

    def set_row_interval(self, interval_s: float) -> None:
        self._row_interval_s = max(0.001, interval_s)

    def reset(self) -> None:
        """Clear the rolling waterfall buffer between distinct candidate frequencies."""
        with self._lock:
            self._waterfall.fill(-100.0)
            self._row_count = 0

    def push_row(self, dbm_row: list[float] | np.ndarray) -> None:
        """Push a 1D frequency power slice into the rolling waterfall."""
        row_arr = np.asarray(dbm_row, dtype=np.float32)
        if len(row_arr) != self.cols:
            x_old = np.linspace(0, 1, len(row_arr))
            x_new = np.linspace(0, 1, self.cols)
            row_arr = np.interp(x_new, x_old, row_arr).astype(np.float32)

        with self._lock:
            self._waterfall[:-1] = self._waterfall[1:]
            self._waterfall[-1] = row_arr
            self._row_count += 1

    def push_iq(
        self,
        iq_samples: np.ndarray,
        sample_rate_sps: float = 2_000_000.0,
        center_freq_hz: float = 100_000_000.0,
    ) -> None:
        """Compute Hann-windowed FFT on complex IQ samples and push row into waterfall."""
        _ = (sample_rate_sps, center_freq_hz)
        n = len(iq_samples)
        if n < self.cols:
            return
        win = np.hanning(n)
        spectrum = np.fft.fftshift(np.fft.fft(iq_samples * win))
        power = 20.0 * np.log10(np.abs(spectrum) + 1e-9) - 80.0
        self.push_row(power)

    def extract_candidates(
        self,
        center_freq_hz: float,
        span_hz: float,
    ) -> list[PictogramROI]:
        """Segment 2D clusters above adaptive per-column median noise floor."""
        with self._lock:
            wf = self._waterfall.copy()

        col_medians = np.median(wf, axis=0)
        global_floor = float(np.median(col_medians))
        noise_floor_2d = np.tile(col_medians, (self.rows, 1))

        snr_map = wf - noise_floor_2d
        binary_mask = (snr_map > self.snr_threshold_db).astype(np.uint8) * 255

        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        closed_mask = cv2.morphologyEx(binary_mask, cv2.MORPH_CLOSE, kernel)

        contours, _ = cv2.findContours(closed_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        candidates: list[PictogramROI] = []

        bin_hz = span_hz / self.cols
        freq_start = center_freq_hz - (span_hz / 2.0)

        for cnt in contours:
            c, r, w, h = cv2.boundingRect(cnt)
            area = w * h

            if area < self.min_area or w < self.min_dim or h < self.min_dim:
                continue
            if w <= 2 and h >= int(self.rows * 0.75):
                continue
            if h <= 2 and w >= int(self.cols * 0.75):
                continue

            # Preserve physical frequency scale across narrow/wide signals (minimum 48-col = 375 kHz context)
            min_ctx_c = min(self.cols, max(w + max(4, w // 4), 48))
            mid_c = c + w // 2
            pc0 = max(0, min(self.cols - min_ctx_c, mid_c - min_ctx_c // 2))
            pc1 = min(self.cols, pc0 + min_ctx_c)
            pr0 = max(0, r - 2)
            pr1 = min(self.rows, r + h + 2)

            roi_patch = wf[pr0:pr1, pc0:pc1]
            roi_snr = float(np.max(snr_map[r : r + h, c : c + w]))
            roi_peak = float(np.max(wf[r : r + h, c : c + w]))

            dyn_range = max(16.0, (roi_peak - global_floor) + 4.0)
            norm = np.clip(
                (roi_patch - global_floor) / dyn_range * 255.0,
                0,
                255,
            ).astype(np.uint8)
            rgb_patch = cv2.applyColorMap(norm, cv2.COLORMAP_VIRIDIS)
            resized_rgb = cv2.resize(rgb_patch, (224, 224), interpolation=cv2.INTER_LINEAR)

            roi_center_f = freq_start + (c + w / 2.0) * bin_hz
            roi_bw = w * bin_hz
            roi_duration = h * self._row_interval_s

            candidates.append(
                PictogramROI(
                    roi_id=str(uuid.uuid4())[:8],
                    row_min=r,
                    row_max=r + h,
                    col_min=c,
                    col_max=c + w,
                    width_bins=w,
                    height_rows=h,
                    area=area,
                    center_freq_hz=roi_center_f,
                    bandwidth_hz=roi_bw,
                    duration_s=roi_duration,
                    snr_db=roi_snr,
                    peak_dbm=roi_peak,
                    patch=roi_patch,
                    normalized_rgb=resized_rgb,
                )
            )

        return candidates


class HailoPictogramClassifier:
    """
    Manages the Hailo-8 PCIe AI coprocessor (26 TOPS) and runs deep neural inference
    to classify RF spectrogram pictograms and modulation footprints using hardware
    backbone feature extraction fused with 16-D physical spectral morphology features
    and a calibrated multi-layer classification head.
    """

    DEFAULT_HEF = _resolve_default_hef()
    DEFAULT_WEIGHTS = str(PROJECT_ROOT / "models" / "hailo_pictogram_head_weights.npz")

    def __init__(
        self,
        hef_path: Optional[str] = None,
        weights_path: Optional[str] = None,
        fallback_ok: bool = True,
    ) -> None:
        self.hef_path = hef_path or _resolve_default_hef()
        self.weights_path = weights_path or os.environ.get(
            "PICTOSIGHT_WEIGHTS_PATH", self.DEFAULT_WEIGHTS
        )
        self.fallback_ok = fallback_ok
        self._lock = threading.Lock()

        self._vdevice: Optional[Any] = None
        self._network_group: Optional[Any] = None
        self._infer_pipeline: Optional[Any] = None
        self._infer_pipeline_cm: Optional[Any] = None
        self._activation_cm: Optional[Any] = None
        self._input_name: str = ""
        self._output_name: str = ""
        self._is_initialized = False
        self._is_hardware_active = False

        self._head_classes: list[str] = [
            "carrier_comb", "geometric", "glyph", "noise_reject", "raster", "tone_matrix"
        ]
        self._head_weights_loaded = False
        self._bn_mean: Optional[np.ndarray] = None
        self._bn_var: Optional[np.ndarray] = None
        self._bn_weight: Optional[np.ndarray] = None
        self._bn_bias: Optional[np.ndarray] = None
        self._bn_eps: float = 1e-5
        self._w1: Optional[np.ndarray] = None
        self._b1: Optional[np.ndarray] = None
        self._w2: Optional[np.ndarray] = None
        self._b2: Optional[np.ndarray] = None

        # Optional trained 16-D CPU fallback MLP weights stored in NPZ
        self._cpu_mean: Optional[np.ndarray] = None
        self._cpu_std: Optional[np.ndarray] = None
        self._cpu_w1: Optional[np.ndarray] = None
        self._cpu_b1: Optional[np.ndarray] = None
        self._cpu_w2: Optional[np.ndarray] = None
        self._cpu_b2: Optional[np.ndarray] = None

        self._load_head_weights()

    def _load_head_weights(self) -> bool:
        """Load trained BatchNorm + 2-layer MLP spectrogram classification head weights."""
        if os.path.exists(self.weights_path):
            try:
                npz = np.load(self.weights_path)
                self._bn_mean = npz["bn_mean"]
                self._bn_var = npz["bn_var"]
                self._bn_weight = npz["bn_weight"]
                self._bn_bias = npz["bn_bias"]
                self._bn_eps = float(npz.get("bn_eps", 1e-5))
                self._w1 = npz["w1"]
                self._b1 = npz["b1"]
                self._w2 = npz["w2"]
                self._b2 = npz["b2"]
                if "classes" in npz:
                    self._head_classes = [str(c) for c in npz["classes"]]
                if "cpu_w1" in npz and "cpu_w2" in npz:
                    self._cpu_mean = npz["cpu_mean"]
                    self._cpu_std = npz["cpu_std"]
                    self._cpu_w1 = npz["cpu_w1"]
                    self._cpu_b1 = npz["cpu_b1"]
                    self._cpu_w2 = npz["cpu_w2"]
                    self._cpu_b2 = npz["cpu_b2"]
                self._head_weights_loaded = True
                logger.info(
                    "Loaded Hailo-8 pictogram classification head (%d classes, dim=%d) from %s",
                    len(self._head_classes),
                    int(self._w1.shape[0]),
                    self.weights_path,
                )
                return True
            except Exception as exc:
                logger.warning("Could not load head weights from %s: %s", self.weights_path, exc)
        return False

    @property
    def is_hardware_active(self) -> bool:
        return self._is_hardware_active

    def initialize(self) -> bool:
        """Configure PCIe VDevice interface and activate persistent Hailo-8 streaming pipeline."""
        with self._lock:
            if self._is_initialized:
                return True

            if not HAILO_AVAILABLE:
                logger.warning("HailoRT Python platform not installed; using CPU neural/spectral fallback.")
                self._is_initialized = self.fallback_ok
                return self._is_initialized

            if not os.path.exists(self.hef_path):
                logger.warning("HEF model not found at %s; using CPU neural/spectral fallback.", self.hef_path)
                self._is_initialized = self.fallback_ok
                return self._is_initialized

            try:
                logger.info("Initializing Hailo-8 PCIe NPU with HEF: %s", self.hef_path)
                hef = HEF(self.hef_path)
                self._input_name = hef.get_input_vstream_infos()[0].name
                self._output_name = hef.get_output_vstream_infos()[0].name

                self._vdevice = VDevice()
                configure_params = ConfigureParams.create_from_hef(
                    hef, interface=HailoStreamInterface.PCIe
                )
                self._network_group = self._vdevice.configure(hef, configure_params)[0]
                self._network_group_params = self._network_group.create_params()

                self._input_vstreams_params = InputVStreamParams.make(
                    self._network_group, format_type=FormatType.UINT8
                )
                self._output_vstreams_params = OutputVStreamParams.make(
                    self._network_group, format_type=FormatType.FLOAT32
                )

                self._activation_cm = self._network_group.activate(self._network_group_params)
                self._activation_cm.__enter__()

                self._infer_pipeline_cm = InferVStreams(
                    self._network_group,
                    self._input_vstreams_params,
                    self._output_vstreams_params,
                )
                self._infer_pipeline = self._infer_pipeline_cm.__enter__()

                # Warmup pass
                dummy = np.zeros((1, 224, 224, 3), dtype=np.uint8)
                _ = self._infer_pipeline.infer({self._input_name: dummy})

                self._is_hardware_active = True
                self._is_initialized = True
                logger.info("Hailo-8 26 TOPS PCIe inference pipeline active.")
                return True

            except Exception as exc:
                logger.error("Failed to initialize Hailo-8 hardware: %s", exc)
                self._cleanup_hardware()
                self._is_hardware_active = False
                self._is_initialized = self.fallback_ok
                return self._is_initialized

    def extract_hailo_embedding(self, rgb_224: np.ndarray) -> Optional[np.ndarray]:
        """Run raw 224x224x3 uint8 tensor on Hailo-8 NPU and return flattened float32 embedding."""
        if not self._is_initialized:
            self.initialize()
        if not self._is_hardware_active or self._infer_pipeline is None:
            return None
        with self._lock:
            in_tensor = np.expand_dims(rgb_224, axis=0)
            res = self._infer_pipeline.infer({self._input_name: in_tensor})
            return np.asarray(res[self._output_name][0], dtype=np.float32).reshape(-1)

    def _cleanup_hardware(self) -> None:
        if self._infer_pipeline_cm:
            try:
                self._infer_pipeline_cm.__exit__(None, None, None)
            except Exception:
                pass
            self._infer_pipeline_cm = None
            self._infer_pipeline = None

        if self._activation_cm:
            try:
                self._activation_cm.__exit__(None, None, None)
            except Exception:
                pass
            self._activation_cm = None

        self._network_group = None

        if self._vdevice:
            try:
                self._vdevice.release()
            except Exception:
                pass
            self._vdevice = None

    def _predict_head(
        self,
        deep_features: np.ndarray,
        phys_features: Optional[np.ndarray] = None,
    ) -> Tuple[str, float, list[dict[str, Any]]]:
        """Run calibrated BatchNorm + ReLU MLP transfer head over Hailo-8 + physical RF features."""
        if not self._head_weights_loaded:
            self._load_head_weights()

        flat_deep = np.asarray(deep_features, dtype=np.float32).reshape(-1)
        if self._head_weights_loaded and self._w1 is not None and self._w2 is not None:
            expected_dim = int(self._w1.shape[0])
            if phys_features is not None and expected_dim > len(flat_deep):
                needed_phys = expected_dim - len(flat_deep)
                p_vec = phys_features.astype(np.float32)
                if len(p_vec) >= needed_phys:
                    feat_vec = np.concatenate([flat_deep, p_vec[:needed_phys]], axis=0)
                else:
                    pad = np.zeros(needed_phys - len(p_vec), dtype=np.float32)
                    feat_vec = np.concatenate([flat_deep, p_vec, pad], axis=0)
            else:
                feat_vec = flat_deep[:expected_dim]

            x_norm = (
                (feat_vec - self._bn_mean) / np.sqrt(self._bn_var + self._bn_eps) * self._bn_weight
                + self._bn_bias
            )
            h1 = np.maximum(0.0, np.dot(x_norm, self._w1) + self._b1)
            logits = np.dot(h1, self._w2) + self._b2
            exp_l = np.exp(logits - np.max(logits))
            probs = exp_l / np.sum(exp_l)

            top_indices = np.argsort(probs)[::-1]
            candidates = [
                {"class": self._head_classes[idx], "score": round(float(probs[idx]), 4)}
                for idx in top_indices
            ]
            best_idx = int(top_indices[0])
            return self._head_classes[best_idx], float(probs[best_idx]), candidates

        top_idx = int(np.argmax(flat_deep))
        conf = float(flat_deep[top_idx])
        return "glyph", min(0.99, max(0.40, conf)), [{"class": "glyph", "score": conf}]

    def _predict_cpu_fallback(self, phys_features: np.ndarray, roi: PictogramROI) -> Tuple[str, float, list[dict[str, Any]]]:
        """Run trained 24-D / 16-D spectral MLP head if available, else deterministic spectral heuristic."""
        if self._cpu_w1 is not None and self._cpu_w2 is not None and self._cpu_mean is not None and self._cpu_std is not None:
            cpu_dim = int(self._cpu_w1.shape[0])
            p_in = phys_features[:cpu_dim] if len(phys_features) >= cpu_dim else np.pad(phys_features, (0, cpu_dim - len(phys_features)))
            x_norm = (p_in - self._cpu_mean) / (self._cpu_std + 1e-5)
            h1 = np.maximum(0.0, np.dot(x_norm, self._cpu_w1) + self._cpu_b1)
            logits = np.dot(h1, self._cpu_w2) + self._cpu_b2
            exp_l = np.exp(logits - np.max(logits))
            probs = exp_l / np.sum(exp_l)
            top_indices = np.argsort(probs)[::-1]
            candidates = [
                {"class": self._head_classes[idx], "score": round(float(probs[idx]), 4)}
                for idx in top_indices
            ]
            best_idx = int(top_indices[0])
            return self._head_classes[best_idx], float(probs[best_idx]), candidates

        aspect = roi.width_bins / max(roi.height_rows, 1)
        if 0.5 <= aspect <= 2.0:
            return "glyph", 0.78, [{"class": "glyph", "score": 0.78}]
        if aspect > 2.5:
            return "geometric", 0.74, [{"class": "geometric", "score": 0.74}]
        return "nfm_voice", 0.72, [{"class": "nfm_voice", "score": 0.72}]

    def classify_roi(
        self,
        roi: PictogramROI,
        source: str = "hackrf",
        zoom_metrics: Optional[Dict[str, Any]] = None,
    ) -> PictogramDetection:
        """Execute Hailo-8 neural classification on a 224x224x3 Viridis spectrogram ROI (Macro or Ultra-Zoom)."""
        if not self._is_initialized:
            self.initialize()

        t0 = time.perf_counter()
        lat_ms = 0.0
        used_hw = False
        top_candidates: list[dict[str, Any]] = []
        best_class = "nfm_voice"
        confidence = 0.50
        phys_feats = compute_extended_rf_features(roi, zoom_metrics=zoom_metrics)

        if self._is_hardware_active and self._infer_pipeline is not None:
            try:
                with self._lock:
                    in_tensor = np.expand_dims(roi.normalized_rgb, axis=0)
                    t_inf_start = time.perf_counter()
                    res = self._infer_pipeline.infer({self._input_name: in_tensor})
                    lat_ms = (time.perf_counter() - t_inf_start) * 1000.0
                    deep_features = np.asarray(res[self._output_name][0], dtype=np.float32).reshape(-1)
                    used_hw = True

                best_class, confidence, top_candidates = self._predict_head(deep_features, phys_feats)

            except Exception as ex:
                logger.warning("Hailo-8 hardware inference pass failed, falling back: %s", ex)
                used_hw = False

        if not used_hw:
            best_class, confidence, top_candidates = self._predict_cpu_fallback(phys_feats, roi)
            lat_ms = (time.perf_counter() - t0) * 1000.0

        category, label = NEURAL_CLASS_SEMANTICS.get(
            best_class,
            (PictogramCategory.ANOMALY, f"Pictogram-{best_class}"),
        )

        now = time.time()
        iso = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat()

        return PictogramDetection(
            detection_id=f"pct-{str(uuid.uuid4())[:8]}",
            timestamp=now,
            utc_iso=iso,
            center_freq_hz=roi.center_freq_hz,
            bandwidth_hz=roi.bandwidth_hz,
            duration_s=roi.duration_s,
            snr_db=round(roi.snr_db, 2),
            peak_dbm=round(roi.peak_dbm, 2),
            category=category,
            label=label,
            confidence=round(confidence, 3),
            top_candidates=top_candidates,
            bbox=(roi.row_min, roi.row_max, roi.col_min, roi.col_max),
            hailo_latency_ms=round(lat_ms, 2),
            hardware_accelerated=used_hw,
            model_name=os.path.basename(self.hef_path),
            source=source,
        )

    def close(self) -> None:
        """Release Hailo-8 PCIe VDevice resources cleanly."""
        with self._lock:
            self._is_hardware_active = False
            self._is_initialized = False
            self._cleanup_hardware()
