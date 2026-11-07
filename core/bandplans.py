"""
Bandplan Definitions, Channel Raster Alignment, Regulatory Classifications & Gqrx Demod Heuristics
Covers FCC Region 2 (US/Americas), ITU Region 1 (EU/Africa), and ITU Region 3 (Asia-Pacific).
Translates measured RF characteristics (center frequency, true dBFS, SNR, OBW, IQ modulation signature)
into exact Gqrx 2.17.6 configuration & rigctl parameters.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple, Dict, Any, List

# Supported Demodulation Options exposed in UI and translated to Gqrx 2.17.6
SUPPORTED_DEMOD_MODES: List[str] = [
    "AUTO (Band + AI)",
    "WFM (stereo)",
    "WFM (mono)",
    "Narrow FM",
    "AM",
    "AM-Sync",
    "USB",
    "LSB",
    "CW-U",
    "Raw I/Q",
]

# Exact mapping from Gqrx UI demod string -> (rigctl TCP mode, default passband Hz, low_cut, high_cut, fm_maxdev)
GQRX_DEMOD_SPECS: Dict[str, Tuple[str, int, int, int, int]] = {
    "WFM (stereo)": ("WFM_ST", 160_000, -80_000, 80_000, 75_000),
    "WFM (mono)":   ("WFM",    160_000, -80_000, 80_000, 75_000),
    "Narrow FM":    ("FM",      12_500,  -6_250,  6_250,  5_000),
    "AM":           ("AM",      10_000,  -5_000,  5_000,      0),
    "AM-Sync":      ("AMS",     10_000,  -5_000,  5_000,      0),
    "USB":          ("USB",      2_800,     100,  2_900,      0),
    "LSB":          ("LSB",      2_800,  -2_900,   -100,      0),
    "CW-U":         ("CW",         500,    -250,    250,      0),
    "CW-L":         ("CWR",        500,    -250,    250,      0),
    "Raw I/Q":      ("RAW",     40_000, -20_000, 20_000,      0),
}

# Gqrx Bookmark Tag Color Palette (two-table bookmarks.csv spec)
BOOKMARK_CATEGORY_COLORS: Dict[str, str] = {
    "Active_Target":     "#00ffff",
    "SmartScanner":      "#38bdf8",
    "Voice_Broadcast":   "#10b981",
    "Broadcast_Audio":   "#10b981",
    "Broadcast_TV":      "#e11d48",
    "Fire_EMS":          "#ef4444",
    "Sheriff_Police":    "#3b82f6",
    "Search_Rescue":     "#f97316",
    "Medevac_Hospital":  "#ec4899",
    "Trucker_CB":        "#a855f7",
    "Railroad_Radio":    "#eab308",
    "Aviation_Airband":  "#f59e0b",
    "Emergency_Weather": "#ef4444",
    "Amateur_Radio":     "#c084fc",
    "Marine_VHF":        "#06b6d4",
    "Public_Safety":     "#3b82f6",
    "Pictogram_Exotic":  "#ec4899",
    "ISM_Telemetry":     "#eab308",
    "Data_Telemetry":    "#64748b",
}

NOAA_WX_CHANNELS_HZ: Tuple[int, ...] = (
    162_400_000,
    162_425_000,
    162_450_000,
    162_475_000,
    162_500_000,
    162_525_000,
    162_550_000,
)


@dataclass
class BandDefinition:
    start_hz: float
    end_hz: float
    band_name: str
    service_type: str
    primary_demod: str        # Must exist in GQRX_DEMOD_SPECS
    channel_step_hz: float
    default_bandwidth_hz: float
    filter_low_cut: int
    filter_high_cut: int
    fm_maxdev_hz: int
    is_legal_demod: bool
    legal_notes: str
    profile_category: str


# Prioritized more-specific sub-bands first so sub-allocations (NOAA, Marine, FRS, ISM) match before broad LMR
BANDPLANS_FCC_US: List[BandDefinition] = [
    BandDefinition(
        start_hz=26_965_000, end_hz=27_405_000,
        band_name="CB Radio (11m Citizens Band)", service_type="Civilian Voice",
        primary_demod="AM", channel_step_hz=10_000, default_bandwidth_hz=10_000,
        filter_low_cut=-4_500, filter_high_cut=4_500, fm_maxdev_hz=0,
        is_legal_demod=True, legal_notes="Legal civilian communications (FCC Part 95)",
        profile_category="Amateur_Radio",
    ),
    BandDefinition(
        start_hz=28_000_000, end_hz=29_700_000,
        band_name="10m Amateur Band", service_type="Amateur Radio HF/VHF",
        primary_demod="USB", channel_step_hz=5_000, default_bandwidth_hz=2_800,
        filter_low_cut=100, filter_high_cut=2_900, fm_maxdev_hz=0,
        is_legal_demod=True, legal_notes="Legal amateur radio reception (FCC Part 97)",
        profile_category="Amateur_Radio",
    ),
    BandDefinition(
        start_hz=50_000_000, end_hz=54_000_000,
        band_name="6m Amateur Band (VHF)", service_type="Amateur Radio",
        primary_demod="Narrow FM", channel_step_hz=20_000, default_bandwidth_hz=15_000,
        filter_low_cut=-6_500, filter_high_cut=6_500, fm_maxdev_hz=5_000,
        is_legal_demod=True, legal_notes="Legal amateur radio reception",
        profile_category="Amateur_Radio",
    ),
    BandDefinition(
        start_hz=54_000_000, end_hz=72_000_000,
        band_name="VHF Low Broadcast TV (Ch 2-4)", service_type="ATSC Digital Television",
        primary_demod="Raw I/Q", channel_step_hz=6_000_000, default_bandwidth_hz=6_000_000,
        filter_low_cut=-20_000, filter_high_cut=20_000, fm_maxdev_hz=0,
        is_legal_demod=True, legal_notes="Open ATSC terrestrial digital television (FCC Part 73)",
        profile_category="Broadcast_TV",
    ),
    BandDefinition(
        start_hz=76_000_000, end_hz=88_000_000,
        band_name="VHF Low Broadcast TV (Ch 5-6)", service_type="ATSC Digital Television",
        primary_demod="Raw I/Q", channel_step_hz=6_000_000, default_bandwidth_hz=6_000_000,
        filter_low_cut=-20_000, filter_high_cut=20_000, fm_maxdev_hz=0,
        is_legal_demod=True, legal_notes="Open ATSC terrestrial digital television (FCC Part 73)",
        profile_category="Broadcast_TV",
    ),
    BandDefinition(
        start_hz=87_900_000, end_hz=108_000_000,
        band_name="VHF FM Commercial Broadcast", service_type="Commercial Stereo Broadcast",
        primary_demod="WFM (stereo)", channel_step_hz=200_000, default_bandwidth_hz=180_000,
        filter_low_cut=-90_000, filter_high_cut=90_000, fm_maxdev_hz=75_000,
        is_legal_demod=True, legal_notes="Public commercial/educational broadcast",
        profile_category="Broadcast_Audio",
    ),
    BandDefinition(
        start_hz=108_000_000, end_hz=117_975_000,
        band_name="VHF Airband (VOR / ILS Navigation)", service_type="Aeronautical Navigation",
        primary_demod="AM", channel_step_hz=50_000, default_bandwidth_hz=10_000,
        filter_low_cut=-5_000, filter_high_cut=5_000, fm_maxdev_hz=0,
        is_legal_demod=True, legal_notes="Open aviation navigation beacons",
        profile_category="Aviation_Airband",
    ),
    BandDefinition(
        start_hz=118_000_000, end_hz=136_975_000,
        band_name="VHF Civil Aviation Voice (ATC)", service_type="Aeronautical Air Traffic Control",
        primary_demod="AM", channel_step_hz=25_000, default_bandwidth_hz=10_000,
        filter_low_cut=-4_500, filter_high_cut=4_500, fm_maxdev_hz=0,
        is_legal_demod=True, legal_notes="Civil aviation ATC/ATIS open for passive monitoring",
        profile_category="Aviation_Airband",
    ),
    BandDefinition(
        start_hz=137_000_000, end_hz=138_000_000,
        band_name="VHF Meteorological Satellite Downlink", service_type="Weather Satellite Telemetry",
        primary_demod="Narrow FM", channel_step_hz=25_000, default_bandwidth_hz=38_000,
        filter_low_cut=-19_000, filter_high_cut=19_000, fm_maxdev_hz=15_000,
        is_legal_demod=True, legal_notes="Public NOAA/Meteor orbital weather telemetry",
        profile_category="Emergency_Weather",
    ),
    BandDefinition(
        start_hz=144_000_000, end_hz=148_000_000,
        band_name="2m Amateur Radio Band", service_type="Amateur Repeaters & Simplex",
        primary_demod="Narrow FM", channel_step_hz=15_000, default_bandwidth_hz=14_000,
        filter_low_cut=-6_500, filter_high_cut=6_500, fm_maxdev_hz=5_000,
        is_legal_demod=True, legal_notes="Legal amateur radio reception (FCC Part 97)",
        profile_category="Amateur_Radio",
    ),
    BandDefinition(
        start_hz=151_820_000, end_hz=151_940_000,
        band_name="MURS Unlicensed VHF Radio", service_type="Civilian Short-Range VHF",
        primary_demod="Narrow FM", channel_step_hz=60_000, default_bandwidth_hz=11_250,
        filter_low_cut=-5_500, filter_high_cut=5_500, fm_maxdev_hz=2_500,
        is_legal_demod=True, legal_notes="Legal Part 95 MURS civilian channels",
        profile_category="Public_Safety",
    ),
    BandDefinition(
        start_hz=156_000_000, end_hz=162_025_000,
        band_name="VHF International Marine Band", service_type="Maritime Safety & Port Ops",
        primary_demod="Narrow FM", channel_step_hz=25_000, default_bandwidth_hz=16_000,
        filter_low_cut=-7_500, filter_high_cut=7_500, fm_maxdev_hz=5_000,
        is_legal_demod=True, legal_notes="Open international maritime safety channels",
        profile_category="Marine_VHF",
    ),
    BandDefinition(
        start_hz=162_390_000, end_hz=162_560_000,
        band_name="NOAA All-Hazards Weather Radio", service_type="Continuous Emergency Weather",
        primary_demod="Narrow FM", channel_step_hz=25_000, default_bandwidth_hz=16_000,
        filter_low_cut=-7_500, filter_high_cut=7_500, fm_maxdev_hz=5_000,
        is_legal_demod=True, legal_notes="Official 24/7 public NOAA weather broadcast",
        profile_category="Emergency_Weather",
    ),
    BandDefinition(
        start_hz=150_000_000, end_hz=174_000_000,
        band_name="VHF Public Safety & Land Mobile", service_type="Municipal / Fire / EMS / Rail",
        primary_demod="Narrow FM", channel_step_hz=12_500, default_bandwidth_hz=12_500,
        filter_low_cut=-6_000, filter_high_cut=6_000, fm_maxdev_hz=2_500,
        is_legal_demod=True, legal_notes="Unencrypted analog/clear dispatch open to reception",
        profile_category="Public_Safety",
    ),
    BandDefinition(
        start_hz=174_000_000, end_hz=216_000_000,
        band_name="VHF High Broadcast TV (Ch 7-13)", service_type="ATSC Digital Television",
        primary_demod="Raw I/Q", channel_step_hz=6_000_000, default_bandwidth_hz=6_000_000,
        filter_low_cut=-20_000, filter_high_cut=20_000, fm_maxdev_hz=0,
        is_legal_demod=True, legal_notes="Open ATSC terrestrial digital television (FCC Part 73)",
        profile_category="Broadcast_TV",
    ),
    BandDefinition(
        start_hz=222_000_000, end_hz=225_000_000,
        band_name="1.25m Amateur Band", service_type="Amateur Radio",
        primary_demod="Narrow FM", channel_step_hz=20_000, default_bandwidth_hz=15_000,
        filter_low_cut=-6_500, filter_high_cut=6_500, fm_maxdev_hz=5_000,
        is_legal_demod=True, legal_notes="Legal amateur radio reception",
        profile_category="Amateur_Radio",
    ),
    BandDefinition(
        start_hz=225_000_000, end_hz=380_000_000,
        band_name="UHF Military Airband", service_type="Military Aviation Voice",
        primary_demod="AM", channel_step_hz=25_000, default_bandwidth_hz=12_000,
        filter_low_cut=-5_500, filter_high_cut=5_500, fm_maxdev_hz=0,
        is_legal_demod=True, legal_notes="Clear-voice UHF aviation ATC monitoring",
        profile_category="Aviation_Airband",
    ),
    BandDefinition(
        start_hz=433_050_000, end_hz=434_790_000,
        band_name="433 MHz ISM Telemetry Band", service_type="IoT / Weather Sensors / LPD433",
        primary_demod="Narrow FM", channel_step_hz=25_000, default_bandwidth_hz=25_000,
        filter_low_cut=-12_500, filter_high_cut=12_500, fm_maxdev_hz=5_000,
        is_legal_demod=True, legal_notes="Unlicensed ISM short-range telemetry",
        profile_category="ISM_Telemetry",
    ),
    BandDefinition(
        start_hz=420_000_000, end_hz=450_000_000,
        band_name="70cm Amateur Radio Band", service_type="Amateur UHF Voice & Satellites",
        primary_demod="Narrow FM", channel_step_hz=25_000, default_bandwidth_hz=15_000,
        filter_low_cut=-6_500, filter_high_cut=6_500, fm_maxdev_hz=5_000,
        is_legal_demod=True, legal_notes="Legal amateur radio reception",
        profile_category="Amateur_Radio",
    ),
    BandDefinition(
        start_hz=462_550_000, end_hz=467_725_000,
        band_name="FRS / GMRS Personal Radio", service_type="Civilian UHF Two-Way Radio",
        primary_demod="Narrow FM", channel_step_hz=12_500, default_bandwidth_hz=12_500,
        filter_low_cut=-5_500, filter_high_cut=5_500, fm_maxdev_hz=2_500,
        is_legal_demod=True, legal_notes="Legal Part 95 personal/family radio channels",
        profile_category="Public_Safety",
    ),
    BandDefinition(
        start_hz=450_000_000, end_hz=470_000_000,
        band_name="UHF Public Safety & Commercial LMR", service_type="Dispatch / Transit / Utilities",
        primary_demod="Narrow FM", channel_step_hz=12_500, default_bandwidth_hz=12_500,
        filter_low_cut=-5_500, filter_high_cut=5_500, fm_maxdev_hz=2_500,
        is_legal_demod=True, legal_notes="Unencrypted analog LMR channels legal to monitor",
        profile_category="Public_Safety",
    ),
    BandDefinition(
        start_hz=470_000_000, end_hz=608_000_000,
        band_name="UHF Digital Broadcast TV (Ch 14-36)", service_type="ATSC 1.0 / ATSC 3.0 Digital TV",
        primary_demod="Raw I/Q", channel_step_hz=6_000_000, default_bandwidth_hz=6_000_000,
        filter_low_cut=-20_000, filter_high_cut=20_000, fm_maxdev_hz=0,
        is_legal_demod=True, legal_notes="Open ATSC terrestrial digital television (FCC Part 73)",
        profile_category="Broadcast_TV",
    ),
    BandDefinition(
        start_hz=769_000_000, end_hz=869_000_000,
        band_name="700/800 MHz Public Safety Trunking", service_type="Regional Trunked Dispatch",
        primary_demod="Narrow FM", channel_step_hz=12_500, default_bandwidth_hz=12_500,
        filter_low_cut=-6_000, filter_high_cut=6_000, fm_maxdev_hz=3_000,
        is_legal_demod=True, legal_notes="Clear public safety channels; encrypted talkgroups skipped",
        profile_category="Public_Safety",
    ),
    # Cellular ECPA restricted bands explicitly marked non-legal so legal-only filter rejects them!
    BandDefinition(
        start_hz=869_000_000, end_hz=894_000_000,
        band_name="Cellular Band CLR Downlink (Restricted)", service_type="Commercial Cellular",
        primary_demod="Raw I/Q", channel_step_hz=200_000, default_bandwidth_hz=200_000,
        filter_low_cut=-20_000, filter_high_cut=20_000, fm_maxdev_hz=0,
        is_legal_demod=False, legal_notes="Restricted cellular band under US ECPA (excluded from demod)",
        profile_category="ISM_Telemetry",
    ),
    BandDefinition(
        start_hz=902_000_000, end_hz=928_000_000,
        band_name="902-928 MHz ISM / 33cm Band", service_type="LoRa / Meshtastic / Smart Grid / Ham",
        primary_demod="Narrow FM", channel_step_hz=25_000, default_bandwidth_hz=25_000,
        filter_low_cut=-12_500, filter_high_cut=12_500, fm_maxdev_hz=5_000,
        is_legal_demod=True, legal_notes="Unlicensed 915 MHz ISM & 33cm Amateur radio",
        profile_category="ISM_Telemetry",
    ),
    BandDefinition(
        start_hz=1_089_000_000, end_hz=1_091_000_000,
        band_name="ADS-B 1090 MHz Aircraft Transponders", service_type="Mode-S Aviation Surveillance",
        primary_demod="AM", channel_step_hz=1_000_000, default_bandwidth_hz=2_000_000,
        filter_low_cut=-20_000, filter_high_cut=20_000, fm_maxdev_hz=0,
        is_legal_demod=True, legal_notes="Unencrypted international aircraft ADS-B broadcasts",
        profile_category="Aviation_Airband",
    ),
]


def find_band_definition(freq_hz: float, region: str = "US") -> Optional[BandDefinition]:
    """Finds matching regulatory band definition for a given frequency."""
    for b in BANDPLANS_FCC_US:
        if b.start_hz <= freq_hz <= b.end_hz:
            return b
    return None


def snap_to_channel_raster(freq_hz: float, region: str = "US") -> Tuple[int, float]:
    """
    Aligns a measured peak frequency to standard regulatory channel centers when within
    valid capture tolerance, returning (snapped_freq_hz, offset_from_raw_hz).
    """
    raw = float(freq_hz)

    # 0. Commercial AM Mediumwave Broadcast (10 kHz Region 2 / 9 kHz Region 1/3)
    if 525_000 <= raw <= 1_705_000:
        step = 10_000.0 if region in ("US", "CA", "MX") else 9_000.0
        snapped = int(round(raw / step) * step)
        if abs(raw - snapped) <= 4_200:
            return snapped, float(raw - snapped)

    # 1. NOAA All-Hazards Weather Radio exact 7-channel table
    if 162_385_000 <= raw <= 162_565_000:
        nearest = min(NOAA_WX_CHANNELS_HZ, key=lambda c: abs(c - raw))
        if abs(nearest - raw) <= 14_000:
            return int(nearest), float(raw - nearest)

    # 2. Commercial FM Broadcast Band:
    # US/CA/MX (Region 2): 200 kHz odd-tenth channels (87.9, 88.1, 88.3, ..., 107.9 MHz)
    # EU/Region 1: 100 kHz channels (87.5, 87.6, ..., 108.0 MHz)
    if 87_800_000 <= raw <= 108_050_000:
        if region in ("US", "CA", "MX"):
            base = 87_900_000.0
            step = 200_000.0
            idx = round((raw - base) / step)
            snapped = int(round(base + idx * step))
            if 87_900_000 <= snapped <= 107_900_000 and abs(raw - snapped) <= 96_000:
                return snapped, float(raw - snapped)
        else:
            step = 100_000.0
            snapped = int(round(raw / step) * step)
            if abs(raw - snapped) <= 45_000:
                return snapped, float(raw - snapped)

    # 3. Civil & Military Aviation Airband (25 kHz channels)
    if (108_000_000 <= raw <= 136_975_000) or (225_000_000 <= raw <= 380_000_000):
        step = 25_000.0
        snapped = int(round(raw / step) * step)
        if abs(raw - snapped) <= 9_000:
            return snapped, float(raw - snapped)

    # 4a. AAR North American Railroad Band (160.215 - 161.565 MHz: 15 kHz standard & 7.5 kHz NXDN/narrowband)
    # Evaluated BEFORE Marine VHF so 25 kHz Marine grid never steals AAR railroad channels!
    if region in ("US", "CA", "MX") and 160_207_500 <= raw <= 161_572_500:
        aar_base = 160_215_000.0
        aar_15k = int(round(aar_base + round((raw - aar_base) / 15_000.0) * 15_000.0))
        if abs(raw - aar_15k) <= 4_200:
            return aar_15k, float(raw - aar_15k)
        aar_7k5 = int(round(160_207_500.0 + round((raw - 160_207_500.0) / 7_500.0) * 7_500.0))
        if abs(raw - aar_7k5) <= 2_800:
            return aar_7k5, float(raw - aar_7k5)

    # 4b. Marine VHF (25 kHz grid: 156.000-157.425 MHz & 161.600-162.025 MHz in US/CA; full 156-162.025 elsewhere)
    is_marine_vhf = (
        (156_000_000 <= raw <= 157_437_500) or (161_587_500 <= raw <= 162_037_500)
        if region in ("US", "CA", "MX")
        else (156_000_000 <= raw <= 162_037_500)
    )
    if is_marine_vhf:
        step = 25_000.0
        snapped = int(round(raw / step) * step)
        if abs(raw - snapped) <= 8_000:
            return snapped, float(raw - snapped)

    # 4c. Citizens Band (CB) Radio 40-channel 10 kHz grid (26.965 - 27.405 MHz)
    if 26_960_000 <= raw <= 27_410_000:
        cb_freqs = (
            26_965_000, 26_975_000, 26_985_000, 27_005_000, 27_015_000,
            27_025_000, 27_035_000, 27_055_000, 27_065_000, 27_075_000,
            27_085_000, 27_105_000, 27_115_000, 27_125_000, 27_135_000,
            27_155_000, 27_165_000, 27_175_000, 27_185_000, 27_205_000,
            27_215_000, 27_225_000, 27_235_000, 27_245_000, 27_255_000,
            27_265_000, 27_275_000, 27_285_000, 27_295_000, 27_305_000,
            27_315_000, 27_325_000, 27_335_000, 27_345_000, 27_355_000,
            27_365_000, 27_375_000, 27_385_000, 27_395_000, 27_405_000,
        )
        nearest_cb = min(cb_freqs, key=lambda c: abs(c - raw))
        if abs(nearest_cb - raw) <= 4_500:
            return int(nearest_cb), float(raw - nearest_cb)

    # 4d. FCC ATSC Digital Broadcast TV Channels (6.0 MHz channels: Ch 2-4, 5-6, 7-13, 14-36)
    # Snaps pilot carriers (lo + 309.441 kHz) and in-band multiplex peaks to official channel center (lo + 3.0 MHz)
    if region in ("US", "CA", "MX"):
        tv_lo: Optional[int] = None
        if 54_000_000 <= raw < 72_000_000:
            tv_lo = 54_000_000 + int((raw - 54_000_000) // 6_000_000) * 6_000_000
        elif 76_000_000 <= raw < 87_800_000:
            tv_lo = 76_000_000 + int((raw - 76_000_000) // 6_000_000) * 6_000_000
        elif 174_000_000 <= raw < 216_000_000:
            tv_lo = 174_000_000 + int((raw - 174_000_000) // 6_000_000) * 6_000_000
        elif 470_000_000 <= raw < 608_000_000:
            tv_lo = 470_000_000 + int((raw - 470_000_000) // 6_000_000) * 6_000_000
        if tv_lo is not None:
            tv_center = tv_lo + 3_000_000
            return int(tv_center), float(raw - tv_center)

    # 5a. FRS / GMRS UHF Channels (exact 12.5 kHz grid: 462.5500-462.7250 & 467.5500-467.7250 MHz)
    if (462_543_750 <= raw <= 462_731_250) or (467_543_750 <= raw <= 467_731_250):
        step = 12_500.0
        snapped = int(round(raw / step) * step)
        if abs(raw - snapped) <= 4_500:
            return snapped, float(raw - snapped)

    # 5b. FCC Part 90 VHF High Public Safety / Land Mobile / Paging (150.000 - 174.000 MHz: 2.5 kHz / 7.5 kHz raster)
    # Exact common raster for .845, .880, .400, .310, .160, .235, .340, .480 MHz channels!
    if 150_000_000 <= raw <= 174_000_000:
        step = 2_500.0
        snapped = int(round(raw / step) * step)
        return snapped, float(raw - snapped)

    # 5c. FCC Part 90 UHF Land Mobile (450.000 - 470.000 MHz: 6.25 kHz / 12.5 kHz narrowband raster)
    if 450_000_000 <= raw < 470_000_000:
        step = 6_250.0
        snapped = int(round(raw / step) * step)
        if abs(raw - snapped) <= 3_125:
            return snapped, float(raw - snapped)

    # 6. 2m / 1.25m / 70cm Amateur Radio (5 kHz standard repeater/simplex step within crystal tolerance)
    if (144_000_000 <= raw <= 148_000_000) or (222_000_000 <= raw <= 225_000_000) or (420_000_000 <= raw <= 450_000_000):
        step = 5_000.0
        snapped = int(round(raw / step) * step)
        if abs(raw - snapped) <= 2_100:
            return snapped, float(raw - snapped)

    # Default VFO / ISM / Telemetry: preserve sub-10 Hz zoom measurement to nearest 10 Hz!
    snapped = int(round(raw / 10.0) * 10)
    return snapped, float(raw - snapped)


def refine_precision_channel_frequency(
    measured_carrier_hz: float,
    obw_hz: float = 16_000.0,
    iq_mod_hint: str = "",
    region: str = "US",
) -> Tuple[int, float]:
    """
    Fuses a sub-10 Hz (7.63 Hz/bin) multi-pass precision IQ carrier lock with regulatory
    channel rasters so clickable Gqrx links tune dead-center on both channelized broadcasters/repeaters
    and arbitrary VFO / ISM / CW / Amateur frequencies.
    """
    raw = float(measured_carrier_hz)
    snapped, off_hz = snap_to_channel_raster(raw, region=region)

    # Broadcast FM, ATSC TV, NOAA WX, Aviation, Marine, CB, AAR Rail: always honor assigned channel center
    if (
        (87_800_000 <= raw <= 136_975_000)
        or (162_385_000 <= raw <= 162_565_000)
        or (160_207_500 <= raw <= 161_572_500)
        or (26_960_000 <= raw <= 27_410_000)
        or (525_000 <= raw <= 1_705_000)
        or obw_hz >= 65_000.0
    ):
        return snapped, off_hz

    # Narrowband CW / ISM / Telemetry / VFO: if off channel grid by > 1,800 Hz, lock to exact 10 Hz carrier!
    if iq_mod_hint in ("CW", "DIGITAL_BURST") and (
        (314_000_000 <= raw <= 316_000_000)
        or (433_000_000 <= raw <= 434_900_000)
        or (902_000_000 <= raw <= 928_000_000)
        or raw < 144_000_000
    ):
        exact_10hz = int(round(raw / 10.0) * 10)
        return exact_10hz, float(raw - exact_10hz)

    # VHF/UHF Land Mobile: snap to FCC raster when within HackRF crystal PPM tolerance (~2.2 kHz),
    # otherwise lock directly to 10 Hz precision carrier center.
    max_ppm_tol_hz = max(1_600.0, raw * 8.0e-6)  # ~1.2 kHz @ 155 MHz, ~3.7 kHz @ 460 MHz
    if abs(off_hz) <= max_ppm_tol_hz:
        return snapped, off_hz

    exact_10hz = int(round(raw / 10.0) * 10)
    return exact_10hz, float(raw - exact_10hz)


def compute_optimal_hackrf_gains(snr_db: float, peak_dbfs: float) -> Tuple[int, int, int]:
    """
    Computes calibrated HackRF One gains (LNA/IF, VGA/BB, AMP/RF) in valid hardware steps:
      - LNA (IF): 0..40 dB in 8 dB steps
      - VGA (BB): 0..62 dB in 2 dB steps
      - AMP (RF): 0 or 14 dB
    Prevents ADC clipping/IMD on booming local broadcasters while maximizing sensitivity on weak signals.
    """
    if peak_dbfs >= -34.0 or snr_db >= 34.0:
        return 16, 16, 0
    if peak_dbfs >= -48.0 or snr_db >= 22.0:
        return 24, 20, 0
    if peak_dbfs >= -60.0 or snr_db >= 14.0:
        return 32, 24, 0
    if snr_db >= 8.0:
        return 32, 30, 0
    return 40, 34, 14


def normalize_gqrx_waterfall_bounds(plot_min: float, plot_max: float) -> Tuple[int, int]:
    """
    Normalizes pandapter & waterfall floor/ceiling dBFS ranges to Gqrx 2.17.6's 8192-bin
    Hann-windowed FFT scale on HackRF One (where thermal noise floor sits around -94..-85 dBFS
    and active carriers sit around -65..-20 dBFS). Idempotent across repeated calls.
    """
    p_min = float(plot_min)
    p_max = float(plot_max)
    if p_min > -75.0:
        wf_min = max(-100, min(-84, int(round(p_min - 38.0))))
        wf_max = max(-42, min(-18, int(round(p_max - 18.0))))
    else:
        wf_min = max(-100, min(-82, int(round(p_min))))
        wf_max = max(-42, min(-18, int(round(p_max))))
    if wf_max < wf_min + 48:
        wf_max = min(-18, wf_min + 52)
    return wf_min, wf_max


def evaluate_signal_demod_feasibility(
    freq_hz: float,
    snr_db: float,
    measured_bw_hz: float,
    peak_dbfs: float = -45.0,
    noise_floor_dbfs: float = -78.0,
    iq_mod_hint: str = "",
    demod_override: str = "AUTO (Band + AI)",
    squelch_mode: str = "OPEN",
    region: str = "US",
    stereo_pilot: Optional[bool] = None,
    ctcss_hz: float = 0.0,
    freq_dev_hz: float = 0.0,
) -> Dict[str, Any]:
    """
    Evaluates whether a detected signal is legal & viable to demodulate, and translates
    its RF signature into exact Gqrx 2.17.6 receiver, filter, gain, squelch, and FFT display settings.
    """
    _ = ctcss_hz
    band = find_band_definition(freq_hz, region)

    if band:
        band_name = band.band_name
        service = band.service_type
        demod = band.primary_demod
        is_legal = band.is_legal_demod
        legal_notes = band.legal_notes
        category = band.profile_category
        filt_low = band.filter_low_cut
        filt_high = band.filter_high_cut
        maxdev = band.fm_maxdev_hz
    else:
        if freq_hz < 30_000_000:
            band_name = "HF Shortwave / Utility"
            service = "HF Communications"
            demod = "AM" if measured_bw_hz >= 5_500 else "USB"
            category = "Amateur_Radio"
        elif measured_bw_hz >= 75_000:
            band_name = f"Wideband VHF/UHF ({freq_hz / 1e6:.3f} MHz)"
            service = "Wideband Audio / Multiplex"
            demod = "WFM (stereo)"
            category = "Broadcast_Audio"
        elif iq_mod_hint == "AM":
            band_name = f"AM Carrier Channel ({freq_hz / 1e6:.3f} MHz)"
            service = "Amplitude Modulated Voice/Beacon"
            demod = "AM"
            category = "Aviation_Airband"
        else:
            band_name = f"VHF/UHF Narrowband ({freq_hz / 1e6:.3f} MHz)"
            service = "Narrowband FM Channel"
            demod = "Narrow FM"
            category = "Public_Safety"

        _, _, filt_low, filt_high, maxdev = GQRX_DEMOD_SPECS[demod]
        is_legal = True
        legal_notes = "Open civilian reception"

    # Refine AUTO demodulation using measured IQ modulation signature, stereo pilot & occupied bandwidth
    if demod_override and demod_override != "AUTO (Band + AI)" and demod_override in GQRX_DEMOD_SPECS:
        demod = demod_override
        _, _, filt_low, filt_high, maxdev = GQRX_DEMOD_SPECS[demod]
    else:
        if demod == "WFM (stereo)" and stereo_pilot is False and snr_db < 14.5:
            # Weak distant FM DX without 19 kHz pilot: drop to mono for 20 dB cleaner audio SNR
            demod = "WFM (mono)"
            _, _, filt_low, filt_high, maxdev = GQRX_DEMOD_SPECS[demod]
        elif demod == "Narrow FM" and measured_bw_hz > 85_000 and not (87.8e6 <= freq_hz <= 108.1e6):
            demod = "WFM (mono)"
            _, _, filt_low, filt_high, maxdev = GQRX_DEMOD_SPECS[demod]
        elif demod == "Narrow FM" and iq_mod_hint == "CW" and measured_bw_hz < 1_500:
            demod = "CW-U"
            _, _, filt_low, filt_high, maxdev = GQRX_DEMOD_SPECS[demod]
        elif demod == "Narrow FM":
            # Carson's rule passband tailoring: 12.5 kHz narrowband LMR (+-5.5 kHz, dev 2.5 kHz)
            # vs 25 kHz wideband NOAA/Marine/Amateur (+-8.0 kHz, dev 5.0 kHz)
            is_25k_alloc = (162_385_000 <= freq_hz <= 162_565_000) or (156_000_000 <= freq_hz <= 157_425_000) or (144_000_000 <= freq_hz <= 148_000_000)
            if not is_25k_alloc and measured_bw_hz <= 13_500 and (freq_dev_hz <= 3_200 or freq_dev_hz == 0.0):
                filt_low, filt_high, maxdev = -5_500, 5_500, 2_500
            elif is_25k_alloc or measured_bw_hz >= 18_000 or freq_dev_hz >= 3_800:
                filt_low, filt_high, maxdev = -8_000, 8_000, 5_000
        elif demod == "AM" and (108_000_000 <= freq_hz <= 137_000_000 or 26_960_000 <= freq_hz <= 27_410_000):
            filt_low, filt_high = -4_800, 4_800

    rigctl_mode, passband_hz, spec_low, spec_high, spec_maxdev = GQRX_DEMOD_SPECS.get(
        demod, GQRX_DEMOD_SPECS["Narrow FM"]
    )
    if demod_override and demod_override != "AUTO (Band + AI)":
        filt_low, filt_high, maxdev = spec_low, spec_high, spec_maxdev

    lna_gain, vga_gain, amp_gain = compute_optimal_hackrf_gains(snr_db, peak_dbfs)

    # Calibrated Gqrx 2.17.6 8192-pt Hann FFT Pandapter & Waterfall dBFS floor/ceiling
    # Coherent 8192-pt FFT lowers white thermal noise by 10*log10(8192/256) ≈ 15.1 dB vs 256-pt STFT
    # while keeping carrier peaks near -65..-20 dBFS at calibrated HackRF IF/BB gains.
    est_gqrx_floor = min(-86.0, noise_floor_dbfs - 26.0) if noise_floor_dbfs > -68.0 else (noise_floor_dbfs - 15.0)
    est_gqrx_ceil = max(-36.0, min(-18.0, -44.0 + snr_db * 0.75))
    fft_floor, fft_ceil = normalize_gqrx_waterfall_bounds(est_gqrx_floor - 4.0, est_gqrx_ceil)

    # Squelch calculation calibrated to Gqrx's post-channel-filter 'l STRENGTH' dBFS detector
    if squelch_mode == "AUTO" and "WFM" not in demod:
        gqrx_chan_floor = min(-68.0, noise_floor_dbfs - 22.0) if noise_floor_dbfs > -68.0 else (noise_floor_dbfs + 4.0)
        sql_level = max(-105.0, min(-52.0, round(gqrx_chan_floor + 6.0, 1)))
    elif squelch_mode.startswith("CUSTOM:"):
        try:
            sql_level = float(squelch_mode.split(":", 1)[1])
        except Exception:
            sql_level = -150.0
    else:
        # OPEN (-150 dBFS): audio plays live immediately upon Gqrx launch
        sql_level = -150.0

    return {
        "freq_hz": int(freq_hz),
        "freq_mhz": round(freq_hz / 1e6, 4),
        "band_name": band_name,
        "service_type": service,
        "demod": demod,
        "rigctl_mode": rigctl_mode,
        "passband_hz": passband_hz,
        "filter_low_cut": int(filt_low),
        "filter_high_cut": int(filt_high),
        "fm_maxdev": int(maxdev),
        "sql_level": float(sql_level),
        "lna_gain": int(lna_gain),
        "vga_gain": int(vga_gain),
        "amp_gain": int(amp_gain),
        "pandapter_min_db": int(fft_floor),
        "pandapter_max_db": int(fft_ceil),
        "is_legal": bool(is_legal),
        "legal_notes": legal_notes,
        "profile_category": category,
    }


def classify_voice_broadcast(
    freq_hz: float,
    demod: str = "",
    band_name: str = "",
    service_type: str = "",
    pic_label: str = "",
    profile_category: str = "",
    obw_hz: float = 0.0,
) -> Dict[str, Any]:
    """
    Identifies voice broadcast channels and general voice transmissions across the RF spectrum.
    Returns structured categorization, prominent visual label with emoji badge, tag, and UI colors.
    """
    f_hz = float(freq_hz)

    # 0. Explicit Data, Telemetry, and Beacon allocations (Non-Voice)
    data_keywords = ("telemetry", "transponder", "satellite downlink", "ads-b", "lora", "sensor", "meshtastic")
    if profile_category in ("ISM_Telemetry", "Telemetry", "Data") or any(
        k in band_name.lower() or k in service_type.lower() for k in data_keywords
    ):
        return {
            "is_voice_broadcast": False,
            "is_any_voice": False,
            "voice_label": "[📊 DATA / TELEM]",
            "short_label": "DATA/TELEM",
            "voice_tag": "Data_Telemetry",
            "color": "#64748b",
            "bg": "#1e293b",
        }

    # 0a. ATSC Digital Broadcast Television (6 MHz Digital Multiplex)
    if profile_category == "Broadcast_TV" or any(k in band_name.upper() for k in ("BROADCAST TV", "ATSC", "DIGITAL TV")):
        return {
            "is_voice_broadcast": False,
            "is_any_voice": False,
            "voice_label": "[📺 BROADCAST TV]",
            "short_label": "BROADCAST TV",
            "voice_tag": "Broadcast_TV",
            "color": "#e11d48",  # rose / red
            "bg": "#4c0519",
        }

    # 0b. Aviation ADS-B Mode-S Transponders (1090 MHz & 978 MHz UAT)
    if (1_088_000_000 <= f_hz <= 1_092_000_000) or abs(f_hz - 978_000_000) <= 1_000_000 or (f_hz >= 960_000_000 and "ADS-B" in pic_label.upper()):
        return {
            "is_voice_broadcast": False,
            "is_any_voice": False,
            "voice_label": "[📡 ADS-B TELEMETRY]",
            "short_label": "ADS-B",
            "voice_tag": "Data_Telemetry",
            "color": "#38bdf8",  # sky blue
            "bg": "#082f49",
        }

    # 0c. Commercial FM Broadcast (87.5 - 108.0 MHz, OIRT 65.8 - 74.0 MHz) - Checked BEFORE Pager/LMR!
    if (87_500_000 <= f_hz <= 108_100_000) or (65_800_000 <= f_hz <= 74_000_000):
        return {
            "is_voice_broadcast": True,
            "is_any_voice": True,
            "voice_label": "[📻 VOICE BROADCAST]",
            "short_label": "VOICE BROADCAST",
            "voice_tag": "Voice_Broadcast",
            "color": "#10b981",  # emerald green
            "bg": "#064e3b",
        }

    # 0d. POCSAG / FLEX Digital Pager Channels (Strictly VHF/UHF/900MHz Paging Allocations >= 138 MHz)
    pocsag_freqs = (152_007_500, 152_180_000, 157_740_000, 158_100_000, 158_700_000, 454_025_000, 454_100_000, 459_000_000)
    if f_hz >= 138_000_000 and (
        any(abs(f_hz - pf) <= 6_000 for pf in pocsag_freqs)
        or pic_label.upper().startswith("PAGER-")
        or "POCSAG" in band_name.upper()
    ):
        return {
            "is_voice_broadcast": False,
            "is_any_voice": False,
            "voice_label": "[📟 POCSAG / PAGER]",
            "short_label": "POCSAG PAGER",
            "voice_tag": "Data_Telemetry",
            "color": "#06b6d4",
            "bg": "#083344",
        }

    # 1. Fire & EMS Dispatch & Operations (Penrose, Florence, Cañon City, Deer Mtn)
    fire_freqs = (155_880_000, 154_400_000, 154_310_000, 154_235_000, 154_370_000, 154_280_000, 154_265_000, 154_295_000, 154_070_000)
    if any(abs(f_hz - ff) <= 6_000 for ff in fire_freqs) or any(k in band_name.upper() for k in ("PENROSE FIRE", "FIRE DISPATCH", "FIRE DEPT", "FIRE OPS")):
        return {
            "is_voice_broadcast": False,
            "is_any_voice": True,
            "voice_label": "[🚒 FIRE / EMS DISPATCH]",
            "short_label": "FIRE/EMS",
            "voice_tag": "Fire_EMS",
            "color": "#ef4444",  # red
            "bg": "#450a0a",
        }

    # 2. Sheriff & Law Enforcement (Fremont County Sheriff VHF, State Patrol, CLEER)
    sheriff_freqs = (154_845_000, 155_010_000, 155_475_000, 154_950_000)
    if any(abs(f_hz - sf) <= 6_000 for sf in sheriff_freqs) or any(k in band_name.upper() for k in ("SHERIFF", "POLICE", "LAW ENFORCEMENT")):
        return {
            "is_voice_broadcast": False,
            "is_any_voice": True,
            "voice_label": "[🚓 SHERIFF / POLICE]",
            "short_label": "SHERIFF",
            "voice_tag": "Sheriff_Police",
            "color": "#3b82f6",  # blue
            "bg": "#172554",
        }

    # 3. Search and Rescue (Colorado SAR MRA-1, MRA-2, CO VSAR)
    sar_freqs = (155_160_000, 155_235_000, 155_175_000, 155_205_000, 155_220_000)
    if any(abs(f_hz - sar_f) <= 6_000 for sar_f in sar_freqs) or any(k in band_name.upper() for k in ("SAR", "SEARCH & RESCUE", "SEARCH AND RESCUE", "VSAR")):
        return {
            "is_voice_broadcast": False,
            "is_any_voice": True,
            "voice_label": "[🏔️ SEARCH & RESCUE]",
            "short_label": "SEARCH & RESCUE",
            "voice_tag": "Search_Rescue",
            "color": "#f97316",  # orange
            "bg": "#431407",
        }

    # 4. Medevac & Hospital Interop (CHERN Med-9, Med-10, Flight for Life Air-to-Ground)
    med_freqs = (155_340_000, 155_400_000, 123_025_000, 123_050_000, 155_280_000)
    if any(abs(f_hz - mf) <= 6_000 for mf in med_freqs) or any(k in band_name.upper() for k in ("CHERN", "MEDEVAC", "FLIGHT FOR LIFE", "HOSPITAL")):
        return {
            "is_voice_broadcast": False,
            "is_any_voice": True,
            "voice_label": "[🚑 MEDEVAC / HOSPITAL]",
            "short_label": "MEDEVAC",
            "voice_tag": "Medevac_Hospital",
            "color": "#ec4899",  # pink
            "bg": "#500724",
        }

    # 5. Trucker CB Traffic & 2-Way CB (26.965 - 27.405 MHz AM)
    if 26_965_000 <= f_hz <= 27_405_000:
        if abs(f_hz - 27_185_000) <= 6_000:
            lbl = "[🚛 TRUCKER CH 19 HWY]"
        elif abs(f_hz - 27_065_000) <= 6_000:
            lbl = "[🚨 CB CH 09 EMERGENCY]"
        elif abs(f_hz - 27_165_000) <= 6_000:
            lbl = "[🚛 TRUCKER CH 17 N/S]"
        else:
            lbl = "[🚛 TRUCKER CB RADIO]"
        return {
            "is_voice_broadcast": False,
            "is_any_voice": True,
            "voice_label": lbl,
            "short_label": "TRUCKER CB",
            "voice_tag": "Trucker_CB",
            "color": "#c084fc",  # purple
            "bg": "#3b0764",
        }

    # 6. Railroad Road & Yard Operations (AAR Channels 160.215 - 161.565 MHz)
    rail_freqs = (161_490_000, 160_590_000, 160_425_000, 161_550_000, 161_100_000, 160_890_000)
    if (160_215_000 <= f_hz <= 161_565_000 and any(abs(f_hz - rf) <= 6_000 for rf in rail_freqs)) or any(
        k in band_name.upper() for k in ("RAILROAD", "UNION PACIFIC", "ROYAL GORGE", "AAR", "JOINT LINE")
    ):
        return {
            "is_voice_broadcast": False,
            "is_any_voice": True,
            "voice_label": "[🚆 RAILROAD ROAD/YARD]",
            "short_label": "RAILROAD",
            "voice_tag": "Railroad_Radio",
            "color": "#eab308",  # gold / yellow
            "bg": "#422006",
        }

    # 1. Commercial FM Broadcast (87.5 - 108.0 MHz, OIRT 65.8 - 74.0 MHz)
    # Primary public voice/audio broadcast
    if (87_500_000 <= f_hz <= 108_100_000) or (65_800_000 <= f_hz <= 74_000_000) or ("WFM" in demod and obw_hz >= 75_000):
        return {
            "is_voice_broadcast": True,
            "is_any_voice": True,
            "voice_label": "[📻 VOICE BROADCAST]",
            "short_label": "VOICE BROADCAST",
            "voice_tag": "Voice_Broadcast",
            "color": "#10b981",  # emerald green
            "bg": "#064e3b",
        }

    # 2. Aviation Automated Terminal Information / Weather Voice Broadcast (ATIS / AWOS)
    if (118_000_000 <= f_hz <= 136_975_000) and any(k in band_name.upper() or k in pic_label.upper() for k in ("ATIS", "AWOS", "ASOS")):
        return {
            "is_voice_broadcast": True,
            "is_any_voice": True,
            "voice_label": "[🎙️ ATIS VOICE BROADCAST]",
            "short_label": "ATIS BROADCAST",
            "voice_tag": "Voice_Broadcast",
            "color": "#f59e0b",  # amber
            "bg": "#451a03",
        }

    # 3. NOAA All-Hazards Emergency Weather Radio (162.400 - 162.550 MHz)
    # Continuous 24/7 automated voice broadcast
    if (162_385_000 <= f_hz <= 162_565_000) or ("NOAA" in band_name.upper()) or ("NOAA" in service_type.upper()) or (f_hz > 160_000_000 and "WEATHER" in band_name.upper()) or "WX" in pic_label.upper():
        return {
            "is_voice_broadcast": True,
            "is_any_voice": True,
            "voice_label": "[🎙️ WX VOICE BROADCAST]",
            "short_label": "WX BROADCAST",
            "voice_tag": "Voice_Broadcast",
            "color": "#06b6d4",  # cyan
            "bg": "#083344",
        }

    # 4. Medium-Wave AM Commercial Broadcast (520 - 1720 kHz)
    if 520_000 <= f_hz <= 1_720_000:
        return {
            "is_voice_broadcast": True,
            "is_any_voice": True,
            "voice_label": "[📻 AM VOICE BROADCAST]",
            "short_label": "AM BROADCAST",
            "voice_tag": "Voice_Broadcast",
            "color": "#10b981",
            "bg": "#064e3b",
        }

    # 5. Shortwave International Broadcast Bands
    sw_ranges = [
        (3_900_000, 4_000_000), (5_900_000, 6_200_000), (7_200_000, 7_450_000),
        (9_400_000, 9_900_000), (11_600_000, 12_100_000), (13_570_000, 13_870_000),
        (15_100_000, 15_800_000), (17_480_000, 17_900_000), (21_450_000, 21_850_000),
    ]
    if any(lo <= f_hz <= hi for lo, hi in sw_ranges) and demod in ("AM", "AM-Sync", "USB", "LSB"):
        return {
            "is_voice_broadcast": True,
            "is_any_voice": True,
            "voice_label": "[📻 SW VOICE BROADCAST]",
            "short_label": "SW BROADCAST",
            "voice_tag": "Voice_Broadcast",
            "color": "#10b981",
            "bg": "#064e3b",
        }

    # 6. Civil & Military Aviation Voice (Air Traffic Control)
    if (118_000_000 <= f_hz <= 136_975_000) or (225_000_000 <= f_hz <= 380_000_000 and "AM" in demod):
        return {
            "is_voice_broadcast": False,
            "is_any_voice": True,
            "voice_label": "[✈️ AIR VOICE (ATC)]",
            "short_label": "AIR VOICE",
            "voice_tag": "Aviation_Voice",
            "color": "#f59e0b",
            "bg": "#451a03",
        }

    # 7. Marine VHF Voice
    if 156_000_000 <= f_hz <= 162_025_000 and "Narrow FM" in demod:
        return {
            "is_voice_broadcast": False,
            "is_any_voice": True,
            "voice_label": "[⚓ MARINE VOICE]",
            "short_label": "MARINE VOICE",
            "voice_tag": "Marine_Voice",
            "color": "#06b6d4",
            "bg": "#083344",
        }

    # 8. Civilian Two-Way Voice (CB Radio 27MHz, FRS/GMRS 462/467MHz, MURS 151MHz)
    if (26_965_000 <= f_hz <= 27_405_000) or (462_550_000 <= f_hz <= 467_725_000) or (151_820_000 <= f_hz <= 151_940_000):
        return {
            "is_voice_broadcast": False,
            "is_any_voice": True,
            "voice_label": "[🗣️ 2-WAY VOICE]",
            "short_label": "2-WAY VOICE",
            "voice_tag": "TwoWay_Voice",
            "color": "#a855f7",
            "bg": "#3b0764",
        }

    # 9. Amateur Radio Voice
    ham_voice_ranges = [
        (28_300_000, 29_700_000), (50_100_000, 54_000_000), (144_200_000, 148_000_000),
        (222_000_000, 225_000_000), (440_000_000, 450_000_000),
    ]
    if any(lo <= f_hz <= hi for lo, hi in ham_voice_ranges) and demod in ("Narrow FM", "AM", "USB", "LSB"):
        return {
            "is_voice_broadcast": False,
            "is_any_voice": True,
            "voice_label": "[🗣️ HAM VOICE]",
            "short_label": "HAM VOICE",
            "voice_tag": "Amateur_Voice",
            "color": "#c084fc",
            "bg": "#3b0764",
        }

    # 10. Exotic Pictogram / Waterfall Art
    if pic_label.startswith("PICTO_") or "Pictogram" in pic_label or "Glyph" in pic_label:
        return {
            "is_voice_broadcast": False,
            "is_any_voice": False,
            "voice_label": "[🎨 PICTOGRAM]",
            "short_label": "PICTOGRAM",
            "voice_tag": "Pictogram_Exotic",
            "color": "#ec4899",
            "bg": "#500724",
        }

    # 11. Generic voice channel if standard voice demodulation
    if demod in ("Narrow FM", "AM", "USB", "LSB") and obw_hz <= 30_000:
        return {
            "is_voice_broadcast": False,
            "is_any_voice": True,
            "voice_label": "[🗣️ VOICE CHANNEL]",
            "short_label": "VOICE CH",
            "voice_tag": "Voice_Channel",
            "color": "#38bdf8",
            "bg": "#0c4a6e",
        }

    # 12. Unclassified Data / Carrier
    return {
        "is_voice_broadcast": False,
        "is_any_voice": False,
        "voice_label": "[📊 DATA / TELEM]",
        "short_label": "DATA/TELEM",
        "voice_tag": "Data_Telemetry",
        "color": "#64748b",
        "bg": "#1e293b",
    }

