"""
Custom Graphical + Alphanumerical Telemetry & RF Visualization Widgets (PyQt6 QPainter)
Provides simultaneous graphical gauges, animated pipeline nodes, live RF spectrum traces,
and >=3-frame 1 MHz AI Pictogram spectrogram displays for every timed operation.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple, Any

import numpy as np
from PyQt6.QtCore import Qt, QRectF, QPointF, QTimer
from PyQt6.QtGui import (
    QColor,
    QFont,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
)
from PyQt6.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QLabel, QSizePolicy


class TelemetryMeterPill(QWidget):
    """
    Compact hardware/status indicator combining a live graphical fill meter,
    pulsing LED state dot, and crisp alphanumeric readout.
    """

    def __init__(
        self,
        title: str,
        initial_text: str = "INIT",
        accent_hex: str = "#00f0ff",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._title = title
        self._text = initial_text
        self._accent = QColor(accent_hex)
        self._pct = 100.0
        self._active_pulse = False
        self._phase = 0.0
        self.setMinimumHeight(26)
        self.setMaximumHeight(34)
        self.setMinimumWidth(115)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_telemetry(
        self,
        text: str,
        pct: float = 100.0,
        accent_hex: Optional[str] = None,
        pulsing: bool = False,
        tooltip: str = "",
    ) -> None:
        self._text = text
        self._pct = max(0.0, min(100.0, float(pct)))
        if accent_hex:
            self._accent = QColor(accent_hex)
        self._active_pulse = pulsing
        if tooltip:
            self.setToolTip(tooltip)
        self.update()

    def step_animation(self, phase: float) -> None:
        self._phase = phase
        if self._active_pulse:
            self.update()

    def paintEvent(self, _event: Any) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = float(self.width()), float(self.height())
        rect = QRectF(1.0, 1.0, w - 2.0, h - 2.0)

        # Pill background
        p.setBrush(QColor("#0d1728"))
        border_col = QColor(self._accent)
        border_col.setAlpha(155 if not self._active_pulse else int(140 + 100 * math.sin(self._phase)))
        p.setPen(QPen(border_col, 1.2))
        p.drawRoundedRect(rect, 5.0, 5.0)

        # Bottom graphical bar (4px high)
        bar_bg = QRectF(5.0, h - 6.5, w - 10.0, 3.5)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#17263c"))
        p.drawRoundedRect(bar_bg, 1.8, 1.8)

        fill_w = (w - 10.0) * (self._pct / 100.0)
        if fill_w > 1.0:
            p.setBrush(self._accent)
            p.drawRoundedRect(QRectF(5.0, h - 6.5, fill_w, 3.5), 1.8, 1.8)

        # Status LED dot
        led_alpha = 255 if not self._active_pulse else int(130 + 125 * (0.5 + 0.5 * math.sin(self._phase * 2.0)))
        led_col = QColor(self._accent)
        led_col.setAlpha(led_alpha)
        p.setBrush(led_col)
        p.drawEllipse(QPointF(10.0, (h - 5.0) * 0.5), 3.6, 3.6)

        # Alphanumeric text
        font = QFont("Monospace", 8)
        font.setBold(True)
        p.setFont(font)
        p.setPen(QColor("#e2e8f0"))
        text_rect = QRectF(18.0, 1.0, w - 22.0, h - 7.0)
        fm = p.fontMetrics()
        elided = fm.elidedText(self._text, Qt.TextElideMode.ElideRight, int(text_rect.width()))
        p.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, elided)
        p.end()


class PipelineStageStepper(QWidget):
    """
    Graphical 4-node multi-layer pipeline indicator + alphanumeric Elapsed/ETA clock.
    Stages:
      1: L1 Wideband
      2: L2 Zoom
      3: L3 1MHz IQ+AI
      4: Profile & Gqrx
    """

    STAGE_LABELS = ("1.L1 SWEEP", "2.L2 ZOOM", "3.1MHz IQ+AI", "4.PROFILE/GQRX")

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._active_stage = 0       # 0=idle/init, 1..4=active, 5=all complete
        self._is_running = False
        self._elapsed_s = 0.0
        self._eta_s = 0.0
        self._phase = 0.0
        self.setMinimumHeight(22)
        self.setMaximumHeight(26)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_state(
        self,
        stage: int,
        running: bool,
        elapsed_s: float = 0.0,
        eta_s: float = 0.0,
    ) -> None:
        self._active_stage = stage
        self._is_running = running
        self._elapsed_s = elapsed_s
        self._eta_s = eta_s
        self.update()

    def step_animation(self, phase: float) -> None:
        self._phase = phase
        if self._is_running:
            self.update()

    def paintEvent(self, _event: Any) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = float(self.width()), float(self.height())

        # Right side reserved for alphanumeric timer readout
        clock_w = 128.0 if w >= 520 else 104.0
        nodes_w = max(180.0, w - clock_w - 6.0)
        seg_w = nodes_w / 4.0

        font = QFont("Monospace", 8)
        font.setBold(True)
        p.setFont(font)

        for i, lbl in enumerate(self.STAGE_LABELS):
            idx = i + 1
            x0 = i * seg_w + 2.0
            pill_r = QRectF(x0, 2.0, seg_w - 5.0, h - 4.0)

            done = (self._active_stage > idx) or (self._active_stage == 4 and not self._is_running)
            active = (self._active_stage == idx and self._is_running) or (idx == 1 and self._active_stage == 0 and self._is_running)

            if done:
                bg = QColor("#063626")
                fg = QColor("#34d399")
                bd = QColor("#10b981")
                prefix = "✔ "
            elif active:
                pulse = int(155 + 95 * (0.5 + 0.5 * math.sin(self._phase * 2.5)))
                bg = QColor("#0a2f4d")
                fg = QColor("#00f0ff")
                bd = QColor(0, 240, 255, pulse)
                prefix = "◉ "
            else:
                bg = QColor("#0e1624")
                fg = QColor("#64748b")
                bd = QColor("#1e2d45")
                prefix = ""

            p.setBrush(bg)
            p.setPen(QPen(bd, 1.6 if active else 1.0))
            p.drawRoundedRect(pill_r, 4.0, 4.0)

            p.setPen(fg)
            txt = p.fontMetrics().elidedText(prefix + lbl, Qt.TextElideMode.ElideRight, int(pill_r.width() - 6))
            p.drawText(pill_r, Qt.AlignmentFlag.AlignCenter, txt)

        # Alphanumeric Elapsed & ETA clock pill
        clk_rect = QRectF(nodes_w + 4.0, 2.0, clock_w - 4.0, h - 4.0)
        p.setBrush(QColor("#111b2e"))
        p.setPen(QPen(QColor("#243b5e"), 1.0))
        p.drawRoundedRect(clk_rect, 4.0, 4.0)

        if self._is_running:
            clk_txt = f"T:{self._elapsed_s:4.1f}s | ETA {self._eta_s:2.0f}s"
            p.setPen(QColor("#fbbf24"))
        elif self._elapsed_s > 0:
            clk_txt = f"✔ DONE {self._elapsed_s:4.1f}s"
            p.setPen(QColor("#34d399"))
        else:
            clk_txt = "READY 0.0s"
            p.setPen(QColor("#94a3b8"))

        elided_clk = p.fontMetrics().elidedText(clk_txt, Qt.TextElideMode.ElideRight, int(clk_rect.width() - 6))
        p.drawText(clk_rect, Qt.AlignmentFlag.AlignCenter, elided_clk)
        p.end()


class DualLayerProgressBar(QWidget):
    """
    Graphical dual-track progress gauge (overall % + sub-task %) with overlaid
    high-contrast alphanumeric status telemetry.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._overall_pct = 0.0
        self._sub_pct = 0.0
        self._status_text = "System Ready — Select Quick, Deep, or Custom Scan"
        self._busy = False
        self._phase = 0.0
        self.setMinimumHeight(22)
        self.setMaximumHeight(26)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_progress(
        self,
        overall_pct: float,
        sub_pct: float,
        text: str,
        busy: bool = False,
    ) -> None:
        self._overall_pct = max(0.0, min(100.0, float(overall_pct)))
        self._sub_pct = max(0.0, min(100.0, float(sub_pct)))
        self._status_text = text
        self._busy = busy
        self.update()

    def step_animation(self, phase: float) -> None:
        self._phase = phase
        if self._busy:
            self.update()

    def paintEvent(self, _event: Any) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = float(self.width()), float(self.height())
        outer = QRectF(1.0, 1.0, w - 2.0, h - 2.0)

        p.setBrush(QColor("#091220"))
        p.setPen(QPen(QColor("#00f0ff" if self._busy else "#1e3250"), 1.1))
        p.drawRoundedRect(outer, 4.5, 4.5)

        # Primary overall % fill
        fill_w = (w - 4.0) * (self._overall_pct / 100.0)
        if fill_w > 1.5:
            grad = QLinearGradient(2.0, 0.0, w - 2.0, 0.0)
            grad.setColorAt(0.0, QColor(2, 132, 199, 175))
            grad.setColorAt(0.55, QColor(16, 185, 129, 185))
            grad.setColorAt(1.0, QColor(0, 240, 255, 195))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(grad)
            p.drawRoundedRect(QRectF(2.0, 2.0, fill_w, h - 7.0), 3.5, 3.5)

        # Bottom sub-task % accent bar (3px)
        sub_w = (w - 4.0) * (self._sub_pct / 100.0)
        if sub_w > 1.5:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#f59e0b" if self._busy else "#10b981"))
            p.drawRoundedRect(QRectF(2.0, h - 4.5, sub_w, 2.8), 1.4, 1.4)

        # Alphanumeric readout
        font = QFont("Monospace", 8)
        font.setBold(True)
        p.setFont(font)
        p.setPen(QColor("#ffffff"))
        full_label = f"{self._overall_pct:5.1f}% │ {self._status_text}"
        txt_rect = QRectF(8.0, 1.0, w - 16.0, h - 4.0)
        elided = p.fontMetrics().elidedText(full_label, Qt.TextElideMode.ElideRight, int(txt_rect.width()))
        p.drawText(txt_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, elided)
        p.end()


