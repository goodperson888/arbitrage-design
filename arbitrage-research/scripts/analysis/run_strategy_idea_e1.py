#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import defaultdict
import csv
from dataclasses import dataclass
from decimal import Decimal
from datetime import datetime
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

from arb_research.experiments.strategy_idea_validation import (
    select_recovery_entry,
    select_target_exit,
)
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
    "90_60": (60, 90),
    "60_30": (30, 60),
}
BANDS = {
    "20_30": (Decimal("0.20"), Decimal("0.30")),
    "30_40": (Decimal("0.30"), Decimal("0.40")),
    "40_50": (Decimal("0.40"), Decimal("0.50")),
}
RECOVERIES = (0.0, 0.01, 0.02)
TARGETS = (0.50, 0.60, 0.70)
DELAYS_US = (0, 1_000_000, 3_000_000)
FALLBACKS = ("resolution", "close_5s")
DETAIL_FIELDS = (
    "split", "marketId", "marketStartUs", "winner", "outcome", "window", "band",
    "recovery", "entryUs", "entryRemaining", "entryVwap", "entryFee", "target",
    "delaySeconds", "fallback", "targetReached", "exitMode", "exitUs", "exitVwap",
    "exitFee", "completeExit", "wonAtResolution", "pnlPerShare", "holdPnlPerShare",
)


