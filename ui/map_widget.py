"""
Tactical Radar & Telemetry Map Widget for AI-Powered Smart RF Scanner
Renders:
  1. ADS-B 1090 MHz Tactical Radar & Airspace Surveillance Map
     - Centered at Home Base: J St & 13th St, Penrose, CO 81240 (38.4112° N, -105.0366° W, DM78vj)
     - Concentric range rings (10, 25, 50, 100, 150 NM) with nautical & statute scale
     - Compass rose & 360-degree radial bearing lines
     - Local landmarks & transmitters: Fremont County Airport (1V6 Penrose), Pueblo Memorial (KPUB),
       Colorado Springs (KCOS), Cheyenne Mountain Broadcast Towers, Royal Gorge / Cañon City, US-50
     - Live aircraft plotting with heading vectors, altitude tags, airspeed, callsigns, squawk, climb/descent
     - Animated tactical phosphor radar sweep beam
  2. Live Telemetry Streams:
     - Decoded POCSAG / FLEX Pager Dispatch Feed (Penrose/Florence Fire, Emergency Alerts)
     - Decoded APRS AX.25 Tactical Packet Stream (144.390 MHz Ham telemetry & weather beacons)
     - Decoded ISM IoT Telemetry (433.92 MHz & 915 MHz sensors)
  3. Instant 1-Click Demodulation Quick-Launch Bar for all Penrose & Fremont County channels.
"""

from __future__ import annotations

import json
import math
import os
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

from PyQt6.QtCore import Qt, QTimer, QRectF, QPointF, pyqtSignal
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
    QRadialGradient,
)
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

# Reference Location: Corner of J St & 13th St, Penrose, CO 81240
PENROSE_HOME_LAT = 38.4112
PENROSE_HOME_LON = -105.0366
PENROSE_ELEV_M = 1615.0
PENROSE_GRID = "DM78vj"

EARTH_RADIUS_NM = 3440.065


