"""
USB u-blox GPS & Locality Engine
Continuously monitors USB u-blox GNSS serial devices and gpsd (port 2947),
persists last-known GPS/locality across reboots for offline field use,
and exposes both graphical (0-100% signal/fix quality) and alphanumerical telemetry.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import socket
import threading
import time
import urllib.request
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional, Dict, Any

logger = logging.getLogger("smart_scanner.gps")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOCALITY_CACHE_PATH = PROJECT_ROOT / "profiles" / "last_known_locality.json"


@dataclass
class GPSLocalityFix:
    has_fix: bool
    source: str                 # "USB_UBLOX_GPS", "GPSD_DAEMON", "CACHED_GPS_FIX", "IP_GEOLOCATION", "LOCAL_DEFAULT"
    latitude: float
    longitude: float
    altitude_m: float
    satellites_visible: int
    satellites_used: int
    fix_quality_pct: float      # 0..100 for graphical fix strength bar
    device_port: str
    maidenhead_grid: str        # e.g. "DM79mr"
    locality_name: str          # e.g. "Denver, Colorado, United States"
    region_code: str            # "US", "EU", "AU", etc.
    regulatory_domain: str      # "FCC_US", "ITU_R1", "ITU_R3"
    utc_timestamp_iso: str
    local_datetime_approx: str  # Human-readable local approximate date & time
    status_message: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def coords_to_maidenhead(lat: float, lon: float, precision: int = 3) -> str:
    """Converts latitude/longitude into a 6-character Maidenhead Grid Locator (e.g. DM79mr)."""
    lon_adj = max(0.0, min(359.9999, lon + 180.0))
    lat_adj = max(0.0, min(179.9999, lat + 90.0))

    field_lon = chr(ord("A") + int(lon_adj // 20))
    field_lat = chr(ord("A") + int(lat_adj // 10))
    square_lon = str(int((lon_adj % 20) // 2))
    square_lat = str(int((lat_adj % 10) // 1))
    sub_lon = chr(ord("a") + int(((lon_adj % 2) / 2.0) * 24))
    sub_lat = chr(ord("a") + int(((lat_adj % 1) / 1.0) * 24))

    if precision == 1:
        return f"{field_lon}{field_lat}"
    if precision == 2:
        return f"{field_lon}{field_lat}{square_lon}{square_lat}"
    return f"{field_lon}{field_lat}{square_lon}{square_lat}{sub_lon}{sub_lat}"


def format_approx_local_datetime(ts: Optional[float] = None) -> str:
    t = time.localtime(ts if ts is not None else time.time())
    return time.strftime("%Y-%m-%d ~%H:%M %Z", t)


class GPSLocalityManager:
    """
    Non-blocking background GPS & locality tracker with reboot-persistent locality cache.
    """

    def __init__(self, preferred_device: Optional[str] = None, baud_rate: int = 9600) -> None:
        self.preferred_device = preferred_device
        self.baud_rate = baud_rate
        self._lock = threading.Lock()
        self._running = False
        self._monitor_thread: Optional[threading.Thread] = None

        self._current_fix = GPSLocalityFix(
            has_fix=False,
            source="LOCAL_DEFAULT",
            latitude=39.7392,
            longitude=-104.9903,
            altitude_m=1609.0,
            satellites_visible=0,
            satellites_used=0,
            fix_quality_pct=35.0,
            device_port="Polling USB u-blox / gpsd",
            maidenhead_grid=coords_to_maidenhead(39.7392, -104.9903),
            locality_name="Denver, Colorado, United States",
            region_code="US",
            regulatory_domain="FCC_US",
            utc_timestamp_iso=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            local_datetime_approx=format_approx_local_datetime(),
            status_message="Polling USB u-blox GNSS & gpsd:2947...",
        )
        self._load_cached_locality()

    def _load_cached_locality(self) -> None:
        try:
            if LOCALITY_CACHE_PATH.exists():
                raw = json.loads(LOCALITY_CACHE_PATH.read_text(encoding="utf-8"))
                lat = float(raw.get("latitude", 39.7392))
                lon = float(raw.get("longitude", -104.9903))
                loc = str(raw.get("locality_name", "Denver, Colorado, United States"))
                reg = str(raw.get("region_code", "US"))
                dom = str(raw.get("regulatory_domain", "FCC_US"))
                src = "CACHED_GPS_FIX" if "GPS" in str(raw.get("source", "")) else "IP_GEOLOCATION"
                with self._lock:
                    self._current_fix = GPSLocalityFix(
                        has_fix=True,
                        source=src,
                        latitude=lat,
                        longitude=lon,
                        altitude_m=float(raw.get("altitude_m", 1609.0)),
                        satellites_visible=0,
                        satellites_used=0,
                        fix_quality_pct=60.0,
                        device_port="Cached Locality (Polling USB u-blox)",
                        maidenhead_grid=coords_to_maidenhead(lat, lon),
                        locality_name=loc,
                        region_code=reg,
                        regulatory_domain=dom,
                        utc_timestamp_iso=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        local_datetime_approx=format_approx_local_datetime(),
                        status_message=f"Restored saved locality: {loc}. Polling USB u-blox GPS...",
                    )
        except Exception as exc:
            logger.debug("Locality cache load note: %s", exc)

    def _save_cached_locality(self, fix: GPSLocalityFix) -> None:
        try:
            LOCALITY_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            LOCALITY_CACHE_PATH.write_text(json.dumps(fix.to_dict(), indent=2), encoding="utf-8")
        except Exception as exc:
            logger.debug("Locality cache save note: %s", exc)

    def get_current_fix(self) -> GPSLocalityFix:
        with self._lock:
            # Refresh approximate local time stamp on read
            self._current_fix.local_datetime_approx = format_approx_local_datetime()
            self._current_fix.utc_timestamp_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            return GPSLocalityFix(**self._current_fix.to_dict())

    def refresh_now(self) -> GPSLocalityFix:
        """Performs an immediate fast poll of gpsd / USB u-blox before starting a scan."""
        if not self._query_gpsd():
            dev = self._find_ublox_device()
            if dev:
                self._read_serial_gps(dev)
        return self.get_current_fix()

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True, name="GPSMonitor")
        self._monitor_thread.start()

    def stop(self) -> None:
        self._running = False
        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=1.0)

    def _resolve_ip_fallback(self) -> None:
        try:
            req = urllib.request.Request(
                "http://ip-api.com/json",
                headers={"User-Agent": "SmartRFScanner/2.0"},
            )
            with urllib.request.urlopen(req, timeout=2.5) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="ignore"))
                if data.get("status") == "success":
                    lat = float(data.get("lat", 39.7392))
                    lon = float(data.get("lon", -104.9903))
                    city = data.get("city", "Denver")
                    region_name = data.get("regionName", "Colorado")
                    country = data.get("country", "United States")
                    country_code = data.get("countryCode", "US")
                    reg_domain = (
                        "FCC_US"
                        if country_code in ("US", "CA", "MX")
                        else ("ITU_R1" if country_code in ("GB", "DE", "FR", "IT", "ES", "NL", "PL", "SE") else "ITU_R3")
                    )
                    grid = coords_to_maidenhead(lat, lon)
                    fix = GPSLocalityFix(
                        has_fix=True,
                        source="IP_GEOLOCATION",
                        latitude=lat,
                        longitude=lon,
                        altitude_m=1609.0 if city == "Denver" else 250.0,
                        satellites_visible=0,
                        satellites_used=0,
                        fix_quality_pct=58.0,
                        device_port="Network Geo (Polling USB u-blox)",
                        maidenhead_grid=grid,
                        locality_name=f"{city}, {region_name}, {country}",
                        region_code=country_code,
                        regulatory_domain=reg_domain,
                        utc_timestamp_iso=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        local_datetime_approx=format_approx_local_datetime(),
                        status_message=f"Locality: {city}, {region_name} ({grid}) • Polling USB u-blox GNSS",
                    )
                    with self._lock:
                        if self._current_fix.source not in ("USB_UBLOX_GPS", "GPSD_DAEMON"):
                            self._current_fix = fix
                    self._save_cached_locality(fix)
        except Exception as exc:
            logger.debug("IP geolocation fallback unavailable: %s", exc)

    def _find_ublox_device(self) -> Optional[str]:
        if self.preferred_device and os.path.exists(self.preferred_device):
            return self.preferred_device

        patterns = [
            "/dev/serial/by-id/*[uU][-]?[bB][lL][oO][xX]*",
            "/dev/serial/by-id/*GNSS*",
            "/dev/serial/by-id/*GPS*",
            "/dev/ttyACM*",
            "/dev/ttyUSB*",
        ]
        for pat in patterns:
            matches = sorted(glob.glob(pat))
            if matches:
                return matches[0]
        return None

    @staticmethod
    def _parse_nmea_coord(val_str: str, hemi_str: str) -> Optional[float]:
        try:
            if not val_str or not hemi_str:
                return None
            dot = val_str.find(".")
            if dot < 2:
                return None
            degrees = float(val_str[: dot - 2])
            minutes = float(val_str[dot - 2 :])
            dec = degrees + (minutes / 60.0)
            if hemi_str.upper() in ("S", "W"):
                dec = -dec
            return dec
        except Exception:
            return None

    def _read_serial_gps(self, port_path: str) -> bool:
        try:
            import serial
        except ImportError:
            return False

        sats_vis = 0
        for baud in (9600, 38400, 115200):
            if not self._running:
                break
            try:
                with serial.Serial(port_path, baud, timeout=1.0) as ser:
                    for _ in range(10):
                        line = ser.readline().decode("ascii", errors="ignore").strip()
                        if not line:
                            continue
                        if line.startswith(("$GPGSV", "$GNGSV", "$GLGSV")):
                            parts = line.split(",")
                            if len(parts) >= 4 and parts[3].isdigit():
                                sats_vis = max(sats_vis, int(parts[3]))
                        elif line.startswith(("$GPGGA", "$GNGGA")):
                            parts = line.split(",")
                            if len(parts) >= 10:
                                lat = self._parse_nmea_coord(parts[2], parts[3])
                                lon = self._parse_nmea_coord(parts[4], parts[5])
                                qual = int(parts[6]) if parts[6].isdigit() else 0
                                sats = int(parts[7]) if parts[7].isdigit() else 0
                                alt = float(parts[9]) if parts[9] else 0.0
                                if lat is not None and lon is not None and qual > 0:
                                    self._apply_hardware_fix(
                                        source="USB_UBLOX_GPS",
                                        lat=lat,
                                        lon=lon,
                                        alt=alt,
                                        sats_used=sats,
                                        sats_vis=max(sats_vis, sats),
                                        port=port_path,
                                        mode=3 if alt != 0.0 else 2,
                                    )
                                    return True
                        elif line.startswith(("$GPRMC", "$GNRMC")):
                            parts = line.split(",")
                            if len(parts) >= 7 and parts[2] == "A":
                                lat = self._parse_nmea_coord(parts[3], parts[4])
                                lon = self._parse_nmea_coord(parts[5], parts[6])
                                if lat is not None and lon is not None:
                                    self._apply_hardware_fix(
                                        source="USB_UBLOX_GPS",
                                        lat=lat,
                                        lon=lon,
                                        alt=0.0,
                                        sats_used=max(4, sats_vis),
                                        sats_vis=max(4, sats_vis),
                                        port=port_path,
                                        mode=3,
                                    )
                                    return True
            except Exception:
                continue
        return False

    def _query_gpsd(self) -> bool:
        try:
            sock = socket.create_connection(("127.0.0.1", 2947), timeout=0.8)
            sock.sendall(b'?WATCH={"enable":true,"json":true};\n?POLL;\n')
            time.sleep(0.25)
            chunks = []
            sock.settimeout(0.35)
            try:
                while True:
                    buf = sock.recv(4096)
                    if not buf:
                        break
                    chunks.append(buf.decode("utf-8", errors="ignore"))
                    if len(chunks) >= 4:
                        break
            except Exception:
                pass
            sock.close()

            data = "".join(chunks)
            sats_vis = 0
            sats_used = 0
            for line in data.splitlines():
                line = line.strip()
                if not line.startswith("{"):
                    continue
                msg = json.loads(line)
                if msg.get("class") == "SKY":
                    sats = msg.get("satellites", [])
                    sats_vis = max(sats_vis, len(sats))
                    sats_used = max(sats_used, sum(1 for s in sats if s.get("used")))
                elif msg.get("class") == "POLL":
                    for sky in msg.get("sky", []):
                        sats = sky.get("satellites", [])
                        sats_vis = max(sats_vis, len(sats))
                        sats_used = max(sats_used, sum(1 for s in sats if s.get("used")))
                    for tpv in msg.get("tpv", []):
                        if tpv.get("mode", 0) >= 2 and "lat" in tpv and "lon" in tpv:
                            self._apply_hardware_fix(
                                source="GPSD_DAEMON",
                                lat=float(tpv["lat"]),
                                lon=float(tpv["lon"]),
                                alt=float(tpv.get("alt", 0.0)),
                                sats_used=max(sats_used, 4),
                                sats_vis=max(sats_vis, sats_used, 4),
                                port=str(tpv.get("device", "gpsd:2947")),
                                mode=int(tpv.get("mode", 3)),
                            )
                            return True
                elif msg.get("class") == "TPV":
                    if msg.get("mode", 0) >= 2 and "lat" in msg and "lon" in msg:
                        self._apply_hardware_fix(
                            source="GPSD_DAEMON",
                            lat=float(msg["lat"]),
                            lon=float(msg["lon"]),
                            alt=float(msg.get("alt", 0.0)),
                            sats_used=max(sats_used, 4),
                            sats_vis=max(sats_vis, sats_used, 4),
                            port=str(msg.get("device", "gpsd:2947")),
                            mode=int(msg.get("mode", 3)),
                        )
                        return True
        except Exception:
            pass
        return False

    def _apply_hardware_fix(
        self,
        source: str,
        lat: float,
        lon: float,
        alt: float,
        sats_used: int,
        sats_vis: int,
        port: str,
        mode: int,
    ) -> None:
        grid = coords_to_maidenhead(lat, lon)
        is_americas = -170.0 <= lon <= -30.0
        region_code = "US" if is_americas else ("EU" if -30.0 < lon <= 60.0 else "APAC")
        reg_domain = "FCC_US" if is_americas else ("ITU_R1" if region_code == "EU" else "ITU_R3")
        pct = min(100.0, 70.0 + min(30.0, sats_used * 3.5)) if mode >= 3 else 75.0

        with self._lock:
            prev_name = self._current_fix.locality_name
            # Keep human city name if coordinates are within ~0.75 deg of previous named locality
            if (
                abs(self._current_fix.latitude - lat) < 0.75
                and abs(self._current_fix.longitude - lon) < 0.75
                and not prev_name.startswith(("GPS", "3D"))
            ):
                loc_name = f"{prev_name} [{lat:.4f}, {lon:.4f}]"
            else:
                loc_name = f"GNSS {lat:.4f}, {lon:.4f} ({grid})"

            fix = GPSLocalityFix(
                has_fix=True,
                source=source,
                latitude=lat,
                longitude=lon,
                altitude_m=alt,
                satellites_visible=sats_vis,
                satellites_used=sats_used,
                fix_quality_pct=round(pct, 1),
                device_port=port,
                maidenhead_grid=grid,
                locality_name=loc_name,
                region_code=region_code,
                regulatory_domain=reg_domain,
                utc_timestamp_iso=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                local_datetime_approx=format_approx_local_datetime(),
                status_message=f"u-blox GNSS {mode}D Lock • {sats_used}/{sats_vis} Sats • Grid {grid} ({port})",
            )
            self._current_fix = fix
        self._save_cached_locality(fix)

    def _monitor_loop(self) -> None:
        # Resolve IP locality once on background thread startup
        self._resolve_ip_fallback()
        while self._running:
            if not self._query_gpsd():
                dev = self._find_ublox_device()
                if dev:
                    self._read_serial_gps(dev)
            for _ in range(10):
                if not self._running:
                    break
                time.sleep(0.4)


_gps_manager: Optional[GPSLocalityManager] = None
_gps_lock = threading.Lock()


def get_gps_manager() -> GPSLocalityManager:
    global _gps_manager
    with _gps_lock:
        if _gps_manager is None:
            _gps_manager = GPSLocalityManager()
            _gps_manager.start()
        return _gps_manager
