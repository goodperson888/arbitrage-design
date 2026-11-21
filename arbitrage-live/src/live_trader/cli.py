from __future__ import annotations

import argparse
import asyncio
import json
import os
from decimal import Decimal
from pathlib import Path

from . import __version__
from .clob_transport import ClobTransportConfig, PyClobClientTransport
from .dashboard import DashboardServer
from .execution import SafetyExecution
from .identity import credentials_from_store
from .models import StrategyConfig
from .monitoring import MonitoringRecorder, data_usage
from .preflight import run_preflight
from .readonly import ReadOnlyNetworkDisabled
from .runner import LiveRunner, RunnerConfig
from .settings import KeychainSecretStore, SettingsStore, effective_order_budget, effective_daily_loss_count, effective_daily_loss_total


def _load_strategy(path: Path, settings: dict[str, object] | None = None) -> StrategyConfig:
    data = json.loads(path.read_text())
    settings = dict(settings or {})
    remaining = data.get("remaining_seconds", {})
    return StrategyConfig(
        side=str(settings.get("strategySide", data.get("side", "DOWN"))).upper(),
        min_remaining_seconds=Decimal(str(remaining.get("min", 30))),
        max_remaining_seconds=Decimal(str(remaining.get("max", 90))),
        max_budget=Decimal(str(settings.get("maxBudgetUsd", data.get("max_budget_usd", 5)))),
        min_valid_cost=Decimal(str(data.get("min_valid_cost_usd", 2.5))),
        max_entry_price=Decimal(str(data.get("max_entry_price", 0.85))),
        reversal_window_seconds=Decimal(str(data.get("reversal_window_seconds", 5))),
        reversal_tolerance=Decimal(str(data.get("reversal_tolerance", 0.01))),
        binance_lookback_seconds=Decimal(str(data.get("binance_lookback_seconds", 300))),
        fee_rate=Decimal(str(data.get("fee_rate", 0.07))),
        fee_exponent=Decimal(str(data.get("fee_exponent", 1))),
        quantity_step=Decimal(str(data.get("quantity_step", 0.01))),
        active_trade_min_quantity=Decimal(str(settings.get("activeTradeMinQuantity", data.get("active_trade_min_quantity", 10)))),
        market_data_max_age_seconds=Decimal(str(data.get("market_data_max_age_seconds", 2))),
        min_entry_vwap=Decimal(str(data.get("min_entry_vwap", 0))),
        min_entry_momentum=Decimal(str(data.get("min_entry_momentum", 0))),
        active_trade_multiplier=Decimal(str(settings.get("activeTradeMultiplier", data.get("active_trade_multiplier", 1.5)))),
    )


def runtime_environment_from_settings(settings: dict[str, object], *, base: dict[str, str] | None = None, private_key: str = "") -> dict[str, str]:
    env = dict(base or os.environ)
    mode = str(settings.get("liveMode", env.get("LIVE_MODE", "observe")))
    env["LIVE_MODE"] = mode
    enabled = mode != "observe"
    flag_map = {
        "transportEnabled": "LIVE_TRANSPORT_ENABLED",
        "liveTradingEnabled": "LIVE_TRADING_ENABLE",
        "orderEnabled": "POLYMARKET_ORDER_ENABLED",
        "preflightApproved": "POLYMARKET_PREFLIGHT_APPROVED",
        "polymarketReadonlyEnabled": "POLYMARKET_READONLY_NETWORK_ENABLED",
        "binanceReadonlyEnabled": "BINANCE_READONLY_NETWORK_ENABLED",
    }
    for setting, name in flag_map.items():
        if setting in {"transportEnabled", "liveTradingEnabled", "orderEnabled", "preflightApproved"}:
            env[name] = "1" if enabled else "0"
        else:
            # Both feeds are safe read-only inputs required by the strategy.
            env[name] = "1"
    env["LIVE_DATA_RECORDING_ENABLED"] = "1" if bool(settings.get("dataRecordingEnabled", True)) else "0"
    env["LIVE_ORDER_SUBMISSION_ENABLED"] = "1" if bool(settings.get("orderSubmissionEnabled", True)) else "0"
    if private_key:
        env["POLYMARKET_PRIVATE_KEY"] = private_key
    if settings.get("proxy"):
        env["LIVE_PROXY"] = str(settings["proxy"])
    if settings.get("funder"):
        env["POLYMARKET_FUNDER"] = str(settings["funder"])
    if settings.get("signatureType") is not None:
        env["POLYMARKET_SIGNATURE_TYPE"] = str(settings["signatureType"])
    return env


