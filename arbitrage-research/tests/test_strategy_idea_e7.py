from __future__ import annotations

import unittest

from scripts.analysis.run_strategy_idea_e7 import (
    DETAIL_FIELDS,
    E2_STABILITY,
    FULL_LIFECYCLE_WINDOWS,
    REVERSAL_GRID,
    accumulate_summary_rows,
    build_result,
    lifecycle_window,
    parse_reversal_drops,
    parse_reversal_horizons,
    precompute_points,
    select_fixed_remaining_exit,
    select_take_profit_exit,
)


class StrategyIdeaE7Test(unittest.TestCase):
    def test_reversal_horizon_parser_accepts_one_to_four_seconds(self) -> None:
        self.assertEqual(parse_reversal_horizons("1,2,3,4,5"), (1, 2, 3, 4, 5))

    def test_reversal_drop_parser_accepts_extended_grid(self) -> None:
        self.assertEqual(parse_reversal_drops("0.10,0.20,0.60"), (0.10, 0.20, 0.60))

    def test_e2_includes_five_second_stability_window(self) -> None:
        self.assertIn(5, E2_STABILITY)

    def test_lifecycle_window_labels_full_300_second_axis(self) -> None:
        self.assertEqual(lifecycle_window(299.9), "300_240")
        self.assertEqual(lifecycle_window(200), "240_180")
        self.assertEqual(lifecycle_window(120), "180_120")
        self.assertEqual(lifecycle_window(0.1), "5_0")

    def test_summary_accumulator_preserves_entries_and_pnl_without_details(self) -> None:
        groups = {}
        rows = [
            {
                "split": "validation", "experiment": "E1_full_lifecycle",
                "entryWindow": "300_240", "case": "20_30", "exitRule": "settlement",
                "won": True, "pnlPerShare": 0.10, "holdPnlPerShare": 0.10,
                "exitMode": "settlement",
            },
            {
                "split": "validation", "experiment": "E1_full_lifecycle",
                "entryWindow": "300_240", "case": "20_30", "exitRule": "settlement",
                "won": False, "pnlPerShare": -0.20, "holdPnlPerShare": -0.20,
                "exitMode": "settlement",
            },
        ]
        accumulate_summary_rows(groups, rows)
        result = build_result([], 1, 1, grouped=groups, selected_markets=1, detail_mode="summary_only")
        summary = result["summaries"][0]
        self.assertEqual(result["rowCount"], 2)
        self.assertEqual(summary["entries"], 2)
        self.assertEqual(summary["winsAtResolution"], 1)
        self.assertAlmostEqual(summary["averagePnlPerShare"], -0.05)
        self.assertEqual(result["detailMode"], "summary_only")

    def test_reversal_opposite_entry_reads_opposite_asks_not_complement_bids(self) -> None:
        points, _sells = precompute_points(
            [{
                "sourceUs": 100,
                "asks": {
                    "UP": [["0.20", "10"]],
                    "DOWN": [["0.80", "10"]],
                },
            }],
            300,
        )

        self.assertAlmostEqual(points["DOWN"][0]["vwap"], 0.80)
        self.assertAlmostEqual(points["DOWN"][0]["oppositeVwap"], 0.20)

    def test_detail_schema_contains_reversal_fields_even_when_first_rows_are_e1(self) -> None:
        self.assertIn("horizon", DETAIL_FIELDS)
        self.assertIn("highOutcome", DETAIL_FIELDS)
        self.assertIn("dropFromHigh", DETAIL_FIELDS)
        self.assertIn("entryWindow", DETAIL_FIELDS)
        self.assertIn("exitRule", DETAIL_FIELDS)

    def test_full_lifecycle_windows_cover_300_to_zero_without_overlap(self) -> None:
        labels = [label for label, _minimum, _maximum in FULL_LIFECYCLE_WINDOWS]
        self.assertEqual(labels[0], "300_240")
        self.assertEqual(labels[-1], "5_0")
        intervals = [(minimum, maximum) for _label, minimum, maximum in FULL_LIFECYCLE_WINDOWS]
        self.assertEqual(len(intervals), 8)
        for left, right in zip(intervals, intervals[1:]):
            self.assertEqual(left[0], right[1])

    def test_fixed_remaining_exit_uses_first_point_after_entry(self) -> None:
        path = [
            {"sourceUs": 1_000_000, "remaining": 59, "vwap": 0.70},
            {"sourceUs": 2_000_000, "remaining": 30, "vwap": 0.75},
        ]

        selected = select_fixed_remaining_exit(path, remaining_seconds=60)

        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected["sourceUs"], 1_000_000)

    def test_reversal_grid_decouples_four_horizons_and_six_drop_sizes(self) -> None:
        self.assertEqual(len(REVERSAL_GRID), 24)
        self.assertIn((5, 0.10), REVERSAL_GRID)
        self.assertIn((30, 0.01), REVERSAL_GRID)

    def test_take_profit_exit_uses_first_reachable_target(self) -> None:
        path = [
            {"sourceUs": 1_000_000, "remaining": 50, "vwap": 0.20},
            {"sourceUs": 2_000_000, "remaining": 49, "vwap": 0.26},
        ]

        selected = select_take_profit_exit(path, target=0.25)

        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected["sourceUs"], 2_000_000)


if __name__ == "__main__":
    unittest.main()
