#!/usr/bin/env python3
"""E16: full-timeline strict positive-crossing grid for BTC five-minute markets.

The entry price is the executable VWAP for ten shares.  A signal exists only
when that VWAP moves from below a threshold to the threshold or above inside a
specific remaining-time bucket.  A downward touch from above is never an
entry.  Results are split chronologically so parameter ranking can be judged on
the later 30% of markets instead of on the same rows used for exploration.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import math
from pathlib import Path
import sqlite3
from statistics import mean
import sys
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.analysis.analyze_intramarket_exits import QTY, consume, load_market_snapshots


THRESHOLDS = tuple(Decimal(f"{value / 100:.2f}") for value in range(50, 91, 5))
TIME_BUCKETS = tuple((high, high - 20) for high in range(300, 0, -20))
MAX_CROSS_GAP_US = 5_000_000
MAX_OVERSHOOT = Decimal("0.05")


def remaining_bucket(remaining: Decimal) -> tuple[int, int] | None:
    """Return (upper, lower) for a countdown bucket such as 300–280 seconds."""
    for upper, lower in TIME_BUCKETS:
        if Decimal(lower) < remaining <= Decimal(upper):
            return upper, lower
    return None


def build_buy_points(
    snapshots: list[dict[str, Any]], end_us: int, outcome: str
) -> list[dict[str, Any]]:
    points: list[dict[str, Any]] = []
    for snapshot in snapshots:
        source_us = int(snapshot["sourceUs"])
        if source_us >= end_us:
            continue
        filled = consume(snapshot["asks"].get(outcome) or [], QTY, cap=Decimal("0.99"))
        if filled is None:
            continue
        vwap, entry_fee = filled
        points.append({
            "sourceUs": source_us,
            "remaining": Decimal(end_us - source_us) / Decimal("1000000"),
            "vwap": vwap,
            "fee": entry_fee,
        })
    return points


def prior_value(
    points: list[dict[str, Any]], entry_index: int, seconds: int
) -> Decimal | None:
    target_us = int(points[entry_index]["sourceUs"]) - seconds * 1_000_000
    lower_us = target_us - 2_000_000
    selected: dict[str, Any] | None = None
    for point in points[:entry_index]:
        source_us = int(point["sourceUs"])
        if lower_us <= source_us <= target_us:
            selected = point
        elif source_us > target_us:
            break
    return None if selected is None else Decimal(str(selected["vwap"]))


def strict_upcrosses(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the first valid positive crossing for every bucket/threshold pair."""
    found: dict[tuple[int, int, Decimal], dict[str, Any]] = {}
    if len(points) < 2:
        return []
    session_low = Decimal(str(points[0]["vwap"]))
    session_high = session_low
    for index in range(1, len(points)):
        previous, current = points[index - 1], points[index]
        prior_price = Decimal(str(previous["vwap"]))
        current_price = Decimal(str(current["vwap"]))
        session_low = min(session_low, prior_price, current_price)
        session_high = max(session_high, prior_price, current_price)
        gap_us = int(current["sourceUs"]) - int(previous["sourceUs"])
        bucket = remaining_bucket(Decimal(str(current["remaining"])))
        if bucket is None or not (0 < gap_us <= MAX_CROSS_GAP_US):
            continue
        for threshold in THRESHOLDS:
            key = (bucket[0], bucket[1], threshold)
            if key in found:
                continue
            if (
                prior_price < threshold <= current_price
                and current_price <= threshold + MAX_OVERSHOOT
            ):
                prior5 = prior_value(points, index, 5)
                found[key] = {
                    **current,
                    "bucketUpper": bucket[0],
                    "bucketLower": bucket[1],
                    "threshold": threshold,
                    "priorVwap": prior_price,
                    "crossGapSeconds": Decimal(gap_us) / Decimal("1000000"),
                    "crossDelta": current_price - prior_price,
                    "crossVelocityPerSecond": (
                        (current_price - prior_price) * Decimal("1000000") / Decimal(gap_us)
                    ),
                    "momentum5": None if prior5 is None else current_price - prior5,
                    "sessionRangeAtEntry": session_high - session_low,
                    "distanceFromSessionLow": current_price - session_low,
                }
    return sorted(
        found.values(),
        key=lambda row: (-int(row["bucketUpper"]), Decimal(str(row["threshold"]))),
    )


