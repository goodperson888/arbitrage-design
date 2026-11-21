from __future__ import annotations

import asyncio
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from live_trader.clob import ClobSigningConfig, ConfiguredClobTransport
from live_trader.credentials import credentials_from_env, redact_secrets
from live_trader.adapters import BinanceSpotReadOnlyAdapter, PolymarketReadOnlyAdapter
from live_trader.execution import LiveOrderDisabled
from live_trader.preflight import run_preflight
from live_trader.readonly import (
    ConnectionState,
    ReadOnlyConnectionSession,
    ReadOnlyNetworkDisabled,
)
from live_trader.replay import ReadOnlyReplay


class FakeConnector:
    def __init__(self) -> None:
        self.connect_calls = 0
        self.ping_calls = 0
        self.closed = False
        self.sent = []

    async def connect(self) -> None:
        self.connect_calls += 1

    async def send(self, payload: object) -> None:
        self.sent.append(payload)

    async def recv(self) -> object:
        return {"type": "trade"}

    async def ping(self) -> None:
        self.ping_calls += 1

    async def close(self) -> None:
        self.closed = True


class PhaseThreeTests(unittest.IsolatedAsyncioTestCase):
    async def test_readonly_session_connects_heartbeats_and_reconnects(self) -> None:
        connector = FakeConnector()
        session = ReadOnlyConnectionSession(connector, heartbeat_interval=2, max_silence=5)
        await session.connect()
        self.assertEqual(session.health.state, ConnectionState.CONNECTED)
        await session.heartbeat()
        self.assertEqual(connector.ping_calls, 1)
        await session.reconnect()
        self.assertEqual(connector.connect_calls, 2)

    async def test_venue_adapters_only_send_readonly_subscriptions(self) -> None:
        connector = FakeConnector()
        polymarket = PolymarketReadOnlyAdapter("wss://unused", connector)
        await polymarket.connect(["down-1"])
        self.assertEqual(connector.sent, [{"assets_ids": ["down-1"], "type": "market"}])
        binance = BinanceSpotReadOnlyAdapter("BTCUSDT", connector)
        await binance.connect()
        self.assertEqual(connector.connect_calls, 2)

    async def test_readonly_session_marks_stale_data_and_blocks_input(self) -> None:
        connector = FakeConnector()
        session = ReadOnlyConnectionSession(connector, max_silence=2)
        await session.connect()
        session.note_message(datetime.now(timezone.utc) - timedelta(seconds=3))
        self.assertEqual(session.health.state, ConnectionState.STALE)
        self.assertFalse(session.can_process_new_signal)

    async def test_readonly_session_rejects_future_event_clock_skew(self) -> None:
        connector = FakeConnector()
        session = ReadOnlyConnectionSession(connector, max_silence=2, max_clock_skew=1)
        await session.connect()
        now = datetime.now(timezone.utc)
        session.note_message(now, event_time=now + timedelta(seconds=3))
        self.assertEqual(session.health.state, ConnectionState.STALE)
        self.assertIn("clock", session.health.disconnect_reason)

    async def test_default_public_network_connector_is_disabled(self) -> None:
        with self.assertRaises(ReadOnlyNetworkDisabled):
            await ReadOnlyConnectionSession.public("wss://example.invalid").connect()

    def test_clob_transport_has_config_but_rejects_orders_by_default(self) -> None:
        config = ClobSigningConfig(chain_id=137, host="", signature_type=0, funder="")
        transport = ConfiguredClobTransport(config)
        with self.assertRaises(LiveOrderDisabled):
            transport.submit({"price": "0.85"})

    def test_credentials_are_environment_only_and_redacted(self) -> None:
        env = {"POLYMARKET_PRIVATE_KEY": "super-secret", "POLYMARKET_API_KEY": "api-secret"}
        credentials = credentials_from_env(env)
        self.assertEqual(credentials.private_key, "super-secret")
        rendered = redact_secrets(json.dumps({"key": credentials.private_key, "api": credentials.api_key}), credentials)
        self.assertNotIn("super-secret", rendered)
        self.assertNotIn("api-secret", rendered)

    def test_replay_normalizes_readonly_events_without_order_attempts(self) -> None:
        messages = [
            ("polymarket", {"type": "trade", "market_id": "btc-1", "token_id": "down-1", "side": "BUY", "price": "0.82", "size": "12", "timestamp": "2026-08-24T10:00:00Z"}),
            ("binance", {"s": "BTCUSDT", "k": {"t": 1_000, "T": 61_000, "o": "100", "c": "99.5"}}),
        ]
        result = ReadOnlyReplay().run(messages)
        self.assertEqual(result.normalized_count, 2)
        self.assertEqual(result.order_attempts, 0)

    def test_preflight_defaults_to_readonly_safe(self) -> None:
        report = run_preflight({})
        self.assertTrue(report.safe)
        self.assertTrue(all(check.passed for check in report.checks))

    def test_preflight_fails_closed_for_order_switch_or_non_observe_mode(self) -> None:
        report = run_preflight({"POLYMARKET_ORDER_ENABLED": "1", "LIVE_MODE": "live"})
        self.assertFalse(report.safe)
        self.assertTrue(any(not check.passed for check in report.checks))
