from __future__ import annotations

import unittest
import sqlite3
import gzip
from pathlib import Path
import subprocess
import sys
import tempfile

import numpy as np

from scripts.analysis.run_strategy_idea_e5 import volatility_regime

from arb_research.experiments.strategy_idea_validation import (
    LogisticProbabilityModel,
    chronological_market_split,
    complement_mid_probability,
    e0_feature_values,
    latest_polymarket_state,
    maker_limit_from_opposite_ask,
    maker_trade_fill_evidence,
    probability_metrics,
    read_partition_rows,
    realized_volatility_bps,
    select_recovery_entry,
    select_target_exit,
    select_stable_entry,
    select_reversal_signal,
    required_remaining_average,
    time_weighted_mean,
)


class StrategyIdeaValidationTest(unittest.TestCase):
    def test_time_weighted_mean_ignores_samples_after_observation(self) -> None:
        start_us = 1_000_000
        observation_us = 201_000_000
        samples = [
            (101_000_000, 110.0),
            (201_000_000, 999.0),
            (250_000_000, 999.0),
        ]

        mean = time_weighted_mean(
            start_us=start_us,
            observation_us=observation_us,
            anchor_price=100.0,
            samples=samples,
        )

        self.assertAlmostEqual(mean, 105.0)

    def test_required_remaining_average_uses_elapsed_twap(self) -> None:
        value = required_remaining_average(
            anchor_price=100.0,
            elapsed_twap=105.0,
            elapsed_seconds=200.0,
            total_seconds=300.0,
        )

        self.assertAlmostEqual(value, 90.0)

    def test_chronological_split_keeps_all_rows_of_a_market_together(self) -> None:
        rows = [
            {"marketId": "m1", "marketStartUs": 1, "remainingSeconds": 90},
            {"marketId": "m1", "marketStartUs": 1, "remainingSeconds": 30},
            {"marketId": "m2", "marketStartUs": 2, "remainingSeconds": 90},
            {"marketId": "m3", "marketStartUs": 3, "remainingSeconds": 90},
            {"marketId": "m4", "marketStartUs": 4, "remainingSeconds": 90},
        ]

        train, validation = chronological_market_split(rows, train_fraction=0.5)

        self.assertEqual({row["marketId"] for row in train}, {"m1", "m2"})
        self.assertEqual({row["marketId"] for row in validation}, {"m3", "m4"})

    def test_complement_mid_probability_uses_both_executable_sides(self) -> None:
        probability = complement_mid_probability(up_ask=0.62, down_ask=0.40)

        self.assertAlmostEqual(probability, 0.61)

    def test_logistic_probability_model_learns_monotone_direction(self) -> None:
        features = np.asarray([[-3.0], [-2.0], [-1.0], [1.0], [2.0], [3.0]])
        labels = np.asarray([0, 0, 0, 1, 1, 1], dtype=float)
        model = LogisticProbabilityModel(l2=0.1).fit(features, labels)

        probabilities = model.predict_proba(np.asarray([[-2.5], [2.5]]))

        self.assertLess(probabilities[0], 0.25)
        self.assertGreater(probabilities[1], 0.75)

    def test_e0_feature_values_include_terminal_and_cumulative_interpretations(self) -> None:
        values = e0_feature_values(
            anchor_price=100.0,
            current_price=101.0,
            elapsed_twap=100.5,
            elapsed_seconds=200.0,
            realized_vol_bps=5.0,
        )

        self.assertAlmostEqual(values["terminalGapBps"], 100.0)
        self.assertAlmostEqual(values["elapsedTwapGapBps"], 50.0)
        self.assertAlmostEqual(values["requiredRemainingAverage"], 99.0)
        self.assertAlmostEqual(values["requiredMarginBps"], 200.0)

    def test_latest_polymarket_state_never_uses_later_received_snapshot(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript(
            """
            CREATE TABLE snapshots (
                event_id TEXT PRIMARY KEY, market_id TEXT, source_us INTEGER,
                received_us INTEGER, outcome TEXT, best_ask TEXT,
                best_ask_size TEXT, asks_json TEXT
            );
            """
        )
        rows = [
            ("up-before", "m1", 90, 100, "UP", "0.62", "10", '[["0.62","10"]]'),
            ("down-before", "m1", 90, 100, "DOWN", "0.40", "10", '[["0.40","10"]]'),
            ("up-after", "m1", 190, 200, "UP", "0.90", "10", '[["0.90","10"]]'),
            ("down-after", "m1", 190, 200, "DOWN", "0.10", "10", '[["0.10","10"]]'),
        ]
        connection.executemany(
            "INSERT INTO snapshots VALUES(?,?,?,?,?,?,?,?)", rows
        )

        state = latest_polymarket_state(
            connection,
            market_id="m1",
            decision_received_us=150,
            quantity=10.0,
            max_age_us=100,
        )

        self.assertIsNotNone(state)
        assert state is not None
        self.assertAlmostEqual(state["upAsk"], 0.62)
        self.assertAlmostEqual(state["downAsk"], 0.40)
        self.assertAlmostEqual(state["marketProbabilityUp"], 0.61)

    def test_probability_metrics_use_literal_brier_and_log_loss(self) -> None:
        metrics = probability_metrics(
            probabilities=np.asarray([0.8, 0.2]),
            labels=np.asarray([1.0, 0.0]),
        )

        self.assertAlmostEqual(metrics["brierScore"], 0.04)
        self.assertAlmostEqual(metrics["logLoss"], -np.log(0.8))
        self.assertEqual(metrics["correct"], 2)

    def test_partition_reader_keeps_complete_rows_before_truncated_gzip_tail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "part.ndjson.gz"
            with gzip.open(path, "wt", encoding="utf-8") as handle:
                handle.write('{"value":1}\n{"value":2}\n')
            path.write_bytes(path.read_bytes()[:-6])

            rows = list(read_partition_rows([path]))

        self.assertEqual(rows, [{"value": 1}, {"value": 2}])

    def test_realized_volatility_ignores_price_updates_after_observation(self) -> None:
        volatility = realized_volatility_bps(
            samples=[(1, 100.0), (2, 100.0), (3, 100.0), (5, 200.0)],
            observation_us=4,
            lookback_us=10,
        )

        self.assertAlmostEqual(volatility, 0.0)

    def test_recovery_entry_waits_for_price_to_rebound_from_running_low(self) -> None:
        points = [
            {"sourceUs": 1, "vwap": 0.24},
            {"sourceUs": 2, "vwap": 0.21},
            {"sourceUs": 3, "vwap": 0.215},
            {"sourceUs": 4, "vwap": 0.22},
        ]

        selected = select_recovery_entry(points, recovery=0.01)

        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected["sourceUs"], 4)

    def test_delayed_target_exit_uses_actual_price_after_delay(self) -> None:
        path = [
            {"sourceUs": 1_000_000, "vwap": 0.49},
            {"sourceUs": 2_000_000, "vwap": 0.51},
            {"sourceUs": 3_000_000, "vwap": 0.48},
        ]

        selected = select_target_exit(path, target=0.50, delay_us=1_000_000)

        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected["sourceUs"], 3_000_000)
        self.assertAlmostEqual(selected["vwap"], 0.48)

    def test_e1_script_can_start_directly_from_project_root(self) -> None:
        root = Path(__file__).resolve().parents[1]

        completed = subprocess.run(
            [sys.executable, "scripts/analysis/run_strategy_idea_e1.py", "--help"],
            cwd=root,
            capture_output=True,
            text=True,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_stable_entry_resets_after_price_drops_more_than_tolerance(self) -> None:
        points = [
            {"sourceUs": 0, "vwap": 0.80, "worstPrice": 0.80},
            {"sourceUs": 5_000_000, "vwap": 0.78, "worstPrice": 0.78},
            {"sourceUs": 10_000_000, "vwap": 0.81, "worstPrice": 0.81},
            {"sourceUs": 20_000_000, "vwap": 0.82, "worstPrice": 0.82},
        ]

        selected = select_stable_entry(
            points,
            duration_us=10_000_000,
            stability_floor=0.80,
            reset_tolerance=0.01,
            band_low=0.80,
            band_high=0.85,
            worst_price_cap=0.85,
        )

        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected["sourceUs"], 20_000_000)

    def test_reversal_signal_requires_drop_from_recent_high_inside_horizon(self) -> None:
        points = [
            {"sourceUs": 0, "vwap": 0.82},
            {"sourceUs": 11_000_000, "vwap": 0.79},
            {"sourceUs": 20_000_000, "vwap": 0.83},
            {"sourceUs": 25_000_000, "vwap": 0.80},
        ]

        selected = select_reversal_signal(
            points, horizon_us=10_000_000, drop_min=0.03, high_floor=0.80
        )

        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected["sourceUs"], 25_000_000)
        self.assertAlmostEqual(selected["dropFromHigh"], 0.03)

    def test_volatility_regime_skips_missing_spot_history(self) -> None:
        self.assertIsNone(volatility_regime(None, median=0.001))

    def test_maker_limit_uses_complementary_ask_as_executable_bid_bound(self) -> None:
        self.assertAlmostEqual(maker_limit_from_opposite_ask(0.19), 0.81)

    def test_maker_fill_evidence_distinguishes_trade_through_from_touch(self) -> None:
        trades = [
            {"receivedUs": 1_200_000, "side": "BUY", "price": 0.80, "size": 50},
            {"receivedUs": 1_400_000, "side": "SELL", "price": 0.80, "size": 6},
            {"receivedUs": 1_600_000, "side": "SELL", "price": 0.80, "size": 5},
            {"receivedUs": 1_800_000, "side": "SELL", "price": 0.79, "size": 1},
            {"receivedUs": 2_500_000, "side": "SELL", "price": 0.70, "size": 100},
        ]

        evidence = maker_trade_fill_evidence(
            trades,
            order_received_us=1_000_000,
            wait_us=1_000_000,
            limit_price=0.80,
            quantity=10.0,
        )

        self.assertTrue(evidence["touchFill"])
        self.assertEqual(evidence["touchFillUs"], 1_600_000)
        self.assertTrue(evidence["tradeThroughFill"])
        self.assertEqual(evidence["tradeThroughFillUs"], 1_800_000)
        self.assertAlmostEqual(evidence["touchVolume"], 12.0)


if __name__ == "__main__":
    unittest.main()
