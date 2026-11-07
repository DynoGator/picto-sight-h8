"""
Tactical Radar, Live 1090 MHz ADS-B Surveillance & Telemetry Decoder Panel
==========================================================================
Renders:
  1. ADS-B 1090 MHz Tactical Radar & Airspace Surveillance Map
     - Centered at Home Base: J St & 13th St, Penrose, CO 81240 (38.4112° N, -105.0366° W, DM78vj)
     - Concentric range rings (15, 25, 50, 100, 150 NM) + 360° radial bearing lines
     - Local landmarks & transmitters: Fremont Co Airport (1V6), Pueblo (KPUB),
       Colorado Springs (KCOS), Cheyenne Mtn TV/FM Towers, Royal Gorge UP Rail, US-50 CB Corridor
     - Real-time HackRF 2.4 MSPS -> readsb Mode-S / ADS-B live aircraft plotting (plus optional
       explicitly-labeled [SIM DEMO] mode)
  2. Thread-Safe Live Telemetry Decoders (Qt pyqtSignal marshaled):
     - POCSAG / FLEX Pager Dispatch Decoder (multimon-ng)
     - APRS AX.25 144.390 MHz Packet Decoder (multimon-ng)
     - ISM 433.92 / 915 MHz Sensor Decoder (rtl_433)
     - Broadcast TV VHF/UHF Channel & 8VSB Pilot Scanner + Live TV Receiver launcher
  3. Zero-overflow layout tailored for 5" 800x480 DSI + scalable 1080p HDMI Studio mode,
     including 1-tap [Split | Radar | Feed] view switching.
"""

from __future__ import annotations

import json
import math
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer, pyqtSignal
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
    QSizePolicy,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.adsb_receiver import get_adsb_receiver

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
    TacticalWaypoint("CHEY", "Cheyenne Mtn TV/FM Towers", 38.7428, -104.8828, "TRANSMITTER", 539.000, "ATSC TV Ch 19,22,24,25 / FM", "⛰️"),
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
    has_position: bool = True
    history: List[Tuple[float, float]] = field(default_factory=list)
    last_update_ts: float = 0.0


