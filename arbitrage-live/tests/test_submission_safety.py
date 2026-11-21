from __future__ import annotations

import unittest
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType
from unittest.mock import patch

from live_trader.clob_transport import ClobTransportConfig, PyClobClientTransport
from live_trader.credentials import Credentials
from live_trader.fak import BookLevel, FAKRejected, build_fak_request
from live_trader.monitoring import iter_partitioned_events
from live_trader.order_state import OrderRecord, OrderState, OrderStateMachine, OrderUnknown, SQLiteOrderStore
from live_trader.runner import LiveRunner, RunnerConfig
from test_runner import MockTransport, event_feed, strategy_config


@dataclass
class OrderArgs:
    token_id: str
    price: float
    size: float
    side: str
    user_usdc_balance: float | None = None


class StrictClient:
    """SDK types live on the module, never on a real ClobClient instance."""

    def __init__(self, *, signing_error=None, posting_error=None):
        self.signing_error = signing_error
        self.posting_error = posting_error
        self.signed = []
        self.posts = []

    def create_order(self, args, options=None):
        if self.signing_error:
            raise self.signing_error
        if not isinstance(args, OrderArgs):
            raise TypeError("SDK requires typed OrderArgs")
        self.signed.append((args.token_id, args.price, args.size, args.side, args.user_usdc_balance))
        return args

    def post_order(self, signed, order_type):
        self.posts.append((signed, order_type))
        if self.posting_error:
            raise self.posting_error
        return {"success": True, "status": "matched", "orderID": "remote-offline"}


def sdk_module():
    module = ModuleType("py_clob_client_v2")
    module.OrderArgs = OrderArgs
    module.OrderType = type("OrderType", (), {"FAK": "FAK"})
    module.Side = type("Side", (), {"BUY": "BUY"})
    return module


def request():
    return build_fak_request("btc-5m-1", "down-1", [BookLevel(Decimal("0.82"), Decimal("10"))], strategy_config())


