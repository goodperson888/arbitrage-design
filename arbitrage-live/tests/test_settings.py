from __future__ import annotations

import json
import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError, URLError

from live_trader.settings import (
    KeychainSecretStore,
    PolygonAccountDetector,
    SettingsStore,
    classify_polygon_code,
    effective_order_budget,
    effective_daily_loss_count,
    effective_daily_loss_total,
)
from live_trader.cli import _load_strategy, runtime_environment_from_settings


class SettingsTests(unittest.TestCase):
    def test_runtime_controls_default_to_recording_and_trade_gate(self) -> None:
        with TemporaryDirectory() as temp:
            store = SettingsStore(Path(temp))
            initial = store.load()
            self.assertTrue(initial["dataRecordingEnabled"])
            self.assertTrue(initial["orderSubmissionEnabled"])
            saved = store.save({"liveMode": "live", "orderSubmissionEnabled": False, "dataRecordingEnabled": False})
            self.assertFalse(saved["orderSubmissionEnabled"])
            self.assertFalse(saved["dataRecordingEnabled"])

    def test_strategy_controls_roundtrip_and_validate_budget_floor(self) -> None:
        with TemporaryDirectory() as temp:
            store = SettingsStore(Path(temp))
            saved = store.save({"maxBudgetUsd": "7.5", "activeTradeMinQuantity": "12"})
            self.assertEqual(saved["maxBudgetUsd"], "7.5")
            self.assertEqual(saved["activeTradeMinQuantity"], "12")
            with self.assertRaises(ValueError):
                store.save({"maxBudgetUsd": "2"})

    def test_single_order_amount_is_the_effective_order_budget(self) -> None:
        with TemporaryDirectory() as temp:
            store = SettingsStore(Path(temp))
            saved = store.save({"maxBudgetUsd": "3.5", "activeTradeMinQuantity": "10", "activeTradeMultiplier": "1.5"})
            self.assertEqual(effective_order_budget(saved), Decimal("3.5"))
            strategy = _load_strategy(
                Path(__file__).parents[1] / "config" / "strategy_down_spot5m_v1.json",
                settings=saved,
            )
            self.assertEqual(strategy.max_budget, Decimal("3.5"))
            self.assertEqual(strategy.active_trade_multiplier, Decimal("1.5"))

    def test_single_order_amount_and_multiplier_reject_unsafe_ranges(self) -> None:
        with TemporaryDirectory() as temp:
            store = SettingsStore(Path(temp))
            for values in (
                {"maxBudgetUsd": "2"},
                {"activeTradeMultiplier": "0.9"},
            ):
                with self.subTest(values=values), self.assertRaises(ValueError):
                    store.save(values)

    def test_daily_loss_limits_are_configurable_and_have_safe_defaults(self) -> None:
        with TemporaryDirectory() as temp:
            store = SettingsStore(Path(temp))
            defaults = store.load()
            self.assertEqual(effective_daily_loss_count(defaults), 2)
            self.assertEqual(effective_daily_loss_total(defaults), Decimal("10"))
            saved = store.save({"maxDailyLossCount": "3", "maxDailyLossTotalUsd": "7.5"})
            self.assertEqual(effective_daily_loss_count(saved), 3)
            self.assertEqual(effective_daily_loss_total(saved), Decimal("7.5"))

    def test_daily_loss_limits_reject_non_positive_or_fractional_count(self) -> None:
        with TemporaryDirectory() as temp:
            store = SettingsStore(Path(temp))
            for values in (
                {"maxDailyLossCount": "0"},
                {"maxDailyLossCount": "1.5"},
                {"maxDailyLossTotalUsd": "0"},
            ):
                with self.subTest(values=values), self.assertRaises(ValueError):
                    store.save(values)

    def test_strategy_side_can_be_configured_as_up_down_or_both(self) -> None:
        with TemporaryDirectory() as temp:
            store = SettingsStore(Path(temp))
            for side in ("UP", "DOWN", "BOTH"):
                saved = store.save({"strategySide": side})
                self.assertEqual(saved["strategySide"], side)
            with self.assertRaises(ValueError):
                store.save({"strategySide": "LEFT"})

    def test_settings_accepts_all_polymarket_signature_types(self) -> None:
        with TemporaryDirectory() as temp:
            store = SettingsStore(Path(temp))
            saved = store.save({"signatureType": 3})
            self.assertEqual(saved["signatureType"], 3)

    def test_settings_store_persists_only_allowed_non_secret_fields(self) -> None:
        with TemporaryDirectory() as temp:
            store = SettingsStore(Path(temp))
            saved = store.save({"walletAddress": "0x" + "1" * 40, "rpcUrl": "https://polygon-rpc.com", "privateKey": "must-not-persist"})
            self.assertEqual(saved["walletAddress"], "0x" + "1" * 40)
            raw = (Path(temp) / "ui_settings.json").read_text()
            self.assertNotIn("must-not-persist", raw)
            self.assertNotIn("privateKey", raw)

    def test_settings_store_migrates_deprecated_polygon_public_rpc(self) -> None:
        with TemporaryDirectory() as temp:
            path = Path(temp) / "ui_settings.json"
            path.write_text(json.dumps({"rpcUrl": "https://polygon-rpc.com"}))
            store = SettingsStore(Path(temp))
            self.assertEqual(store.load()["rpcUrl"], "https://polygon.drpc.org")

    def test_keychain_store_uses_fixed_service_and_never_logs_secret(self) -> None:
        calls: list[list[str]] = []

        def fake_run(args, **kwargs):
            calls.append(list(args))
            class Result:
                stdout = "stored-secret\n"
            return Result()

        store = KeychainSecretStore(runner=fake_run, service="test-service", account="test-account")
        self.assertEqual(store.get(), "stored-secret")
        store.set("new-secret")
        self.assertEqual(calls[0], ["security", "find-generic-password", "-a", "test-account", "-s", "test-service", "-w"])
        self.assertEqual(calls[1][:7], ["security", "add-generic-password", "-U", "-a", "test-account", "-s", "test-service"])
        self.assertNotIn("new-secret", " ".join(calls[0]))

    def test_polygon_code_classification(self) -> None:
        self.assertEqual(classify_polygon_code("0x"), ("EOA", False, 0))
        self.assertEqual(classify_polygon_code("0x60016000"), ("CONTRACT_PROXY", True, 1))
        self.assertEqual(classify_polygon_code(""), ("UNKNOWN", False, None))

    def test_account_detector_validates_address_and_maps_rpc_code(self) -> None:
        detector = PolygonAccountDetector(rpc_call=lambda address, url: "0x")
        result = detector.detect("0x" + "a" * 40, "https://polygon-rpc.com")
        self.assertEqual(result["accountType"], "EOA")
        self.assertEqual(result["signatureTypeSuggestion"], 0)
        invalid = detector.detect("not-an-address", "https://polygon-rpc.com")
        self.assertEqual(invalid["status"], "INVALID")

    def test_account_detector_explains_rpc_http_and_network_errors(self) -> None:
        http_detector = PolygonAccountDetector(rpc_call=lambda address, url: (_ for _ in ()).throw(HTTPError(url, 429, "Too Many Requests", None, None)))
        http_result = http_detector.detect("0x" + "a" * 40, "https://polygon-rpc.com")
        self.assertIn("HTTP 429", http_result["error"])
        auth_detector = PolygonAccountDetector(rpc_call=lambda address, url: (_ for _ in ()).throw(HTTPError(url, 401, "Unauthorized", None, None)))
        auth_result = auth_detector.detect("0x" + "a" * 40, "https://polygon-rpc.com")
        self.assertIn("身份验证失败", auth_result["error"])
        forbidden_detector = PolygonAccountDetector(rpc_call=lambda address, url: (_ for _ in ()).throw(HTTPError(url, 403, "Forbidden", None, None)))
        forbidden_result = forbidden_detector.detect("0x" + "a" * 40, "https://polygon.drpc.org")
        self.assertIn("访问被拒绝", forbidden_result["error"])
        network_detector = PolygonAccountDetector(rpc_call=lambda address, url: (_ for _ in ()).throw(URLError("offline")))
        network_result = network_detector.detect("0x" + "a" * 40, "https://polygon-rpc.com")
        self.assertIn("网络连接失败", network_result["error"])

    def test_default_rpc_detector_receives_configured_proxy(self) -> None:
        detector = PolygonAccountDetector()
        calls = {}

        def fake_rpc(address, url, proxy=""):
            calls["proxy"] = proxy
            return "0x"

        detector._rpc_call = fake_rpc
        result = detector.detect("0x" + "a" * 40, "https://polygon-rpc.com", proxy="http://127.0.0.1:7890")
        self.assertEqual(result["accountType"], "EOA")
        self.assertEqual(calls["proxy"], "http://127.0.0.1:7890")

    def test_ui_settings_translate_to_runner_environment_without_persisting_secret(self) -> None:
        env = runtime_environment_from_settings(
            {"liveMode": "live", "transportEnabled": True, "liveTradingEnabled": True, "orderEnabled": True, "preflightApproved": True, "polymarketReadonlyEnabled": True, "binanceReadonlyEnabled": True},
            base={"PYTHONPATH": "src"},
            private_key="runtime-secret",
        )
        self.assertEqual(env["LIVE_MODE"], "live")
        self.assertEqual(env["LIVE_TRANSPORT_ENABLED"], "1")
        self.assertEqual(env["POLYMARKET_PRIVATE_KEY"], "runtime-secret")

    def test_mode_selection_derives_internal_order_switches(self) -> None:
        live = runtime_environment_from_settings({"liveMode": "live"}, base={})
        for name in (
            "LIVE_TRANSPORT_ENABLED",
            "LIVE_TRADING_ENABLE",
            "POLYMARKET_ORDER_ENABLED",
            "POLYMARKET_PREFLIGHT_APPROVED",
        ):
            self.assertEqual(live[name], "1")
        self.assertEqual(live["POLYMARKET_READONLY_NETWORK_ENABLED"], "1")
        self.assertEqual(live["BINANCE_READONLY_NETWORK_ENABLED"], "1")

        observe = runtime_environment_from_settings({"liveMode": "observe"}, base={})
        for name in (
            "LIVE_TRANSPORT_ENABLED",
            "LIVE_TRADING_ENABLE",
            "POLYMARKET_ORDER_ENABLED",
            "POLYMARKET_PREFLIGHT_APPROVED",
        ):
            self.assertEqual(observe[name], "0")

    def test_runtime_controls_translate_to_environment(self) -> None:
        env = runtime_environment_from_settings(
            {"liveMode": "live", "dataRecordingEnabled": False, "orderSubmissionEnabled": False},
            base={},
        )
        self.assertEqual(env["LIVE_DATA_RECORDING_ENABLED"], "0")
        self.assertEqual(env["LIVE_ORDER_SUBMISSION_ENABLED"], "0")

    def test_strategy_file_can_be_overridden_by_saved_ui_values(self) -> None:
        strategy = _load_strategy(
            Path(__file__).parents[1] / "config" / "strategy_down_spot5m_v1.json",
            settings={"maxBudgetUsd": "7.5", "activeTradeMinQuantity": "12"},
        )
        self.assertEqual(strategy.max_budget, Decimal("7.5"))
        self.assertEqual(strategy.active_trade_min_quantity, Decimal("12"))
        self.assertEqual(strategy.min_entry_vwap, Decimal("0.80"))
        self.assertEqual(strategy.min_entry_momentum, Decimal("0.01"))