class LiveSpectrumCanvas(QWidget):
    """
    Real-time Wideband RF Spectrum Plotter (QPainter).
    Renders measured dBFS spectrum, 20th-percentile baseline noise floor,
    user's interactive Min Peak dBFS threshold line, and detected carrier markers.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._freqs_mhz: Optional[np.ndarray] = None
        self._powers_db: Optional[np.ndarray] = None
        self._floor_db: Optional[np.ndarray] = None
        self._min_peak_thresh: float = -72.0
        self._min_snr_thresh: float = 9.0
        self._highlight_mhz: Optional[float] = None
        self._markers: List[Tuple[float, float, str]] = []  # (freq_mhz, peak_db, short_lbl)
        self.setMinimumHeight(105)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_thresholds(self, min_snr_db: float, min_peak_dbfs: float) -> None:
        self._min_snr_thresh = float(min_snr_db)
        self._min_peak_thresh = float(min_peak_dbfs)
        self.update()

    def set_spectrum_data(
        self,
        freqs_hz: Optional[np.ndarray],
        powers_db: Optional[np.ndarray],
        floor_db: Optional[np.ndarray] = None,
    ) -> None:
        if freqs_hz is not None and powers_db is not None and len(freqs_hz) > 4:
            # Downsample cleanly to <= 420 points for fast 60fps touch rendering
            step = max(1, len(freqs_hz) // 420)
            self._freqs_mhz = (freqs_hz[::step] / 1e6).astype(np.float32)
            self._powers_db = powers_db[::step].astype(np.float32)
            if floor_db is not None and len(floor_db) == len(powers_db):
                self._floor_db = floor_db[::step].astype(np.float32)
            else:
                self._floor_db = None
        self.update()

    def set_signal_markers(
        self,
        markers: List[Tuple[float, float, str]],
        highlight_mhz: Optional[float] = None,
    ) -> None:
        self._markers = markers
        self._highlight_mhz = highlight_mhz
        self.update()

    def paintEvent(self, _event: Any) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = float(self.width()), float(self.height())

        p.setBrush(QColor("#070d18"))
        p.setPen(QPen(QColor("#1c314f"), 1.0))
        p.drawRoundedRect(QRectF(0.5, 0.5, w - 1.0, h - 1.0), 5.0, 5.0)

        pad_l, pad_r, pad_t, pad_b = 38.0, 10.0, 16.0, 18.0
        pw = max(40.0, w - pad_l - pad_r)
        ph = max(30.0, h - pad_t - pad_b)

        db_min, db_max = -95.0, -15.0
        if self._powers_db is not None and len(self._powers_db) > 0:
            db_min = min(-90.0, float(np.min(self._powers_db)) - 6.0)
            db_max = max(-20.0, float(np.max(self._powers_db)) + 8.0)

        def y_of(db: float) -> float:
            norm = (db - db_min) / max(10.0, db_max - db_min)
            return pad_t + ph * (1.0 - max(0.0, min(1.0, norm)))

        # Grid lines + dBFS axis labels
        small_f = QFont("Monospace", 7)
        p.setFont(small_f)
        grid_pen = QPen(QColor("#132238"), 1.0, Qt.PenStyle.DotLine)
        for step_i in range(4):
            db_v = db_min + (db_max - db_min) * (step_i / 3.0)
            yy = y_of(db_v)
            p.setPen(grid_pen)
            p.drawLine(QPointF(pad_l, yy), QPointF(pad_l + pw, yy))
            p.setPen(QColor("#64748b"))
            p.drawText(QRectF(2.0, yy - 7.0, pad_l - 4.0, 14.0), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, f"{db_v:.0f}")

        # Interactive Min Peak dBFS threshold line
        y_th = y_of(self._min_peak_thresh)
        th_pen = QPen(QColor("#ec4899"), 1.2, Qt.PenStyle.DashLine)
        p.setPen(th_pen)
        p.drawLine(QPointF(pad_l, y_th), QPointF(pad_l + pw, y_th))
        p.setPen(QColor("#f472b6"))
        p.drawText(
            QRectF(pad_l + 4.0, 2.0, 250.0, 13.0),
            Qt.AlignmentFlag.AlignLeft,
            f"THRESH: Peak >= {self._min_peak_thresh:.0f} dBFS | SNR >= +{self._min_snr_thresh:.0f} dB",
        )

        if self._freqs_mhz is None or self._powers_db is None or len(self._freqs_mhz) < 2:
            p.setPen(QColor("#38bdf8"))
            msg_f = QFont("Monospace", 9)
            msg_f.setBold(True)
            p.setFont(msg_f)
            p.drawText(
                QRectF(pad_l, pad_t, pw, ph),
                Qt.AlignmentFlag.AlignCenter,
                "WIDEBAND RF SPECTRUM READY — START QUICK / DEEP / CUSTOM SCAN",
            )
            p.end()
            return

        f_lo = float(self._freqs_mhz[0])
        f_hi = max(f_lo + 0.1, float(self._freqs_mhz[-1]))

        def x_of(f_mhz: float) -> float:
            return pad_l + pw * ((f_mhz - f_lo) / (f_hi - f_lo))

        # Frequency X-axis ticks
        p.setFont(small_f)
        for ti in range(5):
            fm = f_lo + (f_hi - f_lo) * (ti / 4.0)
            xx = x_of(fm)
            p.setPen(grid_pen)
            p.drawLine(QPointF(xx, pad_t), QPointF(xx, pad_t + ph))
            p.setPen(QColor("#7dd3fc"))
            p.drawText(QRectF(xx - 26.0, pad_t + ph + 2.0, 52.0, 14.0), Qt.AlignmentFlag.AlignCenter, f"{fm:.1f}M")

        # Baseline noise floor curve
        if self._floor_db is not None and len(self._floor_db) == len(self._freqs_mhz):
            fl_path = QPainterPath()
            for i in range(len(self._freqs_mhz)):
                pt = QPointF(x_of(float(self._freqs_mhz[i])), y_of(float(self._floor_db[i])))
                if i == 0:
                    fl_path.moveTo(pt)
                else:
                    fl_path.lineTo(pt)
            p.setPen(QPen(QColor("#f59e0b"), 1.1, Qt.PenStyle.DashLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(fl_path)

        # Spectrum trace + gradient fill
        poly = QPolygonF()
        trace_path = QPainterPath()
        poly.append(QPointF(x_of(f_lo), pad_t + ph))
        for i in range(len(self._freqs_mhz)):
            pt = QPointF(x_of(float(self._freqs_mhz[i])), y_of(float(self._powers_db[i])))
            poly.append(pt)
            if i == 0:
                trace_path.moveTo(pt)
            else:
                trace_path.lineTo(pt)
        poly.append(QPointF(x_of(f_hi), pad_t + ph))

        fill_grad = QLinearGradient(0.0, pad_t, 0.0, pad_t + ph)
        fill_grad.setColorAt(0.0, QColor(0, 240, 255, 115))
        fill_grad.setColorAt(0.7, QColor(16, 185, 129, 45))
        fill_grad.setColorAt(1.0, QColor(7, 13, 24, 5))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(fill_grad)
        p.drawPolygon(poly)

        p.setPen(QPen(QColor("#00f0ff"), 1.4))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(trace_path)

        # Viable signal peak markers
        for fm, pk, lbl in self._markers:
            if f_lo <= fm <= f_hi:
                xx = x_of(fm)
                yy = y_of(pk)
                is_sel = self._highlight_mhz is not None and abs(fm - self._highlight_mhz) < 0.05
                col = QColor("#00ffaa" if is_sel else "#fbbf24")
                p.setBrush(col)
                p.setPen(QPen(QColor("#ffffff"), 1.0))
                rad = 4.2 if is_sel else 2.8
                p.drawEllipse(QPointF(xx, yy), rad, rad)
                if is_sel:
                    p.setPen(QColor("#00ffaa"))
                    p.drawText(QRectF(min(w - 95.0, max(pad_l, xx - 42.0)), max(2.0, yy - 15.0), 90.0, 13.0), Qt.AlignmentFlag.AlignCenter, f"{fm:.3f}M")

        # Title overlay
        p.setPen(QColor("#94a3b8"))
        p.drawText(QRectF(pad_l + 4.0, 2.0, pw - 8.0, 13.0), Qt.AlignmentFlag.AlignRight, f"SPAN {f_lo:.1f}-{f_hi:.1f} MHz • FLOOR (AMBER) • TRACE (CYAN)")
        p.end()


class PictogramFramesWidget(QWidget):
    """
    Displays the >=3 1.000 MHz STFT spectrogram frames strip (with ROI boxes) alongside
    graphical Hailo-8 26 TOPS neural class confidence bars and alphanumeric RF metrics.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._pixmap: Optional[QPixmap] = None
        self._freq_mhz: float = 0.0
        self._label: str = "Awaiting Layer 3 1MHz IQ Capture (>=3 frames/MHz)"
        self._conf: float = 0.0
        self._snr_db: float = 0.0
        self._peak_dbfs: float = -80.0
        self._bw_khz: float = 0.0
        self._frames_n: int = 3
        self._hw_accel: bool = True
        self._top_classes: List[Tuple[str, float]] = []
        self.setMinimumHeight(105)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_frame_analysis(
        self,
        freq_mhz: float,
        label: str,
        confidence: float,
        snr_db: float,
        peak_dbfs: float,
        bw_hz: float,
        frames_count: int,
        hw_accel: bool,
        composite_rgb: Optional[np.ndarray] = None,
        thumbnail_path: str = "",
        top_candidates: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        self._freq_mhz = freq_mhz
        self._label = label
        self._conf = confidence
        self._snr_db = snr_db
        self._peak_dbfs = peak_dbfs
        self._bw_khz = bw_hz / 1000.0
        self._frames_n = max(3, int(frames_count))
        self._hw_accel = hw_accel

        if composite_rgb is not None and composite_rgb.ndim == 3:
            h, w, ch = composite_rgb.shape
            bytes_per_line = ch * w
            qimg = QImage(composite_rgb.data, w, h, bytes_per_line, QImage.Format.Format_RGB888).copy()
            self._pixmap = QPixmap.fromImage(qimg)
        elif thumbnail_path:
            pm = QPixmap(thumbnail_path)
            if not pm.isNull():
                self._pixmap = pm

        if top_candidates:
            self._top_classes = [(str(c.get("class", "")), float(c.get("score", 0.0))) for c in top_candidates[:4]]
        else:
            self._top_classes = [(label[:16], confidence)]
        self.update()

    def paintEvent(self, _event: Any) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = float(self.width()), float(self.height())

        p.setBrush(QColor("#080f1c"))
        p.setPen(QPen(QColor("#1c314f"), 1.0))
        p.drawRoundedRect(QRectF(0.5, 0.5, w - 1.0, h - 1.0), 5.0, 5.0)

        # Left 58% = >=3 1MHz Spectrogram Frames strip; Right 42% = Neural Bars & Numeric Gauges
        left_w = max(180.0, w * 0.58)
        img_rect = QRectF(6.0, 18.0, left_w - 10.0, h - 24.0)

        hdr_font = QFont("Monospace", 8)
        hdr_font.setBold(True)
        p.setFont(hdr_font)
        p.setPen(QColor("#00f0ff"))
        hdr_str = (
            f"1.000 MHz IQ WATERFALL ({self._frames_n} FRAMES) @ {self._freq_mhz:.4f} MHz"
            if self._freq_mhz > 0
            else "1.000 MHz MULTI-FRAME PICTOGRAM WATERFALL (MIN 3 FRAMES / 1 MHz)"
        )
        p.drawText(QRectF(6.0, 2.0, left_w - 8.0, 15.0), Qt.AlignmentFlag.AlignVCenter, hdr_str)

        if self._pixmap is not None and not self._pixmap.isNull():
            scaled = self._pixmap.scaled(
                int(img_rect.width()),
                int(img_rect.height()),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            px = img_rect.x() + (img_rect.width() - scaled.width()) * 0.5
            py = img_rect.y() + (img_rect.height() - scaled.height()) * 0.5
            p.drawPixmap(int(px), int(py), scaled)
        else:
            p.setBrush(QColor("#0d1728"))
            p.setPen(QPen(QColor("#1e3250"), 1.0, Qt.PenStyle.DashLine))
            p.drawRoundedRect(img_rect, 4.0, 4.0)
            p.setPen(QColor("#64748b"))
            p.drawText(img_rect, Qt.AlignmentFlag.AlignCenter, "3+ 1MHz STFT Spectrogram Frames\nRender Live During Layer 3 & Signal Selection")

        # Right column: Hailo-8 Neural Confidence Gauges + RF Metrics
        rx = left_w + 4.0
        rw = max(100.0, w - rx - 6.0)
        npu_tag = "⚡HAILO-8 26T" if self._hw_accel else "AI ENGINE"
        p.setPen(QColor("#f472b6"))
        p.drawText(QRectF(rx, 2.0, rw, 15.0), Qt.AlignmentFlag.AlignVCenter, f"{npu_tag}: {self._label[:24]}")

        small_f = QFont("Monospace", 7)
        small_f.setBold(True)
        p.setFont(small_f)

        # Metric summary bar
        metrics_txt = f"SNR +{self._snr_db:.1f}dB │ {self._peak_dbfs:.1f}dBFS │ OBW {self._bw_khz:.1f}kHz"
        p.setPen(QColor("#34d399"))
        p.drawText(QRectF(rx, 18.0, rw, 13.0), Qt.AlignmentFlag.AlignLeft, p.fontMetrics().elidedText(metrics_txt, Qt.TextElideMode.ElideRight, int(rw)))

        # Draw graphical probability bars
        bar_y = 35.0
        row_h = max(14.0, min(19.0, (h - 38.0) / 4.0))
        for idx, (cname, score) in enumerate(self._top_classes[:4]):
            yy = bar_y + idx * row_h
            lbl_w = min(84.0, rw * 0.46)
            p.setPen(QColor("#cbd5e1" if idx == 0 else "#94a3b8"))
            p.drawText(QRectF(rx, yy, lbl_w, row_h - 2.0), Qt.AlignmentFlag.AlignVCenter, cname[:12])

            bx = rx + lbl_w + 3.0
            bw = max(24.0, rw - lbl_w - 36.0)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor("#152238"))
            p.drawRoundedRect(QRectF(bx, yy + 3.0, bw, row_h - 7.0), 2.5, 2.5)

            fw = bw * max(0.03, min(1.0, score))
            p.setBrush(QColor("#00ffaa" if idx == 0 else "#38bdf8"))
            p.drawRoundedRect(QRectF(bx, yy + 3.0, fw, row_h - 7.0), 2.5, 2.5)

            p.setPen(QColor("#00f0ff" if idx == 0 else "#94a3b8"))
            p.drawText(QRectF(bx + bw + 3.0, yy, 32.0, row_h - 2.0), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, f"{int(round(score * 100))}%")

        p.end()