async def _run_network(args: argparse.Namespace) -> int:
    settings = SettingsStore(args.data_dir).load()
    strategy = _load_strategy(args.strategy_file, settings=settings)
    try:
        recording_policy = json.loads(args.recording_policy_file.read_text())
    except (FileNotFoundError, OSError, ValueError):
        recording_policy = {}
    keychain_key = KeychainSecretStore().get()
    runtime_env = runtime_environment_from_settings(settings, base=dict(os.environ), private_key=keychain_key or os.environ.get("POLYMARKET_PRIVATE_KEY", ""))
    runtime_env.update(credentials_from_store(KeychainSecretStore(account="polymarket-clob-credentials")))
    mode = args.run_mode or runtime_env.get("LIVE_MODE", "observe")
    transport_enabled = runtime_env.get("LIVE_TRANSPORT_ENABLED", "") == "1"
    live_enabled = runtime_env.get("LIVE_TRADING_ENABLE", "") == "1"
    preflight_passed = runtime_env.get("POLYMARKET_PREFLIGHT_APPROVED", "") == "1"
    credentials_present = bool(runtime_env.get("POLYMARKET_PRIVATE_KEY", ""))
    transport = None
    if transport_enabled:
        transport = PyClobClientTransport(
            ClobTransportConfig(
                host=os.environ.get("POLYMARKET_CLOB_HOST", "https://clob.polymarket.com"),
                chain_id=int(runtime_env.get("POLYMARKET_CHAIN_ID", "137")),
                signature_type=int(runtime_env.get("POLYMARKET_SIGNATURE_TYPE", "0")),
                funder=runtime_env.get("POLYMARKET_FUNDER", ""),
                proxy=runtime_env.get("LIVE_PROXY", ""),
                transport_enabled=True,
            ),
            environ=runtime_env,
            preflight_passed=preflight_passed,
        )
    runner = LiveRunner(
        RunnerConfig(
            mode=mode,
            data_dir=args.data_dir,
            strategy=strategy,
            transport_enabled=transport_enabled,
            live_enabled=live_enabled,
            preflight_passed=preflight_passed,
            credentials_present=credentials_present,
            proxy=args.proxy or runtime_env.get("LIVE_PROXY"),
            gamma_url=runtime_env.get("POLYMARKET_GAMMA_URL", "https://gamma-api.polymarket.com"),
            raw_sample_seconds=float(recording_policy.get("raw_sample_seconds", 5)),
            orderbook_sample_seconds=float(recording_policy.get("orderbook_sample_seconds", 5)),
            signal_reject_cooldown_seconds=float(recording_policy.get("signal_reject_cooldown_seconds", 1)),
            orderbook_levels=int(recording_policy.get("orderbook_levels", 10)),
            canary_max_session_orders=1,
            max_daily_loss_count=effective_daily_loss_count(settings),
            max_daily_loss_total=effective_daily_loss_total(settings),
            max_unsettled_markets=int(settings.get("maxUnsettledMarkets", 1)),
            preflight_environment=runtime_env,
        ),
        transport=transport,
    )
    print(f"mode={mode} data={args.data_dir.resolve()} submit_allowed={runner.submit_allowed}")
    # A canary/live session with the explicit order switch paused is still
    # useful for live market monitoring and strategy diagnostics.  Do not
    # terminate the feed just because CLOB credentials/preflight are missing;
    # accepted signals will be recorded as preflight-blocked and no request
    # can reach the transport while the switch is off.  If submission was
    # requested, retain the fail-closed startup behavior.
    order_submission_requested = bool(settings.get("orderSubmissionEnabled", True))
    if mode != "observe" and not runner.submit_allowed:
        for check in runner.preflight.checks:
            if not check.passed:
                print(f"BLOCKED {check.name}: {check.detail}")
        # A failed live/canary preflight must not make the monitoring process
        # disappear.  Keep the read-only feeds and diagnostics alive, while
        # LiveRunner records accepted signals as preflight_blocked and cannot
        # reach the transport.  The operator can repair the failed check and
        # restart before enabling new orders.
        print("ORDER_SUBMISSION_DISABLED: continuing market monitoring; no orders will be submitted")
    try:
        await runner.run_forever()
    except ReadOnlyNetworkDisabled as exc:
        print(f"BLOCKED network: {exc}")
        return 2
    finally:
        runner.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Safety-first Polymarket live executor")
    parser.add_argument("mode", choices=["observe", "canary", "live", "run", "preflight", "data_usage", "dashboard"])
    parser.add_argument("--data-dir", type=Path, default=Path("data/live_trading"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--run-mode", choices=["observe", "canary", "live"], default=None)
    parser.add_argument("--strategy-file", type=Path, default=Path("config/strategy_down_spot5m_v1.json"))
    parser.add_argument("--recording-policy-file", type=Path, default=Path("config/recording_policy.json"))
    parser.add_argument("--proxy", default=os.environ.get("LIVE_PROXY"))
    args = parser.parse_args()

    if args.mode == "run":
        return asyncio.run(_run_network(args))

    if args.mode in {"canary", "live"}:
        args.run_mode = args.mode
        return asyncio.run(_run_network(args))

    if args.mode == "preflight":
        report = run_preflight(os.environ, transport_enabled=False)
        for check in report.checks:
            print(f"{check.name}: {'PASS' if check.passed else 'FAIL'} - {check.detail}")
        return 0 if report.safe else 2

    if args.mode == "data_usage":
        print(data_usage(args.data_dir))
        return 0

    if args.mode == "dashboard":
        server = DashboardServer(args.data_dir, host=args.host, port=args.port)
        server.start()
        print(f"dashboard=http://{args.host}:{server.port} data={args.data_dir.resolve()}")
        try:
            while True:
                import time

                time.sleep(1)
        except KeyboardInterrupt:
            server.stop()
            return 130

    execution = SafetyExecution(args.mode)
    recorder = MonitoringRecorder(args.data_dir, live_mode=args.mode)
    recorder.record(
        "sessions",
        "SESSION_STARTED",
        venue="SYSTEM",
        payload={"mode": args.mode, "version": __version__, "paperOnly": False},
    )
    recorder.record(
        "health",
        "HEALTH_STATUS",
        venue="SYSTEM",
        payload={"transport": "disabled", "status": "observe_only"},
    )
    print(f"mode={args.mode} version={__version__} real_order_transport=DISABLED")
    if args.mode == "live":
        print("live mode is intentionally blocked until a separately reviewed transport exists")
    recorder.record(
        "health",
        "HEALTH_STOPPED",
        venue="SYSTEM",
        payload={"transport": "disabled", "status": "stopped"},
    )
    recorder.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
