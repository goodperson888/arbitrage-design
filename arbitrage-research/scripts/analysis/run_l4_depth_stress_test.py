#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
from decimal import Decimal
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from arb_research.experiments.candidate_stress import load_trade_series
from arb_research.experiments.orderbook_depth import (
    DepthCase,
    has_book_coverage,
    load_book_markets,
    run_depth_case,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stress-test bounded entries against captured 10-level order books."
    )
    parser.add_argument(
        "--glob",
        default="data/markets/btc-updown-5m-*.json",
        help="Glob containing raw order-book snapshot files.",
    )
    parser.add_argument(
        "--official",
        default="data/markets/btc_5m_official_trades_combined.ndjson",
        help="Official event file used for corrected winner and end time.",
    )
    parser.add_argument(
        "--out",
        default="runs/l4_depth_stress_75_80_80_85",
        help="Output basename (without extension).",
    )
    return parser.parse_args()


def build_cases() -> list[DepthCase]:
    cases: list[DepthCase] = []
    for low, high in ((Decimal("0.75"), Decimal("0.80")), (Decimal("0.80"), Decimal("0.85"))):
        label = f"{low}-{high}"
        for outcome in ("UP", "DOWN"):
            for max_remaining, min_remaining in ((90, 60), (60, 30), (30, 0), (90, 0)):
                for path in ("any", "up_cross", "down_cross"):
                    for quantity in (Decimal("1"), Decimal("10"), Decimal("50"), Decimal("100")):
                        for delay in (0, 1, 2, 5):
                            cases.append(
                                DepthCase(
                                    case_id=(
                                        f"depth__{label}__{outcome}__{max_remaining}-{min_remaining}"
                                        f"__{path}__q{quantity}__d{delay}"
                                    ),
                                    price_min=low,
                                    price_max=high,
                                    outcome=outcome,
                                    quantity=quantity,
                                    max_remaining_seconds=max_remaining,
                                    min_remaining_seconds=min_remaining,
                                    delay_seconds=delay,
                                    path=path,
                                )
                            )
    return cases


def main() -> None:
    args = parse_args()
    paths = list(ROOT.glob(args.glob))
    if not paths:
        raise SystemExit(f"no files matched: {args.glob}")
    official = load_trade_series(ROOT / args.official)
    markets = load_book_markets(paths, official)
    snapshot_count = sum(len(market.snapshots) for market in markets.values())
    print(
        f"Scanned {len(paths)} files, loaded {len(markets)} unique markets, "
        f"{snapshot_count} deduplicated snapshots"
    )
    for max_remaining, min_remaining in ((90, 60), (60, 30), (30, 0), (90, 0)):
        for outcome in ("UP", "DOWN"):
            covered = sum(
                has_book_coverage(
                    market,
                    outcome=outcome,
                    max_remaining_seconds=max_remaining,
                    min_remaining_seconds=min_remaining,
                )
                for market in markets.values()
            )
            print(
                f"Coverage {outcome} {max_remaining}-{min_remaining}s remaining: "
                f"{covered}/{len(markets)} markets (max gap 5s)"
            )

    cases = build_cases()
    rows: list[dict[str, object]] = []
    for index, case in enumerate(cases, start=1):
        covered_markets = {
            market_id: market
            for market_id, market in markets.items()
            if has_book_coverage(
                market,
                outcome=case.outcome,
                max_remaining_seconds=case.max_remaining_seconds,
                min_remaining_seconds=case.min_remaining_seconds,
            )
        }
        row = run_depth_case(covered_markets, case)
        row["sourceMarketCount"] = len(markets)
        row["coverageQualifiedMarketCount"] = len(covered_markets)
        rows.append(row)
        if index % 128 == 0:
            print(f"  {index}/{len(cases)} cases")

    output = ROOT / args.out
    output.parent.mkdir(parents=True, exist_ok=True)
    csv_path = output.with_suffix(".csv")
    json_path = output.with_suffix(".json")
    with csv_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    json_path.write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n")
    print(f"CSV: {csv_path.relative_to(ROOT)}")
    print(f"JSON: {json_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
