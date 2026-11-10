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


if __name__ == "__main__":
    unittest.main()
