#!/usr/bin/env python3
"""
Comprehensive End-to-End Audit Test Suite for Picto-Sight H8
Verifies:
  1. Upgraded 10-class Hailo-8 RF Pictogram Classifier + 16-D physical RF features
  2. Fremont County / Penrose priority voice vs PAGER_DATA disambiguation & ATSC snapping
  3. Channel-filtered FM IQ->PCM demodulator (+250 kHz LO offset) & ADS-B UC8 DC cancellation
  4. Broadcast TV Receiver Dialog & Controller (ATSC 8VSB + Analog NTSC raster, zero crashes)
  5. Gqrx 8.0 MSPS TV_WIDE sampling tier & SDRResourceArbiter auxiliary receiver arbitration
  6. 5-inch DSI (800x480 / 800x444) zero-overflow GUI geometry across all 5 Deck tabs
  7. Reboot-persistent UI & receiver settings (profiles/ui_settings.json) round-trip
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PyQt6.QtWidgets import QApplication

from core.adsb_receiver import convert_hackrf_int8_to_uc8
from core.bandplans import snap_to_channel_raster
from core.demod_engine import fm_demodulate_iq_to_pcm, get_demod_profile_for_signal
from core.gqrx_controller import GqrxController
from core.hailo_pictogram import PictogramROI, compute_roi_physical_features
from core.pictogram_engine import AIPictogramClassifierManager
from core.profile_organizer import ClassifiedSignalProfile
from core.sdr_arbiter import get_sdr_arbiter
from core.tv_demodulator import TVReceiverTelemetry
from scripts.train_hailo_rf_classifier import synthesize_class_frame
from ui.main_window import SmartScannerMainWindow
from ui.tv_dialog import BroadcastTVReceiverDialog


_QT_APP = QApplication.instance() or QApplication(sys.argv[:1])


def _synth_3_frames(cls_name: str, rng: np.random.Generator):
    frames = []
    ref_db = -84.0
    for _ in range(3):
        f, ref_db = synthesize_class_frame(cls_name, rng)
        frames.append(f)
    return frames, ref_db


class TestFullSystemAudit(unittest.TestCase):
    """End-to-end verification of every Picto-Sight H8 subsystem."""

    def test_01_ten_class_hailo_weights_and_live_classification(self) -> None:
        weights_path = PROJECT_ROOT / "models" / "hailo_pictogram_head_weights.npz"
        self.assertTrue(weights_path.exists(), "Trained 10-class Hailo head weights missing")

        npz = np.load(str(weights_path), allow_pickle=False)
        classes = [str(x) for x in npz["classes"].tolist()]
        self.assertEqual(len(classes), 10)
        for expected in (
            "adsb_pulse",
            "am_voice",
            "atsc_tv",
            "fsk_pager",
            "ism_telemetry",
            "nfm_voice",
            "noise_reject",
            "wfm_stereo",
        ):
            self.assertIn(expected, classes)
        val_acc = float(npz["val_accuracy"])
        self.assertGreaterEqual(val_acc, 0.95, f"Expected >=95% validation accuracy, got {val_acc:.3f}")

        mgr = AIPictogramClassifierManager()
        try:
            rng = np.random.default_rng(12345)
            # 1. Commercial WFM Stereo @ 98.9 MHz
            wfm_frames, wfm_ref = _synth_3_frames("wfm_stereo", rng)
            res_wfm = mgr.analyze_spectrogram_frames(
                98_900_000, wfm_frames, iq_metrics={"ref_floor_dbfs": wfm_ref, "measured_obw_hz": 180_000.0}
            )
            self.assertEqual(res_wfm.category, "BROADCAST_WFM")
            self.assertEqual(res_wfm.raw_neural_class, "wfm_stereo")
            self.assertGreater(res_wfm.confidence, 0.55)

            # 2. ATSC 8VSB Digital Broadcast TV @ 539.0 MHz (Ch 25 KOAA)
            tv_frames, tv_ref = _synth_3_frames("atsc_tv", rng)
            res_tv = mgr.analyze_spectrogram_frames(
                539_000_000, tv_frames, iq_metrics={"ref_floor_dbfs": tv_ref, "measured_obw_hz": 5_380_000.0}
            )
            self.assertEqual(res_tv.category, "BROADCAST_TV")
            self.assertEqual(res_tv.raw_neural_class, "atsc_tv")

            # 3. 1090 MHz ADS-B Mode-S Transponder Pulses
            adsb_frames, adsb_ref = _synth_3_frames("adsb_pulse", rng)
            res_adsb = mgr.analyze_spectrogram_frames(
                1_090_000_000, adsb_frames, iq_metrics={"ref_floor_dbfs": adsb_ref}
            )
            self.assertEqual(res_adsb.category, "AVIATION_ADSB")
            self.assertEqual(res_adsb.raw_neural_class, "adsb_pulse")

            # 4. Fremont County Penrose Fire Dispatch @ 155.880 MHz MUST classify as FIRE_EMS (never PAGER_DATA!)
            nfm_frames, nfm_ref = _synth_3_frames("nfm_voice", rng)
            res_fire = mgr.analyze_spectrogram_frames(
                155_880_000, nfm_frames, iq_metrics={"ref_floor_dbfs": nfm_ref, "measured_obw_hz": 12_500.0}
            )
            self.assertEqual(res_fire.category, "FIRE_EMS")

            # 5. Fremont County Sheriff Dispatch @ 154.845 MHz MUST classify as SHERIFF_POLICE
            res_so = mgr.analyze_spectrogram_frames(
                154_845_000, nfm_frames, iq_metrics={"ref_floor_dbfs": nfm_ref, "measured_obw_hz": 12_500.0}
            )
            self.assertEqual(res_so.category, "SHERIFF_POLICE")

            # 6. Dedicated POCSAG Pager @ 152.480 MHz -> PAGER_DATA
            pgr_frames, pgr_ref = _synth_3_frames("fsk_pager", rng)
            res_pgr = mgr.analyze_spectrogram_frames(
                152_480_000, pgr_frames, iq_metrics={"ref_floor_dbfs": pgr_ref, "measured_obw_hz": 16_000.0}
            )
            self.assertEqual(res_pgr.category, "PAGER_DATA")
        finally:
            mgr.close()

    def test_02_physical_rf_feature_vector_bounds(self) -> None:
        rng = np.random.default_rng(7)
        frames, _ = _synth_3_frames("wfm_stereo", rng)
        patch_db = frames[0]
        roi = PictogramROI(
            roi_id="test_roi",
            row_min=0,
            row_max=patch_db.shape[0],
            col_min=32,
            col_max=96,
            width_bins=26,
            height_rows=patch_db.shape[0],
            area=26 * patch_db.shape[0],
            center_freq_hz=98_900_000.0,
            bandwidth_hz=200_000.0,
            duration_s=0.045,
            snr_db=28.0,
            peak_dbm=-40.0,
            patch=patch_db[:, 32:96],
            normalized_rgb=np.zeros((224, 224, 3), dtype=np.uint8),
        )
        feats = compute_roi_physical_features(roi)
        self.assertEqual(feats.shape, (16,))
        self.assertTrue(np.all(np.isfinite(feats)))
        self.assertTrue(np.all((feats >= -1.5) & (feats <= 1.5)))

    def test_03_channel_raster_snapping_cb_and_atsc_tv(self) -> None:
        # ATSC Ch 25 pilot at 536.309441 MHz -> snaps to 539.000 MHz channel center
        snapped_25, _ = snap_to_channel_raster(536_309_441, "US")
        self.assertEqual(snapped_25, 539_000_000)
        # UHF Ch 14 pilot at 470.309441 MHz -> snaps to 473.000 MHz (NOT 470.3125 MHz LMR!)
        snapped_14, _ = snap_to_channel_raster(470_309_441, "US")
        self.assertEqual(snapped_14, 473_000_000)
        # CB Ch 19 trucker highway channel (27.185 MHz)
        snapped_cb, _ = snap_to_channel_raster(27_184_200, "US")
        self.assertEqual(snapped_cb, 27_185_000)

        # Regression: 96.9 MHz KCCY FM Broadcast must NEVER match POCSAG/Pager even with transient FSK sub-label
        from core.bandplans import classify_voice_broadcast
        from core.demod_engine import get_demod_profile_for_signal

        vb_fm = classify_voice_broadcast(
            freq_hz=96_900_000,
            band_name="Commercial FM Broadcast (KCCY 96.9 Country)",
            service_type="FM Stereo Broadcast",
            demod="WFM (stereo)",
            obw_hz=55_000.0,
            pic_label="WFM-Stereo (WFM-fsk_pager)",
        )
        self.assertTrue(vb_fm["is_voice_broadcast"])
        self.assertEqual(vb_fm["voice_label"], "[📻 VOICE BROADCAST]")

        dp_fm = get_demod_profile_for_signal(
            freq_hz=96_900_000,
            band_name="Commercial FM Broadcast (KCCY 96.9 Country)",
            service_type="FM Stereo Broadcast",
            demod="WFM (stereo)",
            obw_hz=55_000.0,
            pic_label="WFM-Stereo (WFM-fsk_pager)",
            profile_category="BROADCAST_WFM",
        )
        self.assertEqual(dp_fm.tool_id, "gqrx")
        self.assertEqual(dp_fm.demod_label, "[📻 VOICE BROADCAST]")

    def test_04_channel_filtered_fm_iq_demod_and_adsb_uc8(self) -> None:
        # Synthesize 2.0 MSPS HackRF int8 IQ with an NFM carrier at +250 kHz modulated by a 1200 Hz tone
        fs = 2_000_000
        dur = 0.08
        t = np.arange(int(fs * dur), dtype=np.float64) / fs
        audio_tone_hz = 1200.0
        dev_hz = 4000.0
        mod_phase = 2.0 * np.pi * 250_000.0 * t + (dev_hz / audio_tone_hz) * np.sin(2.0 * np.pi * audio_tone_hz * t)
        # Add out-of-channel interferer at -350 kHz + DC spike at 0 Hz to prove channel filtering works!
        iq = (
            65.0 * np.exp(1j * mod_phase)
            + 50.0 * np.exp(-1j * 2.0 * np.pi * 350_000.0 * t)
            + (18.0 + 14.0j)
        )
        interleaved = np.empty(iq.size * 2, dtype=np.int8)
        interleaved[0::2] = np.clip(np.real(iq), -127, 127).astype(np.int8)
        interleaved[1::2] = np.clip(np.imag(iq), -127, 127).astype(np.int8)

        pcm_bytes = fm_demodulate_iq_to_pcm(
            interleaved.tobytes(),
            in_rate=fs,
            out_rate=22_050,
            lo_offset_hz=250_000.0,
        )
        pcm = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float64)
        self.assertGreater(pcm.size, 1200)
        # Verify dominant audio frequency is ~1200 Hz
        pcm = pcm[200:] - np.mean(pcm[200:])
        spec = np.abs(np.fft.rfft(pcm * np.hanning(pcm.size)))
        freqs = np.fft.rfftfreq(pcm.size, d=1.0 / 22_050.0)
        peak_hz = float(freqs[int(np.argmax(spec[2:]) + 2)])
        self.assertAlmostEqual(peak_hz, 1200.0, delta=45.0)

        # Verify HackRF signed int8 -> unsigned UC8 DC bias cancellation
        uc8 = np.frombuffer(convert_hackrf_int8_to_uc8(interleaved.tobytes()), dtype=np.uint8)
        self.assertEqual(uc8.size, interleaved.size)
        self.assertTrue(125.5 <= float(np.mean(uc8)) <= 129.5)

    def test_05_gqrx_tv_wide_8msps_tier_and_arbiter_aux_lock(self) -> None:
        ctrl = GqrxController()
        tier_name, rate, decim, bw_hz, offset = ctrl._compute_sampling_tier(
            "Raw I/Q", 240_000, 539_000_000, profile_category="Broadcast_TV", bandwidth_hz=6_000_000.0
        )
        self.assertEqual(tier_name, "TV_WIDE")
        self.assertEqual(rate, 8_000_000)
        self.assertEqual(decim, 1)
        self.assertEqual(bw_hz, 6_000_000)
        self.assertEqual(offset, 0)

        arb = get_sdr_arbiter()
        stopped_flag = {"called": False}
        arb.register_aux_stopper("TV_DEMOD", lambda: stopped_flag.__setitem__("called", True))
        ok, _ = arb.prepare_for_aux_receiver("TV_DEMOD", "UnitTest Ch 25 ATSC")
        self.assertTrue(ok)
        snap = arb.get_status_snapshot()
        self.assertEqual(snap.owner, "TV_DEMOD")
        arb.stop_aux_receivers()
        self.assertTrue(stopped_flag["called"])
        arb.release_aux_receiver("TV_DEMOD")

    def test_06_broadcast_tv_dialog_and_main_window_no_crash(self) -> None:
        dlg = BroadcastTVReceiverDialog(
            initial_freq_hz=539_000_000.0,
            compact_screen=True,
            auto_start=False,
        )
        dlg.show()
        _QT_APP.processEvents()
        self.assertLessEqual(dlg.width(), 800)
        self.assertLessEqual(dlg.height(), 444)

        # Feed synthetic 8.0 MSPS ATSC 8VSB spectrum + pilot lock
        f_mhz = np.linspace(535.0, 543.0, 256, dtype=np.float32)
        p_db = np.full_like(f_mhz, -84.0)
        in_ch = (f_mhz >= 536.31) & (f_mhz <= 541.69)
        p_db[in_ch] = -52.0
        pilot_idx = int(np.argmin(np.abs(f_mhz - 536.309441)))
        p_db[pilot_idx] = -36.0
        telem = TVReceiverTelemetry(
            running=True,
            mode="ATSC_8VSB",
            center_freq_hz=539_000_000,
            channel=25,
            pilot_freq_hz=536_309_441.0,
            pilot_snr_db=22.4,
            channel_snr_db=19.8,
            mer_est_db=19.2,
            pilot_locked=True,
            ts_bitrate_kbps=19392.0,
            spectrum_freqs_mhz=f_mhz.tolist(),
            spectrum_dbfs=p_db.tolist(),
        )
        dlg.canvas.update_state(telem)
        _QT_APP.processEvents()

        # Switch to Analog NTSC mode & render synthetic 525-line raster
        telem.mode = "ANALOG_NTSC"
        raster = np.linspace(0, 255, 240 * 320, dtype=np.uint8).reshape(240, 320)
        dlg.canvas.resize(760, 200)
        dlg.canvas.update_state(telem, raster=raster)
        pix = dlg.canvas.grab()
        self.assertFalse(pix.isNull())
        _QT_APP.processEvents()
        dlg.close()

    def test_07_dsi_800x480_geometry_demod_dispatch_and_settings_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings_file = Path(tmpdir) / "ui_settings.json"

            win = SmartScannerMainWindow()
            win._ui_settings_path = settings_file
            win._layout_preference = "DECK"
            win._apply_topology(force=True)
            win.show()
            win.showNormal()
            win.resize(800, 444)
            _QT_APP.processEvents()

            # Verify zero overflow across all 5 Deck tabs on 800x444 DSI
            self.assertLessEqual(win.width(), 800)
            self.assertLessEqual(win.height(), 480)
            self.assertLessEqual(win.minimumSizeHint().width(), 800)
            self.assertLessEqual(win.minimumSizeHint().height(), 444)
            for tab_idx in range(5):
                win._switch_deck_tab(tab_idx)
                _QT_APP.processEvents()
                current_widget = win.deck_stack.currentWidget()
                self.assertIsNotNone(current_widget)
                self.assertLessEqual(current_widget.width(), 800)

            # Verify Broadcast TV signal click NEVER crashes (both atsc_tv_rx and legacy w_scan_cpp!)
            d_tv = get_demod_profile_for_signal(539_000_000, demod="Raw I/Q", profile_category="Broadcast_TV")
            tv_prof = ClassifiedSignalProfile(
                frequency_hz=539_000_000,
                frequency_mhz=539.0,
                raw_measured_hz=539_000_000.0,
                carrier_offset_hz=0.0,
                signal_name="[📺 BROADCAST TV (Ch 25)] KOAA-TV NBC Pueblo/Colorado Springs",
                profile_category="Broadcast_TV",
                modulation="Raw I/Q",
                rigctl_mode="RAW",
                passband_hz=240_000,
                filter_low_cut=-120_000,
                filter_high_cut=120_000,
                fm_maxdev=75_000,
                sql_level=-150.0,
                bandwidth_hz=5_380_000.0,
                snr_db=24.0,
                peak_dbm=-44.0,
                noise_floor_dbm=-82.0,
                iq_mod_hint="RAW",
                duty_cycle=1.0,
                frames_evaluated=3,
                pictogram_category="BROADCAST_TV",
                pictogram_neural_class="atsc_tv",
                pictogram_label="ATSC-8VSB",
                pictogram_confidence=0.98,
                hailo_accelerated=True,
                lna_gain=32,
                vga_gain=26,
                amp_gain=0,
                plot_min=-95.0,
                plot_max=-25.0,
                is_legal=True,
                legal_notes="Public broadcast television",
                locality_ref="Penrose, CO",
                timestamp_iso="2026-11-07T00:00:00Z",
                is_voice_broadcast=False,
                is_any_voice=False,
                voice_label="[📺 BROADCAST TV (Ch 25)]",
                demod_tool=d_tv.tool_id,
                demod_cmd=d_tv.command,
                atsc_channel=25,
            )

            opened = {"freq": 0.0}
            with patch.object(
                win,
                "open_broadcast_tv_dialog",
                side_effect=lambda f, **kw: opened.__setitem__("freq", f),
            ):
                win.handle_signal_demod_action(tv_prof)
                self.assertAlmostEqual(opened["freq"], 539_000_000.0)
                # Also verify legacy saved profile with demod_tool == "w_scan_cpp" routes cleanly to TV Dialog!
                tv_prof.demod_tool = "w_scan_cpp"
                opened["freq"] = 0.0
                win.handle_signal_demod_action(tv_prof)
                self.assertAlmostEqual(opened["freq"], 539_000_000.0)

            # Verify Pager / ISM / APRS profiles route into built-in Tab 5 Tactical Feed without spawning terminal
            pager_called = {"hz": 0}
            pgr_prof = ClassifiedSignalProfile(**{**tv_prof.to_dict(), "frequency_hz": 152_480_000, "frequency_mhz": 152.48, "profile_category": "Public_Safety", "demod_tool": "multimon_ng", "is_voice_broadcast": False, "is_any_voice": False, "atsc_channel": None})
            with patch.object(win.panel_map, "decode_pager_frequency", side_effect=lambda hz: pager_called.__setitem__("hz", hz)):
                win._switch_deck_tab(2)
                win.handle_signal_demod_action(pgr_prof)
                self.assertEqual(pager_called["hz"], 152_480_000)
                self.assertEqual(win.deck_stack.currentIndex(), 4)

            # Verify HDMI Studio 4-pane topology switch works without errors
            win._layout_preference = "STUDIO"
            win._apply_topology(force=True)
            _QT_APP.processEvents()
            self.assertFalse(win._is_compact_mode)
            win._layout_preference = "DECK"
            win._apply_topology(force=True)
            _QT_APP.processEvents()
            self.assertTrue(win._is_compact_mode)

            # Verify reboot-persistent UI settings round-trip
            win._select_scan_mode("DEEP")
            win.spin_min_mhz.setValue(154)
            win.spin_max_mhz.setValue(156)
            win.slider_snr.setValue(14)
            win.slider_peak.setValue(-68)
            win.spin_lna.setValue(32)
            win.spin_vga.setValue(28)
            win._set_signal_filter_mode("VOICE_BROADCAST")
            win._save_ui_settings()
            self.assertTrue(settings_file.exists())

            saved_json = json.loads(settings_file.read_text(encoding="utf-8"))
            self.assertEqual(saved_json["scan_mode"], "DEEP")
            self.assertEqual(saved_json["min_mhz"], 154)
            self.assertEqual(saved_json["max_mhz"], 156)
            self.assertEqual(saved_json["min_snr"], 14)
            self.assertEqual(saved_json["min_peak"], -68)
            self.assertEqual(saved_json["signal_filter_mode"], "VOICE_BROADCAST")

            # Reload into a second window instance
            win2 = SmartScannerMainWindow()
            win2._ui_settings_path = settings_file
            win2._load_ui_settings()
            self.assertEqual(win2._current_scan_mode, "DEEP")
            self.assertEqual(win2.spin_min_mhz.value(), 154)
            self.assertEqual(win2.spin_max_mhz.value(), 156)
            self.assertEqual(win2.slider_snr.value(), 14)
            self.assertEqual(win2.slider_peak.value(), -68)
            self.assertEqual(win2.spin_lna.value(), 32)
            self.assertEqual(win2.spin_vga.value(), 28)
            self.assertEqual(win2._signal_filter_mode, "VOICE_BROADCAST")

            win2.close()
            win.close()

    def test_08_version_doctor_gqrx_provision_and_deb_portability(self) -> None:
        import core
        from core import get_runtime_data_dir
        from scripts.doctor_check import provision_gqrx_out_of_box, run_doctor_check

        self.assertEqual(core.__version__, "3.1.0")
        self.assertEqual(core.__release_codename__, "Ultra-Zoom-Spectrum")

        # 1. Verify PICTOSIGHT_DATA_DIR override & read-only /opt fallback portability
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {"PICTOSIGHT_DATA_DIR": tmpdir}):
                resolved = get_runtime_data_dir()
                self.assertEqual(resolved, Path(tmpdir).resolve())

            with patch.dict("os.environ", {"PICTOSIGHT_DATA_DIR": "", "HOME": tmpdir}):
                with patch("os.access", return_value=False):
                    ro_fallback = get_runtime_data_dir()
                    self.assertEqual(ro_fallback, Path(tmpdir) / ".local" / "share" / "picto-sight-h8")
                    self.assertTrue(ro_fallback.exists())

        # 2. Verify System Doctor passes all required stack checks
        report = run_doctor_check()
        self.assertEqual(report["version"], "3.1.0")
        self.assertTrue(report["ready"])
        self.assertEqual(report["required_failures"], 0)

        # 3. Verify out-of-the-box Gqrx provisioner writes crash-free config
        prov = provision_gqrx_out_of_box(force_bookmarks=False)
        self.assertTrue(prov["ok"])
        default_conf_txt = Path(prov["default_conf"]).read_text(encoding="utf-8", errors="ignore")
        self.assertNotIn("crash=true", default_conf_txt.lower())

    def test_09_ultra_zoom_dsp_precision_raster_and_dwell(self) -> None:
        from core.bandplans import refine_precision_channel_frequency
        from core.hackrf_engine import HackRFScannerEngine, ScanConfig

        # 1. Verify VHF High & AAR Railroad snap with 0 Hz error on FCC 2.5 kHz & 15 kHz grids
        f_vhf_tests = [154_845_000, 155_880_000, 154_400_000, 154_310_000, 152_480_000]
        for f in f_vhf_tests:
            snapped, off = refine_precision_channel_frequency(f + 2.0, obw_hz=12_500.0, iq_mod_hint="NFM")
            self.assertEqual(snapped, f, f"Expected {f} to snap with 0 Hz error, got {snapped}")
            self.assertAlmostEqual(off, 2.0, delta=1.0)

        # AAR Rail channel: 161.490 MHz (AAR 92)
        f_rail = 161_490_000
        snapped_r, _ = refine_precision_channel_frequency(f_rail + 1.5, obw_hz=12_000.0, iq_mod_hint="NFM")
        self.assertEqual(snapped_r, f_rail)

        # AM MW 1040 kHz (10 kHz grid)
        snapped_am, _ = refine_precision_channel_frequency(1_040_003, obw_hz=10_000.0, iq_mod_hint="AM")
        self.assertEqual(snapped_am, 1_040_000)

        # 2. Test 1,024x Welch fine PSD (df <= 7.63 Hz/bin) + 244 Hz/bin Ultra-Zoom frame extraction
        sample_rate = 2_000_000
        n_samples = 262_144
        t = np.arange(n_samples) / float(sample_rate)
        # Carrier at +250 kHz LO offset + 19 kHz stereo pilot tone
        iq_sig = np.exp(1j * 2 * np.pi * 250_000.0 * t).astype(np.complex64)
        iq_sig += 0.35 * np.exp(1j * 2 * np.pi * 269_000.0 * t).astype(np.complex64)

        # Synthetic 64x128 macro frames
        macro_frames = [np.full((64, 128), -85.0, dtype=np.float32) for _ in range(3)]
        metrics = HackRFScannerEngine._compute_iq_signal_metrics(
            iq=iq_sig,
            frames=macro_frames,
            sample_rate=sample_rate,
            lo_offset_hz=250_000,
            nominal_center_hz=98_900_000.0,
            target_zoom_frames=6,
        )

        self.assertLessEqual(metrics["fine_fft_resolution_hz"], 7.63)
        self.assertAlmostEqual(metrics["precision_carrier_hz"], 98_900_000.0, delta=15.0)
        self.assertTrue(metrics.get("stereo_pilot", False))
        self.assertIn("zoom_frames", metrics)
        self.assertGreater(len(metrics["zoom_frames"]), 0)
        self.assertAlmostEqual(metrics["zoom_bin_hz"], 976.56, delta=1.0)

        # Test Narrowband 31.25 kHz Ultra-Zoom (244.14 Hz/bin = 32x zoomed-in waterfall!)
        t_nb = np.arange(131_072) / float(sample_rate)
        iq_nb = (np.exp(1j * 2 * np.pi * 250_000.0 * t_nb) + 0.1 * (np.random.randn(len(t_nb)) + 1j * np.random.randn(len(t_nb)))).astype(np.complex64)
        nb_macro = [np.full((64, 128), -85.0, dtype=np.float32) for _ in range(3)]
        metrics_nb = HackRFScannerEngine._compute_iq_signal_metrics(
            iq=iq_nb,
            frames=nb_macro,
            sample_rate=sample_rate,
            lo_offset_hz=250_000,
            nominal_center_hz=155_880_000.0,
            target_zoom_frames=6,
        )
        self.assertLessEqual(metrics_nb["zoom_bin_hz"], 245.0)
        self.assertEqual(metrics_nb["zoom_span_hz"], 31_250.0)

        # 3. Test Dual-Scale multi-pass dwell score fusion
        mgr = AIPictogramClassifierManager()
        rng = np.random.default_rng(999)
        synth_macro, m_ref = _synth_3_frames("wfm_stereo", rng)
        res_pass1 = mgr.analyze_spectrogram_frames(
            98_900_000.0,
            synth_macro,
            iq_metrics={
                "ref_floor_dbfs": m_ref,
                "measured_obw_hz": 180_000.0,
                "zoom_frames": metrics["zoom_frames"],
                "zoom_span_hz": metrics["zoom_span_hz"],
                "fine_fft_resolution_hz": metrics["fine_fft_resolution_hz"],
            },
            dwell_passes=1,
        )
        self.assertGreater(res_pass1.zoom_frames_evaluated, 0)
        self.assertIn("wfm_stereo", res_pass1.accumulated_scores)

        # Pass 2 with prior accumulated scores
        res_pass2 = mgr.analyze_spectrogram_frames(
            98_900_000.0,
            synth_macro,
            iq_metrics={
                "ref_floor_dbfs": m_ref,
                "measured_obw_hz": 180_000.0,
                "zoom_frames": metrics["zoom_frames"],
                "zoom_span_hz": metrics["zoom_span_hz"],
                "fine_fft_resolution_hz": metrics["fine_fft_resolution_hz"],
            },
            prior_class_scores=res_pass1.accumulated_scores,
            dwell_passes=2,
        )
        self.assertGreaterEqual(res_pass2.confidence, res_pass1.confidence)

    def test_10_gui_full_spectrum_and_clickable_links(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            settings_path = Path(tmpdir) / "ui_settings.json"
            win = SmartScannerMainWindow()
            win._ui_settings_path = settings_path

            # 1. Preset 0 is Full Spectrum (1 - 6000 MHz)
            lbl, lo, hi = win.BAND_PRESETS[0]
            self.assertIn("Full Spectrum", lbl)
            self.assertEqual(lo, 1)
            self.assertEqual(hi, 6000)

            # 2. Verify Default ScanConfig is 1 - 6000 MHz
            cfg = win._build_current_scan_config()
            self.assertEqual(cfg.freq_min_mhz, 1)
            self.assertEqual(cfg.freq_max_mhz, 6000)

            # 3. Populate mock signals and verify clickable Column 0 + Gqrx Surfing
            prof1 = ClassifiedSignalProfile(
                frequency_hz=104_500_000,
                frequency_mhz=104.5,
                raw_measured_hz=104_500_000.0,
                carrier_offset_hz=0.0,
                signal_name="104.5 MHz FM KRFX",
                profile_category="Broadcast_Audio",
                modulation="WFM (Stereo)",
                rigctl_mode="WFM_ST",
                passband_hz=180000,
                filter_low_cut=-90000,
                filter_high_cut=90000,
                fm_maxdev=75000,
                sql_level=-150.0,
                bandwidth_hz=180000.0,
                snr_db=25.0,
                peak_dbm=-40.0,
                noise_floor_dbm=-75.0,
                iq_mod_hint="WFM",
                duty_cycle=1.0,
                frames_evaluated=24,
                pictogram_category="BROADCAST_WFM",
                pictogram_neural_class="wfm_stereo",
                pictogram_label="WFM-Stereo",
                pictogram_confidence=0.98,
                hailo_accelerated=True,
                lna_gain=24,
                vga_gain=22,
                amp_gain=0,
                plot_min=-85.0,
                plot_max=-30.0,
                is_legal=True,
                legal_notes="FM Broadcast",
                locality_ref="Penrose",
                timestamp_iso="2026-11-07T12:00:00Z",
                is_voice_broadcast=True,
                is_any_voice=True,
                voice_label="[📻 BROADCAST]",
                demod_tool="gqrx",
                demod_cmd="gqrx",
                atsc_channel=None,
                dwell_passes=3,
                zoom_resolution_hz=7.63,
                situational_notes="3p/3.8s (48f @7.6Hz) | 19kHz-Stereo | 0Hz-Lock",
            )
            prof2 = ClassifiedSignalProfile(
                frequency_hz=162_400_000,
                frequency_mhz=162.4,
                raw_measured_hz=162_400_000.0,
                carrier_offset_hz=0.0,
                signal_name="162.4 MHz NOAA Weather WX2",
                profile_category="Emergency_Weather",
                modulation="NFM (Voice)",
                rigctl_mode="FM",
                passband_hz=16000,
                filter_low_cut=-8000,
                filter_high_cut=8000,
                fm_maxdev=5000,
                sql_level=-150.0,
                bandwidth_hz=16000.0,
                snr_db=18.0,
                peak_dbm=-52.0,
                noise_floor_dbm=-80.0,
                iq_mod_hint="NFM",
                duty_cycle=1.0,
                frames_evaluated=24,
                pictogram_category="WEATHER_NFM",
                pictogram_neural_class="nfm_voice",
                pictogram_label="NOAA-WX",
                pictogram_confidence=0.95,
                hailo_accelerated=True,
                lna_gain=32,
                vga_gain=24,
                amp_gain=0,
                plot_min=-90.0,
                plot_max=-40.0,
                is_legal=True,
                legal_notes="NOAA WX",
                locality_ref="Penrose",
                timestamp_iso="2026-11-07T12:00:00Z",
                is_voice_broadcast=True,
                is_any_voice=True,
                voice_label="[📻 NOAA WX]",
                demod_tool="gqrx",
                demod_cmd="gqrx",
                atsc_channel=None,
                dwell_passes=2,
                zoom_resolution_hz=7.63,
                situational_notes="2p/2.5s (24f @7.6Hz) | 0Hz-Lock",
            )
            win.all_profiles = [prof1, prof2]
            win._scan_unlocked = True
            win._apply_viable_filter_and_populate()

            self.assertEqual(win.table_signals.rowCount(), 2)
            c0_item = win.table_signals.item(0, 0)
            self.assertIn("104.5000 MHz", c0_item.text())

            # 4. Verify 1-click Col 0 tuning handler
            dispatched = {"called": False, "prof": None}
            with patch.object(win, "handle_signal_demod_action", side_effect=lambda p: dispatched.update({"called": True, "prof": p})):
                win._on_signal_cell_clicked(0, 0)
                self.assertTrue(dispatched["called"])
                self.assertEqual(dispatched["prof"].frequency_hz, 104_500_000)

            # 5. Verify Gqrx Surfing next / prev
            dispatched["called"] = False
            with patch.object(win, "handle_signal_demod_action", side_effect=lambda p: dispatched.update({"called": True, "prof": p})):
                win._active_gqrx_freq_hz = 104_500_000
                win._surf_gqrx_channel(+1)
                self.assertTrue(dispatched["called"])
                self.assertEqual(dispatched["prof"].frequency_hz, 162_400_000)

            win.close()


if __name__ == "__main__":
    unittest.main()
