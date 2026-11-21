#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from arb_research.cross_market.history import (
    HistoricalVenueClient, build_historical_rows, candles_from_json, candles_to_json,
    evaluate_historical_rows, load_history_cache, load_market_labels, save_history_cache,
    write_historical_outputs,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="拉取并验证三家市场对 Polymarket BTC 5分钟结果的历史关系")
    parser.add_argument(
        "--events", type=Path,
        default=ROOT / "data/markets/btc_5m_official_trades_combined.ndjson",
    )
    parser.add_argument("--limit-markets", type=int, default=200)
    parser.add_argument("--remaining-seconds", default="240,180,120,100,90,60,30")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "outputs/cross_market_validation")
    parser.add_argument("--cache", type=Path, default=ROOT / "data/cross_market_history/cache.json")
    parser.add_argument("--proxy")
    parser.add_argument("--use-cache", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    markets = load_market_labels(args.events, args.limit_markets)
    if not markets:
        raise SystemExit("没有从输入文件找到同时具备定义和官方结算的市场")
    start_ms = min(item.start_ms for item in markets) - 60_000
    end_ms = max(item.end_ms for item in markets) + 60_000
    availability: dict[str, str] = {}
    if args.use_cache:
        cached = load_history_cache(args.cache)
    else:
        cached = {"range": {"startMs": start_ms, "endMs": end_ms}, "venues": {}, "errors": {}}
        with HistoricalVenueClient(proxy=args.proxy) as client:
            fetchers = {
                "binance": lambda: candles_to_json(client.binance_klines(start_ms, end_ms)),
                "hyperliquid": lambda: candles_to_json(client.hyperliquid_klines(start_ms, end_ms)),
                "deribit": lambda: candles_to_json(client.deribit_perpetual_klines(start_ms, end_ms)),
                "dvol": lambda: client.deribit_dvol(start_ms, end_ms),
            }
            for venue, fetch in fetchers.items():
                try:
                    cached["venues"][venue] = fetch()
                except Exception as exc:  # retain partial data and make absence explicit.
                    cached["errors"][venue] = f"{type(exc).__name__}: {exc}"
                    cached["venues"][venue] = []
            cached["endpointsUsed"] = client.endpoints_used
        save_history_cache(args.cache, cached)
    venue_candles = {
        venue: candles_from_json(cached.get("venues", {}).get(venue, []))
        for venue in ("binance", "hyperliquid", "deribit")
    }
    for venue, values in venue_candles.items():
        error = cached.get("errors", {}).get(venue)
        availability[venue] = f"不可用：{error}" if error else f"已拉取 {len(values)} 根1分钟K线"
    dvol = cached.get("venues", {}).get("dvol", [])
    dvol_error = cached.get("errors", {}).get("dvol")
    availability["Deribit DVOL"] = (
        f"不可用：{dvol_error}" if dvol_error else f"已拉取 {len(dvol)} 个历史点"
    )
    rows = build_historical_rows(
        markets, venue_candles, dvol,
        [int(value) for value in args.remaining_seconds.split(",") if value.strip()],
    )
    results = evaluate_historical_rows(rows)
    paths = write_historical_outputs(
        args.output_dir, rows, results, source_file=args.events, availability=availability
    )
    print(json.dumps({"markets": len(markets), "rows": len(rows),
                      "outputs": [str(path.resolve()) for path in paths],
                      "availability": availability}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
