#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import signal
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from arb_research.live.cross_market_validation import (
    CrossMarketValidationConfig,
    CrossMarketValidationService,
)


def parse_args(default_config: Path | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="采集 Binance、Hyperliquid、Deribit 并验证其对 Polymarket BTC 市场结果的关系。"
    )
    parser.add_argument(
        "--config", type=Path,
        default=default_config or ROOT / "configs/research/cross_market_5m_validation_v1.json",
    )
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--proxy")
    parser.add_argument("--duration-hours", type=float)
    parser.add_argument("--store-raw", action="store_true", help="抽样保存原始消息（默认关闭）")
    return parser.parse_args()


async def run(args: argparse.Namespace) -> None:
    raw = json.loads(args.config.read_text())
    config = CrossMarketValidationConfig(
        data_dir=args.data_dir or ROOT / raw["data_dir"],
        market_duration_minutes=int(raw.get("market_duration_minutes", 5)),
        observation_remaining_seconds=tuple(raw["observation_remaining_seconds"]),
        observation_tolerance_seconds=float(raw["observation_tolerance_seconds"]),
        venue_snapshot_interval_ms=int(raw["venue_snapshot_interval_ms"]),
        memory_window_seconds=int(raw["memory_window_seconds"]),
        discovery_interval_seconds=float(raw["discovery_interval_seconds"]),
        settlement_interval_seconds=float(raw["settlement_interval_seconds"]),
        store_raw_messages=bool(args.store_raw or raw.get("store_raw_messages", False)),
        raw_sample_interval_ms=int(raw["raw_sample_interval_ms"]),
        lead_lag_interval_ms=int(raw.get("lead_lag_interval_ms", 100)),
        option_strikes_each_side=int(raw["option_strikes_each_side"]),
        polymarket_book_enabled=bool(raw.get("polymarket_book_enabled", False)),
        polymarket_snapshot_interval_ms=int(
            raw.get("polymarket_snapshot_interval_ms", 1_000)
        ),
        polymarket_quantity=float(raw.get("polymarket_quantity", 10)),
        polymarket_depth_caps=tuple(
            float(value)
            for value in raw.get(
                "polymarket_depth_caps", [0.70, 0.75, 0.80, 0.85, 0.90]
            )
        ),
        proxy=args.proxy,
    )
    service = CrossMarketValidationService(config)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, service.request_stop)
        except NotImplementedError:
            pass
    print(f"Cross-market BTC {config.market_duration_minutes}m validation collector started")
    print(f"Data: {config.data_dir.resolve()}")
    print(
        "Sources: Binance Spot/Futures WS + Hyperliquid WS + "
        "Deribit WS + Polymarket RTDS/Gamma labels"
    )
    print("Storage: 1 aggregate snapshot/second; raw high-frequency messages disabled by default")
    print("Trading: disabled (observation and validation only)")
    await service.run(args.duration_hours * 3600 if args.duration_hours else None)


def main() -> int:
    try:
        asyncio.run(run(parse_args()))
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
