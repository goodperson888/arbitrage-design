from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from arb_research.live.strategy_monitor import (
    LiveMarket,
    LiveMonitorConfig,
    PartitionedNDJSONStore,
    ResearchPaperMonitor,
    parse_gamma_market,
    winner_from_gamma,
    settlement_retry_delay_seconds,
)


class LiveStrategyMonitorTest(unittest.TestCase):
    def test_settlement_retry_delay_is_exponential_and_capped(self) -> None:
        self.assertEqual(settlement_retry_delay_seconds(5.0, 0), 5.0)
        self.assertEqual(settlement_retry_delay_seconds(5.0, 1), 10.0)
        self.assertEqual(settlement_retry_delay_seconds(5.0, 4), 80.0)
        self.assertEqual(settlement_retry_delay_seconds(5.0, 10), 3600.0)

    def test_paper_strategy_config_is_up_down_momentum_candidate(self) -> None:
        config_path = Path(__file__).parents[1] / "configs/research/live_paper_monitor_v1.json"
        config = json.loads(config_path.read_text())
        self.assertEqual(config["outcomes"], ["UP", "DOWN"])
        self.assertIn("up_down", config["monitor_id"])
        self.assertEqual(config["max_remaining_seconds"], 90)
        self.assertEqual(config["min_remaining_seconds"], 30)
        self.assertEqual(config["price_min"], "0.80")
        self.assertEqual(config["price_max"], "0.85")
        self.assertEqual(config["min_entry_momentum"], "0.01")

    def test_signal_requires_five_second_same_direction_momentum_when_configured(self) -> None:
        source_us = 1_700_000_000_000_000
        market = LiveMarket(
            slug="btc-updown-5m-1700000000",
            condition_id="0xmomentum",
            start_us=source_us - 240_000_000,
            end_us=source_us + 60_000_000,
            token_by_outcome={"UP": "token-up", "DOWN": "token-down"},
            title="BTC Up or Down",
        )
        with tempfile.TemporaryDirectory() as temporary:
            monitor = ResearchPaperMonitor(
                LiveMonitorConfig(
                    data_dir=Path(temporary),
                    outcomes=("DOWN",),
                    min_entry_momentum=Decimal("0.01"),
                )
            )
            monitor.books["token-down"] = {"ASK:0.82": Decimal("20")}
            monitor.book_updated_us["token-down"] = source_us
            monitor.price_history["token-down"] = [
                (source_us - 5_000_000, Decimal("0.82")),
                (source_us, Decimal("0.82")),
            ]
            trade = {
                "eventId": "trade-flat",
                "outcome": "DOWN",
                "sourceTimeUs": str(source_us),
                "payload": {"outcome": "DOWN", "price": "0.82", "size": "12", "side": "BUY"},
            }
            monitor._evaluate_signal(market, trade, source_us + 100_000)
            self.assertEqual(monitor.recent_signals[-1]["payload"]["status"], "REJECTED")
            self.assertIn("sameDirectionMomentum", monitor.recent_signals[-1]["payload"]["reasons"])

            monitor.price_history["token-down"] = [
                (source_us - 5_000_000, Decimal("0.80")),
                (source_us, Decimal("0.82")),
            ]
            monitor._evaluate_signal(market, {**trade, "eventId": "trade-rising"}, source_us + 200_000)
            self.assertEqual(monitor.recent_signals[-1]["payload"]["status"], "PAPER_FILLED")
            monitor.close()

    def test_partition_store_closes_previous_hour_handle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = PartitionedNDJSONStore(Path(temporary))
            first_us = 1_700_000_000_000_000
            store.append("events", {"value": 1}, first_us)
            old_handle = next(iter(store._handles.values()))
            store.append("events", {"value": 2}, first_us + 3_600_000_000)
            self.assertTrue(old_handle.closed)
            self.assertEqual(len(store._handles), 1)
            store.close()

    def test_raw_sampling_keeps_trades_and_throttles_noisy_book_updates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            monitor = ResearchPaperMonitor(
                LiveMonitorConfig(
                    data_dir=Path(temporary),
                    raw_price_change_interval_ms=100,
                    raw_book_interval_ms=1_000,
                )
            )
            base_us = 1_700_000_000_000_000

            self.assertTrue(
                monitor._should_record_raw({"event_type": "price_change"}, base_us)
            )
            self.assertFalse(
                monitor._should_record_raw({"event_type": "price_change"}, base_us + 50_000)
            )
            self.assertTrue(
                monitor._should_record_raw({"event_type": "price_change"}, base_us + 100_000)
            )
            self.assertTrue(monitor._should_record_raw({"event_type": "book"}, base_us))
            self.assertFalse(
                monitor._should_record_raw({"event_type": "book"}, base_us + 999_999)
            )
            self.assertTrue(
                monitor._should_record_raw({"event_type": "last_trade_price"}, base_us + 1)
            )
            monitor.close()

    def test_parses_gamma_market_and_winner(self) -> None:
        raw = {
            "slug": "btc-updown-5m-1700000100",
            "conditionId": "0xcondition",
            "endDate": "2023-11-14T22:20:00Z",
            "question": "Bitcoin Up or Down",
            "outcomes": json.dumps(["Up", "Down"]),
            "clobTokenIds": json.dumps(["token-up", "token-down"]),
            "outcomePrices": json.dumps(["0", "1"]),
            "closed": True,
        }
        market = parse_gamma_market(raw)
        self.assertEqual(market.token_by_outcome, {"UP": "token-up", "DOWN": "token-down"})
        self.assertEqual(winner_from_gamma(raw), "DOWN")

    def test_qualified_trade_consumes_book_and_settles_paper_position(self) -> None:
        base_ms = 1_700_000_000_000
        source_us = base_ms * 1_000
        market = LiveMarket(
            slug="btc-updown-5m-1700000000",
            condition_id="0xcondition",
            start_us=source_us - 240_000_000,
            end_us=source_us + 60_000_000,
            token_by_outcome={"UP": "token-up", "DOWN": "token-down"},
            title="BTC Up or Down",
        )
        with tempfile.TemporaryDirectory() as temporary:
            monitor = ResearchPaperMonitor(
                LiveMonitorConfig(data_dir=Path(temporary), paper_quantity=Decimal("10"))
            )
            tokens = {"token-up": "UP", "token-down": "DOWN"}
            monitor._process_ws_event(
                market,
                tokens,
                {
                    "event_type": "book",
                    "asset_id": "token-up",
                    "timestamp": str(base_ms),
                    "bids": [{"price": "0.17", "size": "100"}],
                    "asks": [{"price": "0.18", "size": "100"}],
                },
                source_us,
            )
            monitor._process_ws_event(
                market,
                tokens,
                {
                    "event_type": "book",
                    "asset_id": "token-down",
                    "timestamp": str(base_ms),
                    "bids": [{"price": "0.81", "size": "100"}],
                    "asks": [
                        {"price": "0.82", "size": "6"},
                        {"price": "0.84", "size": "6"},
                    ],
                },
                source_us,
            )
            monitor._process_ws_event(
                market,
                tokens,
                {
                    "event_type": "last_trade_price",
                    "asset_id": "token-down",
                    "price": "0.82",
                    "size": "12",
                    "side": "BUY",
                    "timestamp": str(base_ms),
                    "transaction_hash": "0xtrade",
                },
                source_us + 100_000,
            )

            self.assertIn(
                monitor.ledger.position_key(market.market_id, "DOWN"),
                monitor.ledger.positions,
            )
            fill = monitor.ledger.positions[monitor.ledger.position_key(market.market_id, "DOWN")]
            self.assertEqual(fill["payload"]["entryVwap"], "0.828")
            self.assertEqual(monitor.recent_signals[-1]["payload"]["status"], "PAPER_FILLED")

            monitor._record_settlement(market, "DOWN", source_us + 61_000_000)
            stats = monitor.ledger.stats()
            self.assertEqual(stats["closedTrades"], 1)
            self.assertEqual(stats["wins"], 1)
            self.assertGreater(Decimal(stats["totalPnl"]), Decimal("0"))
            monitor.close()

    def test_qualified_signal_records_no_fill_when_depth_is_insufficient(self) -> None:
        base_ms = 1_700_000_100_000
        source_us = base_ms * 1_000
        market = LiveMarket(
            slug="btc-updown-5m-1700000100",
            condition_id="0xcondition2",
            start_us=source_us - 240_000_000,
            end_us=source_us + 60_000_000,
            token_by_outcome={"UP": "token-up", "DOWN": "token-down"},
            title="BTC Up or Down",
        )
        with tempfile.TemporaryDirectory() as temporary:
            monitor = ResearchPaperMonitor(LiveMonitorConfig(data_dir=Path(temporary)))
            monitor.books["token-down"] = {"ASK:0.84": Decimal("2")}
            monitor.book_updated_us["token-down"] = source_us
            trade = {
                "eventId": "trade-1",
                "outcome": "DOWN",
                "sourceTimeUs": str(source_us),
                "payload": {"price": "0.82", "size": "20", "side": "BUY"},
            }
            monitor._evaluate_signal(market, trade, source_us + 100_000)
            signal = monitor.recent_signals[-1]
            self.assertEqual(signal["payload"]["status"], "REJECTED")
            self.assertIn("fullDepthAtPriceCap", signal["payload"]["reasons"])
            self.assertEqual(monitor.counters["paperNoFills"], 0)
            monitor.close()

    def test_only_one_direction_can_fill_per_market(self) -> None:
        base_ms = 1_700_000_200_000
        source_us = base_ms * 1_000
        market = LiveMarket(
            slug="btc-updown-5m-1700000200",
            condition_id="0xcondition3",
            start_us=source_us - 240_000_000,
            end_us=source_us + 60_000_000,
            token_by_outcome={"UP": "token-up", "DOWN": "token-down"},
            title="BTC Up or Down",
        )
        with tempfile.TemporaryDirectory() as temporary:
            monitor = ResearchPaperMonitor(
                LiveMonitorConfig(
                    data_dir=Path(temporary), outcomes=("UP", "DOWN"), enable_early_exit=True
                )
            )
            monitor.books = {
                "token-up": {"ASK:0.82": Decimal("20")},
                "token-down": {"ASK:0.83": Decimal("20")},
            }
            monitor.book_updated_us = {"token-up": source_us, "token-down": source_us}
            for outcome, price_value in (("UP", "0.82"), ("DOWN", "0.83")):
                monitor._evaluate_signal(
                    market,
                    {
                        "eventId": f"trade-{outcome.lower()}",
                        "outcome": outcome,
                        "sourceTimeUs": str(source_us),
                        "payload": {
                            "outcome": outcome,
                            "price": price_value,
                            "size": "12",
                            "side": "BUY",
                        },
                    },
                    source_us + 100_000,
                )

            self.assertEqual(len(monitor.ledger.positions), 1)
            self.assertEqual(monitor.recent_signals[-1]["payload"]["status"], "ALREADY_FILLED")
            monitor._record_settlement(market, "UP", source_us + 61_000_000)
            self.assertEqual(monitor.ledger.stats("UP")["wins"], 1)
            self.assertEqual(monitor.ledger.stats("DOWN")["losses"], 0)
            self.assertEqual(monitor.counters["paperFillsByOutcome"], {"UP": 1, "DOWN": 0})
            monitor.close()

    def test_position_exits_early_only_when_ten_shares_can_sell_at_099(self) -> None:
        source_us = 1_700_000_250_000_000
        market = LiveMarket(
            slug="btc-updown-5m-1700000250",
            condition_id="0xcondition-exit",
            start_us=source_us - 240_000_000,
            end_us=source_us + 60_000_000,
            token_by_outcome={"UP": "token-up", "DOWN": "token-down"},
            title="BTC Up or Down",
        )
        with tempfile.TemporaryDirectory() as temporary:
            monitor = ResearchPaperMonitor(
                LiveMonitorConfig(
                    data_dir=Path(temporary), outcomes=("UP", "DOWN"), enable_early_exit=True
                )
            )
            monitor.books["token-up"] = {"ASK:0.83": Decimal("20")}
            monitor.book_updated_us["token-up"] = source_us
            monitor._evaluate_signal(
                market,
                {
                    "eventId": "trade-up-exit",
                    "outcome": "UP",
                    "sourceTimeUs": str(source_us),
                    "payload": {"outcome": "UP", "price": "0.83", "size": "12", "side": "BUY"},
                },
                source_us + 100_000,
            )
            self.assertEqual(len(monitor.ledger.positions), 1)
            monitor.books["token-up"] = {"BID:0.99": Decimal("9")}
            monitor._try_paper_exits(market, source_us + 200_000)
            self.assertEqual(len(monitor.ledger.positions), 1)
            monitor.books["token-up"] = {"BID:0.99": Decimal("10")}
            monitor._try_paper_exits(market, source_us + 300_000)
            self.assertEqual(len(monitor.ledger.positions), 0)
            self.assertEqual(monitor.ledger.closed[-1]["eventType"], "PAPER_EXIT")
            self.assertGreater(Decimal(monitor.ledger.closed[-1]["payload"]["netPnl"]), 0)
            monitor.close()

    def test_restart_restores_open_market_for_later_settlement(self) -> None:
        source_us = 1_700_000_300_000_000
        market = LiveMarket(
            slug="btc-updown-5m-1700000300",
            condition_id="0xcondition4",
            start_us=source_us - 240_000_000,
            end_us=source_us + 60_000_000,
            token_by_outcome={"UP": "token-up", "DOWN": "token-down"},
            title="BTC Up or Down",
        )
        with tempfile.TemporaryDirectory() as temporary:
            config = LiveMonitorConfig(data_dir=Path(temporary))
            monitor = ResearchPaperMonitor(config)
            monitor.books["token-down"] = {"ASK:0.82": Decimal("20")}
            monitor.book_updated_us["token-down"] = source_us
            monitor._evaluate_signal(
                market,
                {
                    "eventId": "trade-down-restart",
                    "outcome": "DOWN",
                    "sourceTimeUs": str(source_us),
                    "payload": {"outcome": "DOWN", "price": "0.82", "size": "12", "side": "BUY"},
                },
                source_us + 100_000,
            )
            monitor.close()

            restarted = ResearchPaperMonitor(config)
            self.assertIn(market.market_id, restarted.known_markets)
            restarted._record_settlement(market, "UP", source_us + 61_000_000)
            self.assertEqual(restarted.ledger.stats("DOWN")["losses"], 1)
            restarted.close()


if __name__ == "__main__":
    unittest.main()
