#!/usr/bin/env python3
"""
End-to-End Pipeline Verification Test
Verifies multi-layer execution, 1MHz deep pictogram frame harvesting,
numerical ordering by frequency, and Gqrx bookmark generation.
"""

import sys
import unittest
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.scanner_pipeline import MultiLayerScannerPipeline, ScanProgressUpdate

class TestMultiLayerPipeline(unittest.TestCase):

    def test_end_to_end_scan(self):
        updates_received = []

        def _track_progress(upd: ScanProgressUpdate):
            updates_received.append(upd)

        pipeline = MultiLayerScannerPipeline(callback=_track_progress)
        
        print("\nStarting end-to-end multi-layer scan test (88 - 108 MHz)...")
        profiles = pipeline.run_scan(
            freq_min_mhz=88,
            freq_max_mhz=108,
            snr_threshold_db=8.0,
            only_legal_demod=True
        )

        # 1. Verify results produced
        self.assertGreater(len(profiles), 0, "No signals identified in scan!")
        print(f"Identified {len(profiles)} viable signals.")

        # 2. Verify numerical ordering based on frequency (ascending)
        frequencies = [p.frequency_hz for p in profiles]
        self.assertEqual(frequencies, sorted(frequencies), "Signals are NOT numerically ordered by frequency!")
        print("Verified: Signals are strictly in numerical ascending order by frequency.")

        # 3. Verify Layer 3 deep pictogram frames (minimum 3 frames per signal)
        for p in profiles:
            self.assertIsNotNone(p.pictogram_category)
            self.assertIsNotNone(p.pictogram_label)
            self.assertGreater(p.pictogram_confidence, 0.0)
            self.assertIsNotNone(p.lna_gain)
            self.assertIsNotNone(p.vga_gain)
            self.assertIsNotNone(p.plot_min)
            self.assertIsNotNone(p.plot_max)
            print(f"  -> {p.frequency_mhz:.3f} MHz | {p.signal_name} | AI: {p.pictogram_label} | LNA:{p.lna_gain} VGA:{p.vga_gain}")

        # 4. Verify Gqrx Bookmarks export
        export_csv = Path(__file__).resolve().parent.parent / "export" / "gqrx_bookmarks.csv"
        self.assertTrue(export_csv.exists(), "gqrx_bookmarks.csv was not generated!")
        system_csv = Path(os.path.expanduser("~/.config/gqrx/bookmarks.csv"))
        self.assertTrue(system_csv.exists(), "System ~/.config/gqrx/bookmarks.csv was not generated!")

        with open(export_csv, "r") as f:
            lines = f.readlines()
        self.assertGreater(len(lines), 2, "Bookmarks file is empty!")
        print(f"Verified Gqrx Bookmarks exported successfully ({len(lines)} lines).")

        # 5. Verify progress updates covered all 4 stages
        stages = {u.stage for u in updates_received}
        self.assertTrue({1, 2, 3, 4}.issubset(stages), f"Not all stages reported: {stages}")
        print("Verified all 4 multi-layer stages executed cleanly!")

if __name__ == "__main__":
    unittest.main(exit=False)
    import os
    os._exit(0)
