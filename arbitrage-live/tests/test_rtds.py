from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from live_trader.rtds import (
    RTDSReadOnlyAdapter,
    RTDS_NETWORK_ENV,
    normalize_rtds_message,
)
from live_trader.readonly import ReadOnlyNetworkDisabled


class FakeConnector:
    def __init__(self) -> None:
        self.sent = []
        self.connected = False

    async def connect(self) -> None:
        self.connected = True

    async def send(self, payload) -> None:
        self.sent.append(payload)

    async def send_text(self, payload: str) -> None:
        self.sent.append(payload)

    async def recv(self):
        return {"topic": "prices.crypto.chainlink", "type": "update", "timestamp": 1_700_000_000_100, "payload": {"symbol": "btc/usd", "timestamp": 1_700_000_000_000, "value": "65000.5"}}

    async def ping(self) -> None:
        return None

    async def close(self) -> None:
        self.connected = False


class RTDSTests(unittest.IsolatedAsyncioTestCase):
    def test_normalizes_binance_and_chainlink_reference_values_without_float_loss(self) -> None:
        for message, source, symbol in (
            (
                {"topic": "prices.crypto.binance", "type": "update", "timestamp": 1_700_000_000_100, "payload": {"symbol": "btcusdt", "timestamp": 1_700_000_000_000, "value": "65000.50000000"}},
                "binance",
                "btcusdt",
            ),
            (
                {"topic": "prices.crypto.chainlink", "type": "update", "timestamp": 1_700_000_000_100, "payload": {"symbol": "btc/usd", "timestamp": 1_700_000_000_000, "value": "65000.5"}},
                "chainlink",
                "btc/usd",
            ),
            (
                {"topic": "prices.crypto.chainlink.twap", "type": "update", "timestamp": 1_700_000_000_100, "payload": {"symbol": "btc/usd", "timestamp": 1_700_000_000_000, "value": "65000.4", "windowSeconds": 30}},
                "chainlink_twap",
                "btc/usd",
            ),
        ):
            result = normalize_rtds_message(message)
            self.assertEqual(result["source"], source)
            self.assertEqual(result["symbol"], symbol)
            self.assertEqual(result["value"], Decimal(str(message["payload"]["value"])))
            self.assertEqual(result["source_time"], datetime.fromtimestamp(1_700_000_000, tz=timezone.utc))

    def test_rejects_unknown_or_malformed_rtds_messages(self) -> None:
        with self.assertRaises(ValueError):
            normalize_rtds_message({"topic": "comments", "type": "update", "payload": {}})
        with self.assertRaises(ValueError):
            normalize_rtds_message({"topic": "prices.crypto.chainlink", "type": "update", "payload": {"symbol": "btc/usd"}})

        parsed = normalize_rtds_message({"topic": "crypto_prices_chainlink", "type": "update", "timestamp": "2026-08-24T10:00:00Z", "payload": {"symbol": "btc/usd", "value": "65000.5"}})
        self.assertEqual(parsed["source_time"], datetime(2026, 8, 24, 10, tzinfo=timezone.utc))

    async def test_adapter_is_network_disabled_by_default_and_subscribes_read_only_when_enabled(self) -> None:
        connector = FakeConnector()
        adapter = RTDSReadOnlyAdapter(connector=connector)
        with self.assertRaises(ReadOnlyNetworkDisabled):
            await adapter.connect()
        adapter = RTDSReadOnlyAdapter(connector=FakeConnector(), enabled=True)
        await adapter.connect(sources=("chainlink", "chainlink_twap"), symbols=("btc/usd",), twap_windows=(30, 60))
        self.assertEqual(adapter.connector.sent, [
            {"action": "subscribe", "subscriptions": [
                {"topic": "crypto_prices_chainlink", "type": "*", "filters": '{"symbol":"btc/usd"}'},
                {"topic": "prices.crypto.chainlink.twap", "type": "update", "filters": '{"symbol":"btc/usd","windowSeconds":30}'},
                {"topic": "prices.crypto.chainlink.twap", "type": "update", "filters": '{"symbol":"btc/usd","windowSeconds":60}'},
            ]}
        ])

    async def test_adapter_messages_are_normalized(self) -> None:
        adapter = RTDSReadOnlyAdapter(connector=FakeConnector(), enabled=True)
        await adapter.connect(sources=("chainlink",), symbols=("btc/usd",))
        message = await adapter.messages().__anext__()
        self.assertEqual(message["source"], "chainlink")
        self.assertEqual(message["value"], Decimal("65000.5"))

    async def test_heartbeat_uses_raw_rtds_ping_when_supported(self) -> None:
        connector = FakeConnector()
        adapter = RTDSReadOnlyAdapter(connector=connector, enabled=True)
        await adapter.connect(sources=(), symbols=())
        await adapter.heartbeat()
        self.assertEqual(connector.sent, ["PING"])


if __name__ == "__main__":
    unittest.main()
