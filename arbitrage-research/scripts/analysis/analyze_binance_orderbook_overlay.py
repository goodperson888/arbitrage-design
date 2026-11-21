#!/usr/bin/env python3
"""T059: align Binance order-book features to mature Polymarket entries.

Only fields present in the recorded cross-market snapshots are used.  The
script evaluates sign/threshold overlays and never uses data after entry.
"""

from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import defaultdict
import gzip
import json
from pathlib import Path
from statistics import mean
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = ROOT / "outputs/research_tests/t055_mature_reversal_overlay_20260901/mature_reversal_overlay_results.json"
DEFAULT_CROSS = ROOT / "data/cross_market_validation/features"
DEFAULT_OUT = ROOT / "outputs/research_tests/t059_binance_orderbook_20260902"


def rows_from(paths: Iterable[Path]) -> Iterable[dict[str, Any]]:
    for path in sorted(paths):
        try:
            with gzip.open(path, "rt", encoding="utf-8") if path.suffix == ".gz" else path.open("rt", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        value = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if isinstance(value, dict):
                        yield value
        except (OSError, EOFError, gzip.BadGzipFile):
            continue


def num(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None


def stats(items: list[dict[str, Any]]) -> dict[str, Any]:
    wins = sum(bool(item["won"]) for item in items)
    pnls = [float(item["pnlPerShare"]) for item in items]
    return {"trades": len(items), "wins": wins, "losses": len(items) - wins,
            "winRate": wins / len(items) if items else None,
            "avgPnlPerShare": mean(pnls) if pnls else None}


def same_direction(value: float | None, outcome: str, threshold: float = 0.0) -> bool:
    if value is None or abs(value) < threshold:
        return False
    return value > 0 if outcome == "UP" else value < 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--cross-dir", type=Path, default=DEFAULT_CROSS)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    source = json.loads(args.input.read_text(encoding="utf-8"))
    mature = [dict(row) for row in source.get("rows", [])]
    market_ids = {str(row.get("marketId")) for row in mature}
    dates = {str(row.get("localTime", ""))[:10] for row in mature}
    feature_paths = [path for date in dates for path in (args.cross_dir / f"date={date}").glob("hour=*/part-*.ndjson*")]
    features: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for event in rows_from(feature_paths):
        market_id = str(event.get("marketId") or "")
        if market_id not in market_ids:
            continue
        ts = int(event.get("sourceTimeUs") or event.get("receivedTimeUs") or 0)
        if ts <= 0:
            continue
        binance = ((event.get("features") or {}).get("binance") or {})
        if not binance:
            continue
        features[market_id].append((ts, binance))
    for values in features.values():
        values.sort(key=lambda item: item[0])

    aligned: list[dict[str, Any]] = []
    skipped_age = 0
    for row in mature:
        values = features.get(str(row.get("marketId")), [])
        times = [item[0] for item in values]
        pos = bisect_right(times, int(row["executionUs"])) - 1
        if pos < 0:
            continue
        ts, book = values[pos]
        age_ms = (int(row["executionUs"]) - ts) / 1000
        if age_ms < 0 or age_ms > 5000:
            skipped_age += 1
            continue
        bid, ask = num(book.get("bestBid")), num(book.get("bestAsk"))
        bid_size, ask_size = num(book.get("bestBidSize")), num(book.get("bestAskSize"))
        micro_edge = None
        if bid is not None and ask is not None and bid_size is not None and ask_size is not None and bid_size + ask_size > 0:
            micro = (ask * bid_size + bid * ask_size) / (bid_size + ask_size)
            mid = (bid + ask) / 2
            micro_edge = (micro - mid) / mid if mid else None
        item = dict(row)
        item["binanceAgeMs"] = age_ms
        item["bookImbalance"] = num(book.get("bookImbalance"))
        item["micropriceEdge"] = micro_edge
        item["return1s"] = num(book.get("return1s"))
        item["return3s"] = num(book.get("return3s"))
        item["return5s"] = num(book.get("return5s"))
        item["tradeImbalance60s"] = num(book.get("tradeImbalance60s"))
        item["takerBuyRatio60s"] = num(book.get("takerBuyRatio60s"))
        item["sourceTimeUs"] = ts
        aligned.append(item)

    factors = [
        ("bookImbalance同向", lambda r: same_direction(r.get("bookImbalance"), str(r["outcome"]))),
        ("bookImbalance同向≥0.20", lambda r: same_direction(r.get("bookImbalance"), str(r["outcome"]), 0.20)),
        ("bookImbalance同向≥0.50", lambda r: same_direction(r.get("bookImbalance"), str(r["outcome"]), 0.50)),
        ("micropriceEdge同向", lambda r: same_direction(r.get("micropriceEdge"), str(r["outcome"]))),
        ("return1s同向", lambda r: same_direction(r.get("return1s"), str(r["outcome"]))),
        ("return3s同向", lambda r: same_direction(r.get("return3s"), str(r["outcome"]))),
        ("return5s同向", lambda r: same_direction(r.get("return5s"), str(r["outcome"]))),
        ("tradeImbalance60s同向", lambda r: same_direction(r.get("tradeImbalance60s"), str(r["outcome"]))),
        ("book+trade同向", lambda r: same_direction(r.get("bookImbalance"), str(r["outcome"])) and same_direction(r.get("tradeImbalance60s"), str(r["outcome"]))),
    ]
    results = []
    for outcome in ("UP", "DOWN"):
        base = [row for row in aligned if str(row["outcome"]) == outcome]
        for name, predicate in factors:
            selected = [row for row in base if predicate(row)]
            results.append({"outcome": outcome, "factor": name, **stats(selected), "retainedRate": len(selected) / len(base) if base else None, "baseline": stats(base)})

    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    payload = {"schemaVersion": 1, "experiment": "T059_binance_orderbook_overlay", "source": str(args.input),
               "sourceRows": len(mature), "alignedRows": len(aligned), "skippedAgeRows": skipped_age,
               "featureDates": sorted(dates), "maxFeatureAgeMs": 5000, "results": results,
               "note": "盘口字段来自当时已记录的Binance特征快照；所有条件只使用入场时刻及以前数据。"}
    (out / "binance_orderbook_overlay_results.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def pct(value: float | None) -> str:
        return "—" if value is None else f"{value:.1%}"
    def pnl(value: float | None) -> str:
        return "—" if value is None else f"{value:+.4f}"
    lines = ["# T059 Binance实时盘口因子叠加验证", "", "## 口径", "",
             f"- 输入为T055成熟策略{len(mature)}笔；按市场和入场时间匹配Binance特征，成功对齐{len(aligned)}笔，入场前快照最大年龄5秒。",
             "- 当前采集字段包含：买卖一价/数量、盘口不平衡、1/3/5秒收益、60秒主动成交不平衡；由一档价格和数量计算微观价格偏移。",
             "- UP/DOWN分开；所有因子只使用入场时刻及以前数据；收益沿用成熟策略逐笔扣费后净收益。", "",
             "## 结果", "", "| 方向 | 因子 | 笔数 | 胜率 | 平均净收益/份 | 保留率 | 基线胜率 | 基线收益/份 |", "|---|---|---:|---:|---:|---:|---:|---:|"]
    for row in results:
        lines.append(f"| {row['outcome']} | {row['factor']} | {row['trades']} | {pct(row['winRate'])} | {pnl(row['avgPnlPerShare'])} | {pct(row['retainedRate'])} | {pct(row['baseline']['winRate'])} | {pnl(row['baseline']['avgPnlPerShare'])} |")
    lines += ["", "## 解读", "",
              "- 盘口不平衡和微观价格是短期压力指标，不代表最终结算方向；盘口可以撤单，必须结合主动成交和价格距离。",
              "- 1/3/5秒收益是已有特征快照字段，不等同于逐笔撮合级速度；当前没有独立的连续稳定500ms/1秒字段。",
              "- 盘口因子只在样本外、跨日期和扣除真实滑点后仍优于成熟基线时，才考虑作为软过滤；本轮不改策略。", "",
              "## 复现", "", "```bash", "PYTHONDONTWRITEBYTECODE=1 python3 scripts/analysis/analyze_binance_orderbook_overlay.py", "```", ""]
    (out / "T059_Binance实时盘口因子叠加验证.md").write_text("\n".join(lines), encoding="utf-8")
    print(out / "T059_Binance实时盘口因子叠加验证.md")
    print(f"features={sum(len(v) for v in features.values())} aligned={len(aligned)} skipped_age={skipped_age}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
