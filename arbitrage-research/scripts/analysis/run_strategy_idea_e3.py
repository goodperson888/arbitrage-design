#!/usr/bin/env python3
from __future__ import annotations

import argparse
from bisect import bisect_right
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
from arb_research.experiments.strategy_idea_validation import (
    read_partition_rows,
    select_reversal_signal,
)
from scripts.analysis.analyze_intramarket_exits import QTY, load_market_snapshots


REVERSAL_CASES = {
    "pullback_5s_01": (5, 0.01),
    "reversal_10s_02": (10, 0.02),
    "reversal_15s_03": (15, 0.03),
}
FILTERS = (
    "none",
    "spot_1s", "spot_3s", "spot_5s",
    "spot_futures_1s", "spot_futures_3s", "spot_futures_5s",
)


@dataclass
class Summary:
    entries: int = 0
    wins: int = 0
    pnl_sum: float = 0.0
    entry_sum: float = 0.0
    drop_sum: float = 0.0

    def add(self, row: dict[str, Any]) -> None:
        self.entries += 1
        self.wins += int(row["won"])
        self.pnl_sum += float(row["pnlPerShare"])
        self.entry_sum += float(row["entryVwap"])
        self.drop_sum += float(row["dropFromHigh"])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="执行E3高位快速反向入场回测")
    parser.add_argument(
        "--cache", type=Path,
        default=ROOT / "data/derived/live_strategy_cache.sqlite3",
    )
    parser.add_argument(
        "--cross-dir", type=Path,
        default=ROOT / "data/cross_market_validation",
    )
    parser.add_argument(
        "--out", type=Path,
        default=ROOT / "outputs/research_tests/e3_high_price_reversal_20260826",
    )
    parser.add_argument("--train-fraction", type=float, default=0.7)
    parser.add_argument("--max-cross-age-seconds", type=float, default=3.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(args.cache)
    connection.row_factory = sqlite3.Row
    markets = list(connection.execute(
        """SELECT market_id,start_us,end_us,winner FROM markets
           WHERE winner IN ('UP','DOWN') AND start_us IS NOT NULL AND end_us IS NOT NULL
           ORDER BY start_us"""
    ))
    market_ids = {str(row["market_id"]) for row in markets}
    cross = load_cross_snapshots(args.cross_dir, market_ids)
    split_index = min(len(markets) - 1, max(1, int(len(markets) * args.train_fraction)))
    validation_ids = {str(row["market_id"]) for row in markets[split_index:]}
    summaries: defaultdict[tuple[Any, ...], Summary] = defaultdict(Summary)
    daily: defaultdict[tuple[Any, ...], Summary] = defaultdict(Summary)
    detail_path = args.out / "e3_entries.csv.gz"
    fields = (
        "split", "beijingDate", "marketId", "marketStartUs", "winner",
        "highOutcome", "entryOutcome", "case", "filter", "signalUs",
        "remainingSeconds", "highVwap", "dropFromHigh", "entryVwap",
        "entryWorstPrice", "entryFee", "crossAgeMs", "spotReturn",
        "futuresReturn", "won", "pnlPerShare",
    )
    with gzip.open(detail_path, "wt", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for index, market in enumerate(markets, 1):
            market_id = str(market["market_id"])
            snapshots = load_market_snapshots(connection, market_id)
            points = precompute_points(snapshots, int(market["end_us"]))
            split = "validation" if market_id in validation_ids else "train"
            beijing_date = datetime.fromtimestamp(
                int(market["start_us"]) / 1_000_000,
                tz=ZoneInfo("Asia/Shanghai"),
            ).strftime("%Y-%m-%d")
            for high_outcome in ("UP", "DOWN"):
                entry_outcome = "DOWN" if high_outcome == "UP" else "UP"
                for case, (horizon_seconds, drop_min) in REVERSAL_CASES.items():
                    signal = select_reversal_signal(
                        points[high_outcome],
                        horizon_us=horizon_seconds * 1_000_000,
                        drop_min=drop_min,
                        high_floor=0.80,
                    )
                    if signal is None or signal.get("oppositeVwap") is None:
                        continue
                    cross_state = latest_cross_state(
                        cross.get(market_id, []), int(signal["sourceUs"]),
                        max_age_us=int(args.max_cross_age_seconds * 1_000_000),
                    )
                    won = entry_outcome == str(market["winner"])
                    pnl = (
                        (1.0 if won else 0.0)
                        - float(signal["oppositeVwap"])
                        - float(signal["oppositeFee"])
                    )
                    for filter_name in FILTERS:
                        passed, spot_return, futures_return = passes_filter(
                            filter_name, entry_outcome, cross_state
                        )
                        if not passed:
                            continue
                        row = {
                            "split": split, "beijingDate": beijing_date,
                            "marketId": market_id,
                            "marketStartUs": int(market["start_us"]),
                            "winner": str(market["winner"]),
                            "highOutcome": high_outcome,
                            "entryOutcome": entry_outcome,
                            "case": case, "filter": filter_name,
                            "signalUs": int(signal["sourceUs"]),
                            "remainingSeconds": signal["remaining"],
                            "highVwap": signal["highVwap"],
                            "dropFromHigh": signal["dropFromHigh"],
                            "entryVwap": signal["oppositeVwap"],
                            "entryWorstPrice": signal["oppositeWorstPrice"],
                            "entryFee": signal["oppositeFee"],
                            "crossAgeMs": cross_state["ageUs"] / 1_000 if cross_state else "",
                            "spotReturn": spot_return if spot_return is not None else "",
                            "futuresReturn": futures_return if futures_return is not None else "",
                            "won": won, "pnlPerShare": pnl,
                        }
                        writer.writerow(row)
                        for scope in ("all", split):
                            summaries[(scope, entry_outcome, case, filter_name)].add(row)
                        if split == "validation":
                            daily[(beijing_date, entry_outcome, case, filter_name)].add(row)
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
        "experiment": "E3_高位快速反向入场",
        "settledMarkets": len(markets),
        "trainMarkets": split_index,
        "validationMarkets": len(markets) - split_index,
        "crossMarkets": len(cross),
        "quantity": float(QTY),
        "reversalCases": REVERSAL_CASES,
        "filters": FILTERS,
        "summaries": sorted(summary_rows, key=summary_sort_key),
        "dailyValidationSummaries": sorted(
            daily_rows, key=lambda row: (row["beijingDate"], *summary_sort_key(row))
        ),
    }
    result_path = args.out / "e3_results.json"
    report_path = args.out / "E3_高位快速反向入场回测.md"
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    write_report(report_path, result)
    print(result_path)
    print(detail_path)
    print(report_path)
    return 0


def load_cross_snapshots(
    cross_dir: Path, market_ids: set[str]
) -> dict[str, list[dict[str, Any]]]:
    output: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    paths = sorted((cross_dir / "venue_snapshots").glob("date=*/hour=*/part-*.ndjson*"))
    for row in read_partition_rows(paths):
        market_id = str(row.get("marketId", ""))
        if market_id not in market_ids:
            continue
        features = row.get("features") or {}
        spot = features.get("binance") or {}
        futures = features.get("binanceFutures") or {}
        output[market_id].append({
            "sourceUs": int(row.get("sourceTimeUs") or 0),
            "spot1s": spot.get("return1s"), "spot3s": spot.get("return3s"),
            "spot5s": spot.get("return5s"),
            "futures1s": futures.get("return1s"),
            "futures3s": futures.get("return3s"),
            "futures5s": futures.get("return5s"),
        })
    for market_id in output:
        output[market_id].sort(key=lambda item: item["sourceUs"])
    return dict(output)


def latest_cross_state(
    rows: list[dict[str, Any]], signal_us: int, *, max_age_us: int
) -> dict[str, Any] | None:
    if not rows:
        return None
    times = [row["sourceUs"] for row in rows]
    index = bisect_right(times, signal_us) - 1
    if index < 0:
        return None
    selected = rows[index]
    age_us = signal_us - int(selected["sourceUs"])
    return None if age_us > max_age_us else {**selected, "ageUs": age_us}


def precompute_points(
    snapshots: list[dict[str, Any]], end_us: int
) -> dict[str, list[dict[str, Any]]]:
    output = {"UP": [], "DOWN": []}
    for snapshot in snapshots:
        remaining = (end_us - snapshot["sourceUs"]) / 1_000_000.0
        if not 30 <= remaining <= 240:
            continue
        fills: dict[str, tuple[Decimal, Decimal, Decimal] | None] = {}
        for outcome in ("UP", "DOWN"):
            levels = [
                BookLevel(Decimal(str(price)), Decimal(str(size)))
                for price, size in snapshot["asks"].get(outcome) or []
            ]
            fills[outcome] = consume_asks(
                levels, QTY, Decimal("1"),
                fee_model="polymarket_crypto_taker", fee_rate=Decimal("0.07"),
            )
        for outcome in ("UP", "DOWN"):
            own = fills[outcome]
            opposite = fills["DOWN" if outcome == "UP" else "UP"]
            if own is None:
                continue
            output[outcome].append({
                "sourceUs": snapshot["sourceUs"], "remaining": remaining,
                "vwap": float(own[0]), "fee": float(own[1]),
                "worstPrice": float(own[2]),
                "oppositeVwap": float(opposite[0]) if opposite else None,
                "oppositeFee": float(opposite[1]) if opposite else None,
                "oppositeWorstPrice": float(opposite[2]) if opposite else None,
            })
    return output


def passes_filter(
    filter_name: str, direction: str, state: dict[str, Any] | None
) -> tuple[bool, float | None, float | None]:
    if filter_name == "none":
        return True, None, None
    if state is None:
        return False, None, None
    seconds = filter_name.rsplit("_", 1)[-1]
    spot = state.get(f"spot{seconds}")
    futures = state.get(f"futures{seconds}")
    expected = 1 if direction == "UP" else -1
    spot_ok = spot is not None and float(spot) * expected > 0
    if filter_name.startswith("spot_futures"):
        futures_ok = futures is not None and float(futures) * expected > 0
        return spot_ok and futures_ok, as_float(spot), as_float(futures)
    return spot_ok, as_float(spot), as_float(futures)


def as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def summary_row(key: tuple[Any, ...], value: Summary) -> dict[str, Any]:
    scope, direction, case, filter_name = key
    return {
        "scope": scope, "entryOutcome": direction, "case": case,
        "filter": filter_name, "entries": value.entries,
        "wins": value.wins, "losses": value.entries - value.wins,
        "winRate": value.wins / value.entries,
        "averageEntryVwap": value.entry_sum / value.entries,
        "averageDropFromHigh": value.drop_sum / value.entries,
        "averageNetPnlPerShare": value.pnl_sum / value.entries,
        "totalNetPnlPerShare": value.pnl_sum,
    }


def summary_sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return row["scope"], row["entryOutcome"], row["case"], row["filter"]


def write_report(path: Path, result: dict[str, Any]) -> None:
    validation = [
        row for row in result["summaries"]
        if row["scope"] == "validation"
    ]
    labels = {
        "pullback_5s_01": "5秒回落≥0.01",
        "reversal_10s_02": "10秒回落≥0.02",
        "reversal_15s_03": "15秒回落≥0.03",
    }
    lines = [
        "# E3：高位快速反向后的反转入场回测", "",
        "## 固定口径", "",
        f"- 数据：{result['settledMarkets']}个已结算市场；后{result['validationMarkets']}个为时间外验证；可对齐跨市场快照的市场{result['crossMarkets']}个。",
        "- 先要求某方向10份可成交VWAP达到0.80，再观察其在最近5/10/15秒内回落0.01/0.02/0.03。",
        "- 信号出现时买入相反方向，按当时10份逐档VWAP和taker手续费，持有到官方结算。",
        "- 比较：只看Polymarket、Binance Spot 1/3/5秒确认、Spot+Futures 1/3/5秒同时确认。",
        "- 外部确认只使用信号时点以前、年龄不超过3秒的跨市场快照。", "",
        "## 时间外验证结果", "",
        "| 反转定义 | 入场方向 | 外部确认 | 笔数 | 胜/负 | 胜率 | 平均入场VWAP | 平均净收益/份 |",
        "|---|---|---|---:|---:|---:|---:|---:|",
    ]
    for row in sorted(validation, key=lambda item: (
        item["case"], item["entryOutcome"], item["filter"]
    )):
        if row["entries"] < 10:
            continue
        lines.append(
            f"| {labels[row['case']]} | {row['entryOutcome']} | {row['filter']} | "
            f"{row['entries']} | {row['wins']}/{row['losses']} | {row['winRate']:.1%} | "
            f"{row['averageEntryVwap']:.3f} | {row['averageNetPnlPerShare']:+.4f} |"
        )
    candidates = [
        row for row in validation
        if row["entries"] >= 100 and row["averageNetPnlPerShare"] > 0
    ]
    lines.extend(["", "## 结论", ""])
    if candidates:
        top = max(candidates, key=lambda row: row["averageNetPnlPerShare"])
        lines.append(
            f"有{len(candidates)}个组合达到验证段至少100笔且正收益；最佳为{labels[top['case']]}、"
            f"买{top['entryOutcome']}、过滤{top['filter']}，{top['entries']}笔，"
            f"平均{top['averageNetPnlPerShare']:+.4f}/份。仍需逐日一致性检查。"
        )
    else:
        lines.append("没有组合同时达到验证段至少100笔和正净收益，E3不通过；快速回落不能单独当作可交易反转。")
    lines.extend([
        "", "## 限制", "",
        "- 反方向入场没有额外限制价格带；报告同时给出实际平均入场VWAP。",
        "- 外部过滤会大幅减少样本，少于30笔仅作观察。",
        "- 只模拟持有到结算；中途退出归入后续止损实验。",
        "- 盘口与跨市场来自两个采集进程，虽使用因果时间对齐，仍可能有本机时钟和网络延迟误差。", "",
        "## 复现命令", "", "```bash",
        "PYTHONPATH=src python3 scripts/analysis/run_strategy_idea_e3.py", "```", "",
    ])
    path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
