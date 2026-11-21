from __future__ import annotations

import json
import subprocess
import time
import threading
import unittest
from urllib.request import Request, urlopen
from pathlib import Path
from tempfile import TemporaryDirectory
from live_trader.dashboard import STATIC_DIR, DashboardServer, build_state, merge_runtime_control_state, _safe_identity_runtime_error
from live_trader.monitoring import MonitoringRecorder, iter_partitioned_events
from live_trader.order_state import OrderRecord, OrderState, SQLiteOrderStore
from live_trader.settings import KeychainSecretStore, PolygonAccountDetector


class FakeIdentityManager:
    def __init__(self) -> None:
        self.configured = False
        self.last = {}

    def is_configured(self) -> bool:
        return self.configured

    def summary(self):
        return {"configured": self.configured, "message": "配置" if self.configured else "未配置"}

    def configure(self, **kwargs):
        self.configured = True
        self.last = kwargs
        return {"status": "OK", "configured": True, "accountType": "EOA", "signatureType": kwargs["signature_type"], "funderAddress": kwargs["funder_address"], "message": "ok"}

    def validate(self, **_kwargs):
        return {"status": "OK", "ok": True, "message": "validated", "collateralBalance": "5", "allowanceConfigured": True, "openOrderCount": 0, "closedOnly": False}


