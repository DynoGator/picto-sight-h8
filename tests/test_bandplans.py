#!/usr/bin/env python3
"""
Unit tests for Bandplan definitions and legal demodulation heuristics
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.bandplans import (
    find_band_definition,
    evaluate_signal_demod_feasibility,
    classify_voice_broadcast,
)

class TestBandplans(unittest.TestCase):

    def test_voice_broadcast_classification(self):
        # 1. Commercial FM broadcast
        fm = classify_voice_broadcast(107_900_000, demod="WFM (stereo)", band_name="VHF FM Commercial Broadcast", obw_hz=180_000)
        self.assertTrue(fm["is_voice_broadcast"])
        self.assertTrue(fm["is_any_voice"])
        self.assertEqual(fm["voice_label"], "[📻 VOICE BROADCAST]")
        self.assertEqual(fm["voice_tag"], "Voice_Broadcast")

        # 2. NOAA Weather Radio
        wx = classify_voice_broadcast(162_550_000, demod="Narrow FM", band_name="NOAA All-Hazards Weather Radio")
        self.assertTrue(wx["is_voice_broadcast"])
        self.assertTrue(wx["is_any_voice"])
        self.assertEqual(wx["voice_label"], "[🎙️ WX VOICE BROADCAST]")

        # 3. AM Commercial Broadcast
        am = classify_voice_broadcast(850_000, demod="AM")
        self.assertTrue(am["is_voice_broadcast"])
        self.assertTrue(am["is_any_voice"])
        self.assertEqual(am["voice_label"], "[📻 AM VOICE BROADCAST]")

        # 4. Shortwave International Broadcast
        sw = classify_voice_broadcast(6_000_000, demod="AM")
        self.assertTrue(sw["is_voice_broadcast"])
        self.assertTrue(sw["is_any_voice"])
        self.assertEqual(sw["voice_label"], "[📻 SW VOICE BROADCAST]")

        # 5. Civil Aviation ATC (Air Voice, two-way, not broadcast)
        air = classify_voice_broadcast(119_500_000, demod="AM", band_name="VHF Civil Aviation Voice (ATC)")
        self.assertFalse(air["is_voice_broadcast"])
        self.assertTrue(air["is_any_voice"])
        self.assertEqual(air["voice_label"], "[✈️ AIR VOICE (ATC)]")

        # 6. Aviation ATIS (Automated weather broadcast)
        atis = classify_voice_broadcast(125_600_000, demod="AM", band_name="KDEN ATIS Weather")
        self.assertTrue(atis["is_voice_broadcast"])
        self.assertTrue(atis["is_any_voice"])
        self.assertEqual(atis["voice_label"], "[🎙️ ATIS VOICE BROADCAST]")

        # 7. Marine VHF Voice
        marine = classify_voice_broadcast(156_800_000, demod="Narrow FM", band_name="VHF International Marine Band")
        self.assertFalse(marine["is_voice_broadcast"])
        self.assertTrue(marine["is_any_voice"])
        self.assertEqual(marine["voice_label"], "[⚓ MARINE VOICE]")

        # 8. Amateur 2m Voice
        ham = classify_voice_broadcast(146_520_000, demod="Narrow FM", band_name="2m Amateur Radio Band")
        self.assertFalse(ham["is_voice_broadcast"])
        self.assertTrue(ham["is_any_voice"])
        self.assertEqual(ham["voice_label"], "[🗣️ HAM VOICE]")

        # 9. FRS/GMRS
        gmrs = classify_voice_broadcast(462_562_500, demod="Narrow FM", band_name="FRS / GMRS Personal Radio")
        self.assertFalse(gmrs["is_voice_broadcast"])
        self.assertTrue(gmrs["is_any_voice"])
        self.assertEqual(gmrs["voice_label"], "[🗣️ 2-WAY VOICE]")

        # 10. ISM Telemetry (Data / Non-Voice)
        data = classify_voice_broadcast(433_920_000, demod="Narrow FM", band_name="433 MHz ISM Telemetry Band", profile_category="ISM_Telemetry")
        self.assertFalse(data["is_voice_broadcast"])
        self.assertFalse(data["is_any_voice"])
        self.assertEqual(data["voice_label"], "[📊 DATA / TELEM]")

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
