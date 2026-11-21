from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from arb_research.experiments.live_strategy_cache import (
    CachedStrategyCase,
    connect_cache,
    evaluate_cases_incrementally,
    refresh_event_cache,
)


class LiveStrategyCacheTest(unittest.TestCase):
    def test_entry_vwap_floor_is_applied_after_depth_consumption(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            events = root / "events" / "date=2026-01-01" / "hour=00" / "part-001.ndjson"
            events.parent.mkdir(parents=True)
            start = 1_700_000_000_000_000
            end = start + 300_000_000
            rows = [{"eventType": "MARKET_DEFINITION", "eventId": "definition", "marketId": "m1", "sourceTimeUs": str(start), "receivedTimeUs": str(start), "payload": {"eventStartUs": str(start), "eventEndUs": str(end)}}]
            for remaining in (90, 60, 30):
                timestamp = end - remaining * 1_000_000
                rows.append({"eventType": "ORDERBOOK_SNAPSHOT", "eventId": f"book-{remaining}", "marketId": "m1", "sourceTimeUs": str(timestamp), "receivedTimeUs": str(timestamp), "payload": {"outcomes": [{"outcome": "DOWN", "bestAsk": "0.79", "bestAskSize": "10", "asks": [["0.79", "10"]], "bids": []}]}})
            rows.extend([
                {"eventType": "TRADE", "eventId": "trade-1", "marketId": "m1", "outcome": "DOWN", "sourceTimeUs": str(end - 60_000_000), "receivedTimeUs": str(end - 59_999_000), "payload": {"outcome": "DOWN", "side": "BUY", "price": "0.79", "size": "12"}},
                {"eventType": "SETTLEMENT", "eventId": "settlement", "marketId": "m1", "sourceTimeUs": str(end), "receivedTimeUs": str(end + 1_000_000), "payload": {"winnerOutcome": "DOWN"}},
            ])
            events.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
            connection = connect_cache(root / "cache.sqlite3")
            try:
                refresh_event_cache(connection, root / "events")
                cases = [CachedStrategyCase(case_id="no_floor", trigger="buy_trade", outcome="DOWN", price_min=Decimal("0"), price_max=Decimal("0.85"), entry_vwap_min=None, max_remaining_seconds=90, min_remaining_seconds=30, quantity=Decimal("10"), min_trade_size=Decimal("10")), CachedStrategyCase(case_id="floor", trigger="buy_trade", outcome="DOWN", price_min=Decimal("0"), price_max=Decimal("0.85"), entry_vwap_min=Decimal("0.80"), max_remaining_seconds=90, min_remaining_seconds=30, quantity=Decimal("10"), min_trade_size=Decimal("10"))]
                summaries, _, _ = evaluate_cases_incrementally(connection, cases)
            finally:
                connection.close()
            self.assertEqual(summaries[0]["fillCount"], 1)
            self.assertEqual(summaries[1]["fillCount"], 0)

    def test_incremental_refresh_depth_fill_and_result_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            events = root / "events" / "date=2026-01-01" / "hour=00" / "part-001.ndjson"
            events.parent.mkdir(parents=True)
            start = 1_700_000_000_000_000
            end = start + 300_000_000
            rows = [
                {"eventType": "MARKET_DEFINITION", "eventId": "definition", "marketId": "m1",
                 "sourceTimeUs": str(start), "receivedTimeUs": str(start),
                 "payload": {"eventStartUs": str(start), "eventEndUs": str(end)}},
            ]
            for remaining in range(90, 29, -1):
                timestamp = end - remaining * 1_000_000
                rows.append({
                    "eventType": "ORDERBOOK_SNAPSHOT", "eventId": f"book-{remaining}",
                    "marketId": "m1", "sourceTimeUs": str(timestamp),
                    "receivedTimeUs": str(timestamp + 10_000),
                    "payload": {"outcomes": [
                        {"outcome": "DOWN", "bestAsk": "0.82", "bestAskSize": "6",
                         "asks": [["0.82", "6"], ["0.84", "5"]], "bids": []},
                        {"outcome": "UP", "bestAsk": "0.18", "bestAskSize": "5",
                         "asks": [["0.18", "5"]], "bids": []},
                    ]},
                })
            trade_us = end - 60_000_000
            rows.extend([
                {"eventType": "TRADE", "eventId": "trade-1", "marketId": "m1",
                 "outcome": "DOWN", "sourceTimeUs": str(trade_us),
                 "receivedTimeUs": str(trade_us + 20_000),
                 "payload": {"outcome": "DOWN", "side": "BUY", "price": "0.80", "size": "12"}},
                {"eventType": "SETTLEMENT", "eventId": "settlement", "marketId": "m1",
                 "sourceTimeUs": str(end), "receivedTimeUs": str(end + 1_000_000),
                 "payload": {"winnerOutcome": "DOWN"}},
            ])
            events.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
            connection = connect_cache(root / "cache.sqlite3")
            try:
                first_refresh = refresh_event_cache(connection, root / "events")
                second_refresh = refresh_event_cache(connection, root / "events")
                self.assertEqual(first_refresh["filesScanned"], 1)
                self.assertEqual(second_refresh["filesSkipped"], 1)
                cases = [
                    CachedStrategyCase(
                        case_id="quote", outcome="DOWN", price_min=Decimal("0.80"),
                        price_max=Decimal("0.85"), max_remaining_seconds=90,
                        min_remaining_seconds=30, quantity=Decimal("10"),
                    ),
                    CachedStrategyCase(
                        case_id="trade", trigger="buy_trade", outcome="DOWN",
                        price_min=Decimal("0.80"), price_max=Decimal("0.85"),
                        max_remaining_seconds=90, min_remaining_seconds=30,
                        quantity=Decimal("10"), min_trade_size=Decimal("10"),
                    ),
                ]
                summaries, fills, first = evaluate_cases_incrementally(connection, cases)
                _, _, second = evaluate_cases_incrementally(connection, cases)
            finally:
                connection.close()
            self.assertEqual(first["marketsEvaluated"], 2)
            self.assertEqual(second["marketsEvaluated"], 0)
            self.assertEqual(second["cachedMarketResults"], 2)
            self.assertEqual(len(fills), 2)
            self.assertTrue(all(row["fillCount"] == 1 for row in summaries))
            self.assertTrue(all(row["winRate"] == "1" for row in summaries))
            self.assertEqual(summaries[0]["averageEntryVwap"], "0.828")

    def test_entry_momentum_floor_requires_same_direction_price_rise(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            events = root / "events" / "date=2026-01-01" / "hour=00" / "part-001.ndjson"
            events.parent.mkdir(parents=True)
            start = 1_700_000_000_000_000
            end = start + 300_000_000
            rows = [{
                "eventType": "MARKET_DEFINITION", "eventId": "definition", "marketId": "m1",
                "sourceTimeUs": str(start), "receivedTimeUs": str(start),
                "payload": {"eventStartUs": str(start), "eventEndUs": str(end)},
            }]
            for remaining, ask in ((65, "0.81"), (60, "0.82")):
                timestamp = end - remaining * 1_000_000
                rows.append({
                    "eventType": "ORDERBOOK_SNAPSHOT", "eventId": f"book-{remaining}",
                    "marketId": "m1", "sourceTimeUs": str(timestamp),
                    "receivedTimeUs": str(timestamp),
                    "payload": {"outcomes": [{
                        "outcome": "DOWN", "bestAsk": ask, "bestAskSize": "10",
                        "asks": [[ask, "10"]], "bids": [],
                    }]},
                })
            trade_us = end - 60_000_000
            rows.extend([
                {"eventType": "TRADE", "eventId": "trade-1", "marketId": "m1",
                 "outcome": "DOWN", "sourceTimeUs": str(trade_us),
                 "receivedTimeUs": str(trade_us),
                 "payload": {"outcome": "DOWN", "side": "BUY", "price": "0.82", "size": "12"}},
                {"eventType": "SETTLEMENT", "eventId": "settlement", "marketId": "m1",
                 "sourceTimeUs": str(end), "receivedTimeUs": str(end + 1_000_000),
                 "payload": {"winnerOutcome": "DOWN"}},
            ])
            events.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
            connection = connect_cache(root / "cache.sqlite3")
            try:
                refresh_event_cache(connection, root / "events")
                cases = [
                    CachedStrategyCase(
                        case_id="without_momentum", trigger="buy_trade", outcome="DOWN",
                        price_min=Decimal("0.80"), price_max=Decimal("0.85"),
                        max_remaining_seconds=90, min_remaining_seconds=30,
                        quantity=Decimal("10"), min_trade_size=Decimal("10"),
                    ),
                    CachedStrategyCase(
                        case_id="with_momentum", trigger="buy_trade", outcome="DOWN",
                        price_min=Decimal("0.80"), price_max=Decimal("0.85"),
                        max_remaining_seconds=90, min_remaining_seconds=30,
                        quantity=Decimal("10"), min_trade_size=Decimal("10"),
                        entry_momentum_min=Decimal("0.01"),
                    ),
                ]
                summaries, _, _ = evaluate_cases_incrementally(connection, cases)
            finally:
                connection.close()
            self.assertEqual(summaries[0]["fillCount"], 1)
            self.assertEqual(summaries[1]["fillCount"], 1)

            # A flat five-second move must fail the momentum-qualified case.
            rows[1]["payload"]["outcomes"][0]["bestAsk"] = "0.82"
            rows[1]["payload"]["outcomes"][0]["asks"] = [["0.82", "10"]]
            events.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
            connection = connect_cache(root / "flat-cache.sqlite3")
            try:
                refresh_event_cache(connection, root / "events")
                summaries, _, _ = evaluate_cases_incrementally(connection, cases)
            finally:
                connection.close()
            self.assertEqual(summaries[1]["fillCount"], 0)


if __name__ == "__main__":
    unittest.main()
