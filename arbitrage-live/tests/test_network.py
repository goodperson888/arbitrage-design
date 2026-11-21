from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from live_trader.network import LiveNetworkFeed, gamma_to_market_message, gamma_winner_outcome, market_slugs_for_now


class NetworkFeedTests(unittest.TestCase):
    def test_successful_feed_connections_publish_recovery_health(self) -> None:
        class Recorder:
            def __init__(self):
                self.events = []

            def record(self, stream, event_type, **kwargs):
                self.events.append((stream, event_type, kwargs))

        class Runner:
            def __init__(self):
                self.recorder = Recorder()

        runner = Runner()

        async def exercise():
            feed = LiveNetworkFeed(runner)
            feed._record_connection_health("POLYMARKET_FEED_CONNECTED", "POLYMARKET")
            feed._record_connection_health("BINANCE_FEED_CONNECTED", "BINANCE")

        asyncio.run(exercise())
        self.assertEqual([event[1] for event in runner.recorder.events], ["POLYMARKET_FEED_CONNECTED", "BINANCE_FEED_CONNECTED"])
        self.assertTrue(all(event[2]["payload"]["status"] == "connected" for event in runner.recorder.events))

    def test_gamma_resolution_requires_explicit_winner_price(self) -> None:
        self.assertEqual(gamma_winner_outcome({"closed": True, "outcomes": '["Up", "Down"]', "outcomePrices": '["1", "0"]'}), "UP")
        self.assertEqual(gamma_winner_outcome({"closed": True, "outcomes": '["Up", "Down"]', "outcomePrices": '["0", "1"]'}), "DOWN")
        self.assertIsNone(gamma_winner_outcome({"closed": True, "outcomes": '["Up", "Down"]', "outcomePrices": '["0.5", "0.5"]'}))

    def test_gamma_payload_becomes_runner_market_definition(self) -> None:
        message = gamma_to_market_message(
            {
                "slug": "btc-updown-5m-1787565600",
                "conditionId": "condition-1",
                "question": "BTC Up or Down - 5m",
                "startDate": "2026-08-24T10:00:00Z",
                "endDate": "2026-08-24T10:05:00Z",
                "active": True,
                "closed": False,
                "outcomes": '["Up", "Down"]',
                "clobTokenIds": '["up-1", "down-1"]',
            }
        )
        self.assertEqual(message["type"], "market_definition")
        self.assertEqual(message["id"], "btc-updown-5m-1787565600")
        self.assertEqual(message["tokens"][1]["token_id"], "down-1")

    def test_market_slug_candidates_are_adjacent_five_minute_windows(self) -> None:
        result = market_slugs_for_now(datetime(2026, 8, 24, 10, 2, 30, tzinfo=timezone.utc))
        self.assertEqual(result, [
            "btc-updown-5m-1787565600",
            "btc-updown-5m-1787565300",
            "btc-updown-5m-1787565900",
        ])

    def test_gamma_discovery_propagates_configured_proxy(self) -> None:
        calls = []

        def fake_fetch(url, timeout=10.0, proxy=None):
            calls.append((url, proxy))
            return {"active": True, "closed": False, "slug": "btc-updown-5m-1"}

        class Runner:
            pass

        feed = LiveNetworkFeed(Runner(), proxy="http://127.0.0.1:7890")
        with patch("live_trader.network._fetch_json", side_effect=fake_fetch):
            result = asyncio.run(feed._discover())
        self.assertIsNotNone(result)
        self.assertEqual(calls[0][1], "http://127.0.0.1:7890")

    def test_market_loop_uses_normalized_definition_mapping_for_settlement(self) -> None:
        class Runner:
            def __init__(self):
                self.events = []
                self.errors = []

            def process_event(self, venue, message):
                self.events.append((venue, message))

            def record_health(self, event_type, detail):
                self.errors.append((event_type, detail))

        raw = {
            "id": "market-1",
            "slug": "btc-updown-5m-1",
            "active": True,
            "closed": False,
            "outcomes": '["Up", "Down"]',
            "clobTokenIds": '["up", "down"]',
        }
        runner = Runner()
        settlement_args = []

        async def exercise():
            feed = LiveNetworkFeed(runner)

            async def discover():
                return raw

            async def socket(_definition):
                feed.request_stop()

            def schedule(slug, *, market_id, by_id=False):
                settlement_args.append((slug, market_id, by_id))

            feed._discover = discover
            feed._market_socket = socket
            feed._schedule_settlement = schedule
            await feed._market_loop()

        asyncio.run(exercise())
        self.assertEqual(runner.errors, [])
        self.assertEqual(settlement_args, [("btc-updown-5m-1", "market-1", False)])

    def test_pending_settlement_keeps_polling_until_official_winner(self) -> None:
        class Runner:
            def __init__(self):
                self.events = []
                self.health = []

            def process_event(self, venue, message):
                self.events.append((venue, message))

            def record_health(self, event_type, detail):
                self.health.append((event_type, detail))

        runner = Runner()
        responses = [
            {"closed": True, "outcomes": '["Up", "Down"]', "outcomePrices": '["0.5", "0.5"]'},
            {"closed": True, "outcomes": '["Up", "Down"]', "outcomePrices": '["0", "1"]'},
        ]

        async def exercise():
            feed = LiveNetworkFeed(runner, settlement_poll_seconds=0.001)
            with patch("live_trader.network._fetch_json", side_effect=responses) as fetch:
                await feed._settlement_loop("btc-updown-5m-1", market_id="market-1", by_id=False)
                self.assertEqual(fetch.call_count, 2)

        asyncio.run(exercise())
        self.assertEqual(runner.events[-1][1]["winner"], "DOWN")

    def test_runner_restart_schedules_persisted_unsettled_market_by_id(self) -> None:
        class Runner:
            def pending_settlement_market_ids(self):
                return {"market-1"}

        async def exercise():
            runner = Runner()
            feed = LiveNetworkFeed(runner)
            scheduled = []

            async def idle():
                await feed.stop_event.wait()

            def schedule(lookup, *, market_id, by_id=False):
                scheduled.append((lookup, market_id, by_id))
                feed.request_stop()

            feed._market_loop = idle
            feed._binance_loop = idle
            feed._schedule_settlement = schedule
            await feed.run()
            return scheduled

        with patch.dict("os.environ", {"POLYMARKET_READONLY_NETWORK_ENABLED": "1", "BINANCE_READONLY_NETWORK_ENABLED": "1"}, clear=False):
            scheduled = asyncio.run(exercise())
        self.assertEqual(scheduled, [("market-1", "market-1", True)])
