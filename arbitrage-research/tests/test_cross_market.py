from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
import gzip

from arb_research.live.cross_market_validation import (
    CrossMarketValidationConfig,
    CrossMarketValidationService,
)
from arb_research.live.strategy_monitor import LiveMarket

from arb_research.cross_market.collectors import (
    BinanceCollector,
    BinanceFuturesCollector,
    DeribitCollector,
    HyperliquidCollector,
    PolymarketReferenceCollector,
)
from arb_research.cross_market.history import (
    Candle,
    MarketLabel,
    build_historical_rows,
    evaluate_historical_rows,
    load_market_labels,
)
from arb_research.cross_market.paper_validation import (
    build_paper_cross_rows,
    evaluate_paper_cross_rows,
)
from arb_research.cross_market.state import CrossMarketState
from arb_research.cross_market.validation import (
    evaluate_deribit_option_signals,
    evaluate_dvol_regimes,
)


class CollectorParserTest(unittest.TestCase):
    def test_binance_futures_uses_separate_venue_and_spot_parser(self) -> None:
        update = BinanceFuturesCollector.parse_message({"data": {
            "e": "aggTrade", "E": 1_700_000_001_000, "T": 1_700_000_000_900,
            "p": "70000", "q": "0.1", "m": False, "a": 7,
        }}, 1_700_000_001_100_000)
        self.assertEqual(BinanceFuturesCollector.venue, "BINANCE_FUTURES")
        self.assertEqual(update["kind"], "trade")
        self.assertEqual(update["price"], "70000")

    def test_binance_keeps_incomplete_kline(self) -> None:
        update = BinanceCollector.parse_message({"data": {
            "e": "kline", "E": 1_700_000_001_000,
            "k": {"t": 1_700_000_000_000, "T": 1_700_000_059_999, "i": "1m",
                  "o": "100", "h": "102", "l": "99", "c": "101", "v": "5",
                  "q": "500", "V": "3", "n": 10, "x": False},
        }}, 1_700_000_001_100_000)
        self.assertIsNotNone(update)
        self.assertFalse(update["closed"])
        self.assertEqual(update["close"], "101")

    def test_hyperliquid_trade_and_bbo(self) -> None:
        trades = HyperliquidCollector.parse_message({
            "channel": "trades", "data": [
                {"coin": "BTC", "px": "100", "sz": "2", "side": "B", "time": 1_700_000_000_000}
            ]
        }, 1_700_000_000_100_000)
        self.assertEqual(trades[0]["kind"], "trade")
        book = HyperliquidCollector.parse_message({
            "channel": "bbo", "data": {"coin": "BTC", "time": 1_700_000_000_000,
                                           "bbo": [{"px": "99", "sz": "3"},
                                                   {"px": "101", "sz": "1"}]}
        }, 1_700_000_000_100_000)
        self.assertEqual(book[0]["askSize"], "1")

    def test_deribit_selects_nearest_expiry_and_strikes(self) -> None:
        now_ms = 1_700_000_000_000
        instruments = []
        for expiry_delta, expiry_name in ((24, "A"), (48, "B")):
            for strike in (90, 100, 110):
                for option_type, suffix in (("call", "C"), ("put", "P")):
                    instruments.append({
                        "instrument_name": f"BTC-{expiry_name}-{strike}-{suffix}",
                        "expiration_timestamp": now_ms + expiry_delta * 3_600_000,
                        "strike": strike, "option_type": option_type,
                    })
        selected = DeribitCollector.select_option_instruments(instruments, 101, now_ms, 2)
        self.assertEqual(len(selected), 4)
        self.assertTrue(all(item["expiration_timestamp"] == now_ms + 24 * 3_600_000 for item in selected))

    def test_deribit_option_trade_parser(self) -> None:
        update = DeribitCollector.parse_subscription({
            "params": {
                "channel": "trades.option.BTC.100ms",
                "data": [{
                    "trade_id": "t1", "trade_seq": 2, "timestamp": 1_700_000_000_000,
                    "instrument_name": "BTC-X-100-C", "direction": "buy",
                    "amount": 2, "price": 0.01, "iv": 50, "index_price": 100,
                    "mark_price": 0.009,
                }],
            }
        }, 1_700_000_000_100_000)
        self.assertEqual(update["kind"], "option_trades")
        self.assertEqual(update["trades"][0]["direction"], "buy")
        self.assertEqual(update["trades"][0]["timestampUs"], "1700000000000000")

        dvol = DeribitCollector.parse_subscription({
            "params": {"channel": "deribit_volatility_index.btc_usd",
                       "data": {"timestamp": 1_700_000_000_000, "volatility": 48.7}}
        }, 1_700_000_000_100_000)
        self.assertEqual(dvol["value"], 48.7)

    def test_polymarket_chainlink_reference_parser(self) -> None:
        update = PolymarketReferenceCollector.parse_message({
            "topic": "crypto_prices_chainlink", "type": "update", "timestamp": 1001,
            "payload": {"symbol": "btc/usd", "timestamp": 1000, "value": 70000.5},
        }, 1_002_000)
        self.assertEqual(update["source"], "CHAINLINK")
        self.assertEqual(update["sourceTimeUs"], "1000000")
        self.assertEqual(update["price"], 70000.5)

    def test_polymarket_binance_reference_parser(self) -> None:
        update = PolymarketReferenceCollector.parse_message({
            "topic": "crypto_prices", "type": "update", "timestamp": 1001,
            "payload": {"symbol": "BTCUSDT", "timestamp": 1000, "value": 70001.5},
        }, 1_002_000)
        self.assertEqual(update["source"], "POLYMARKET_BINANCE")
        self.assertEqual(update["symbol"], "BTCUSDT")
        self.assertEqual(update["price"], 70001.5)

    def test_polymarket_reference_historical_snapshot_uses_latest_point(self) -> None:
        update = PolymarketReferenceCollector.parse_message({
            "topic": "crypto_prices", "type": "update", "timestamp": 1002,
            "payload": {
                "symbol": "BTCUSDT",
                "data": [
                    {"timestamp": 1000, "value": 70000.0},
                    {"timestamp": 1001, "value": 70002.0},
                ],
            },
        }, 1_003_000)
        self.assertEqual(update["source"], "POLYMARKET_BINANCE")
        self.assertEqual(update["sourceTimeUs"], "1001000")
        self.assertEqual(update["price"], 70002.0)

    def test_polymarket_chainlink_snapshot_is_classified_by_symbol(self) -> None:
        update = PolymarketReferenceCollector.parse_message({
            "topic": "crypto_prices", "type": "subscribe", "timestamp": 1002,
            "payload": {
                "symbol": "btc/usd",
                "data": [{"timestamp": 1001, "value": 70003.0}],
            },
        }, 1_003_000)
        self.assertEqual(update["source"], "CHAINLINK")
        self.assertEqual(update["symbol"], "btc/usd")