@dataclass
class Summary:
    entries: int = 0
    target_hits: int = 0
    complete_exits: int = 0
    positive: int = 0
    wins: int = 0
    pnl_sum: float = 0.0
    hold_pnl_sum: float = 0.0

    def add(self, row: dict[str, Any]) -> None:
        self.entries += 1
        self.target_hits += int(row["targetReached"])
        self.complete_exits += int(row["completeExit"])
        self.positive += int(float(row["pnlPerShare"]) > 0)
        self.wins += int(row["wonAtResolution"])
        self.pnl_sum += float(row["pnlPerShare"])
        self.hold_pnl_sum += float(row["holdPnlPerShare"])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="执行E1低价入场、目标价退出回测")
    parser.add_argument(
        "--cache", type=Path,
        default=ROOT / "data/derived/live_strategy_cache.sqlite3",
    )
    parser.add_argument(
        "--out", type=Path,
        default=ROOT / "outputs/research_tests/e1_low_price_target_exit_20260826",
    )
    parser.add_argument("--train-fraction", type=float, default=0.7)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    detail_path = args.out / "e1_simulations.csv.gz"
    result_path = args.out / "e1_results.json"
    report_path = args.out / "E1_低价入场目标退出回测.md"

    connection = sqlite3.connect(args.cache)
    connection.row_factory = sqlite3.Row
    markets = list(connection.execute(
        """SELECT market_id,start_us,end_us,winner FROM markets
           WHERE winner IN ('UP','DOWN') AND start_us IS NOT NULL AND end_us IS NOT NULL
           ORDER BY start_us"""
    ))
    if len(markets) < 2:
        raise RuntimeError("已结算市场不足")
    split_index = min(len(markets) - 1, max(1, int(len(markets) * args.train_fraction)))
    validation_ids = {str(row["market_id"]) for row in markets[split_index:]}
    summaries: defaultdict[tuple[Any, ...], Summary] = defaultdict(Summary)
    daily_summaries: defaultdict[tuple[Any, ...], Summary] = defaultdict(Summary)
    base_entries: defaultdict[str, int] = defaultdict(int)

    with gzip.open(detail_path, "wt", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=DETAIL_FIELDS)
        writer.writeheader()
        for index, market in enumerate(markets, 1):
            market_id = str(market["market_id"])
            snapshots = load_market_snapshots(connection, market_id)
            if not snapshots:
                continue
            entry_points, sell_points = precompute_points(
                snapshots, int(market["end_us"])
            )
            split = "validation" if market_id in validation_ids else "train"
            for outcome in ("UP", "DOWN"):
                for window, (minimum, maximum) in WINDOWS.items():
                    for band, (low, high) in BANDS.items():
                        candidates = [
                            point for point in entry_points[outcome]
                            if minimum <= point["remaining"] <= maximum
                            and float(low) <= point["vwap"] <= float(high)
                        ]
                        for recovery in RECOVERIES:
                            entry = select_recovery_entry(candidates, recovery=recovery)
                            if entry is None:
                                continue
                            base_entries[f"{split}:{outcome}:{window}:{band}:{recovery:.2f}"] += 1
                            path = [
                                point for point in sell_points[outcome]
                                if point["sourceUs"] > entry["sourceUs"]
                            ]
                            won = outcome == str(market["winner"])
                            hold_pnl = (1.0 if won else 0.0) - entry["vwap"] - entry["fee"]
                            close_exit = close_fallback(path)
                            for target in TARGETS:
                                immediate = select_target_exit(path, target=target, delay_us=0)
                                target_reached = immediate is not None
                                for delay_us in DELAYS_US:
                                    delayed = select_target_exit(
                                        path, target=target, delay_us=delay_us
                                    )
                                    for fallback in FALLBACKS:
                                        selected = delayed
                                        exit_mode = "TARGET"
                                        if selected is None and fallback == "close_5s":
                                            selected = close_exit
                                            exit_mode = "CLOSE_5S" if selected else "NO_EXIT"
                                        elif selected is None:
                                            exit_mode = "RESOLUTION"
                                        if selected is None and fallback == "close_5s":
                                            continue
                                        if selected is None:
                                            pnl = hold_pnl
                                            complete_exit = True
                                        else:
                                            pnl = (
                                                selected["vwap"] - entry["vwap"]
                                                - entry["fee"] - selected["fee"]
                                            )
                                            complete_exit = True
                                        row = {
                                            "split": split,
                                            "marketId": market_id,
                                            "marketStartUs": int(market["start_us"]),
                                            "winner": str(market["winner"]),
                                            "outcome": outcome,
                                            "window": window,
                                            "band": band,
                                            "recovery": recovery,
                                            "entryUs": entry["sourceUs"],
                                            "entryRemaining": entry["remaining"],
                                            "entryVwap": entry["vwap"],
                                            "entryFee": entry["fee"],
                                            "target": target,
                                            "delaySeconds": delay_us / 1_000_000,
                                            "fallback": fallback,
                                            "targetReached": target_reached,
                                            "exitMode": exit_mode,
                                            "exitUs": selected["sourceUs"] if selected else "",
                                            "exitVwap": selected["vwap"] if selected else "",
                                            "exitFee": selected["fee"] if selected else "",
                                            "completeExit": complete_exit,
                                            "wonAtResolution": won,
                                            "pnlPerShare": pnl,
                                            "holdPnlPerShare": hold_pnl,
                                        }
                                        writer.writerow(row)
                                        for scope in ("all", split):
                                            key = (
                                                scope, outcome, window, band, recovery,
                                                target, delay_us / 1_000_000, fallback,
                                            )
                                            summaries[key].add(row)
                                        if split == "validation":
                                            beijing_date = datetime.fromtimestamp(
                                                int(market["start_us"]) / 1_000_000,
                                                tz=ZoneInfo("Asia/Shanghai"),
                                            ).strftime("%Y-%m-%d")
                                            daily_key = (
                                                beijing_date, outcome, window, band, recovery,
                                                target, delay_us / 1_000_000, fallback,
                                            )
                                            daily_summaries[daily_key].add(row)
            if index % 100 == 0:
                print(f"processed={index}/{len(markets)}")
    connection.close()

    summary_rows = [
        summary_to_row(key, value) for key, value in summaries.items()
    ]
    summary_rows.sort(key=lambda row: (
        row["scope"], row["outcome"], row["window"], row["band"],
        row["recovery"], row["target"], row["delaySeconds"], row["fallback"],
    ))
    daily_rows = [
        {"beijingDate": key[0], **summary_to_row(("validation", *key[1:]), value)}
        for key, value in daily_summaries.items()
    ]
    daily_rows.sort(key=lambda row: (
        row["beijingDate"], row["outcome"], row["window"], row["band"],
        row["recovery"], row["target"], row["delaySeconds"], row["fallback"],
    ))
    result = {
        "schemaVersion": 1,
        "experiment": "E1_低价入场目标退出",
        "settledMarkets": len(markets),
        "trainMarkets": split_index,
        "validationMarkets": len(markets) - split_index,
        "trainMaxStartUs": int(markets[split_index - 1]["start_us"]),
        "validationMinStartUs": int(markets[split_index]["start_us"]),
        "quantity": float(QTY),
        "windows": WINDOWS,
        "bands": {key: [float(low), float(high)] for key, (low, high) in BANDS.items()},
        "recoveries": RECOVERIES,
        "targets": TARGETS,
        "delaysSeconds": [value / 1_000_000 for value in DELAYS_US],
        "fallbacks": FALLBACKS,
        "baseEntryCounts": dict(base_entries),
        "summaries": summary_rows,
        "dailyValidationSummaries": daily_rows,
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
    entries: dict[str, list[dict[str, Any]]] = {"UP": [], "DOWN": []}
    sells: dict[str, list[dict[str, Any]]] = {"UP": [], "DOWN": []}
    for snapshot in snapshots:
        remaining = (end_us - snapshot["sourceUs"]) / 1_000_000.0
        if not 0 < remaining <= 240:
            continue
        for outcome in ("UP", "DOWN"):
            buy = consume(
                snapshot["asks"].get(outcome) or [], QTY,
                cap=Decimal("0.50"),
            )
            if buy is not None:
                entries[outcome].append({
                    "sourceUs": snapshot["sourceUs"], "remaining": remaining,
                    "vwap": float(buy[0]), "fee": float(buy[1]),
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
    return entries, sells


def close_fallback(path: list[dict[str, Any]]) -> dict[str, Any] | None:
    for point in path:
        if point["remaining"] <= 5:
            return point
    return path[-1] if path else None


def summary_to_row(key: tuple[Any, ...], summary: Summary) -> dict[str, Any]:
    scope, outcome, window, band, recovery, target, delay_seconds, fallback = key
    return {
        "scope": scope,
        "outcome": outcome,
        "window": window,
        "band": band,
        "recovery": recovery,
        "target": target,
        "delaySeconds": delay_seconds,
        "fallback": fallback,
        "entries": summary.entries,
        "targetHits": summary.target_hits,
        "targetHitRate": summary.target_hits / summary.entries,
        "completeExits": summary.complete_exits,
        "completeExitRate": summary.complete_exits / summary.entries,
        "positive": summary.positive,
        "positiveRate": summary.positive / summary.entries,
        "resolutionWins": summary.wins,
        "resolutionWinRate": summary.wins / summary.entries,
        "averageNetPnlPerShare": summary.pnl_sum / summary.entries,
        "totalNetPnlPerShare": summary.pnl_sum,
        "averageHoldPnlPerShare": summary.hold_pnl_sum / summary.entries,
    }


def write_report(path: Path, result: dict[str, Any]) -> None:
    validation = [
        row for row in result["summaries"]
        if row["scope"] == "validation" and row["entries"] >= 30
    ]
    baseline = [
        row for row in validation
        if row["recovery"] == 0.0 and row["delaySeconds"] == 0.0
        and row["fallback"] == "resolution"
    ]
    best = sorted(
        baseline, key=lambda row: row["averageNetPnlPerShare"], reverse=True
    )[:20]
    recovery_compare = [
        row for row in validation
        if row["target"] == 0.60 and row["delaySeconds"] == 0.0
        and row["fallback"] == "resolution"
    ]
    recovery_best = sorted(
        recovery_compare,
        key=lambda row: row["averageNetPnlPerShare"], reverse=True,
    )[:20]
    lines = [
        "# E1：低价入场、目标价退出回测", "",
        "## 固定口径", "",
        f"- 数据：{result['settledMarkets']}个已结算BTC 5分钟市场；前{result['trainMarkets']}个为探索段，后{result['validationMarkets']}个为时间外验证段。",
        "- 入场：UP/DOWN分开；剩余240–30秒；10份逐档VWAP在0.20–0.30、0.30–0.40或0.40–0.50。",
        "- 恢复确认：不等待、从触达后的运行低点反弹0.01、反弹0.02。",
        "- 退出：实际可完整卖出10份的VWAP首次达到0.50/0.60/0.70；比较立即、延迟1秒、延迟3秒。",
        "- 未触达目标：分别持有到官方结算，或在剩余约5秒按真实可卖盘口退出。买卖两端均计手续费。",
        "- E0未优于Polymarket盘口，因此本轮没有把E0概率作为筛选条件。", "",
        "## 时间外验证段表现最好的基础组合", "",
        "仅列至少30笔；按平均净收益排序，不代表已经通过样本外复验。", "",
        "| 时间窗 | 入场VWAP | 方向 | 目标 | 笔数 | 目标触达 | 结算胜率 | 正收益率 | 平均净收益/份 | 持有到结算/份 |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in best:
        lines.append(
            f"| {row['window'].replace('_','–')}秒 | 0.{row['band'][:2]}–0.{row['band'][3:]} | "
            f"{row['outcome']} | {row['target']:.2f} | {row['entries']} | {row['targetHitRate']:.1%} | "
            f"{row['resolutionWinRate']:.1%} | {row['positiveRate']:.1%} | "
            f"{row['averageNetPnlPerShare']:+.4f} | {row['averageHoldPnlPerShare']:+.4f} |"
        )
    lines.extend([
        "", "## 0.60目标下的恢复确认候选", "",
        "| 时间窗 | 入场VWAP | 方向 | 恢复 | 笔数 | 目标触达 | 正收益率 | 平均净收益/份 |",
        "|---|---|---|---:|---:|---:|---:|---:|",
    ])
    for row in recovery_best:
        lines.append(
            f"| {row['window'].replace('_','–')}秒 | 0.{row['band'][:2]}–0.{row['band'][3:]} | "
            f"{row['outcome']} | {row['recovery']:.2f} | {row['entries']} | "
            f"{row['targetHitRate']:.1%} | {row['positiveRate']:.1%} | "
            f"{row['averageNetPnlPerShare']:+.4f} |"
        )
    positive = [row for row in baseline if row["averageNetPnlPerShare"] > 0]
    stable = [row for row in positive if row["entries"] >= 100]
    lines.extend(["", "## 结论", ""])
    if stable:
        top = max(stable, key=lambda row: row["averageNetPnlPerShare"])
        lines.append(
            f"时间外验证中有{len(stable)}个至少100笔且为正的基础组合；其中最好的是"
            f"{top['outcome']}、{top['window'].replace('_','–')}秒、0.{top['band'][:2]}–0.{top['band'][3:]}、目标{top['target']:.2f}，"
            f"{top['entries']}笔，平均{top['averageNetPnlPerShare']:+.4f}/份。仍需检查跨日期一致性后再进入策略候选。"
        )
        daily = [
            row for row in result["dailyValidationSummaries"]
            if all(row[key] == top[key] for key in (
                "outcome", "window", "band", "recovery", "target",
                "delaySeconds", "fallback",
            ))
        ]
        lines.extend([
            "", "### 当前最佳大样本候选的北京时间逐日结果", "",
            "| 日期 | 笔数 | 目标触达 | 正收益率 | 平均净收益/份 |",
            "|---|---:|---:|---:|---:|",
        ])
        for row in daily:
            lines.append(
                f"| {row['beijingDate']} | {row['entries']} | {row['targetHitRate']:.1%} | "
                f"{row['positiveRate']:.1%} | {row['averageNetPnlPerShare']:+.4f} |"
            )
        if len(daily) < 2 or any(row["averageNetPnlPerShare"] <= 0 for row in daily):
            lines.append("\n该候选尚未在至少两个北京时间自然日同时保持正收益，因此E1仍不判定通过。")
        else:
            lines.append("\n该候选已跨至少两个自然日为正，可进入下一轮固定参数样本外观察，但仍不自动修改实盘。")
    elif positive:
        lines.append(
            f"验证段有{len(positive)}个正收益组合，但都不足100笔；E1只能保留为小样本候选，不能升级实盘条件。"
        )
    else:
        lines.append("验证段没有至少30笔且扣除双边手续费后为正的基础组合，E1暂不通过。")
    lines.extend([
        "", "## 限制", "",
        "- 只模拟10份全部成交；没有模拟挂单排队和部分成交。",
        "- 目标触发后延迟版本按届时真实盘口成交，因此可能低于目标价。",
        "- 参数网格属于探索；报告重点看后30%时间段，不能从全样本最佳值反推实盘。",
        "- 逐笔模拟保存在压缩CSV，汇总保存在JSON。", "",
        "## 复现命令", "", "```bash",
        "PYTHONPATH=src python3 scripts/analysis/run_strategy_idea_e1.py", "```", "",
    ])
    path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
