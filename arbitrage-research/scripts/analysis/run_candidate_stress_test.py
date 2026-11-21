#!/usr/bin/env python3
"""Run the shortlisted final-90-second entry stress-test matrix."""
from __future__ import annotations

import argparse
import csv
from decimal import Decimal
import json
from pathlib import Path

from arb_research.experiments.candidate_stress import StressCase, load_trade_series, run_stress_case


RANGES = ((Decimal("0.75"), Decimal("0.80")), (Decimal("0.80"), Decimal("0.85")))
OUTCOMES = ("UP", "DOWN")


def cases() -> list[StressCase]:
    result: list[StressCase] = []
    for lower, upper in RANGES:
        price_label = f"{lower}-{upper}"
        for outcome in OUTCOMES:
            for max_remaining, min_remaining in ((90, 60), (60, 30), (30, 0), (90, 0)):
                result.append(StressCase(
                    case_id=f"time__{price_label}__{outcome}__{max_remaining}-{min_remaining}",
                    price_min=lower,
                    price_max=upper,
                    outcome=outcome,
                    max_remaining_seconds=max_remaining,
                    min_remaining_seconds=min_remaining,
                ))
            for path in ("up_cross", "down_cross"):
                result.append(StressCase(
                    case_id=f"path__{price_label}__{outcome}__{path}",
                    price_min=lower,
                    price_max=upper,
                    outcome=outcome,
                    path=path,
                ))
            for count in (2, 3):
                result.append(StressCase(
                    case_id=f"confirm_trades__{price_label}__{outcome}__{count}",
                    price_min=lower,
                    price_max=upper,
                    outcome=outcome,
                    consecutive_trades=count,
                ))
            for seconds in (2, 5):
                result.append(StressCase(
                    case_id=f"confirm_seconds__{price_label}__{outcome}__{seconds}",
                    price_min=lower,
                    price_max=upper,
                    outcome=outcome,
                    hold_seconds=seconds,
                ))
            for seconds in (1, 2, 5):
                result.append(StressCase(
                    case_id=f"delay__{price_label}__{outcome}__{seconds}",
                    price_min=lower,
                    price_max=upper,
                    outcome=outcome,
                    delay_seconds=seconds,
                ))
            for size in (Decimal("10"), Decimal("50"), Decimal("100")):
                result.append(StressCase(
                    case_id=f"size_proxy__{price_label}__{outcome}__{size}",
                    price_min=lower,
                    price_max=upper,
                    outcome=outcome,
                    min_trade_size=size,
                ))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Run candidate stress-test matrix")
    parser.add_argument(
        "--dataset",
        action="append",
        required=True,
        help="Named NDJSON dataset in NAME=PATH form; repeatable",
    )
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument(
        "--chronological-split",
        type=float,
        help="Also evaluate disjoint older/newer subsets using this older fraction.",
    )
    parser.add_argument(
        "--market-reference",
        type=Path,
        help="Restrict every dataset to market IDs defined in this NDJSON file.",
    )
    parser.add_argument(
        "--chronological-blocks",
        type=int,
        help="Also evaluate this many disjoint chronological market blocks.",
    )
    args = parser.parse_args()

    datasets: list[tuple[str, Path]] = []
    for value in args.dataset:
        if "=" not in value:
            parser.error("--dataset must be NAME=PATH")
        name, raw_path = value.split("=", 1)
        datasets.append((name, Path(raw_path)))

    test_cases = cases()
    rows: list[dict[str, object]] = []
    reference_ids = (
        set(load_trade_series(args.market_reference)) if args.market_reference is not None else None
    )
    for name, path in datasets:
        markets = load_trade_series(path)
        if reference_ids is not None:
            markets = {
                market_id: market
                for market_id, market in markets.items()
                if market_id in reference_ids
            }
        groups = [(name, markets)]
        if args.chronological_split is not None:
            if not 0 < args.chronological_split < 1:
                parser.error("--chronological-split must be between 0 and 1")
            ordered = sorted(markets.values(), key=lambda market: (market.end_us, market.market_id))
            split_index = int(len(ordered) * args.chronological_split)
            older = {market.market_id: market for market in ordered[:split_index]}
            newer = {market.market_id: market for market in ordered[split_index:]}
            groups.extend(((f"{name}__older", older), (f"{name}__newer", newer)))
        if args.chronological_blocks is not None:
            if args.chronological_blocks < 2:
                parser.error("--chronological-blocks must be at least 2")
            ordered = sorted(markets.values(), key=lambda market: (market.end_us, market.market_id))
            for block_index in range(args.chronological_blocks):
                start = len(ordered) * block_index // args.chronological_blocks
                end = len(ordered) * (block_index + 1) // args.chronological_blocks
                block = {market.market_id: market for market in ordered[start:end]}
                groups.append((f"{name}__block_{block_index + 1:02d}", block))

        for group_name, group_markets in groups:
            print(f"{group_name}: {len(group_markets)} markets", flush=True)
            for index, case in enumerate(test_cases, start=1):
                row = run_stress_case(group_markets, case)
                row["dataset"] = group_name
                rows.append(row)
                if index % 16 == 0:
                    print(f"  {index}/{len(test_cases)} cases", flush=True)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    csv_path = args.out.with_suffix(".csv")
    json_path = args.out.with_suffix(".json")
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    json_path.write_text(
        json.dumps({"schemaVersion": 1, "rows": rows}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"CSV: {csv_path}")
    print(f"JSON: {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
