#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import defaultdict
import csv
from dataclasses import dataclass
from decimal import Decimal
import gzip
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from arb_research.experiments.orderbook_depth import BookLevel, consume_asks


@dataclass
class Summary:
    entries: int = 0
    wins: int = 0
    pnl_sum: float = 0.0
    original_pnl_sum: float = 0.0
    vwap_sum: float = 0.0
    worst_sum: float = 0.0

    def add(self, row: dict[str, Any]) -> None:
        self.entries += 1
        self.wins += int(row["won"])
        self.pnl_sum += float(row["pnlPerShare"])
        self.original_pnl_sum += float(row["originalPnlPerShare"])
        self.vwap_sum += float(row["entryVwap"])
        self.worst_sum += float(row["worstPrice"])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="执行E4低胜率条件严格反向回测")
    parser.add_argument(
        "--cache", type=Path,
        default=ROOT / "data/derived/live_strategy_cache.sqlite3",
    )
    parser.add_argument(
        "--e3-details", type=Path,
        default=ROOT / "outputs/research_tests/e3_high_price_reversal_20260826/e3_entries.csv.gz",
    )
    parser.add_argument(
        "--out", type=Path,
        default=ROOT / "outputs/research_tests/e4_reverse_low_win_20260826",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(args.cache)
    connection.row_factory = sqlite3.Row
    summaries: defaultdict[tuple[str, str, str], Summary] = defaultdict(Summary)
    daily: defaultdict[tuple[str, str, str], Summary] = defaultdict(Summary)
    details: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    with gzip.open(args.e3_details, "rt", encoding="utf-8") as handle:
        for source in csv.DictReader(handle):
            if source["filter"] != "none":
                continue
            unique = (source["marketId"], source["highOutcome"], source["case"])
            if unique in seen:
                continue
            seen.add(unique)
            snapshot = connection.execute(
                """SELECT asks_json,source_us FROM snapshots
                   WHERE market_id=? AND outcome=? AND source_us<=?
                   ORDER BY source_us DESC,received_us DESC LIMIT 1""",
                (source["marketId"], source["highOutcome"], int(source["signalUs"])),
            ).fetchone()
            if snapshot is None:
                continue
            levels = [
                BookLevel(Decimal(str(price)), Decimal(str(size)))
                for price, size in json.loads(snapshot["asks_json"])
            ]
            fill = consume_asks(
                levels, Decimal("10"), Decimal("1"),
                fee_model="polymarket_crypto_taker", fee_rate=Decimal("0.07"),
            )
            if fill is None:
                continue
            vwap, fee, worst = fill
            won = source["highOutcome"] == source["winner"]
            pnl = (Decimal("1") if won else Decimal("0")) - vwap - fee
            row = {
                "split": source["split"],
                "beijingDate": source["beijingDate"],
                "marketId": source["marketId"],
                "case": source["case"],
                "winner": source["winner"],
                "originalDirection": source["entryOutcome"],
                "reverseDirection": source["highOutcome"],
                "signalUs": int(source["signalUs"]),
                "remainingSeconds": float(source["remainingSeconds"]),
                "entryVwap": float(vwap),
                "feePerShare": float(fee),
                "worstPrice": float(worst),
                "won": won,
                "pnlPerShare": float(pnl),
                "originalPnlPerShare": float(source["pnlPerShare"]),
            }
            details.append(row)
            for scope in ("all", source["split"]):
                summaries[(scope, source["case"], source["highOutcome"])].add(row)
            if source["split"] == "validation":
                daily[(source["beijingDate"], source["case"], source["highOutcome"])].add(row)
    connection.close()
    summary_rows = [summary_row(key, value) for key, value in summaries.items()]
    daily_rows = [
        {"beijingDate": key[0], **summary_row(("validation", *key[1:]), value)}
        for key, value in daily.items()
    ]
    result = {
        "schemaVersion": 1,
        "experiment": "E4_低胜率条件严格反向",
        "sourceExperiment": str(args.e3_details),
        "quantity": 10,
        "uniqueSignals": len(details),
        "summaries": sorted(summary_rows, key=lambda row: (
            row["scope"], row["case"], row["reverseDirection"]
        )),
        "dailyValidationSummaries": sorted(daily_rows, key=lambda row: (
            row["beijingDate"], row["case"], row["reverseDirection"]
        )),
    }
    result_path = args.out / "e4_results.json"
    detail_path = args.out / "e4_entries.csv"
    report_path = args.out / "E4_低胜率条件反向回测.md"
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    with detail_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(details[0]) if details else [])
        if details:
            writer.writeheader()
            writer.writerows(details)
    write_report(report_path, result)
    print(result_path)
    print(detail_path)
    print(report_path)
    return 0


