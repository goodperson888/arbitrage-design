from __future__ import annotations

import unittest

from scripts.analysis.run_strategy_idea_e10 import (
    enrich_e0,
    failure_coverage,
    summarize_variant,
)


class StrategyIdeaE10Test(unittest.TestCase):
    def test_e0_join_uses_latest_observation_before_execution(self) -> None:
        rows = [{"marketId": "m1", "executionUs": 12_000_000, "outcome": "DOWN"}]
        observations = [
            {"marketId": "m1", "observationSourceUs": "10_000_000", "combinedProbabilityUp": "0.40"},
            {"marketId": "m1", "observationSourceUs": "13_000_000", "combinedProbabilityUp": "0.90"},
        ]

        enriched = enrich_e0(rows, observations, max_age_seconds=45)

        self.assertAlmostEqual(enriched[0]["e0ProbabilityForEntry"], 0.60)
        self.assertAlmostEqual(enriched[0]["e0AgeSeconds"], 2.0)

    def test_e0_join_does_not_use_future_observation(self) -> None:
        rows = [{"marketId": "m1", "executionUs": 12_000_000, "outcome": "DOWN"}]
        observations = [{
            "marketId": "m1", "observationSourceUs": "13_000_000", "combinedProbabilityUp": "0.10",
        }]

        enriched = enrich_e0(rows, observations, max_age_seconds=45)

        self.assertIsNone(enriched[0]["e0ProbabilityForEntry"])

    def test_failure_coverage_counts_removed_losses_and_wins(self) -> None:
        rows = [
            {"won": False, "pnlPerShare": -0.2},
            {"won": True, "pnlPerShare": 0.1},
            {"won": False, "pnlPerShare": -0.2},
        ]
        kept = rows[:2]

        coverage = failure_coverage(rows, kept)

        self.assertEqual(coverage["excludedLosses"], 1)
        self.assertEqual(coverage["excludedWins"], 0)
        self.assertAlmostEqual(coverage["excludedLossRate"], 0.5)

    def test_summary_reports_net_pnl_after_existing_row_costs(self) -> None:
        summary = summarize_variant([
            {"won": True, "pnlPerShare": 0.1},
            {"won": False, "pnlPerShare": -0.2},
        ])

        self.assertEqual(summary["trades"], 2)
        self.assertEqual(summary["wins"], 1)
        self.assertAlmostEqual(summary["winRate"], 0.5)
        self.assertAlmostEqual(summary["avgPnlPerShare"], -0.05)


if __name__ == "__main__":
    unittest.main()
