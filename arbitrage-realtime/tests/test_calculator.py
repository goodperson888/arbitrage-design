import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from realtime_probability.calculator import CalculatorConfig, RealtimeObservation, RealtimeProbabilityEngine
from realtime_probability.history import LightweightFactRecorder


class RealtimeProbabilityTests(unittest.TestCase):
    def test_current_session_high_low_and_distance_are_online(self) -> None:
        engine = RealtimeProbabilityEngine()
        for timestamp, price in ((100.0, 100.0), (101.0, 102.0), (102.0, 99.0)):
            result = engine.update(RealtimeObservation("m", 100.0, 400.0, price, timestamp))
        self.assertEqual(result["session_high"], 102.0)
        self.assertEqual(result["session_low"], 99.0)
        self.assertAlmostEqual(result["distance_from_open_bps"], -100.0)
        self.assertGreater(result["probability_down"], result["probability_up"])
        self.assertTrue(result["entry_allowed"])

    def test_stale_feed_blocks_entry(self) -> None:
        engine = RealtimeProbabilityEngine(CalculatorConfig(stale_after_seconds=2))
        result = engine.update(RealtimeObservation("m", 100.0, 400.0, 101.0, 101.0, source_age_ms=2501))
        self.assertEqual(result["data_quality"], "stale")
        self.assertFalse(result["entry_allowed"])

    def test_momentum_uses_five_second_lookback(self) -> None:
        engine = RealtimeProbabilityEngine(CalculatorConfig(rolling_window_seconds=60))
        for timestamp, price in ((100.0, 100.0), (101.0, 101.0), (105.0, 102.0)):
            result = engine.update(RealtimeObservation("m", 100.0, 400.0, price, timestamp))
        self.assertAlmostEqual(result["momentum_bps"], 200.0)
        self.assertIn("z_score", result)
        self.assertIn("volatility_bps_per_sqrt_second", result)

    def test_rolling_state_is_bounded_by_time(self) -> None:
        engine = RealtimeProbabilityEngine(CalculatorConfig(rolling_window_seconds=2))
        for timestamp in range(10):
            engine.update(RealtimeObservation("m", 0.0, 300.0, 100.0 + timestamp, float(timestamp)))
        state = engine._markets["m"]
        self.assertLessEqual(len(state.samples), 3)

    def test_orderbook_and_trade_facts_are_calculated_online(self) -> None:
        engine = RealtimeProbabilityEngine()
        engine.update(RealtimeObservation(
            "m", 100.0, 400.0, 100.0, 100.0,
            best_bid=99.9, best_ask=100.1, best_bid_size=3.0, best_ask_size=1.0,
            trade_side="BUY", trade_quantity=2.0,
        ))
        result = engine.update(RealtimeObservation(
            "m", 100.0, 400.0, 100.2, 101.0,
            best_bid=100.1, best_ask=100.3, best_bid_size=3.0, best_ask_size=1.0,
            trade_side="SELL", trade_quantity=1.0,
        ))
        self.assertAlmostEqual(result["book_imbalance"], 0.5)
        self.assertAlmostEqual(result["spread_bps"], 19.98, places=1)
        self.assertGreater(result["microprice_edge_bps"], 0)
        self.assertAlmostEqual(result["trade_imbalance_60s"], 1 / 3, places=5)
        self.assertAlmostEqual(result["taker_buy_ratio_60s"], 2 / 3, places=5)
        self.assertEqual(result["trade_count_60s"], 2)
        self.assertAlmostEqual(result["trade_rate_10s"], 0.2)
        self.assertFalse(result["book_stale"])

    def test_lightweight_recorder_deduplicates_time_buckets(self) -> None:
        with TemporaryDirectory() as directory:
            recorder = LightweightFactRecorder(Path(directory) / "facts.ndjson", interval_seconds=10)
            self.assertTrue(recorder.record({"market_id": "m", "observed_at": 100, "signal": "UP"}))
            self.assertFalse(recorder.record({"market_id": "m", "observed_at": 109, "signal": "UP"}))
            self.assertTrue(recorder.record({"market_id": "m", "observed_at": 110, "signal": "DOWN"}))
            rows = (Path(directory) / "facts.ndjson").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(rows), 2)
            self.assertEqual(recorder.snapshot()["records_written"], 2)


if __name__ == "__main__":
    unittest.main()