def summary_row(key: tuple[str, str, str], value: Summary) -> dict[str, Any]:
    scope, case, direction = key
    return {
        "scope": scope, "case": case, "reverseDirection": direction,
        "entries": value.entries, "wins": value.wins,
        "losses": value.entries - value.wins,
        "winRate": value.wins / value.entries,
        "averageEntryVwap": value.vwap_sum / value.entries,
        "averageWorstPrice": value.worst_sum / value.entries,
        "averageNetPnlPerShare": value.pnl_sum / value.entries,
        "totalNetPnlPerShare": value.pnl_sum,
        "originalAverageNetPnlPerShare": value.original_pnl_sum / value.entries,
        "improvementPerShare": (value.pnl_sum - value.original_pnl_sum) / value.entries,
    }


def write_report(path: Path, result: dict[str, Any]) -> None:
    labels = {
        "pullback_5s_01": "5秒回落≥0.01",
        "reversal_10s_02": "10秒回落≥0.02",
        "reversal_15s_03": "15秒回落≥0.03",
    }
    validation = [
        row for row in result["summaries"] if row["scope"] == "validation"
    ]
    lines = [
        "# E4：低胜率条件严格反向回测", "",
        "## 固定口径", "",
        "- 来源：E3中不加外部过滤的同一批高位快速回落信号。",
        "- E3原策略是在高位方向回落时买相反方向；E4改为在完全相同的信号时刻重新买原高位方向。",
        "- 不是用`1−原胜率`推算；重新读取该时刻原方向卖盘，按10份逐档VWAP、最坏成交价和taker手续费计算。",
        "- 持有到官方结算；UP/DOWN分开；重点看E3后30%时间外验证段。", "",
        "## 时间外验证结果", "",
        "| 原信号 | 反向后买入 | 笔数 | 胜/负 | 胜率 | 平均VWAP | 最坏价均值 | 原策略/份 | 反向后/份 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in validation:
        lines.append(
            f"| {labels[row['case']]} | {row['reverseDirection']} | {row['entries']} | "
            f"{row['wins']}/{row['losses']} | {row['winRate']:.1%} | "
            f"{row['averageEntryVwap']:.3f} | {row['averageWorstPrice']:.3f} | "
            f"{row['originalAverageNetPnlPerShare']:+.4f} | {row['averageNetPnlPerShare']:+.4f} |"
        )
    candidates = [
        row for row in validation
        if row["entries"] >= 100 and row["averageNetPnlPerShare"] > 0
    ]
    lines.extend(["", "## 结论", ""])
    if candidates:
        top = max(candidates, key=lambda row: row["averageNetPnlPerShare"])
        lines.append(
            f"有{len(candidates)}个严格反向组合达到验证段至少100笔且正收益；最佳为"
            f"{labels[top['case']]}后买{top['reverseDirection']}，{top['entries']}笔、"
            f"{top['winRate']:.1%}、平均{top['averageNetPnlPerShare']:+.4f}/份。"
        )
        daily = [
            row for row in result["dailyValidationSummaries"]
            if row["case"] == top["case"]
            and row["reverseDirection"] == top["reverseDirection"]
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
            lines.append("\n逐日结果不稳定，E4不判定通过。")
        else:
            lines.append("\n候选跨至少两个自然日为正，可进入固定参数观察，但不自动修改实盘。")
    else:
        lines.append("严格重算真实反方向成本后，没有组合同时达到至少100笔和正净收益；低胜率策略不能简单反过来。")
    lines.extend([
        "", "## 限制", "",
        "- E4只诊断E3这组已确认低胜率的条件，不代表所有低胜率策略反向都无效或有效。",
        "- 原高位方向价格通常较贵，胜率提高后仍可能因赔率和手续费亏损。",
        "- 同一市场每个反转定义、每个原高位方向只取首次信号。", "",
        "## 复现命令", "", "```bash",
        "PYTHONPATH=src python3 scripts/analysis/run_strategy_idea_e4.py", "```", "",
    ])
    path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
