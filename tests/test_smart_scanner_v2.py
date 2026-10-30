"""
Comprehensive Automated Test Suite for AI-Powered Smart RF Scanner v2
Tests:
  1. Channel raster snapping, demod translation & calibrated HackRF gain computation
  2. GPS Maidenhead grid locator & reboot-persistent locality caching
  3. 1-at-a-time SDRResourceArbiter state machine & Gqrx crash-flag sanitization
  4. ScanConfig modes (QUICK, DEEP, CUSTOM) & 20th-percentile spike extraction
  5. Multi-frame (>=3 frames per 1 MHz) Hailo-8 AI Pictogram inference & RGB strip rendering
  6. Permanent Location Profile persistence, reload & spec-compliant two-table Gqrx bookmarks.csv
  7. Native QSettings Gqrx 2.17.6 profile generation (QVariantMap tenths-of-dB gains, waterfall dB ranges)
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import numpy as np
from PyQt6.QtCore import QSettings

from core.bandplans import (
    SUPPORTED_DEMOD_MODES,
    compute_optimal_hackrf_gains,
    evaluate_signal_demod_feasibility,
    normalize_gqrx_waterfall_bounds,
    snap_to_channel_raster,
)
from core.gps_engine import coords_to_maidenhead, get_gps_manager
from core.gqrx_controller import GqrxController
from core.hackrf_engine import HackRFScannerEngine, ScanConfig, is_likely_internal_clock_spur
from core.pictogram_engine import get_pictogram_classifier
from core.profile_organizer import ClassifiedSignalProfile, SignalProfileOrganizer
from core.scanner_pipeline import MultiLayerScannerPipeline
from core.sdr_arbiter import (
    detect_hackrf_sysfs,
    format_osmosdr_hackrf_device_arg,
    get_sdr_arbiter,
    sanitize_gqrx_crash_flags,
)


def test_channel_raster_and_demod_translation() -> None:
    # US FM odd-tenth 200 kHz snapping
    snapped_fm, off_fm = snap_to_channel_raster(107_874_000, region="US")
    assert snapped_fm == 107_900_000
    assert abs(off_fm) < 30_000

    # NOAA exact 7-channel snapping
    snapped_wx, _ = snap_to_channel_raster(162_546_200, region="US")
    assert snapped_wx == 162_550_000

    # Cellular band restricted under US ECPA
    cell = evaluate_signal_demod_feasibility(880_000_000, snr_db=25.0, measured_bw_hz=200_000)
    assert cell["is_legal"] is False

    # Demod override verification for all supported modes
    for mode in SUPPORTED_DEMOD_MODES:
        res = evaluate_signal_demod_feasibility(
            146_520_000,
            snr_db=18.0,
            measured_bw_hz=14_000,
            demod_override=mode,
            squelch_mode="AUTO",
        )
        assert res["lna_gain"] in (0, 8, 16, 24, 32, 40)
        assert res["vga_gain"] % 2 == 0
        assert res["amp_gain"] in (0, 14)


def test_calibrated_gain_curve() -> None:
    # Strong local signal must NOT max out AMP/LNA
    lna_hi, vga_hi, amp_hi = compute_optimal_hackrf_gains(snr_db=35.0, peak_dbfs=-32.0)
    assert amp_hi == 0 and lna_hi <= 24

    # Weak DX signal increases LNA/VGA/AMP
    lna_lo, vga_lo, amp_lo = compute_optimal_hackrf_gains(snr_db=6.0, peak_dbfs=-78.0)
    assert lna_lo == 40 and amp_lo == 14 and vga_lo >= 30


def test_gps_maidenhead_and_fix() -> None:
    grid = coords_to_maidenhead(39.7392, -104.9903)
    assert grid.startswith("DM79")
    fix = get_gps_manager().get_current_fix()
    assert len(fix.maidenhead_grid) == 6
    assert fix.fix_quality_pct > 0.0
    assert len(fix.local_datetime_approx) >= 10


def test_scan_modes_and_layer3_min_frames() -> None:
    q_cfg = ScanConfig.from_mode("QUICK", 88, 108)
    d_cfg = ScanConfig.from_mode("DEEP", 88, 108)
    c_cfg = ScanConfig.from_mode("CUSTOM", 118, 137, l3_frames_per_mhz=6, min_snr_db=12.0, min_peak_dbfs=-65.0)

    assert q_cfg.l3_frames_per_mhz >= 3
    assert d_cfg.l3_frames_per_mhz >= 5
    assert c_cfg.l3_frames_per_mhz == 6
    assert c_cfg.min_snr_db == 12.0
    assert c_cfg.min_peak_dbfs == -65.0


def test_hailo_multiframe_pictogram_classifier() -> None:
    mgr = get_pictogram_classifier()
    rng = np.random.default_rng(7)
    frames = [rng.normal(-80.0, 2.0, size=(64, 128)).astype(np.float32) for _ in range(3)]
    for f in frames:
        f[:, 56:72] = -42.0
    res = mgr.analyze_spectrogram_frames(
        center_freq_hz=107_900_000,
        frames=frames,
        bandwidth_hz=1_000_000.0,
        iq_metrics={"snr_db": 28.0, "obw_hz": 185_000.0, "iq_mod_hint": "WFM", "duty_cycle": 1.0},
    )
    assert res.frames_evaluated == 3
    assert res.num_rois_analyzed >= 3
    assert res.composite_rgb is not None and res.composite_rgb.shape[2] == 3


def test_permanent_location_profiles_and_gqrx_qsettings() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        exp_dir = os.path.join(tmpdir, "export")
        prof_dir = os.path.join(tmpdir, "profiles")
        org = SignalProfileOrganizer(export_dir=exp_dir, profiles_dir=prof_dir)

        pipe = MultiLayerScannerPipeline()
        pipe.organizer = org
        cfg = ScanConfig.from_mode("QUICK", 88, 165, min_snr_db=8.0, min_peak_dbfs=-78.0)
        cfg.simulate = True

        profiles, saved_loc = pipe.run_scan_with_config(cfg)
        assert len(profiles) >= 4
        assert saved_loc is not None
        assert os.path.exists(saved_loc.json_path)
        assert os.path.exists(saved_loc.bookmarks_csv_path)

        # Verify strict ascending numerical frequency order
        freqs = [p.frequency_hz for p in profiles]
        assert freqs == sorted(freqs)

        # Verify reload from permanent disk file
        reloaded = org.load_location_profile(saved_loc.json_path)
        assert reloaded is not None
        assert len(reloaded.signals) == len(profiles)

        # Verify two-section Gqrx 2.17.6 bookmarks.csv compliance
        bm_text = Path(saved_loc.bookmarks_csv_path).read_text(encoding="utf-8")
        assert "# Tag name ; color" in bm_text
        assert "# Frequency ; Name ; Modulation ; Bandwidth ; Tags" in bm_text
        for line in bm_text.splitlines():
            if line and not line.startswith("#") and ";" in line:
                cols = [c.strip() for c in line.split(";")]
                assert len(cols) in (2, 5)

        # Verify native QSettings Gqrx .conf translation
        conf_file = Path(tmpdir) / "test_gqrx.conf"
        ctrl = GqrxController(conf_path=conf_file)
        named_conf = ctrl.generate_profile(profiles[0])
        qs = QSettings(str(named_conf), QSettings.Format.IniFormat)
        assert str(qs.value("crashed")).lower() == "false"
        assert int(qs.value("input/frequency")) == profiles[0].frequency_hz
        gains_map = qs.value("input/gains")
        assert isinstance(gains_map, dict)
        assert int(gains_map["IF"]) == profiles[0].lna_gain * 10
        assert int(gains_map["BB"]) == profiles[0].vga_gain * 10
        assert int(qs.value("fft/pandapter_min_db")) == int(profiles[0].plot_min)
        assert int(qs.value("fft/waterfall_max_db")) == int(profiles[0].plot_max)
        assert str(qs.value("fft/markers")).lower() == "true"

        # Clean up named conf generated in ~/.config/gqrx
        if named_conf.exists():
            named_conf.unlink()


def test_sdr_arbiter_snapshot() -> None:
    arb = get_sdr_arbiter()
    sanitize_gqrx_crash_flags()
    snap = arb.get_status_snapshot()
    assert snap.owner in ("IDLE", "GQRX_LIVE", "FAULT")


def test_osmosdr_hackrf_device_arg_and_sysfs() -> None:
    assert format_osmosdr_hackrf_device_arg("0000000000000000010961dc2a7c5f4f") == "hackrf=7c5f4f"
    assert format_osmosdr_hackrf_device_arg("") == "hackrf=0"
    present, ser, bus, dev = detect_hackrf_sysfs()
    assert isinstance(present, bool)
    if present:
        assert len(ser) >= 6
        assert bus > 0 and dev > 0


def test_legacy_waterfall_normalization_and_auto_squelch() -> None:
    # Legacy 256-bin STFT range (-55..-4) normalizes cleanly & idempotently to Gqrx 8192-FFT scale
    wf_min, wf_max = normalize_gqrx_waterfall_bounds(-55.0, -4.0)
    assert -110 <= wf_min <= -82
    assert -45 <= wf_max <= -15
    assert normalize_gqrx_waterfall_bounds(wf_min, wf_max) == (wf_min, wf_max)

    # AUTO squelch on Layer 3 STFT noise floor (-41 dBFS) must calibrate to Gqrx channel filter (~-65 dBFS)
    feas = evaluate_signal_demod_feasibility(
        120_000_000,
        snr_db=18.0,
        measured_bw_hz=10_000,
        peak_dbfs=-23.0,
        noise_floor_dbfs=-41.0,
        demod_override="AM",
        squelch_mode="AUTO",
    )
    assert -85.0 <= feas["sql_level"] <= -48.0
    assert feas["pandapter_min_db"] <= -80


def test_gqrx_v21_adaptive_decimation_audio_and_crash_sanitization() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        conf_path = Path(tmpdir) / "SmartScanner_Active.conf"
        # Seed geometry, crashed=true, muted audio=-60, and stale output/device=default
        seed = QSettings(str(conf_path), QSettings.Format.IniFormat)
        seed.setValue("crashed", True)
        seed.setValue("audio/gain", -60)
        seed.setValue("output/device", "default")
        seed.setValue("gui/geometry", "TEST_GEOM_BYTES")
        seed.sync()

        sanitize_gqrx_crash_flags(extra_conf=conf_path)
        chk = QSettings(str(conf_path), QSettings.Format.IniFormat)
        assert str(chk.value("crashed")).lower() == "false"
        assert int(chk.value("audio/gain")) == 0
        assert not chk.contains("output/device")

        ctrl = GqrxController(conf_path=conf_path)

        # 1. Wideband FM (2.4 MSPS, decimation=1)
        wfm_feas = evaluate_signal_demod_feasibility(104_300_000, 26.0, 180_000, -18.0, -42.0)
        wfm_prof = ClassifiedSignalProfile.from_dict(
            {
                "frequency_hz": 104_300_000,
                "frequency_mhz": 104.3,
                "signal_name": "104.3 FM",
                "profile_category": "Broadcast_Audio",
                "modulation": wfm_feas["demod"],
                "rigctl_mode": wfm_feas["rigctl_mode"],
                "passband_hz": wfm_feas["passband_hz"],
                "filter_low_cut": wfm_feas["filter_low_cut"],
                "filter_high_cut": wfm_feas["filter_high_cut"],
                "fm_maxdev": wfm_feas["fm_maxdev"],
                "sql_level": -150.0,
                "bandwidth_hz": 180_000.0,
                "snr_db": 26.0,
                "peak_dbm": -18.0,
                "noise_floor_dbm": -42.0,
                "pictogram_category": "WIDEBAND_BROADCAST",
                "pictogram_label": "WFM",
                "pictogram_confidence": 0.95,
                "hailo_accelerated": True,
                "lna_gain": 24,
                "vga_gain": 20,
                "amp_gain": 0,
                "plot_min": -55.0,
                "plot_max": -4.0,
                "is_legal": True,
                "legal_notes": "ok",
                "locality_ref": "Denver",
                "timestamp_iso": "2026-10-29T23:00:00Z",
            }
        )
        assert wfm_prof.plot_min <= -82.0  # Legacy JSON plot_min normalized on load!
        wfm_cfg = ctrl.generate_profile(wfm_prof)
        qs_w = QSettings(str(wfm_cfg), QSettings.Format.IniFormat)
        assert int(qs_w.value("input/sample_rate")) == 2_400_000
        assert int(qs_w.value("input/decimation")) == 1
        assert str(qs_w.value("input/ignore_limits")).lower() == "true"
        assert not qs_w.contains("output/device")
        assert int(qs_w.value("audio/gain")) == 0
        assert str(qs_w.value("fft/db_ranges_locked")).lower() == "false"
        assert str(qs_w.value("fft/fft_window")) == "hann"
        assert str(qs_w.value("gui/geometry")) == "TEST_GEOM_BYTES"

        # 2. Narrowband Low-HF AM at 1.100 MHz (2.0 MSPS, decimation=2 = 1.0 MHz view, negative offset=-150 kHz)
        hf_prof = ClassifiedSignalProfile.from_dict(
            {
                **wfm_prof.to_dict(),
                "frequency_hz": 1_100_000,
                "frequency_mhz": 1.1,
                "modulation": "AM",
                "rigctl_mode": "AM",
                "passband_hz": 10_000,
                "filter_low_cut": -5_000,
                "filter_high_cut": 5_000,
            }
        )
        hf_cfg = ctrl.generate_profile(hf_prof)
        qs_n = QSettings(str(hf_cfg), QSettings.Format.IniFormat)
        assert int(qs_n.value("input/sample_rate")) == 2_000_000
        assert int(qs_n.value("input/decimation")) == 2
        assert int(qs_n.value("receiver/offset")) == -150_000


def test_internal_clock_spur_rejection_and_6ghz_scan_config() -> None:
    # Exact unmodulated Si5351 N*20 MHz & RP1 250/350 MHz clock needles rejected
    assert is_likely_internal_clock_spur(800_000_000.0, obw_hz=7_812.0, iq_mod_hint="CW", freq_dev_hz=300.0) is True
    assert is_likely_internal_clock_spur(250_000_000.0, obw_hz=7_812.0, iq_mod_hint="NFM", freq_dev_hz=900.0) is True

    # Real modulated signals or broadcast/NOAA/ADS-B frequencies are NEVER rejected
    assert is_likely_internal_clock_spur(100_000_000.0, obw_hz=7_812.0, iq_mod_hint="CW", freq_dev_hz=100.0) is False
    assert is_likely_internal_clock_spur(162_550_000.0, obw_hz=15_000.0, iq_mod_hint="NFM", freq_dev_hz=3200.0) is False
    assert is_likely_internal_clock_spur(440_000_000.0, obw_hz=15_000.0, iq_mod_hint="NFM", freq_dev_hz=3800.0) is False
    assert is_likely_internal_clock_spur(1_090_000_000.0, obw_hz=500_000.0, iq_mod_hint="DIGITAL_BURST") is False

    # Full 1-6000 MHz Custom scan scales Layer 1 bin width cleanly
    full_cfg = ScanConfig.from_mode("CUSTOM", 1, 6000)
    assert full_cfg.l1_bin_width_hz >= 150_000


def test_recentconfig_dangling_profile_sanitization() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        cdir = Path(tmpdir)
        valid_conf = cdir / "default.conf"
        qs = QSettings(str(valid_conf), QSettings.Format.IniFormat)
        qs.setValue("crashed", True)
        qs.setValue("input/device", "hackrf=0")
        qs.setValue("fft/pandapter_min_db", -55)
        qs.sync()

        recent = cdir / "recentconfig.cfg"
        recent.write_text(
            f"{cdir / 'Deleted_Old_Profile.conf'}\n{valid_conf}\n",
            encoding="utf-8",
        )

        sanitize_gqrx_crash_flags(config_dir=cdir)
        lines = [ln.strip() for ln in recent.read_text(encoding="utf-8").splitlines() if ln.strip()]
        assert lines == [str(valid_conf.resolve())]
        qs_after = QSettings(str(valid_conf), QSettings.Format.IniFormat)
        assert str(qs_after.value("crashed")).lower() == "false"
        assert int(qs_after.value("fft/pandapter_min_db")) == -95