class SubmissionSafetyTests(unittest.TestCase):
    def transport(self):
        transport = PyClobClientTransport(
            ClobTransportConfig(host="https://offline.invalid", transport_enabled=True),
            credentials=Credentials(private_key="offline-private", api_secret="offline-secret"),
            environ={"LIVE_TRADING_ENABLE": "1"}, preflight_passed=True,
        )
        self.addCleanup(transport.close)
        return transport

    def test_lazy_module_types_are_used_for_persisted_mapping_and_fak_limit(self):
        client = StrictClient()
        transport = self.transport()
        with TemporaryDirectory() as temp, patch.dict("sys.modules", {"py_clob_client_v2": sdk_module()}), patch.object(transport, "_build_client", return_value=client):
            machine = OrderStateMachine(SQLiteOrderStore(Path(temp) / "orders.sqlite3"), transport)
            order = machine.register(request())
            result = machine.submit(order.client_order_id)
            self.assertEqual(result.state, OrderState.ACKNOWLEDGED)
            self.assertEqual(len(client.posts), 1)
            self.assertEqual(client.posts[0][1], "FAK")
            self.assertEqual(client.signed[0][1], 0.85)
            self.assertEqual(client.signed[0][3], "BUY")
            self.assertLessEqual(client.signed[0][4], 5)

    def test_signing_failure_is_not_unknown_and_survives_restart_without_recovery(self):
        client = StrictClient(signing_error=ValueError("invalid signature; api_key=offline-secret"))
        transport = self.transport()
        with TemporaryDirectory() as temp, patch.dict("sys.modules", {"py_clob_client_v2": sdk_module()}), patch.object(transport, "_build_client", return_value=client):
            store = SQLiteOrderStore(Path(temp) / "orders.sqlite3")
            machine = OrderStateMachine(store, transport)
            order = machine.register(request())
            result = machine.submit(order.client_order_id)
            self.assertEqual(result.state, OrderState.REJECTED)
            self.assertFalse(result.payload["submission_error"]["requestSent"])
            self.assertEqual(result.payload["submission_error"]["phase"], "prepare")
            self.assertNotIn("offline-secret", str(result.payload))
            self.assertEqual(client.posts, [])
            self.assertEqual(OrderStateMachine(store, transport).recover_open_orders(), [])
            machine.submit(order.client_order_id)
            self.assertEqual(client.posts, [])

    def test_preparation_timeout_is_definitely_not_submitted(self):
        client = StrictClient(signing_error=TimeoutError("metadata lookup timed out"))
        transport = self.transport()
        with TemporaryDirectory() as temp, patch.dict("sys.modules", {"py_clob_client_v2": sdk_module()}), patch.object(transport, "_build_client", return_value=client):
            machine = OrderStateMachine(SQLiteOrderStore(Path(temp) / "orders.sqlite3"), transport)
            order = machine.register(request())
            result = machine.submit(order.client_order_id)
            self.assertEqual(result.state, OrderState.REJECTED)
            self.assertFalse(result.payload["submission_error"]["requestSent"])
            self.assertEqual(client.posts, [])

    def test_late_signing_worker_cannot_post_after_preparation_timeout(self):
        class SlowSigner(StrictClient):
            def create_order(self, args, options=None):
                time.sleep(.04)
                return super().create_order(args, options)

        client = SlowSigner()
        transport = self.transport()
        transport.config = replace(transport.config, request_timeout_seconds=.005)
        with TemporaryDirectory() as temp, patch.dict("sys.modules", {"py_clob_client_v2": sdk_module()}), patch.object(transport, "_build_client", return_value=client):
            machine = OrderStateMachine(SQLiteOrderStore(Path(temp) / "orders.sqlite3"), transport)
            order = machine.register(request())
            result = machine.submit(order.client_order_id)
            self.assertEqual(result.state, OrderState.REJECTED)
            time.sleep(.06)
            self.assertEqual(client.posts, [])

    def test_post_timeout_stays_unknown_without_retry_and_preserves_safe_error(self):
        client = StrictClient(posting_error=TimeoutError("reply lost offline-secret"))
        transport = self.transport()
        with TemporaryDirectory() as temp, patch.dict("sys.modules", {"py_clob_client_v2": sdk_module()}), patch.object(transport, "_build_client", return_value=client):
            store = SQLiteOrderStore(Path(temp) / "orders.sqlite3")
            machine = OrderStateMachine(store, transport)
            order = machine.register(request())
            with self.assertRaises(OrderUnknown):
                machine.submit(order.client_order_id)
            saved = store.get(order.client_order_id)
            self.assertEqual(saved.state, OrderState.UNKNOWN)
            self.assertEqual(saved.payload["submission_error"]["exceptionType"], "TimeoutError")
            self.assertNotIn("offline-secret", str(saved.payload))
            restarted = OrderStateMachine(store, transport)
            with self.assertRaises(OrderUnknown):
                restarted.submit(order.client_order_id)
            self.assertEqual(len(client.posts), 1)

    def test_submitting_record_is_never_sent_again(self):
        with TemporaryDirectory() as temp:
            transport = MockTransport()
            store = SQLiteOrderStore(Path(temp) / "orders.sqlite3")
            machine = OrderStateMachine(store, transport)
            order = machine.register(request())
            machine._transition(order, OrderState.SUBMITTING)
            restarted = OrderStateMachine(store, transport)
            with self.assertRaises(OrderUnknown):
                restarted.submit(order.client_order_id)
            self.assertEqual(transport.submits, 0)

    def test_acknowledged_record_is_not_submitted_twice(self):
        with TemporaryDirectory() as temp:
            transport = MockTransport()
            machine = OrderStateMachine(SQLiteOrderStore(Path(temp) / "orders.sqlite3"), transport)
            order = machine.register(request())
            machine.submit(order.client_order_id)
            with self.assertRaises(OrderUnknown):
                machine.submit(order.client_order_id)
            self.assertEqual(transport.submits, 1)

    def test_two_state_machines_atomically_claim_only_one_submission(self):
        class BlockingTransport(MockTransport):
            def submit(self, payload):
                self.submits += 1
                entered.set()
                release.wait(2)
                return {"status": "matched", "orderID": "one-remote"}

        with TemporaryDirectory() as temp:
            entered, release = threading.Event(), threading.Event()
            transport = BlockingTransport()
            store = SQLiteOrderStore(Path(temp) / "orders.sqlite3")
            first = OrderStateMachine(store, transport)
            second = OrderStateMachine(store, transport)
            order = first.register(request())
            with ThreadPoolExecutor(max_workers=2) as pool:
                pending = pool.submit(first.submit, order.client_order_id)
                try:
                    self.assertTrue(entered.wait(1))
                    with self.assertRaises(OrderUnknown):
                        second.submit(order.client_order_id)
                finally:
                    release.set()
                self.assertEqual(pending.result().state, OrderState.ACKNOWLEDGED)
            self.assertEqual(transport.submits, 1)

    def test_http_408_and_500_remain_unknown_while_400_is_terminal(self):
        class HttpError(Exception):
            def __init__(self, status):
                self.status_code = status
                super().__init__('provider error; "api_key": "offline-secret"')

        for status in (400, 408, 500):
            with self.subTest(status=status), TemporaryDirectory() as temp:
                client = StrictClient(posting_error=HttpError(status))
                transport = self.transport()
                with patch.dict("sys.modules", {"py_clob_client_v2": sdk_module()}), patch.object(transport, "_build_client", return_value=client):
                    store = SQLiteOrderStore(Path(temp) / "orders.sqlite3")
                    machine = OrderStateMachine(store, transport)
                    order = machine.register(request())
                    if status == 400:
                        self.assertEqual(machine.submit(order.client_order_id).state, OrderState.REJECTED)
                    else:
                        with self.assertRaises(OrderUnknown):
                            machine.submit(order.client_order_id)
                        self.assertEqual(store.get(order.client_order_id).state, OrderState.UNKNOWN)
                    self.assertNotIn("offline-secret", str(store.get(order.client_order_id).payload))
                    self.assertEqual(len(client.posts), 1)

    def test_malformed_response_is_unknown_instead_of_stuck_submitting(self):
        class MalformedTransport(MockTransport):
            def submit(self, payload):
                self.submits += 1
                return None

        with TemporaryDirectory() as temp:
            transport = MalformedTransport()
            store = SQLiteOrderStore(Path(temp) / "orders.sqlite3")
            machine = OrderStateMachine(store, transport)
            order = machine.register(request())
            with self.assertRaises(OrderUnknown):
                machine.submit(order.client_order_id)
            self.assertEqual(store.get(order.client_order_id).state, OrderState.UNKNOWN)

    def test_runner_releases_pre_send_failure_but_keeps_legacy_unknown(self):
        client = StrictClient(signing_error=ValueError("invalid signing arguments"))
        transport = self.transport()
        transport.market_fee_details = lambda _: {"rate": "0.07", "exponent": "1"}
        with TemporaryDirectory() as temp, patch.dict("sys.modules", {"py_clob_client_v2": sdk_module()}), patch.object(transport, "_build_client", return_value=client):
            root = Path(temp)
            cfg = RunnerConfig(mode="canary", data_dir=root, strategy=strategy_config(), transport_enabled=True, live_enabled=True, preflight_passed=True, credentials_present=True)
            runner = LiveRunner(cfg, transport=transport)
            runner.run_events(event_feed())
            self.assertEqual(runner.risk.reserved_markets, set())
            self.assertEqual(runner._order_markets, set())
            self.assertEqual(runner.order_attempts, 0)
            runner.close()
            events = list(iter_partitioned_events(root, "orders"))
            failure = next(e for e in events if e["eventType"] == "ORDER_REJECTED")
            self.assertFalse(failure["payload"]["requestSent"])
            self.assertTrue(failure["payload"]["clientOrderId"])
            restarted = LiveRunner(cfg, transport=MockTransport())
            self.assertTrue(restarted.submit_allowed)
            self.assertEqual(restarted.risk.reserved_markets, set())
            restarted.close()
            store = SQLiteOrderStore(root / "orders.sqlite3")
            store.save(OrderRecord("legacy-unknown", "old", "token", OrderState.UNKNOWN, {}))
            blocked = LiveRunner(cfg, transport=MockTransport())
            self.assertFalse(blocked.submit_allowed)
            blocked.close()

    def test_gate_closed_during_signing_refuses_post_without_consuming_canary(self):
        transport = self.transport()

        class GateClosingClient(StrictClient):
            def create_order(self, args, options=None):
                result = super().create_order(args, options)
                transport.preflight_passed = False
                return result

        client = GateClosingClient()
        transport.market_fee_details = lambda _: {"rate": "0.07", "exponent": "1"}
        with TemporaryDirectory() as temp, patch.dict("sys.modules", {"py_clob_client_v2": sdk_module()}), patch.object(transport, "_build_client", return_value=client):
            runner = LiveRunner(RunnerConfig(mode="canary", data_dir=Path(temp), strategy=strategy_config(), transport_enabled=True, live_enabled=True, preflight_passed=True, credentials_present=True), transport=transport)
            runner.run_events(event_feed())
            self.assertEqual(runner.order_store.list_all()[0].state, OrderState.DISABLED)
            self.assertEqual(runner.order_attempts, 0)
            self.assertEqual(runner.risk.reserved_markets, set())
            self.assertEqual(client.posts, [])
            runner.close()

    def test_canceled_record_with_fills_cannot_release_exposure(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = SQLiteOrderStore(root / "orders.sqlite3")
            store.save(OrderRecord("partially-filled", "other-market", "token", OrderState.CANCELED, {"actual_filled_quantity": "1"}, "remote"))
            runner = LiveRunner(RunnerConfig(mode="live", data_dir=root, strategy=strategy_config(), transport_enabled=True, live_enabled=True, preflight_passed=True, credentials_present=True), transport=MockTransport())
            self.assertIn("other-market", runner.risk.reserved_markets)
            runner.run_events(event_feed())
            self.assertIn("other-market", runner.risk.reserved_markets)
            self.assertEqual(runner.transport.submits, 0)
            runner.close()

    def test_rejected_record_cannot_release_another_active_order_in_same_market(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            store = SQLiteOrderStore(root / "orders.sqlite3")
            store.save(OrderRecord("rejected", "other-market", "token", OrderState.REJECTED, {}))
            store.save(OrderRecord("filled", "other-market", "token", OrderState.FILLED, {"actual_filled_quantity": "1"}, "remote"))
            runner = LiveRunner(RunnerConfig(mode="live", data_dir=root, strategy=strategy_config(), transport_enabled=True, live_enabled=True, preflight_passed=True, credentials_present=True), transport=MockTransport())
            runner.run_events(event_feed())
            self.assertIn("other-market", runner.risk.reserved_markets)
            self.assertEqual(runner.transport.submits, 0)
            runner.close()

    def test_depth_walks_multiple_levels_but_never_crosses_price_cap(self):
        cfg = replace(strategy_config(), fee_rate=Decimal("0"))
        order = build_fak_request("m", "t", [BookLevel(Decimal(".86"), Decimal("100")), BookLevel(Decimal(".84"), Decimal("2")), BookLevel(Decimal(".82"), Decimal("2"))], cfg)
        self.assertEqual(order.quantity, Decimal("4"))
        self.assertEqual(order.expected_vwap, Decimal(".83"))
        self.assertEqual(order.limit_price, Decimal(".85"))
        with self.assertRaises(FAKRejected):
            build_fak_request("m", "t", [BookLevel(Decimal(".84"), Decimal("2")), BookLevel(Decimal(".86"), Decimal("100"))], cfg)
