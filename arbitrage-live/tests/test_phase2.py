from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Optional

from live_trader.adapters import (
    normalize_binance_kline,
    normalize_polymarket_message,
    rolling_binance_return,
)
from live_trader.discovery import discover_btc_5m, normalize_market_payload
from live_trader.execution import LiveOrderDisabled
from live_trader.fak import BookLevel, FAKRejected, build_fak_request
from live_trader.models import StrategyConfig
from live_trader.order_state import (
    InvalidOrderTransition,
    OrderState,
    OrderStateMachine,
    OrderUnknown,
    SQLiteOrderStore,
)
from live_trader.transport import DisabledOrderTransport


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
    )


class FakeTransport:
    def __init__(self, response: Optional[dict[str, object]] = None, timeout: bool = False, error: bool = False) -> None:
        self.response = response or {"status": "acknowledged", "order_id": "remote-1"}
        self.timeout = timeout
        self.error = error
        self.submits = 0

    def submit(self, request: object) -> dict[str, object]:
        self.submits += 1
        if self.timeout:
            raise TimeoutError("simulated submit timeout")
        if self.error:
            raise RuntimeError("simulated provider TLS failure")
        return self.response


class PhaseTwoTests(unittest.TestCase):
    def test_polymarket_and_binance_messages_normalize_to_decimal_events(self) -> None:
        trade = normalize_polymarket_message(
            {
                "type": "trade",
                "market_id": "btc-1",
                "token_id": "down-1",
                "side": "BUY",
                "price": "0.82",
                "size": "12",
                "timestamp": "2026-08-24T10:00:00Z",
            }
        )
        candle = normalize_binance_kline(
            {
                "s": "BTCUSDT",
                "k": {"t": 1_000, "T": 61_000, "o": "100", "c": "99.5"},
            }
        )
        self.assertEqual(trade.price, Decimal("0.82"))
        self.assertEqual(trade.quantity, Decimal("12"))
        self.assertEqual(candle.close, Decimal("99.5"))
        self.assertEqual(rolling_binance_return([candle] * 5), Decimal("-0.005"))

    def test_polymarket_numeric_string_timestamp_is_epoch_milliseconds(self) -> None:
        trade = normalize_polymarket_message(
            {
                "type": "last_trade_price",
                "market_id": "btc-1",
                "token_id": "down-1",
                "side": "BUY",
                "price": "0.63",
                "size": "20",
                "timestamp": "1787633938904",
            }
        )
        self.assertEqual(trade.event_time, datetime.fromtimestamp(1787633938.904, tz=timezone.utc))

    def test_market_discovery_only_accepts_active_btc_five_minute_down_markets(self) -> None:
        payload = {
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
        market = normalize_market_payload(payload)
        self.assertEqual(discover_btc_5m([payload], datetime(2026, 8, 24, 10, 2, tzinfo=timezone.utc))[0], market)

    def test_market_definition_normalizes_gamma_json_token_fields(self) -> None:
        market = normalize_market_payload(
            {
                "slug": "btc-updown-5m-1",
                "conditionId": "condition-1",
                "question": "BTC Up or Down - 5m",
                "startDate": "2026-08-24T10:00:00Z",
                "endDate": "2026-08-24T10:05:00Z",
                "outcomes": '["Up", "Down"]',
                "clobTokenIds": '["up-token", "down-token"]',
            }
        )
        self.assertEqual(market.market_id, "btc-updown-5m-1")
        self.assertEqual(market.up_token_id, "up-token")
        self.assertEqual(market.down_token_id, "down-token")

    def test_fak_request_uses_one_request_idempotency_and_fee_inclusive_budget(self) -> None:
        request = build_fak_request(
            market_id="btc-5m-1",
            token_id="down-1",
            asks=[BookLevel(Decimal("0.82"), Decimal("3")), BookLevel(Decimal("0.85"), Decimal("10"))],
            config=strategy_config(),
        )
        self.assertEqual(request.order_type, "FAK")
        self.assertLessEqual(request.expected_cost, Decimal("5"))
        self.assertGreaterEqual(request.quantity, Decimal("2.5"))
        self.assertTrue(request.client_order_id.startswith("pm-"))

    def test_fak_depth_rejection_exposes_fee_inclusive_available_depth(self) -> None:
        with self.assertRaises(FAKRejected) as raised:
            build_fak_request(
                "btc-1", "down-1", [BookLevel(Decimal("0.85"), Decimal("1"))], strategy_config()
            )
        self.assertEqual(raised.exception.details["eligibleQuantity"], "1")
        self.assertEqual(raised.exception.details["minimumValidCost"], "2.5")

    def test_disabled_transport_cannot_send(self) -> None:
        with self.assertRaises(LiveOrderDisabled):
            DisabledOrderTransport().submit(object())

    def test_timeout_becomes_unknown_and_restart_recovery_reconciles(self) -> None:
        with TemporaryDirectory() as temp:
            path = Path(temp) / "orders.sqlite3"
            store = SQLiteOrderStore(path)
            machine = OrderStateMachine(store, FakeTransport(timeout=True))
            request = build_fak_request_for_test()
            order = machine.register(request)
            with self.assertRaises(OrderUnknown):
                machine.submit(order.client_order_id)
            self.assertEqual(store.get(order.client_order_id).state, OrderState.UNKNOWN)
            self.assertIn("btc-5m-1", machine.paused_markets)

            restarted = OrderStateMachine(store, FakeTransport({"status": "partial", "order_id": "remote-1"}))
            self.assertEqual(len(restarted.recover_open_orders()), 1)
            restarted.reconcile(order.client_order_id, {"status": "partial", "order_id": "remote-1"})
            self.assertEqual(store.get(order.client_order_id).state, OrderState.PARTIAL)

    def test_provider_error_after_submit_becomes_unknown_without_retry(self) -> None:
        with TemporaryDirectory() as temp:
            store = SQLiteOrderStore(Path(temp) / "orders.sqlite3")
            machine = OrderStateMachine(store, FakeTransport(error=True))
            order = machine.register(build_fak_request_for_test())
            with self.assertRaises(OrderUnknown):
                machine.submit(order.client_order_id)
            self.assertEqual(store.get(order.client_order_id).state, OrderState.UNKNOWN)
            self.assertIn(order.market_id, machine.paused_markets)

    def test_register_is_idempotent(self) -> None:
        with TemporaryDirectory() as temp:
            store = SQLiteOrderStore(Path(temp) / "orders.sqlite3")
            machine = OrderStateMachine(store, FakeTransport())
            request = build_fak_request_for_test()
            first = machine.register(request)
            second = machine.register(request)
            self.assertEqual(first.client_order_id, second.client_order_id)
            self.assertEqual(len(store.list_all()), 1)

    def test_state_machine_rejects_terminal_to_submitting_transition(self) -> None:
        with TemporaryDirectory() as temp:
            store = SQLiteOrderStore(Path(temp) / "orders.sqlite3")
            machine = OrderStateMachine(store, FakeTransport({"status": "filled", "order_id": "remote-1"}))
            request = build_fak_request_for_test()
            order = machine.register(request)
            machine.submit(order.client_order_id)
            with self.assertRaises(InvalidOrderTransition):
                machine._transition(store.get(order.client_order_id), OrderState.SUBMITTING)


def build_fak_request_for_test() -> object:
    return build_fak_request(
        market_id="btc-5m-1",
        token_id="down-1",
        asks=[BookLevel(Decimal("0.82"), Decimal("5"))],
        config=strategy_config(),
    )