class CompactPolymarketCaptureTest(unittest.TestCase):
    def test_records_one_second_depth_vwap_worst_price_and_trade(self) -> None:
        base_us = 1_700_000_000_000_000
        base_ms = base_us // 1_000
        market = LiveMarket(
            slug="btc-updown-15m-1700000000",
            condition_id="0x15m",
            start_us=base_us,
            end_us=base_us + 900_000_000,
            token_by_outcome={"UP": "up-token", "DOWN": "down-token"},
            title="BTC 15m Up or Down",
        )
        with tempfile.TemporaryDirectory() as temporary:
            service = CrossMarketValidationService(CrossMarketValidationConfig(
                data_dir=Path(temporary),
                market_duration_minutes=15,
                polymarket_book_enabled=True,
                polymarket_snapshot_interval_ms=1_000,
                polymarket_quantity=10,
                polymarket_depth_caps=(0.60, 0.65, 0.85),
            ))
            service.active_market = market
            service.polymarket_ws_market_id = market.market_id
            tokens = {"up-token": "UP", "down-token": "DOWN"}
            service._process_polymarket_ws_event(
                market, tokens,
                {
                    "event_type": "book", "asset_id": "up-token",
                    "timestamp": str(base_ms),
                    "bids": [
                        {"price": "0.59", "size": "6"},
                        {"price": "0.58", "size": "8"},
                    ],
                    "asks": [
                        {"price": "0.60", "size": "4"},
                        {"price": "0.62", "size": "6"},
                        {"price": "0.64", "size": "4"},
                    ],
                }, base_us,
            )
            service._process_polymarket_ws_event(
                market, tokens,
                {
                    "event_type": "book", "asset_id": "down-token",
                    "timestamp": str(base_ms),
                    "bids": [{"price": "0.37", "size": "20"}],
                    "asks": [{"price": "0.38", "size": "20"}],
                }, base_us,
            )
            service._record_polymarket_snapshot(base_us + 1_000_000)
            service._process_polymarket_ws_event(
                market, tokens,
                {
                    "event_type": "last_trade_price", "asset_id": "up-token",
                    "timestamp": str(base_ms + 1), "side": "BUY",
                    "price": "0.62", "size": "12",
                    "transaction_hash": "0xtrade15m",
                }, base_us + 1_100_000,
            )
            service.close()

            snapshot_path = next(
                (Path(temporary) / "polymarket_snapshots").glob(
                    "date=*/hour=*/part-*.ndjson.gz"
                )
            )
            with gzip.open(snapshot_path, "rt", encoding="utf-8") as handle:
                snapshot = json.loads(next(handle))
            up = snapshot["outcomes"]["UP"]
            self.assertTrue(snapshot["completeOutcomeBooks"])
            self.assertEqual(up["bestAsk"], "0.60")
            self.assertEqual(up["buy"], {"vwap": "0.612", "worstPrice": "0.62"})
            self.assertEqual(up["sell"], {"vwap": "0.586", "worstPrice": "0.58"})
            self.assertEqual(up["depthCaps"]["0.60"]["askDepth"], "4")
            self.assertIsNone(up["depthCaps"]["0.60"]["buy"])
            self.assertEqual(
                up["depthCaps"]["0.65"]["buy"],
                {"vwap": "0.612", "worstPrice": "0.62"},
            )

            trade_path = next(
                (Path(temporary) / "polymarket_trades").glob(
                    "date=*/hour=*/part-*.ndjson.gz"
                )
            )
            with gzip.open(trade_path, "rt", encoding="utf-8") as handle:
                trade = json.loads(next(handle))
            self.assertEqual(trade["outcome"], "UP")
            self.assertEqual(trade["payload"]["side"], "BUY")
            self.assertEqual(trade["payload"]["size"], "12")


