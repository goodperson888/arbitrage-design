#!/usr/bin/env python3
"""验证 Binance Spot/Futures 短时方向作为 Polymarket 入场前过滤的效果。

这是离线过滤回测，不改变实时监控。它在每个 Polymarket 主动买入前，
匹配本地已经收到的最后一份跨市场快照，并检查该快照中 Spot/Futures 的
1秒、3秒方向是否与入场方向一致。由于数据是按快照落盘，结果只能说明
“入场前已有同向外部变化时，历史样本的条件胜率”，不能证明可获得无风险
的1秒套利。
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
QTY = Decimal("10")
FEE_RATE = Decimal("0.07")


def json_rows(paths: Iterable[Path]) -> Iterable[dict[str, Any]]:
    for path in sorted(paths):
        opener = gzip.open if path.suffix == ".gz" else open
        try:
            with opener(path, "rt", encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(row, dict):
                        yield row
        except (EOFError, gzip.BadGzipFile, OSError):
            continue


def fee(price: Decimal) -> Decimal:
    return FEE_RATE * price * (Decimal("1") - price)


def consume(asks: list[list[str]], quantity: Decimal, cap: Decimal) -> tuple[Decimal, Decimal] | None:
    remaining = quantity
    notional = Decimal("0")
    total_fee = Decimal("0")
    for raw_price, raw_size in sorted(asks, key=lambda item: Decimal(str(item[0]))):
        price, size = Decimal(str(raw_price)), Decimal(str(raw_size))
        if price > cap:
            break
        take = min(remaining, size)
        if take <= 0:
            continue
        notional += take * price
        total_fee += take * fee(price)
        remaining -= take
        if remaining <= 0:
            return notional / quantity, total_fee / quantity
    return None


def at_or_before(items: list[tuple[int, Any]], timestamp_us: int) -> Any | None:
    if not items:
        return None
    pos = bisect_right([item[0] for item in items], timestamp_us) - 1
    return items[pos][1] if pos >= 0 else None


def build() -> tuple[dict[str, dict[str, Any]], dict[str, list[tuple[int, Any]]]]:
    events_dir = ROOT / "data" / "live_monitor" / "events"
    markets: dict[str, dict[str, Any]] = defaultdict(lambda: {"books": [], "trades": [], "winner": None, "endUs": None})
    for row in json_rows(events_dir.glob("date=*/hour=*/part-*.ndjson*")):
        market_id = str(row.get("marketId") or "")
        if not market_id:
            continue
        ts = int(row.get("sourceTimeUs") or row.get("receivedTimeUs") or 0)
        bucket = markets[market_id]
        if row.get("eventType") == "ORDERBOOK_SNAPSHOT":
            bucket["books"].append((ts, row.get("payload") or {}))
        elif row.get("eventType") == "TRADE":
            bucket["trades"].append((ts, row))
        elif row.get("eventType") == "MARKET_DEFINITION":
            bucket["endUs"] = int((row.get("payload") or {}).get("eventEndUs") or 0)
        elif row.get("eventType") == "SETTLEMENT":
            bucket["winner"] = (row.get("payload") or {}).get("winnerOutcome")
            bucket["endUs"] = ts
    for bucket in markets.values():
        bucket["books"].sort(key=lambda item: item[0])
        bucket["trades"].sort(key=lambda item: item[0])

    cross_dir = ROOT / "data" / "cross_market_validation" / "venue_snapshots"
    cross: dict[str, list[tuple[int, Any]]] = defaultdict(list)
    for row in json_rows(cross_dir.glob("date=*/hour=*/part-*.ndjson*")):
        market_id = str(row.get("marketId") or "")
        if market_id:
            cross[market_id].append((int(row.get("sourceTimeUs") or row.get("receivedTimeUs") or 0), row))
    for items in cross.values():
        items.sort(key=lambda item: item[0])
    return dict(markets), cross


def direction(feature: dict[str, Any], field: str, threshold: float) -> str | None:
    value = feature.get(field)
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if abs(value) < threshold:
        return None
    return "UP" if value > 0 else "DOWN"


def evaluate(markets: dict[str, dict[str, Any]], cross: dict[str, list[tuple[int, Any]]], threshold: float) -> list[dict[str, Any]]:
    groups = {"45_60": (45, 60), "5_60": (5, 60), "5_45": (5, 45)}
    modes = {
        "base": lambda ctx, outcome: True,
        "spot_1s": lambda ctx, outcome: ctx.get("spot1") == outcome,
        "futures_1s": lambda ctx, outcome: ctx.get("futures1") == outcome,
        "spot_or_futures_1s": lambda ctx, outcome: outcome in {ctx.get("spot1"), ctx.get("futures1")},
        "spot_and_futures_1s": lambda ctx, outcome: ctx.get("spot1") == outcome and ctx.get("futures1") == outcome,
        "spot_3s": lambda ctx, outcome: ctx.get("spot3") == outcome,
        "futures_3s": lambda ctx, outcome: ctx.get("futures3") == outcome,
    }
    rows: list[dict[str, Any]] = []
    for group, (min_remaining, max_remaining) in groups.items():
        for mode, predicate in modes.items():
            fills: list[dict[str, Any]] = []
            for market_id, bucket in markets.items():
                winner, end_us = bucket.get("winner"), bucket.get("endUs")
                if winner not in ("UP", "DOWN") or not end_us:
                    continue
                for timestamp, trade in bucket["trades"]:
                    remaining = (end_us - timestamp) / 1_000_000
                    if not min_remaining <= remaining <= max_remaining:
                        continue
                    outcome = str(trade.get("outcome") or "")
                    if outcome not in ("UP", "DOWN") or str((trade.get("payload") or {}).get("side", "")).upper() != "BUY":
                        continue
                    snapshot = at_or_before(bucket["books"], timestamp)
                    if not snapshot:
                        continue
                    book = next((item for item in snapshot.get("outcomes", []) if item.get("outcome") == outcome), None)
                    if not book:
                        continue
                    consumed = consume(book.get("asks") or [], QTY, Decimal("0.85"))
                    if consumed is None or not Decimal("0.80") <= consumed[0] <= Decimal("0.85"):
                        continue
                    cross_row = at_or_before(cross.get(market_id, []), timestamp) or {}
                    features = cross_row.get("features") or {}
                    spot = features.get("binance") or {}
                    futures = features.get("binanceFutures") or {}
                    context = {
                        "spot1": direction(spot, "return1s", threshold),
                        "spot3": direction(spot, "return3s", threshold),
                        "futures1": direction(futures, "return1s", threshold),
                        "futures3": direction(futures, "return3s", threshold),
                    }
                    if not predicate(context, outcome):
                        continue
                    vwap, entry_fee = consumed
                    pnl = (QTY if winner == outcome else Decimal("0")) - QTY * (vwap + entry_fee)
                    fills.append({"marketId": market_id, "outcome": outcome, "winner": winner, "won": winner == outcome,
                                  "remainingSeconds": round(remaining, 3), "vwap": str(vwap), "netPnl": str(pnl), **context})
                    # One first fill per market and direction in every mode.
                    if any(item["marketId"] == market_id and item["outcome"] == outcome for item in fills[:-1]):
                        fills.pop()
                    break
            wins = sum(int(item["won"]) for item in fills)
            pnl = sum((Decimal(item["netPnl"]) for item in fills), Decimal("0"))
            rows.append({"group": group, "mode": mode, "trades": len(fills), "wins": wins,
                         "losses": len(fills) - wins, "winRate": wins / len(fills) if fills else None,
                         "netPnl": str(pnl), "fills": fills})
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--threshold", type=float, default=0.000025, help="1秒/3秒收益绝对阈值，默认0.25bps")
    parser.add_argument("--output", type=Path, default=ROOT / "outputs/cross_market_validation/spot_futures_lead_backtest.json")
    args = parser.parse_args()
    markets, cross = build()
    results = evaluate(markets, cross, args.threshold)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"markets": len(markets), "threshold": args.threshold, "results": results}, ensure_ascii=False, indent=2) + "\n")
    print(f"markets={len(markets)} rows={len(results)} output={args.output}")
    for row in results:
        if row["trades"] >= 3:
            rate = "—" if row["winRate"] is None else f"{row['winRate'] * 100:.1f}%"
            print(f"{row['group']:>5} {row['mode']:<22} n={row['trades']:>3} wins={row['wins']:>3} rate={rate:>6} pnl={row['netPnl']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
