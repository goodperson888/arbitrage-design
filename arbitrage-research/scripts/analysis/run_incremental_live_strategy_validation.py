#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from arb_research.experiments.live_strategy_cache import (
    connect_cache,
    evaluate_cases_incrementally,
    load_cases,
    refresh_event_cache,
    write_cached_report,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="增量缓存实时盘口，并快速验证不同价格、秒数、方向和数量。"
    )
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data/live_monitor")
    parser.add_argument(
        "--cache", type=Path,
        default=ROOT / "data/derived/live_strategy_cache.sqlite3",
    )
    parser.add_argument(
        "--cases", type=Path,
        default=ROOT / "configs/research/live_strategy_cases_v1.json",
    )
    parser.add_argument(
        "--out", type=Path,
        default=ROOT / "outputs/live_incremental_strategy_validation",
    )
    parser.add_argument("--no-refresh", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    connection = connect_cache(args.cache)
    try:
        refresh = (
            {"filesScanned": 0, "filesSkipped": 0, "rowsRead": 0}
            if args.no_refresh else refresh_event_cache(connection, args.data_dir / "events")
        )
        summaries, fills, evaluation = evaluate_cases_incrementally(
            connection, load_cases(args.cases)
        )
        paths = write_cached_report(
            args.out, summaries, fills, refresh, evaluation, args.cache
        )
    finally:
        connection.close()
    print(f"Cache refresh: {refresh}")
    print(f"Evaluation: {evaluation}")
    for path in paths:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