@dataclass
class DecodedMessageRecord:
    timestamp_str: str
    channel_type: str   # "ADSB", "POCSAG", "APRS", "ISM_433", "ATSC_TV", "SYSTEM"
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
        self.setMinimumSize(210, 175)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)

        self.home_lat = PENROSE_HOME_LAT
        self.home_lon = PENROSE_HOME_LON
        self.max_range_nm = 50.0
        self.sweep_angle_deg = 0.0
        self.selected_target: Optional[AircraftTarget] = None
        self.targets: Dict[str, AircraftTarget] = {}
        self.mode_badge: str = "STANDBY"
        self.mode_s_msgs: int = 0
        self.sim_enabled: bool = False

        self.ring_steps = [10.0, 25.0, 50.0, 75.0, 100.0, 150.0]

    def set_home_coords(self, lat: float, lon: float) -> None:
        self.home_lat = lat
        self.home_lon = lon
        self.update()

    def set_simulation_mode(self, enabled: bool) -> None:
        self.sim_enabled = enabled
        self.targets.clear()
        self.selected_target = None
        if enabled:
            self.mode_badge = "SIM DEMO"
            self._init_mock_targets()
        else:
            self.mode_badge = "STANDBY"
        self.update()

    def update_live_targets(self, parsed_list: List[Dict[str, Any]], total_msgs: int) -> None:
        self.mode_badge = "LIVE 1090 RF"
        self.mode_s_msgs = total_msgs
        fresh_icaos = set()
        for d in parsed_list:
            icao = d["icao"]
            fresh_icaos.add(icao)
            prev = self.targets.get(icao)
            hist = list(prev.history) if prev else []
            if d.get("has_position", False):
                pt = (float(d["latitude"]), float(d["longitude"]))
                if not hist or math.hypot(hist[-1][0] - pt[0], hist[-1][1] - pt[1]) > 0.0005:
                    hist.append(pt)
                    if len(hist) > 18:
                        hist.pop(0)
            self.targets[icao] = AircraftTarget(
                icao=icao,
                callsign=d["callsign"],
                latitude=float(d["latitude"]),
                longitude=float(d["longitude"]),
                altitude_ft=int(d["altitude_ft"]),
                ground_speed_kt=int(d["ground_speed_kt"]),
                heading_deg=float(d["heading_deg"]),
                vertical_rate_fpm=int(d["vertical_rate_fpm"]),
                squawk=str(d["squawk"]),
                aircraft_type=str(d["aircraft_type"]),
                distance_nm=float(d["distance_nm"]),
                bearing_deg=float(d["bearing_deg"]),
                has_position=bool(d.get("has_position", True)),
                history=hist,
                last_update_ts=float(d.get("updated_ts", time.time())),
            )

        # Prune stale targets not seen in latest readsb window
        for k in list(self.targets.keys()):
            if k not in fresh_icaos:
                del self.targets[k]
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
        if self.sim_enabled:
            self._simulate_step()
        self.update()

    def _init_mock_targets(self) -> None:
        now = time.time()
        seeds = [
            ("A14B20", "UAL1422", 38.6500, -104.8200, 28000, 440, 345.0, 1200, "1200", "B738"),
            ("A89C33", "SWA291", 38.3200, -104.9100, 14500, 310, 195.0, -800, "2411", "B737"),
            ("AB4102", "FFL3", 38.4500, -105.1500, 7800, 135, 80.0, 0, "1200", "EC35"),
            ("A32F19", "N1V6CO", 38.4180, -105.0800, 6200, 110, 95.0, 250, "1200", "C172"),
            ("A091FE", "SKW4811", 38.7800, -104.6500, 8900, 210, 350.0, -1100, "3520", "CRJ7"),
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
                has_position=True,
                history=[(lat, lon)],
                last_update_ts=now,
            )

    def _simulate_step(self) -> None:
        now = time.time()
        for target in self.targets.values():
            spd_deg_s = (target.ground_speed_kt / 3600.0) / 60.0
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
            radius_px = min(cx, cy) - 20.0
            px, py = event.position().x(), event.position().y()

            best_target: Optional[AircraftTarget] = None
            best_dist_px = 18.0

            for target in self.targets.values():
                if not target.has_position:
                    continue
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
        theta = math.radians(brg_deg - 90.0)
        x = cx + r * math.cos(theta)
        y = cy + r * math.sin(theta)
        return x, y

    def paintEvent(self, _event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        w, h = self.width(), self.height()
        cx, cy = w * 0.5, h * 0.5
        radius_px = max(35.0, min(cx, cy) - 24.0)

        painter.fillRect(0, 0, w, h, QColor("#020617"))

        bg_grad = QRadialGradient(cx, cy, radius_px)
        bg_grad.setColorAt(0.0, QColor(6, 24, 38, 240))
        bg_grad.setColorAt(0.7, QColor(3, 14, 24, 240))
        bg_grad.setColorAt(1.0, QColor(1, 7, 13, 255))
        painter.setBrush(QBrush(bg_grad))
        painter.setPen(QPen(QColor("#0891b2"), 1.4))
        painter.drawEllipse(QPointF(cx, cy), radius_px, radius_px)

        # Range rings (diagonal -45 deg labels so East axis stays uncluttered)
        ring_pen = QPen(QColor(14, 116, 144, 110), 1.0, Qt.PenStyle.DashLine)
        painter.setFont(QFont("Monospace", 7, QFont.Weight.Bold))

        visible_rings = [r for r in self.ring_steps if r <= self.max_range_nm]
        if not visible_rings or visible_rings[-1] != self.max_range_nm:
            visible_rings.append(self.max_range_nm)

        diag = 0.70710678
        for ring_nm in visible_rings:
            r_px = (ring_nm / self.max_range_nm) * radius_px
            if r_px > 5.0:
                painter.setPen(ring_pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawEllipse(QPointF(cx, cy), r_px, r_px)
                if r_px >= 24.0:
                    painter.setPen(QColor("#38bdf8"))
                    lx = int(cx + r_px * diag - 14)
                    ly = int(cy - r_px * diag + 10)
                    painter.drawText(lx, ly, f"{ring_nm:.0f}NM")

        # Compass spokes
        spoke_pen = QPen(QColor(8, 145, 178, 65), 1.0)
        painter.setFont(QFont("SansSerif", 7, QFont.Weight.Bold))
        cardinals = {0: "N", 90: "E", 180: "S", 270: "W"}
        for deg in range(0, 360, 30):
            rad = math.radians(deg - 90.0)
            x1 = cx + (radius_px * 0.15) * math.cos(rad)
            y1 = cy + (radius_px * 0.15) * math.sin(rad)
            x2 = cx + radius_px * math.cos(rad)
            y2 = cy + radius_px * math.sin(rad)
            painter.setPen(spoke_pen)
            painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))
            if deg in cardinals:
                tx = cx + (radius_px + 10.0) * math.cos(rad)
                ty = cy + (radius_px + 10.0) * math.sin(rad)
                painter.setPen(QColor("#22d3ee"))
                painter.drawText(QRectF(tx - 12, ty - 8, 24, 16), Qt.AlignmentFlag.AlignCenter, cardinals[deg])

        # Sweep beam when active
        if self.mode_badge in ("LIVE 1090 RF", "SIM DEMO"):
            sweep_rad = math.radians(self.sweep_angle_deg - 90.0)
            sweep_x = cx + radius_px * math.cos(sweep_rad)
            sweep_y = cy + radius_px * math.sin(sweep_rad)
            sweep_col = QColor(16, 185, 129, 190) if self.mode_badge == "LIVE 1090 RF" else QColor(245, 158, 11, 170)
            painter.setPen(QPen(sweep_col, 1.5))
            painter.drawLine(QPointF(cx, cy), QPointF(sweep_x, sweep_y))
            sweep_path = QPainterPath()
            sweep_path.moveTo(cx, cy)
            sweep_path.arcTo(
                QRectF(cx - radius_px, cy - radius_px, radius_px * 2, radius_px * 2),
                -(self.sweep_angle_deg - 90.0),
                28.0,
            )
            sweep_path.closeSubpath()
            fill_col = QColor(16, 185, 129, 24) if self.mode_badge == "LIVE 1090 RF" else QColor(245, 158, 11, 22)
            painter.fillPath(sweep_path, fill_col)

        # Waypoints (suppress overlapping near-origin text labels at wide zoom)
        painter.setFont(QFont("SansSerif", 7, QFont.Weight.Bold))
        for wp in WAYPOINTS_LOCAL:
            dist, _ = haversine_distance_bearing_nm(self.home_lat, self.home_lon, wp.latitude, wp.longitude)
            if dist <= self.max_range_nm * 1.05:
                wx, wy = self._coords_to_screen(wp.latitude, wp.longitude, cx, cy, radius_px)
                px_from_home = math.hypot(wx - cx, wy - cy)
                painter.setPen(Qt.PenStyle.NoPen)
                if wp.code == "HOME":
                    painter.setBrush(QColor("#10b981"))
                    painter.drawEllipse(QPointF(wx, wy), 4.5, 4.5)
                    painter.setPen(QColor("#34d399"))
                    painter.drawText(int(wx - 14), int(wy + 12), "HOME")
                elif wp.code == "1V6":
                    painter.setBrush(QColor("#f59e0b"))
                    painter.drawRect(QRectF(wx - 3.0, wy - 3.0, 6.0, 6.0))
                    if px_from_home >= 22.0:
                        painter.setPen(QColor("#fbbf24"))
                        painter.drawText(int(wx + 5), int(wy + 3), "1V6")
                elif wp.waypoint_type == "TRANSMITTER":
                    painter.setBrush(QColor("#e11d48"))
                    painter.drawEllipse(QPointF(wx, wy), 3.5, 3.5)
                    if px_from_home >= 22.0:
                        painter.setPen(QColor("#f43f5e"))
                        painter.drawText(int(wx + 5), int(wy + 3), wp.code)
                else:
                    painter.setBrush(QColor("#0284c7"))
                    painter.drawRect(QRectF(wx - 2.5, wy - 2.5, 5.0, 5.0))
                    if px_from_home >= 22.0:
                        painter.setPen(QColor("#38bdf8"))
                        painter.drawText(int(wx + 5), int(wy + 3), wp.code)

        # Plotted Aircraft
        painter.setFont(QFont("Monospace", 7, QFont.Weight.Bold))
        pos_targets = [t for t in self.targets.values() if t.has_position]
        for target in pos_targets:
            if target.distance_nm <= self.max_range_nm * 1.1:
                tx, ty = self._coords_to_screen(target.latitude, target.longitude, cx, cy, radius_px)
                if len(target.history) >= 2:
                    painter.setPen(QPen(QColor(34, 211, 238, 90), 1.0, Qt.PenStyle.DotLine))
                    prev_pt: Optional[QPointF] = None
                    for h_lat, h_lon in target.history:
                        hx, hy = self._coords_to_screen(h_lat, h_lon, cx, cy, radius_px)
                        cur_pt = QPointF(hx, hy)
                        if prev_pt is not None:
                            painter.drawLine(prev_pt, cur_pt)
                        prev_pt = cur_pt

                is_sel = self.selected_target and self.selected_target.icao == target.icao
                color = QColor("#00ffff") if is_sel else (
                    QColor("#10b981") if self.mode_badge == "LIVE 1090 RF" else QColor("#f59e0b")
                )
                painter.save()
                painter.translate(tx, ty)
                painter.rotate(target.heading_deg)
                poly = QPolygonF([QPointF(0, -8), QPointF(5, 5), QPointF(0, 2.5), QPointF(-5, 5)])
                painter.setBrush(QBrush(color))
                painter.setPen(QPen(QColor("#ffffff") if is_sel else QColor("#000000"), 1.0))
                painter.drawPolygon(poly)
                painter.restore()

                alt_str = f"FL{target.altitude_ft // 100}" if target.altitude_ft >= 18000 else f"{target.altitude_ft}ft"
                painter.setPen(QColor("#ffffff" if is_sel else "#e2e8f0"))
                painter.drawText(int(tx + 8), int(ty - 2), f"{target.callsign}")
                painter.setPen(QColor("#38bdf8" if is_sel else "#94a3b8"))
                painter.drawText(int(tx + 8), int(ty + 8), f"{alt_str} {target.ground_speed_kt}kt")

        # Top-left concise status overlay
        badge_col = "#10b981" if self.mode_badge == "LIVE 1090 RF" else (
            "#f59e0b" if self.mode_badge == "SIM DEMO" else "#94a3b8"
        )
        painter.setPen(QColor(badge_col))
        painter.setFont(QFont("SansSerif", 8, QFont.Weight.Bold))
        extra = f" • {self.mode_s_msgs} DF17" if self.mode_badge == "LIVE 1090 RF" else ""
        painter.drawText(
            6, 14,
            f"[{self.mode_badge}] {self.max_range_nm:.0f}NM • {len(self.targets)} ACFT{extra}",
        )
        painter.setPen(QColor("#64748b"))
        painter.setFont(QFont("SansSerif", 7))
        painter.drawText(6, 26, f"Penrose CO ({PENROSE_GRID})")
        painter.end()


