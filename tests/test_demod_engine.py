#!/usr/bin/env python3
"""
tests/test_demod_engine.py - Unit tests for Demodulation & Decoder Engine
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.demod_engine import (
    get_installed_demod_tools,
    identify_atsc_tv_channel,
    get_demod_profile_for_signal,
    run_telemetry_decode,
    fm_demodulate_iq_to_pcm,
    run_atsc_tv_analysis,
    demodulate_signal,
)


class TestDemodEngine(unittest.TestCase):

    def test_installed_tools_detection(self):
        tools = get_installed_demod_tools()
        self.assertIsInstance(tools, dict)
        self.assertTrue(tools.get("gqrx", False))
        self.assertTrue(tools.get("rtl_433", False))
        self.assertTrue(tools.get("multimon_ng", False))
        self.assertTrue(tools.get("readsb", False))
        self.assertTrue(tools.get("w_scan_cpp", False))

    def test_atsc_tv_channel_identification(self):
        # VHF Low: Channel 2 (54-60 MHz)
        ch2 = identify_atsc_tv_channel(57_000_000)
        self.assertIsNotNone(ch2)
        self.assertEqual(ch2[0], 2)
        self.assertAlmostEqual(ch2[1], 57_000_000)
        self.assertAlmostEqual(ch2[2], 54_000_000)
        self.assertAlmostEqual(ch2[3], 54_309_441, delta=1.0)

        # VHF High: Channel 7 (174-180 MHz)
        ch7 = identify_atsc_tv_channel(175_500_000)
        self.assertIsNotNone(ch7)
        self.assertEqual(ch7[0], 7)

        # UHF: Channel 14 (470-476 MHz)
        ch14 = identify_atsc_tv_channel(473_000_000)
        self.assertIsNotNone(ch14)
        self.assertEqual(ch14[0], 14)
        self.assertAlmostEqual(ch14[2], 470_000_000)
        self.assertAlmostEqual(ch14[3], 470_309_441, delta=1.0)

        # UHF: Channel 36 (602-608 MHz)
        ch36 = identify_atsc_tv_channel(605_000_000)
        self.assertIsNotNone(ch36)
        self.assertEqual(ch36[0], 36)

        # Non-TV frequencies
        self.assertIsNone(identify_atsc_tv_channel(107_900_000))
        self.assertIsNone(identify_atsc_tv_channel(146_520_000))
        self.assertIsNone(identify_atsc_tv_channel(433_920_000))

    def test_voice_broadcast_demod_profiles(self):
        # 1. FM Commercial Broadcast
        p_fm = get_demod_profile_for_signal(
            freq_hz=107_900_000, demod="WFM (stereo)", band_name="VHF FM Commercial Broadcast", obw_hz=180_000
        )
        self.assertEqual(p_fm.tool_id, "gqrx")
        self.assertEqual(p_fm.category, "Voice_Broadcast")
        self.assertEqual(p_fm.demod_label, "[📻 VOICE BROADCAST]")
        self.assertTrue(p_fm.is_installed)

        # 2. NOAA All-Hazards Emergency Weather Radio
        p_wx = get_demod_profile_for_signal(
            freq_hz=162_550_000, demod="Narrow FM", band_name="NOAA All-Hazards Weather Radio"
        )
        self.assertEqual(p_wx.tool_id, "gqrx")
        self.assertEqual(p_wx.demod_label, "[🎙️ WX VOICE BROADCAST]")

        # 3. Aviation ATIS Voice Broadcast
        p_atis = get_demod_profile_for_signal(
            freq_hz=125_600_000, demod="AM", band_name="KDEN ATIS Weather"
        )
        self.assertEqual(p_atis.tool_id, "gqrx")
        self.assertEqual(p_atis.demod_label, "[🎙️ ATIS VOICE BROADCAST]")

    def test_telemetry_tv_and_data_profiles(self):
        # 1. 433 MHz ISM IoT Telemetry (weather stations, sensors, meters)
        p_433 = get_demod_profile_for_signal(
            freq_hz=433_920_000, demod="Narrow FM", band_name="433 MHz ISM Telemetry Band", profile_category="ISM_Telemetry"
        )
        self.assertEqual(p_433.tool_id, "rtl_433")
        self.assertEqual(p_433.category, "ISM_Telemetry")
        self.assertIn("TELEMETRY", p_433.demod_label)
        self.assertTrue(p_433.is_installed)
        self.assertIn("rtl_433", p_433.command)

        # 2. 915 MHz ISM Smart Meter / LoRa
        p_915 = get_demod_profile_for_signal(
            freq_hz=915_000_000, demod="Narrow FM", band_name="902-928 MHz ISM / 33cm Band"
        )
        self.assertEqual(p_915.tool_id, "rtl_433")

        # 3. ATSC Digital Broadcast Television Channel 14
        p_tv = get_demod_profile_for_signal(
            freq_hz=473_000_000, demod="Raw I/Q", band_name="UHF Digital Broadcast TV (Ch 14-36)", profile_category="Broadcast_TV"
        )
        self.assertEqual(p_tv.tool_id, "w_scan_cpp")
        self.assertEqual(p_tv.category, "Broadcast_TV")
        self.assertEqual(p_tv.atsc_channel, 14)
        self.assertIn("BROADCAST TV (Ch 14)", p_tv.demod_label)

        # 4. Aviation 1090 MHz ADS-B Transponder
        p_adsb = get_demod_profile_for_signal(
            freq_hz=1_090_000_000, demod="AM", band_name="ADS-B 1090 MHz Aircraft Transponders"
        )
        self.assertEqual(p_adsb.tool_id, "readsb")
        self.assertEqual(p_adsb.demod_label, "[✈️ ADS-B TELEMETRY]")

        # 5. APRS Amateur Packet Radio (144.390 MHz)
        p_aprs = get_demod_profile_for_signal(
            freq_hz=144_390_000, demod="Narrow FM", band_name="2m Amateur Radio Band"
        )
        self.assertIn(p_aprs.tool_id, ("direwolf", "multimon_ng"))
        self.assertEqual(p_aprs.demod_label, "[📍 APRS TELEMETRY]")

    def test_fm_demodulate_iq_to_pcm(self):
        import numpy as np
        # 10,000 synthetic IQ bytes
        raw = np.random.randint(-128, 127, size=10_000, dtype=np.int8).tobytes()
        pcm = fm_demodulate_iq_to_pcm(raw, in_rate=2_000_000, out_rate=22_050)
        self.assertIsInstance(pcm, bytes)
        self.assertGreater(len(pcm), 0)
        # Even number of bytes (16-bit PCM)
        self.assertEqual(len(pcm) % 2, 0)

    def test_run_atsc_tv_analysis(self):
        # Channel 14 (470 - 476 MHz, center 473 MHz, pilot 470.3094 MHz)
        res = run_atsc_tv_analysis(473_000_000)
        self.assertTrue(res["is_atsc_tv"])
        self.assertEqual(res["channel"], 14)
        self.assertEqual(res["center_freq_mhz"], 473.0)
        self.assertAlmostEqual(res["pilot_carrier_mhz"], 470.3094, places=3)
        self.assertEqual(res["scanner_tool"], "w_scan_cpp")

        # Non-TV carrier
        res_non = run_atsc_tv_analysis(107_900_000)
        self.assertFalse(res_non["is_atsc_tv"])
        self.assertIsNone(res_non["channel"])

    def test_demodulate_signal_dispatch(self):
        # 1. Telemetry dispatch
        res_telem = demodulate_signal(433_920_000, demod="Narrow FM", band_name="433 MHz ISM Telemetry Band")
        self.assertEqual(res_telem["tool_id"], "rtl_433")
        self.assertEqual(res_telem["demod_data"]["type"], "TELEMETRY")

        # 2. TV dispatch
        res_tv = demodulate_signal(473_000_000, demod="Raw I/Q", band_name="UHF Digital Broadcast TV (Ch 14-36)")
        self.assertEqual(res_tv["tool_id"], "w_scan_cpp")
        self.assertEqual(res_tv["demod_data"]["type"], "BROADCAST_TV")
        self.assertEqual(res_tv["demod_data"]["tv_info"]["channel"], 14)

        # 3. Voice broadcast dispatch
        res_voice = demodulate_signal(107_900_000, demod="WFM (stereo)", band_name="VHF FM Commercial Broadcast")
        self.assertEqual(res_voice["tool_id"], "gqrx")
        self.assertEqual(res_voice["demod_data"]["type"], "VOICE_AUDIO")


if __name__ == "__main__":
    unittest.main()