def fee_adjusted_hold_pnl(entry: dict[str, Any], winner: str, outcome: str) -> Decimal:
    payout = Decimal("1") if winner == outcome else Decimal("0")
    return payout - Decimal(str(entry["vwap"])) - Decimal(str(entry["fee"]))


def mark_first_side(signals: list[dict[str, Any]]) -> None:
    groups: dict[tuple[int, int, str], list[dict[str, Any]]] = defaultdict(list)
    for signal in signals:
        signal["isFirstSideCross"] = False
        groups[(
            int(signal["bucketUpper"]), int(signal["bucketLower"]), str(signal["threshold"])
        )].append(signal)
    for items in groups.values():
        earliest = min(int(item["entryUs"]) for item in items)
        selected = [item for item in items if int(item["entryUs"]) == earliest]
        if len(selected) == 1:
            selected[0]["isFirstSideCross"] = True


def assign_split(signals: list[dict[str, Any]], fraction: float = 0.70) -> dict[str, Any]:
    market_times: dict[str, int] = {}
    for signal in signals:
        market_id = str(signal["marketId"])
        market_times[market_id] = min(
            int(signal["marketEndUs"]), market_times.get(market_id, int(signal["marketEndUs"]))
        )
    ordered = sorted(market_times.items(), key=lambda item: (item[1], item[0]))
    if len(ordered) < 2:
        raise RuntimeError("正向突破信号不足两个市场，无法进行时间切分")
    train_count = min(len(ordered) - 1, max(1, int(len(ordered) * fraction)))
    train_ids = {market_id for market_id, _ in ordered[:train_count]}
    for signal in signals:
        signal["timeSplit"] = "train" if signal["marketId"] in train_ids else "validation"
    return {
        "trainFraction": fraction,
        "signalMarkets": len(ordered),
        "trainMarkets": train_count,
        "validationMarkets": len(ordered) - train_count,
        "validationStartUs": ordered[train_count][1],
    }


def wilson_lower(wins: int, total: int, z: float = 1.96) -> float | None:
    if not total:
        return None
    p = wins / total
    denominator = 1 + z * z / total
    centre = p + z * z / (2 * total)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total)
    return (centre - margin) / denominator


def summarize(items: list[dict[str, Any]]) -> dict[str, Any]:
    wins = sum(item["winner"] == item["outcome"] for item in items)
    pnls = [float(item["holdPnlPerShare"]) for item in items]
    return {
        "entries": len(items),
        "wins": wins,
        "losses": len(items) - wins,
        "winRate": wins / len(items) if items else None,
        "wilsonLower95": wilson_lower(wins, len(items)),
        "averageEntryVwap": mean(float(item["entryVwap"]) for item in items) if items else None,
        "averagePnlPerShare": mean(pnls) if pnls else None,
        "totalPnlPerShare": sum(pnls),
        "averageMomentum5": mean(
            float(item["momentum5"]) for item in items if item.get("momentum5") is not None
        ) if any(item.get("momentum5") is not None for item in items) else None,
        "averageSessionRange": mean(float(item["sessionRangeAtEntry"]) for item in items) if items else None,
    }


