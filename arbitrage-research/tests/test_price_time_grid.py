from __future__ import annotations

import unittest
from decimal import Decimal

from scripts.analysis.analyze_price_time_grid import (
    PRICE_BANDS,
    classify_price_band,
    favorite_outcome,
    in_target_window,
)


class PriceTimeGridTest(unittest.TestCase):
    def test_target_window_uses_five_second_tolerance(self) -> None:
        self.assertTrue(in_target_window(Decimal("64.9"), 60))
        self.assertTrue(in_target_window(Decimal("55.0"), 60))
        self.assertFalse(in_target_window(Decimal("54.9"), 60))

    def test_price_bands_are_lower_inclusive_and_upper_exclusive(self) -> None:
        self.assertEqual(classify_price_band(Decimal("0.50"), PRICE_BANDS), "50_60")
        self.assertEqual(classify_price_band(Decimal("0.60"), PRICE_BANDS), "60_70")
        self.assertEqual(classify_price_band(Decimal("0.90"), PRICE_BANDS), "90_99")
        self.assertIsNone(classify_price_band(Decimal("0.99"), PRICE_BANDS))

    def test_favorite_requires_a_strictly_larger_executable_vwap(self) -> None:
        self.assertEqual(favorite_outcome({"UP": Decimal("0.72"), "DOWN": Decimal("0.28")}), "UP")
        self.assertEqual(favorite_outcome({"UP": Decimal("0.22"), "DOWN": Decimal("0.78")}), "DOWN")
        self.assertIsNone(favorite_outcome({"UP": Decimal("0.50"), "DOWN": Decimal("0.50")}))
        self.assertIsNone(favorite_outcome({"UP": Decimal("0.50")}))


if __name__ == "__main__":
    unittest.main()
