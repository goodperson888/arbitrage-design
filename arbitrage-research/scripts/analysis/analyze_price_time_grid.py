#!/usr/bin/env python3
"""Compare price bands above 0.50 at fixed late-market time points.

The test uses only snapshots already available at or before each observation,
executes a 10-share marketable buy through the asks, and labels the result with
the official market winner.  It is an offline diagnostic; it does not change
the live paper strategy.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import csv
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
from statistics import median
import sys
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.analysis.analyze_intramarket_exits import QTY, consume, fee, load_market_snapshots


TARGET_SECONDS = (60, 50, 40, 30)
TARGET_TOLERANCE_SECONDS = Decimal("5")
PRICE_BANDS: tuple[tuple[str, Decimal, Decimal], ...] = (
    ("50_60", Decimal("0.50"), Decimal("0.60")),
    ("60_70", Decimal("0.60"), Decimal("0.70")),
    ("70_80", Decimal("0.70"), Decimal("0.80")),
    ("80_90", Decimal("0.80"), Decimal("0.90")),
    ("90_99", Decimal("0.90"), Decimal("0.99")),
)


def in_target_window(remaining: Decimal, target_seconds: int, tolerance: Decimal = TARGET_TOLERANCE_SECONDS) -> bool:
    target = Decimal(str(target_seconds))
    return target - tolerance <= remaining <= target + tolerance


def classify_price_band(price: Decimal, bands: Iterable[tuple[str, Decimal, Decimal]]) -> str | None:
    for band_id, lower, upper in bands:
        if lower <= price < upper:
            return band_id
    return None


def favorite_outcome(vwaps: dict[str, Decimal]) -> str | None:
    if set(vwaps) != {"UP", "DOWN"} or vwaps["UP"] == vwaps["DOWN"]:
        return None
    return max(vwaps, key=vwaps.get)


def buy_vwap(asks: list[list[str]], cap: Decimal = Decimal("0.99")) -> tuple[Decimal, Decimal] | None:
    return consume(asks, QTY, cap=cap)


def hold_pnl(vwap: Decimal, winner: str, outcome: str) -> Decimal:
    return (Decimal("1") if winner == outcome else Decimal("0")) - vwap - fee(vwap)


def first_entry_in_window(
    snapshots: list[dict[str, Any]],
    end_us: int,
    outcome: str,
    target_seconds: int,
) -> dict[str, Any] | None:
    for snapshot in snapshots:
        remaining = Decimal(end_us - int(snapshot["sourceUs"])) / Decimal("1000000")
        if not in_target_window(remaining, target_seconds):
            continue
        filled = buy_vwap(snapshot["asks"].get(outcome) or [])
        if filled is None:
            continue
        vwap, entry_fee = filled
        band = classify_price_band(vwap, PRICE_BANDS)
        if band is None:
            continue
        return {
            "sourceUs": int(snapshot["sourceUs"]),
            "remaining": remaining,
            "vwap": vwap,
            "fee": entry_fee,
            "band": band,
        }
    return None


def nearest_snapshot_in_window(
    snapshots: list[dict[str, Any]],
    end_us: int,
    target_seconds: int,
) -> dict[str, Any] | None:
    candidates = []
    for snapshot in snapshots:
        remaining = Decimal(end_us - int(snapshot["sourceUs"])) / Decimal("1000000")
        if in_target_window(remaining, target_seconds):
            candidates.append((abs(remaining - Decimal(str(target_seconds))), snapshot, remaining))
    if not candidates:
        return None
    _, snapshot, remaining = min(candidates, key=lambda item: item[0])
    return {"snapshot": snapshot, "remaining": remaining}


def metric_rows(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[(record["selection"], str(record["targetSeconds"]), record["outcome"], record["band"])].append(record)
    summaries = []
    for (selection, target, outcome, band), items in sorted(grouped.items()):
        pnls = [Decimal(str(item["holdPnlPerShare"])) for item in items]
        wins = sum(item["winner"] == outcome for item in items)
        summaries.append({
            "selection": selection,
            "targetSeconds": int(target),
            "outcome": outcome,
            "band": band,
            "entries": len(items),
            "wins": wins,
            "losses": len(items) - wins,
            "winRate": wins / len(items),
            "averageEntryVwap": float(sum(Decimal(str(item["entryVwap"])) for item in items) / len(items)),
            "averageHoldPnlPerShare": float(sum(pnls) / len(pnls)),
            "medianHoldPnlPerShare": float(Decimal(str(median(pnls)))),
            "totalHoldPnlPerShare": float(sum(pnls)),
        })
    return summaries


def write_markdown(path: Path, markets: int, records: list[dict[str, Any]], summaries: list[dict[str, Any]], cache: Path) -> None:
    lines = [
        "# E11：价格≥0.50与尾盘时间点入场网格验证",
        "",
        "## 测试口径",
        "",
        f"- 数据缓存：`{cache}`；已结算市场 {markets} 个。",
        f"- 目标时点：剩余 {', '.join(str(x) for x in TARGET_SECONDS)} 秒；每个目标使用 ±{TARGET_TOLERANCE_SECONDS} 秒容差，避免逐秒快照缺口。",
        "- 入场：每个市场、目标时点、方向只取第一笔可完整成交的10份逐档VWAP；买入手续费计入。",
        "- 价格带：0.50–0.60、0.60–0.70、0.70–0.80、0.80–0.90、0.90–0.99；上限不含0.99。",
        "- 方向：UP、DOWN分开；favorite为同一快照中可执行VWAP较高的一侧，仅作对照，不是独立信息源。",
        "- 结算：只使用市场结束后官方winner；收益为持有到结算的每份净收益，包含买入手续费。",
        "- 同一市场可在多个目标时点分别出现，因此表格是观察时点比较，不等同于一次实盘可重复下注。",
        "",
        "## 汇总（至少10笔）",
        "",
        "| 选择 | 剩余秒 | 方向 | 价格带 | 笔数 | 胜率 | 平均入场 | 平均净收益/份 | 总净收益/份 |",
        "|---|---:|---|---|---:|---:|---:|---:|---:|",
    ]
    for row in summaries:
        if row["entries"] < 10:
            continue
        lines.append(
            f"| {row['selection']} | {row['targetSeconds']} | {row['outcome']} | {row['band']} | "
            f"{row['entries']} | {row['winRate'] * 100:.1f}% | {row['averageEntryVwap']:.4f} | "
            f"{row['averageHoldPnlPerShare']:+.4f} | {row['totalHoldPnlPerShare']:+.2f} |"
        )
    lines += [
        "",
        "## 解释与限制",
        "",
        "- 这轮的0.60是可执行10份VWAP分层，不是单笔成交价首次触碰0.6000；严格首次触达应另做事件级测试。",
        "- favorite只是选择当时预测市场价格较高的一侧。由于UP/DOWN价格互补，单看‘哪边涨得多’不能视为额外预测因子。",
        "- 价格越高，胜率可能上升，但一次失败的损失更接近整笔成本；必须同时看平均净收益。",
        "- Chainlink E0不在本轮作为硬过滤；它应在同一入场记录上按锚定距离、模型概率和数据年龄分层比较。",
        "- 本报告为离线研究，不修改实时纸上交易策略。",
        "",
        "## 复现命令",
        "",
        "```bash",
        "PYTHONDONTWRITEBYTECODE=1 python3 scripts/analysis/analyze_price_time_grid.py",
        "```",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, default=ROOT / "data/derived/live_strategy_cache.sqlite3")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "outputs/research_tests/e11_price_time_grid_20260828")
    args = parser.parse_args()

    connection = sqlite3.connect(args.cache)
    markets = list(connection.execute("SELECT market_id,end_us,winner FROM markets WHERE winner IN ('UP','DOWN') AND end_us IS NOT NULL ORDER BY end_us"))
    records: list[dict[str, Any]] = []
    for market_id, end_us, winner in markets:
        snapshots = load_market_snapshots(connection, str(market_id))
        for target in TARGET_SECONDS:
            by_direction: dict[str, dict[str, Any]] = {}
            for outcome in ("UP", "DOWN"):
                entry = first_entry_in_window(snapshots, int(end_us), outcome, target)
                if entry is not None:
                    by_direction[outcome] = entry
                    records.append({
                        "marketId": str(market_id), "targetSeconds": target, "selection": "direction",
                        "outcome": outcome, "winner": str(winner), "entryUs": entry["sourceUs"],
                        "remainingSeconds": float(entry["remaining"]), "entryVwap": str(entry["vwap"]),
                        "entryFee": str(entry["fee"]), "band": entry["band"],
                        "holdPnlPerShare": str(hold_pnl(entry["vwap"], str(winner), outcome)),
                    })
            nearest = nearest_snapshot_in_window(snapshots, int(end_us), target)
            if nearest is None:
                continue
            snapshot = nearest["snapshot"]
            favorite_vwaps: dict[str, Decimal] = {}
            favorite_fees: dict[str, Decimal] = {}
            for outcome in ("UP", "DOWN"):
                filled = buy_vwap(snapshot["asks"].get(outcome) or [])
                if filled is not None:
                    favorite_vwaps[outcome], favorite_fees[outcome] = filled
            favorite = favorite_outcome(favorite_vwaps)
            if favorite is not None:
                vwap = favorite_vwaps[favorite]
                band = classify_price_band(vwap, PRICE_BANDS)
                if band is not None:
                    records.append({
                        "marketId": str(market_id), "targetSeconds": target, "selection": "favorite",
                        "outcome": favorite, "winner": str(winner), "entryUs": int(snapshot["sourceUs"]),
                        "remainingSeconds": float(nearest["remaining"]), "entryVwap": str(vwap),
                        "entryFee": str(favorite_fees[favorite]), "band": band,
                        "holdPnlPerShare": str(hold_pnl(vwap, str(winner), favorite)),
                    })
    connection.close()

    summaries = metric_rows(records)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / "e11_price_time_grid_results.json"
    csv_path = args.output_dir / "e11_price_time_grid_entries.csv"
    md_path = args.output_dir / "E11_价格时间网格验证.md"
    json_path.write_text(json.dumps({
        "schemaVersion": 1, "experiment": "E11_price_time_grid", "cache": str(args.cache),
        "settledMarkets": len(markets), "targetSeconds": TARGET_SECONDS,
        "targetToleranceSeconds": str(TARGET_TOLERANCE_SECONDS),
        "priceBands": [[name, str(low), str(high)] for name, low, high in PRICE_BANDS],
        "summaries": summaries, "records": records,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if records:
        with csv_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(records[0]))
            writer.writeheader()
            writer.writerows(records)
    write_markdown(md_path, len(markets), records, summaries, args.cache)
    print(f"markets={len(markets)} records={len(records)} summaries={len(summaries)}")
    print(json_path.resolve())
    print(md_path.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
