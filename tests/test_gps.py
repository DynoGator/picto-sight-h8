#!/usr/bin/env python3
"""
Unit tests for GPS and Locality Engine
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.gps_engine import coords_to_maidenhead, get_gps_manager

class TestGPSEngine(unittest.TestCase):

    def test_maidenhead_conversion(self):
        # Denver, CO approx (39.7392, -104.9903) -> DM79
        grid = coords_to_maidenhead(39.7392, -104.9903, precision=2)
        self.assertEqual(grid, "DM79")
        
        # 6-char precision
        grid_fine = coords_to_maidenhead(39.7392, -104.9903, precision=3)
        self.assertTrue(grid_fine.startswith("DM79"))

    def test_locality_manager_resolution(self):
        mgr = get_gps_manager()
        fix = mgr.get_current_fix()
        self.assertIsNotNone(fix)
        self.assertTrue(fix.has_fix)
        self.assertIn("US", fix.region_code)
        self.assertEqual(fix.regulatory_domain, "FCC_US")
        print(f"[TEST PASS] GPS Locality: {fix.locality_name} ({fix.maidenhead_grid}) from {fix.source}")

if __name__ == "__main__":
    unittest.main()
