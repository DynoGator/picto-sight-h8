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
    "Broadcast_Audio":   "#10b981",
    "Aviation_Airband":  "#f59e0b",
    "Emergency_Weather": "#ef4444",
    "Amateur_Radio":     "#a855f7",
    "Marine_VHF":        "#06b6d4",
    "Public_Safety":     "#3b82f6",
    "Pictogram_Exotic":  "#ec4899",
    "ISM_Telemetry":     "#eab308",
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
        start_hz=450_000_000, end_hz=512_000_000,
        band_name="UHF Public Safety & Commercial LMR", service_type="Dispatch / Transit / Utilities",
        primary_demod="Narrow FM", channel_step_hz=12_500, default_bandwidth_hz=12_500,
        filter_low_cut=-5_500, filter_high_cut=5_500, fm_maxdev_hz=2_500,
        is_legal_demod=True, legal_notes="Unencrypted analog LMR channels legal to monitor",
        profile_category="Public_Safety",
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

    # 4. Marine VHF (25 kHz grid)
    if 156_000_000 <= raw <= 162_025_000:
        step = 25_000.0
        snapped = int(round(raw / step) * step)
        if abs(raw - snapped) <= 8_000:
            return snapped, float(raw - snapped)

    # 5. FRS / GMRS & UHF/VHF Land Mobile (12.5 kHz grid)
    if (462_550_000 <= raw <= 467_725_000) or (150_000_000 <= raw <= 174_000_000) or (450_000_000 <= raw <= 512_000_000):
        step = 12_500.0
        snapped = int(round(raw / step) * step)
        if abs(raw - snapped) <= 4_500:
            return snapped, float(raw - snapped)

    # 6. 2m / 70cm Amateur Radio (5 kHz standard tuning step)
    if (144_000_000 <= raw <= 148_000_000) or (420_000_000 <= raw <= 450_000_000):
        step = 5_000.0
        snapped = int(round(raw / step) * step)
        if abs(raw - snapped) <= 2_500:
            return snapped, float(raw - snapped)

    # Default: round cleanly to nearest 100 Hz
    snapped = int(round(raw / 100.0) * 100)
    return snapped, float(raw - snapped)


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
) -> Dict[str, Any]:
    """
    Evaluates whether a detected signal is legal & viable to demodulate, and translates
    its RF signature into exact Gqrx 2.17.6 receiver, filter, gain, squelch, and FFT display settings.
    """
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

    # Refine AUTO demodulation using measured IQ modulation signature & occupied bandwidth
    if demod_override and demod_override != "AUTO (Band + AI)" and demod_override in GQRX_DEMOD_SPECS:
        demod = demod_override
        _, _, filt_low, filt_high, maxdev = GQRX_DEMOD_SPECS[demod]
    else:
        if demod == "Narrow FM" and measured_bw_hz > 85_000 and not (87.8e6 <= freq_hz <= 108.1e6):
            demod = "WFM (mono)"
            _, _, filt_low, filt_high, maxdev = GQRX_DEMOD_SPECS[demod]
        elif demod == "Narrow FM" and iq_mod_hint == "CW" and measured_bw_hz < 1_500:
            demod = "CW-U"
            _, _, filt_low, filt_high, maxdev = GQRX_DEMOD_SPECS[demod]

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