class CrossMarketStateTest(unittest.TestCase):
    def test_binance_futures_is_kept_separate_from_spot(self) -> None:
        state = CrossMarketState()
        timestamp = 1_700_000_000_000_000
        state.update_binance({
            "kind": "trade", "sourceTimeUs": str(timestamp), "price": "100",
            "quantity": "1", "isBuyerMaker": False,
        })
        state.update_binance_futures({
            "kind": "trade", "sourceTimeUs": str(timestamp), "price": "101",
            "quantity": "2", "isBuyerMaker": False,
        })
        self.assertEqual(state.message_counts["BINANCE"], 1)
        self.assertEqual(state.message_counts["BINANCE_FUTURES"], 1)
        self.assertEqual(state.binance.prices[-1].price, 100)
        self.assertEqual(state.binance_futures.prices[-1].price, 101)

    def test_snapshot_uses_only_prices_at_or_before_observation(self) -> None:
        state = CrossMarketState(memory_window_seconds=600)
        start_us = 1_700_000_000_000_000
        for offset, price in ((0, "100"), (30, "101"), (60, "102"), (90, "500")):
            state.update_binance({
                "kind": "trade", "sourceTimeUs": str(start_us + offset * 1_000_000),
                "price": price, "quantity": "1", "isBuyerMaker": False,
            })
        snapshot = state.snapshot(
            market_id="m", start_us=start_us, end_us=start_us + 300_000_000,
            as_of_us=start_us + 60_000_000,
        )
        self.assertEqual(snapshot["features"]["binance"]["price"], 102)
        self.assertEqual(snapshot["signals"]["binance"], "UP")

    def test_history_is_bounded(self) -> None:
        state = CrossMarketState(memory_window_seconds=60)
        base = 1_700_000_000_000_000
        for offset in range(180):
            state.update_binance({
                "kind": "trade", "sourceTimeUs": str(base + offset * 1_000_000),
                "price": str(100 + offset), "quantity": "1", "isBuyerMaker": False,
            })
        self.assertLessEqual(len(state.binance.trades), 61)
        self.assertLessEqual(len(state.binance.prices), 62)

    def test_hyperliquid_primary_direction_uses_mark_not_trade(self) -> None:
        state = CrossMarketState()
        start = 1_700_000_000_000_000
        for offset, mark, trade in ((0, "100", "100"), (60, "101", "99")):
            timestamp = start + offset * 1_000_000
            state.update_hyperliquid({
                "kind": "context", "sourceTimeUs": str(timestamp),
                "markPrice": mark, "oraclePrice": mark, "midPrice": trade,
            })
            state.update_hyperliquid({
                "kind": "trade", "sourceTimeUs": str(timestamp),
                "price": trade, "quantity": "1", "side": "A",
            })
        snapshot = state.snapshot(
            market_id="m", start_us=start, end_us=start + 300_000_000,
            as_of_us=start + 60_000_000,
        )
        self.assertGreater(snapshot["features"]["hyperliquid"]["returnFromStart"], 0)

    def test_deribit_rr25_oi_dvol_and_option_flow(self) -> None:
        state = CrossMarketState()
        start = 1_700_000_000_000_000
        expiry_ms = (start + 24 * 3_600_000_000) // 1_000
        instruments = [
            {"instrument_name": "BTC-X-100-C", "expiration_timestamp": expiry_ms,
             "strike": 100, "option_type": "call"},
            {"instrument_name": "BTC-X-110-C", "expiration_timestamp": expiry_ms,
             "strike": 110, "option_type": "call"},
            {"instrument_name": "BTC-X-100-P", "expiration_timestamp": expiry_ms,
             "strike": 100, "option_type": "put"},
            {"instrument_name": "BTC-X-90-P", "expiration_timestamp": expiry_ms,
             "strike": 90, "option_type": "put"},
        ]
        state.set_deribit_instruments(instruments)
        for offset, values in (
            (0, {
                "BTC-X-100-C": (50, 0.5, 20), "BTC-X-110-C": (58, 0.25, 10),
                "BTC-X-100-P": (51, -0.5, 20), "BTC-X-90-P": (56, -0.25, 12),
            }),
            (61, {
                "BTC-X-100-C": (52, 0.5, 22), "BTC-X-110-C": (60, 0.25, 13),
                "BTC-X-100-P": (52, -0.5, 21), "BTC-X-90-P": (55, -0.25, 14),
            }),
        ):
            timestamp = start + offset * 1_000_000
            state.update_deribit({"kind": "index", "sourceTimeUs": str(timestamp), "price": 100})
            state.update_deribit({"kind": "volatility_index", "sourceTimeUs": str(timestamp),
                                  "value": 60 + offset / 61})
            for name, (iv, delta, oi) in values.items():
                state.update_deribit({
                    "kind": "option", "sourceTimeUs": str(timestamp), "instrument": name,
                    "markIv": iv, "delta": delta, "openInterest": oi, "markPrice": 0.1,
                })
        state.update_deribit({
            "kind": "option_trades", "sourceTimeUs": str(start + 61_000_000),
            "trades": [
                {"timestampUs": str(start + 61_000_000), "instrument": "BTC-X-110-C",
                 "direction": "buy", "amount": 2, "price": 0.1, "indexPrice": 100, "iv": 60},
                {"timestampUs": str(start + 61_000_000), "instrument": "BTC-X-90-P",
                 "direction": "buy", "amount": 1, "price": 0.1, "indexPrice": 100, "iv": 55},
            ],
        })
        snapshot = state.snapshot(
            market_id="m", start_us=start, end_us=start + 300_000_000,
            as_of_us=start + 61_000_000,
        )
        features = snapshot["features"]["deribit"]
        self.assertEqual(features["call25Instrument"], "BTC-X-110-C")
        self.assertEqual(features["put25Instrument"], "BTC-X-90-P")
        self.assertEqual(features["rr25"], 5)
        self.assertEqual(features["rr25Change60s"], 3)
        self.assertEqual(features["call25OiChange60s"], 3)
        self.assertAlmostEqual(features["optionFlowImbalance60s"], 1 / 3)
        self.assertEqual(features["dvolChange60s"], 1)

    def test_deribit_option_history_is_time_and_count_bounded(self) -> None:
        state = CrossMarketState(memory_window_seconds=60)
        base = 1_700_000_000_000_000
        expiry_ms = (base + 24 * 3_600_000_000) // 1_000
        state.set_deribit_instruments([{
            "instrument_name": "BTC-X-100-C", "expiration_timestamp": expiry_ms,
            "strike": 100, "option_type": "call",
        }])
        for offset in range(180):
            state.update_deribit({
                "kind": "option", "sourceTimeUs": str(base + offset * 1_000_000),
                "instrument": "BTC-X-100-C", "markIv": 50, "delta": 0.25,
                "openInterest": 10 + offset,
            })
        self.assertLessEqual(len(state.deribit_option_history["BTC-X-100-C"]), 61)


