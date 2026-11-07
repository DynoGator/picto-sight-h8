"""
ui/tv_dialog.py - Live Broadcast Television Demodulator & Spectrum Dialog
=========================================================================
Designed for zero-overflow operation on the 5" 800x480 DSI touchscreen while scaling
cleanly to 1080p+ HDMI monitors. Provides:
  - Live GNU Radio ATSC 1.0 (8VSB) Digital TV Demodulation + mpv/ffplay MPEG-TS video
  - Live Analog NTSC / Fast-Scan ATV 525-line Envelope CRT Raster View
  - Real-time 7.2 MHz RF Channel Spectrum + +309.441 kHz 8VSB Pilot Lock Reticle
  - 1-tap Southern Colorado / Penrose TV presets & Wideband VHF/UHF TV Channel Scan
  - 1-tap handoff to Gqrx 8.0 MHz Wideband Pandapter
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Dict, List, Optional

import numpy as np
from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QImage, QLinearGradient, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from core.demod_engine import identify_atsc_tv_channel
from core.tv_demodulator import (
    SOUTHERN_CO_TV_PRESETS,
    TVReceiverTelemetry,
    get_all_fcc_tv_channels,
    get_tv_controller,
    scan_active_tv_channels,
)


class TVSpectrumAndRasterCanvas(QWidget):
    """Custom GPU-light QPainter canvas rendering 6 MHz ATSC spectrum + pilot lock & NTSC raster."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(165)
        self._telem = TVReceiverTelemetry()
        self._raster: Optional[np.ndarray] = None

    def update_state(self, telem: TVReceiverTelemetry, raster: Optional[np.ndarray] = None) -> None:
        self._telem = telem
        self._raster = raster
        self.update()

    def paintEvent(self, _event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        w = float(self.width())
        h = float(self.height())

        # Deep tactical background
        painter.fillRect(QRectF(0, 0, w, h), QColor("#060b14"))
        painter.setPen(QPen(QColor("#1e293b"), 1))
        painter.drawRect(QRectF(0.5, 0.5, w - 1.0, h - 1.0))

        show_raster = self._telem.mode == "ANALOG_NTSC" and self._raster is not None
        spec_rect = QRectF(8.0, 8.0, (w * 0.54 - 12.0) if show_raster else (w - 16.0), h - 16.0)

        self._draw_spectrum(painter, spec_rect)
        if show_raster and self._raster is not None:
            ras_rect = QRectF(w * 0.54 + 4.0, 8.0, w * 0.46 - 12.0, h - 16.0)
            self._draw_raster(painter, ras_rect, self._raster)

        painter.end()

    def _draw_spectrum(self, painter: QPainter, rect: QRectF) -> None:
        x0, y0, rw, rh = rect.x(), rect.y(), rect.width(), rect.height()
        pad_l, pad_r, pad_t, pad_b = 34.0, 8.0, 20.0, 18.0
        px0 = x0 + pad_l
        py0 = y0 + pad_t
        pw = max(40.0, rw - pad_l - pad_r)
        ph = max(30.0, rh - pad_t - pad_b)

        center_mhz = self._telem.center_freq_hz / 1e6
        f_min = center_mhz - 3.6
        f_max = center_mhz + 3.6
        lo_edge = center_mhz - 3.0
        hi_edge = center_mhz + 3.0
        pilot_mhz = lo_edge + 0.309441

        def fx(f_mhz: float) -> float:
            return px0 + ((f_mhz - f_min) / (f_max - f_min)) * pw

        db_min = -102.0
        db_max = -28.0

        def fy(db_val: float) -> float:
            clamped = max(db_min, min(db_max, db_val))
            return py0 + ph - ((clamped - db_min) / (db_max - db_min)) * ph

        # Highlight 6.000 MHz FCC TV channel passband
        ch_x0 = fx(lo_edge)
        ch_x1 = fx(hi_edge)
        painter.fillRect(QRectF(ch_x0, py0, ch_x1 - ch_x0, ph), QColor(14, 165, 233, 24))

        # Horizontal dBFS grid lines
        font_sm = QFont("DejaVu Sans Mono", 7)
        painter.setFont(font_sm)
        for db_tick in (-95, -80, -65, -50, -35):
            yy = fy(float(db_tick))
            painter.setPen(QPen(QColor("#17253a"), 1, Qt.PenStyle.DotLine))
            painter.drawLine(QPointF(px0, yy), QPointF(px0 + pw, yy))
            painter.setPen(QColor("#64748b"))
            painter.drawText(QRectF(x0, yy - 6, pad_l - 3, 12), Qt.AlignmentFlag.AlignRight, f"{db_tick}")

        # Lower/Upper 6 MHz channel edges
        painter.setPen(QPen(QColor("#334155"), 1, Qt.PenStyle.DashLine))
        painter.drawLine(QPointF(ch_x0, py0), QPointF(ch_x0, py0 + ph))
        painter.drawLine(QPointF(ch_x1, py0), QPointF(ch_x1, py0 + ph))

        # +309.441 kHz ATSC 8VSB Pilot Carrier Reticle
        px_pilot = fx(pilot_mhz)
        pilot_col = QColor("#10b981") if self._telem.pilot_locked else QColor("#f59e0b")
        painter.setPen(QPen(pilot_col, 1.4, Qt.PenStyle.DashLine))
        painter.drawLine(QPointF(px_pilot, py0), QPointF(px_pilot, py0 + ph))
        painter.drawText(
            QRectF(px_pilot + 3, py0 + 2, 130, 14),
            Qt.AlignmentFlag.AlignLeft,
            f"8VSB Pilot {pilot_mhz:.3f}M",
        )

        # Draw live spectrum curve if available
        freqs = self._telem.spectrum_freqs_mhz
        vals = self._telem.spectrum_dbfs
        if len(freqs) >= 8 and len(freqs) == len(vals):
            path = QPainterPath()
            fill_path = QPainterPath()
            first_x = fx(freqs[0])
            fill_path.moveTo(first_x, py0 + ph)
            for idx, (fm, dbv) in enumerate(zip(freqs, vals)):
                xx = fx(fm)
                yy = fy(dbv)
                if idx == 0:
                    path.moveTo(xx, yy)
                else:
                    path.lineTo(xx, yy)
                fill_path.lineTo(xx, yy)
            fill_path.lineTo(fx(freqs[-1]), py0 + ph)
            fill_path.closeSubpath()

            grad = QLinearGradient(0, py0, 0, py0 + ph)
            grad.setColorAt(0.0, QColor(225, 29, 72, 125))
            grad.setColorAt(1.0, QColor(14, 165, 233, 18))
            painter.fillPath(fill_path, grad)
            painter.setPen(QPen(QColor("#38bdf8"), 1.6))
            painter.drawPath(path)

            # Thermal noise floor reference
            y_nf = fy(self._telem.noise_floor_dbfs)
            painter.setPen(QPen(QColor("#94a3b8"), 1, Qt.PenStyle.DotLine))
            painter.drawLine(QPointF(px0, y_nf), QPointF(px0 + pw, y_nf))
        else:
            painter.setPen(QColor("#94a3b8"))
            msg = "Press [▶ START TV] to stream live 8.0 MSPS HackRF ATSC 8VSB / NTSC spectrum"
            painter.drawText(QRectF(px0, py0, pw, ph), Qt.AlignmentFlag.AlignCenter, msg)

        # Top banner & bottom axis labels
        painter.setFont(QFont("DejaVu Sans", 8, QFont.Weight.Bold))
        painter.setPen(QColor("#f8fafc"))
        painter.drawText(
            QRectF(px0, y0, pw, 16),
            Qt.AlignmentFlag.AlignLeft,
            f"Ch {self._telem.channel} • {self._telem.callsign} ({lo_edge:.1f}–{hi_edge:.1f} MHz, 6.0 MHz BW)",
        )
        painter.setFont(font_sm)
        painter.setPen(QColor("#94a3b8"))
        painter.drawText(QRectF(ch_x0 - 20, py0 + ph + 2, 55, 14), Qt.AlignmentFlag.AlignCenter, f"{lo_edge:.1f}M")
        painter.drawText(QRectF(fx(center_mhz) - 28, py0 + ph + 2, 56, 14), Qt.AlignmentFlag.AlignCenter, f"{center_mhz:.1f}M")
        painter.drawText(QRectF(ch_x1 - 35, py0 + ph + 2, 55, 14), Qt.AlignmentFlag.AlignCenter, f"{hi_edge:.1f}M")

    def _draw_raster(self, painter: QPainter, rect: QRectF, raster: np.ndarray) -> None:
        painter.setPen(QPen(QColor("#334155"), 1))
        painter.drawRect(rect)
        h_arr, w_arr = raster.shape[:2]
        raw_bytes = np.ascontiguousarray(raster, dtype=np.uint8).tobytes()
        qimg = QImage(raw_bytes, w_arr, h_arr, w_arr, QImage.Format.Format_Grayscale8).copy()
        inner = rect.adjusted(2, 18, -2, -2)
        painter.drawImage(inner, qimg)
        painter.setFont(QFont("DejaVu Sans", 8, QFont.Weight.Bold))
        painter.setPen(QColor("#38bdf8"))
        painter.drawText(
            QRectF(rect.x() + 4, rect.y() + 2, rect.width() - 8, 15),
            Qt.AlignmentFlag.AlignLeft,
            "LIVE NTSC / ATV AM VIDEO RASTER (525-LINE)",
        )


class BroadcastTVReceiverDialog(QDialog):
    """
    Non-crashing, DSI-optimized Broadcast TV Demodulator & Channel Analyzer Dialog.
    """

    scan_completed_sig = pyqtSignal(dict)
    open_gqrx_wideband_sig = pyqtSignal(int, int)  # (center_freq_hz, channel)

    def __init__(
        self,
        initial_freq_hz: float = 539_000_000.0,
        compact_screen: bool = True,
        auto_start: bool = True,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("📺 Picto-Sight H8 — Live Broadcast TV Demodulator (ATSC 8VSB & NTSC)")
        self._compact = compact_screen
        self._ctrl = get_tv_controller()
        self._all_channels = get_all_fcc_tv_channels()
        self._handed_off_to_gqrx = False
        self._auto_opened_mpv = False

        if compact_screen:
            self.resize(784, 424)
            self.setMaximumSize(798, 440)
        else:
            self.resize(960, 580)

        self.setStyleSheet(
            """
            QDialog { background-color: #070d19; color: #e2e8f0; }
            QLabel { color: #e2e8f0; font-size: 11px; }
            QComboBox, QSpinBox {
                background-color: #0f172a; color: #f8fafc; border: 1px solid #334155;
                border-radius: 4px; padding: 2px 5px; font-size: 11px; min-height: 24px;
            }
            QPushButton {
                background-color: #1e293b; color: #f8fafc; border: 1px solid #334155;
                border-radius: 5px; padding: 4px 8px; font-size: 11px; font-weight: bold;
                min-height: 26px;
            }
            QPushButton:hover { background-color: #334155; border-color: #38bdf8; }
            """
        )

        self._build_ui()
        self.scan_completed_sig.connect(self._on_tv_scan_finished)

        self._select_frequency(initial_freq_hz)

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(300)
        self._poll_timer.timeout.connect(self._poll_telemetry)
        self._poll_timer.start()

        if auto_start:
            QTimer.singleShot(120, self._on_start_clicked)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 5, 6, 5)
        root.setSpacing(4)

        # Row 1: Southern Colorado Quick Presets + Channel & Mode Selectors
        top_row = QHBoxLayout()
        top_row.setSpacing(4)

        lbl_ch = QLabel("TV Ch:")
        lbl_ch.setStyleSheet("font-weight: bold; color: #fb7185;")
        top_row.addWidget(lbl_ch)

        self.cmb_channel = QComboBox()
        for entry in self._all_channels:
            ch = entry["channel"]
            c_mhz = entry["center_hz"] / 1e6
            self.cmb_channel.addItem(f"Ch {ch:02d} ({c_mhz:.0f}M) {entry['call']}", entry["center_hz"])
        self.cmb_channel.currentIndexChanged.connect(self._on_channel_combo_changed)
        top_row.addWidget(self.cmb_channel, stretch=2)

        self.cmb_mode = QComboBox()
        self.cmb_mode.addItem("ATSC 8VSB Digital + Video", "ATSC_8VSB")
        self.cmb_mode.addItem("Analog NTSC / ATV Raster", "ANALOG_NTSC")
        self.cmb_mode.currentIndexChanged.connect(self._on_mode_combo_changed)
        top_row.addWidget(self.cmb_mode, stretch=1)

        top_row.addWidget(QLabel("LNA:"))
        self.spn_lna = QSpinBox()
        self.spn_lna.setRange(0, 40)
        self.spn_lna.setSingleStep(8)
        self.spn_lna.setValue(32)
        self.spn_lna.setSuffix("dB")
        top_row.addWidget(self.spn_lna)

        top_row.addWidget(QLabel("VGA:"))
        self.spn_vga = QSpinBox()
        self.spn_vga.setRange(0, 62)
        self.spn_vga.setSingleStep(2)
        self.spn_vga.setValue(30)
        self.spn_vga.setSuffix("dB")
        top_row.addWidget(self.spn_vga)

        self.chk_amp = QCheckBox("+14dB")
        self.chk_amp.setChecked(True)
        self.chk_amp.setStyleSheet("color: #cbd5e1; font-size: 10px;")
        top_row.addWidget(self.chk_amp)

        root.addLayout(top_row)

        # Row 2: Quick Southern Colorado Tower Buttons (fits comfortably in <760px)
        preset_row = QHBoxLayout()
        preset_row.setSpacing(3)
        lbl_p = QLabel("CO Towers:")
        lbl_p.setStyleSheet("color: #94a3b8; font-size: 10px;")
        preset_row.addWidget(lbl_p)

        for p in SOUTHERN_CO_TV_PRESETS[:6]:
            btn = QPushButton(f"Ch{p['channel']} {p['call'].split()[0]}")
            btn.setStyleSheet(
                "QPushButton { background-color: #172554; color: #93c5fd; border: 1px solid #1d4ed8; "
                "padding: 2px 6px; font-size: 10px; min-height: 22px; }"
                "QPushButton:hover { background-color: #1e40af; color: #ffffff; }"
            )
            hz = int(p["center_hz"])
            btn.clicked.connect(lambda _checked=False, f=hz: self._quick_tune_preset(f))
            preset_row.addWidget(btn)

        preset_row.addStretch(1)
        root.addLayout(preset_row)

        # Row 3: Live Graphical Spectrum + Analog NTSC Raster Canvas
        self.canvas = TVSpectrumAndRasterCanvas(self)
        root.addWidget(self.canvas, stretch=1)

        # Row 4: Alphanumeric + Graphical Telemetry Bar
        metrics_frame = QFrame()
        metrics_frame.setStyleSheet(
            "QFrame { background-color: #0b1324; border: 1px solid #1e293b; border-radius: 5px; }"
        )
        m_lay = QHBoxLayout(metrics_frame)
        m_lay.setContentsMargins(6, 3, 6, 3)
        m_lay.setSpacing(8)

        self.lbl_lock_badge = QLabel("● STANDBY")
        self.lbl_lock_badge.setStyleSheet("color: #94a3b8; font-weight: bold; font-size: 11px;")
        m_lay.addWidget(self.lbl_lock_badge)

        self.lbl_pilot_stats = QLabel("Pilot: -- dBFS (SNR +0.0 dB)")
        self.lbl_pilot_stats.setStyleSheet("color: #38bdf8; font-family: monospace; font-size: 10px;")
        m_lay.addWidget(self.lbl_pilot_stats)

        self.lbl_ts_stats = QLabel("MPEG-TS: 0.0 kbps (0 pkts)")
        self.lbl_ts_stats.setStyleSheet("color: #a7f3d0; font-family: monospace; font-size: 10px;")
        m_lay.addWidget(self.lbl_ts_stats)

        self.bar_snr = QProgressBar()
        self.bar_snr.setRange(0, 30)
        self.bar_snr.setValue(0)
        self.bar_snr.setFormat("SNR %v dB")
        self.bar_snr.setFixedWidth(105)
        self.bar_snr.setFixedHeight(16)
        self.bar_snr.setStyleSheet(
            "QProgressBar { background: #0f172a; border: 1px solid #334155; border-radius: 3px; "
            "text-align: center; color: #f8fafc; font-size: 9px; }"
            "QProgressBar::chunk { background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #0284c7, stop:1 #10b981); }"
        )
        m_lay.addWidget(self.bar_snr)
        root.addWidget(metrics_frame)

        # Status detail line
        self.lbl_status = QLabel("Ready — Select a Broadcast TV channel and press [▶ START TV].")
        self.lbl_status.setStyleSheet("color: #cbd5e1; font-size: 10px;")
        root.addWidget(self.lbl_status)

        # Row 5: Action Controls
        act_row = QHBoxLayout()
        act_row.setSpacing(5)

        self.btn_start = QPushButton("▶ START TV")
        self.btn_start.setStyleSheet(
            "QPushButton { background-color: #be123c; color: #ffffff; border: 1px solid #fb7185; }"
            "QPushButton:hover { background-color: #e11d48; }"
        )
        self.btn_start.clicked.connect(self._on_start_clicked)
        act_row.addWidget(self.btn_start)

        self.btn_video = QPushButton("🎬 VIDEO PLAYER (mpv)")
        self.btn_video.setStyleSheet(
            "QPushButton { background-color: #065f46; color: #ecfdf5; border: 1px solid #10b981; }"
            "QPushButton:hover { background-color: #059669; }"
        )
        self.btn_video.clicked.connect(self._on_open_video_clicked)
        act_row.addWidget(self.btn_video)

        self.btn_scan_tv = QPushButton("🔍 SCAN TV BAND")
        self.btn_scan_tv.clicked.connect(self._on_scan_tv_clicked)
        act_row.addWidget(self.btn_scan_tv)

        self.btn_gqrx = QPushButton("📻 8MHz GQRX")
        self.btn_gqrx.setStyleSheet(
            "QPushButton { background-color: #0c4a6e; color: #e0f2fe; border: 1px solid #38bdf8; }"
            "QPushButton:hover { background-color: #0284c7; }"
        )
        self.btn_gqrx.clicked.connect(self._on_open_gqrx_clicked)
        act_row.addWidget(self.btn_gqrx)

        self.btn_stop = QPushButton("■ STOP && CLOSE")
        self.btn_stop.clicked.connect(self.close)
        act_row.addWidget(self.btn_stop)

        root.addLayout(act_row)

    def _select_frequency(self, freq_hz: float) -> None:
        tv_info = identify_atsc_tv_channel(freq_hz)
        target_center = int(tv_info[1]) if tv_info is not None else 539_000_000
        for idx in range(self.cmb_channel.count()):
            if int(self.cmb_channel.itemData(idx)) == target_center:
                self.cmb_channel.blockSignals(True)
                self.cmb_channel.setCurrentIndex(idx)
                self.cmb_channel.blockSignals(False)
                break

    def _quick_tune_preset(self, center_hz: int) -> None:
        self._select_frequency(float(center_hz))
        self._on_start_clicked()

    def _on_channel_combo_changed(self, _idx: int) -> None:
        if self._ctrl.is_running():
            self._on_start_clicked()

    def _on_mode_combo_changed(self, _idx: int) -> None:
        if self._ctrl.is_running():
            self._on_start_clicked()

    def _on_start_clicked(self) -> None:
        center_hz = int(self.cmb_channel.currentData() or 539_000_000)
        mode = str(self.cmb_mode.currentData() or "ATSC_8VSB")
        self._auto_opened_mpv = False
        self.lbl_status.setText(f"Starting {mode} receiver at {center_hz / 1e6:.1f} MHz...")
        ok, msg = self._ctrl.start(
            center_freq_hz=center_hz,
            mode=mode,
            lna_gain=self.spn_lna.value(),
            vga_gain=self.spn_vga.value(),
            amp_enable=1 if self.chk_amp.isChecked() else 0,
            auto_open_player=False,
        )
        self.lbl_status.setText(msg)
        if not ok:
            self.lbl_lock_badge.setText("● SDR BUSY")
            self.lbl_lock_badge.setStyleSheet("color: #ef4444; font-weight: bold;")

    def _on_open_video_clicked(self) -> None:
        if not self._ctrl.is_running():
            self._on_start_clicked()
        ok, msg = self._ctrl.launch_video_player()
        self.lbl_status.setText(msg)

    def _on_scan_tv_clicked(self) -> None:
        self.btn_scan_tv.setEnabled(False)
        self.btn_scan_tv.setText("⏳ SCANNING...")
        self._ctrl.stop()
        self.lbl_status.setText("Sweeping VHF-Hi (174-216 MHz) & UHF (470-608 MHz) for 8VSB TV pilots...")
        lna = self.spn_lna.value()
        vga = self.spn_vga.value()
        amp = 1 if self.chk_amp.isChecked() else 0

        def _bg() -> None:
            res = scan_active_tv_channels(lna_gain=lna, vga_gain=vga, amp_enable=amp)
            self.scan_completed_sig.emit(res)

        threading.Thread(target=_bg, daemon=True).start()

    def _on_tv_scan_finished(self, res: Dict[str, Any]) -> None:
        self.btn_scan_tv.setEnabled(True)
        self.btn_scan_tv.setText("🔍 SCAN TV BAND")
        if not res.get("success"):
            self.lbl_status.setText(f"TV scan error: {res.get('error', 'unknown')}")
            return
        chans: List[Dict[str, Any]] = res.get("channels", [])
        if not chans:
            self.lbl_status.setText("TV scan finished: no channels returned.")
            return
        best = chans[0]
        top_str = ", ".join(
            f"Ch{c['channel']}(+{c['pilot_snr_db']:.1f}dB)" for c in chans[:4]
        )
        self.lbl_status.setText(f"Strongest TV: {top_str} — Tuning Ch {best['channel']} ({best['call']})!")
        self._quick_tune_preset(int(best["center_hz"]))

    def _on_open_gqrx_clicked(self) -> None:
        center_hz = int(self.cmb_channel.currentData() or 539_000_000)
        tv_info = identify_atsc_tv_channel(center_hz)
        ch = tv_info[0] if tv_info else 25
        self._handed_off_to_gqrx = True
        self._ctrl.stop()
        self.open_gqrx_wideband_sig.emit(center_hz, ch)
        self.accept()

    def _poll_telemetry(self) -> None:
        telem = self._ctrl.read_telemetry()
        raster = self._ctrl.read_analog_raster() if telem.mode == "ANALOG_NTSC" else None
        self.canvas.update_state(telem, raster)

        if not telem.running:
            return

        self.bar_snr.setValue(int(round(max(0.0, min(30.0, telem.pilot_snr_db)))))
        self.lbl_pilot_stats.setText(
            f"Pilot: {telem.pilot_dbfs:.1f}dBFS (+{telem.pilot_snr_db:.1f}dB) | Shelf: {telem.shelf_dbfs:.1f}dBFS"
        )
        self.lbl_ts_stats.setText(
            f"MPEG-TS: {telem.ts_bitrate_kbps:.0f} kbps ({telem.ts_packets_total} pkts) | MER~{telem.mer_est_db:.1f}dB"
        )
        if telem.status_text:
            self.lbl_status.setText(telem.status_text)

        if telem.ts_sync_locked:
            self.lbl_lock_badge.setText("● ATSC VIDEO LOCK")
            self.lbl_lock_badge.setStyleSheet("color: #10b981; font-weight: bold;")
            if not self._auto_opened_mpv and not self._ctrl.is_player_running():
                self._auto_opened_mpv = True
                self._ctrl.launch_video_player()
        elif telem.pilot_locked:
            self.lbl_lock_badge.setText("● 8VSB PILOT LOCK")
            self.lbl_lock_badge.setStyleSheet("color: #f59e0b; font-weight: bold;")
        elif telem.mode == "ANALOG_NTSC":
            self.lbl_lock_badge.setText("● NTSC RASTER LIVE")
            self.lbl_lock_badge.setStyleSheet("color: #38bdf8; font-weight: bold;")
        else:
            self.lbl_lock_badge.setText("● RF MONITORING")
            self.lbl_lock_badge.setStyleSheet("color: #fb7185; font-weight: bold;")

    def closeEvent(self, event: Any) -> None:
        self._poll_timer.stop()
        if not self._handed_off_to_gqrx:
            self._ctrl.stop()
        super().closeEvent(event)
