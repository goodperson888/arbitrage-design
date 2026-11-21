#!/usr/bin/env python3
"""Explain losing fills for one observed-trade candidate."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from arb_research.experiments.candidate_stress import (  # noqa: E402
    StressCase,
    _find_fill,
    load_trade_series,
)
from arb_research.io import read_events  # noqa: E402


def decimal_string(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None


def iso_time(timestamp_us: int) -> str:
    return datetime.fromtimestamp(timestamp_us / 1_000_000, timezone.utc).isoformat()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--outcome", choices=("UP", "DOWN"), default="DOWN")
    parser.add_argument("--price-min", type=Decimal, default=Decimal("0.80"))
    parser.add_argument("--price-max", type=Decimal, default=Decimal("0.85"))
    parser.add_argument("--max-remaining", type=int, default=90)
    parser.add_argument("--min-remaining", type=int, default=30)
    parser.add_argument("--min-trade-size", type=Decimal, default=Decimal("10"))
    args = parser.parse_args()

    markets = load_trade_series(args.dataset)
    definitions: dict[str, dict[str, object]] = {}
    event_ids: dict[str, list[str]] = defaultdict(list)
    settlement_counts: Counter[str] = Counter()
    for event in read_events(args.dataset):
        if event.event_id:
            event_ids[event.market_id].append(event.event_id)
        if event.event_type == "MARKET_DEFINITION":
            definitions[event.market_id] = event.payload
        elif event.event_type == "SETTLEMENT":
            settlement_counts[event.market_id] += 1

    case = StressCase(
        case_id=(
            f"{args.outcome.lower()}_{args.price_min}_{args.price_max}_"
            f"size_gte_{args.min_trade_size}_remaining_{args.max_remaining}_{args.min_remaining}"
        ),
        price_min=args.price_min,
        price_max=args.price_max,
        outcome=args.outcome,
        max_remaining_seconds=args.max_remaining,
        min_remaining_seconds=args.min_remaining,
        min_trade_size=args.min_trade_size,
    )
    fills = []
    failures = []
    for market in markets.values():
        fill = _find_fill(market, case, "polymarket_crypto_taker", Decimal("0.07"))
        if fill is None:
            continue
        fills.append(fill)
        if fill.won:
            continue

        target_ticks = market.trades.get(args.outcome, [])
        opposing = "DOWN" if args.outcome == "UP" else "UP"
        opposing_ticks = market.trades.get(opposing, [])
        entry_tick = next(
            (
                tick
                for tick in target_ticks
                if tick.time_us == fill.execution_time_us and tick.price == fill.price
                and tick.size >= args.min_trade_size
            ),
            None,
        )
        after = [tick for tick in target_ticks if fill.execution_time_us <= tick.time_us <= market.end_us]
        opposing_after = [
            tick for tick in opposing_ticks if fill.execution_time_us <= tick.time_us <= market.end_us
        ]
        final_target = after[-1] if after else None
        final_opposing = opposing_after[-1] if opposing_after else None
        first_below_half = next((tick for tick in after if tick.price < Decimal("0.5")), None)
        definition = definitions.get(market.market_id, {})
        ids = event_ids.get(market.market_id, [])
        terminal_evidence = bool(
            final_target
            and final_opposing
            and market.end_us - final_target.time_us <= 30_000_000
            and market.end_us - final_opposing.time_us <= 30_000_000
            and final_target.price <= Decimal("0.15")
            and final_opposing.price >= Decimal("0.85")
        )
        failures.append({
            "marketId": market.market_id,
            "winner": market.winner,
            "entryTimeUtc": iso_time(fill.execution_time_us),
            "entryRemainingSeconds": decimal_string(fill.remaining_seconds),
            "entryPrice": decimal_string(fill.price),
            "entryObservedTradeSize": decimal_string(entry_tick.size if entry_tick else None),
            "entryFeePerShare": decimal_string(fill.fee),
            "targetPriceAfterEntry": {
                "minimum": decimal_string(min((tick.price for tick in after), default=None)),
                "maximum": decimal_string(max((tick.price for tick in after), default=None)),
                "last": decimal_string(final_target.price if final_target else None),
                "lastRemainingSeconds": decimal_string(
                    Decimal(market.end_us - final_target.time_us) / Decimal("1000000")
                    if final_target else None
                ),
            },
            "opposingLastPrice": decimal_string(final_opposing.price if final_opposing else None),
            "opposingLastRemainingSeconds": decimal_string(
                Decimal(market.end_us - final_opposing.time_us) / Decimal("1000000")
                if final_opposing else None
            ),
            "firstTargetBelowHalfRemainingSeconds": decimal_string(
                Decimal(market.end_us - first_below_half.time_us) / Decimal("1000000")
                if first_below_half else None
            ),
            "classification": (
                "confirmed_terminal_reversal" if terminal_evidence else "settlement_reversal_with_sparse_terminal_trades"
            ),
            "dataChecks": {
                "tradeHistoryComplete": definition.get("tradeHistoryComplete"),
                "settlementEventCount": settlement_counts[market.market_id],
                "eventIdCount": len(ids),
                "duplicateEventIdCount": len(ids) - len(set(ids)),
                "timestampsSecondPrecision": True,
            },
        })

    wins = sum(fill.won for fill in fills)
    result = {
        "schemaVersion": 1,
        "dataset": str(args.dataset),
        "case": {
            "outcome": args.outcome,
            "priceMin": decimal_string(args.price_min),
            "priceMax": decimal_string(args.price_max),
            "maxRemainingSeconds": args.max_remaining,
            "minRemainingSeconds": args.min_remaining,
            "minObservedTradeSize": decimal_string(args.min_trade_size),
        },
        "summary": {
            "marketCount": len(markets),
            "fillCount": len(fills),
            "winCount": wins,
            "lossCount": len(failures),
            "winRate": decimal_string(Decimal(wins) / Decimal(len(fills))) if fills else "0",
            "confirmedTerminalReversals": sum(
                item["classification"] == "confirmed_terminal_reversal" for item in failures
            ),
            "failuresWithDuplicateEventIds": sum(
                item["dataChecks"]["duplicateEventIdCount"] > 0 for item in failures
            ),
            "failuresWithIncompleteTradeHistory": sum(
                item["dataChecks"]["tradeHistoryComplete"] is not True for item in failures
            ),
        },
        "limitations": [
            "Observed trades prove that another taker traded; they do not prove our order could fill afterward.",
            "Official historical trade timestamps are second precision, so ordering within one second is uncertain.",
            "Classification uses prediction-market trades and official settlement, not tick-level BTC reference prices.",
        ],
        "failures": failures,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    print(f"Report: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