class HistoricalValidationTest(unittest.TestCase):
    def test_loads_only_markets_with_official_label(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.ndjson"
            path.write_text("\n".join(json.dumps(item) for item in [
                {"eventType": "MARKET_DEFINITION", "marketId": "m1",
                 "payload": {"eventStartUs": "1000000", "eventEndUs": "301000000"}},
                {"eventType": "SETTLEMENT", "marketId": "m1",
                 "payload": {"winnerOutcome": "UP"}},
                {"eventType": "MARKET_DEFINITION", "marketId": "m2",
                 "payload": {"eventStartUs": "401000000", "eventEndUs": "701000000"}},
            ]) + "\n")
            labels = load_market_labels(path)
            self.assertEqual([item.market_id for item in labels], ["m1"])

    def test_build_and_evaluate_has_no_future_candle(self) -> None:
        market = MarketLabel("m1", 0, 300_000, "UP")
        candles = [
            Candle(0, 59_999, 100, 102, 99, 101),
            Candle(60_000, 119_999, 101, 104, 100, 103),
            Candle(120_000, 179_999, 103, 105, 90, 91),
        ]
        rows = build_historical_rows(
            [market], {"binance": candles, "hyperliquid": candles, "deribit": candles},
            [], [180],
        )
        # Observation is at 120s; the candle closing at 179.999s must not be used.
        self.assertEqual(rows[0]["binancePrice"], 103)
        self.assertEqual(rows[0]["binanceSignal"], "UP")
        results = evaluate_historical_rows(rows)
        self.assertTrue(all(item["accuracy"] == 1 for item in results))

    def test_option_and_dvol_evaluators(self) -> None:
        rows = [{
            "observationTargetSeconds": 60, "winner": "UP",
            "deribit.rr25": 1, "deribit.rr25Change60s": 0.5,
            "deribit.optionFlowImbalance60s": 0.4,
            "deribit.call25OiChange60s": 2, "deribit.call25IvChange60s": 1,
            "deribit.dvolChange60s": 0.2, "signal.consensus": "UP",
        }]
        option = evaluate_deribit_option_signals(rows)
        self.assertTrue(all(item["accuracy"] == 1 for item in option))
        dvol = evaluate_dvol_regimes(rows)
        self.assertEqual(dvol[0]["consensusAccuracy"], 1)


class PaperCrossValidationTest(unittest.TestCase):
    def test_matches_only_snapshot_before_fill(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paper_path = root / "paper" / "paper" / "date=2026-01-01" / "hour=00" / "part-001.ndjson"
            paper_path.parent.mkdir(parents=True)
            events = [
                {"eventType": "PAPER_FILL", "marketId": "m1", "outcome": "UP",
                 "receivedTimeUs": "2000000", "payload": {"quantity": "10", "entryVwap": "0.82"}},
                {"eventType": "PAPER_SETTLEMENT", "marketId": "m1", "outcome": "UP",
                 "receivedTimeUs": "3000000", "payload": {"quantity": "10", "netPnl": "1.5",
                                                                "won": True, "winnerOutcome": "UP"}},
            ]
            paper_path.write_text("\n".join(json.dumps(item) for item in events) + "\n")
            snapshot_path = root / "cross" / "venue_snapshots" / "date=2026-01-01" / "hour=00" / "part-001.ndjson.gz"
            snapshot_path.parent.mkdir(parents=True)
            snapshots = [
                {"marketId": "m1", "receivedTimeUs": "1500000",
                 "signals": {"binance": "UP", "hyperliquid": "DOWN", "deribit": "UP"},
                 "features": {}},
                {"marketId": "m1", "receivedTimeUs": "2100000",
                 "signals": {"binance": "DOWN", "hyperliquid": "DOWN", "deribit": "DOWN"},
                 "features": {}},
            ]
            with gzip.open(snapshot_path, "wt", encoding="utf-8") as handle:
                handle.write("\n".join(json.dumps(item) for item in snapshots) + "\n")
            rows, counters = build_paper_cross_rows(root / "paper", root / "cross")
            self.assertEqual(counters["matched"], 1)
            self.assertEqual(rows[0]["snapshotReceivedTimeUs"], "1500000")
            self.assertEqual(rows[0]["agreementCount"], 2)
            cohorts = evaluate_paper_cross_rows(rows)
            self.assertEqual(cohorts[4]["winRate"], 1)


if __name__ == "__main__":
    unittest.main()
