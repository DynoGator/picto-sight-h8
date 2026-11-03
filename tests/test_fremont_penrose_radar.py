"""
Unit Tests for Fremont County / Penrose Locality, Special Services, and Tactical Radar Map
"""

from __future__ import annotations

import pytest

from core.bandplans import classify_voice_broadcast
from core.gps_engine import coords_to_maidenhead, get_gps_manager, GPSLocalityFix
from core.profile_organizer import (
    CB_CHANNELS_HZ,
    FREMONT_PENROSE_FREQUENCY_GUIDE,
    SOUTHERN_CO_FM_CALLSIGNS_MHZ,
    SignalProfileOrganizer,
)
from ui.map_widget import (
    haversine_distance_bearing_nm,
    PENROSE_HOME_LAT,
    PENROSE_HOME_LON,
    PENROSE_ELEV_M,
    PENROSE_GRID,
    TacticalRadarTelemetryWidget,
)


class TestFremontPenroseAndRadar:

    def test_penrose_gps_locality_and_grid(self) -> None:
        grid = coords_to_maidenhead(PENROSE_HOME_LAT, PENROSE_HOME_LON)
        assert grid.lower().startswith("dm78")
        assert PENROSE_GRID.lower().startswith("dm78")

        gps_mgr = get_gps_manager()
        fix = gps_mgr.get_current_fix()
        assert "Penrose" in fix.locality_name or "Fremont" in fix.locality_name
        assert abs(fix.latitude - PENROSE_HOME_LAT) < 0.1
        assert abs(fix.longitude - PENROSE_HOME_LON) < 0.1

    def test_haversine_distance_bearing(self) -> None:
        # Distance from Penrose Home to Fremont County Airport 1V6
        dist_nm, brg_deg = haversine_distance_bearing_nm(
            PENROSE_HOME_LAT, PENROSE_HOME_LON,
            38.4287, -105.1054,
        )
        assert 2.0 <= dist_nm <= 6.0
        assert 0.0 <= brg_deg <= 360.0

        # Distance from Penrose Home to Pueblo Memorial Airport (KPUB)
        dist_pub, brg_pub = haversine_distance_bearing_nm(
            PENROSE_HOME_LAT, PENROSE_HOME_LON,
            38.2891, -104.4966,
        )
        assert 20.0 <= dist_pub <= 35.0

    def test_fremont_services_voice_badges(self) -> None:
        # Penrose Fire Department
        p_fire = classify_voice_broadcast(155_880_000, demod="Narrow FM", band_name="Penrose Fire Dept")
        assert "[🚒 FIRE / EMS DISPATCH]" in p_fire["voice_label"]
        assert p_fire["is_any_voice"] is True

        # Florence / Penrose Fire Dispatch
        f_fire = classify_voice_broadcast(154_400_000, demod="Narrow FM")
        assert "[🚒 FIRE / EMS DISPATCH]" in f_fire["voice_label"]

        # Fremont County Sheriff
        sheriff = classify_voice_broadcast(154_845_000, demod="Narrow FM")
        assert "[🚓 SHERIFF / POLICE]" in sheriff["voice_label"]

        # Colorado Search & Rescue MRA-1
        sar = classify_voice_broadcast(155_160_000, demod="Narrow FM")
        assert "[🏔️ SEARCH & RESCUE]" in sar["voice_label"]

        # Medevac / CHERN Med-9
        med = classify_voice_broadcast(155_340_000, demod="Narrow FM")
        assert "[🚑 MEDEVAC / HOSPITAL]" in med["voice_label"]

        # Trucker CB Channel 19
        cb19 = classify_voice_broadcast(27_185_000, demod="AM")
        assert "[🚛 TRUCKER CH 19 HWY]" in cb19["voice_label"]

        # CB Emergency Channel 9
        cb09 = classify_voice_broadcast(27_065_000, demod="AM")
        assert "[🚨 CB CH 09 EMERGENCY]" in cb09["voice_label"]

        # Railroad Union Pacific AAR 092
        rail = classify_voice_broadcast(161_490_000, demod="Narrow FM")
        assert "[🚆 RAILROAD ROAD/YARD]" in rail["voice_label"]

        # Broadcast TV Channel 25 KOAA
        tv = classify_voice_broadcast(539_000_000, profile_category="Broadcast_TV")
        assert "[📺 BROADCAST TV]" in tv["voice_label"]

    def test_channel_name_generator_fremont_priors(self) -> None:
        organizer = SignalProfileOrganizer()
        fix = GPSLocalityFix(
            has_fix=True,
            source="LOCAL_PINNED",
            latitude=PENROSE_HOME_LAT,
            longitude=PENROSE_HOME_LON,
            altitude_m=PENROSE_ELEV_M,
            satellites_visible=0,
            satellites_used=0,
            fix_quality_pct=80.0,
            device_port="Mock Port",
            maidenhead_grid=PENROSE_GRID,
            locality_name="Penrose, Fremont County, Colorado",
            region_code="US",
            regulatory_domain="FCC_US",
            utc_timestamp_iso="2026-11-03T16:00:00Z",
            local_datetime_approx="2026-11-03 ~10:00 MST",
            status_message="Penrose Fix",
        )

        # Test Penrose Fire name generation
        name_pfd = organizer._generate_channel_name(
            freq_hz=155_880_000,
            band_name="Public Safety",
            demod="Narrow FM",
            pic_label="NFM-Voice",
            gps_fix=fix,
        )
        assert "Penrose Fire Dept" in name_pfd
        assert "FIRE / EMS" in name_pfd

        # Test Trucker CB Ch 19 name generation
        name_cb = organizer._generate_channel_name(
            freq_hz=27_185_000,
            band_name="CB Radio",
            demod="AM",
            pic_label="AM-Voice",
            gps_fix=fix,
        )
        assert "CB Ch 19 Trucker Highway" in name_cb

        # Test Fremont Co 1V6 Airport CTAF
        name_ctaf = organizer._generate_channel_name(
            freq_hz=122_800_000,
            band_name="Civil Airband",
            demod="AM",
            pic_label="Airband-AM",
            gps_fix=fix,
        )
        assert "Fremont Co" in name_ctaf or "1V6" in name_ctaf

        # Test UP Railroad Tennessee Pass Sub
        name_up = organizer._generate_channel_name(
            freq_hz=161_490_000,
            band_name="Railroad",
            demod="Narrow FM",
            pic_label="NFM-Voice",
            gps_fix=fix,
        )
        assert "Union Pacific" in name_up or "AAR 092" in name_up