def summarize_grid(signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for split in ("all", "train", "validation"):
        candidates = [item for item in signals if split == "all" or item["timeSplit"] == split]
        for selection in ("direction", "first_side"):
            groups: dict[tuple[int, int, str, str], list[dict[str, Any]]] = defaultdict(list)
            for item in candidates:
                upper, lower, threshold = (
                    int(item["bucketUpper"]), int(item["bucketLower"]), str(item["threshold"])
                )
                groups[(upper, lower, threshold, item["outcome"])].append(item)
                if selection == "first_side" and item["isFirstSideCross"]:
                    groups[(upper, lower, threshold, "ANY")].append(item)
            for (upper, lower, threshold, outcome), items in sorted(
                groups.items(), key=lambda pair: (-pair[0][0], pair[0][2], pair[0][3])
            ):
                if selection == "first_side" and outcome != "ANY":
                    continue
                rows.append({
                    "split": split,
                    "selection": selection,
                    "bucketUpper": upper,
                    "bucketLower": lower,
                    "timeBucket": f"{upper}–{lower}",
                    "threshold": threshold,
                    "outcome": outcome,
                    **summarize(items),
                })
    return rows


def local_time(timestamp_us: int) -> str:
    tz = timezone(timedelta(hours=8))
    return datetime.fromtimestamp(timestamp_us / 1_000_000, tz).isoformat()


def pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def num(value: float | None, digits: int = 4) -> str:
    return "—" if value is None else f"{value:+.{digits}f}"


def report_lines(payload: dict[str, Any]) -> list[str]:
    rows = payload["summaries"]
    validation = [row for row in rows if row["split"] == "validation"]
    threshold80 = [
        row for row in validation
        if row["threshold"] == "0.80" and (
            row["selection"] == "first_side" or row["outcome"] in {"UP", "DOWN"}
        )
    ]
    ranked = sorted(
        [
            row for row in validation
            if row["selection"] == "first_side" and row["entries"] >= 20
        ],
        key=lambda row: (row["averagePnlPerShare"], row["wilsonLower95"]),
        reverse=True,
    )[:25]
    early = [
        row for row in validation
        if row["selection"] == "first_side"
        and row["bucketLower"] >= 180
        and row["entries"] >= 10
    ]

    def table(items: Iterable[dict[str, Any]]) -> list[str]:
        output = [
            "| 剩余时间 | 阈值 | 方向/选择 | 笔数 | 胜率 | 95%胜率下界 | 平均VWAP | 净收益/份 | 5秒动量 | 本场振幅 |",
            "|---|---:|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for row in items:
            direction = "最先突破侧" if row["selection"] == "first_side" else row["outcome"]
            output.append(
                f"| {row['timeBucket']}秒 | {row['threshold']} | {direction} | {row['entries']} | "
                f"{pct(row['winRate'])} | {pct(row['wilsonLower95'])} | "
                f"{row['averageEntryVwap']:.4f} | {num(row['averagePnlPerShare'])} | "
                f"{num(row['averageMomentum5'], 3)} | {num(row['averageSessionRange'], 3)} |"
            )
        return output

    data = payload["data"]
    return [
        "# E16 全时间轴正向首次突破网格", "",
        "## 固定口径", "",
        "- 只认10份可执行VWAP从阈值下方向上穿越；从高位回落进入阈值不计入。",
        "- 五分钟全时间轴按剩余300–0秒切成20秒一档；例如300–280秒就是开盘后的前20秒。",
        "- 阈值为0.50–0.90、每0.05一档；相邻盘口间隔不超过5秒，且穿越后价格不得高出阈值0.05以上。",
        "- 买入后持有至官方结算；入场按Polymarket加密市场taker手续费计入净收益。",
        "- 同时保留UP、DOWN各自信号，以及同一市场/时间档/阈值最先突破的一侧。",
        f"- 数据：{data['settledMarkets']}个已结算市场，{data['signals']}条方向突破；北京时间 {data['startLocal']} 至 {data['endLocal']}。",
        f"- 时间切分：训练{data['trainMarkets']}个有信号市场，后30%验证{data['validationMarkets']}个；下表以验证段为主。", "",
        "## 0.80正向突破：后30%时间外验证", "",
        *table(sorted(threshold80, key=lambda row: (-row["bucketUpper"], row["selection"], row["outcome"]))), "",
        "## 全网格净收益靠前（验证段、最先突破侧、至少20笔）", "",
        *table(ranked), "",
        "## 开局前120秒候选（验证段、最先突破侧、至少10笔）", "",
        *table(sorted(early, key=lambda row: (-row["bucketUpper"], row["threshold"]))), "",
        "## 解释边界", "",
        "- 100%胜率若只有几笔，95% Wilson下界仍会很低；排序时必须同时看样本、下界、净收益和跨日期稳定性。",
        "- 本实验回答预测市场本身的正向突破路径，不要求主动成交确认，因此不能直接替换成熟策略。",
        "- 本场振幅和5秒动量已经保存在逐笔CSV，下一步可与E15的Binance距离概率做因果时点连接。",
        "- 参数很多，表内最优组合属于探索结果；至少需要新的日期样本复验，不能直接写入实盘。", "",
        "## 复现", "", "```bash",
        "PYTHONDONTWRITEBYTECODE=1 python3 scripts/analysis/analyze_positive_upcross_time_grid.py",
        "```", "",
    ]


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    fields: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                fields.append(key)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="E16全时间轴正向首次突破网格")
    parser.add_argument(
        "--cache", type=Path,
        default=ROOT / "data/derived/live_strategy_cache.sqlite3",
    )
    parser.add_argument(
        "--out", type=Path,
        default=ROOT / "outputs/research_tests/e16_positive_upcross_probability_20260830",
    )
    args = parser.parse_args()
    connection = sqlite3.connect(args.cache)
    markets = list(connection.execute(
        "SELECT market_id,end_us,winner FROM markets "
        "WHERE winner IN ('UP','DOWN') AND end_us IS NOT NULL ORDER BY end_us"
    ))
    signals: list[dict[str, Any]] = []
    min_us, max_us = connection.execute("SELECT MIN(source_us),MAX(source_us) FROM snapshots").fetchone()
    for index, (market_id, end_us, winner) in enumerate(markets, 1):
        snapshots = load_market_snapshots(connection, str(market_id))
        if not snapshots:
            continue
        market_signals: list[dict[str, Any]] = []
        for outcome in ("UP", "DOWN"):
            points = build_buy_points(snapshots, int(end_us), outcome)
            for entry in strict_upcrosses(points):
                market_signals.append({
                    "marketId": str(market_id),
                    "marketEndUs": int(end_us),
                    "outcome": outcome,
                    "winner": str(winner),
                    "bucketUpper": int(entry["bucketUpper"]),
                    "bucketLower": int(entry["bucketLower"]),
                    "threshold": str(entry["threshold"]),
                    "entryUs": int(entry["sourceUs"]),
                    "entryRemainingSeconds": str(entry["remaining"]),
                    "priorVwap": str(entry["priorVwap"]),
                    "entryVwap": str(entry["vwap"]),
                    "entryFeePerShare": str(entry["fee"]),
                    "crossGapSeconds": str(entry["crossGapSeconds"]),
                    "crossDelta": str(entry["crossDelta"]),
                    "crossVelocityPerSecond": str(entry["crossVelocityPerSecond"]),
                    "momentum5": None if entry["momentum5"] is None else str(entry["momentum5"]),
                    "sessionRangeAtEntry": str(entry["sessionRangeAtEntry"]),
                    "distanceFromSessionLow": str(entry["distanceFromSessionLow"]),
                    "holdPnlPerShare": str(fee_adjusted_hold_pnl(entry, str(winner), outcome)),
                })
        mark_first_side(market_signals)
        signals.extend(market_signals)
        if index % 100 == 0:
            print(f"processed={index}/{len(markets)} signals={len(signals)}")
    connection.close()
    split = assign_split(signals)
    summaries = summarize_grid(signals)
    payload = {
        "schemaVersion": 1,
        "experiment": "E16_positive_upcross_full_timeline",
        "cache": str(args.cache),
        "thresholds": [str(value) for value in THRESHOLDS],
        "timeBuckets": [list(value) for value in TIME_BUCKETS],
        "maxCrossGapUs": MAX_CROSS_GAP_US,
        "maxOvershoot": str(MAX_OVERSHOOT),
        "data": {
            "settledMarkets": len(markets),
            "signals": len(signals),
            "startUs": int(min_us),
            "endUs": int(max_us),
            "startLocal": local_time(int(min_us)),
            "endLocal": local_time(int(max_us)),
            "trainMarkets": split["trainMarkets"],
            "validationMarkets": split["validationMarkets"],
            "validationStartLocal": local_time(split["validationStartUs"]),
        },
        "timeSplit": split,
        "summaries": summaries,
    }
    args.out.mkdir(parents=True, exist_ok=True)
    json_path = args.out / "e16_positive_upcross_time_grid.json"
    signals_path = args.out / "e16_positive_upcross_signals.csv"
    summaries_path = args.out / "e16_positive_upcross_summaries.csv"
    report_path = args.out / "E16_全时间轴正向首次突破网格.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_csv(signals_path, signals)
    write_csv(summaries_path, summaries)
    report_path.write_text("\n".join(report_lines(payload)), encoding="utf-8")
    print(report_path)
    print(json_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