class DashboardTests(unittest.TestCase):
    def test_runtime_overlay_distinguishes_preflight_ready_from_submission_enabled(self) -> None:
        state = {"safety": {"ordersDisabled": True, "transport": "DISABLED"}, "service": {}, "overview": {}}
        runner = {"status": "running", "mode": "canary", "freshnessVerified": True}
        paused = {
            "desiredMode": "canary",
            "freshnessVerified": True,
            "orderSubmissionEnabled": False,
            "effectiveOrderSubmissionEnabled": False,
        }
        merge_runtime_control_state(state, runner, paused)
        self.assertEqual(state["safety"]["transport"], "READY")
        self.assertTrue(state["safety"]["ordersDisabled"])
        self.assertEqual(state["safety"]["orderBlockReason"], "submission_paused")
        self.assertIn("等待允许新订单", state["overview"]["orderGate"])

        enabled = {**paused, "orderSubmissionEnabled": True, "effectiveOrderSubmissionEnabled": True}
        merge_runtime_control_state(state, runner, enabled)
        self.assertFalse(state["safety"]["ordersDisabled"])
        self.assertEqual(state["safety"]["orderBlockReason"], "")
        self.assertEqual(state["overview"]["orderGate"], "已开启")

    def test_identity_runtime_error_is_actionable_without_exposing_details(self) -> None:
        self.assertIn("未安装 py-clob-client-v2", _safe_identity_runtime_error(RuntimeError("请使用项目 bundled Python，或安装 py-clob-client-v2")))
        self.assertIn("缺少 eth-account", _safe_identity_runtime_error(RuntimeError("当前运行环境缺少 eth-account，无法验证 EOA 私钥对应地址")))
        self.assertNotIn("secret-value", _safe_identity_runtime_error(RuntimeError("secret-value")))
    def test_dashboard_stop_action_does_not_run_startup_preflight(self) -> None:
        result = subprocess.run(
            ["node", "--test", str(Path(__file__).with_name("test_dashboard_ui.mjs"))],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_dashboard_reads_recent_events_without_scanning_large_partition(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            stream = root / "events" / "date=2026-08-25" / "hour=05"
            stream.mkdir(parents=True)
            path = stream / "part-001.ndjson"
            with path.open("w", encoding="utf-8") as handle:
                for index in range(200_000):
                    handle.write(json.dumps({"eventType": "NOISE", "receivedTimeUs": index}) + "\n")
                handle.write(json.dumps({
                    "schemaVersion": 1,
                    "eventId": "latest",
                    "eventType": "MARKET_SNAPSHOT_1S",
                    "venue": "POLYMARKET",
                    "marketId": "btc-5m-large",
                    "outcome": "DOWN",
                    "receivedTimeUs": 999999999,
                    "payload": {"bestAsk": "0.82"},
                }) + "\n")
            started = time.monotonic()
            state = build_state(root)
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 0.5)
            self.assertEqual(state["market"]["marketId"], "btc-5m-large")
            self.assertEqual(state["market"]["bestAsk"], "0.82")

    def test_dashboard_state_exposes_latest_polymarket_market_values(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            recorder = MonitoringRecorder(root, live_mode="observe")
            recorder.record("sessions", "SESSION_STARTED", venue="SYSTEM", payload={"mode": "observe"})
            recorder.record(
                "events",
                "MARKET_DEFINITION",
                venue="POLYMARKET",
                market_id="btc-5m-1",
                outcome="DOWN",
                payload={"question": "BTC Up or Down - 5m", "slug": "btc-updown-5m-1"},
            )
            recorder.record(
                "events",
                "MARKET_SNAPSHOT_1S",
                venue="POLYMARKET",
                market_id="btc-5m-1",
                outcome="DOWN",
                payload={
                    "downPrice": "0.82",
                    "bestAsk": "0.82",
                    "bestBid": "0.81",
                    "remainingSeconds": "58",
                    "binanceSpotReturn5m": "-0.004",
                    "asks": [["0.80", "4"], ["0.82", "6"]],
                },
            )
            recorder.record(
                "events",
                "TRADE",
                venue="POLYMARKET",
                market_id="btc-5m-1",
                outcome="DOWN",
                payload={"price": "0.82", "quantity": "12", "aggressor": "BUY"},
            )
            recorder.close()

            state = build_state(root)
            self.assertEqual(state["market"]["marketId"], "btc-5m-1")
            self.assertEqual(state["market"]["bestAsk"], "0.82")
            self.assertEqual(state["market"]["bestBid"], "0.81")
            self.assertEqual(state["market"]["lastTradePrice"], "0.82")
            self.assertEqual(state["market"]["lastTradeQuantity"], "12")
            self.assertEqual(state["market"]["remainingSeconds"], "58")
            self.assertEqual(state["market"]["binanceSpotReturn5m"], "-0.004")
            self.assertEqual(state["market"]["quotes"]["DOWN"]["bestAsk"], "0.82")
            self.assertEqual(state["market"]["quotes"]["UP"]["bestAsk"], "")
            self.assertEqual(state["market"]["quotes"]["DOWN"]["lastTradePrice"], "0.82")
            self.assertEqual(state["market"]["quotes"]["DOWN"]["vwap10"], "0.8120")
            self.assertEqual(state["market"]["quotes"]["DOWN"]["bestAskSize"], "4")
            self.assertEqual(state["market"]["quotes"]["UP"]["lastTradePrice"], "")
            html = (STATIC_DIR / "index.html").read_text()
            self.assertIn('id="marketBestAsk"', html)
            self.assertIn('id="marketUpAsk"', html)
            self.assertIn('id="marketDownAsk"', html)
            self.assertIn('id="marketUpLast"', html)
            self.assertIn('id="marketDownLast"', html)
            self.assertIn('id="marketUpVwap"', html)
            self.assertIn('id="marketDownVwap"', html)
            self.assertIn('id="marketLiveQuestion"', html)
            self.assertIn('id="marketLiveCountdown"', html)
            self.assertIn('id="marketUpAskSize"', html)
            self.assertIn('id="marketDownAskSize"', html)
            self.assertIn('id="resultUpPrice"', html)
            self.assertIn('id="resultDownPrice"', html)
            self.assertIn('id="resultSum"', html)

    def test_dashboard_separates_paper_and_live_performance_with_sample_aware_win_rate(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            paper = MonitoringRecorder(root, live_mode="observe")
            paper.record(
                "fills",
                "PAPER_FILL",
                venue="POLYMARKET",
                market_id="paper-settled",
                outcome="DOWN",
                payload={"quantity": "5.88", "expectedCost": "4.99451904", "entryPrice": "0.84"},
            )
            paper.record(
                "settlements",
                "SETTLEMENT",
                venue="POLYMARKET",
                market_id="paper-settled",
                outcome="DOWN",
                payload={"netPnl": "0.88548096", "winner": "DOWN", "paper": True},
            )
            paper.record(
                "fills",
                "PAPER_FILL",
                venue="POLYMARKET",
                market_id="paper-open",
                outcome="DOWN",
                payload={"quantity": "5", "expectedCost": "4", "entryPrice": "0.80"},
            )
            paper.record(
                "events",
                "MARKET_SNAPSHOT_1S",
                venue="POLYMARKET",
                market_id="paper-open",
                outcome="DOWN",
                payload={"bestBid": "0.82", "bestAsk": "0.83", "remainingSeconds": "40"},
            )
            paper.close()

            live = MonitoringRecorder(root, live_mode="live")
            live.record(
                "fills",
                "FILL",
                venue="POLYMARKET",
                market_id="live-settled",
                outcome="DOWN",
                event_id="real-fill-1",
                payload={"trade_id": "trade-1", "price": "0.82", "size": "5", "fee": "0.01"},
            )
            live.record(
                "settlements",
                "SETTLEMENT",
                venue="POLYMARKET",
                market_id="live-settled",
                outcome="DOWN",
                payload={"netPnl": "-4.11", "winner": "UP", "formal": True},
            )
            live.close()

            state = build_state(root)
            paper_stats = state["performance"]["paper"]
            live_stats = state["performance"]["live"]
            self.assertEqual(paper_stats["realizedPnl"], "0.88548096")
            self.assertEqual(paper_stats["settledCost"], "4.99451904")
            self.assertEqual(paper_stats["returnRate"], "17.7")
            self.assertEqual(paper_stats["bySide"]["DOWN"]["realizedPnl"], "0.88548096")
            self.assertEqual(paper_stats["bySide"]["DOWN"]["returnRate"], "17.7")
            self.assertEqual(paper_stats["bySide"]["UP"]["settledCount"], 0)
            self.assertEqual(state["countsByKind"]["paper"]["fills"], 2)
            self.assertEqual(state["countsByKind"]["paper"]["settlements"], 1)
            self.assertEqual(state["countsByKind"]["live"]["fills"], 1)
            self.assertEqual(state["countsByKind"]["live"]["settlements"], 1)
            self.assertEqual(paper_stats["winRate"], "100.0")
            self.assertEqual(paper_stats["settledCount"], 1)
            self.assertEqual(paper_stats["openPositions"], 1)
            self.assertEqual(paper_stats["unrealizedPnl"], "0.10")
            self.assertEqual(paper_stats["valuationStatus"], "priced")
            self.assertEqual(live_stats["realizedPnl"], "-4.11")
            self.assertEqual(live_stats["settledCost"], "4.11")
            self.assertEqual(live_stats["returnRate"], "-100.0")
            self.assertEqual(live_stats["bySide"]["DOWN"]["realizedPnl"], "-4.11")
            self.assertEqual(live_stats["bySide"]["DOWN"]["returnRate"], "-100.0")
            self.assertEqual(live_stats["winRate"], "0.0")
            self.assertEqual(live_stats["settledCount"], 1)
            self.assertEqual(live_stats["openPositions"], 0)
            self.assertIsNone(live_stats["unrealizedPnl"])

    def test_dashboard_state_exposes_reference_feeds_and_operator_summary(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            recorder = MonitoringRecorder(root, live_mode="observe")
            recorder.record("health", "CONNECTION_HEALTH", venue="POLYMARKET_RTDS", payload={"state": "connected", "feed": "chainlink"})
            recorder.record("events", "REFERENCE_PRICE", venue="POLYMARKET_RTDS", market_id="btc/usd", payload={"source": "chainlink", "symbol": "btc/usd", "value": "65000.50", "sourceTimeUs": 1700000000000000})
            recorder.record("events", "REFERENCE_PRICE", venue="POLYMARKET_RTDS", market_id="btc/usd", payload={"source": "chainlink_twap", "symbol": "btc/usd", "value": "64999.90", "windowSeconds": 30, "sourceTimeUs": 1700000000000000})
            recorder.close()
            state = build_state(root)
            self.assertEqual(state["referencePrices"]["chainlink"]["value"], "65000.50")
            self.assertEqual(state["referencePrices"]["chainlink_twap"]["windowSeconds"], 30)
            self.assertIn("RTDS", state["connections"]["polymarketRtds"])
            self.assertIn("overview", state)
            html = (STATIC_DIR / "index.html").read_text()
            self.assertIn('id="operatorOverview"', html)
            self.assertIn('id="advancedSettings"', html)

    def test_dashboard_explains_network_terms_and_rtds_default_gate(self) -> None:
        html = (STATIC_DIR / "index.html").read_text()
        self.assertIn("RTDS 默认关闭", html)
        self.assertIn("行情动态", html)
        self.assertIn("被动推送", html)
        self.assertIn("data-tooltip=\"RTDS", html)
        self.assertIn("data-tooltip=\"Market WebSocket", html)
        self.assertIn("data-tooltip=\"CLOB", html)
        self.assertIn("data-tooltip=\"Chainlink TWAP", html)

    def test_dashboard_state_describes_push_feed_and_page_refresh(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            recorder = MonitoringRecorder(root, live_mode="observe")
            recorder.record("events", "ORDERBOOK_SNAPSHOT", venue="POLYMARKET", market_id="m", outcome="DOWN", payload={"down_price_now": "0.4"})
            recorder.close()
            state = build_state(root)
            self.assertIn("被动推送", state["market"]["feed"])
            self.assertEqual(state["market"]["pageRefreshSeconds"], 2.5)

    def test_fresh_live_feeds_override_an_old_persisted_feed_error(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            recorder = MonitoringRecorder(root, live_mode="observe")
            recorder.record("health", "POLYMARKET_FEED_ERROR", venue="SYSTEM", payload={"status": "feed_error"})
            now_us = int(time.time() * 1_000_000)
            recorder.record("events", "ORDERBOOK_SNAPSHOT", venue="POLYMARKET", market_id="m", outcome="DOWN", received_time_us=now_us, payload={"bestAsk": "0.5"})
            recorder.record("events", "REFERENCE_PRICE", venue="BINANCE", market_id="btcusdt_spot", received_time_us=now_us, payload={"open": "100", "close": "99"})
            recorder.close()
            state = build_state(root)
            self.assertEqual(state["service"]["health"], "connected")

    def test_dashboard_uses_runtime_state_when_high_frequency_recording_is_paused(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            recorder = MonitoringRecorder(root, live_mode="observe")
            recorder.enabled = False
            recorder.record(
                "events",
                "MARKET_SNAPSHOT_1S",
                venue="POLYMARKET",
                market_id="runtime-market",
                outcome="DOWN",
                payload={"bestAsk": "0.41", "remainingSeconds": "44"},
            )
            recorder.close()
            state = build_state(root)
            self.assertEqual(state["market"]["marketId"], "runtime-market")
            self.assertEqual(state["market"]["bestAsk"], "0.41")

    def test_dashboard_serves_state_and_recent_events(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            recorder = MonitoringRecorder(root, live_mode="observe")
            recorder.record("sessions", "SESSION_STARTED", venue="SYSTEM", payload={"version": "test"})
            recorder.record("signals", "SIGNAL_REJECTED", venue="POLYMARKET", market_id="m", outcome="DOWN", payload={"reason": "stale"})
            recorder.close()

            state = build_state(root)
            self.assertEqual(state["service"]["status"], "observe_only")
            self.assertEqual(state["mode"], "observe")
            self.assertEqual(state["strategy"]["vwapBand"], "0.80–0.85")
            self.assertEqual(state["strategy"]["momentum"], "入场前5秒同方向上涨≥0.01")
            self.assertGreaterEqual(state["counts"]["signals"], 1)
            self.assertEqual(state["strategyDecision"]["eventType"], "SIGNAL_REJECTED")
            self.assertEqual(state["strategyDecision"]["reason"], "stale")
            self.assertTrue(state["safety"]["ordersDisabled"])
            html = (STATIC_DIR / "index.html").read_text()
            self.assertIn("Polymarket Live Console", html)
            self.assertIn("实盘设置", html)
            self.assertIn("配置并派生身份", html)
            self.assertIn('id="paperWinsCount"', html)
            self.assertIn('id="realWinsCount"', html)
            self.assertIn('id="paperReturnRate"', html)
            self.assertIn('id="realReturnRate"', html)
            self.assertIn('id="paperFillsCount"', html)
            self.assertIn('id="liveSettlementsCount"', html)
            self.assertIn('id="paperUpPnl"', html)
            self.assertIn('id="realDownReturnRate"', html)
            self.assertIn('id="lastSignalStatus"', html)
            self.assertIn('id="lastSignalReason"', html)
            self.assertIn("纸上交易", html)
            self.assertIn("真实交易", html)
            app = (STATIC_DIR / "app.js").read_text()
            self.assertIn("renderPerformance", app)
            self.assertIn("validateIdentity", app)
            self.assertIn("startRunner", html)
            self.assertIn("stopRunner", html)
            self.assertIn("privateKey", (STATIC_DIR / "app.js").read_text())
            app = (STATIC_DIR / "app.js").read_text()
            self.assertIn("盘口更新", app)
            self.assertIn("策略未通过", app)

    def test_dashboard_exposes_execution_diagnostics_and_stop_reason(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            now_us = int(time.time() * 1_000_000)
            recorder = MonitoringRecorder(root, live_mode="canary")
            recorder.record("sessions", "SESSION_STARTED", venue="SYSTEM", received_time_us=now_us - 4_000_000, payload={"mode": "canary"})
            recorder.record("signals", "SIGNAL_REJECTED", venue="POLYMARKET", market_id="m", outcome="DOWN", received_time_us=now_us - 2_000_000, payload={"reason": "active_trade_below_threshold"})
            recorder.record("orders", "ORDER_REJECTED", venue="POLYMARKET", market_id="m", outcome="DOWN", received_time_us=now_us - 1_000_000, payload={"reason": "insufficient depth for minimum valid FAK cost"})
            recorder.record("health", "HEALTH_STOPPED", venue="SYSTEM", received_time_us=now_us - 500_000, payload={"status": "runner_stopped", "reason": "operator_stop"})
            recorder.close()
            state = build_state(root)
            diagnostics = state["executionDiagnostics"]
            self.assertEqual(diagnostics["signalEvaluations"], 1)
            self.assertEqual(diagnostics["rejectedSignals"], 1)
            self.assertEqual(diagnostics["orderRejected"], 1)
            self.assertEqual(diagnostics["runner"]["stopReason"], "operator_stop")
            self.assertIn("insufficient depth for minimum valid FAK cost", {item["reason"] for item in diagnostics["rejectReasons"]})
            html = (STATIC_DIR / "index.html").read_text()
            self.assertIn("为什么没有成交", html)
            self.assertIn('id="diagStopReason"', html)

    def test_settings_api_masks_private_key_and_detects_account(self) -> None:
        with TemporaryDirectory() as temp:
            secret = {"value": ""}

            def fake_run(args, **kwargs):
                class Result:
                    stdout = secret["value"] + "\n"
                if args[1] == "add-generic-password":
                    secret["value"] = args[-1]
                return Result()

            detector = PolygonAccountDetector(rpc_call=lambda address, url: "0x")
            server = DashboardServer(Path(temp), port=0, keychain_store=KeychainSecretStore(runner=fake_run), account_detector=detector)
            initial = server.settings_payload()
            self.assertFalse(initial["privateKeyConfigured"])
            saved = server.save_settings({"walletAddress": "0x" + "1" * 40, "privateKey": "do-not-return"})
            self.assertTrue(saved["privateKeyConfigured"])
            self.assertNotIn("do-not-return", json.dumps(saved))
            detected = server.detect_account({"walletAddress": "0x" + "1" * 40})
            self.assertEqual(detected["accountType"], "EOA")
            preflight = server.preflight_state()
            self.assertTrue(preflight["checks"]["privateKey"]["passed"])

    def test_configure_and_validate_identity_use_clob_manager_without_rpc(self) -> None:
        with TemporaryDirectory() as temp:
            keychain = KeychainSecretStore(runner=lambda *args, **kwargs: type("Result", (), {"stdout": ""})())
            identity = FakeIdentityManager()
            server = DashboardServer(Path(temp), port=0, keychain_store=keychain, identity_manager=identity)
            configured = server.configure_account({"walletAddress": "0x" + "1" * 40, "funder": "0x" + "1" * 40, "signatureType": 0, "privateKey": "0x" + "a" * 64})
            self.assertTrue(configured["configured"])
            self.assertEqual(identity.last["proxy"], "")
            validated = server.validate_account({})
            self.assertTrue(validated["ok"])

    def test_runner_start_uses_saved_settings_without_putting_secret_in_command(self) -> None:
        with TemporaryDirectory() as temp:
            secret = {"value": "runtime-secret"}

            def fake_run(args, **kwargs):
                class Result:
                    stdout = secret["value"] + "\n"
                return Result()

            class FakeProcess:
                def __init__(self):
                    self.returncode = None
                    self.terminated = False
                def poll(self):
                    return self.returncode
                def terminate(self):
                    self.terminated = True
                    self.returncode = 0

            launched = []
            process = FakeProcess()
            def fake_popen(command, **kwargs):
                launched.append((command, kwargs))
                return process

            identity = FakeIdentityManager()
            identity.configured = True
            server = DashboardServer(Path(temp), port=0, keychain_store=KeychainSecretStore(runner=fake_run), account_detector=PolygonAccountDetector(rpc_call=lambda address, url: "0x"), identity_manager=identity, runner_popen=fake_popen, sdk_available=lambda: True, feed_health_provider=lambda: {"marketFresh": True, "binanceFresh": True})
            server.save_settings({
                "walletAddress": "0x" + "1" * 40,
                "liveMode": "live",
                "transportEnabled": True,
                "liveTradingEnabled": True,
                "orderEnabled": True,
                "preflightApproved": True,
                "polymarketReadonlyEnabled": True,
                "binanceReadonlyEnabled": True,
            })
            server.detect_account({})
            started = server.start_runner()
            self.assertEqual(started["status"], "running")
            self.assertNotIn("runtime-secret", json.dumps(launched[0][0]))
            self.assertNotIn("POLYMARKET_PRIVATE_KEY", launched[0][1]["env"])
            stopped = server.stop_runner()
            self.assertEqual(stopped["status"], "stopped")

    def test_live_preflight_requires_authenticated_balance_allowance_and_clean_orders(self) -> None:
        with TemporaryDirectory() as temp:
            keychain = KeychainSecretStore(runner=lambda *args, **kwargs: type("Result", (), {"stdout": "runtime-secret\n"})())
            identity = FakeIdentityManager()
            identity.configured = True
            server = DashboardServer(
                Path(temp),
                port=0,
                keychain_store=keychain,
                identity_manager=identity,
                sdk_available=lambda: True,
                feed_health_provider=lambda: {"marketFresh": True, "binanceFresh": True},
            )
            server.save_settings({"liveMode": "canary", "walletAddress": "0x" + "1" * 40, "funder": "0x" + "1" * 40, "accountType": "EOA", "maxBudgetUsd": "5"})
            report = server.preflight_state()
            self.assertTrue(report["safe"])
            self.assertTrue(report["checks"]["clobIdentity"]["passed"])
            self.assertTrue(report["checks"]["collateralBalance"]["passed"])
            self.assertTrue(report["checks"]["allowance"]["passed"])
            self.assertTrue(report["checks"]["openOrders"]["passed"])
            self.assertTrue(report["checks"]["marketFreshness"]["passed"])
            self.assertTrue(report["checks"]["binanceFreshness"]["passed"])

    def test_live_preflight_fails_closed_on_low_balance_or_open_orders(self) -> None:
        with TemporaryDirectory() as temp:
            class UnsafeIdentity(FakeIdentityManager):
                def validate(self, **_kwargs):
                    return {"status": "OK", "ok": True, "collateralBalance": "2", "allowanceConfigured": False, "openOrderCount": 1, "closedOnly": False}

            identity = UnsafeIdentity()
            identity.configured = True
            keychain = KeychainSecretStore(runner=lambda *args, **kwargs: type("Result", (), {"stdout": "runtime-secret\n"})())
            server = DashboardServer(Path(temp), port=0, keychain_store=keychain, identity_manager=identity, sdk_available=lambda: True, feed_health_provider=lambda: {"marketFresh": False, "binanceFresh": True})
            server.save_settings({"liveMode": "canary", "walletAddress": "0x" + "1" * 40, "funder": "0x" + "1" * 40, "accountType": "EOA", "maxBudgetUsd": "5"})
            report = server.preflight_state(allow_pending_feeds=False)
            self.assertFalse(report["safe"])
            self.assertFalse(report["checks"]["collateralBalance"]["passed"])
            self.assertFalse(report["checks"]["allowance"]["passed"])
            self.assertFalse(report["checks"]["openOrders"]["passed"])
            self.assertFalse(report["checks"]["marketFreshness"]["passed"])

    def test_stopped_live_preflight_marks_feed_checks_pending_instead_of_failed(self) -> None:
        with TemporaryDirectory() as temp:
            identity = FakeIdentityManager()
            identity.configured = True
            keychain = KeychainSecretStore(runner=lambda *args, **kwargs: type("Result", (), {"stdout": "runtime-secret\n"})())
            server = DashboardServer(
                Path(temp),
                port=0,
                keychain_store=keychain,
                identity_manager=identity,
                sdk_available=lambda: True,
                feed_health_provider=lambda: {
                    "marketFresh": False,
                    "binanceFresh": False,
                    "marketAgeSeconds": 900,
                    "binanceAgeSeconds": 900,
                },
            )
            server.save_settings({"liveMode": "canary", "walletAddress": "0x" + "1" * 40, "accountType": "EOA"})

            report = server.preflight_state()

            self.assertTrue(report["safe"])
            self.assertTrue(report["checks"]["marketFreshness"]["passed"])
            self.assertTrue(report["checks"]["marketFreshness"]["pending"])
            self.assertIn("启动后", report["checks"]["marketFreshness"]["detail"])
            self.assertFalse(report["checks"]["binanceFreshness"].get("pending", False))
            self.assertTrue(report["checks"]["binanceFreshness"].get("optional"))

    def test_live_preflight_blocks_unresolved_local_order_state(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            identity = FakeIdentityManager()
            identity.configured = True
            keychain = KeychainSecretStore(runner=lambda *args, **kwargs: type("Result", (), {"stdout": "runtime-secret\n"})())
            SQLiteOrderStore(root / "orders.sqlite3").save(
                OrderRecord("client-1", "market-1", "down-1", OrderState.UNKNOWN, {}, None)
            )
            server = DashboardServer(root, port=0, keychain_store=keychain, identity_manager=identity, sdk_available=lambda: True, feed_health_provider=lambda: {"marketFresh": True, "binanceFresh": True})
            server.save_settings({"liveMode": "canary", "walletAddress": "0x" + "1" * 40, "maxBudgetUsd": "5"})
            report = server.preflight_state()
            self.assertFalse(report["safe"])
            self.assertFalse(report["checks"]["localOrders"]["passed"])

    def test_runner_start_propagates_readonly_feed_switches_to_child(self) -> None:
        with TemporaryDirectory() as temp:
            class FakeProcess:
                pid = 123
                returncode = None
                def poll(self):
                    return self.returncode
                def terminate(self):
                    self.returncode = 0

            launched = []
            process = FakeProcess()
            def fake_popen(command, **kwargs):
                launched.append(kwargs["env"])
                return process

            server = DashboardServer(Path(temp), port=0, runner_popen=fake_popen)
            server.save_settings({"liveMode": "observe", "dataRecordingEnabled": True})
            server.start_runner()
            child_env = launched[0]
            self.assertEqual(child_env["POLYMARKET_READONLY_NETWORK_ENABLED"], "1")
            self.assertEqual(child_env["BINANCE_READONLY_NETWORK_ENABLED"], "1")
            self.assertEqual(child_env["LIVE_MODE"], "observe")
            self.assertEqual(child_env["LIVE_TRANSPORT_ENABLED"], "0")
            self.assertEqual(child_env["LIVE_TRADING_ENABLE"], "0")
            self.assertEqual(child_env["LIVE_DATA_RECORDING_ENABLED"], "1")

    def test_start_runner_switches_a_running_process_to_the_saved_mode(self) -> None:
        with TemporaryDirectory() as temp:
            class FakeProcess:
                def __init__(self, pid):
                    self.pid = pid
                    self.returncode = None
                    self.terminated = False

                def poll(self):
                    return self.returncode

                def terminate(self):
                    self.terminated = True
                    self.returncode = 0

                def wait(self, timeout=None):
                    return self.returncode

            processes = []
            launched = []

            def fake_popen(command, **kwargs):
                process = FakeProcess(100 + len(processes))
                processes.append(process)
                launched.append(kwargs["env"])
                return process

            identity = FakeIdentityManager()
            identity.configured = True
            keychain = KeychainSecretStore(runner=lambda *args, **kwargs: type("Result", (), {"stdout": "runtime-secret\n"})())
            server = DashboardServer(
                Path(temp),
                port=0,
                keychain_store=keychain,
                identity_manager=identity,
                runner_popen=fake_popen,
                sdk_available=lambda: True,
                feed_health_provider=lambda: {"marketFresh": True, "binanceFresh": True},
            )
            server.save_settings({"liveMode": "observe"})
            server.start_runner()
            server.save_settings({"liveMode": "canary", "walletAddress": "0x" + "1" * 40, "accountType": "EOA"})

            switched = server.start_runner()

            self.assertTrue(processes[0].terminated)
            self.assertEqual(len(processes), 2)
            self.assertEqual(launched[1]["LIVE_MODE"], "canary")
            self.assertEqual(switched["mode"], "canary")
            self.assertEqual(server.runner_state()["mode"], "canary")

    def test_stopped_canary_bootstraps_feeds_with_orders_paused_before_enabling(self) -> None:
        with TemporaryDirectory() as temp:
            class FakeProcess:
                pid = 321
                returncode = None

                def poll(self):
                    return self.returncode

                def terminate(self):
                    self.returncode = 0

                def wait(self, timeout=None):
                    return self.returncode

            health = iter([
                {"marketFresh": False, "binanceFresh": False},
                {"marketFresh": True, "binanceFresh": True},
                {"marketFresh": True, "binanceFresh": True},
            ])
            launched = []
            identity = FakeIdentityManager()
            identity.configured = True
            keychain = KeychainSecretStore(runner=lambda *args, **kwargs: type("Result", (), {"stdout": "runtime-secret\n"})())
            server = DashboardServer(
                Path(temp),
                port=0,
                keychain_store=keychain,
                identity_manager=identity,
                runner_popen=lambda command, **kwargs: launched.append(kwargs["env"]) or FakeProcess(),
                sdk_available=lambda: True,
                feed_health_provider=lambda: next(health),
                bootstrap_timeout_seconds=1,
                bootstrap_poll_seconds=0,
            )
            server.save_settings({"liveMode": "canary", "walletAddress": "0x" + "1" * 40, "accountType": "EOA", "orderSubmissionEnabled": True})

            started = server.start_runner()

            self.assertEqual(launched[0]["LIVE_ORDER_SUBMISSION_ENABLED"], "0")
            self.assertTrue(server.settings_store.load()["orderSubmissionEnabled"])
            self.assertTrue(server.control_state()["effectiveOrderSubmissionEnabled"])
            self.assertTrue(started["freshnessVerified"])

    def test_canary_bootstrap_timeout_stops_runner_and_never_enables_orders(self) -> None:
        with TemporaryDirectory() as temp:
            class FakeProcess:
                pid = 654
                returncode = None

                def poll(self):
                    return self.returncode

                def terminate(self):
                    self.returncode = 0

                def wait(self, timeout=None):
                    return self.returncode

            process = FakeProcess()
            identity = FakeIdentityManager()
            identity.configured = True
            keychain = KeychainSecretStore(runner=lambda *args, **kwargs: type("Result", (), {"stdout": "runtime-secret\n"})())
            server = DashboardServer(
                Path(temp),
                port=0,
                keychain_store=keychain,
                identity_manager=identity,
                runner_popen=lambda *args, **kwargs: process,
                sdk_available=lambda: True,
                feed_health_provider=lambda: {"marketFresh": False, "binanceFresh": False},
                bootstrap_timeout_seconds=0,
                bootstrap_poll_seconds=0,
            )
            server.save_settings({"liveMode": "canary", "walletAddress": "0x" + "1" * 40, "accountType": "EOA", "orderSubmissionEnabled": True})

            with self.assertRaisesRegex(RuntimeError, "行情连接超时"):
                server.start_runner()

            self.assertEqual(server.runner_state()["status"], "stopped")
            self.assertFalse(server.control_state()["effectiveOrderSubmissionEnabled"])
            self.assertTrue(server.settings_store.load()["orderSubmissionEnabled"])

    def test_saved_mode_does_not_claim_orders_are_effective_for_running_observe(self) -> None:
        with TemporaryDirectory() as temp:
            class FakeProcess:
                pid = 123
                returncode = None

                def poll(self):
                    return self.returncode

                def terminate(self):
                    self.returncode = 0

                def wait(self, timeout=None):
                    return self.returncode

            server = DashboardServer(Path(temp), port=0, runner_popen=lambda *args, **kwargs: FakeProcess())
            server.save_settings({"liveMode": "observe"})
            server.start_runner()
            server.save_settings({"liveMode": "canary"})

            state = server.control_state()

            self.assertEqual(state["mode"], "observe")
            self.assertEqual(state["desiredMode"], "canary")
            self.assertFalse(state["effectiveOrderSubmissionEnabled"])

    def test_proxy_account_preflight_requires_funder(self) -> None:
        with TemporaryDirectory() as temp:
            keychain = KeychainSecretStore(runner=lambda *args, **kwargs: type("Result", (), {"stdout": "secret\n"})())
            server = DashboardServer(Path(temp), port=0, keychain_store=keychain, account_detector=PolygonAccountDetector(rpc_call=lambda address, url: "0x6001"))
            server.save_settings({"walletAddress": "0x" + "2" * 40, "signatureType": 1, "liveMode": "live"})
            server.save_settings({"accountType": "POLY_PROXY"})
            detected = server.detect_account({"funder": ""})
            self.assertEqual(detected["status"], "INVALID")
            self.assertFalse(server.preflight_state()["checks"]["funder"]["passed"])

    def test_observe_preflight_does_not_require_wallet_secret(self) -> None:
        with TemporaryDirectory() as temp:
            server = DashboardServer(Path(temp), port=0, keychain_store=KeychainSecretStore(runner=lambda *args, **kwargs: type("Result", (), {"stdout": ""})()))
            report = server.preflight_state()
            self.assertTrue(report["safe"])
            self.assertTrue(report["checks"]["privateKey"]["passed"])
            self.assertTrue(report["checks"]["readonlyNetwork"]["passed"])

    def test_mode_is_authoritative_and_redundant_switches_are_not_rendered(self) -> None:
        with TemporaryDirectory() as temp:
            server = DashboardServer(Path(temp), port=0)
            observe = server.save_settings({"liveMode": "observe"})
            self.assertFalse(observe["transportEnabled"])
            self.assertFalse(observe["liveTradingEnabled"])
            canary = server.save_settings({"liveMode": "canary"})
            self.assertTrue(canary["transportEnabled"])
            self.assertTrue(canary["liveTradingEnabled"])
            self.assertTrue(canary["orderEnabled"])
            self.assertTrue(canary["preflightApproved"])
            html = (STATIC_DIR / "index.html").read_text()
            for field_id in ("transportEnabled", "liveTradingEnabled", "orderEnabled", "preflightApproved", "polymarketReadonlyEnabled", "binanceReadonlyEnabled"):
                self.assertNotIn(f'id="{field_id}"', html)
            self.assertIn('id="modeHelp"', html)
            self.assertIn("全部停止", html)
            self.assertIn('id="maxBudgetUsd"', html)
            self.assertIn('id="activeTradeMinQuantity"', html)
            self.assertIn('id="activeTradeMultiplier"', html)
            self.assertIn('id="maxDailyLossCount"', html)
            self.assertIn('id="maxDailyLossTotalUsd"', html)
            self.assertIn('id="maxBudgetUsd"', html)
            self.assertIn('class="config-help"', html)
            self.assertIn("Relayer API", html)

    def test_runtime_control_can_pause_recording_and_order_submission(self) -> None:
        with TemporaryDirectory() as temp:
            server = DashboardServer(Path(temp), port=0)
            initial = server.control_state()
            self.assertTrue(initial["dataRecordingEnabled"])
            self.assertFalse(initial["effectiveOrderSubmissionEnabled"])
            server.save_settings({"liveMode": "live"})
            enabled = server.control_state()
            self.assertFalse(enabled["effectiveOrderSubmissionEnabled"])
            updated = server.set_control({"dataRecordingEnabled": False, "orderSubmissionEnabled": False})
            self.assertFalse(updated["dataRecordingEnabled"])
            self.assertFalse(updated["effectiveOrderSubmissionEnabled"])
            html = (STATIC_DIR / "index.html").read_text()
            self.assertIn('id="toggleCollection"', html)
            self.assertIn('id="toggleTrading"', html)
            self.assertIn("全部停止", html)

    def test_runtime_control_cannot_enable_orders_when_runner_is_stopped(self) -> None:
        with TemporaryDirectory() as temp:
            server = DashboardServer(Path(temp), port=0)
            server.save_settings({"liveMode": "canary", "orderSubmissionEnabled": False})
            with self.assertRaisesRegex(ValueError, "runner 未运行"):
                server.set_control({"orderSubmissionEnabled": True})

    def test_recovery_panel_requires_typed_confirmation_and_audits_close(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            server = DashboardServer(root, port=0)
            server.save_settings({"liveMode": "canary", "orderSubmissionEnabled": False})
            store = SQLiteOrderStore(root / "orders.sqlite3")
            store.save(OrderRecord(
                "pm-test-pending",
                "market-1",
                "token-1",
                OrderState.SUBMITTING,
                {"side": "DOWN", "quantity": "5.95", "expected_cost": "4.99"},
            ))
            recovery = server.recovery_orders()
            self.assertEqual(recovery["unresolvedCount"], 1)
            self.assertTrue(recovery["orders"][0]["canConfirmNoRemote"])
            with self.assertRaises(ValueError):
                server.reconcile_no_remote_order({
                    "clientOrderId": "pm-test-pending",
                    "confirmation": "wrong",
                    "officialNoRecordConfirmed": True,
                })
            result = server.reconcile_no_remote_order({
                "clientOrderId": "pm-test-pending",
                "confirmation": "pm-test-pending",
                "officialNoRecordConfirmed": True,
            })
            self.assertEqual(result["status"], "canceled")
            self.assertEqual(server.recovery_orders()["unresolvedCount"], 0)
            order_events = list(iter_partitioned_events(root, "orders"))
            self.assertEqual(order_events[-1]["eventType"], "ORDER_RECONCILED_NO_REMOTE")

    def test_recovery_can_close_unknown_when_runner_is_stopped_even_if_switch_is_stale(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client_id = "pm-stale-after-restart"
            store = SQLiteOrderStore(root / "orders.sqlite3")
            store.save(OrderRecord(client_id, "market-1", "down-1", OrderState.UNKNOWN, {"side": "DOWN", "quantity": "5"}))
            server = DashboardServer(root, port=0)
            server.save_settings({"liveMode": "canary", "orderSubmissionEnabled": True})
            result = server.reconcile_no_remote_order({"clientOrderId": client_id, "confirmation": client_id, "officialNoRecordConfirmed": True})
            self.assertEqual(result["status"], "canceled")
            self.assertEqual(store.get(client_id).state, OrderState.CANCELED)

    def test_recovery_panel_blocks_when_new_orders_are_enabled(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            server = DashboardServer(root, port=0)
            class RunningProcess:
                def poll(self):
                    return None
            server.runner_process = RunningProcess()
            server.save_settings({"liveMode": "canary", "orderSubmissionEnabled": True})
            SQLiteOrderStore(root / "orders.sqlite3").save(OrderRecord(
                "pm-test-pending",
                "market-1",
                "token-1",
                OrderState.UNKNOWN,
                {"side": "UP", "quantity": "5"},
            ))
            with self.assertRaisesRegex(RuntimeError, "暂停新订单"):
                server.reconcile_no_remote_order({
                    "clientOrderId": "pm-test-pending",
                    "confirmation": "pm-test-pending",
                    "officialNoRecordConfirmed": True,
                })
