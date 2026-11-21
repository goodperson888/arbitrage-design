from __future__ import annotations

from decimal import Decimal
import unittest

from arb_research.models import MarketEvent
from arb_research.validation import build_last_window_report


class LastWindowValidationTest(unittest.TestCase):
    def test_complete_dataset_produces_directional_range_result(self) -> None:
        events = [_definition()]
        for second in range(60, 181, 5):
            events.append(_quote(second, "UP", "0.75" if second == 60 else "0.90"))
        events.append(_settlement("UP"))

        report = build_last_window_report(
            events,
            windows_seconds=(120,),
            price_min=Decimal("0.70"),
            price_max=Decimal("0.88"),
            bucket_size=Decimal("0.05"),
            max_quote_gap_seconds=Decimal("5"),
        )

        self.assertTrue(report["datasetComplete"])
        row = next(
            row for row in report["results"]
            if row["bucketType"] == "RANGE" and row["outcome"] == "UP"
        )
        self.assertEqual(row["settledTradeCount"], 1)
        self.assertEqual(row["winCount"], 1)
        self.assertEqual(row["winRate"], "1")
        self.assertEqual(row["baselineOutcomeWinRate"], "1")
        self.assertEqual(row["winRateLiftVsUnconditional"], "0")
        self.assertIsNotNone(row["winRateCi95Low"])

    def test_missing_end_of_window_marks_report_provisional(self) -> None:
        events = [_definition(), _quote(60, "UP", "0.75"), _quote(120, "UP", "0.80"), _settlement("UP")]

        report = build_last_window_report(
            events,
            windows_seconds=(120,),
            bucket_size=Decimal("0.05"),
            max_quote_gap_seconds=Decimal("5"),
        )

        self.assertFalse(report["datasetComplete"])
        self.assertTrue(report["provisional"])
        self.assertIn("incomplete quote coverage", report["warnings"][0])

    def test_trade_source_requires_complete_history_and_uses_buy_only(self) -> None:
        definition = _definition()
        definition.payload["tradeHistoryComplete"] = True
        events = [
            definition,
            _trade(70, "UP", "SELL", "0.70"),
            _trade(80, "UP", "BUY", "0.75"),
            _settlement("UP"),
        ]

        report = build_last_window_report(
            events,
            windows_seconds=(120,),
            bucket_size=Decimal("0.05"),
            price_source="trade",
        )

        self.assertTrue(report["datasetComplete"])
        row = next(
            row for row in report["results"]
            if row["bucketType"] == "RANGE" and row["outcome"] == "UP"
        )
        self.assertEqual(row["settledTradeCount"], 1)
        self.assertEqual(row["averageEntryPrice"], "0.75")

        definition.payload["tradeHistoryComplete"] = False
        incomplete = build_last_window_report(
            events,
            windows_seconds=(120,),
            bucket_size=Decimal("0.05"),
            price_source="trade",
        )
        self.assertFalse(incomplete["datasetComplete"])
        self.assertTrue(incomplete["provisional"])


def _definition() -> MarketEvent:
    return MarketEvent(
        schema_version=1,
        event_type="MARKET_DEFINITION",
        venue="POLYMARKET",
        market_id="market-1",
        source_time_us=0,
        received_time_us=0,
        payload={
            "eventStartUs": "0",
            "eventEndUs": "180000000",
            "outcomes": ["UP", "DOWN"],
        },
    )


def _quote(second: int, outcome: str, price: str) -> MarketEvent:
    timestamp = second * 1_000_000
    return MarketEvent(
        schema_version=1,
        event_type="PRICE_QUOTE",
        venue="POLYMARKET",
        market_id="market-1",
        source_time_us=timestamp,
        received_time_us=timestamp,
        outcome=outcome,
        payload={
            "outcome": outcome,
            "bestAsk": price,
            "bestAskSize": "10",
            "bestBid": str(Decimal(price) - Decimal("0.01")),
            "bestBidSize": "10",
        },
    )


def _settlement(winner: str) -> MarketEvent:
    return MarketEvent(
        schema_version=1,
        event_type="SETTLEMENT",
        venue="POLYMARKET",
        market_id="market-1",
        source_time_us=181_000_000,
        received_time_us=181_000_000,
        payload={"winnerOutcome": winner, "settlementPrice": "1"},
    )


def _trade(second: int, outcome: str, side: str, price: str) -> MarketEvent:
    timestamp = second * 1_000_000
    return MarketEvent(
        schema_version=1,
        event_type="TRADE",
        venue="POLYMARKET",
        market_id="market-1",
        source_time_us=timestamp,
        received_time_us=timestamp,
        outcome=outcome,
        payload={
            "outcome": outcome,
            "side": side,
            "price": price,
            "size": "10",
        },
    )


if __name__ == "__main__":
    unittest.main()
