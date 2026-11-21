from __future__ import annotations

import time
import unittest
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from live_trader.clob_transport import (
    ClobTransportConfig,
    PyClobClientTransport,
    TransportRejected,
    TransportTimeout,
)
from live_trader.credentials import Credentials
from live_trader.execution import LiveOrderDisabled
from live_trader.fak import BookLevel, build_fak_request
from live_trader.order_state import OrderState, OrderStateMachine, OrderUnknown, SQLiteOrderStore
from live_trader.preflight import run_preflight
from live_trader.models import StrategyConfig


def config() -> StrategyConfig:
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


def request():
    return build_fak_request(
        "btc-5m-1", "down-1", [BookLevel(Decimal("0.82"), Decimal("10"))], config()
    )


class FakeSdk:
    @dataclass
    class OrderArgs:
        token_id: str
        price: float
        size: float
        side: str
        user_usdc_balance: float

    def __init__(self, timeout: bool = False) -> None:
        self.timeout = timeout
        self.created = []
        self.posted = []
        self.cancelled = []

    def create_order(self, args, options=None):
        self.created.append(args)
        return {"signed": True, "args": args}

    def post_order(self, order, order_type):
        if self.timeout:
            time.sleep(0.05)
        self.posted.append((order, order_type))
        return {"status": "matched", "order_id": "remote-1", "tradeIDs": ["trade-1"]}

    def get_order(self, order_id):
        return {"status": "matched", "order_id": order_id, "tradeIDs": ["trade-1"]}

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return {"status": "canceled", "order_id": order_id}

    def get_trades(self, order_id=None):
        return [{"id": "trade-1", "order_id": order_id, "price": "0.82", "size": "5"}]


class FakeSdkV2(FakeSdk):
    class OrderPayload:
        def __init__(self, orderID):
            self.orderID = orderID

    class TradeParams:
        def __init__(self, id=None):
            self.id = id

    def get_order(self, order_id):
        return {"status": "live", "orderID": order_id, "associate_trades": ["trade-1"]}

    def cancel_order(self, payload):
        self.cancelled.append(payload.orderID)
        return {"status": "canceled", "orderID": payload.orderID}

    def get_trades(self, params=None, only_first_page=False):
        return [{"id": params.id, "taker_order_id": "remote-1", "price": "0.82", "size": "5"}]

    def get_clob_market_info(self, condition_id):
        return {"condition_id": condition_id, "fd": {"r": 0.12, "e": 2, "to": True}, "t": [{"t": "down-1", "o": "Down"}]}


class Provider400(Exception):
    status_code = 400
    error_msg = "no orders found to match with FAK; api_key=secret-value"


class RejectingSdk(FakeSdk):
    def post_order(self, order, order_type):
        raise Provider400()


class MarketOrderSdk(FakeSdkV2):
    class Side:
        BUY = "BUY"

    class OrderType:
        FAK = "FAK"

    class MarketOrderArgs:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class PartialCreateOrderOptions:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    def __init__(self):
        super().__init__()
        self.market_calls = []

    def create_and_post_market_order(self, **kwargs):
        self.market_calls.append(kwargs)
        return {"status": "matched", "order_id": "remote-market"}


