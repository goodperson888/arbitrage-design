#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import defaultdict
import csv
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import gzip
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from arb_research.experiments.orderbook_depth import BookLevel, consume_asks
from arb_research.experiments.strategy_idea_validation import select_stable_entry
from scripts.analysis.analyze_intramarket_exits import (
    QTY,
    complement_bids,
    consume,
    load_market_snapshots,
)


WINDOWS = {
    "240_180": (180, 240),
    "180_120": (120, 180),
    "120_90": (90, 120),
    "90_30": (30, 90),
}
BANDS = {
    "80_85": (0.80, 0.85),
    "85_90": (0.85, 0.90),
}
STABILITY_SECONDS = (0, 10, 30, 60)
WORST_PRICE_CAPS = (0.85, 0.90)


@dataclass
class Summary:
    entries: int = 0
    wins: int = 0
    pnl_sum: float = 0.0
    entry_sum: float = 0.0
    drawdown03: int = 0
    drawdown05: int = 0
    drawdown08: int = 0
    mae_sum: float = 0.0

    def add(self, row: dict[str, Any]) -> None:
        self.entries += 1
        self.wins += int(row["won"])
        self.pnl_sum += float(row["pnlPerShare"])
        self.entry_sum += float(row["entryVwap"])
        self.drawdown03 += int(float(row["maeAfterEntry"]) <= -0.03)
        self.drawdown05 += int(float(row["maeAfterEntry"]) <= -0.05)
        self.drawdown08 += int(float(row["maeAfterEntry"]) <= -0.08)
        self.mae_sum += float(row["maeAfterEntry"])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="执行E2早期稳定提前入场回测")
    parser.add_argument(
        "--cache", type=Path,
        default=ROOT / "data/derived/live_strategy_cache.sqlite3",
    )
    parser.add_argument(
        "--out", type=Path,
        default=ROOT / "outputs/research_tests/e2_early_stability_20260826",
    )
    parser.add_argument("--train-fraction", type=float, default=0.7)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    detail_path = args.out / "e2_entries.csv.gz"
    result_path = args.out / "e2_results.json"
    report_path = args.out / "E2_早期稳定提前入场回测.md"
    connection = sqlite3.connect(args.cache)
    connection.row_factory = sqlite3.Row
    markets = list(connection.execute(
        """SELECT market_id,start_us,end_us,winner FROM markets
           WHERE winner IN ('UP','DOWN') AND start_us IS NOT NULL AND end_us IS NOT NULL
           ORDER BY start_us"""
    ))
    split_index = min(len(markets) - 1, max(1, int(len(markets) * args.train_fraction)))
    validation_ids = {str(row["market_id"]) for row in markets[split_index:]}
    summaries: defaultdict[tuple[Any, ...], Summary] = defaultdict(Summary)
    daily: defaultdict[tuple[Any, ...], Summary] = defaultdict(Summary)
    fields = (
        "split", "marketId", "marketStartUs", "beijingDate", "winner", "outcome",
        "window", "band", "stabilitySeconds", "worstPriceCap", "entryUs",
        "entryRemaining", "entryVwap", "entryWorstPrice", "entryFee", "won",
        "pnlPerShare", "maeAfterEntry",
    )
    with gzip.open(detail_path, "wt", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for index, market in enumerate(markets, 1):
            market_id = str(market["market_id"])
            snapshots = load_market_snapshots(connection, market_id)
            points, sells = precompute_points(snapshots, int(market["end_us"]))
            if not points["UP"] and not points["DOWN"]:
                continue
            split = "validation" if market_id in validation_ids else "train"
            beijing_date = datetime.fromtimestamp(
                int(market["start_us"]) / 1_000_000,
                tz=ZoneInfo("Asia/Shanghai"),
            ).strftime("%Y-%m-%d")
            for outcome in ("UP", "DOWN"):
                for window, (minimum, maximum) in WINDOWS.items():
                    window_points = [
                        point for point in points[outcome]
                        if minimum <= point["remaining"] <= maximum
                    ]
                    for band, (low, high) in BANDS.items():
                        for stability_seconds in STABILITY_SECONDS:
                            for cap in WORST_PRICE_CAPS:
                                entry = select_stable_entry(
                                    window_points,
                                    duration_us=stability_seconds * 1_000_000,
                                    stability_floor=0.80,
                                    reset_tolerance=0.01,
                                    band_low=low,
                                    band_high=high,
                                    worst_price_cap=cap,
                                )
                                if entry is None:
                                    continue
                                future_sells = [
                                    point for point in sells[outcome]
                                    if point["sourceUs"] > entry["sourceUs"]
                                ]
                                mae = min(
                                    (point["vwap"] - entry["vwap"] for point in future_sells),
                                    default=0.0,
                                )
                                won = outcome == str(market["winner"])
                                pnl = (1.0 if won else 0.0) - entry["vwap"] - entry["fee"]
                                row = {
                                    "split": split, "marketId": market_id,
                                    "marketStartUs": int(market["start_us"]),
                                    "beijingDate": beijing_date,
                                    "winner": str(market["winner"]), "outcome": outcome,
                                    "window": window, "band": band,
                                    "stabilitySeconds": stability_seconds,
                                    "worstPriceCap": cap,
                                    "entryUs": entry["sourceUs"],
                                    "entryRemaining": entry["remaining"],
                                    "entryVwap": entry["vwap"],
                                    "entryWorstPrice": entry["worstPrice"],
                                    "entryFee": entry["fee"], "won": won,
                                    "pnlPerShare": pnl, "maeAfterEntry": mae,
                                }
                                writer.writerow(row)
                                for scope in ("all", split):
                                    summaries[(
                                        scope, outcome, window, band,
                                        stability_seconds, cap,
                                    )].add(row)
                                if split == "validation":
                                    daily[(
                                        beijing_date, outcome, window, band,
                                        stability_seconds, cap,
                                    )].add(row)
            if index % 100 == 0:
                print(f"processed={index}/{len(markets)}")
    connection.close()
    summary_rows = [summary_row(key, value) for key, value in summaries.items()]
    daily_rows = [
        {"beijingDate": key[0], **summary_row(("validation", *key[1:]), value)}
        for key, value in daily.items()
    ]
    result = {
        "schemaVersion": 1,
        "experiment": "E2_早期稳定提前入场",
        "settledMarkets": len(markets),
        "trainMarkets": split_index,
        "validationMarkets": len(markets) - split_index,
        "quantity": float(QTY),
        "windows": WINDOWS,
        "bands": BANDS,
        "stabilitySeconds": STABILITY_SECONDS,
        "worstPriceCaps": WORST_PRICE_CAPS,
        "summaries": sorted(summary_rows, key=summary_sort_key),
        "dailyValidationSummaries": sorted(
            daily_rows, key=lambda row: (row["beijingDate"], *summary_sort_key(row))
        ),
    }
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    write_report(report_path, result)
    print(result_path)
    print(detail_path)
    print(report_path)
    return 0


def precompute_points(
    snapshots: list[dict[str, Any]], end_us: int
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    points = {"UP": [], "DOWN": []}
    sells = {"UP": [], "DOWN": []}
    for snapshot in snapshots:
        remaining = (end_us - snapshot["sourceUs"]) / 1_000_000.0
        if not 0 < remaining <= 300:
            continue
        for outcome in ("UP", "DOWN"):
            levels = [
                BookLevel(Decimal(str(price)), Decimal(str(size)))
                for price, size in snapshot["asks"].get(outcome) or []
            ]
            fill = consume_asks(
                levels, QTY, Decimal("0.90"),
                fee_model="polymarket_crypto_taker", fee_rate=Decimal("0.07"),
            )
            if fill is not None:
                vwap, fee, worst = fill
                points[outcome].append({
                    "sourceUs": snapshot["sourceUs"], "remaining": remaining,
                    "vwap": float(vwap), "fee": float(fee),
                    "worstPrice": float(worst),
                })
            opposite = "DOWN" if outcome == "UP" else "UP"
            sell = consume(
                complement_bids(snapshot["asks"].get(opposite) or []),
                QTY, descending=True,
            )
            if sell is not None:
                sells[outcome].append({
                    "sourceUs": snapshot["sourceUs"], "remaining": remaining,
                    "vwap": float(sell[0]), "fee": float(sell[1]),
                })
    return points, sells


def summary_row(key: tuple[Any, ...], value: Summary) -> dict[str, Any]:
    scope, outcome, window, band, stability_seconds, cap = key
    return {
        "scope": scope, "outcome": outcome, "window": window, "band": band,
        "stabilitySeconds": stability_seconds, "worstPriceCap": cap,
        "entries": value.entries, "wins": value.wins,
        "losses": value.entries - value.wins,
        "winRate": value.wins / value.entries,
        "averageEntryVwap": value.entry_sum / value.entries,
        "averageNetPnlPerShare": value.pnl_sum / value.entries,
        "totalNetPnlPerShare": value.pnl_sum,
        "drawdown03Rate": value.drawdown03 / value.entries,
        "drawdown05Rate": value.drawdown05 / value.entries,
        "drawdown08Rate": value.drawdown08 / value.entries,
        "averageMaeAfterEntry": value.mae_sum / value.entries,
    }


def summary_sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        row["scope"], row["outcome"], row["window"], row["band"],
        row["stabilitySeconds"], row["worstPriceCap"],
    )


def write_report(path: Path, result: dict[str, Any]) -> None:
    validation = [
        row for row in result["summaries"]
        if row["scope"] == "validation" and row["entries"] >= 30
    ]
    early = [row for row in validation if row["window"] != "90_30"]
    baseline = [row for row in validation if row["window"] == "90_30"]
    best_early = sorted(
        early, key=lambda row: row["averageNetPnlPerShare"], reverse=True
    )[:20]
    lines = [
        "# E2：早期稳定到0.80以上提前入场回测", "",
        "## 固定口径", "",
        f"- 数据：{result['settledMarkets']}个已结算市场；前{result['trainMarkets']}个探索，后{result['validationMarkets']}个时间外验证。",
        "- 稳定定义：10份可成交VWAP达到0.80后，未跌破0.79；分别等待0/10/30/60秒。",
        "- 入场区间：实际10份VWAP 0.80–0.85或0.85–0.90；最坏成交价上限0.85/0.90分开。",
        "- 提前窗口：剩余240–180、180–120、120–90秒；对照为90–30秒。",
        "- 收益：持有到官方结算，计入买入taker手续费；UP/DOWN分开。",
        "- 同时记录入场后的最大不利变化是否超过0.03/0.05/0.08。", "",
        "## 时间外验证中表现最好的提前入场组合", "",
        "| 时间窗 | 方向 | VWAP | 稳定 | 最坏价 | 笔数 | 胜/负 | 胜率 | 平均净收益/份 | 回撤≥0.05 |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in best_early:
        lines.append(
            f"| {row['window'].replace('_','–')}秒 | {row['outcome']} | 0.{row['band'][:2]}–0.{row['band'][3:]} | "
            f"{row['stabilitySeconds']}秒 | {row['worstPriceCap']:.2f} | {row['entries']} | "
            f"{row['wins']}/{row['losses']} | {row['winRate']:.1%} | "
            f"{row['averageNetPnlPerShare']:+.4f} | {row['drawdown05Rate']:.1%} |"
        )
    early_positive = [
        row for row in early
        if row["entries"] >= 100 and row["averageNetPnlPerShare"] > 0
    ]
    lines.extend(["", "## 结论", ""])
    if early_positive:
        top = max(early_positive, key=lambda row: row["averageNetPnlPerShare"])
        counterpart = [
            row for row in baseline
            if all(row[key] == top[key] for key in (
                "outcome", "band", "stabilitySeconds", "worstPriceCap",
            ))
        ]
        lines.append(
            f"提前窗口中有{len(early_positive)}个至少100笔且为正的组合；最佳为"
            f"{top['outcome']}、{top['window'].replace('_','–')}秒、0.{top['band'][:2]}–0.{top['band'][3:]}、"
            f"稳定{top['stabilitySeconds']}秒，{top['entries']}笔，平均{top['averageNetPnlPerShare']:+.4f}/份。"
        )
        if counterpart:
            base = counterpart[0]
            lines.append(
                f"同参数90–30秒对照为{base['entries']}笔、{base['averageNetPnlPerShare']:+.4f}/份。"
            )
        daily = [
            row for row in result["dailyValidationSummaries"]
            if all(row[key] == top[key] for key in (
                "outcome", "window", "band", "stabilitySeconds", "worstPriceCap",
            ))
        ]
        lines.extend([
            "", "### 最佳候选逐日", "",
            "| 北京日期 | 笔数 | 胜率 | 平均净收益/份 |",
            "|---|---:|---:|---:|",
        ])
        for row in daily:
            lines.append(
                f"| {row['beijingDate']} | {row['entries']} | {row['winRate']:.1%} | {row['averageNetPnlPerShare']:+.4f} |"
            )
        if len(daily) < 2 or any(row["averageNetPnlPerShare"] <= 0 for row in daily):
            lines.append("\n逐日结果不稳定，E2不判定通过。")
        else:
            lines.append("\n候选跨至少两个自然日为正，可进入固定参数继续观察，但不自动修改实盘。")
    else:
        lines.append("没有提前窗口同时满足验证段至少100笔和正净收益，E2不通过。")
    lines.extend([
        "", "## 限制", "",
        "- 稳定计时从各测试窗口内第一次满足0.80开始，不向窗口之前追溯。",
        "- 只模拟10份完整成交；未模拟真实网络延迟和部分成交。",
        "- 最大不利变化使用后续可完整卖出10份的盘口，不是页面显示价。",
        "- 本实验不使用E0作为硬过滤，因为E0未优于盘口基线。", "",
        "## 复现命令", "", "```bash",
        "PYTHONPATH=src python3 scripts/analysis/run_strategy_idea_e2.py", "```", "",
    ])
    path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
