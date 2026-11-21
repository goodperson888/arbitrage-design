#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from decimal import Decimal
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from arb_research.live.dashboard import DashboardServer
from arb_research.live.strategy_monitor import LiveMonitorConfig, ResearchPaperMonitor
from arb_research.runtime_control import shutdown_control


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Record official BTC 5m market data and paper-trade the research signal."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=ROOT / "configs/research/live_paper_monitor_v1.json",
    )
    parser.add_argument("--data-dir", type=Path, help="Override config data_dir")
    parser.add_argument("--data-root", type=Path, help="Root shared with cross-market and Gate collectors")
    parser.add_argument("--stop-file", type=Path, help="Graceful background shutdown request")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--proxy", help="Optional HTTP/HTTPS/WebSocket proxy URL")
    parser.add_argument("--paper-quantity", type=Decimal, help="Override config paper quantity")
    parser.add_argument("--duration-hours", type=float, help="Stop automatically after this many hours")
    return parser.parse_args()


async def run(args: argparse.Namespace) -> None:
    raw = json.loads(args.config.read_text())
    data_dir = args.data_dir or ROOT / str(raw["data_dir"])
    data_root = getattr(args, "data_root", None)
    config = LiveMonitorConfig(
        data_dir=data_dir,
        strategy_id=str(raw.get("monitor_id", "up_down_active_vwap80_85_remaining90_30_momentum5_01_v1")),
        outcomes=tuple(str(item).upper() for item in raw.get("outcomes", [raw.get("outcome", "DOWN")])),
        price_min=Decimal(str(raw["price_min"])),
        price_max=Decimal(str(raw["price_max"])),
        entry_price_min=Decimal(str(raw.get("entry_price_min", raw["price_min"]))),
        max_remaining_seconds=int(raw["max_remaining_seconds"]),
        min_remaining_seconds=int(raw["min_remaining_seconds"]),
        min_observed_trade_size=Decimal(str(raw["min_observed_trade_size"])),
        momentum_window_seconds=int(raw.get("momentum_window_seconds", 5)),
        min_entry_momentum=Decimal(str(raw.get("min_entry_momentum", "0"))),
        paper_quantity=args.paper_quantity or Decimal(str(raw["paper_quantity"])),
        fee_model=str(raw["fee_model"]),
        fee_rate=Decimal(str(raw["fee_rate"])),
        max_book_age_ms=int(raw["max_book_age_ms"]),
        derived_snapshot_interval_ms=int(raw["derived_snapshot_interval_ms"]),
        raw_price_change_interval_ms=int(raw["raw_price_change_interval_ms"]),
        raw_book_interval_ms=int(raw["raw_book_interval_ms"]),
        stable_book_ms=int(raw.get("stable_book_ms", 0)),
        cross_market_state_path=(
            ((data_root / "cross_market_validation/state/latest.json") if data_root else ROOT / str(raw["cross_market_state_path"]))
            if raw.get("cross_market_state_path") else None
        ),
        cross_market_max_age_ms=int(raw.get("cross_market_max_age_ms", 2500)),
        require_cross_market_resonance=bool(
            raw.get("require_cross_market_resonance", False)
        ),
        early_exit_price=Decimal(str(raw.get("early_exit_price", "0.99"))),
        enable_early_exit=bool(raw.get("enable_early_exit", False)),
        proxy=args.proxy,
        candidates_enabled=bool(raw.get("candidates_enabled", False)),
    )
    monitor = ResearchPaperMonitor(config)
    dashboard = DashboardServer(
        monitor.get_state,
        args.host,
        args.port,
        gate_state_path=(data_root or ROOT / "data") / "gate_monitor/state/latest.json",
    )
    dashboard.start()
    monitor.dashboard_url = f"http://{args.host}:{dashboard.port}/"
    print("Research paper monitor started")
    print(f"Dashboard: {monitor.dashboard_url}")
    print(f"Config: {args.config.resolve()}")
    print(f"Data: {data_dir.resolve()}")
    print("Paper only: no wallet, private key, or real order endpoint is used")

    try:
        duration = args.duration_hours * 3600 if args.duration_hours else None
        async with shutdown_control(monitor.request_stop, getattr(args, "stop_file", None)):
            await monitor.run(duration)
    finally:
        dashboard.stop()


def main() -> int:
    args = parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