class PhaseFourTests(unittest.TestCase):
    def test_prepare_warms_sdk_without_posting(self) -> None:
        sdk = FakeSdk()
        transport = PyClobClientTransport(
            ClobTransportConfig(host="https://example.invalid", transport_enabled=True),
            credentials=Credentials(private_key="fake", api_key="fake"),
            sdk=sdk,
            environ={"LIVE_TRADING_ENABLE": "1"},
            preflight_passed=True,
        )
        transport.prepare()
        self.assertEqual(sdk.posted, [])

    def test_no_remote_reconciliation_closes_local_pending_order_with_audit_note(self) -> None:
        with TemporaryDirectory() as temp:
            store = SQLiteOrderStore(Path(temp) / "orders.sqlite3")
            machine = OrderStateMachine(store, FakeSdk())
            order = machine.register(request())
            submitting = machine._transition(order, OrderState.SUBMITTING)
            closed = machine.reconcile_no_remote_record(submitting.client_order_id)
            self.assertEqual(closed.state, OrderState.CANCELED)
            self.assertEqual(closed.payload["reconciliation"]["status"], "no_remote_record")
            self.assertEqual(machine.recover_open_orders(), [])

    def test_submit_requires_explicit_live_switch_and_preflight(self) -> None:
        sdk = FakeSdk()
        transport = PyClobClientTransport(
            ClobTransportConfig(host="https://example.invalid", chain_id=137, transport_enabled=False),
            credentials=Credentials(private_key="fake", api_key="fake"),
            sdk=sdk,
            environ={"LIVE_TRADING_ENABLE": "1"},
            preflight_passed=True,
        )
        with self.assertRaises(LiveOrderDisabled):
            transport.submit(request())
        self.assertEqual(sdk.posted, [])

    def test_explicit_empty_environment_does_not_fall_back_to_process_secrets(self) -> None:
        transport = PyClobClientTransport(
            ClobTransportConfig(host="https://example.invalid"),
            environ={},
        )
        self.assertEqual(transport.credentials.private_key, "")
        self.assertEqual(transport.credentials.api_secret, "")

    def test_mock_sdk_submits_fak_queries_cancels_and_fetches_trades(self) -> None:
        sdk = FakeSdk()
        transport = PyClobClientTransport(
            ClobTransportConfig(host="https://example.invalid", chain_id=137, transport_enabled=True),
            credentials=Credentials(private_key="fake", api_key="fake"),
            sdk=sdk,
            environ={"LIVE_TRADING_ENABLE": "1"},
            preflight_passed=True,
        )
        result = transport.submit(request())
        self.assertEqual(result["status"], "matched")
        self.assertEqual(transport.fetch("remote-1")["status"], "matched")
        self.assertEqual(transport.cancel("remote-1")["status"], "canceled")
        self.assertEqual(transport.fills("remote-1")[0]["id"], "trade-1")
        self.assertEqual(sdk.posted[0][1], "FAK")

    def test_separate_sign_and_post_preserve_limit_and_avoid_combined_helpers(self) -> None:
        sdk = MarketOrderSdk()
        transport = PyClobClientTransport(
            ClobTransportConfig(host="https://example.invalid", chain_id=137, transport_enabled=True),
            credentials=Credentials(private_key="fake", api_key="fake"),
            sdk=sdk,
            environ={"LIVE_TRADING_ENABLE": "1"},
            preflight_passed=True,
        )
        result = transport.submit(request())
        self.assertEqual(result["order_id"], "remote-1")
        self.assertEqual(sdk.market_calls, [])
        self.assertEqual(sdk.created[0].price, 0.85)
        self.assertEqual(sdk.created[0].user_usdc_balance, float(request().expected_cost))
        self.assertEqual(sdk.posted[0][1], "FAK")

    def test_authoritative_provider_4xx_is_rejected_and_sanitized(self) -> None:
        sdk = RejectingSdk()
        transport = PyClobClientTransport(
            ClobTransportConfig(host="https://example.invalid", chain_id=137, transport_enabled=True),
            credentials=Credentials(private_key="fake", api_key="fake"),
            sdk=sdk,
            environ={"LIVE_TRADING_ENABLE": "1"},
            preflight_passed=True,
        )
        with self.assertRaises(TransportRejected) as raised:
            transport.submit(request())
        self.assertEqual(raised.exception.status_code, 400)
        self.assertNotIn("secret-value", str(raised.exception))

    def test_authoritative_provider_4xx_closes_state_as_rejected(self) -> None:
        with TemporaryDirectory() as temp:
            transport = PyClobClientTransport(
                ClobTransportConfig(host="https://example.invalid", chain_id=137, transport_enabled=True),
                credentials=Credentials(private_key="fake", api_key="fake"),
                sdk=RejectingSdk(),
                environ={"LIVE_TRADING_ENABLE": "1"},
                preflight_passed=True,
            )
            machine = OrderStateMachine(SQLiteOrderStore(Path(temp) / "orders.sqlite3"), transport)
            order = machine.register(request())
            result = machine.submit(order.client_order_id)
            self.assertEqual(result.state, OrderState.REJECTED)
            self.assertEqual(machine.recover_open_orders(), [])
            self.assertTrue(result.payload["provider_error"]["authoritative"])

    def test_v2_sdk_uses_order_payload_and_trade_params(self) -> None:
        sdk = FakeSdkV2()
        transport = PyClobClientTransport(
            ClobTransportConfig(host="https://example.invalid", chain_id=137, transport_enabled=True),
            credentials=Credentials(private_key="fake", api_key="fake"),
            sdk=sdk,
            environ={"LIVE_TRADING_ENABLE": "1"},
            preflight_passed=True,
        )
        self.assertEqual(transport.cancel("remote-1")["orderID"], "remote-1")
        fills = transport.fills("remote-1")
        self.assertEqual(fills[0]["id"], "trade-1")
        self.assertEqual(sdk.cancelled, ["remote-1"])

    def test_v2_market_fee_details_and_order_balance_are_used(self) -> None:
        sdk = FakeSdkV2()
        transport = PyClobClientTransport(
            ClobTransportConfig(host="https://example.invalid", chain_id=137, transport_enabled=True),
            credentials=Credentials(private_key="fake", api_key="fake"),
            sdk=sdk,
            environ={"LIVE_TRADING_ENABLE": "1"},
            preflight_passed=True,
        )
        self.assertEqual(transport.market_fee_details("condition-1"), {"rate": "0.12", "exponent": "2"})
        args = transport._order_args(request())
        self.assertEqual(args.user_usdc_balance, float(request().expected_cost))

    def test_state_machine_accepts_official_v2_order_id_and_statuses(self) -> None:
        with TemporaryDirectory() as temp:
            store = SQLiteOrderStore(Path(temp) / "orders.sqlite3")
            machine = OrderStateMachine(store, FakeSdkV2())
            order = machine.register(request())
            live = machine.reconcile(order.client_order_id, {"success": True, "status": "live", "orderID": "remote-1"})
            self.assertEqual(live.state, OrderState.ACKNOWLEDGED)
            self.assertEqual(live.remote_order_id, "remote-1")
            delayed = machine.reconcile(order.client_order_id, {"success": True, "status": "delayed", "orderID": "remote-1"})
            self.assertEqual(delayed.state, OrderState.ACKNOWLEDGED)

    def test_sdk_timeout_is_reported_without_retry(self) -> None:
        sdk = FakeSdk(timeout=True)
        transport = PyClobClientTransport(
            ClobTransportConfig(host="https://example.invalid", chain_id=137, transport_enabled=True, request_timeout_seconds=0.01),
            credentials=Credentials(private_key="fake", api_key="fake"),
            sdk=sdk,
            environ={"LIVE_TRADING_ENABLE": "1"},
            preflight_passed=True,
        )
        with self.assertRaises(TransportTimeout):
            transport.submit(request())
        self.assertLessEqual(len(sdk.posted), 1)

    def test_transport_works_with_order_state_restart_and_unknown(self) -> None:
        with TemporaryDirectory() as temp:
            sdk = FakeSdk(timeout=True)
            transport = PyClobClientTransport(
                ClobTransportConfig(host="https://example.invalid", chain_id=137, transport_enabled=True, request_timeout_seconds=0.01),
                credentials=Credentials(private_key="fake", api_key="fake"),
                sdk=sdk,
                environ={"LIVE_TRADING_ENABLE": "1"},
                preflight_passed=True,
            )
            store = SQLiteOrderStore(Path(temp) / "orders.sqlite3")
            machine = OrderStateMachine(store, transport)
            order = machine.register(request())
            with self.assertRaises(OrderUnknown):
                machine.submit(order.client_order_id)
            self.assertEqual(store.get(order.client_order_id).state, OrderState.UNKNOWN)

    def test_preflight_blocks_live_switch_even_if_transport_flag_is_true(self) -> None:
        report = run_preflight({"LIVE_TRADING_ENABLE": "1", "LIVE_MODE": "live"})
        self.assertFalse(report.safe)

    def test_preflight_blocks_configured_transport_without_rollout(self) -> None:
        report = run_preflight({}, transport_enabled=True)
        self.assertFalse(report.safe)

    def test_preflight_allows_only_explicit_rollout_with_l1_key(self) -> None:
        report = run_preflight(
            {
                "LIVE_MODE": "canary",
                "LIVE_TRADING_ENABLE": "1",
                "POLYMARKET_ORDER_ENABLED": "1",
                "POLYMARKET_PREFLIGHT_APPROVED": "1",
                "POLYMARKET_PRIVATE_KEY": "runtime-only",
            },
            transport_enabled=True,
            require_credentials=True,
            rollout_approved=True,
        )
        self.assertTrue(report.safe)
