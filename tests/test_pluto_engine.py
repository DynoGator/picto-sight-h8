"""
Unit & Hardware Integration Tests for LibreSDR HamGeek AD9363 rev.5 / PlutoSDR+
Tezuka firmware v0.3.12 over 1Gbps Ethernet link (ip:192.168.2.1)
"""

from __future__ import annotations

import unittest
import numpy as np

from core.pluto_engine import PlutoScannerEngine, PlutoDeviceInfo, probe_pluto_network
from core.sdr_arbiter import (
    get_sdr_arbiter,
    detect_plutosdr_network,
    format_osmosdr_plutosdr_device_arg,
    format_osmosdr_hackrf_device_arg,
)


class TestPlutoEngine(unittest.TestCase):
    def test_01_probe_pluto_network(self) -> None:
        online = probe_pluto_network("192.168.2.1", 30431, timeout_s=0.2)
        self.assertTrue(online, "LibreSDR IIO daemon not reachable at 192.168.2.1:30431")

    def test_02_detect_plutosdr_network_metadata(self) -> None:
        ok, uri, ser, model = detect_plutosdr_network()
        self.assertTrue(ok)
        self.assertEqual(uri, "ip:192.168.2.1")
        self.assertTrue(len(ser) > 0)
        self.assertIn("LibreSDR", model)

    def test_03_format_osmosdr_args(self) -> None:
        pluto_arg = format_osmosdr_plutosdr_device_arg("ip:192.168.2.1")
        self.assertEqual(pluto_arg, "soapy=0,driver=plutosdr,uri=ip:192.168.2.1")

        hackrf_arg = format_osmosdr_hackrf_device_arg("0000000000000000010961dc2a7c5f4f")
        self.assertEqual(hackrf_arg, "hackrf=7c5f4f")

    def test_04_pluto_engine_connect_and_info(self) -> None:
        engine = PlutoScannerEngine()
        self.assertTrue(engine.is_online())
        info = engine.get_device_info()
        self.assertEqual(info.adc_bits, 12)
        self.assertEqual(info.max_sample_rate_msps, 61.44)
        self.assertEqual(info.model, "LibreSDR Rev.5 (Z7020-AD9361)")

    def test_05_pluto_live_iq_capture_and_rssi(self) -> None:
        engine = PlutoScannerEngine()
        iq = engine.capture_raw_iq(num_samples=4096, center_freq_hz=101_300_000)
        self.assertEqual(len(iq), 4096)
        self.assertEqual(iq.dtype, np.complex64)
        rssi = engine.read_hardware_rssi()
        self.assertGreater(rssi, 0.0)

    def test_06_pluto_1mhz_spectrogram_frames(self) -> None:
        engine = PlutoScannerEngine()
        frames, metrics = engine.capture_1mhz_pictogram_frames(
            center_freq_hz=101_300_000,
            num_frames=6,
            rows_per_frame=64,
            cols_per_frame=128,
        )
        self.assertEqual(len(frames), 6)
        self.assertEqual(frames[0].shape, (64, 128))
        self.assertEqual(metrics["sdr_device"], "LIBRESDR_PLUTO")
        self.assertEqual(metrics["adc_bits"], 12)
        self.assertIn("hardware_rssi_db", metrics)

    def test_07_arbiter_dual_sdr_support(self) -> None:
        arbiter = get_sdr_arbiter()
        snap = arbiter.get_status_snapshot()
        self.assertTrue(snap.pluto_online)
        self.assertTrue(snap.hackrf_online)
        self.assertIn("LIBRESDR_PLUTO", snap.available_sdrs)
        self.assertIn("HACKRF_ONE", snap.available_sdrs)
        self.assertEqual(arbiter.get_active_sdr(), "LIBRESDR_PLUTO")
        self.assertEqual(arbiter.get_sdr_device_arg(), "soapy=0,driver=plutosdr,uri=ip:192.168.2.1")

        # Test switching to HackRF One and back
        arbiter.set_active_sdr("HACKRF_ONE")
        self.assertEqual(arbiter.get_active_sdr(), "HACKRF_ONE")
        self.assertIn("hackrf=", arbiter.get_sdr_device_arg())

        arbiter.set_active_sdr("LIBRESDR_PLUTO")
        self.assertEqual(arbiter.get_active_sdr(), "LIBRESDR_PLUTO")
        self.assertEqual(arbiter.get_sdr_device_arg(), "soapy=0,driver=plutosdr,uri=ip:192.168.2.1")

    def test_08_dynamic_lo_offset_boundary_steering(self) -> None:
        """Verify dynamic LO offset steering across AD9361 RF limits (46.875 MHz - 6.000 GHz)."""
        engine = PlutoScannerEngine()

        # Lower boundary test: 47.0 MHz (< 48 MHz) -> LO steered above to protect 46.875 MHz floor
        f_low = 47_000_000
        frames_low, metrics_low = engine.capture_1mhz_pictogram_frames(
            center_freq_hz=f_low,
            num_frames=3,
        )
        self.assertEqual(len(frames_low), 3)
        self.assertEqual(frames_low[0].shape, (64, 128))
        self.assertEqual(metrics_low["lo_offset_hz"], -625_000)
        self.assertEqual(metrics_low["sample_rate_hz"], 4_000_000)

        # Mid-band test: 146.52 MHz -> LO steered below target carrier
        f_mid = 146_520_000
        frames_mid, metrics_mid = engine.capture_1mhz_pictogram_frames(
            center_freq_hz=f_mid,
            num_frames=3,
        )
        self.assertEqual(len(frames_mid), 3)
        self.assertEqual(metrics_mid["lo_offset_hz"], 625_000)

        # Upper boundary test: 5995.0 MHz (near 6.000 GHz ceiling) -> LO <= 6.0 GHz, no overflow
        f_high = 5_995_000_000
        frames_high, metrics_high = engine.capture_1mhz_pictogram_frames(
            center_freq_hz=f_high,
            num_frames=3,
        )
        self.assertEqual(len(frames_high), 3)
        self.assertEqual(frames_high[0].shape, (64, 128))
        self.assertEqual(metrics_high["lo_offset_hz"], 625_000)

    def test_09_hardware_agc_modes(self) -> None:
        """Verify AD9361 hardware AGC modes: manual, fast_attack, slow_attack, hybrid."""
        engine = PlutoScannerEngine()
        for mode in ["manual", "fast_attack", "slow_attack", "hybrid"]:
            frames, metrics = engine.capture_1mhz_pictogram_frames(
                center_freq_hz=101_100_000,
                num_frames=3,
                gain_mode=mode,
                hardware_gain_db=45.0 if mode == "manual" else None,
            )
            self.assertEqual(len(frames), 3)
            self.assertEqual(metrics["gain_mode"], mode)
            self.assertGreater(metrics["hardware_rssi_db"], 0.0)

    def test_10_ai_pictogram_classifier_handoff(self) -> None:
        """Verify seamless end-to-end handoff between PlutoSDR+ frames and AIPictogramClassifierManager."""
        from core.pictogram_engine import AIPictogramClassifierManager

        engine = PlutoScannerEngine()
        mgr = AIPictogramClassifierManager()
        mgr.initialize()

        frames, metrics = engine.capture_1mhz_pictogram_frames(
            center_freq_hz=101_100_000,
            num_frames=6,
            rows_per_frame=64,
            cols_per_frame=128,
        )
        self.assertEqual(len(frames), 6)
        self.assertEqual(frames[0].shape, (64, 128))
        self.assertEqual(metrics["adc_bits"], 12)
        self.assertEqual(metrics["sdr_device"], "LIBRESDR_PLUTO")
        self.assertIn("zoom_frames", metrics)
        self.assertGreater(len(metrics["zoom_frames"]), 0)
        self.assertEqual(metrics["zoom_frames"][0].shape, (64, 128))

        res = mgr.analyze_spectrogram_frames(
            center_freq_hz=101_100_000,
            frames=frames,
            bandwidth_hz=1_000_000.0,
            iq_metrics=metrics,
        )
        self.assertIsNotNone(res)
        self.assertGreater(res.confidence, 0.0)
        self.assertTrue(len(res.label) > 0)
        self.assertTrue(len(res.category) > 0)
        self.assertGreater(res.zoom_frames_evaluated, 0)
        self.assertIsNotNone(res.composite_rgb)
        self.assertEqual(res.composite_rgb.ndim, 3)
        self.assertEqual(res.composite_rgb.shape[2], 3)


if __name__ == "__main__":
    unittest.main()
