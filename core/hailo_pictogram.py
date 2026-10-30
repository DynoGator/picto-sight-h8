"""
Picto-Sight H8 | Hailo-8 (26 TOPS) RF Waterfall Pictogram Extractor & Neural Classifier
Hardware-accelerated 2D spectrogram ROI segmentation, Viridis 224x224x3 normalization,
HailoRT PCIe VDevice feature extraction, and calibrated transfer-head classification.
"""

from __future__ import annotations

import datetime
import enum
import logging
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional, Tuple

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
        str(PROJECT_ROOT / "models" / "resnet_v1_50_h8l.hef"),
        str(PROJECT_ROOT / "models" / "resnet_v1_50_h8.hef"),
        "/usr/share/hailo-models/resnet_v1_50_h8l.hef",
        "/usr/share/hailo-models/resnet_v1_50_h8.hef",
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return "/usr/share/hailo-models/resnet_v1_50_h8l.hef"


class PictogramCategory(str, enum.Enum):
    GLYPH = "PICTO_GLYPH"                # Alphanumeric character / spectrogram symbol
    RASTER = "PICTO_RASTER"              # Dense 2D waterfall image / SSTV / OFDM raster
    GEOMETRIC = "PICTO_GEOMETRIC"        # Swept polygons, chirps, stepped carriers
    BURST_MATRIX = "PICTO_BURST_MATRIX"  # Pulsed multi-tone / FSK matrix
    CARRIER_COMB = "PICTO_CARRIER_COMB"  # Multi-carrier / MPX harmonic comb
    ANOMALY = "PICTO_ANOMALY"            # High-SNR unclassified structural energy


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

            roi_patch = wf[r : r + h, c : c + w]
            roi_snr = float(np.max(snr_map[r : r + h, c : c + w]))
            roi_peak = float(np.max(roi_patch))

            norm = np.clip(
                (roi_patch - global_floor) / max(self.snr_threshold_db * 2.5, 15.0) * 255.0,
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
    backbone feature extraction paired with a calibrated transfer classification head.
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
                self._head_weights_loaded = True
                logger.info("Loaded Hailo-8 pictogram classification head from %s", self.weights_path)
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
                logger.warning("HailoRT Python platform not installed; using CPU heuristic fallback.")
                self._is_initialized = self.fallback_ok
                return self._is_initialized

            if not os.path.exists(self.hef_path):
                logger.warning("HEF model not found at %s; using CPU heuristic fallback.", self.hef_path)
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

    def _predict_head(self, features: np.ndarray) -> Tuple[str, float, list[dict[str, Any]]]:
        """Run calibrated BatchNorm + ReLU MLP transfer head over Hailo-8 feature vector."""
        if not self._head_weights_loaded:
            self._load_head_weights()

        if self._head_weights_loaded and self._w1 is not None and self._w2 is not None:
            x_norm = (
                (features - self._bn_mean) / np.sqrt(self._bn_var + self._bn_eps) * self._bn_weight
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

        top_idx = int(np.argmax(features))
        conf = float(features[top_idx])
        return "glyph", min(0.99, max(0.40, conf)), [{"class": "glyph", "score": conf}]

    def classify_roi(
        self,
        roi: PictogramROI,
        source: str = "hackrf",
    ) -> PictogramDetection:
        """Execute Hailo-8 neural classification on a 224x224x3 Viridis spectrogram ROI."""
        if not self._is_initialized:
            self.initialize()

        t0 = time.perf_counter()
        lat_ms = 0.0
        used_hw = False
        top_candidates: list[dict[str, Any]] = []
        category = PictogramCategory.ANOMALY
        label = "Unclassified Anomaly"
        confidence = 0.50

        if self._is_hardware_active and self._infer_pipeline is not None:
            try:
                with self._lock:
                    in_tensor = np.expand_dims(roi.normalized_rgb, axis=0)
                    t_inf_start = time.perf_counter()
                    res = self._infer_pipeline.infer({self._input_name: in_tensor})
                    lat_ms = (time.perf_counter() - t_inf_start) * 1000.0
                    deep_features = res[self._output_name][0]
                    used_hw = True

                best_class, confidence, top_candidates = self._predict_head(deep_features)

                category_map = {
                    "glyph": PictogramCategory.GLYPH,
                    "raster": PictogramCategory.RASTER,
                    "geometric": PictogramCategory.GEOMETRIC,
                    "tone_matrix": PictogramCategory.BURST_MATRIX,
                    "carrier_comb": PictogramCategory.CARRIER_COMB,
                    "noise_reject": PictogramCategory.ANOMALY,
                }
                category = category_map.get(best_class, PictogramCategory.ANOMALY)

                label_map = {
                    "glyph": f"Glyph/Symbol-{best_class}",
                    "raster": f"RasterArt-Pattern-{best_class}",
                    "geometric": f"Geometric-Sweep-{best_class}",
                    "tone_matrix": f"Tone-Burst-Matrix-{best_class}",
                    "carrier_comb": f"Carrier-Comb-{best_class}",
                    "noise_reject": "Noise-Reject-Cluster",
                }
                label = label_map.get(best_class, f"Pictogram-{best_class}")

            except Exception as ex:
                logger.warning("Hailo-8 hardware inference pass failed, falling back: %s", ex)
                used_hw = False

        if not used_hw:
            lat_ms = (time.perf_counter() - t0) * 1000.0
            aspect = roi.width_bins / max(roi.height_rows, 1)
            if 0.5 <= aspect <= 2.0:
                category = PictogramCategory.GLYPH
                label = f"SimGlyph-A{int(roi.area)}"
                confidence = 0.78
            elif aspect > 2.5:
                category = PictogramCategory.GEOMETRIC
                label = "SimGeometric-Chirp"
                confidence = 0.72
            else:
                category = PictogramCategory.CARRIER_COMB
                label = "SimCarrier-Comb"
                confidence = 0.70
            top_candidates = [{"class": "carrier_comb", "score": confidence}]

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
