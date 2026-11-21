#!/usr/bin/env python3
"""回测剩余时间、实际VWAP、盘口稳定性和跨市场共振组合。

这是离线研究工具，不会修改实时监控，也不会下单。每个组合在每个市场
只取第一笔满足条件的主动买入，并用当时之前的盘口和跨市场快照计算。
"""

from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import defaultdict
from decimal import Decimal
import gzip
import json
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[2]
ZERO = Decimal("0")
QTY = Decimal("10")
FEE_RATE = Decimal("0.07")


def rows(paths: Iterable[Path]) -> Iterable[dict[str, Any]]:
    for path in sorted(paths):
        opener = gzip.open if path.suffix == ".gz" else open
        try:
            with opener(path, "rt", encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(value, dict):
                        yield value
        except (EOFError, gzip.BadGzipFile, OSError):
            continue


def fee(price: Decimal) -> Decimal:
    return FEE_RATE * price * (Decimal("1") - price)


def consume(levels: list[list[str]], quantity: Decimal, *, buy: bool, cap: Decimal) -> tuple[Decimal, Decimal, Decimal] | None:
    ordered = sorted(levels, key=lambda item: Decimal(item[0]), reverse=not buy)
    remaining = quantity
    notional = ZERO
    total_fee = ZERO
    worst = ZERO
    for raw_price, raw_size in ordered:
        price = Decimal(str(raw_price))
        size = Decimal(str(raw_size))
        if (buy and price > cap) or (not buy and price < cap):
            break
        take = min(remaining, size)
        if take <= 0:
            continue
        notional += take * price
        total_fee += fee(price) * take
        worst = price
        remaining -= take
        if remaining <= 0:
            return notional / quantity, total_fee / quantity, worst
    return None


def at_or_before(items: list[tuple[int, Any]], timestamp_us: int) -> Any | None:
    if not items:
        return None
    index = bisect_right([item[0] for item in items], timestamp_us) - 1
    return items[index][1] if index >= 0 else None


def stable_for_500ms(items: list[tuple[int, Any]], timestamp_us: int, outcome: str, cap: Decimal) -> bool:
    current = at_or_before(items, timestamp_us)
    previous = at_or_before(items, timestamp_us - 500_000)
    for snapshot in (previous, current):
        if snapshot is None:
            return False
        book = next((item for item in snapshot.get("outcomes", []) if item.get("outcome") == outcome), None)
        if not book or consume(book.get("asks") or [], QTY, buy=True, cap=cap) is None:
            return False
    return True


def build() -> tuple[dict[str, dict[str, Any]], dict[str, list[tuple[int, Any]]], dict[str, list[tuple[int, Any]]]]:
    events_dir = ROOT / "data" / "live_monitor" / "events"
    markets: dict[str, dict[str, Any]] = defaultdict(lambda: {"snapshots": [], "trades": [], "winner": None, "endUs": None})
    for row in rows(events_dir.glob("date=*/hour=*/part-*.ndjson*")):
        market_id = str(row.get("marketId") or "")
        if not market_id:
            continue
        event_type = row.get("eventType")
        bucket = markets[market_id]
        timestamp = int(row.get("sourceTimeUs") or row.get("receivedTimeUs") or 0)
        if event_type == "ORDERBOOK_SNAPSHOT":
            bucket["snapshots"].append((timestamp, row.get("payload") or {}))
        elif event_type == "TRADE":
            bucket["trades"].append((timestamp, row))
        elif event_type == "MARKET_DEFINITION":
            payload = row.get("payload") or {}
            bucket["endUs"] = int(payload.get("eventEndUs") or 0)
        elif event_type == "SETTLEMENT":
            bucket["winner"] = (row.get("payload") or {}).get("winnerOutcome")
            bucket["endUs"] = int(row.get("sourceTimeUs") or 0)
    for bucket in markets.values():
        bucket["snapshots"].sort(key=lambda item: item[0])
        bucket["trades"].sort(key=lambda item: item[0])

    cross_dir = ROOT / "data" / "cross_market_validation"
    cross: dict[str, list[tuple[int, Any]]] = defaultdict(list)
    for row in rows(cross_dir.joinpath("venue_snapshots").glob("date=*/hour=*/part-*.ndjson*")):
        market_id = str(row.get("marketId") or "")
        if market_id:
            cross[market_id].append((int(row.get("sourceTimeUs") or row.get("receivedTimeUs") or 0), row))
    for items in cross.values():
        items.sort(key=lambda item: item[0])
    return dict(markets), cross, {}


def evaluate(markets: dict[str, dict[str, Any]], cross: dict[str, list[tuple[int, Any]]]) -> list[dict[str, Any]]:
    groups = {
        "45_60": (45, 60),
        "5_60": (5, 60),
        "5_45": (5, 45),
    }
    price_bands = {
        "0.70_0.72": (Decimal("0.70"), Decimal("0.72")),
        "0.72_0.74": (Decimal("0.72"), Decimal("0.74")),
        "0.74_0.76": (Decimal("0.74"), Decimal("0.76")),
        "0.76_0.78": (Decimal("0.76"), Decimal("0.78")),
        "0.78_0.80": (Decimal("0.78"), Decimal("0.80")),
        "0.80_0.82": (Decimal("0.80"), Decimal("0.82")),
        "0.82_0.84": (Decimal("0.82"), Decimal("0.84")),
        "0.84_0.86": (Decimal("0.84"), Decimal("0.86")),
        "0.83_0.85": (Decimal("0.83"), Decimal("0.85")),
        "0.86_0.90": (Decimal("0.86"), Decimal("0.90")),
    }
    result: list[dict[str, Any]] = []
    for group, (min_remaining, max_remaining) in groups.items():
        for direction in ("UP", "DOWN"):
            for band, (low, high) in price_bands.items():
                for mode, (require_same, require_stable) in {
                    "base": (False, False),
                    "same_sources": (True, False),
                    "same_and_stable": (True, True),
                }.items():
                    fills: list[dict[str, Any]] = []
                    for market_id, bucket in markets.items():
                        winner = bucket.get("winner")
                        end_us = bucket.get("endUs")
                        if winner not in ("UP", "DOWN") or not end_us:
                            continue
                        if market_id not in cross:
                            cross_row = None
                        else:
                            cross_row = None
                        for timestamp, trade in bucket["trades"]:
                            remaining = (end_us - timestamp) / 1_000_000
                            if not (min_remaining <= remaining <= max_remaining):
                                continue
                            if str(trade.get("outcome")) != direction:
                                continue
                            payload = trade.get("payload") or {}
                            if str(payload.get("side", "")).upper() != "BUY":
                                continue
                            signal_price = Decimal(str(payload.get("price", "0")))
                            if signal_price < Decimal("0.70") or signal_price > Decimal("0.90"):
                                continue
                            snapshot = at_or_before(bucket["snapshots"], timestamp)
                            if not snapshot:
                                continue
                            book = next((item for item in snapshot.get("outcomes", []) if item.get("outcome") == direction), None)
                            if not book:
                                continue
                            consumed = consume(book.get("asks") or [], QTY, buy=True, cap=Decimal("0.90"))
                            if consumed is None:
                                continue
                            vwap, entry_fee, _ = consumed
                            if not (low <= vwap < high):
                                continue
                            cross_row = at_or_before(cross.get(market_id, []), timestamp)
                            signals = (cross_row or {}).get("signals") or {}
                            sources = [signals.get(name) for name in ("binance", "binanceFutures", "hyperliquid")]
                            same = all(value == direction for value in sources)
                            if require_same and not same:
                                continue
                            stable = stable_for_500ms(bucket["snapshots"], timestamp, direction, Decimal("0.90"))
                            if require_stable and not stable:
                                continue
                            if any(item["marketId"] == market_id for item in fills):
                                continue
                            hold_pnl = (QTY if winner == direction else ZERO) - QTY * (vwap + entry_fee)
                            exit_pnl = hold_pnl
                            fills.append({
                                "marketId": market_id, "direction": direction,
                                "remainingSeconds": round(remaining, 3), "vwap": str(vwap),
                                "winner": winner, "won": winner == direction,
                                "sameSources": same, "stable500ms": stable,
                                "holdNetPnl": str(hold_pnl), "exitNetPnl": str(exit_pnl),
                            })
                            break
                    wins = sum(int(item["won"]) for item in fills)
                    pnl = sum((Decimal(item["holdNetPnl"]) for item in fills), ZERO)
                    result.append({
                        "group": group, "direction": direction, "vwapBand": band,
                        "mode": mode, "requireSameSources": require_same,
                        "requireStable500ms": require_stable, "trades": len(fills),
                        "wins": wins, "losses": len(fills) - wins,
                        "winRate": wins / len(fills) if fills else None,
                        "netPnl": str(pnl), "fills": fills,
                    })
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "outputs/cross_market_validation/entry_conditions_backtest.json")
    args = parser.parse_args()
    markets, cross, _ = build()
    result = evaluate(markets, cross)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"markets": len(markets), "results": result}, ensure_ascii=False, indent=2) + "\n")
    print(f"markets={len(markets)} rows={len(result)} output={args.output}")
    for row in result:
        if row["trades"] >= 5 and (row["group"], row["vwapBand"], row["direction"]) in {
            ("45_60", "0.82_0.84", "UP"), ("45_60", "0.82_0.84", "DOWN"),
            ("5_60", "0.82_0.84", "UP"), ("5_60", "0.82_0.84", "DOWN"),
        }:
            print(row["group"], row["direction"], row["vwapBand"], row["mode"], row["trades"], row["winRate"], row["netPnl"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
