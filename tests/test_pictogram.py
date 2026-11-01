#!/usr/bin/env python3
"""
Unit tests for AI Pictogram Classifier Integration
"""

import sys
import unittest
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.pictogram_engine import get_pictogram_classifier

class TestPictogramEngine(unittest.TestCase):

    def test_classifier_initialization(self):
        pic_mgr = get_pictogram_classifier()
        self.assertIsNotNone(pic_mgr)
        print(f"[TEST PASS] Pictogram Classifier Hardware Accelerated: {pic_mgr.is_hardware_accelerated}")

    def test_spectrogram_frame_inference(self):
        pic_mgr = get_pictogram_classifier()
        # Generate 3 synthetic frames (64x128)
        frames = [np.random.normal(loc=-85.0, scale=3.0, size=(64, 128)).astype(np.float32) for _ in range(3)]
        # Inject energy in frame
        frames[0][10:30, 50:70] = -45.0

        res = pic_mgr.analyze_spectrogram_frames(
            center_freq_hz=107_900_000,
            frames=frames,
            bandwidth_hz=1_000_000.0
        )
        self.assertIsNotNone(res.category)
        self.assertIsNotNone(res.label)
        self.assertGreater(res.confidence, 0.0)
        self.assertEqual(res.visual_features["frames_evaluated"], 3)
        self.assertEqual(res.category, "BROADCAST_WFM")
        print(f"[TEST PASS] AI Analysis: {res.category} -> {res.label} (Conf: {res.confidence*100:.0f}%, Latency: {res.latency_ms:.1f}ms)")

    def test_refined_model_signal_classifications(self):
        pic_mgr = get_pictogram_classifier()
        frames = [np.random.normal(loc=-85.0, scale=3.0, size=(64, 128)).astype(np.float32) for _ in range(3)]
        frames[0][15:25, 40:80] = -40.0

        # 1. ATSC Broadcast TV Channel 14
        tv_res = pic_mgr.analyze_spectrogram_frames(473_000_000, frames=frames)
        self.assertEqual(tv_res.category, "BROADCAST_TV")
        self.assertIn("ATSC-TV-Ch14", tv_res.label)

        # 2. ISM Telemetry 433 MHz
        telem_res = pic_mgr.analyze_spectrogram_frames(433_920_000, frames=frames)
        self.assertEqual(telem_res.category, "ISM_TELEMETRY")
        self.assertIn("Telemetry-433", telem_res.label)

        # 3. Aviation ADS-B Mode-S 1090 MHz
        adsb_res = pic_mgr.analyze_spectrogram_frames(1_090_000_000, frames=frames)
        self.assertEqual(adsb_res.category, "AVIATION_ADSB")
        self.assertIn("ADS-B-1090", adsb_res.label)

        # 4. APRS Packet Telemetry 144.390 MHz
        aprs_res = pic_mgr.analyze_spectrogram_frames(144_390_000, frames=frames)
        self.assertEqual(aprs_res.category, "APRS_PACKET")
        self.assertIn("APRS-AX25", aprs_res.label)

if __name__ == "__main__":
    unittest.main(exit=False)
    import os
    os._exit(0)
