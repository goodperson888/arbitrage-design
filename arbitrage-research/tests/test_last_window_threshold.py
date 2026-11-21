from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import unittest

from arb_research.backtest import run_backtest
from arb_research.io import read_events
from arb_research.models import MarketEvent
from arb_research.strategy import LastWindowThresholdConfig, LastWindowThresholdStrategy


ROOT = Path(__file__).resolve().parents[1]


class LastWindowThresholdTest(unittest.TestCase):
    def test_demo_backtest_records_one_win_and_one_loss(self) -> None:
        config = LastWindowThresholdConfig(
            strategy_id="prediction_last_window_threshold",
            version=1,
            window_seconds=120,
            threshold_price=Decimal("0.80"),
            max_entry_price=Decimal("0.82"),
            order_size=Decimal("1"),
            min_ask_size=Decimal("1"),
            fee_model="polymarket_crypto_taker",
            fee_rate=Decimal("0.07"),
        )

        result = run_backtest(
            read_events(ROOT / "examples" / "prediction_events.ndjson"),
            LastWindowThresholdStrategy(config),
        )

        self.assertEqual(result.metrics["fillCount"], 2)
        self.assertEqual(result.metrics["settledFillCount"], 2)
        self.assertEqual(result.metrics["winCount"], 1)
        self.assertEqual(result.metrics["winRate"], "0.5")
        self.assertEqual(result.fills[0].outcome, "UP")
        self.assertEqual(result.fills[0].winner_outcome, "UP")
        self.assertEqual(result.fills[1].outcome, "DOWN")
        self.assertEqual(result.fills[1].winner_outcome, "UP")

    def test_ignores_quotes_before_last_window(self) -> None:
        config = LastWindowThresholdConfig(
            strategy_id="prediction_last_window_threshold",
            version=1,
            window_seconds=60,
            threshold_price=Decimal("0.80"),
            max_entry_price=Decimal("0.82"),
            order_size=Decimal("1"),
            min_ask_size=Decimal("1"),
            fee_model="none",
            fee_rate=Decimal("0"),
        )

        result = run_backtest(
            read_events(ROOT / "examples" / "prediction_events.ndjson"),
            LastWindowThresholdStrategy(config),
        )

        self.assertEqual(result.metrics["fillCount"], 0)

    def test_does_not_trade_stale_quote_from_other_outcome(self) -> None:
        config = LastWindowThresholdConfig(
            strategy_id="prediction_last_window_threshold",
            version=2,
            window_seconds=120,
            threshold_price=Decimal("0.70"),
            max_entry_price=Decimal("0.88"),
            order_size=Decimal("1"),
            min_ask_size=Decimal("1"),
            fee_model="none",
            fee_rate=Decimal("0"),
        )
        rows = [
            _event("MARKET_DEFINITION", 0, {
                "eventStartUs": "0",
                "eventEndUs": "300000000",
                "outcomes": ["UP", "DOWN"],
            }),
            _event("PRICE_QUOTE", 100_000_000, {
                "outcome": "DOWN", "bestAsk": "0.75", "bestAskSize": "10",
            }, outcome="DOWN"),
            _event("PRICE_QUOTE", 190_000_000, {
                "outcome": "UP", "bestAsk": "0.20", "bestAskSize": "10",
            }, outcome="UP"),
            _event("SETTLEMENT", 301_000_000, {"winnerOutcome": "DOWN"}),
        ]

        result = run_backtest(rows, LastWindowThresholdStrategy(config))

        self.assertEqual(result.metrics["fillCount"], 0)

    def test_respects_minimum_remaining_seconds(self) -> None:
        config = LastWindowThresholdConfig(
            strategy_id="prediction_last_window_threshold",
            version=3,
            window_seconds=120,
            threshold_price=Decimal("0.80"),
            max_entry_price=Decimal("0.88"),
            order_size=Decimal("1"),
            min_ask_size=Decimal("1"),
            fee_model="none",
            fee_rate=Decimal("0"),
            min_remaining_seconds=60,
        )
        rows = [
            _event("MARKET_DEFINITION", 0, {
                "eventStartUs": "0",
                "eventEndUs": "300000000",
                "outcomes": ["UP", "DOWN"],
            }),
            _event("PRICE_QUOTE", 250_000_000, {
                "outcome": "UP", "bestAsk": "0.82", "bestAskSize": "10",
            }, outcome="UP"),
            _event("SETTLEMENT", 301_000_000, {"winnerOutcome": "UP"}),
        ]

        result = run_backtest(rows, LastWindowThresholdStrategy(config))

        self.assertEqual(result.metrics["fillCount"], 0)


def _event(
    event_type: str,
    source_time_us: int,
    payload: dict[str, str | list[str]],
    *,
    outcome: str | None = None,
) -> MarketEvent:
    return MarketEvent(
        schema_version=1,
        event_type=event_type,
        venue="POLYMARKET",
        market_id="market-1",
        source_time_us=source_time_us,
        received_time_us=source_time_us,
        payload=payload,
        outcome=outcome,
    )


if __name__ == "__main__":
    unittest.main()
