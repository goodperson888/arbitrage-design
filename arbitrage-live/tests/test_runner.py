from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from live_trader.clob_transport import ClobTransportConfig, PyClobClientTransport
from live_trader.models import StrategyConfig
from live_trader.order_state import OrderRecord, OrderState, SQLiteOrderStore
from live_trader.monitoring import MonitoringRecorder, iter_partitioned_events
from live_trader.runner import LiveRunner, RunnerConfig
from live_trader.settings import SettingsStore


def strategy_config() -> StrategyConfig:
    return StrategyConfig(
        side="DOWN",
        min_remaining_seconds=Decimal("30"),
        max_remaining_seconds=Decimal("90"),
        max_budget=Decimal("5"),
        min_valid_cost=Decimal("2.5"),
        max_entry_price=Decimal("0.85"),
        reversal_window_seconds=Decimal("5"),
        reversal_tolerance=Decimal("0.01"),
        binance_lookback_seconds=Decimal("300"),
        fee_rate=Decimal("0.07"),
        quantity_step=Decimal("0.01"),
        active_trade_min_quantity=Decimal("10"),
        market_data_max_age_seconds=Decimal("2"),
    )


class MockTransport:
    def __init__(self, timeout: bool = False) -> None:
        self.timeout = timeout
        self.submits = 0
        self.last_payload = None

    def submit(self, payload):
        self.submits += 1
        self.last_payload = payload
        if self.timeout:
            raise TimeoutError("mock timeout")
        return {"status": "matched", "order_id": "remote-1"}

    def fetch(self, order_id):
        return {"status": "matched", "order_id": order_id}

    def cancel(self, order_id):
        return {"status": "canceled", "order_id": order_id}

    def fills(self, order_id):
        return [{"trade_id": "fill-1", "order_id": order_id, "price": "0.82", "size": "5", "fee": "0.01"}]

    def market_fee_details(self, condition_id):
        return {"rate": "0.07", "exponent": "1", "conditionId": condition_id}


class PartialFillTransport(MockTransport):
    def __init__(self, size: str) -> None:
        super().__init__()
        self.size = size

    def fills(self, order_id):
        return [
            {"trade_id": "fill-1", "order_id": order_id, "price": "0.82", "size": self.size, "fee": "0.01"},
            # Provider retries must not double count the same trade.
            {"trade_id": "fill-1", "order_id": order_id, "price": "0.82", "size": self.size, "fee": "0.01"},
        ]


class LowPriceFillTransport(MockTransport):
    def fills(self, order_id):
        return [{"trade_id": "fill-low", "order_id": order_id, "price": "0.79", "size": "5", "fee": "0.01"}]


def market_message():
    return {
        "type": "market_definition",
        "id": "btc-5m-1",
        "conditionId": "condition-1",
        "question": "BTC Up or Down - 5m",
        "active": True,
        "closed": False,
        "startDate": "2026-08-24T10:00:00Z",
        "endDate": "2026-08-24T10:05:00Z",
        "tokens": [
            {"outcome": "Up", "token_id": "up-1"},
            {"outcome": "Down", "token_id": "down-1"},
        ],
    }


def event_feed():
    yield ("polymarket", market_message())
    for minute in range(5):
        yield (
            "binance",
            {"s": "BTCUSDT", "k": {"t": 1_000 + minute * 60_000, "T": 61_000 + minute * 60_000, "o": "100", "c": "99.5"}},
        )
    yield (
        "book",
        {
            "market_id": "btc-5m-1",
            "remaining_seconds": "60",
            "down_price_now": "0.82",
            "down_price_five_seconds_ago": "0.82",
            "asks": [["0.82", "10"]],
            "received_time_us": int(datetime.now(timezone.utc).timestamp() * 1_000_000),
        },
    )
    yield (
        "polymarket",
        {"type": "trade", "market_id": "btc-5m-1", "token_id": "down-1", "side": "BUY", "price": "0.82", "size": "12", "timestamp": "2026-08-24T10:04:00Z"},
    )


