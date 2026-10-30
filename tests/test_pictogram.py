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
        print(f"[TEST PASS] AI Analysis: {res.category} -> {res.label} (Conf: {res.confidence*100:.0f}%, Latency: {res.latency_ms:.1f}ms)")

if __name__ == "__main__":
    unittest.main(exit=False)
    import os
    os._exit(0)
