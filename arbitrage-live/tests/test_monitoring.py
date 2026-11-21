from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from live_trader.monitoring import MonitoringRecorder, data_usage, iter_partitioned_events, load_runtime_state


class MonitoringTests(unittest.TestCase):
    def test_runtime_state_is_available_when_high_frequency_recording_is_paused(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            recorder = MonitoringRecorder(root, live_mode="observe")
            recorder.enabled = False
            recorder.record(
                "events",
                "MARKET_SNAPSHOT_1S",
                venue="POLYMARKET",
                market_id="m",
                outcome="DOWN",
                payload={"bestAsk": "0.42", "remainingSeconds": "55"},
            )
            recorder.close()
            self.assertEqual(list(iter_partitioned_events(root, "events")), [])
            runtime = load_runtime_state(root)
            self.assertEqual(runtime["events"][-1]["eventType"], "MARKET_SNAPSHOT_1S")
            self.assertEqual(runtime["events"][-1]["payload"]["bestAsk"], "0.42")

    def test_recording_can_be_paused_without_writing_events(self) -> None:
        with TemporaryDirectory() as temp:
            recorder = MonitoringRecorder(Path(temp), live_mode="observe")
            recorder.enabled = False
            self.assertEqual(recorder.record("events", "PAUSED", venue="SYSTEM"), "")
            self.assertTrue(recorder.record("fills", "FILL", venue="POLYMARKET", market_id="m"))
            recorder.close()
            self.assertEqual(list(iter_partitioned_events(Path(temp), "events")), [])
            self.assertEqual(len(list(iter_partitioned_events(Path(temp), "fills"))), 1)

    def test_event_uses_research_envelope_and_live_markers_in_utc_partition(self) -> None:
        with TemporaryDirectory() as temp:
            recorder = MonitoringRecorder(Path(temp), live_mode="observe", raw_queue_size=10)
            event_id = recorder.record(
                "signals",
                "SIGNAL_REJECTED",
                venue="POLYMARKET",
                market_id="btc-5m-1",
                outcome="DOWN",
                source_time_us=1_756_600_000_000_000,
                received_time_us=1_756_600_000_123_000,
                payload={"reason": "stale"},
            )
            recorder.close()
            paths = list((Path(temp) / "signals").glob("date=*/hour=*/part-*.ndjson"))
            self.assertEqual(len(paths), 1)
            event = json.loads(paths[0].read_text().splitlines()[0])
            self.assertEqual(event["schemaVersion"], 1)
            self.assertEqual(event["eventId"], event_id)
            self.assertEqual(event["eventType"], "SIGNAL_REJECTED")
            self.assertEqual(event["venue"], "POLYMARKET")
            self.assertEqual(event["marketId"], "btc-5m-1")
            self.assertEqual(event["outcome"], "DOWN")
            self.assertEqual(event["sourceTimeUs"], 1_756_600_000_000_000)
            self.assertEqual(event["receivedTimeUs"], 1_756_600_000_123_000)
            self.assertTrue(event["paperOnly"])
            self.assertEqual(event["liveMode"], "observe")

    def test_critical_orders_fills_settlements_are_sync_and_failure_disables_orders(self) -> None:
        with TemporaryDirectory() as temp:
            recorder = MonitoringRecorder(Path(temp), live_mode="canary")
            recorder.record("orders", "ORDER_REQUEST", venue="POLYMARKET", market_id="m", payload={"id": "1"})
            recorder.record("fills", "FILL", venue="POLYMARKET", market_id="m", payload={"size": "1"})
            recorder.record("settlements", "SETTLEMENT", venue="POLYMARKET", market_id="m", payload={"pnl": "-1"})
            self.assertTrue(recorder.accepting_orders)
            recorder.fail_critical_writes()
            with self.assertRaises(OSError):
                recorder.record("orders", "ORDER_RESPONSE", venue="POLYMARKET", market_id="m", payload={})
            self.assertFalse(recorder.accepting_orders)

    def test_observe_events_are_marked_paper_only(self) -> None:
        with TemporaryDirectory() as temp:
            recorder = MonitoringRecorder(Path(temp), live_mode="observe")
            recorder.record("signals", "SIGNAL_ACCEPTED", venue="POLYMARKET", market_id="m")
            recorder.close()
            event = next(iter(iter_partitioned_events(Path(temp), "signals")))
            self.assertTrue(event["paperOnly"])

    def test_raw_is_bounded_async_and_export_reads_same_envelope(self) -> None:
        with TemporaryDirectory() as temp:
            recorder = MonitoringRecorder(Path(temp), live_mode="observe", raw_queue_size=1)
            self.assertTrue(recorder.record("raw", "RAW", venue="BINANCE", market_id="btc", payload={"n": 1}))
            # A second event may be accepted after the writer drains; it must never block.
            recorder.record("raw", "RAW", venue="BINANCE", market_id="btc", payload={"n": 2})
            recorder.close()
            events = list(iter_partitioned_events(Path(temp), "raw"))
            self.assertGreaterEqual(len(events), 1)
            self.assertTrue(all(event["paperOnly"] is True for event in events))

    def test_data_usage_reports_queue_drops_and_directory_sizes(self) -> None:
        with TemporaryDirectory() as temp:
            recorder = MonitoringRecorder(Path(temp), live_mode="observe", raw_queue_size=1)
            recorder.record("raw", "RAW", venue="BINANCE", market_id="btc", payload={"n": 1})
            usage = data_usage(Path(temp), recorder)
            self.assertIn("directoryBytes", usage)
            self.assertIn("rawQueueDepth", usage)
            self.assertIn("rawDropped", usage)
            self.assertIn("runtimeStateBytes", usage)
            recorder.close()

    def test_live_cli_session_uses_compatible_recorder(self) -> None:
        with TemporaryDirectory() as temp:
            env = dict(os.environ)
            env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
            result = subprocess.run(
                [sys.executable, "-m", "live_trader.cli", "observe", "--data-dir", temp],
                check=True,
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertIn("real_order_transport=DISABLED", result.stdout)
            events = list(iter_partitioned_events(Path(temp), "sessions"))
            self.assertEqual(len(events), 1)
            self.assertTrue(events[0]["paperOnly"])
            health = list(iter_partitioned_events(Path(temp), "health"))
            self.assertEqual(len(health), 2)
            self.assertTrue(all(event["liveMode"] == "observe" for event in health))

    def test_network_runner_fails_closed_without_live_readonly_switch(self) -> None:
        with TemporaryDirectory() as temp:
            env = dict(os.environ)
            env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
            env.pop("POLYMARKET_READONLY_NETWORK_ENABLED", None)
            env.pop("BINANCE_READONLY_NETWORK_ENABLED", None)
            result = subprocess.run(
                [sys.executable, "-m", "live_trader.cli", "run", "--run-mode", "observe", "--data-dir", temp],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("POLYMARKET_READONLY_NETWORK_ENABLED", result.stdout)
