#!/usr/bin/env python3
"""
Unit tests for Gqrx 2.17.6 native QSettings configuration generator and remote control
"""

import sys
import tempfile
import unittest
from pathlib import Path

from PyQt6.QtCore import QSettings

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.gqrx_controller import GqrxController
from core.profile_organizer import ClassifiedSignalProfile


class TestGqrxController(unittest.TestCase):

    def test_profile_generation(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_dir = Path(td)
            tmp_path = tmp_dir / "SmartScanner_Active.conf"

            ctrl = GqrxController(conf_path=tmp_path)
            ctrl.profiles_dir = tmp_dir

            sig = ClassifiedSignalProfile.from_dict(
                {
                    "frequency_hz": 107_900_000,
                    "frequency_mhz": 107.9,
                    "signal_name": "KBPI 107.9 FM Denver",
                    "profile_category": "Broadcast_Audio",
                    "modulation": "WFM (stereo)",
                    "rigctl_mode": "WFM_ST",
                    "passband_hz": 180_000,
                    "filter_low_cut": -90_000,
                    "filter_high_cut": 90_000,
                    "fm_maxdev": 75_000,
                    "bandwidth_hz": 200_000,
                    "snr_db": 32.0,
                    "peak_dbm": -42.0,
                    "noise_floor_dbm": -88.0,
                    "pictogram_category": "BROADCAST_WFM",
                    "pictogram_label": "WFM-Stereo (MPX-Comb)",
                    "pictogram_confidence": 0.95,
                    "hailo_accelerated": True,
                    "lna_gain": 16,
                    "vga_gain": 20,
                    "amp_gain": 0,
                    "plot_min": -95.0,
                    "plot_max": -35.0,
                    "is_legal": True,
                    "legal_notes": "Broadcast",
                    "locality_ref": "Denver, CO",
                    "timestamp_iso": "2026-10-29T12:00:00Z",
                }
            )

            cfg_file = ctrl.generate_profile(sig)
            self.assertTrue(cfg_file.exists())
            self.assertTrue(tmp_path.exists())

            qs = QSettings(str(cfg_file), QSettings.Format.IniFormat)
            self.assertEqual(int(qs.value("input/frequency")), 107_900_000)
            self.assertEqual(int(qs.value("receiver/offset")), 250_000)
            self.assertEqual(str(qs.value("receiver/demod")), "WFM (stereo)")
            self.assertEqual(int(qs.value("fft/pandapter_min_db")), -95)
            self.assertEqual(int(qs.value("fft/pandapter_max_db")), -35)
            self.assertEqual(int(qs.value("fft/waterfall_min_db")), -95)
            self.assertEqual(int(qs.value("fft/waterfall_max_db")), -35)
            self.assertEqual(str(qs.value("remote_control/enabled")).lower(), "true")
            self.assertEqual(str(qs.value("crashed")).lower(), "false")

            gains = qs.value("input/gains")
            self.assertIsInstance(gains, dict)
            self.assertEqual(int(gains["IF"]), 160)
            self.assertEqual(int(gains["BB"]), 200)
            self.assertEqual(int(gains["RF"]), 0)


if __name__ == "__main__":
    unittest.main()
