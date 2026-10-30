#!/usr/bin/env python3
"""
Unit tests for Bandplan definitions and legal demodulation heuristics
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.bandplans import find_band_definition, evaluate_signal_demod_feasibility

class TestBandplans(unittest.TestCase):

    def test_fm_broadcast_evaluation(self):
        # 107.9 MHz (Denver KBPI) - standard strong local FM (-45 dBFS default -> LNA 24 dB)
        res = evaluate_signal_demod_feasibility(107_900_000, snr_db=32.0, measured_bw_hz=200_000)
        self.assertEqual(res["demod"], "WFM (stereo)")
        self.assertTrue(res["is_legal"])
        self.assertEqual(res["profile_category"], "Broadcast_Audio")
        self.assertEqual(res["fm_maxdev"], 75000)
        self.assertEqual(res["lna_gain"], 24)
        # Ultra-hot carrier (-18 dBFS) backs off LNA to 16 dB to prevent ADC clipping
        res_hot = evaluate_signal_demod_feasibility(
            107_900_000, snr_db=38.0, measured_bw_hz=200_000, peak_dbfs=-18.0
        )
        self.assertEqual(res_hot["lna_gain"], 16)

    def test_airband_evaluation(self):
        # 119.5 MHz (Denver Tower)
        res = evaluate_signal_demod_feasibility(119_500_000, snr_db=18.0, measured_bw_hz=10_000)
        self.assertEqual(res["demod"], "AM")
        self.assertTrue(res["is_legal"])
        self.assertEqual(res["profile_category"], "Aviation_Airband")
        self.assertEqual(res["fm_maxdev"], 0)

    def test_weather_radio_evaluation(self):
        # 162.55 MHz (NOAA WX01)
        res = evaluate_signal_demod_feasibility(162_550_000, snr_db=24.0, measured_bw_hz=15_000)
        self.assertEqual(res["demod"], "Narrow FM")
        self.assertTrue(res["is_legal"])
        self.assertEqual(res["profile_category"], "Emergency_Weather")

    def test_ham_2m_evaluation(self):
        # 146.52 MHz (2m Simplex)
        res = evaluate_signal_demod_feasibility(146_520_000, snr_db=14.0, measured_bw_hz=15_000)
        self.assertEqual(res["demod"], "Narrow FM")
        self.assertTrue(res["is_legal"])
        self.assertEqual(res["profile_category"], "Amateur_Radio")

if __name__ == "__main__":
    unittest.main()
