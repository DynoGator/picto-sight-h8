"""
Beautified, Adaptive Responsive Main UI Window for AI-Powered Smart RF Scanner
Supports:
  - 5-inch DSI Multitouch Screen (800x480) Compact 4-View Touch Deck Mode (zero clipping/overflow)
  - 2nd HDMI Monitor (1080p/1440p+) Multi-Pane Split SIGINT Studio Mode (automatic + manual toggle)
  - Simultaneous Graphical & Alphanumerical status indicators on all timed operations
  - Highlighted Quick Scan, Deep Scan, and Custom Scan modes with Demod & Signal Strength adjusters
  - Strict 1-at-a-time HackRF One locking & verified Gqrx 2.17.6 live translation & dBFS readback
  - Permanently saved, reboot-persistent timestamped Location Profiles
"""

from __future__ import annotations

import json
import math
import os
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

import numpy as np
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject, QSize
from PyQt6.QtGui import QColor, QFont, QGuiApplication, QIcon, QKeyEvent
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QScroller,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.bandplans import (
    GQRX_DEMOD_SPECS,
    SUPPORTED_DEMOD_MODES,
    evaluate_signal_demod_feasibility,
)
from core.demod_engine import launch_in_gui_terminal
from core.gps_engine import get_gps_manager
from core.gqrx_controller import GqrxController
from core.hackrf_engine import ScanConfig
from core.profile_organizer import (
    ClassifiedSignalProfile,
    SavedLocationProfile,
    SignalProfileOrganizer,
)
from core.scanner_pipeline import MultiLayerScannerPipeline, ScanProgressUpdate
from core.sdr_arbiter import get_sdr_arbiter
from ui.styles import build_theme_qss
from ui.widgets import (
    DualLayerProgressBar,
    LiveSpectrumCanvas,
    PictogramFramesWidget,
    PipelineStageStepper,
    TelemetryMeterPill,
)


GUI_IPC_FILE = "/tmp/smart_rf_scanner_ipc.json"


class UIBridgeSignals(QObject):
    scan_progress = pyqtSignal(object)
    scan_finished = pyqtSignal(list, object)
    scan_error = pyqtSignal(str)
    gqrx_progress = pyqtSignal(str, float)
    gqrx_finished = pyqtSignal(dict)
    usb_reset_progress = pyqtSignal(str, float)
    usb_reset_finished = pyqtSignal(bool, str)
    hw_telemetry_ready = pyqtSignal(dict)


