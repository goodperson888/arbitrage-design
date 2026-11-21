from decimal import Decimal
import unittest

from scripts.analysis.analyze_strict_upcross_chainlink import (
    chainlink_support_band,
    first_strict_upcross,
    mark_first_side_crosses,
    signed_for_outcome,
)


class StrictUpcrossChainlinkTest(unittest.TestCase):
    def test_first_strict_upcross_excludes_downward_touch(self) -> None:
        points = [
            {"sourceUs": 1_000_000, "remaining": Decimal("70"), "vwap": Decimal("0.62")},
            {"sourceUs": 2_000_000, "remaining": Decimal("69"), "vwap": Decimal("0.59")},
            {"sourceUs": 3_000_000, "remaining": Decimal("68"), "vwap": Decimal("0.61")},
        ]
        selected = first_strict_upcross(points, Decimal("0.60"), 90)
        self.assertIsNotNone(selected)
        self.assertEqual(selected["sourceUs"], 3_000_000)
        self.assertEqual(selected["priorVwap"], Decimal("0.59"))

    def test_upcross_rejects_stale_gap_and_large_overshoot(self) -> None:
        stale = [
            {"sourceUs": 1_000_000, "remaining": Decimal("70"), "vwap": Decimal("0.59")},
            {"sourceUs": 8_000_001, "remaining": Decimal("63"), "vwap": Decimal("0.61")},
        ]
        overshoot = [
            {"sourceUs": 1_000_000, "remaining": Decimal("70"), "vwap": Decimal("0.59")},
            {"sourceUs": 2_000_000, "remaining": Decimal("69"), "vwap": Decimal("0.66")},
        ]
        self.assertIsNone(first_strict_upcross(stale, Decimal("0.60"), 90))
        self.assertIsNone(first_strict_upcross(overshoot, Decimal("0.60"), 90))

    def test_upcross_must_happen_inside_requested_window(self) -> None:
        points = [
            {"sourceUs": 1_000_000, "remaining": Decimal("95"), "vwap": Decimal("0.59")},
            {"sourceUs": 2_000_000, "remaining": Decimal("94"), "vwap": Decimal("0.61")},
            {"sourceUs": 3_000_000, "remaining": Decimal("89"), "vwap": Decimal("0.62")},
        ]
        self.assertIsNone(first_strict_upcross(points, Decimal("0.60"), 90))

    def test_chainlink_support_is_signed_to_entry_direction(self) -> None:
        self.assertEqual(signed_for_outcome(Decimal("12"), "UP"), Decimal("12"))
        self.assertEqual(signed_for_outcome(Decimal("12"), "DOWN"), Decimal("-12"))
        self.assertEqual(chainlink_support_band(Decimal("-0.01")), "opposing")
        self.assertEqual(chainlink_support_band(Decimal("7")), "support_05_10")
        self.assertEqual(chainlink_support_band(Decimal("31")), "support_25_plus")

    def test_first_side_uses_earliest_unique_upcross(self) -> None:
        signals = [
            {"windowSeconds": 90, "threshold": "0.60", "outcome": "UP", "entryUs": 2},
            {"windowSeconds": 90, "threshold": "0.60", "outcome": "DOWN", "entryUs": 3},
            {"windowSeconds": 60, "threshold": "0.60", "outcome": "UP", "entryUs": 4},
            {"windowSeconds": 60, "threshold": "0.60", "outcome": "DOWN", "entryUs": 4},
        ]
        mark_first_side_crosses(signals)
        self.assertTrue(signals[0]["isFirstSideCross"])
        self.assertFalse(signals[1]["isFirstSideCross"])
        self.assertFalse(signals[2]["isFirstSideCross"])
        self.assertFalse(signals[3]["isFirstSideCross"])


if __name__ == "__main__":
    unittest.main()