class RunnerTests(unittest.TestCase):
    def test_dashboard_order_pause_is_loaded_by_runner(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            SettingsStore(root).save({"liveMode": "canary", "orderSubmissionEnabled": False})
            runner = LiveRunner(
                RunnerConfig(
                    mode="canary",
                    data_dir=root,
                    strategy=strategy_config(),
                    transport_enabled=True,
                    live_enabled=True,
                    preflight_passed=True,
                    credentials_present=True,
                ),
                transport=MockTransport(),
            )
            self.assertFalse(runner._order_submission_enabled)
            runner.close()

    def test_session_records_strategy_parameter_provenance(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            runner = LiveRunner(RunnerConfig(mode="observe", data_dir=root, strategy=strategy_config()))
            runner.close()
            sessions = list(iter_partitioned_events(root, "sessions"))
            params = sessions[0]["payload"]["strategyParameters"]
            self.assertEqual(params["maxBudgetUsd"], "5")
            self.assertEqual(params["activeTradeMinQuantity"], "10")

    def test_runner_uses_configured_daily_loss_limits(self) -> None:
        with TemporaryDirectory() as temp:
            runner = LiveRunner(
                RunnerConfig(
                    mode="observe",
                    data_dir=Path(temp),
                    strategy=strategy_config(),
                    max_daily_loss_count=3,
                    max_daily_loss_total=Decimal("7.5"),
                )
            )
            self.assertEqual(runner.risk.max_loss_count, 3)
            self.assertEqual(runner.risk.max_loss_total, Decimal("7.5"))
            runner.close()

    def test_observe_records_qualified_signal_without_order_submission(self) -> None:
        with TemporaryDirectory() as temp:
            runner = LiveRunner(RunnerConfig(mode="observe", data_dir=Path(temp), strategy=strategy_config()))
            runner.run_events(event_feed())
            runner.close()
            self.assertEqual(runner.order_attempts, 0)
            self.assertGreaterEqual(runner.accepted_signals, 1)

    def test_dynamic_active_trade_threshold_scales_with_planned_order_quantity(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            runner = LiveRunner(RunnerConfig(mode="observe", data_dir=root, strategy=strategy_config()))
            events = list(event_feed())
            events[-2] = ("book", {**events[-2][1], "down_price_now": "0.40", "down_price_five_seconds_ago": "0.40", "asks": [["0.40", "30"]]})
            runner.run_events(events)
            runner.close()
            signals = list(iter_partitioned_events(root, "signals"))
            dynamic_rejections = [event for event in signals if event["payload"].get("reason") == "active_trade_below_dynamic_threshold"]
            self.assertEqual(len(dynamic_rejections), 1)
            payload = dynamic_rejections[0]["payload"]
            self.assertGreater(Decimal(payload["requiredActiveTradeQuantity"]), Decimal("12"))
            self.assertEqual(payload["effectiveOrderBudgetUsd"], "5")
            self.assertFalse(any(event["eventType"] == "PAPER_FILL" for event in iter_partitioned_events(root, "fills")))

    def test_dynamic_active_trade_threshold_allows_sufficient_market_buy(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            runner = LiveRunner(RunnerConfig(mode="observe", data_dir=root, strategy=strategy_config()))
            events = list(event_feed())
            events[-2] = ("book", {**events[-2][1], "down_price_now": "0.40", "down_price_five_seconds_ago": "0.40", "asks": [["0.40", "30"]]})
            events[-1] = ("polymarket", {**events[-1][1], "size": "20"})
            runner.run_events(events)
            runner.close()
            signals = list(iter_partitioned_events(root, "signals"))
            accepted = [event for event in signals if event["eventType"] == "SIGNAL_ACCEPTED"]
            self.assertEqual(len(accepted), 1)
            self.assertEqual(accepted[0]["payload"]["activeTradeMultiplier"], "1.5")
            self.assertTrue(any(event["eventType"] == "PAPER_FILL" for event in iter_partitioned_events(root, "fills")))

    def test_up_strategy_uses_up_token_book_and_records_up_fill(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            runner = LiveRunner(RunnerConfig(mode="observe", data_dir=root, strategy=replace(strategy_config(), side="UP")))
            events = list(event_feed())
            events[-2] = ("book", {**events[-2][1], "side": "UP", "down_price_now": "0.82", "down_price_five_seconds_ago": "0.82"})
            events[-1] = ("polymarket", {**events[-1][1], "token_id": "up-1", "size": "12"})
            runner.run_events(events)
            runner.close()
            signals = list(iter_partitioned_events(root, "signals"))
            self.assertTrue(any(event["eventType"] == "SIGNAL_ACCEPTED" and event["outcome"] == "UP" for event in signals))
            fills = list(iter_partitioned_events(root, "fills"))
            self.assertTrue(any(event["eventType"] == "PAPER_FILL" and event["outcome"] == "UP" for event in fills))

    def test_observe_records_paper_fill_and_settlement(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            runner = LiveRunner(RunnerConfig(mode="observe", data_dir=root, strategy=strategy_config()))
            runner.run_events(event_feed())
            runner.process_event("polymarket", {"type": "market_resolved", "market_id": "btc-5m-1", "winner": "DOWN"})
            runner.close()
            fills = list(iter_partitioned_events(root, "fills"))
            settlements = list(iter_partitioned_events(root, "settlements"))
            self.assertTrue(any(event["eventType"] == "PAPER_FILL" for event in fills))
            self.assertTrue(any(event["eventType"] == "SETTLEMENT" and event["payload"]["winner"] == "DOWN" for event in settlements))

            restarted = LiveRunner(RunnerConfig(mode="observe", data_dir=root, strategy=strategy_config()))
            self.assertEqual(restarted.pending_settlement_market_ids(), set())
            self.assertNotIn("btc-5m-1", restarted.risk.reserved_markets)
            restarted.close()

    def test_observe_rejects_expected_vwap_below_configured_floor(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            runner = LiveRunner(RunnerConfig(mode="observe", data_dir=root, strategy=replace(strategy_config(), min_entry_vwap=Decimal("0.80"))))
            events = list(event_feed())
            events[-2] = ("book", {**events[-2][1], "asks": [["0.79", "10"]]})
            runner.run_events(events)
            runner.close()
            fills = list(iter_partitioned_events(root, "fills"))
            self.assertFalse(any(event["eventType"] == "PAPER_FILL" for event in fills))

    def test_market_book_records_at_most_one_feature_snapshot_per_second(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            runner = LiveRunner(RunnerConfig(mode="observe", data_dir=root, strategy=strategy_config()))
            runner.process_event("polymarket", market_message())
            now_us = int(datetime.now(timezone.utc).timestamp()) * 1_000_000
            for offset in (0, 200_000, 1_100_000):
                runner.process_event("polymarket", {"event_type": "book", "asset_id": "down-1", "remaining_seconds": "60", "asks": [["0.82", "10"]], "bids": [["0.81", "5"]], "received_time_us": now_us + offset})
            runner.close()
            events = list(iter_partitioned_events(root, "events"))
            snapshots = [event for event in events if event["eventType"] == "MARKET_SNAPSHOT_1S"]
            self.assertEqual(len(snapshots), 2)

    def test_high_frequency_orderbook_events_are_sampled_without_changing_strategy_state(self) -> None:
        with TemporaryDirectory() as temp:
            runner = LiveRunner(RunnerConfig(mode="observe", data_dir=Path(temp), strategy=strategy_config(), orderbook_sample_seconds=5, raw_sample_seconds=5))
            runner.process_event("polymarket", market_message())
            base = (int(datetime.now(timezone.utc).timestamp()) // 5) * 5 * 1_000_000
            for offset in (0, 1_000_000, 2_000_000, 6_000_000):
                runner.process_event("polymarket", {"event_type": "book", "asset_id": "down-1", "remaining_seconds": "60", "asks": [["0.82", "10"]], "bids": [["0.81", "5"]], "received_time_us": base + offset})
            runner.close()
            events = list(iter_partitioned_events(Path(temp), "events"))
            orderbooks = [event for event in events if event["eventType"] == "ORDERBOOK_SNAPSHOT"]
            self.assertLessEqual(len(orderbooks), 2)
            self.assertEqual(runner.book["down_price_now"], "0.82")

    def test_raw_price_changes_are_sampled_but_trades_are_preserved(self) -> None:
        with TemporaryDirectory() as temp:
            runner = LiveRunner(RunnerConfig(mode="observe", data_dir=Path(temp), strategy=strategy_config(), raw_sample_seconds=5))
            base = (int(datetime.now(timezone.utc).timestamp()) // 5) * 5 * 1_000_000
            sampled = [runner._should_record_raw("POLYMARKET", {"event_type": "price_change", "received_time_us": base + offset}) for offset in (0, 1_000_000, 6_000_000)]
            self.assertEqual(sampled, [True, False, True])
            self.assertTrue(runner._should_record_raw("POLYMARKET", {"event_type": "last_trade_price", "received_time_us": base + 1_000_000}))
            runner.close()

    def test_repeated_signal_rejections_are_coalesced_by_reason(self) -> None:
        with TemporaryDirectory() as temp:
            runner = LiveRunner(RunnerConfig(mode="observe", data_dir=Path(temp), strategy=strategy_config(), signal_reject_cooldown_seconds=1))
            runner.process_event("polymarket", market_message())
            base = int(datetime.now(timezone.utc).timestamp()) * 1_000_000
            for offset in (0, 100_000, 200_000, 1_100_000):
                runner.process_event("polymarket", {"event_type": "book", "asset_id": "down-1", "remaining_seconds": "60", "asks": [["0.82", "10"]], "bids": [["0.81", "5"]], "received_time_us": base + offset})
                runner.process_event("polymarket", {"event_type": "last_trade_price", "asset_id": "down-1", "side": "BUY", "price": "0.82", "size": "12", "timestamp": (base + offset) // 1000})
            runner.close()
            signals = list(iter_partitioned_events(Path(temp), "signals"))
            rejected = [event for event in signals if event["eventType"] == "SIGNAL_REJECTED"]
            self.assertLess(len(rejected), 4)

    def test_canary_mock_transport_records_order_and_fill(self) -> None:
        with TemporaryDirectory() as temp:
            transport = MockTransport()
            runner = LiveRunner(
                RunnerConfig(mode="canary", data_dir=Path(temp), strategy=strategy_config(), transport_enabled=True, live_enabled=True, preflight_passed=True, credentials_present=True),
                transport=transport,
            )
            runner.run_events(event_feed())
            runner.close()
            self.assertEqual(transport.submits, 1)
            self.assertEqual(runner.order_attempts, 1)
            fills = list(iter_partitioned_events(Path(temp), "fills"))
            self.assertTrue(any(event["eventType"] == "FILL" and event["payload"]["trade_id"] == "fill-1" for event in fills))

    def test_actual_fill_vwap_below_floor_halts_new_orders(self) -> None:
        with TemporaryDirectory() as temp:
            runner = LiveRunner(
                RunnerConfig(mode="canary", data_dir=Path(temp), strategy=replace(strategy_config(), min_entry_vwap=Decimal("0.80")), transport_enabled=True, live_enabled=True, preflight_passed=True, credentials_present=True),
                transport=LowPriceFillTransport(),
            )
            runner.run_events(event_feed())
            runner.close()
            health = list(iter_partitioned_events(Path(temp), "health"))
            breaches = [event for event in health if event["eventType"] == "FILL_RISK_BREACH"]
            self.assertTrue(breaches)
            self.assertEqual(breaches[-1]["payload"]["actualVwap"], "0.79")
            self.assertFalse(runner.submit_allowed)

    def test_canary_uses_v2_market_dynamic_fee_before_building_fak(self) -> None:
        with TemporaryDirectory() as temp:
            transport = MockTransport()
            transport.market_fee_details = lambda condition_id: {"rate": "0.12", "exponent": "2", "conditionId": condition_id}
            runner = LiveRunner(
                RunnerConfig(mode="canary", data_dir=Path(temp), strategy=strategy_config(), transport_enabled=True, live_enabled=True, preflight_passed=True, credentials_present=True),
                transport=transport,
            )
            runner.run_events(event_feed())
            runner.close()
            expected_per_share = Decimal("0.82") + Decimal("0.12") * (Decimal("0.82") * Decimal("0.18")) ** 2
            expected_max_quantity = (Decimal("5") / expected_per_share / Decimal("0.01")).to_integral_value(rounding="ROUND_DOWN") * Decimal("0.01")
            self.assertEqual(Decimal(str(transport.last_payload["quantity"])), expected_max_quantity)

    def test_partial_fill_is_deduplicated_and_persisted_for_actual_settlement(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            runner = LiveRunner(
                RunnerConfig(mode="canary", data_dir=root, strategy=strategy_config(), transport_enabled=True, live_enabled=True, preflight_passed=True, credentials_present=True),
                transport=PartialFillTransport("4"),
            )
            runner.run_events(event_feed())
            runner.close()
            record = SQLiteOrderStore(root / "orders.sqlite3").list_all()[0]
            self.assertEqual(record.state, OrderState.PARTIAL)
            self.assertEqual(record.payload["actual_filled_quantity"], "4")
            self.assertEqual(record.payload["actual_filled_cost"], "3.29")
            self.assertEqual(record.payload["actual_fee"], "0.01")
            fills = [event for event in iter_partitioned_events(root, "fills") if event["eventType"] == "FILL"]
            self.assertEqual(len(fills), 1)

            restarted = LiveRunner(RunnerConfig(mode="observe", data_dir=root, strategy=strategy_config()))
            restarted.process_event("polymarket", market_message())
            restarted.process_event("polymarket", {"type": "market_resolved", "market_id": "btc-5m-1", "winner": "UP"})
            restarted.close()
            settlement = list(iter_partitioned_events(root, "settlements"))[-1]
            self.assertEqual(settlement["payload"]["quantity"], "4")
            self.assertEqual(settlement["payload"]["actualCost"], "3.29")
            self.assertEqual(settlement["payload"]["netPnl"], "-3.29")
            self.assertTrue(settlement["payload"]["formal"])

    def test_subminimum_partial_fill_is_recorded_but_excluded_from_formal_loss_gate(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            runner = LiveRunner(
                RunnerConfig(mode="canary", data_dir=root, strategy=strategy_config(), transport_enabled=True, live_enabled=True, preflight_passed=True, credentials_present=True),
                transport=PartialFillTransport("2"),
            )
            runner.run_events(event_feed())
            runner.close()

            restarted = LiveRunner(RunnerConfig(mode="observe", data_dir=root, strategy=strategy_config()))
            restarted.process_event("polymarket", market_message())
            restarted.process_event("polymarket", {"type": "market_resolved", "market_id": "btc-5m-1", "winner": "UP"})
            self.assertEqual(restarted.risk.loss_count, 0)
            restarted.close()
            settlement = list(iter_partitioned_events(root, "settlements"))[-1]
            self.assertFalse(settlement["payload"]["formal"])
            self.assertEqual(settlement["payload"]["actualCost"], "1.65")

    def test_one_order_reservation_per_market(self) -> None:
        with TemporaryDirectory() as temp:
            transport = MockTransport()
            runner = LiveRunner(
                RunnerConfig(mode="canary", data_dir=Path(temp), strategy=strategy_config(), transport_enabled=True, live_enabled=True, preflight_passed=True, credentials_present=True),
                transport=transport,
            )
            events = list(event_feed())
            events.append(events[-1])
            runner.run_events(events)
            runner.close()
            self.assertEqual(transport.submits, 1)
            self.assertEqual(runner.order_attempts, 1)

    def test_risk_gate_allows_only_one_unsettled_market(self) -> None:
        with TemporaryDirectory() as temp:
            transport = MockTransport()
            runner = LiveRunner(
                RunnerConfig(mode="canary", data_dir=Path(temp), strategy=strategy_config(), transport_enabled=True, live_enabled=True, preflight_passed=True, credentials_present=True),
                transport=transport,
            )
            runner.run_events(event_feed())
            second = list(event_feed())
            second[0] = ("polymarket", {**market_message(), "id": "btc-5m-2", "conditionId": "condition-2", "tokens": [{"outcome": "Up", "token_id": "up-2"}, {"outcome": "Down", "token_id": "down-2"}]})
            second[-2] = ("book", {**second[-2][1], "market_id": "btc-5m-2"})
            second[-1] = ("polymarket", {**second[-1][1], "market_id": "btc-5m-2", "token_id": "down-2"})
            runner.run_events(second)
            runner.close()
            self.assertEqual(transport.submits, 1)

    def test_timeout_enters_unknown_and_persists_order(self) -> None:
        with TemporaryDirectory() as temp:
            transport = MockTransport(timeout=True)
            runner = LiveRunner(
                RunnerConfig(mode="canary", data_dir=Path(temp), strategy=strategy_config(), transport_enabled=True, live_enabled=True, preflight_passed=True, credentials_present=True),
                transport=transport,
            )
            runner.run_events(event_feed())
            runner.close()
            store = SQLiteOrderStore(Path(temp) / "orders.sqlite3")
            self.assertEqual(len(store.list_all()), 1)
            self.assertEqual(store.list_all()[0].state, OrderState.UNKNOWN)
            order_events = list(iter_partitioned_events(Path(temp), "orders"))
            self.assertTrue(any(event["eventType"] == "ORDER_UNKNOWN" for event in order_events))
            self.assertFalse(any(event["eventType"] == "ORDER_REJECTED" and "timed out" in str(event.get("payload", {}).get("reason", "")) for event in order_events))

    def test_restart_with_unresolved_unknown_order_fails_closed(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            first = LiveRunner(
                RunnerConfig(mode="canary", data_dir=root, strategy=strategy_config(), transport_enabled=True, live_enabled=True, preflight_passed=True, credentials_present=True),
                transport=MockTransport(timeout=True),
            )
            first.run_events(event_feed())
            first.close()

            restarted_transport = MockTransport()
            restarted = LiveRunner(
                RunnerConfig(mode="canary", data_dir=root, strategy=strategy_config(), transport_enabled=True, live_enabled=True, preflight_passed=True, credentials_present=True),
                transport=restarted_transport,
            )
            self.assertFalse(restarted.submit_allowed)
            self.assertEqual(restarted_transport.submits, 0)
            restarted.close()
            health = list(iter_partitioned_events(root, "health"))
            self.assertTrue(any(event["eventType"] == "ORDER_RECOVERY_BLOCKED" for event in health))

    def test_restart_does_not_reserve_canceled_order_request_as_unsettled_exposure(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client_id = "pm-canceled"
            store = SQLiteOrderStore(root / "orders.sqlite3")
            store.save(OrderRecord(client_id, "old-market", "old-token", OrderState.CANCELED, {"side": "DOWN"}))
            recorder = MonitoringRecorder(root, live_mode="canary")
            recorder.record("orders", "ORDER_REQUEST", venue="POLYMARKET", market_id="old-market", outcome="DOWN", payload={"clientOrderId": client_id})
            recorder.close()
            runner = LiveRunner(
                RunnerConfig(mode="canary", data_dir=root, strategy=strategy_config(), transport_enabled=True, live_enabled=True, preflight_passed=True, credentials_present=True),
                transport=MockTransport(),
            )
            self.assertNotIn("old-market", runner.risk.reserved_markets)
            runner.close()

    def test_live_without_credentials_is_preflight_blocked(self) -> None:
        with TemporaryDirectory() as temp:
            transport = MockTransport()
            runner = LiveRunner(
                RunnerConfig(mode="live", data_dir=Path(temp), strategy=strategy_config(), transport_enabled=True, live_enabled=True, preflight_passed=True, credentials_present=False),
                transport=transport,
            )
            runner.run_events(event_feed())
            runner.close()
            self.assertEqual(transport.submits, 0)

    def test_preflight_block_does_not_reserve_unsettled_market_slot(self) -> None:
        """A blocked canary must not poison the one-market risk slot."""
        with TemporaryDirectory() as temp:
            runner = LiveRunner(
                RunnerConfig(
                    mode="canary",
                    data_dir=Path(temp),
                    strategy=strategy_config(),
                    transport_enabled=True,
                    live_enabled=True,
                    preflight_passed=True,
                    credentials_present=False,
                ),
                transport=MockTransport(),
            )
            self.assertFalse(runner.submit_allowed)
            runner.run_events(event_feed())
            self.assertEqual(runner.risk.reserved_markets, set())
            runner.close()
            signals = list(iter_partitioned_events(Path(temp), "signals"))
            self.assertTrue(any(event["payload"].get("reason") == "preflight_blocked" for event in signals))

    def test_market_ws_book_and_trade_events_are_normalized_for_live_feed(self) -> None:
        with TemporaryDirectory() as temp:
            runner = LiveRunner(RunnerConfig(mode="observe", data_dir=Path(temp), strategy=strategy_config()))
            events = list(event_feed())
            events.insert(
                -1,
                (
                    "polymarket",
                    {
                        "event_type": "book",
                        "asset_id": "down-1",
                        "bids": [{"price": "0.81", "size": "5"}],
                        "asks": [{"price": "0.82", "size": "10"}],
                        "remaining_seconds": "60",
                        "timestamp": 1_787_565_840_000,
                    },
                ),
            )
            events[-1] = (
                "polymarket",
                {
                    "event_type": "last_trade_price",
                    "asset_id": "down-1",
                    "side": "BUY",
                    "price": "0.82",
                    "size": "12",
                    "timestamp": 1_787_565_840_000,
                },
            )
            runner.run_events(events)
            runner.close()
            self.assertGreaterEqual(runner.accepted_signals, 1)

    def test_market_book_history_keeps_a_real_five_second_reference(self) -> None:
        with TemporaryDirectory() as temp:
            runner = LiveRunner(RunnerConfig(mode="observe", data_dir=Path(temp), strategy=strategy_config()))
            first = datetime.now(timezone.utc)
            runner._update_down_price(Decimal("0.80"), first)
            runner._update_down_price(Decimal("0.82"), first + timedelta(seconds=5.1))
            self.assertEqual(runner._down_price_five_seconds_ago(first + timedelta(seconds=5.1), Decimal("0.82")), Decimal("0.80"))
            runner.close()

    def test_binance_kline_updates_replace_the_same_minute(self) -> None:
        with TemporaryDirectory() as temp:
            runner = LiveRunner(RunnerConfig(mode="observe", data_dir=Path(temp), strategy=strategy_config()))
            for close in ("99.5", "99.4", "99.3"):
                runner.process_event("binance", {"s": "BTCUSDT", "k": {"t": 1_000, "T": 61_000, "o": "100", "c": close}})
            self.assertEqual(len(runner.binance_candles), 1)
            self.assertEqual(runner.binance_candles[0].close, Decimal("99.3"))
            runner.close()

    def test_market_price_change_updates_the_cached_down_book(self) -> None:
        with TemporaryDirectory() as temp:
            runner = LiveRunner(RunnerConfig(mode="observe", data_dir=Path(temp), strategy=strategy_config()))
            runner.process_event("polymarket", market_message())
            runner.process_event("polymarket", {"event_type": "book", "asset_id": "down-1", "remaining_seconds": "60", "asks": [["0.82", "10"]], "bids": [["0.81", "5"]]})
            runner.process_event("polymarket", {"event_type": "price_change", "price_changes": [{"asset_id": "down-1", "side": "SELL", "price": "0.81", "size": "10"}]})
            self.assertEqual(runner.book["asks"][0], ["0.81", "10"])
            runner.close()

    def test_polymarket_metadata_events_do_not_restart_the_feed(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            runner = LiveRunner(RunnerConfig(mode="observe", data_dir=root, strategy=strategy_config()))
            runner.process_event("polymarket", market_message())
            runner.process_event("polymarket", {"event_type": "tick_size_change", "asset_id": "down-1", "old_tick_size": "0.01", "new_tick_size": "0.001", "timestamp": "1787666900000"})
            runner.process_event("polymarket", {"event_type": "provider_extension", "asset_id": "down-1"})
            runner.close()
            events = list(iter_partitioned_events(root, "events"))
            health = list(iter_partitioned_events(root, "health"))
            self.assertTrue(any(event["eventType"] == "MARKET_TICK_SIZE_CHANGED" for event in events))
            self.assertTrue(any(event["eventType"] == "POLYMARKET_EVENT_IGNORED" for event in health))

    def test_stale_binance_feed_is_optional_for_a_canary_order(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            transport = MockTransport()
            runner = LiveRunner(
                RunnerConfig(mode="canary", data_dir=root, strategy=strategy_config(), transport_enabled=True, live_enabled=True, preflight_passed=True, credentials_present=True),
                transport=transport,
            )
            runner.process_event("polymarket", market_message())
            old_us = int((datetime.now(timezone.utc) - timedelta(seconds=5)).timestamp() * 1_000_000)
            for minute in range(5):
                runner.process_event("binance", {"received_time_us": old_us, "s": "BTCUSDT", "k": {"t": 1_000 + minute * 60_000, "T": 61_000 + minute * 60_000, "o": "100", "c": "99.5"}})
            runner.process_event("book", {"market_id": "btc-5m-1", "remaining_seconds": "60", "down_price_now": "0.82", "down_price_five_seconds_ago": "0.82", "asks": [["0.82", "10"]], "received_time_us": int(datetime.now(timezone.utc).timestamp() * 1_000_000)})
            runner.process_event("polymarket", {"type": "trade", "market_id": "btc-5m-1", "token_id": "down-1", "side": "BUY", "price": "0.82", "size": "12", "timestamp": "2026-08-24T10:04:00Z"})
            runner.close()
            self.assertEqual(transport.submits, 1)
            signals = list(iter_partitioned_events(root, "signals"))
            self.assertFalse(any(event["payload"].get("reason") == "stale_binance_data" for event in signals))

    def test_canary_allows_only_one_order_per_session_across_markets(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            transport = MockTransport()
            runner = LiveRunner(
                RunnerConfig(mode="canary", data_dir=root, strategy=strategy_config(), transport_enabled=True, live_enabled=True, preflight_passed=True, credentials_present=True),
                transport=transport,
            )
            runner.run_events(event_feed())
            runner.risk.reserved_markets.clear()
            runner._order_markets.clear()
            second = market_message()
            second["id"] = "btc-5m-2"
            second["conditionId"] = "condition-2"
            second["tokens"] = [{"outcome": "Up", "token_id": "up-2"}, {"outcome": "Down", "token_id": "down-2"}]
            runner.process_event("polymarket", second)
            runner.process_event("book", {"market_id": "btc-5m-2", "remaining_seconds": "60", "down_price_now": "0.82", "down_price_five_seconds_ago": "0.82", "asks": [["0.82", "10"]], "received_time_us": int(datetime.now(timezone.utc).timestamp() * 1_000_000)})
            runner.process_event("polymarket", {"type": "trade", "market_id": "btc-5m-2", "token_id": "down-2", "side": "BUY", "price": "0.82", "size": "12", "timestamp": "2026-08-24T10:04:00Z"})
            runner.close()
            self.assertEqual(transport.submits, 1)
            signals = list(iter_partitioned_events(root, "signals"))
            self.assertTrue(any(event["payload"].get("reason") == "canary_session_limit" for event in signals))

    def test_rtds_reference_event_is_recorded_without_affecting_order_gate(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            runner = LiveRunner(RunnerConfig(mode="observe", data_dir=root, strategy=strategy_config()))
            runner.process_event("rtds", {"source": "chainlink", "symbol": "btc/usd", "value": Decimal("65000.50"), "source_time": datetime(2026, 8, 24, tzinfo=timezone.utc)})
            runner.close()
            events = list(iter_partitioned_events(root, "events"))
            reference = [event for event in events if event["eventType"] == "REFERENCE_PRICE"]
            self.assertEqual(reference[-1]["venue"], "POLYMARKET_RTDS")
            self.assertEqual(reference[-1]["payload"]["value"], "65000.50")
            self.assertEqual(runner.order_attempts, 0)