class SmartScannerMainWindow(QMainWindow):
    """
    Adaptive Dual-Topology SIGINT & AI Pictogram Scanner Main Window.
    """

    BAND_PRESETS: List[Tuple[str, int, int]] = [
        ("FM Broadcast + Airband (88 - 137 MHz)", 88, 137),
        ("Commercial FM Broadcast (88 - 108 MHz)", 88, 108),
        ("Civil Aviation Airband (118 - 137 MHz)", 118, 137),
        ("VHF High / NOAA / Marine / 2m (136 - 174 MHz)", 136, 174),
        ("UHF FRS / GMRS / 70cm / LMR (420 - 480 MHz)", 420, 480),
        ("ISM 915 MHz Telemetry (902 - 928 MHz)", 902, 928),
        ("Full VHF/UHF Survey (88 - 480 MHz)", 88, 480),
        ("Custom Frequency Range", 88, 137),
    ]

    SQUELCH_OPTIONS: List[Tuple[str, str]] = [
        ("OPEN (-150 dBFS • Instant Audio)", "OPEN"),
        ("AUTO (+5 dB Above Noise Floor)", "AUTO"),
        ("Tight (-45 dBFS)", "CUSTOM:-45"),
        ("Medium (-58 dBFS)", "CUSTOM:-58"),
        ("Sensitive (-70 dBFS)", "CUSTOM:-70"),
    ]

    def __init__(self, auto_scan_mode: Optional[str] = None) -> None:
        super().__init__()
        self._startup_auto_scan = auto_scan_mode.upper().strip() if auto_scan_mode else None

        self.setWindowTitle("Picto-Sight H8 • AI Smart RF Scanner • Hailo-8 + HackRF + Gqrx")
        icon_svg = Path(__file__).resolve().parent.parent / "icons" / "smart-scanner.svg"
        if icon_svg.exists():
            self.setWindowIcon(QIcon(str(icon_svg)))

        self.bridge = UIBridgeSignals()
        self.bridge.scan_progress.connect(self._on_scan_progress)
        self.bridge.scan_finished.connect(self._on_scan_finished)
        self.bridge.scan_error.connect(self._on_scan_error)
        self.bridge.gqrx_progress.connect(self._on_gqrx_progress)
        self.bridge.gqrx_finished.connect(self._on_gqrx_finished)
        self.bridge.usb_reset_progress.connect(self._on_usb_reset_progress)
        self.bridge.usb_reset_finished.connect(self._on_usb_reset_finished)
        self.bridge.hw_telemetry_ready.connect(self._on_hw_telemetry_ready)

        self.arbiter = get_sdr_arbiter()
        self.gps_mgr = get_gps_manager()
        self.organizer = SignalProfileOrganizer()
        self.pipeline = MultiLayerScannerPipeline(callback=self.bridge.scan_progress.emit)
        self.gqrx_ctrl = GqrxController()

        self.all_profiles: List[ClassifiedSignalProfile] = []
        self.filtered_profiles: List[ClassifiedSignalProfile] = []
        self.active_saved_profile: Optional[SavedLocationProfile] = None
        self._scan_unlocked: bool = False
        self._tuning_busy: bool = False
        self._usb_reset_busy: bool = False
        self._hw_poll_inflight: bool = False
        self._anim_phase: float = 0.0

        # Voice broadcast & quick search filter state ("ALL", "VOICE_BROADCAST", "ALL_VOICE")
        self._signal_filter_mode: str = "ALL"
        self._search_filter_text: str = ""

        # Layout topology state: "AUTO", "DECK" (800x480 5in touch), "STUDIO" (1080p+ HDMI)
        self._layout_preference: str = "AUTO"
        self._is_compact_mode: bool = True

        self._detect_initial_screen_geometry()
        self._build_ui()
        self._apply_topology(force=True)

        # Load saved Location Profiles from disk immediately (reboot persistence!)
        self._refresh_saved_profiles_table()
        self._load_initial_saved_profile()

        # Smooth 12 FPS animation timer for graphical pulses & progress indicators
        self._anim_timer = QTimer(self)
        self._anim_timer.timeout.connect(self._on_anim_tick)
        self._anim_timer.start(85)

        # Hardware & Live Gqrx TCP telemetry timer (every 2.0s, off-main-thread)
        self._hw_timer = QTimer(self)
        self._hw_timer.timeout.connect(self._poll_hardware_and_gqrx)
        self._hw_timer.start(2000)
        self._poll_hardware_and_gqrx()

        # Desktop launcher IPC listener (raises window / starts Quick, Deep, Custom scan without duplicate GUIs)
        self._ipc_timer = QTimer(self)
        self._ipc_timer.timeout.connect(self._poll_desktop_ipc)
        self._ipc_timer.start(450)

        # Handle desktop launcher auto-scan trigger (--auto-scan quick / deep / custom)
        if self._startup_auto_scan in ("QUICK", "DEEP", "CUSTOM"):
            self._select_scan_mode(self._startup_auto_scan)
            if self._startup_auto_scan in ("QUICK", "DEEP"):
                QTimer.singleShot(450, self.start_scan)
            else:
                # Custom launcher opens directly to highlighted Custom Scan configuration
                self._switch_deck_tab(0)

    # ------------------------------------------------------------------
    # Screen Detection & Responsive Geometry
    # ------------------------------------------------------------------
    def _detect_initial_screen_geometry(self) -> None:
        screen = QGuiApplication.primaryScreen()
        avail = screen.availableGeometry() if screen else None
        # Fit cleanly inside 800x480 DSI-1 (even with wf-panel-pi top bar ~440px tall)
        self.setMinimumSize(720, 390)
        if avail is not None:
            if avail.width() <= 1024 or avail.height() <= 600:
                self._is_compact_mode = True
                self.setGeometry(avail)
            else:
                self._is_compact_mode = False
                w = min(1480, int(avail.width() * 0.92))
                h = min(920, int(avail.height() * 0.90))
                self.resize(w, h)
        else:
            self.resize(800, 444)

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        if getattr(self, "_initial_show_done", False):
            return
        self._initial_show_done = True
        wh = self.windowHandle()
        if wh is not None:
            try:
                wh.screenChanged.connect(self._on_screen_changed)
            except Exception:
                pass
        screen = self.screen() or QGuiApplication.primaryScreen()
        if (
            self._layout_preference != "STUDIO"
            and screen
            and (screen.availableGeometry().width() <= 900 or screen.availableGeometry().height() <= 540)
        ):
            # Maximize cleanly on the 5-inch 800x480 DSI display
            self.showMaximized()

    def _on_screen_changed(self, new_screen: Any) -> None:
        if new_screen is not None and self._layout_preference == "AUTO":
            self._apply_topology(force=False)

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        if self._layout_preference == "AUTO":
            want_compact = (self.width() < 1080) or (self.height() < 640)
            if want_compact != self._is_compact_mode:
                self._apply_topology(force=False)

    # ------------------------------------------------------------------
    # UI Construction (4 Reusable Panels + Persistent Header & Progress Strip)
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)
        self.root_vbox = QVBoxLayout(central)
        self.root_vbox.setContentsMargins(6, 5, 6, 5)
        self.root_vbox.setSpacing(4)

        # 1. TOP TELEMETRY & HARDWARE STATUS BAR
        self.header_frame = QFrame(self)
        self.header_frame.setObjectName("headerBar")
        hdr_hbox = QHBoxLayout(self.header_frame)
        hdr_hbox.setContentsMargins(7, 3, 7, 3)
        hdr_hbox.setSpacing(5)

        self.lbl_brand = QLabel("⚡ SMART RF AI", self)
        self.lbl_brand.setObjectName("brandTitle")
        hdr_hbox.addWidget(self.lbl_brand)

        self.pill_sdr = TelemetryMeterPill("SDR", "SDR: READY", "#10b981", self)
        self.pill_hailo = TelemetryMeterPill("AI", "HAILO-8: 26T", "#00f0ff", self)
        self.pill_gps = TelemetryMeterPill("GPS", "DM79mr • Denver", "#38bdf8", self)
        self.pill_gqrx = TelemetryMeterPill("GQRX", "GQRX: STANDBY", "#94a3b8", self)

        hdr_hbox.addWidget(self.pill_sdr, 3)
        hdr_hbox.addWidget(self.pill_hailo, 2)
        hdr_hbox.addWidget(self.pill_gps, 3)
        hdr_hbox.addWidget(self.pill_gqrx, 3)

        self.btn_layout_mode = QPushButton("[ ] Auto", self)
        self.btn_layout_mode.setToolTip("Switch Responsive Layout: Auto / 5\" DSI Touch Deck / 2nd HDMI Studio")
        self.btn_layout_mode.clicked.connect(self._cycle_layout_preference)
        hdr_hbox.addWidget(self.btn_layout_mode)

        self.root_vbox.addWidget(self.header_frame)

        # 2. PERSISTENT GRAPHICAL + ALPHANUMERICAL PIPELINE PROGRESS STRIP
        self.prog_frame = QFrame(self)
        self.prog_frame.setObjectName("progressStrip")
        prog_grid = QHBoxLayout(self.prog_frame)
        prog_grid.setContentsMargins(6, 3, 6, 3)
        prog_grid.setSpacing(6)

        bars_vbox = QVBoxLayout()
        bars_vbox.setContentsMargins(0, 0, 0, 0)
        bars_vbox.setSpacing(2)
        self.stage_stepper = PipelineStageStepper(self)
        self.dual_progress = DualLayerProgressBar(self)
        bars_vbox.addWidget(self.stage_stepper)
        bars_vbox.addWidget(self.dual_progress)
        prog_grid.addLayout(bars_vbox, 1)

        act_vbox = QVBoxLayout()
        act_vbox.setContentsMargins(0, 0, 0, 0)
        act_vbox.setSpacing(2)
        self.btn_start_scan = QPushButton("▶ START QUICK SCAN", self)
        self.btn_start_scan.setObjectName("btnStartScan")
        self.btn_start_scan.setMinimumHeight(24)
        self.btn_start_scan.clicked.connect(self.start_scan)

        self.btn_abort_scan = QPushButton("■ STOP", self)
        self.btn_abort_scan.setObjectName("btnAbort")
        self.btn_abort_scan.setMinimumHeight(20)
        self.btn_abort_scan.setEnabled(False)
        self.btn_abort_scan.clicked.connect(self.abort_scan)

        self.btn_reset_sdr = QPushButton("⟳ Reset SDR", self)
        self.btn_reset_sdr.setObjectName("btnResetSdr")
        self.btn_reset_sdr.setMinimumHeight(20)
        self.btn_reset_sdr.setToolTip(
            "Hardware USB Port Self-Heal: resets HackRF One USB transceiver & clears Gqrx crash locks without unplugging"
        )
        self.btn_reset_sdr.clicked.connect(self._on_reset_sdr_clicked)

        sub_act_hbox = QHBoxLayout()
        sub_act_hbox.setContentsMargins(0, 0, 0, 0)
        sub_act_hbox.setSpacing(4)
        sub_act_hbox.addWidget(self.btn_abort_scan, 1)
        sub_act_hbox.addWidget(self.btn_reset_sdr, 1)

        act_vbox.addWidget(self.btn_start_scan)
        act_vbox.addLayout(sub_act_hbox)
        prog_grid.addLayout(act_vbox)

        self.root_vbox.addWidget(self.prog_frame)

        # 3. BUILD THE 4 CORE FUNCTIONAL PANELS
        self.panel_controls = self._build_controls_panel()
        self.panel_visuals = self._build_visuals_panel()
        self.panel_signals = self._build_signals_panel()
        self.panel_profiles = self._build_profiles_panel()

        # 4. DECK NAVIGATION BAR (Visible in 5" 800x480 Touch Deck Mode)
        self.deck_nav_widget = QWidget(self)
        nav_hbox = QHBoxLayout(self.deck_nav_widget)
        nav_hbox.setContentsMargins(0, 0, 0, 0)
        nav_hbox.setSpacing(4)

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.btn_tab_ctrl = QPushButton("⚙ 1. SCAN & OPTIONS", self)
        self.btn_tab_vis = QPushButton("∿ 2. LIVE RF & AI", self)
        self.btn_tab_sigs = QPushButton("◉ 3. SIGNALS (0)", self)
        self.btn_tab_prof = QPushButton("★ 4. PROFILES (0)", self)

        for idx, btn in enumerate((self.btn_tab_ctrl, self.btn_tab_vis, self.btn_tab_sigs, self.btn_tab_prof)):
            btn.setObjectName("navTabBtn")
            btn.setCheckable(True)
            btn.setMinimumHeight(28)
            self.nav_group.addButton(btn, idx)
            nav_hbox.addWidget(btn, 1)

        self.btn_tab_ctrl.setChecked(True)
        self.nav_group.idClicked.connect(self._switch_deck_tab)
        self.root_vbox.addWidget(self.deck_nav_widget)

        # 5. DYNAMIC BODY CONTAINER (Holds either QStackedWidget for 5" Deck or QSplitter for HDMI Studio)
        self.body_host = QWidget(self)
        self.body_host_layout = QVBoxLayout(self.body_host)
        self.body_host_layout.setContentsMargins(0, 0, 0, 0)
        self.body_host_layout.setSpacing(0)
        self.root_vbox.addWidget(self.body_host, 1)

        self.deck_stack: Optional[QStackedWidget] = None
        self.studio_splitter: Optional[QSplitter] = None

    def _build_controls_panel(self) -> QWidget:
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        QScroller.grabGesture(scroll.viewport(), QScroller.ScrollerGestureType.LeftMouseButtonGesture)

        container = QFrame(scroll)
        container.setObjectName("cardPanel")
        vbox = QVBoxLayout(container)
        vbox.setContentsMargins(7, 4, 7, 4)
        vbox.setSpacing(4)

        # --- Section A: Highlighted Scan Mode Selector (QUICK / DEEP / CUSTOM) ---
        mode_hdr = QHBoxLayout()
        lbl_m = QLabel("SCAN MODE (SAVES PERMANENT LOCATION PROFILE):", container)
        lbl_m.setObjectName("sectionHeader")
        mode_hdr.addWidget(lbl_m)
        mode_hdr.addStretch()
        self.lbl_active_mode_pill = QLabel("MODE: QUICK SCAN", container)
        self.lbl_active_mode_pill.setObjectName("valuePillGreen")
        mode_hdr.addWidget(self.lbl_active_mode_pill)
        vbox.addLayout(mode_hdr)

        modes_hbox = QHBoxLayout()
        modes_hbox.setSpacing(6)
        self.mode_btn_group = QButtonGroup(self)
        self.mode_btn_group.setExclusive(True)

        self.btn_mode_quick = QPushButton("⚡ QUICK SCAN\nFast 3-Frame 1MHz AI", container)
        self.btn_mode_deep = QPushButton("◈ DEEP SCAN\n5x Avg + 5-Frame 1MHz AI", container)
        self.btn_mode_custom = QPushButton("⚙ CUSTOM SCAN\nFull Manual RF + Gains", container)

        for idx, (m_name, btn) in enumerate(
            (("QUICK", self.btn_mode_quick), ("DEEP", self.btn_mode_deep), ("CUSTOM", self.btn_mode_custom))
        ):
            btn.setObjectName("modeCardBtn")
            btn.setCheckable(True)
            btn.setMinimumHeight(34)
            self.mode_btn_group.addButton(btn, idx)
            modes_hbox.addWidget(btn, 1)
            btn.clicked.connect(lambda _, m=m_name: self._select_scan_mode(m))

        self.btn_mode_quick.setChecked(True)
        vbox.addLayout(modes_hbox)

        # --- Section B: Frequency Band Preset & Range ---
        band_frame = QFrame(container)
        band_frame.setObjectName("highlightBox")
        b_grid = QGridLayout(band_frame)
        b_grid.setContentsMargins(7, 3, 7, 3)
        b_grid.setHorizontalSpacing(6)
        b_grid.setVerticalSpacing(3)

        b_grid.addWidget(QLabel("RF Band:", band_frame), 0, 0)
        self.combo_preset = QComboBox(band_frame)
        for label, _, _ in self.BAND_PRESETS:
            self.combo_preset.addItem(label)
        self.combo_preset.currentIndexChanged.connect(self._on_preset_changed)
        b_grid.addWidget(self.combo_preset, 0, 1, 1, 3)

        b_grid.addWidget(QLabel("Start MHz:", band_frame), 1, 0)
        self.spin_min_mhz = QSpinBox(band_frame)
        self.spin_min_mhz.setRange(1, 5990)
        self.spin_min_mhz.setValue(88)
        self.spin_min_mhz.setSuffix(" MHz")
        b_grid.addWidget(self.spin_min_mhz, 1, 1)

        b_grid.addWidget(QLabel("Stop MHz:", band_frame), 1, 2)
        self.spin_max_mhz = QSpinBox(band_frame)
        self.spin_max_mhz.setRange(2, 6000)
        self.spin_max_mhz.setValue(137)
        self.spin_max_mhz.setSuffix(" MHz")
        b_grid.addWidget(self.spin_max_mhz, 1, 3)

        vbox.addWidget(band_frame)

        # --- Section C: Highlighted Demodulation & Signal Strength Adjusters ---
        demod_frame = QFrame(container)
        demod_frame.setObjectName("highlightBox")
        d_grid = QGridLayout(demod_frame)
        d_grid.setContentsMargins(7, 3, 7, 3)
        d_grid.setHorizontalSpacing(6)
        d_grid.setVerticalSpacing(3)

        # Demod Mode & Squelch Row
        d_grid.addWidget(QLabel("Demod:", demod_frame), 0, 0)
        self.combo_demod = QComboBox(demod_frame)
        self.combo_demod.addItems(SUPPORTED_DEMOD_MODES)
        self.combo_demod.currentTextChanged.connect(self._on_demod_or_filter_changed)
        d_grid.addWidget(self.combo_demod, 0, 1)

        d_grid.addWidget(QLabel("Squelch:", demod_frame), 0, 2)
        self.combo_squelch = QComboBox(demod_frame)
        for sq_lbl, sq_val in self.SQUELCH_OPTIONS:
            self.combo_squelch.addItem(sq_lbl, sq_val)
        self.combo_squelch.currentIndexChanged.connect(self._on_demod_or_filter_changed)
        d_grid.addWidget(self.combo_squelch, 0, 3)

        # Signal Strength Adjuster 1: Min SNR (dB)
        d_grid.addWidget(QLabel("Min SNR:", demod_frame), 1, 0)
        self.slider_snr = QSlider(Qt.Orientation.Horizontal, demod_frame)
        self.slider_snr.setRange(3, 35)
        self.slider_snr.setValue(9)
        self.slider_snr.valueChanged.connect(self._on_strength_sliders_changed)
        d_grid.addWidget(self.slider_snr, 1, 1, 1, 2)

        self.lbl_snr_pill = QLabel("SNR >= +9 dB", demod_frame)
        self.lbl_snr_pill.setObjectName("valuePillCyan")
        d_grid.addWidget(self.lbl_snr_pill, 1, 3)

        # Signal Strength Adjuster 2: Min Peak Signal Strength (dBFS)
        d_grid.addWidget(QLabel("Min Peak:", demod_frame), 2, 0)
        self.slider_peak = QSlider(Qt.Orientation.Horizontal, demod_frame)
        self.slider_peak.setRange(-95, -20)
        self.slider_peak.setValue(-72)
        self.slider_peak.valueChanged.connect(self._on_strength_sliders_changed)
        d_grid.addWidget(self.slider_peak, 2, 1, 1, 2)

        self.lbl_peak_pill = QLabel("PEAK >= -72 dBFS", demod_frame)
        self.lbl_peak_pill.setObjectName("valuePillMagenta")
        d_grid.addWidget(self.lbl_peak_pill, 2, 3)

        # Highlighted Summary Strip of Active Demod + Viable Filter Count
        summary_hbox = QHBoxLayout()
        self.lbl_demod_badge = QLabel("DEMOD: AUTO (Band + AI)", demod_frame)
        self.lbl_demod_badge.setObjectName("valuePillAmber")
        self.lbl_viable_count_badge = QLabel("VIABLE LISTED: 0", demod_frame)
        self.lbl_viable_count_badge.setObjectName("valuePillGreen")
        summary_hbox.addWidget(self.lbl_demod_badge)
        summary_hbox.addStretch()
        summary_hbox.addWidget(self.lbl_viable_count_badge)
        d_grid.addLayout(summary_hbox, 3, 0, 1, 4)

        vbox.addWidget(demod_frame)

        # --- Section D: Custom Scan Hardware & Layer 3 Frame Controls ---
        self.custom_frame = QFrame(container)
        self.custom_frame.setObjectName("cardPanel")
        c_hbox = QHBoxLayout(self.custom_frame)
        c_hbox.setContentsMargins(7, 4, 7, 4)
        c_hbox.setSpacing(6)

        c_hbox.addWidget(QLabel("1MHz Frames:", self.custom_frame))
        self.spin_l3_frames = QSpinBox(self.custom_frame)
        self.spin_l3_frames.setRange(3, 10)
        self.spin_l3_frames.setValue(3)
        self.spin_l3_frames.setToolTip("Minimum 3 spectrogram frames per 1 MHz in Layer 3")
        c_hbox.addWidget(self.spin_l3_frames)

        c_hbox.addWidget(QLabel("LNA:", self.custom_frame))
        self.spin_lna = QSpinBox(self.custom_frame)
        self.spin_lna.setRange(0, 40)
        self.spin_lna.setSingleStep(8)
        self.spin_lna.setValue(24)
        self.spin_lna.setSuffix("dB")
        c_hbox.addWidget(self.spin_lna)

        c_hbox.addWidget(QLabel("VGA:", self.custom_frame))
        self.spin_vga = QSpinBox(self.custom_frame)
        self.spin_vga.setRange(0, 62)
        self.spin_vga.setSingleStep(2)
        self.spin_vga.setValue(22)
        self.spin_vga.setSuffix("dB")
        c_hbox.addWidget(self.spin_vga)

        self.chk_amp = QCheckBox("+14dB AMP", self.custom_frame)
        c_hbox.addWidget(self.chk_amp)

        self.chk_legal_only = QCheckBox("Legal Demod Only", self.custom_frame)
        self.chk_legal_only.setChecked(True)
        c_hbox.addWidget(self.chk_legal_only)

        vbox.addWidget(self.custom_frame)
        vbox.addStretch(1)

        scroll.setWidget(container)
        self._current_scan_mode = "QUICK"
        return scroll

    def _build_visuals_panel(self) -> QWidget:
        panel = QFrame(self)
        panel.setObjectName("cardPanel")
        vbox = QVBoxLayout(panel)
        vbox.setContentsMargins(6, 5, 6, 5)
        vbox.setSpacing(5)

        self.spectrum_canvas = LiveSpectrumCanvas(panel)
        self.picto_widget = PictogramFramesWidget(panel)

        vbox.addWidget(self.spectrum_canvas, 1)
        vbox.addWidget(self.picto_widget, 1)
        return panel

    def _build_signals_panel(self) -> QWidget:
        panel = QFrame(self)
        panel.setObjectName("cardPanel")
        vbox = QVBoxLayout(panel)
        vbox.setContentsMargins(6, 5, 6, 5)
        vbox.setSpacing(4)

        # Lockout / Unlock + Live Gqrx Receiver Strip
        top_strip = QHBoxLayout()
        top_strip.setSpacing(5)
        self.lbl_lock_banner = QLabel(
            "[LOCKED] AWAITING SCAN — Signals unlock once all layers & profile refinement complete.",
            panel,
        )
        self.lbl_lock_banner.setStyleSheet(
            "background-color: #2b1d0c; border: 1px solid #d97706; border-radius: 4px; "
            "color: #fbbf24; padding: 4px 8px; font-weight: bold;"
        )
        self.lbl_lock_banner.setWordWrap(False)
        top_strip.addWidget(self.lbl_lock_banner, 1)

        self.btn_release_gqrx = QPushButton("■ Release Gqrx", panel)
        self.btn_release_gqrx.setObjectName("btnReleaseGqrx")
        self.btn_release_gqrx.setToolTip("Cleanly stops Gqrx DSP & releases HackRF One USB lock")
        self.btn_release_gqrx.clicked.connect(self._release_gqrx_manually)
        self.btn_release_gqrx.setVisible(False)
        top_strip.addWidget(self.btn_release_gqrx)

        vbox.addLayout(top_strip)

        # Voice Broadcast & Signal Type Quick Filter Bar
        filter_strip = QHBoxLayout()
        filter_strip.setSpacing(4)
        filter_strip.setContentsMargins(0, 0, 0, 0)

        lbl_filter = QLabel("FIND:", panel)
        lbl_filter.setStyleSheet("color: #64748b; font-weight: 800; font-size: 10px; letter-spacing: 0.5px;")
        filter_strip.addWidget(lbl_filter)

        self.filter_btn_group = QButtonGroup(panel)
        self.filter_btn_group.setExclusive(True)

        self.btn_filter_all = QPushButton("🌐 All Signals (0)", panel)
        self.btn_filter_all.setObjectName("btnFilterPill")
        self.btn_filter_all.setCheckable(True)
        self.btn_filter_all.setChecked(True)
        self.btn_filter_all.setToolTip("Show all viable classified frequencies in numerical order")
        self.btn_filter_all.clicked.connect(lambda: self._set_signal_filter_mode("ALL"))
        self.filter_btn_group.addButton(self.btn_filter_all)
        filter_strip.addWidget(self.btn_filter_all)

        self.btn_filter_voice_bc = QPushButton("📻 Voice Broadcast (0)", panel)
        self.btn_filter_voice_bc.setObjectName("btnFilterVoiceBc")
        self.btn_filter_voice_bc.setCheckable(True)
        self.btn_filter_voice_bc.setToolTip(
            "1-Click Filter: Immediately shows ONLY Voice Broadcast frequencies (FM, AM, SW & NOAA Weather) • Hotkey: 'V'"
        )
        self.btn_filter_voice_bc.clicked.connect(lambda: self._set_signal_filter_mode("VOICE_BROADCAST"))
        self.filter_btn_group.addButton(self.btn_filter_voice_bc)
        filter_strip.addWidget(self.btn_filter_voice_bc)

        self.btn_filter_any_voice = QPushButton("🗣️ All Voice (0)", panel)
        self.btn_filter_any_voice.setObjectName("btnFilterPill")
        self.btn_filter_any_voice.setCheckable(True)
        self.btn_filter_any_voice.setToolTip("Show all voice channels (Broadcast + Civil/Mil Airband ATC + Marine + Two-Way)")
        self.btn_filter_any_voice.clicked.connect(lambda: self._set_signal_filter_mode("ALL_VOICE"))
        self.filter_btn_group.addButton(self.btn_filter_any_voice)
        filter_strip.addWidget(self.btn_filter_any_voice)

        filter_strip.addSpacing(6)

        self.edit_filter_search = QLineEdit(panel)
        self.edit_filter_search.setObjectName("searchFilterEdit")
        self.edit_filter_search.setPlaceholderText("🔍 Filter stations, MHz, labels...")
        self.edit_filter_search.setClearButtonEnabled(True)
        self.edit_filter_search.textChanged.connect(self._on_search_filter_changed)
        filter_strip.addWidget(self.edit_filter_search, 1)

        vbox.addLayout(filter_strip)

        # Numerically Ordered Clickable Signals Table
        self.table_signals = QTableWidget(panel)
        self.table_signals.setColumnCount(10)
        self.table_signals.setHorizontalHeaderLabels(
            [
                "Freq (MHz)",
                "Identity & AI Class",
                "Demod",
                "SNR / Peak",
                "OBW",
                "Category",
                "AI Confidence",
                "OBW • Gains",
                "WF Floor/Ceil",
                "Gqrx Action",
            ]
        )
        self.table_signals.setAlternatingRowColors(True)
        self.table_signals.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table_signals.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table_signals.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table_signals.verticalHeader().setVisible(False)
        self.table_signals.itemSelectionChanged.connect(self._on_signal_row_selected)
        self.table_signals.cellDoubleClicked.connect(self._on_signal_row_double_clicked)
        self.table_signals.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table_signals.customContextMenuRequested.connect(self._on_signals_context_menu)
        QScroller.grabGesture(
            self.table_signals.viewport(),
            QScroller.ScrollerGestureType.LeftMouseButtonGesture,
        )
        self.table_signals.setEnabled(False)
        vbox.addWidget(self.table_signals, 1)
        return panel

    def _build_profiles_panel(self) -> QWidget:
        panel = QFrame(self)
        panel.setObjectName("cardPanel")
        vbox = QVBoxLayout(panel)
        vbox.setContentsMargins(6, 5, 6, 5)
        vbox.setSpacing(4)

        hdr = QHBoxLayout()
        lbl_p = QLabel("★ PERMANENTLY SAVED LOCATION PROFILES (SURVIVES REBOOTS)", panel)
        lbl_p.setObjectName("sectionHeader")
        hdr.addWidget(lbl_p)
        hdr.addStretch()

        self.btn_refresh_profiles = QPushButton("↻ Refresh", panel)
        self.btn_refresh_profiles.clicked.connect(self._refresh_saved_profiles_table)
        hdr.addWidget(self.btn_refresh_profiles)

        self.btn_sync_bookmarks = QPushButton("▲ Sync Bookmarks -> Gqrx", panel)
        self.btn_sync_bookmarks.clicked.connect(self._sync_current_bookmarks_to_gqrx)
        hdr.addWidget(self.btn_sync_bookmarks)
        vbox.addLayout(hdr)

        self.table_profiles = QTableWidget(panel)
        self.table_profiles.setColumnCount(5)
        self.table_profiles.setHorizontalHeaderLabels(
            ["Approx Date & Time", "Locality & Grid", "Mode & Band", "Signals", "Actions"]
        )
        self.table_profiles.setAlternatingRowColors(True)
        self.table_profiles.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table_profiles.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table_profiles.verticalHeader().setVisible(False)
        ph = self.table_profiles.horizontalHeader()
        ph.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        ph.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        ph.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        ph.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        ph.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self.table_profiles.setColumnWidth(4, 134)
        QScroller.grabGesture(
            self.table_profiles.viewport(),
            QScroller.ScrollerGestureType.LeftMouseButtonGesture,
        )
        vbox.addWidget(self.table_profiles, 1)
        return panel

    # ------------------------------------------------------------------
    # Dynamic Topology Switching (5" 800x480 Deck <-> 1080p+ HDMI Studio)
    # ------------------------------------------------------------------
    def _cycle_layout_preference(self) -> None:
        order = ["AUTO", "DECK", "STUDIO"]
        idx = (order.index(self._layout_preference) + 1) % len(order)
        self._layout_preference = order[idx]
        self._apply_topology(force=True)

    def _apply_topology(self, force: bool = False) -> None:
        if self._layout_preference == "DECK":
            want_compact = True
        elif self._layout_preference == "STUDIO":
            want_compact = False
        else:
            want_compact = (self.width() < 1080) or (self.height() < 640)

        if not force and want_compact == self._is_compact_mode and (self.deck_stack or self.studio_splitter):
            return

        self._is_compact_mode = want_compact
        self.setStyleSheet(build_theme_qss(compact=want_compact))

        mode_tag = (
            f"Auto ({'5\" Deck' if want_compact else 'HDMI Studio'})"
            if self._layout_preference == "AUTO"
            else ("5\" Deck" if want_compact else "HDMI Studio")
        )
        self.btn_layout_mode.setText(f"[ ] {mode_tag}")

        # Detach the 4 reusable panels safely before rebuilding host container
        for pnl in (self.panel_controls, self.panel_visuals, self.panel_signals, self.panel_profiles):
            pnl.setParent(None)

        while self.body_host_layout.count():
            item = self.body_host_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        self.deck_stack = None
        self.studio_splitter = None

        if want_compact:
            # Topology A: 5-inch 800x480 DSI Multitouch 4-Tab Deck
            self.panel_controls.setMaximumWidth(16777215)
            self.panel_profiles.setMaximumWidth(16777215)
            self.deck_nav_widget.setVisible(True)
            stack = QStackedWidget(self.body_host)
            stack.addWidget(self.panel_controls)
            stack.addWidget(self.panel_visuals)
            stack.addWidget(self.panel_signals)
            stack.addWidget(self.panel_profiles)
            checked_id = self.nav_group.checkedId()
            stack.setCurrentIndex(max(0, checked_id))
            self.body_host_layout.addWidget(stack)
            self.deck_stack = stack
        else:
            # Topology B: 2nd HDMI Monitor Multi-Pane Split SIGINT Studio
            self.panel_controls.setMaximumWidth(665)
            self.panel_profiles.setMaximumWidth(665)
            self.deck_nav_widget.setVisible(False)
            h_split = QSplitter(Qt.Orientation.Horizontal, self.body_host)

            left_vsplit = QSplitter(Qt.Orientation.Vertical, h_split)
            left_vsplit.setMaximumWidth(668)
            left_vsplit.addWidget(self.panel_controls)
            left_vsplit.addWidget(self.panel_profiles)
            left_vsplit.setSizes([430, 270])

            right_vsplit = QSplitter(Qt.Orientation.Vertical, h_split)
            right_vsplit.addWidget(self.panel_visuals)
            right_vsplit.addWidget(self.panel_signals)
            right_vsplit.setSizes([330, 440])

            h_split.addWidget(left_vsplit)
            h_split.addWidget(right_vsplit)
            h_split.setStretchFactor(0, 0)
            h_split.setStretchFactor(1, 1)
            h_split.setSizes([660, 780])
            self.body_host_layout.addWidget(h_split)
            self.studio_splitter = h_split

        for pnl in (self.panel_controls, self.panel_visuals, self.panel_signals, self.panel_profiles):
            pnl.setVisible(True)

        self._configure_signals_table_columns(compact=want_compact)

    def _configure_signals_table_columns(self, compact: bool) -> None:
        hdr = self.table_signals.horizontalHeader()
        # Compact (800x480 DSI): 5 clean columns; Studio (1080p+ HDMI): 6 zero-scroll columns with [TUNE GQRX] visible
        hidden_cols = {4, 5, 6, 7, 8} if compact else {4, 5, 6, 8}
        for col in range(10):
            self.table_signals.setColumnHidden(col, col in hidden_cols)

        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for col in range(2, 9):
            hdr.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(9, QHeaderView.ResizeMode.Fixed)
        self.table_signals.setColumnWidth(9, 118)

        self.table_signals.verticalHeader().setDefaultSectionSize(34 if compact else 32)
        self.table_profiles.verticalHeader().setDefaultSectionSize(34 if compact else 32)

    def _switch_deck_tab(self, index: int) -> None:
        btn = self.nav_group.button(index)
        if btn is not None:
            btn.setChecked(True)
        if self.deck_stack is not None and 0 <= index < self.deck_stack.count():
            self.deck_stack.setCurrentIndex(index)

    # ------------------------------------------------------------------
    # Scan Mode, Demod & Signal Strength Adjuster Handlers
    # ------------------------------------------------------------------
    def _select_scan_mode(self, mode: str) -> None:
        m = mode.upper().strip()
        self._current_scan_mode = m
        self.btn_mode_quick.setChecked(m == "QUICK")
        self.btn_mode_deep.setChecked(m == "DEEP")
        self.btn_mode_custom.setChecked(m == "CUSTOM")

        if m == "QUICK":
            self.spin_l3_frames.setValue(3)
            self.lbl_active_mode_pill.setText("MODE: ⚡ QUICK SCAN (3-Frame 1MHz)")
            self.lbl_active_mode_pill.setObjectName("valuePillGreen")
        elif m == "DEEP":
            self.spin_l3_frames.setValue(5)
            self.lbl_active_mode_pill.setText("MODE: ◈ DEEP SCAN (5-Frame 1MHz)")
            self.lbl_active_mode_pill.setObjectName("valuePillCyan")
        else:
            self.lbl_active_mode_pill.setText("MODE: ⚙ CUSTOM SCAN (Manual RF)")
            self.lbl_active_mode_pill.setObjectName("valuePillAmber")

        self.lbl_active_mode_pill.style().unpolish(self.lbl_active_mode_pill)
        self.lbl_active_mode_pill.style().polish(self.lbl_active_mode_pill)

        # Highlight custom hardware row prominently when Custom mode is selected
        self.custom_frame.setStyleSheet(
            "QFrame#cardPanel { border: 2px solid #f59e0b; background-color: #161f30; }"
            if m == "CUSTOM"
            else ""
        )
        self.btn_start_scan.setText(f"▶ START {m} SCAN")

    def _on_preset_changed(self, idx: int) -> None:
        if 0 <= idx < len(self.BAND_PRESETS) - 1:
            _, lo, hi = self.BAND_PRESETS[idx]
            self.spin_min_mhz.setValue(lo)
            self.spin_max_mhz.setValue(hi)

    def _on_strength_sliders_changed(self) -> None:
        snr_v = float(self.slider_snr.value())
        peak_v = float(self.slider_peak.value())
        self.lbl_snr_pill.setText(f"SNR >= +{snr_v:.0f} dB")
        self.lbl_peak_pill.setText(f"PEAK >= {peak_v:.0f} dBFS")
        self.spectrum_canvas.set_thresholds(snr_v, peak_v)
        self._apply_viable_filter_and_populate()

    def _on_demod_or_filter_changed(self) -> None:
        demod_txt = self.combo_demod.currentText()
        sq_code = str(self.combo_squelch.currentData() or "OPEN")
        self.lbl_demod_badge.setText(f"DEMOD: {demod_txt} │ SQL: {sq_code}")

        # Dynamically update demodulation & squelch settings on loaded profiles so clicking
        # TUNE GQRX immediately uses the user's highlighted Demod & Squelch choice!
        region = self.gps_mgr.get_current_fix().region_code
        for p in self.all_profiles:
            feas = evaluate_signal_demod_feasibility(
                freq_hz=p.frequency_hz,
                snr_db=p.snr_db,
                measured_bw_hz=p.bandwidth_hz,
                peak_dbfs=p.peak_dbm,
                noise_floor_dbfs=p.noise_floor_dbm,
                iq_mod_hint=p.iq_mod_hint,
                demod_override=demod_txt,
                squelch_mode=sq_code,
                region=region,
            )
            p.modulation = feas["demod"]
            p.rigctl_mode = feas["rigctl_mode"]
            p.passband_hz = feas["passband_hz"]
            p.filter_low_cut = feas["filter_low_cut"]
            p.filter_high_cut = feas["filter_high_cut"]
            p.fm_maxdev = feas["fm_maxdev"]
            p.sql_level = feas["sql_level"]
            p.plot_min = float(feas["pandapter_min_db"])
            p.plot_max = float(feas["pandapter_max_db"])

        self._apply_viable_filter_and_populate()

    def _build_current_scan_config(self) -> ScanConfig:
        sq_code = str(self.combo_squelch.currentData() or "OPEN")
        return ScanConfig.from_mode(
            mode=self._current_scan_mode,
            freq_min_mhz=self.spin_min_mhz.value(),
            freq_max_mhz=self.spin_max_mhz.value(),
            min_snr_db=float(self.slider_snr.value()),
            min_peak_dbfs=float(self.slider_peak.value()),
            demod_mode=self.combo_demod.currentText(),
            squelch_mode=sq_code,
            l3_frames_per_mhz=self.spin_l3_frames.value(),
            lna_gain_db=self.spin_lna.value(),
            vga_gain_db=self.spin_vga.value(),
            amp_enable=1 if self.chk_amp.isChecked() else 0,
            only_legal_demod=self.chk_legal_only.isChecked(),
        )

    # ------------------------------------------------------------------
    # Multi-Layer Scan Execution & Progress Telemetry
    # ------------------------------------------------------------------
    def start_scan(self) -> None:
        if self.pipeline.is_scanning() or self._tuning_busy or self._usb_reset_busy:
            return

        cfg = self._build_current_scan_config()
        self._scan_unlocked = False
        self.btn_start_scan.setEnabled(False)
        self.btn_abort_scan.setEnabled(True)
        self.btn_reset_sdr.setEnabled(False)
        self.table_signals.setEnabled(False)
        self.btn_release_gqrx.setVisible(False)

        self.lbl_lock_banner.setText(
            f"[LOCKED] {cfg.mode} SCAN ACTIVE ({cfg.freq_min_mhz}-{cfg.freq_max_mhz} MHz) — "
            f"1-at-a-Time SDR Lock Engaged. List unlocks after Layer 3 & Profile Save."
        )
        self.lbl_lock_banner.setStyleSheet(
            "background-color: #2b1d0c; border: 1px solid #f59e0b; border-radius: 4px; "
            "color: #fbbf24; padding: 4px 8px; font-weight: bold;"
        )

        # In 5" Deck Mode, auto-switch to Live RF & AI Waterfall view so user watches live progress!
        if self._is_compact_mode:
            self._switch_deck_tab(1)

        def _scan_worker() -> None:
            try:
                profiles, saved_loc = self.pipeline.run_scan_with_config(cfg)
                self.bridge.scan_finished.emit(profiles, saved_loc)
            except Exception as exc:
                self.bridge.scan_error.emit(str(exc))

        threading.Thread(target=_scan_worker, daemon=True, name="MultiLayerScanWorker").start()

    def abort_scan(self) -> None:
        self.pipeline.stop_scan()
        self.btn_abort_scan.setEnabled(False)
        self.dual_progress.set_progress(0.0, 0.0, "Aborting scan & releasing HackRF One...", busy=True)

    def _on_scan_progress(self, u: ScanProgressUpdate) -> None:
        is_busy = u.percent_complete < 100.0
        self.stage_stepper.set_state(
            stage=max(1, u.stage),
            running=is_busy,
            elapsed_s=u.elapsed_s,
            eta_s=u.eta_s,
        )
        self.dual_progress.set_progress(
            overall_pct=u.percent_complete,
            sub_pct=u.sub_percent,
            text=f"[{u.stage_name}] {u.message}",
            busy=is_busy,
        )
        self.pill_sdr.set_telemetry(
            text=u.sdr_owner_badge,
            pct=u.sub_percent if is_busy else 100.0,
            accent_hex="#f59e0b" if is_busy else "#10b981",
            pulsing=is_busy,
        )

        if u.spectrum_frequencies is not None and u.spectrum_powers is not None:
            self.spectrum_canvas.set_spectrum_data(
                u.spectrum_frequencies,
                u.spectrum_powers,
                u.spectrum_floor,
            )

        if u.latest_pictogram is not None:
            pic = u.latest_pictogram
            vf = pic.visual_features
            self.picto_widget.set_frame_analysis(
                freq_mhz=u.current_freq_mhz,
                label=pic.label,
                confidence=pic.confidence,
                snr_db=max(0.0, vf.get("mean_peak_dbfs", -50.0) - vf.get("mean_floor_dbfs", -75.0)),
                peak_dbfs=vf.get("mean_peak_dbfs", -50.0),
                bw_hz=vf.get("measured_obw_hz", 25_000.0),
                frames_count=pic.frames_evaluated,
                hw_accel=pic.hardware_accelerated,
                composite_rgb=u.latest_composite_rgb,
                top_candidates=pic.top_candidates,
            )

    def _on_scan_finished(
        self,
        profiles: List[ClassifiedSignalProfile],
        saved_loc: Optional[SavedLocationProfile],
    ) -> None:
        self.btn_start_scan.setEnabled(True)
        self.btn_abort_scan.setEnabled(False)
        self.btn_reset_sdr.setEnabled(True)

        self.all_profiles = list(profiles)
        self.active_saved_profile = saved_loc
        self._scan_unlocked = True

        self._refresh_saved_profiles_table()
        self._apply_viable_filter_and_populate()

        if self.filtered_profiles:
            self.table_signals.selectRow(0)

        # In 5" Deck Mode, automatically switch to the unlocked Clickable Signals List!
        if self._is_compact_mode:
            self._switch_deck_tab(2)

        self._poll_hardware_and_gqrx()

    def _on_scan_error(self, err_msg: str) -> None:
        self.btn_start_scan.setEnabled(True)
        self.btn_abort_scan.setEnabled(False)
        self.btn_reset_sdr.setEnabled(True)
        self.stage_stepper.set_state(stage=0, running=False, elapsed_s=0.0, eta_s=0.0)
        self.dual_progress.set_progress(0.0, 0.0, f"Scan Error: {err_msg}", busy=False)
        # Re-unlock previous profile if one was already loaded
        if self.all_profiles:
            self._scan_unlocked = True
            self.table_signals.setEnabled(True)
        QMessageBox.critical(self, "RF Scanner Hardware Alert", f"Scan stopped:\n\n{err_msg}")

    # ------------------------------------------------------------------
    # Viable Signal Filtering & Clickable Signals Table Population
    # ------------------------------------------------------------------
    def _set_signal_filter_mode(self, mode: str) -> None:
        self._signal_filter_mode = mode
        if hasattr(self, "btn_filter_all"):
            self.btn_filter_all.setChecked(mode == "ALL")
            self.btn_filter_voice_bc.setChecked(mode == "VOICE_BROADCAST")
            self.btn_filter_any_voice.setChecked(mode == "ALL_VOICE")
        self._apply_viable_filter_and_populate()

    def _on_search_filter_changed(self, text: str) -> None:
        self._search_filter_text = text.strip()
        self._apply_viable_filter_and_populate()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_V and not (hasattr(self, "edit_filter_search") and self.edit_filter_search.hasFocus()):
            # Hotkey 'V' toggles between Voice Broadcast only and All signals
            new_mode = "ALL" if self._signal_filter_mode == "VOICE_BROADCAST" else "VOICE_BROADCAST"
            self._set_signal_filter_mode(new_mode)
            event.accept()
            return
        super().keyPressEvent(event)

    def _apply_viable_filter_and_populate(self) -> None:
        min_snr = float(self.slider_snr.value())
        min_peak = float(self.slider_peak.value())

        # Keep strict ascending numerical frequency order!
        ordered_all = sorted(self.all_profiles, key=lambda p: p.frequency_hz)
        viable_candidates = [
            p for p in ordered_all if (p.snr_db >= min_snr - 0.5) and (p.peak_dbm >= min_peak - 0.5)
        ]

        # Calculate live counts across viable signals
        vbc_count = sum(1 for p in viable_candidates if getattr(p, "is_voice_broadcast", False))
        all_v_count = sum(1 for p in viable_candidates if getattr(p, "is_any_voice", False))

        if hasattr(self, "btn_filter_all"):
            self.btn_filter_all.setText(f"🌐 All Signals ({len(viable_candidates)})")
            self.btn_filter_voice_bc.setText(f"📻 Voice Broadcast ({vbc_count})")
            self.btn_filter_any_voice.setText(f"🗣️ All Voice ({all_v_count})")

        # Apply active filter mode
        if self._signal_filter_mode == "VOICE_BROADCAST":
            candidates = [p for p in viable_candidates if getattr(p, "is_voice_broadcast", False)]
        elif self._signal_filter_mode == "ALL_VOICE":
            candidates = [p for p in viable_candidates if getattr(p, "is_any_voice", False)]
        else:
            candidates = list(viable_candidates)

        # Apply search query
        if self._search_filter_text:
            q = self._search_filter_text.lower()
            candidates = [
                p for p in candidates
                if (
                    q in p.signal_name.lower()
                    or q in f"{p.frequency_mhz:.4f}"
                    or q in p.modulation.lower()
                    or q in getattr(p, "voice_label", "").lower()
                    or q in p.profile_category.lower()
                )
            ]

        self.filtered_profiles = candidates
        total_n = len(ordered_all)
        shown_n = len(self.filtered_profiles)
        self.lbl_viable_count_badge.setText(f"VIABLE LISTED: {shown_n} / {total_n}")
        self.btn_tab_sigs.setText(f"◉ 3. SIGNALS ({shown_n})")

        # Update markers on spectrum plot
        markers = [(p.frequency_mhz, p.peak_dbm, p.modulation) for p in self.filtered_profiles]
        self.spectrum_canvas.set_signal_markers(markers)

        # Clear existing rows & cell widgets cleanly first to avoid orphaned Qt child buttons
        self.table_signals.setRowCount(0)
        self.table_signals.setRowCount(shown_n)
        for row, p in enumerate(self.filtered_profiles):
            is_vbc = getattr(p, "is_voice_broadcast", False)
            is_any_v = getattr(p, "is_any_voice", False)

            # Col 0: Exact Frequency (numerically ordered with voice icon)
            f_prefix = "📻 " if is_vbc else ("🗣️ " if is_any_v else "")
            f_item = QTableWidgetItem(f"{f_prefix}{p.frequency_mhz:8.4f} MHz")
            f_item.setFont(QFont("Monospace", 10, QFont.Weight.Bold))
            if is_vbc:
                f_item.setForeground(QColor("#10b981"))  # Emerald Green for Voice Broadcast!
            elif is_any_v:
                f_item.setForeground(QColor("#38bdf8"))  # Sky Blue for Two-Way Voice
            else:
                f_item.setForeground(QColor("#00f0ff"))  # Cyan for Data/Other
            self.table_signals.setItem(row, 0, f_item)

            # Col 1: Service Identity & Voice Broadcast Label
            id_item = QTableWidgetItem(p.signal_name)
            if is_vbc:
                id_item.setFont(QFont("SansSerif", 9, QFont.Weight.Bold))
                id_item.setForeground(QColor("#34d399"))  # Vibrant Emerald
                id_item.setToolTip(
                    f"📻 VOICE BROADCAST FREQUENCY\n"
                    f"Identity: {p.signal_name}\n"
                    f"Frequency: {p.frequency_mhz:.4f} MHz\n"
                    f"Demodulation: {p.modulation}\n"
                    f"SNR: +{p.snr_db:.1f} dB (Peak: {p.peak_dbm:.1f} dBFS)\n"
                    f"Click [▶ TUNE GQRX] to demodulate and listen live!"
                )
            elif is_any_v:
                id_item.setForeground(QColor("#bae6fd"))
                id_item.setToolTip(
                    f"🗣️ TWO-WAY VOICE CHANNEL\n"
                    f"Identity: {p.signal_name}\n"
                    f"Frequency: {p.frequency_mhz:.4f} MHz\n"
                    f"Demodulation: {p.modulation}"
                )
            else:
                id_item.setForeground(QColor("#cbd5e1"))
            self.table_signals.setItem(row, 1, id_item)

            # Col 2: Highlighted Gqrx Demod Mode
            mod_item = QTableWidgetItem(p.modulation)
            mod_item.setFont(QFont("Monospace", 9, QFont.Weight.Bold))
            mod_item.setForeground(QColor("#fbbf24"))
            self.table_signals.setItem(row, 2, mod_item)

            # Col 3: Graphical Bar + Alphanumeric SNR / Peak dBFS
            bars_n = max(1, min(6, int(round(p.snr_db / 5.0))))
            meter_blocks = "▮" * bars_n + "▯" * (6 - bars_n)
            snr_item = QTableWidgetItem(f"{meter_blocks} +{p.snr_db:4.1f}dB ({p.peak_dbm:5.1f}dBFS)")
            snr_item.setFont(QFont("Monospace", 8, QFont.Weight.Bold))
            snr_item.setForeground(QColor("#34d399" if p.snr_db >= 16.0 else "#38bdf8"))
            self.table_signals.setItem(row, 3, snr_item)

            # Col 4: Occupied Bandwidth
            bw_txt = f"{p.bandwidth_hz / 1000.0:.1f} kHz"
            self.table_signals.setItem(row, 4, QTableWidgetItem(bw_txt))

            # Col 5: Category
            cat_display = "📻 Voice_Broadcast" if is_vbc else p.profile_category
            cat_item = QTableWidgetItem(cat_display)
            cat_colors = {
                "Broadcast_Audio": "#34d399",
                "Voice_Broadcast": "#10b981",
                "Aviation_Airband": "#fbbf24",
                "Emergency_Weather": "#f87171",
                "Pictogram_Exotic": "#f472b6",
                "Amateur_Radio": "#c084fc",
                "Marine_VHF": "#22d3ee",
            }
            cat_item.setForeground(QColor("#10b981" if is_vbc else cat_colors.get(p.profile_category, "#93c5fd")))
            self.table_signals.setItem(row, 5, cat_item)

            # Col 6: Hailo-8 AI Confidence & Frame Count
            npu = "⚡" if p.hailo_accelerated else ""
            ai_item = QTableWidgetItem(
                f"{npu}{p.pictogram_label} ({int(p.pictogram_confidence * 100)}% • {p.frames_evaluated}f)"
            )
            self.table_signals.setItem(row, 6, ai_item)

            # Col 7: Occupied Bandwidth + Calibrated HackRF Gains
            g_str = f"{p.bandwidth_hz / 1000.0:.0f}k • IF:{p.lna_gain} BB:{p.vga_gain}"
            self.table_signals.setItem(row, 7, QTableWidgetItem(g_str))

            # Col 8: Calibrated Gqrx Pandapter/Waterfall Floor & Ceiling
            wf_item = QTableWidgetItem(f"{p.plot_min:.0f}..{p.plot_max:.0f} dBFS")
            self.table_signals.setItem(row, 8, wf_item)

            # Col 9: Clickable Action Button (unparented before setCellWidget so viewport owns it)
            tool = getattr(p, "demod_tool", "gqrx")
            if p.is_voice_broadcast:
                btn_text = "📻 TUNE GQRX"
            elif tool == "rtl_433":
                btn_text = "📊 DEMOD 433"
            elif tool == "w_scan_cpp":
                btn_text = "📺 ATSC SCAN"
            elif tool == "readsb":
                btn_text = "✈️ ADS-B"
            elif tool == "multimon_ng":
                btn_text = "📟 PAGER"
            else:
                btn_text = "▶ TUNE GQRX"

            btn_tune = QPushButton(btn_text)
            btn_tune.setObjectName("btnTuneRow")
            btn_tune.setMinimumWidth(104)
            btn_tune.clicked.connect(lambda _, prof=p: self.handle_signal_demod_action(prof))
            self.table_signals.setCellWidget(row, 9, btn_tune)

        can_click = self._scan_unlocked and (not self.pipeline.is_scanning()) and (not self._tuning_busy)
        self.table_signals.setEnabled(can_click)

        if can_click and self.active_saved_profile is not None:
            dt_tag = self.active_saved_profile.approx_datetime_local
            loc_short = self.active_saved_profile.locality_name.split(",")[0]
            if self._signal_filter_mode == "VOICE_BROADCAST":
                self.lbl_lock_banner.setText(
                    f"✔ FILTER: [📻 Voice Broadcast] — Showing {shown_n} broadcast stations (out of {len(viable_candidates)} viable). Tap ▶ TUNE GQRX for live audio!"
                )
                self.lbl_lock_banner.setStyleSheet(
                    "background-color: #052e16; border: 1px solid #10b981; border-radius: 4px; "
                    "color: #34d399; padding: 4px 8px; font-weight: bold;"
                )
            elif self._signal_filter_mode == "ALL_VOICE":
                self.lbl_lock_banner.setText(
                    f"✔ FILTER: [🗣️ All Voice] — Showing {shown_n} voice channels (out of {len(viable_candidates)} viable). Tap ▶ TUNE GQRX for live audio!"
                )
                self.lbl_lock_banner.setStyleSheet(
                    "background-color: #082f49; border: 1px solid #0284c7; border-radius: 4px; "
                    "color: #38bdf8; padding: 4px 8px; font-weight: bold;"
                )
            else:
                self.lbl_lock_banner.setText(
                    f"✔ UNLOCKED [{loc_short} • {dt_tag}] — {shown_n} viable signals ready. Tap ▶ TUNE GQRX for live audio!"
                )
                self.lbl_lock_banner.setStyleSheet(
                    "background-color: #062e1e; border: 1px solid #10b981; border-radius: 4px; "
                    "color: #34d399; padding: 4px 8px; font-weight: bold;"
                )

    def _on_signal_row_selected(self) -> None:
        row = self.table_signals.currentRow()
        if 0 <= row < len(self.filtered_profiles):
            p = self.filtered_profiles[row]
            markers = [(x.frequency_mhz, x.peak_dbm, x.modulation) for x in self.filtered_profiles]
            self.spectrum_canvas.set_signal_markers(markers, highlight_mhz=p.frequency_mhz)
            self.picto_widget.set_frame_analysis(
                freq_mhz=p.frequency_mhz,
                label=p.pictogram_label,
                confidence=p.pictogram_confidence,
                snr_db=p.snr_db,
                peak_dbfs=p.peak_dbm,
                bw_hz=p.bandwidth_hz,
                frames_count=p.frames_evaluated,
                hw_accel=p.hailo_accelerated,
                thumbnail_path=p.thumbnail_path,
                top_candidates=[{"class": p.pictogram_neural_class, "score": p.pictogram_confidence}],
            )

    def _on_signal_row_double_clicked(self, row: int, _col: int) -> None:
        if 0 <= row < len(self.filtered_profiles):
            self.handle_signal_demod_action(self.filtered_profiles[row])

    def _on_signals_context_menu(self, pos) -> None:
        row = self.table_signals.rowAt(pos.y())
        if not (0 <= row < len(self.filtered_profiles)):
            return
        p = self.filtered_profiles[row]
        tool = getattr(p, "demod_tool", "gqrx")
        menu = QMenu(self)
        act_gqrx = menu.addAction(f"📻 Tune in Gqrx SDR ({p.modulation})")
        act_tool = None
        if tool != "gqrx":
            act_tool = menu.addAction(f"🚀 Launch Dedicated Decoder ({tool}) in Terminal")
        menu.addSeparator()
        act_copy_f = menu.addAction(f"📋 Copy Frequency ({p.frequency_mhz:.4f} MHz)")
        act_copy_cmd = menu.addAction("📋 Copy Demod Command")

        action = menu.exec(self.table_signals.viewport().mapToGlobal(pos))
        if action == act_gqrx:
            self.tune_signal_in_gqrx(p)
        elif action == act_tool and act_tool is not None:
            self.handle_signal_demod_action(p)
        elif action == act_copy_f:
            cb = QApplication.clipboard()
            if cb:
                cb.setText(f"{p.frequency_mhz:.4f}")
        elif action == act_copy_cmd:
            cb = QApplication.clipboard()
            if cb:
                cmd = getattr(p, "demod_cmd", "") or "gqrx"
                cb.setText(cmd)

    def handle_signal_demod_action(self, profile: ClassifiedSignalProfile) -> None:
        """Dispatches signal demodulation to specialized digital/TV decoder or Gqrx SDR receiver."""
        if self.pipeline.is_scanning():
            QMessageBox.warning(
                self,
                "SDR Locked by Active Scan",
                "Wait for all scan layers and profile refinement to complete before opening demodulator.",
            )
            return
        if self._tuning_busy or self._usb_reset_busy:
            return

        tool = getattr(profile, "demod_tool", "gqrx")
        # If specialized digital telemetry / TV scanner tool and not primarily analog voice:
        if tool in ("rtl_433", "w_scan_cpp", "readsb", "multimon_ng", "direwolf") and not profile.is_voice_broadcast:
            # Terminate Gqrx cleanly first so HackRF USB transceiver is unlocked
            if self.gqrx_ctrl.is_running():
                self.gqrx_ctrl.terminate()
                time.sleep(0.3)

            cmd = getattr(profile, "demod_cmd", "")
            title = f"{profile.voice_label or profile.signal_name} ({profile.frequency_mhz:.3f} MHz)"
            ok = launch_in_gui_terminal(cmd, window_title=title)
            if ok:
                self.dual_progress.set_progress(
                    100.0,
                    100.0,
                    f"Launched {tool} terminal for {profile.frequency_mhz:.3f} MHz...",
                    busy=False,
                )
                self.pill_gqrx.set_telemetry(
                    f"{tool.upper()} LIVE",
                    pct=100.0,
                    accent_hex="#10b981",
                    pulsing=False,
                )
                return

        # Voice broadcasts or analog signals tune live in Gqrx
        self.tune_signal_in_gqrx(profile)

    # ------------------------------------------------------------------
    # Non-Blocking Gqrx Translation, Launch & Live Verification
    # ------------------------------------------------------------------
    def tune_signal_in_gqrx(self, profile: ClassifiedSignalProfile) -> None:
        if self.pipeline.is_scanning():
            QMessageBox.warning(
                self,
                "SDR Locked by Active Scan",
                "Wait for all scan layers and profile refinement to complete before opening Gqrx.",
            )
            return
        if self._tuning_busy or self._usb_reset_busy:
            return

        self._tuning_busy = True
        self.btn_start_scan.setEnabled(False)
        self.btn_reset_sdr.setEnabled(False)
        self.dual_progress.set_progress(
            20.0,
            20.0,
            f"Configuring Gqrx -> {profile.frequency_mhz:.4f} MHz ({profile.modulation}, "
            f"IF:{profile.lna_gain}dB BB:{profile.vga_gain}dB)...",
            busy=True,
        )
        self.pill_gqrx.set_telemetry(
            f"TUNING {profile.frequency_mhz:.3f}M",
            pct=45.0,
            accent_hex="#f59e0b",
            pulsing=True,
        )

        is_compact = self._is_compact_mode
        all_profs = list(self.filtered_profiles) if self.filtered_profiles else list(self.all_profiles)

        def _tune_worker() -> None:
            res = self.gqrx_ctrl.tune_and_launch(
                signal_profile=profile,
                all_profiles=all_profs,
                organizer=self.organizer,
                compact_screen=is_compact,
                progress_cb=lambda msg, pct: self.bridge.gqrx_progress.emit(msg, pct),
            )
            self.bridge.gqrx_finished.emit(res)

        threading.Thread(target=_tune_worker, daemon=True, name="GqrxTuneWorker").start()

    def _on_gqrx_progress(self, msg: str, pct: float) -> None:
        self.dual_progress.set_progress(pct, pct, msg, busy=(pct < 100.0))
        self.pill_gqrx.set_telemetry(
            text="GQRX: HANDSHAKE...",
            pct=pct,
            accent_hex="#00f0ff",
            pulsing=(pct < 100.0),
        )

    def _on_gqrx_finished(self, res: Dict[str, Any]) -> None:
        self._tuning_busy = False
        self.btn_start_scan.setEnabled(True)
        self.btn_reset_sdr.setEnabled(True)
        self.table_signals.setEnabled(self._scan_unlocked)

        if not res.get("success", False):
            err = res.get("error", "Unknown Gqrx tuning error")
            self.dual_progress.set_progress(0.0, 0.0, f"Gqrx blocked: {err}", busy=False)
            QMessageBox.warning(self, "SDR Resource Arbiter", str(err))
            return

        freq_mhz = res.get("frequency_hz", 0) / 1e6
        demod = res.get("ui_demod", res.get("mode", ""))
        sig_db = float(res.get("signal_strength_dbfs", -55.0))
        meter_pct = max(5.0, min(100.0, (sig_db + 95.0) / 75.0 * 100.0))

        self.dual_progress.set_progress(
            100.0,
            100.0,
            f"((•)) Gqrx Live Demod Active @ {freq_mhz:.4f} MHz [{demod}] • Signal: {sig_db:.1f} dBFS",
            busy=False,
        )
        self.pill_gqrx.set_telemetry(
            text=f"LIVE {freq_mhz:.3f}M {sig_db:.1f}dB",
            pct=meter_pct,
            accent_hex="#10b981",
            pulsing=False,
        )
        self.btn_release_gqrx.setVisible(True)
        self._poll_hardware_and_gqrx()

    def _release_gqrx_manually(self) -> None:
        self.arbiter.stop_gqrx_cleanly()
        self.btn_release_gqrx.setVisible(False)
        self.dual_progress.set_progress(
            100.0,
            100.0,
            "Gqrx closed cleanly & HackRF One USB lock released to Standby.",
            busy=False,
        )
        self._poll_hardware_and_gqrx()

    def _on_reset_sdr_clicked(self) -> None:
        """1-tap non-root HackRF One hardware USB port reset & Gqrx crash-lock sanitizer."""
        if self.pipeline.is_scanning() or self._tuning_busy or self._usb_reset_busy:
            return

        self._usb_reset_busy = True
        self.btn_start_scan.setEnabled(False)
        self.btn_reset_sdr.setEnabled(False)
        self.table_signals.setEnabled(False)
        self.btn_release_gqrx.setVisible(False)
        self.dual_progress.set_progress(
            20.0,
            20.0,
            "Resetting HackRF One USB transceiver & clearing Gqrx locks...",
            busy=True,
        )
        self.pill_sdr.set_telemetry(
            text="SDR: USB RESET...",
            pct=35.0,
            accent_hex="#00f0ff",
            pulsing=True,
        )

        def _reset_worker() -> None:
            ok, msg = self.arbiter.reset_hackrf_usb_port(
                progress_cb=lambda m, p: self.bridge.usb_reset_progress.emit(m, p),
                stop_gqrx=True,
            )
            self.bridge.usb_reset_finished.emit(ok, msg)

        threading.Thread(target=_reset_worker, daemon=True, name="HackRFUsbResetWorker").start()

    def _on_usb_reset_progress(self, msg: str, pct: float) -> None:
        self.dual_progress.set_progress(pct, pct, msg, busy=(pct < 100.0))
        self.pill_sdr.set_telemetry(
            text="SDR: USB SELF-HEAL",
            pct=pct,
            accent_hex="#00f0ff",
            pulsing=(pct < 100.0),
        )

    def _on_usb_reset_finished(self, ok: bool, msg: str) -> None:
        self._usb_reset_busy = False
        self.btn_start_scan.setEnabled(True)
        self.btn_reset_sdr.setEnabled(True)
        self.table_signals.setEnabled(self._scan_unlocked)

        if ok:
            self.dual_progress.set_progress(
                100.0,
                100.0,
                f"✔ HackRF One USB Self-Heal Complete: {msg} — Ready for Scan or Gqrx!",
                busy=False,
            )
        else:
            self.dual_progress.set_progress(0.0, 0.0, f"SDR USB Reset Alert: {msg}", busy=False)
            QMessageBox.warning(self, "HackRF One USB Diagnostics", msg)
        self._poll_hardware_and_gqrx()

    # ------------------------------------------------------------------
    # Permanent Location Profiles Management (Reboot-Persistent)
    # ------------------------------------------------------------------
    def _refresh_saved_profiles_table(self) -> None:
        entries = self.organizer.list_saved_location_profiles()
        self.btn_tab_prof.setText(f"★ 4. PROFILES ({len(entries)})")
        self.table_profiles.setRowCount(0)
        self.table_profiles.setRowCount(len(entries))

        for row, entry in enumerate(entries):
            dt_str = str(entry.get("approx_datetime_local") or entry.get("utc_timestamp_iso", ""))
            loc_str = f"{entry.get('locality_name', '').split(',')[0]} ({entry.get('maidenhead_grid', '')})"
            mode_str = f"{entry.get('scan_mode', 'QUICK')} {entry.get('freq_min_mhz', 88)}-{entry.get('freq_max_mhz', 137)}MHz"
            cnt_str = f"{entry.get('signal_count', 0)} viable"

            dt_item = QTableWidgetItem(dt_str)
            dt_item.setForeground(QColor("#00f0ff"))
            self.table_profiles.setItem(row, 0, dt_item)
            self.table_profiles.setItem(row, 1, QTableWidgetItem(loc_str))

            m_item = QTableWidgetItem(mode_str)
            m_item.setForeground(QColor("#34d399"))
            self.table_profiles.setItem(row, 2, m_item)
            self.table_profiles.setItem(row, 3, QTableWidgetItem(cnt_str))

            act_cell = QWidget()
            hbox = QHBoxLayout(act_cell)
            hbox.setContentsMargins(2, 2, 2, 2)
            hbox.setSpacing(4)

            j_path = str(entry.get("json_path", ""))
            btn_load = QPushButton("LOAD", act_cell)
            btn_load.setObjectName("btnTuneRow")
            btn_load.setFixedSize(64, 25)
            btn_load.setStyleSheet("padding: 2px 4px; font-weight: bold;")
            btn_load.clicked.connect(lambda _, jp=j_path: self._load_saved_profile_by_path(jp))

            btn_del = QPushButton("DEL", act_cell)
            btn_del.setToolTip("Delete saved Location Profile")
            btn_del.setFixedSize(52, 25)
            btn_del.setStyleSheet("padding: 2px 4px; font-weight: bold;")
            btn_del.clicked.connect(lambda _, jp=j_path: self._delete_saved_profile_by_path(jp))

            hbox.addWidget(btn_load)
            hbox.addWidget(btn_del)
            self.table_profiles.setCellWidget(row, 4, act_cell)

    def _load_initial_saved_profile(self) -> None:
        latest = self.organizer.load_latest_location_profile()
        if latest is not None and latest.signals:
            self._activate_loaded_profile(latest, switch_tab=False)

    def _load_saved_profile_by_path(self, json_path: str) -> None:
        if self.pipeline.is_scanning():
            return
        loaded = self.organizer.load_location_profile(json_path)
        if loaded is not None:
            self._activate_loaded_profile(loaded, switch_tab=True)

    def _activate_loaded_profile(self, prof: SavedLocationProfile, switch_tab: bool = True) -> None:
        self.active_saved_profile = prof
        self.all_profiles = list(prof.signals)
        self._scan_unlocked = True

        # Sync UI thresholds cleanly so all signals in the loaded profile are visible immediately
        if prof.signals:
            min_sig_snr = int(math.floor(min(s.snr_db for s in prof.signals)))
            min_sig_pk = int(math.floor(min(s.peak_dbm for s in prof.signals)))
            if self.slider_snr.value() > min_sig_snr:
                self.slider_snr.blockSignals(True)
                self.slider_snr.setValue(max(3, min_sig_snr))
                self.slider_snr.blockSignals(False)
            if self.slider_peak.value() > min_sig_pk:
                self.slider_peak.blockSignals(True)
                self.slider_peak.setValue(max(-95, min_sig_pk))
                self.slider_peak.blockSignals(False)
            self.lbl_snr_pill.setText(f"SNR >= +{self.slider_snr.value()} dB")
            self.lbl_peak_pill.setText(f"PEAK >= {self.slider_peak.value()} dBFS")

        # Render reconstructed RF profile envelope on spectrum canvas when loading a saved profile from disk
        if prof.signals:
            f_lo = float(prof.freq_min_mhz) * 1e6
            f_hi = float(prof.freq_max_mhz) * 1e6
            grid_f = np.linspace(f_lo, f_hi, 320)
            avg_floor = float(np.median([s.noise_floor_dbm for s in prof.signals]))
            floor_arr = np.full_like(grid_f, avg_floor)
            pwr_arr = np.full_like(grid_f, avg_floor + 1.2)
            for s in prof.signals:
                bw = max(120_000.0, float(s.bandwidth_hz) * 2.0)
                env = (s.peak_dbm - avg_floor) * np.exp(-0.5 * ((grid_f - float(s.frequency_hz)) / (bw * 0.5)) ** 2)
                pwr_arr = np.maximum(pwr_arr, avg_floor + env)
            self.spectrum_canvas.set_spectrum_data(grid_f, pwr_arr, floor_arr)

        self._apply_viable_filter_and_populate()
        if self.filtered_profiles:
            self.table_signals.selectRow(0)
            self._on_signal_row_selected()

        self.dual_progress.set_progress(
            100.0,
            100.0,
            f"Loaded Saved Profile: {prof.display_title}",
            busy=False,
        )
        if switch_tab and self._is_compact_mode:
            self._switch_deck_tab(2)

    def _delete_saved_profile_by_path(self, json_path: str) -> None:
        if self.organizer.delete_location_profile(json_path):
            self._refresh_saved_profiles_table()

    def _sync_current_bookmarks_to_gqrx(self) -> None:
        targets = self.filtered_profiles if self.filtered_profiles else self.all_profiles
        if not targets:
            QMessageBox.information(self, "Gqrx Bookmarks", "Run or load a Location Profile first.")
            return
        self.organizer.export_gqrx_bookmarks(targets)
        self.dual_progress.set_progress(
            100.0,
            100.0,
            f"Synced {len(targets)} classified bookmarks to ~/.config/gqrx/bookmarks.csv!",
            busy=False,
        )

    # ------------------------------------------------------------------
    # Periodic Hardware, GPS & Live Gqrx Telemetry Polling
    # ------------------------------------------------------------------
    def _on_anim_tick(self) -> None:
        self._anim_phase += 0.28
        self.pill_sdr.step_animation(self._anim_phase)
        self.pill_gqrx.step_animation(self._anim_phase)
        self.stage_stepper.step_animation(self._anim_phase)
        self.dual_progress.step_animation(self._anim_phase)

    def _poll_desktop_ipc(self) -> None:
        """Handles single-instance IPC commands from Desktop launcher icons."""
        if not os.path.exists(GUI_IPC_FILE):
            return
        try:
            with open(GUI_IPC_FILE, "r", encoding="utf-8") as f:
                cmd = json.load(f)
            os.remove(GUI_IPC_FILE)
        except Exception:
            try:
                os.remove(GUI_IPC_FILE)
            except Exception:
                pass
            return

        if self.isMinimized():
            self.showNormal()
        self.raise_()
        self.activateWindow()

        action = str(cmd.get("action", "")).upper()
        mode = str(cmd.get("mode", "")).upper()
        if action == "AUTO_SCAN" and mode in ("QUICK", "DEEP", "CUSTOM"):
            self._select_scan_mode(mode)
            if mode in ("QUICK", "DEEP"):
                if not self.pipeline.is_scanning() and not self._tuning_busy and not self._usb_reset_busy:
                    QTimer.singleShot(120, self.start_scan)
            else:
                self._switch_deck_tab(0)

    def _poll_hardware_and_gqrx(self) -> None:
        # 1. Hailo-8 26 TOPS PCIe NPU (in-memory check)
        hw_ai = self.pipeline.picto_mgr.is_hardware_accelerated
        self.pill_hailo.set_telemetry(
            text="HAILO-8: 26T PCIe" if hw_ai else "AI: NEURAL FALLBACK",
            pct=100.0 if hw_ai else 65.0,
            accent_hex="#00f0ff" if hw_ai else "#fbbf24",
            tooltip="Hailo-8 26 TOPS Spectrogram Pictogram Classifier (/dev/hailo0)",
        )

        # 2. u-blox GPS & Locality (cached in-memory fix)
        fix = self.gps_mgr.get_current_fix()
        city_short = fix.locality_name.split(",")[0].strip()
        if fix.source in ("USB_UBLOX_GPS", "GPSD_DAEMON"):
            gps_txt = f"GPS 3D:{fix.satellites_used}S {fix.maidenhead_grid}"
            gps_col = "#34d399"
        else:
            gps_txt = f"{fix.maidenhead_grid} • {city_short[:12]}"
            gps_col = "#38bdf8"
        self.pill_gps.set_telemetry(
            text=gps_txt,
            pct=fix.fix_quality_pct,
            accent_hex=gps_col,
            tooltip=f"{fix.status_message}\nApprox Local Time: {fix.local_datetime_approx}",
        )

        # 3. Off-main-thread passive sysfs SDR snapshot & serialized Gqrx TCP 7356 telemetry
        if (
            self.pipeline.is_scanning()
            or self._tuning_busy
            or self._usb_reset_busy
            or self._hw_poll_inflight
        ):
            return

        self._hw_poll_inflight = True

        def _bg_telemetry_worker() -> None:
            try:
                snap = self.arbiter.get_status_snapshot()
                g_tel: Dict[str, Any] = {}
                if snap.gqrx_running and not self._tuning_busy:
                    g_tel = self.gqrx_ctrl.query_live_telemetry()
                self.bridge.hw_telemetry_ready.emit({"snap": snap, "gqrx": g_tel})
            except Exception:
                self._hw_poll_inflight = False

        threading.Thread(target=_bg_telemetry_worker, daemon=True, name="BgTelemetryPoll").start()

    def _on_hw_telemetry_ready(self, payload: Dict[str, Any]) -> None:
        self._hw_poll_inflight = False
        if self.pipeline.is_scanning() or self._tuning_busy or self._usb_reset_busy:
            return

        snap = payload.get("snap")
        g_tel = payload.get("gqrx") or {}
        if snap is None:
            return

        sdr_col = "#10b981" if snap.hackrf_online else "#ef4444"
        if snap.gqrx_running:
            sdr_col = "#38bdf8"
        self.pill_sdr.set_telemetry(
            text=snap.badge_text,
            pct=snap.progress_pct,
            accent_hex=sdr_col,
            pulsing=False,
            tooltip=snap.detail_text,
        )

        if snap.gqrx_running:
            self.btn_release_gqrx.setVisible(True)
            if g_tel.get("tcp_connected"):
                fm = g_tel.get("frequency_mhz", 0.0)
                sig_db = float(g_tel.get("signal_strength_dbfs", -75.0))
                dsp_on = g_tel.get("dsp_active", False)
                mode_s = g_tel.get("mode", "FM")
                pct = max(5.0, min(100.0, (sig_db + 95.0) / 75.0 * 100.0))
                icon = "LIVE " if dsp_on else "PAUSED "
                self.pill_gqrx.set_telemetry(
                    text=f"{icon}{fm:.3f}M {sig_db:.1f}dB",
                    pct=pct,
                    accent_hex="#10b981" if dsp_on else "#f59e0b",
                    tooltip=f"Gqrx Live TCP 7356: {fm:.4f} MHz [{mode_s}] • Signal {sig_db:.1f} dBFS • DSP={'ON' if dsp_on else 'PAUSED'}",
                )
                if dsp_on:
                    self.dual_progress.set_progress(
                        100.0,
                        100.0,
                        f"((•)) Gqrx Live Demod Active @ {fm:.4f} MHz [{mode_s}] • Signal: {sig_db:.1f} dBFS",
                        busy=False,
                    )
            elif not g_tel.get("busy"):
                self.pill_gqrx.set_telemetry("GQRX: ACTIVE", pct=80.0, accent_hex="#38bdf8")
        else:
            self.btn_release_gqrx.setVisible(False)
            self.pill_gqrx.set_telemetry(
                "GQRX: STANDBY",
                pct=0.0,
                accent_hex="#64748b",
                tooltip="Click any unlocked signal row or ▶ TUNE GQRX to launch & demodulate live",
            )

    def closeEvent(self, event: Any) -> None:
        try:
            self._anim_timer.stop()
            self._hw_timer.stop()
            self._ipc_timer.stop()
            if self.pipeline.is_scanning():
                self.pipeline.stop_scan()
            self.gps_mgr.stop()
            self.pipeline.picto_mgr.close()
        except Exception:
            pass
        super().closeEvent(event)


def main(auto_scan_mode: Optional[str] = None) -> None:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("AI Smart RF Scanner")
    window = SmartScannerMainWindow(auto_scan_mode=auto_scan_mode)
    window.show()
    rc = app.exec()
    os._exit(rc)


if __name__ == "__main__":
    main()