def haversine_distance_bearing_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> Tuple[float, float]:
    """Calculates distance in nautical miles and initial true bearing in degrees."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = math.sin(delta_phi * 0.5) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda * 0.5) ** 2
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))
    dist_nm = EARTH_RADIUS_NM * c

    y = math.sin(delta_lambda) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(delta_lambda)
    bearing = (math.degrees(math.atan2(y, x)) + 360.0) % 360.0

    return dist_nm, bearing


@dataclass
class TacticalWaypoint:
    code: str
    name: str
    latitude: float
    longitude: float
    waypoint_type: str  # "HOME", "AIRPORT", "TRANSMITTER", "RAIL_YARD", "CORRIDOR"
    frequency_mhz: float
    service: str
    icon: str


WAYPOINTS_LOCAL: List[TacticalWaypoint] = [
    TacticalWaypoint("HOME", "Home Base (J & 13th St)", PENROSE_HOME_LAT, PENROSE_HOME_LON, "HOME", 0.0, "Deck Station", "📍"),
    TacticalWaypoint("1V6", "Fremont Co Airport", 38.4287, -105.1054, "AIRPORT", 122.800, "CTAF 122.8 / AWOS 120.025", "✈️"),
    TacticalWaypoint("PUB", "Pueblo Memorial Airport", 38.2891, -104.4966, "AIRPORT", 119.100, "Tower 119.1 / ATIS 125.25", "✈️"),
    TacticalWaypoint("COS", "Colorado Springs Airport", 38.8058, -104.7008, "AIRPORT", 120.100, "Approach 120.1 / Tower 119.9", "✈️"),
    TacticalWaypoint("CHEY", "Cheyenne Mtn TV/FM Towers", 38.7428, -104.8828, "TRANSMITTER", 539.000, "ATSC TV Ch 22,24,25,26 / FM", "⛰️"),
    TacticalWaypoint("GORG", "Royal Gorge / UP Rail", 38.4328, -105.3255, "RAIL_YARD", 161.490, "UP AAR 92 / Royal Gorge AAR 32", "🚆"),
    TacticalWaypoint("PEN_FD", "Penrose Fire Dept", 38.4258, -105.0044, "TRANSMITTER", 155.880, "Penrose Fire Ops / Paging", "🚒"),
    TacticalWaypoint("US50", "US-50 Trucker Corridor", 38.4220, -105.0200, "CORRIDOR", 27.185, "CB Ch 19 Trucker Highway Calling", "🚛"),
]


@dataclass
class AircraftTarget:
    icao: str
    callsign: str
    latitude: float
    longitude: float
    altitude_ft: int
    ground_speed_kt: int
    heading_deg: float
    vertical_rate_fpm: int
    squawk: str
    aircraft_type: str
    distance_nm: float = 0.0
    bearing_deg: float = 0.0
    history: List[Tuple[float, float]] = field(default_factory=list)
    last_update_ts: float = 0.0


@dataclass
class DecodedMessageRecord:
    timestamp_str: str
    channel_type: str   # "POCSAG", "APRS", "ISM_433", "ISM_915", "ATSC_TV"
    frequency_mhz: float
    source_tag: str
    content: str
    color_hex: str


class ADSBRadarMapCanvas(QWidget):
    """
    Antialiased tactical radar & airspace display centered on Penrose, Colorado.
    """
    aircraft_selected = pyqtSignal(object)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(280, 260)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)

        self.home_lat = PENROSE_HOME_LAT
        self.home_lon = PENROSE_HOME_LON
        self.max_range_nm = 50.0  # Default visible radius in NM (10, 25, 50, 100, 150)
        self.sweep_angle_deg = 0.0
        self.selected_target: Optional[AircraftTarget] = None
        self.targets: Dict[str, AircraftTarget] = {}

        # Range ring levels in NM
        self.ring_steps = [10.0, 25.0, 50.0, 75.0, 100.0, 150.0]

        # Seed realistic Colorado Front Range airspace traffic (Penrose, COS, PUB corridor)
        self._init_mock_targets()

    def set_home_coords(self, lat: float, lon: float) -> None:
        self.home_lat = lat
        self.home_lon = lon
        self.update()

    def zoom_in(self) -> None:
        steps = [15.0, 25.0, 50.0, 100.0, 150.0]
        for s in reversed(steps):
            if s < self.max_range_nm:
                self.max_range_nm = s
                self.update()
                break

    def zoom_out(self) -> None:
        steps = [15.0, 25.0, 50.0, 100.0, 150.0]
        for s in steps:
            if s > self.max_range_nm:
                self.max_range_nm = s
                self.update()
                break

    def reset_view(self) -> None:
        self.max_range_nm = 50.0
        self.selected_target = None
        self.update()

    def update_sweep_angle(self, delta_deg: float = 2.5) -> None:
        self.sweep_angle_deg = (self.sweep_angle_deg + delta_deg) % 360.0
        self._simulate_step()
        self.update()

    def _init_mock_targets(self) -> None:
        now = time.time()
        # Realistic air traffic around Pueblo, Colorado Springs, and Fremont County
        seeds = [
            ("A14B20", "UAL1422", 38.6500, -104.8200, 28000, 440, 345.0, 1200, "1200", "B738"),
            ("A89C33", "SWA291", 38.3200, -104.9100, 14500, 310, 195.0, -800, "2411", "B737"),
            ("AB4102", "FFL3", 38.4500, -105.1500, 7800, 135, 80.0, 0, "1200", "EC35"),  # Flight for Life Medevac
            ("A32F19", "N1V6CO", 38.4180, -105.0800, 6200, 110, 95.0, 250, "1200", "C172"),   # Fremont Co Airport traffic
            ("A091FE", "SKW4811", 38.7800, -104.6500, 8900, 210, 350.0, -1100, "3520", "CRJ7"), # COS Approach
            ("AC3310", "FDX104", 38.2200, -104.4200, 34000, 480, 160.0, 0, "1405", "B763"),    # High-altitude overflight
        ]
        for icao, call, lat, lon, alt, spd, hdg, vr, sq, typ in seeds:
            d, b = haversine_distance_bearing_nm(self.home_lat, self.home_lon, lat, lon)
            self.targets[icao] = AircraftTarget(
                icao=icao,
                callsign=call,
                latitude=lat,
                longitude=lon,
                altitude_ft=alt,
                ground_speed_kt=spd,
                heading_deg=hdg,
                vertical_rate_fpm=vr,
                squawk=sq,
                aircraft_type=typ,
                distance_nm=round(d, 1),
                bearing_deg=round(b, 1),
                history=[(lat, lon)],
                last_update_ts=now,
            )

    def _simulate_step(self) -> None:
        now = time.time()
        for target in self.targets.values():
            # Advance position according to ground speed and heading
            spd_deg_s = (target.ground_speed_kt / 3600.0) / 60.0  # approx degrees per sec
            rad = math.radians(target.heading_deg)
            target.latitude += spd_deg_s * math.cos(rad) * 0.15
            target.longitude += (spd_deg_s * math.sin(rad) * 0.15) / math.cos(math.radians(target.latitude))
            target.altitude_ft = max(1500, target.altitude_ft + int(target.vertical_rate_fpm * 0.002))

            dist, brg = haversine_distance_bearing_nm(self.home_lat, self.home_lon, target.latitude, target.longitude)
            target.distance_nm = round(dist, 1)
            target.bearing_deg = round(brg, 1)
            target.history.append((target.latitude, target.longitude))
            if len(target.history) > 12:
                target.history.pop(0)
            target.last_update_ts = now

    def mousePressEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            cx, cy = self.width() * 0.5, self.height() * 0.5
            radius_px = min(cx, cy) - 22.0
            px, py = event.position().x(), event.position().y()

            # Find closest aircraft target to click
            best_target: Optional[AircraftTarget] = None
            best_dist_px = 18.0

            for target in self.targets.values():
                tx, ty = self._coords_to_screen(target.latitude, target.longitude, cx, cy, radius_px)
                dist_px = math.hypot(px - tx, py - ty)
                if dist_px < best_dist_px:
                    best_dist_px = dist_px
                    best_target = target

            self.selected_target = best_target
            if best_target is not None:
                self.aircraft_selected.emit(best_target)
            self.update()

    def _coords_to_screen(self, lat: float, lon: float, cx: float, cy: float, radius_px: float) -> Tuple[float, float]:
        dist_nm, brg_deg = haversine_distance_bearing_nm(self.home_lat, self.home_lon, lat, lon)
        r = (dist_nm / self.max_range_nm) * radius_px
        theta = math.radians(brg_deg - 90.0)  # 0 deg is North (up)
        x = cx + r * math.cos(theta)
        y = cy + r * math.sin(theta)
        return x, y

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        w, h = self.width(), self.height()
        cx, cy = w * 0.5, h * 0.5
        radius_px = max(40.0, min(cx, cy) - 22.0)

        # 1. Background dark radar glass
        painter.fillRect(0, 0, w, h, QColor("#020617"))  # Deep slate navy

        # Subtle dark radar circle backing
        bg_grad = QRadialGradient(cx, cy, radius_px)
        bg_grad.setColorAt(0.0, QColor(6, 24, 38, 240))
        bg_grad.setColorAt(0.7, QColor(3, 14, 24, 240))
        bg_grad.setColorAt(1.0, QColor(1, 7, 13, 255))
        painter.setBrush(QBrush(bg_grad))
        painter.setPen(QPen(QColor("#0891b2"), 1.5))
        painter.drawEllipse(QPointF(cx, cy), radius_px, radius_px)

        # 2. Concentric Range Rings with NM Labels
        ring_pen = QPen(QColor(14, 116, 144, 110), 1.0, Qt.PenStyle.DashLine)
        label_font = QFont("Monospace", 8, QFont.Weight.Bold)
        painter.setFont(label_font)

        visible_rings = [r for r in self.ring_steps if r <= self.max_range_nm]
        if not visible_rings or visible_rings[-1] != self.max_range_nm:
            visible_rings.append(self.max_range_nm)

        for ring_nm in visible_rings:
            r_px = (ring_nm / self.max_range_nm) * radius_px
            if r_px > 5.0:
                painter.setPen(ring_pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawEllipse(QPointF(cx, cy), r_px, r_px)

                # Distance text on East horizontal spoke
                painter.setPen(QColor("#38bdf8"))
                painter.drawText(int(cx + r_px - 28), int(cy - 3), f"{ring_nm:.0f}NM")

        # 3. Compass Bearing Spokes (Every 30 degrees)
        spoke_pen = QPen(QColor(8, 145, 178, 65), 1.0)
        cardinal_font = QFont("SansSerif", 8, QFont.Weight.Bold)
        painter.setFont(cardinal_font)

        cardinals = {0: "N 000°", 90: "E 090°", 180: "S 180°", 270: "W 270°"}
        for deg in range(0, 360, 30):
            rad = math.radians(deg - 90.0)
            x1 = cx + (radius_px * 0.15) * math.cos(rad)
            y1 = cy + (radius_px * 0.15) * math.sin(rad)
            x2 = cx + radius_px * math.cos(rad)
            y2 = cy + radius_px * math.sin(rad)

            painter.setPen(spoke_pen)
            painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))

            # Draw Cardinal Indicators
            if deg in cardinals:
                tx = cx + (radius_px + 12.0) * math.cos(rad)
                ty = cy + (radius_px + 12.0) * math.sin(rad)
                painter.setPen(QColor("#22d3ee"))
                painter.drawText(QRectF(tx - 24, ty - 8, 48, 16), Qt.AlignmentFlag.AlignCenter, cardinals[deg])

        # 4. Animated Phosphor Radar Sweep Beam
        sweep_rad = math.radians(self.sweep_angle_deg - 90.0)
        sweep_x = cx + radius_px * math.cos(sweep_rad)
        sweep_y = cy + radius_px * math.sin(sweep_rad)

        sweep_pen = QPen(QColor(0, 240, 255, 180), 1.5)
        painter.setPen(sweep_pen)
        painter.drawLine(QPointF(cx, cy), QPointF(sweep_x, sweep_y))

        # Sweep sector gradient trail
        sweep_path = QPainterPath()
        sweep_path.moveTo(cx, cy)
        trail_rad = math.radians(self.sweep_angle_deg - 120.0)
        sweep_path.arcTo(QRectF(cx - radius_px, cy - radius_px, radius_px * 2, radius_px * 2), -(self.sweep_angle_deg - 90.0), 30.0)
        sweep_path.closeSubpath()
        painter.fillPath(sweep_path, QColor(0, 240, 255, 24))

        # 5. Draw Tactical Waypoints (Airports, Cheyenne Mountain, Penrose Fire, US-50)
        wp_font = QFont("SansSerif", 7, QFont.Weight.Bold)
        painter.setFont(wp_font)

        for wp in WAYPOINTS_LOCAL:
            dist, brg = haversine_distance_bearing_nm(self.home_lat, self.home_lon, wp.latitude, wp.longitude)
            if dist <= self.max_range_nm * 1.05:
                wx, wy = self._coords_to_screen(wp.latitude, wp.longitude, cx, cy, radius_px)

                # Icon marker
                painter.setPen(Qt.PenStyle.NoPen)
                if wp.code == "HOME":
                    painter.setBrush(QColor("#10b981"))  # Emerald Green for Penrose Home
                    painter.drawEllipse(QPointF(wx, wy), 5.0, 5.0)
                    painter.setPen(QColor("#34d399"))
                    painter.drawText(int(wx + 8), int(wy + 4), "📍 HOME (J & 13th)")
                elif wp.code == "1V6":
                    painter.setBrush(QColor("#f59e0b"))
                    painter.drawRect(QRectF(wx - 3.5, wy - 3.5, 7.0, 7.0))
                    painter.setPen(QColor("#fbbf24"))
                    painter.drawText(int(wx + 7), int(wy + 4), "✈ 1V6 CTAF 122.8")
                elif wp.waypoint_type == "TRANSMITTER":
                    painter.setBrush(QColor("#e11d48"))
                    painter.drawEllipse(QPointF(wx, wy), 4.0, 4.0)
                    painter.setPen(QColor("#f43f5e"))
                    painter.drawText(int(wx + 6), int(wy + 4), wp.code)
                else:
                    painter.setBrush(QColor("#0284c7"))
                    painter.drawRect(QRectF(wx - 3, wy - 3, 6, 6))
                    painter.setPen(QColor("#38bdf8"))
                    painter.drawText(int(wx + 6), int(wy + 4), wp.code)

        # 6. Draw Plotted Aircraft Targets
        plane_font = QFont("Monospace", 8, QFont.Weight.Bold)
        painter.setFont(plane_font)

        for target in self.targets.values():
            if target.distance_nm <= self.max_range_nm * 1.1:
                tx, ty = self._coords_to_screen(target.latitude, target.longitude, cx, cy, radius_px)

                # Breadcrumb trail
                if len(target.history) >= 2:
                    trail_pen = QPen(QColor(34, 211, 238, 90), 1.0, Qt.PenStyle.DotLine)
                    painter.setPen(trail_pen)
                    prev_pt: Optional[QPointF] = None
                    for h_lat, h_lon in target.history:
                        hx, hy = self._coords_to_screen(h_lat, h_lon, cx, cy, radius_px)
                        cur_pt = QPointF(hx, hy)
                        if prev_pt is not None:
                            painter.drawLine(prev_pt, cur_pt)
                        prev_pt = cur_pt

                # Aircraft symbol (Arrow / Delta oriented along heading)
                is_sel = self.selected_target and self.selected_target.icao == target.icao
                color = QColor("#00ffff") if is_sel else (QColor("#f59e0b") if target.callsign.startswith("FFL") else QColor("#38bdf8"))

                painter.save()
                painter.translate(tx, ty)
                painter.rotate(target.heading_deg)

                poly = QPolygonF([
                    QPointF(0, -9),
                    QPointF(6, 6),
                    QPointF(0, 3),
                    QPointF(-6, 6),
                ])
                painter.setBrush(QBrush(color))
                painter.setPen(QPen(QColor("#ffffff") if is_sel else QColor("#000000"), 1.0))
                painter.drawPolygon(poly)
                painter.restore()

                # Callout Label: Callsign, Altitude, Speed, Distance
                vr_sym = "▲" if target.vertical_rate_fpm > 200 else ("▼" if target.vertical_rate_fpm < -200 else "―")
                alt_str = f"FL{target.altitude_ft // 100}" if target.altitude_ft >= 18000 else f"{target.altitude_ft}ft"

                painter.setPen(QColor("#ffffff" if is_sel else "#e2e8f0"))
                painter.drawText(int(tx + 9), int(ty - 4), f"{target.callsign} ({target.aircraft_type})")
                painter.setPen(QColor("#38bdf8" if is_sel else "#94a3b8"))
                painter.drawText(int(tx + 9), int(ty + 7), f"{alt_str} {vr_sym} {target.ground_speed_kt}kt")
                painter.setPen(QColor("#a855f7" if is_sel else "#64748b"))
                painter.drawText(int(tx + 9), int(ty + 18), f"{target.distance_nm}NM @ {target.bearing_deg:.0f}°")

        # 7. Header Overlay info
        painter.setPen(QColor("#22d3ee"))
        painter.setFont(QFont("SansSerif", 9, QFont.Weight.Bold))
        painter.drawText(10, 18, f"RADAR RANGE: {self.max_range_nm:.0f} NM • TARGETS: {len(self.targets)} ACTIVE")
        painter.setPen(QColor("#94a3b8"))
        painter.setFont(QFont("SansSerif", 8))
        painter.drawText(10, 32, f"CENTER: Penrose, CO (J & 13th • {PENROSE_GRID}) • ADS-B 1090 MHz")


class TacticalRadarTelemetryWidget(QWidget):
    """
    Complete Tactical Radar, ADS-B Airspace Map, and Live Decoded Telemetry Panel.
    """
    tune_requested = pyqtSignal(str, float, str)  # (name, freq_hz, demod_mode)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("tacticalTelemetryPanel")

        self.messages: List[DecodedMessageRecord] = []
        self._build_ui()

        # Radar sweep animation timer (25 FPS)
        self._sweep_timer = QTimer(self)
        self._sweep_timer.timeout.connect(self._on_sweep_tick)
        self._sweep_timer.start(40)

        # Message stream ticker (adds simulated local dispatch / APRS / IoT events)
        self._msg_timer = QTimer(self)
        self._msg_timer.timeout.connect(self._on_msg_tick)
        self._msg_timer.start(3500)

        self._seed_initial_messages()

    def _build_ui(self) -> None:
        root_vbox = QVBoxLayout(self)
        root_vbox.setContentsMargins(6, 4, 6, 4)
        root_vbox.setSpacing(5)

        # 1. TOP QUICK DEMOD ACTION BAR
        action_strip = QFrame(self)
        action_strip.setObjectName("cardPanel")
        act_hbox = QHBoxLayout(action_strip)
        act_hbox.setContentsMargins(6, 4, 6, 4)
        act_hbox.setSpacing(5)

        lbl_quick = QLabel("⚡ QUICK TUNE:", action_strip)
        lbl_quick.setStyleSheet("color: #38bdf8; font-weight: bold; font-size: 10px;")
        act_hbox.addWidget(lbl_quick)

        quick_buttons = [
            ("🚛 CB 19 Trucker (27.185 AM)", "CB Ch 19 Trucker Highway Calling", 27_185_000.0, "AM"),
            ("🚒 Penrose Fire (155.880 NFM)", "Penrose Fire Dept Pagers / Ops", 155_880_000.0, "Narrow FM"),
            ("🚒 Florence/Penrose (154.400 NFM)", "Florence / Penrose Fire Dispatch", 154_400_000.0, "Narrow FM"),
            ("🚓 Fremont Sheriff (154.845 NFM)", "Fremont County Sheriff VHF Backup", 154_845_000.0, "Narrow FM"),
            ("🏔️ CO SAR MRA-1 (155.160 NFM)", "Colorado Search & Rescue MRA-1", 155_160_000.0, "Narrow FM"),
            ("🚆 UP Rail AAR 92 (161.490 NFM)", "Union Pacific Tennessee Pass AAR 92", 161_490_000.0, "Narrow FM"),
            ("✈️ 1V6 Penrose CTAF (122.800 AM)", "Fremont County Airport 1V6 CTAF", 122_800_000.0, "AM"),
            ("📺 KOAA NBC TV (Ch 25)", "KOAA NBC TV Ch 25 (539.0 MHz)", 539_000_000.0, "Raw I/Q"),
        ]

        for title, name, freq_hz, demod in quick_buttons:
            btn = QPushButton(title, action_strip)
            btn.setObjectName("btnTuneRow")
            btn.clicked.connect(lambda _, n=name, f=freq_hz, d=demod: self.tune_requested.emit(n, f, d))
            act_hbox.addWidget(btn)

        act_hbox.addStretch()
        root_vbox.addWidget(action_strip)

        # 2. MAIN SPLITTER: Left = Tactical Radar Canvas, Right = Live Telemetry & Pager Feed
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.setHandleWidth(4)

        # Left: Radar & Airspace Map Box
        radar_box = QFrame(splitter)
        radar_box.setObjectName("cardPanel")
        radar_vbox = QVBoxLayout(radar_box)
        radar_vbox.setContentsMargins(6, 4, 6, 4)
        radar_vbox.setSpacing(4)

        # Radar Toolbar
        radar_ctrl_bar = QHBoxLayout()
        radar_ctrl_bar.setSpacing(4)
        lbl_radar_title = QLabel("📡 ADS-B TACTICAL AIRSPACE RADAR (1090 MHz)", radar_box)
        lbl_radar_title.setStyleSheet("color: #00f0ff; font-weight: bold; font-size: 11px;")
        radar_ctrl_bar.addWidget(lbl_radar_title)
        radar_ctrl_bar.addStretch()

        self.btn_zoom_in = QPushButton("🔍+ Zoom In", radar_box)
        self.btn_zoom_out = QPushButton("🔍- Zoom Out", radar_box)
        self.btn_radar_reset = QPushButton("⟲ Reset Penrose", radar_box)

        for b in (self.btn_zoom_in, self.btn_zoom_out, self.btn_radar_reset):
            b.setMinimumHeight(22)
            radar_ctrl_bar.addWidget(b)

        self.btn_zoom_in.clicked.connect(self._on_zoom_in)
        self.btn_zoom_out.clicked.connect(self._on_zoom_out)
        self.btn_radar_reset.clicked.connect(self._on_reset_view)
        radar_vbox.addLayout(radar_ctrl_bar)

        self.radar_canvas = ADSBRadarMapCanvas(radar_box)
        self.radar_canvas.aircraft_selected.connect(self._on_aircraft_selected)
        radar_vbox.addWidget(self.radar_canvas, 1)

        # Target Telemetry Status Callout Bar
        self.lbl_target_detail = QLabel(
            "Selected Target: Click any aircraft symbol to view real-time vector, squawk & route telemetry.",
            radar_box,
        )
        self.lbl_target_detail.setStyleSheet(
            "background-color: #04101e; border: 1px solid #0284c7; border-radius: 4px; "
            "color: #bae6fd; padding: 4px 8px; font-family: monospace; font-size: 10px;"
        )
        radar_vbox.addWidget(self.lbl_target_detail)
        splitter.addWidget(radar_box)

        # Right: Live POCSAG Pager, APRS & IoT Telemetry Feed Box
        telem_box = QFrame(splitter)
        telem_box.setObjectName("cardPanel")
        telem_vbox = QVBoxLayout(telem_box)
        telem_vbox.setContentsMargins(6, 4, 6, 4)
        telem_vbox.setSpacing(4)

        telem_hdr = QHBoxLayout()
        lbl_telem_title = QLabel("📟 LIVE DECODED TELEMETRY & DISPATCH FEED", telem_box)
        lbl_telem_title.setStyleSheet("color: #10b981; font-weight: bold; font-size: 11px;")
        telem_hdr.addWidget(lbl_telem_title)
        telem_hdr.addStretch()

        self.btn_clear_telem = QPushButton("✖ Clear Feed", telem_box)
        self.btn_clear_telem.setMinimumHeight(22)
        self.btn_clear_telem.clicked.connect(self._clear_messages)
        telem_hdr.addWidget(self.btn_clear_telem)
        telem_vbox.addLayout(telem_hdr)

        self.table_telem = QTableWidget(telem_box)
        self.table_telem.setColumnCount(4)
        self.table_telem.setHorizontalHeaderLabels(["Time", "Service / Protocol", "Freq (MHz)", "Decoded Message Payload / Telemetry"])
        self.table_telem.setAlternatingRowColors(True)
        self.table_telem.verticalHeader().setVisible(False)
        self.table_telem.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table_telem.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)

        th = self.table_telem.horizontalHeader()
        th.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        th.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        th.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        th.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)

        telem_vbox.addWidget(self.table_telem, 1)
        splitter.addWidget(telem_box)

        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        root_vbox.addWidget(splitter, 1)

    def _on_zoom_in(self) -> None:
        self.radar_canvas.zoom_in()

    def _on_zoom_out(self) -> None:
        self.radar_canvas.zoom_out()

    def _on_reset_view(self) -> None:
        self.radar_canvas.reset_view()

    def _on_sweep_tick(self) -> None:
        self.radar_canvas.update_sweep_angle(3.0)

    def _on_aircraft_selected(self, target: AircraftTarget) -> None:
        self.lbl_target_detail.setText(
            f"TARGET {target.callsign} ({target.icao}) • TYPE: {target.aircraft_type} • "
            f"ALT: {target.altitude_ft:,} ft • SPEED: {target.ground_speed_kt} kt • "
            f"HDG: {target.heading_deg:.0f}° • SQUAWK: {target.squawk} • "
            f"RANGE: {target.distance_nm} NM @ {target.bearing_deg:.0f}° FROM PENROSE BASE"
        )

    def _seed_initial_messages(self) -> None:
        now_str = time.strftime("%H:%M:%S")
        self.add_message(now_str, "POCSAG", 155.880, "PENROSE_FIRE", "[DISPATCH] PENROSE FIRE / FLORENCE FIRE: STANDBY REPORT AT CORNER OF J ST & 13TH", "#ef4444")
        self.add_message(now_str, "APRS", 144.390, "W0PEN-1", "WX STATION: PENROSE ELEV 5300FT • 62.4°F • BARO 30.12in • WIND SW 6MPH", "#38bdf8")
        self.add_message(now_str, "POCSAG", 155.340, "CHERN_MED9", "ST. THOMAS MORE CAÑON CITY: HOSPITAL STATUS NORMAL • MEDEVAC HELIPAD OPEN", "#ec4899")
        self.add_message(now_str, "ISM_433", 433.920, "AURIOL-TH", "LOCAL WEATHER SENSOR: CH1 TEMP 18.2C HUMIDITY 38% BATT OK", "#eab308")
        self.add_message(now_str, "ATSC_TV", 539.000, "KOAA_NBC", "ATSC 1.0 PILOT CARRIER ACQUIRED: RF CH 25 (VIRTUAL 5.1 KOAA-HD PUEBLO/SPRINGS)", "#e11d48")

    def _on_msg_tick(self) -> None:
        now_str = time.strftime("%H:%M:%S")
        pool = [
            ("POCSAG", 155.880, "PENROSE_FIRE", "PAGE: PENROSE FIRE DISTRICT DISPATCH - MEDICAL AID RUN ON HIGHWAY 50 MP 285", "#ef4444"),
            ("APRS", 144.390, "K0FRE-9", "FREMONT CO ARES MOBILE DIGIPEATER ACTIVE (DM78vj)", "#38bdf8"),
            ("ISM_433", 433.920, "ACURITE-6045", "SOIL MOISTURE / RAIN SENSOR 0.00 IN / HR", "#eab308"),
            ("POCSAG", 154.400, "FLORENCE_FIRE", "PAGING: ENGINE 1 & MED-4 EN ROUTE TO COUNTY ROAD 123", "#ef4444"),
            ("APRS", 144.390, "N0PUB-3", "PUEBLO EMERGENCY COMMUNICATIONS NET: STATUS ALL-CLEAR", "#38bdf8"),
            ("ISM_915", 915.000, "LORA_MESH", "MESHTASTIC NODE [Penrose-Deck]: 18 PEERS DISCOVERED ON CANAL 0", "#a855f7"),
        ]
        chosen = random.choice(pool)
        self.add_message(now_str, chosen[0], chosen[1], chosen[2], chosen[3], chosen[4])

    def add_message(self, ts: str, ch_type: str, freq_mhz: float, source: str, content: str, color_hex: str) -> None:
        rec = DecodedMessageRecord(ts, ch_type, freq_mhz, source, content, color_hex)
        self.messages.insert(0, rec)
        if len(self.messages) > 100:
            self.messages.pop()

        self.table_telem.insertRow(0)

        t_item = QTableWidgetItem(ts)
        t_item.setFont(QFont("Monospace", 8))
        self.table_telem.setItem(0, 0, t_item)

        srv_item = QTableWidgetItem(f"[{ch_type}] {source}")
        srv_item.setFont(QFont("SansSerif", 8, QFont.Weight.Bold))
        srv_item.setForeground(QColor(color_hex))
        self.table_telem.setItem(0, 1, srv_item)

        f_item = QTableWidgetItem(f"{freq_mhz:.3f}")
        f_item.setFont(QFont("Monospace", 8, QFont.Weight.Bold))
        f_item.setForeground(QColor("#fbbf24"))
        self.table_telem.setItem(0, 2, f_item)

        c_item = QTableWidgetItem(content)
        c_item.setFont(QFont("Monospace", 8))
        c_item.setForeground(QColor("#e2e8f0"))
        self.table_telem.setItem(0, 3, c_item)

    def _clear_messages(self) -> None:
        self.messages.clear()
        self.table_telem.setRowCount(0)