class TacticalRadarTelemetryWidget(QWidget):
    """
    Complete Tactical Radar, Live 1090 MHz ADS-B Map, Broadcast TV Launcher,
    and Thread-Safe Decoded Telemetry Panel.
    """
    tune_requested = pyqtSignal(str, float, str)  # (name, freq_hz, demod_mode)
    tv_demod_requested = pyqtSignal(float)        # center_freq_hz -> opens BroadcastTVReceiverDialog
    _bg_msg_sig = pyqtSignal(str, str, float, str, str, str)
    _bg_done_sig = pyqtSignal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("tacticalTelemetryPanel")

        self.messages: List[DecodedMessageRecord] = []
        self._adsb_rx = get_adsb_receiver()
        self._seen_live_icaos: set[str] = set()
        self._sim_mode = False

        self._bg_msg_sig.connect(self.add_message)
        self._bg_done_sig.connect(self._on_bg_worker_done)

        self._build_ui()

        # Radar sweep animation timer (20 FPS)
        self._sweep_timer = QTimer(self)
        self._sweep_timer.timeout.connect(self._on_sweep_tick)
        self._sweep_timer.start(50)

        # Live ADS-B readsb JSON poller (1 Hz on Qt main thread)
        self._adsb_poll_timer = QTimer(self)
        self._adsb_poll_timer.timeout.connect(self._poll_live_adsb)
        self._adsb_poll_timer.start(1000)

        # Optional Simulation Demo message ticker (only runs when SIM DEMO is explicitly enabled!)
        self._sim_msg_timer = QTimer(self)
        self._sim_msg_timer.timeout.connect(self._on_sim_msg_tick)

        self._seed_initial_status()

    def _build_ui(self) -> None:
        root_vbox = QVBoxLayout(self)
        root_vbox.setContentsMargins(4, 3, 4, 3)
        root_vbox.setSpacing(4)

        # 1. COMPACT ZERO-OVERFLOW QUICK TUNE & VIEW SWITCHER BAR (< 780px total!)
        action_strip = QFrame(self)
        action_strip.setObjectName("cardPanel")
        act_hbox = QHBoxLayout(action_strip)
        act_hbox.setContentsMargins(4, 3, 4, 3)
        act_hbox.setSpacing(3)

        compact_btn_qss = (
            "QPushButton { background-color: #162235; color: #e2e8f0; border: 1px solid #2b4263; "
            "border-radius: 4px; padding: 2px 5px; font-size: 10px; font-weight: bold; min-height: 22px; }"
            "QPushButton:hover { background-color: #0284c7; color: #ffffff; }"
        )

        quick_buttons = [
            ("🚛 CB19", "CB Ch 19 Trucker (27.185 MHz)", 27_185_000.0, "AM"),
            ("🚒 Fire", "Penrose Fire Ops (155.880 MHz)", 155_880_000.0, "Narrow FM"),
            ("🚓 Sheriff", "Fremont Sheriff (154.845 MHz)", 154_845_000.0, "Narrow FM"),
            ("🏔️ SAR", "CO Search & Rescue MRA-1 (155.160 MHz)", 155_160_000.0, "Narrow FM"),
            ("🚆 Rail", "Union Pacific AAR 92 (161.490 MHz)", 161_490_000.0, "Narrow FM"),
            ("✈️ 1V6", "Penrose 1V6 Airport CTAF (122.800 MHz)", 122_800_000.0, "AM"),
        ]

        for title, name, freq_hz, demod in quick_buttons:
            btn = QPushButton(title, action_strip)
            btn.setToolTip(f"Instant Tune in Gqrx: {name}")
            btn.setStyleSheet(compact_btn_qss)
            btn.clicked.connect(lambda _c=False, n=name, f=freq_hz, d=demod: self.tune_requested.emit(n, f, d))
            act_hbox.addWidget(btn)

        self.btn_quick_tv = QPushButton("📺 Ch25 TV", action_strip)
        self.btn_quick_tv.setToolTip("Open Live Broadcast TV Demodulator on KOAA NBC Ch 25 (539.0 MHz)")
        self.btn_quick_tv.setStyleSheet(
            "QPushButton { background-color: #4c0519; color: #fecdd3; border: 1px solid #e11d48; "
            "border-radius: 4px; padding: 2px 6px; font-size: 10px; font-weight: bold; min-height: 22px; }"
            "QPushButton:hover { background-color: #be123c; color: #ffffff; }"
        )
        self.btn_quick_tv.clicked.connect(lambda: self.tv_demod_requested.emit(539_000_000.0))
        act_hbox.addWidget(self.btn_quick_tv)

        act_hbox.addStretch(1)

        # 5" DSI View Mode Switcher: [Split] [Radar] [Feed]
        for label, mode_idx in (("Split", 0), ("Radar", 1), ("Feed", 2)):
            vb = QPushButton(label, action_strip)
            vb.setStyleSheet(
                "QPushButton { background-color: #0f172a; color: #38bdf8; border: 1px solid #1e3a5f; "
                "border-radius: 3px; padding: 2px 5px; font-size: 9px; font-weight: bold; min-height: 20px; }"
                "QPushButton:hover { background-color: #1e293b; }"
            )
            vb.clicked.connect(lambda _c=False, m=mode_idx: self.set_panel_view_mode(m))
            act_hbox.addWidget(vb)

        root_vbox.addWidget(action_strip)

        # 2. MAIN SPLITTER: Left = Tactical Radar Canvas, Right = Live Telemetry & Pager Feed
        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.setHandleWidth(4)
        self.splitter.setChildrenCollapsible(True)

        # Left: Radar & Airspace Map Box
        self.radar_box = QFrame(self.splitter)
        self.radar_box.setObjectName("cardPanel")
        radar_vbox = QVBoxLayout(self.radar_box)
        radar_vbox.setContentsMargins(4, 3, 4, 3)
        radar_vbox.setSpacing(3)

        radar_ctrl_bar = QHBoxLayout()
        radar_ctrl_bar.setSpacing(3)

        self.btn_live_adsb = QPushButton("📡 LIVE ADS-B", self.radar_box)
        self.btn_live_adsb.setCheckable(True)
        self.btn_live_adsb.setToolTip("Stream real 1090 MHz Mode-S / ADS-B aircraft from HackRF One via readsb")
        self.btn_live_adsb.setStyleSheet(
            "QPushButton { background-color: #082f49; color: #38bdf8; border: 1px solid #0284c7; "
            "border-radius: 4px; padding: 2px 6px; font-size: 10px; font-weight: bold; min-height: 22px; }"
            "QPushButton:checked { background-color: #059669; color: #ffffff; border-color: #34d399; }"
        )
        self.btn_live_adsb.clicked.connect(self._on_toggle_live_adsb)
        radar_ctrl_bar.addWidget(self.btn_live_adsb)

        self.btn_sim_demo = QPushButton("SIM", self.radar_box)
        self.btn_sim_demo.setCheckable(True)
        self.btn_sim_demo.setToolTip("Toggle synthetic demo aircraft & telemetry packets for offline UI preview")
        self.btn_sim_demo.setStyleSheet(
            "QPushButton { background-color: #1e293b; color: #94a3b8; border: 1px solid #334155; "
            "border-radius: 4px; padding: 2px 5px; font-size: 10px; font-weight: bold; min-height: 22px; }"
            "QPushButton:checked { background-color: #78350f; color: #fde68a; border-color: #f59e0b; }"
        )
        self.btn_sim_demo.clicked.connect(self._on_toggle_sim_demo)
        radar_ctrl_bar.addWidget(self.btn_sim_demo)

        radar_ctrl_bar.addStretch(1)

        self.btn_zoom_in = QPushButton("🔍+", self.radar_box)
        self.btn_zoom_out = QPushButton("🔍-", self.radar_box)
        self.btn_radar_reset = QPushButton("⟲", self.radar_box)

        for b in (self.btn_zoom_in, self.btn_zoom_out, self.btn_radar_reset):
            b.setStyleSheet(compact_btn_qss)
            radar_ctrl_bar.addWidget(b)

        self.btn_zoom_in.clicked.connect(self._on_zoom_in)
        self.btn_zoom_out.clicked.connect(self._on_zoom_out)
        self.btn_radar_reset.clicked.connect(self._on_reset_view)
        radar_vbox.addLayout(radar_ctrl_bar)

        self.radar_canvas = ADSBRadarMapCanvas(self.radar_box)
        self.radar_canvas.aircraft_selected.connect(self._on_aircraft_selected)
        radar_vbox.addWidget(self.radar_canvas, 1)

        self.lbl_target_detail = QLabel(
            "Tap [📡 LIVE ADS-B] for real 1090 MHz aircraft tracking, or tap an aircraft icon for vector details.",
            self.radar_box,
        )
        self.lbl_target_detail.setWordWrap(True)
        self.lbl_target_detail.setStyleSheet(
            "background-color: #04101e; border: 1px solid #0284c7; border-radius: 4px; "
            "color: #bae6fd; padding: 2px 5px; font-family: monospace; font-size: 9px;"
        )
        radar_vbox.addWidget(self.lbl_target_detail)
        self.splitter.addWidget(self.radar_box)

        # Right: Live POCSAG Pager, APRS, ISM & Broadcast TV Telemetry Feed Box
        self.telem_box = QFrame(self.splitter)
        self.telem_box.setObjectName("cardPanel")
        telem_vbox = QVBoxLayout(self.telem_box)
        telem_vbox.setContentsMargins(4, 3, 4, 3)
        telem_vbox.setSpacing(3)

        telem_hdr = QHBoxLayout()
        telem_hdr.setSpacing(3)

        self.btn_live_pager = QPushButton("📟 Pager", self.telem_box)
        self.btn_live_pager.setToolTip("Capture 3s on 155.880 MHz & decode POCSAG/FLEX via multimon-ng")
        self.btn_live_pager.setStyleSheet(compact_btn_qss)
        self.btn_live_pager.clicked.connect(self._on_live_pager_clicked)
        telem_hdr.addWidget(self.btn_live_pager)

        self.btn_live_aprs = QPushButton("📍 APRS", self.telem_box)
        self.btn_live_aprs.setToolTip("Capture 4s on 144.390 MHz & decode AX.25 APRS packets via multimon-ng")
        self.btn_live_aprs.setStyleSheet(compact_btn_qss)
        self.btn_live_aprs.clicked.connect(self._on_live_aprs_clicked)
        telem_hdr.addWidget(self.btn_live_aprs)

        self.btn_live_sensors = QPushButton("📊 433M", self.telem_box)
        self.btn_live_sensors.setToolTip("Capture 3s on 433.920 MHz & decode IoT/weather sensors via rtl_433")
        self.btn_live_sensors.setStyleSheet(compact_btn_qss)
        self.btn_live_sensors.clicked.connect(self._on_live_sensors_clicked)
        telem_hdr.addWidget(self.btn_live_sensors)

        self.btn_live_tv = QPushButton("📺 TV RX", self.telem_box)
        self.btn_live_tv.setToolTip("Open Live ATSC 8VSB + Analog NTSC Television Receiver Dialog")
        self.btn_live_tv.setStyleSheet(
            "QPushButton { background-color: #4c0519; color: #fecdd3; border: 1px solid #e11d48; "
            "border-radius: 4px; padding: 2px 5px; font-size: 10px; font-weight: bold; min-height: 22px; }"
            "QPushButton:hover { background-color: #be123c; color: #ffffff; }"
        )
        self.btn_live_tv.clicked.connect(lambda: self.tv_demod_requested.emit(539_000_000.0))
        telem_hdr.addWidget(self.btn_live_tv)

        telem_hdr.addStretch(1)

        self.btn_clear_telem = QPushButton("✖", self.telem_box)
        self.btn_clear_telem.setToolTip("Clear telemetry log")
        self.btn_clear_telem.setStyleSheet(compact_btn_qss)
        self.btn_clear_telem.clicked.connect(self._clear_messages)
        telem_hdr.addWidget(self.btn_clear_telem)
        telem_vbox.addLayout(telem_hdr)

        self.table_telem = QTableWidget(self.telem_box)
        self.table_telem.setColumnCount(4)
        self.table_telem.setHorizontalHeaderLabels(["Time", "Proto", "MHz", "Decoded Payload"])
        self.table_telem.setAlternatingRowColors(True)
        self.table_telem.verticalHeader().setVisible(False)
        self.table_telem.verticalHeader().setDefaultSectionSize(22)
        self.table_telem.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table_telem.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table_telem.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        th = self.table_telem.horizontalHeader()
        th.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        th.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        th.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        th.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)

        telem_vbox.addWidget(self.table_telem, 1)
        self.splitter.addWidget(self.telem_box)

        self.splitter.setStretchFactor(0, 5)
        self.splitter.setStretchFactor(1, 4)
        root_vbox.addWidget(self.splitter, 1)

    def set_panel_view_mode(self, mode: int) -> None:
        """0 = Split, 1 = Full Radar, 2 = Full Feed."""
        if mode == 1:
            self.radar_box.setVisible(True)
            self.telem_box.setVisible(False)
        elif mode == 2:
            self.radar_box.setVisible(False)
            self.telem_box.setVisible(True)
        else:
            self.radar_box.setVisible(True)
            self.telem_box.setVisible(True)
            total = max(400, self.width())
            self.splitter.setSizes([int(total * 0.54), int(total * 0.46)])

    # ------------------------------------------------------------------
    # Live 1090 MHz ADS-B & Optional Simulation Control
    # ------------------------------------------------------------------
    def start_live_adsb(self) -> Tuple[bool, str]:
        """Public helper allowing main_window to activate Live 1090 MHz ADS-B directly."""
        if self._sim_mode:
            self.btn_sim_demo.setChecked(False)
            self._on_toggle_sim_demo(False)
        if not self._adsb_rx.is_running():
            ok, msg = self._adsb_rx.start(
                home_lat=self.radar_canvas.home_lat,
                home_lon=self.radar_canvas.home_lon,
            )
            self.btn_live_adsb.setChecked(ok)
            self.add_message(
                time.strftime("%H:%M:%S"),
                "ADSB",
                1090.000,
                "READSB" if ok else "BUSY",
                msg,
                "#10b981" if ok else "#ef4444",
            )
            if ok:
                self.radar_canvas.update_live_targets([], 0)
                self.lbl_target_detail.setText(
                    "LIVE 1090.0 MHz ADS-B ACTIVE: HackRF One (2.4 MSPS) -> readsb DF17 Mode-S decoder."
                )
            else:
                self.lbl_target_detail.setText(f"1090 MHz ADS-B unavailable: {msg}")
            return ok, msg
        self.btn_live_adsb.setChecked(True)
        return True, "Live 1090 MHz ADS-B already streaming."

    def stop_live_adsb(self) -> None:
        if self._adsb_rx.is_running():
            self._adsb_rx.stop()
        self.btn_live_adsb.setChecked(False)
        if not self._sim_mode:
            self.radar_canvas.mode_badge = "STANDBY"
            self.radar_canvas.update()
            self.lbl_target_detail.setText(
                "STANDBY: Tap [📡 LIVE ADS-B] for real 1090 MHz aircraft tracking, or [SIM] for demo vectors."
            )

    def _on_toggle_live_adsb(self, checked: bool) -> None:
        if checked:
            self.start_live_adsb()
        else:
            self.stop_live_adsb()
            self.add_message(
                time.strftime("%H:%M:%S"),
                "ADSB",
                1090.000,
                "STOPPED",
                "Released HackRF One from 1090 MHz ADS-B mode.",
                "#94a3b8",
            )

    def _on_toggle_sim_demo(self, checked: bool) -> None:
        self._sim_mode = checked
        if checked:
            self.stop_live_adsb()
            self.radar_canvas.set_simulation_mode(True)
            self._sim_msg_timer.start(4500)
            self.add_message(
                time.strftime("%H:%M:%S"),
                "SIM",
                1090.000,
                "DEMO_ON",
                "[SIMULATION] Enabled synthetic Front Range aircraft & demo telemetry.",
                "#f59e0b",
            )
        else:
            self._sim_msg_timer.stop()
            self.radar_canvas.set_simulation_mode(False)

    def _poll_live_adsb(self) -> None:
        if not self.btn_live_adsb.isChecked():
            return
        targets, total_msgs, running = self._adsb_rx.poll_targets()
        if not running:
            # Stopped externally by SDRResourceArbiter (e.g. Scanner or Gqrx claimed HackRF)
            self.btn_live_adsb.setChecked(False)
            self.radar_canvas.mode_badge = "STANDBY"
            self.radar_canvas.update()
            return

        self.radar_canvas.update_live_targets(targets, total_msgs)
        now_s = time.strftime("%H:%M:%S")
        for t in targets:
            icao = t["icao"]
            if icao not in self._seen_live_icaos:
                self._seen_live_icaos.add(icao)
                pos_txt = (
                    f"{t['distance_nm']}NM @ {t['bearing_deg']:.0f}°"
                    if t.get("has_position")
                    else "Mode-S Altitude Squitter"
                )
                self.add_message(
                    now_s,
                    "ADSB",
                    1090.000,
                    t["callsign"],
                    f"ICAO {icao} • {t['altitude_ft']}ft • {t['ground_speed_kt']}kt • {pos_txt}",
                    "#10b981",
                )

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
            f"{target.callsign} ({target.icao}) • {target.altitude_ft:,}ft • "
            f"{target.ground_speed_kt}kt • HDG {target.heading_deg:.0f}° • SQK {target.squawk} • "
            f"{target.distance_nm}NM @ {target.bearing_deg:.0f}°"
        )

    def _seed_initial_status(self) -> None:
        now_str = time.strftime("%H:%M:%S")
        self.add_message(
            now_str,
            "SYSTEM",
            0.000,
            "READY",
            "Penrose Base (DM78vj) ready — tap [📡 LIVE ADS-B], [📺 TV RX], [📟 Pager], [📍 APRS], or [📊 433M].",
            "#38bdf8",
        )

    def _on_sim_msg_tick(self) -> None:
        if not self._sim_mode:
            return
        now_str = time.strftime("%H:%M:%S")
        pool = [
            ("SIM_APRS", 144.390, "W0PEN-1", "[SIM] WX PENROSE 5300FT • 62.4F • WIND SW 6MPH", "#f59e0b"),
            ("SIM_433", 433.920, "ACURITE", "[SIM] SENSOR CH1 TEMP 18.2C HUMIDITY 38% BATT OK", "#f59e0b"),
            ("SIM_TV", 539.000, "KOAA_25", "[SIM] ATSC CH 25 8VSB PILOT 536.309 MHz DEMO", "#f59e0b"),
        ]
        chosen = random.choice(pool)
        self.add_message(now_str, chosen[0], chosen[1], chosen[2], chosen[3], chosen[4])

    def add_message(self, ts: str, ch_type: str, freq_mhz: float, source: str, content: str, color_hex: str) -> None:
        rec = DecodedMessageRecord(ts, ch_type, freq_mhz, source, content, color_hex)
        self.messages.insert(0, rec)
        if len(self.messages) > 100:
            self.messages.pop()

        if self.table_telem.rowCount() >= 100:
            self.table_telem.removeRow(self.table_telem.rowCount() - 1)

        self.table_telem.insertRow(0)

        t_item = QTableWidgetItem(ts)
        t_item.setFont(QFont("Monospace", 8))
        self.table_telem.setItem(0, 0, t_item)

        srv_item = QTableWidgetItem(f"{ch_type}:{source}")
        srv_item.setFont(QFont("SansSerif", 8, QFont.Weight.Bold))
        srv_item.setForeground(QColor(color_hex))
        self.table_telem.setItem(0, 1, srv_item)

        f_txt = f"{freq_mhz:.3f}" if freq_mhz > 0 else "---"
        f_item = QTableWidgetItem(f_txt)
        f_item.setFont(QFont("Monospace", 8, QFont.Weight.Bold))
        f_item.setForeground(QColor("#fbbf24"))
        self.table_telem.setItem(0, 2, f_item)

        c_item = QTableWidgetItem(content)
        c_item.setFont(QFont("Monospace", 8))
        c_item.setForeground(QColor("#e2e8f0"))
        self.table_telem.setItem(0, 3, c_item)

    # ------------------------------------------------------------------
    # Thread-Safe Background Telemetry Captures (Qt Signal Marshaled!)
    # ------------------------------------------------------------------
    def _on_bg_worker_done(self, btn_key: str) -> None:
        if btn_key == "pager":
            self.btn_live_pager.setEnabled(True)
        elif btn_key == "aprs":
            self.btn_live_aprs.setEnabled(True)
        elif btn_key == "sensors":
            self.btn_live_sensors.setEnabled(True)

    def _on_live_pager_clicked(self) -> None:
        self.decode_pager_frequency(155_880_000)

    def decode_pager_frequency(self, freq_hz: int = 155_880_000) -> None:
        f_mhz = freq_hz / 1e6
        self.stop_live_adsb()
        self.btn_live_pager.setEnabled(False)
        self.add_message(
            time.strftime("%H:%M:%S"), "POCSAG", f_mhz, "CAPTURE",
            f"Capturing 3s live HackRF IQ @ {f_mhz:.3f} MHz -> multimon-ng POCSAG/FLEX...", "#38bdf8",
        )

        def _worker() -> None:
            try:
                from core.demod_engine import run_pager_data_decode
                res = run_pager_data_decode(int(freq_hz), duration_sec=3.0)
                now_str = time.strftime("%H:%M:%S")
                if not res.get("success"):
                    self._bg_msg_sig.emit(
                        now_str, "POCSAG", f_mhz, "NOTE", str(res.get("error") or "Busy"), "#f59e0b"
                    )
                elif res.get("messages"):
                    for m in res["messages"]:
                        self._bg_msg_sig.emit(now_str, "POCSAG", f_mhz, "DECODED", str(m), "#ef4444")
                else:
                    self._bg_msg_sig.emit(
                        now_str, "POCSAG", f_mhz, "CLEAR",
                        "3.0s RF capture clean — no POCSAG/FLEX burst during window.", "#10b981",
                    )
            except Exception as exc:
                self._bg_msg_sig.emit(
                    time.strftime("%H:%M:%S"), "POCSAG", f_mhz, "ERR", str(exc), "#ef4444"
                )
            finally:
                self._bg_done_sig.emit("pager")

        threading.Thread(target=_worker, daemon=True, name="LivePagerWorker").start()

    def _on_live_aprs_clicked(self) -> None:
        self.decode_aprs_frequency(144_390_000)

    def decode_aprs_frequency(self, freq_hz: int = 144_390_000) -> None:
        f_mhz = freq_hz / 1e6
        self.stop_live_adsb()
        self.btn_live_aprs.setEnabled(False)
        self.add_message(
            time.strftime("%H:%M:%S"), "APRS", f_mhz, "CAPTURE",
            f"Capturing 4s live HackRF IQ @ {f_mhz:.3f} MHz -> AFSK1200 AX.25 decoder...", "#38bdf8",
        )

        def _worker() -> None:
            try:
                from core.demod_engine import run_aprs_data_decode
                res = run_aprs_data_decode(int(freq_hz), duration_sec=4.0)
                now_str = time.strftime("%H:%M:%S")
                if not res.get("success"):
                    self._bg_msg_sig.emit(
                        now_str, "APRS", f_mhz, "NOTE", str(res.get("error") or "Busy"), "#f59e0b"
                    )
                elif res.get("packets"):
                    for pkt in res["packets"]:
                        self._bg_msg_sig.emit(now_str, "APRS", f_mhz, "AX25", str(pkt), "#38bdf8")
                else:
                    self._bg_msg_sig.emit(
                        now_str, "APRS", f_mhz, "CLEAR",
                        "4.0s RF capture clean — no AX.25 packet burst during window.", "#10b981",
                    )
            except Exception as exc:
                self._bg_msg_sig.emit(
                    time.strftime("%H:%M:%S"), "APRS", f_mhz, "ERR", str(exc), "#ef4444"
                )
            finally:
                self._bg_done_sig.emit("aprs")

        threading.Thread(target=_worker, daemon=True, name="LiveAPRSWorker").start()

    def _on_live_sensors_clicked(self) -> None:
        self.decode_ism_frequency(433_920_000)

    def decode_ism_frequency(self, freq_hz: int = 433_920_000) -> None:
        f_mhz = freq_hz / 1e6
        self.stop_live_adsb()
        self.btn_live_sensors.setEnabled(False)
        self.add_message(
            time.strftime("%H:%M:%S"), "ISM", f_mhz, "CAPTURE",
            f"Capturing 3s live HackRF @ {f_mhz:.3f} MHz -> rtl_433 telemetry decoder...", "#38bdf8",
        )

        def _worker() -> None:
            try:
                from core.demod_engine import run_telemetry_decode
                res = run_telemetry_decode(int(freq_hz), duration_sec=3.0)
                now_str = time.strftime("%H:%M:%S")
                pkts = res.get("packets", [])
                if pkts:
                    for p in pkts:
                        self._bg_msg_sig.emit(
                            now_str, "ISM", f_mhz, str(p.get("model", "SENSOR")), json.dumps(p), "#eab308"
                        )
                elif not res.get("success"):
                    self._bg_msg_sig.emit(
                        now_str, "ISM", f_mhz, "NOTE", str(res.get("error") or "Busy"), "#f59e0b"
                    )
                else:
                    self._bg_msg_sig.emit(
                        now_str, "ISM", f_mhz, "CLEAR",
                        "3.0s rtl_433 capture clean — no OOK/FSK sensor frame during window.", "#10b981",
                    )
            except Exception as exc:
                self._bg_msg_sig.emit(
                    time.strftime("%H:%M:%S"), "ISM", f_mhz, "ERR", str(exc), "#ef4444"
                )
            finally:
                self._bg_done_sig.emit("sensors")

        threading.Thread(target=_worker, daemon=True, name="LiveSensorWorker").start()

    def _clear_messages(self) -> None:
        self.messages.clear()
        self.table_telem.setRowCount(0)
