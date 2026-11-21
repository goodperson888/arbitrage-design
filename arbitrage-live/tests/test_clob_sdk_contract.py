"""Run with project [live] dependencies; every SDK HTTP request is mocked."""
from __future__ import annotations

import base64
import importlib.util
import json
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from live_trader.clob_transport import ClobTransportConfig, PyClobClientTransport
from live_trader.credentials import Credentials
from live_trader.fak import BookLevel, build_fak_request
from live_trader.order_state import OrderState, OrderStateMachine, SQLiteOrderStore
from test_runner import strategy_config


@unittest.skipUnless(importlib.util.find_spec("py_clob_client_v2"), "requires optional project live dependencies")
class InstalledSdkContractTests(unittest.TestCase):
    def test_real_sdk_signs_typed_mapping_and_posts_one_budgeted_fak_over_mock_http(self):
        import httpx
        from py_clob_client_v2.http_helpers import helpers

        bodies = []

        def handle(request):
            path = request.url.path
            if request.method == "GET":
                if path == "/version":
                    return httpx.Response(200, json={"version": 2})
                if path == "/clob-markets/offline-condition":
                    return httpx.Response(200, json={"mts": "0.01", "nr": False, "fd": {"r": .12, "e": 2}, "t": [{"t": "123", "o": "Down"}]})
                raise AssertionError(f"unexpected mock GET: {path}")
            if request.method == "POST" and path == "/order":
                self.assertTrue(request.headers.get("POLY_SIGNATURE"))
                bodies.append(json.loads(request.content))
                return httpx.Response(200, json={"success": True, "status": "matched", "orderID": "offline-remote", "tradeIDs": [], "transactionsHashes": []})
            raise AssertionError(f"unexpected mock method: {request.method}")

        # Synthetic fixture only; neither environment nor Keychain is read.
        credentials = Credentials(
            private_key="0x" + "11" * 32, api_key="offline-api",
            api_secret=base64.urlsafe_b64encode(b"offline-secret").decode(),
            api_passphrase="offline-passphrase",
        )
        transport = PyClobClientTransport(
            ClobTransportConfig(host="https://offline.invalid", transport_enabled=True),
            credentials=credentials, environ={"LIVE_TRADING_ENABLE": "1"}, preflight_passed=True,
        )
        self.addCleanup(transport.close)
        with httpx.Client(transport=httpx.MockTransport(handle), trust_env=False) as http, patch.object(helpers, "_http_client", http), TemporaryDirectory() as temp:
            transport.prepare()
            self.assertEqual(transport.market_fee_details("offline-condition"), {"rate": "0.12", "exponent": "2"})
            cfg = replace(strategy_config(), fee_rate=Decimal(".12"), fee_exponent=Decimal("2"))
            order = build_fak_request("offline-market", "123", [BookLevel(Decimal(".82"), Decimal("10"))], cfg)
            machine = OrderStateMachine(SQLiteOrderStore(Path(temp) / "orders.sqlite3"), transport)
            registered = machine.register(order)
            response = machine.submit(registered.client_order_id)
            self.assertEqual(response.state, OrderState.ACKNOWLEDGED)
            self.assertEqual(response.remote_order_id, "offline-remote")
            self.assertEqual(len(bodies), 1)
            self.assertEqual(bodies[0]["orderType"], "FAK")
            signed = bodies[0]["order"]
            self.assertEqual(str(signed["tokenId"]), "123")
            notional = Decimal(signed["makerAmount"]) / Decimal(1_000_000)
            shares = Decimal(signed["takerAmount"]) / Decimal(1_000_000)
            price = notional / shares
            self.assertLessEqual(price, Decimal(".85"))
            fee = shares * Decimal(".12") * (price * (1 - price)) ** 2
            self.assertLessEqual(notional + fee, order.expected_cost)
            self.assertLessEqual(notional + fee, Decimal("5"))
