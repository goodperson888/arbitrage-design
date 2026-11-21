#!/usr/bin/env python3
"""E12: strict upward price crossings, exit policies, and Chainlink stratification.

This is an offline, causal replay.  An entry exists only when an executable
10-share VWAP moves from below a threshold to the threshold or above while the
market is inside the requested late window.  Downward touches are excluded.
"""

from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import defaultdict
import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
from statistics import median
import sys
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.analysis.analyze_intramarket_exits import (
    QTY,
    complement_bids,
    consume,
    fee,
    load_market_snapshots,
)


WINDOW_SECONDS = (90, 60)
THRESHOLDS = tuple(Decimal(f"{value / 100:.2f}") for value in range(50, 91, 5))
MAX_CROSS_GAP_US = 5_000_000
MAX_OVERSHOOT = Decimal("0.05")
CHAINLINK_MAX_ALIGNMENT_AGE_US = 35_000_000
MOMENTUM_FLOORS = (None, Decimal("0.01"), Decimal("0.03"))
TAKE_PROFITS = (Decimal("0.03"), Decimal("0.05"), Decimal("0.10"), Decimal("0.20"))


def signed_for_outcome(value: Decimal, outcome: str) -> Decimal:
    return value if outcome == "UP" else -value


def chainlink_support_band(value: Decimal) -> str:
    if value < 0:
        return "opposing"
    if value < 5:
        return "support_00_05"
    if value < 10:
        return "support_05_10"
    if value < 25:
        return "support_10_25"
    return "support_25_plus"


def first_strict_upcross(
    points: list[dict[str, Any]],
    threshold: Decimal,
    max_remaining_seconds: int,
    *,
    max_gap_us: int = MAX_CROSS_GAP_US,
    max_overshoot: Decimal = MAX_OVERSHOOT,
) -> dict[str, Any] | None:
    previous: dict[str, Any] | None = None
    maximum = Decimal(str(max_remaining_seconds))
    for point in points:
        remaining = Decimal(str(point["remaining"]))
        current = Decimal(str(point["vwap"]))
        if remaining <= 0:
            break
        if previous is not None and remaining <= maximum:
            prior_price = Decimal(str(previous["vwap"]))
            gap_us = int(point["sourceUs"]) - int(previous["sourceUs"])
            if (
                0 < gap_us <= max_gap_us
                and prior_price < threshold <= current
                and current <= threshold + max_overshoot
            ):
                return {
                    **point,
                    "priorSourceUs": int(previous["sourceUs"]),
                    "priorVwap": prior_price,
                    "crossGapSeconds": Decimal(gap_us) / Decimal("1000000"),
                    "crossDelta": current - prior_price,
                }
        previous = point
    return None


def mark_first_side_crosses(signals: list[dict[str, Any]]) -> None:
    """Mark the unique earliest direction for each window/threshold pair."""
    grouped: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    for signal in signals:
        signal["isFirstSideCross"] = False
        grouped[(int(signal["windowSeconds"]), str(signal["threshold"]))].append(signal)
    for items in grouped.values():
        earliest_us = min(int(item["entryUs"]) for item in items)
        earliest = [item for item in items if int(item["entryUs"]) == earliest_us]
        if len(earliest) == 1:
            earliest[0]["isFirstSideCross"] = True


def assign_time_split(signals: list[dict[str, Any]], train_fraction: float = 0.7) -> dict[str, Any]:
    market_times: dict[str, int] = {}
    for signal in signals:
        market_id = str(signal["marketId"])
        entry_us = int(signal["entryUs"])
        market_times[market_id] = min(entry_us, market_times.get(market_id, entry_us))
    ordered = sorted(market_times.items(), key=lambda item: (item[1], item[0]))
    if len(ordered) < 2:
        raise RuntimeError("严格突破信号不足两个市场，不能做时间切分")
    train_count = min(len(ordered) - 1, max(1, int(len(ordered) * train_fraction)))
    train_ids = {market_id for market_id, _ in ordered[:train_count]}
    for signal in signals:
        signal["timeSplit"] = "train" if str(signal["marketId"]) in train_ids else "validation"
    return {
        "trainFraction": train_fraction,
        "signalMarkets": len(ordered),
        "trainMarkets": train_count,
        "validationMarkets": len(ordered) - train_count,
        "validationMinEntryUs": ordered[train_count][1],
    }


def build_buy_points(
    snapshots: list[dict[str, Any]], end_us: int, outcome: str
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for snapshot in snapshots:
        source_us = int(snapshot["sourceUs"])
        if source_us >= end_us:
            continue
        filled = consume(snapshot["asks"].get(outcome) or [], QTY, cap=Decimal("0.99"))
        if filled is None:
            continue
        vwap, entry_fee = filled
        result.append({
            "sourceUs": source_us,
            "remaining": Decimal(end_us - source_us) / Decimal("1000000"),
            "vwap": vwap,
            "fee": entry_fee,
        })
    return result


def build_sell_points(
    snapshots: list[dict[str, Any]], end_us: int, outcome: str
) -> list[dict[str, Any]]:
    opposite = "DOWN" if outcome == "UP" else "UP"
    result: list[dict[str, Any]] = []
    for snapshot in snapshots:
        source_us = int(snapshot["sourceUs"])
        if source_us >= end_us:
            continue
        bids = complement_bids(snapshot["asks"].get(opposite) or [])
        filled = consume(bids, QTY, descending=True)
        if filled is None:
            continue
        vwap, exit_fee = filled
        result.append({
            "sourceUs": source_us,
            "remaining": Decimal(end_us - source_us) / Decimal("1000000"),
            "vwap": vwap,
            "fee": exit_fee,
        })
    return result


def five_second_momentum(points: list[dict[str, Any]], entry: dict[str, Any]) -> Decimal | None:
    entry_us = int(entry["sourceUs"])
    target_us = entry_us - 5_000_000
    eligible = [
        point for point in points
        if target_us - 2_000_000 <= int(point["sourceUs"]) <= target_us
    ]
    if not eligible:
        return None
    prior = max(eligible, key=lambda point: int(point["sourceUs"]))
    return Decimal(str(entry["vwap"])) - Decimal(str(prior["vwap"]))


def hold_pnl(entry: dict[str, Any], *, winner: str, outcome: str) -> Decimal:
    payout = Decimal("1") if winner == outcome else Decimal("0")
    return payout - Decimal(str(entry["vwap"])) - Decimal(str(entry["fee"]))


def close_point(path: list[dict[str, Any]]) -> dict[str, Any] | None:
    for point in path:
        if Decimal(str(point["remaining"])) <= Decimal("5"):
            return point
    return path[-1] if path else None


def exit_pnl(entry: dict[str, Any], point: dict[str, Any]) -> Decimal:
    return (
        Decimal(str(point["vwap"]))
        - Decimal(str(entry["vwap"]))
        - Decimal(str(entry["fee"]))
        - Decimal(str(point["fee"]))
    )


def evaluate_exit_policies(
    entry: dict[str, Any],
    sell_points: list[dict[str, Any]],
    *,
    winner: str,
    outcome: str,
) -> dict[str, dict[str, Any]]:
    path = [point for point in sell_points if int(point["sourceUs"]) > int(entry["sourceUs"])]
    hold = hold_pnl(entry, winner=winner, outcome=outcome)
    result: dict[str, dict[str, Any]] = {
        "hold_resolution": {"pnl": hold, "trigger": "RESOLUTION", "exited": False}
    }
    close = close_point(path)
    result["close_5s"] = (
        {"pnl": exit_pnl(entry, close), "trigger": "CLOSE_5S", "exited": True}
        if close is not None else
        {"pnl": hold, "trigger": "NO_EXIT_HOLD", "exited": False}
    )
    entry_vwap = Decimal(str(entry["vwap"]))
    for target in TAKE_PROFITS:
        selected = next(
            (
                point for point in path
                if Decimal(str(point["vwap"])) >= entry_vwap + target
            ),
            None,
        )
        suffix = f"{int(target * 100):02d}"
        if selected is not None:
            payload = {"pnl": exit_pnl(entry, selected), "trigger": "TP", "exited": True}
            result[f"tp{suffix}_else_hold"] = payload
            result[f"tp{suffix}_else_close5"] = payload
        else:
            result[f"tp{suffix}_else_hold"] = {
                "pnl": hold, "trigger": "RESOLUTION", "exited": False,
            }
            result[f"tp{suffix}_else_close5"] = (
                {"pnl": exit_pnl(entry, close), "trigger": "CLOSE_5S", "exited": True}
                if close is not None else
                {"pnl": hold, "trigger": "NO_EXIT_HOLD", "exited": False}
            )
    return result


def load_chainlink_rows(path: Path) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            try:
                row["observationSourceUs"] = int(row["observationSourceUs"])
                row["terminalGapBps"] = Decimal(row["terminalGapBps"])
                row["requiredMarginBps"] = Decimal(row["requiredMarginBps"])
            except (KeyError, TypeError, ValueError):
                continue
            grouped[str(row["marketId"])].append(row)
    for market_id in grouped:
        grouped[market_id].sort(key=lambda item: int(item["observationSourceUs"]))
    return grouped


def latest_chainlink_before(
    grouped: dict[str, list[dict[str, Any]]], market_id: str, entry_us: int
) -> dict[str, Any] | None:
    rows = grouped.get(market_id) or []
    if not rows:
        return None
    times = [int(row["observationSourceUs"]) for row in rows]
    index = bisect_right(times, entry_us) - 1
    if index < 0:
        return None
    row = rows[index]
    age_us = entry_us - int(row["observationSourceUs"])
    if age_us > CHAINLINK_MAX_ALIGNMENT_AGE_US:
        return None
    return {**row, "alignmentAgeMs": Decimal(age_us) / Decimal("1000")}


def average(values: Iterable[Decimal]) -> Decimal | None:
    items = list(values)
    return sum(items, Decimal("0")) / len(items) if items else None


def summarize_signals(signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for split in ("all", "train", "validation"):
        for selection in ("direction", "first_side"):
            selected = [
                signal for signal in signals
                if (split == "all" or signal["timeSplit"] == split)
                and (selection == "direction" or signal["isFirstSideCross"])
            ]
            for momentum_floor in MOMENTUM_FLOORS:
                grouped: dict[tuple[int, str, str], list[dict[str, Any]]] = defaultdict(list)
                for signal in selected:
                    momentum = signal.get("fiveSecondMomentum")
                    if momentum_floor is not None and (
                        momentum is None or Decimal(str(momentum)) < momentum_floor
                    ):
                        continue
                    grouped[(signal["windowSeconds"], signal["threshold"], signal["outcome"])].append(signal)
                    if selection == "first_side":
                        grouped[(signal["windowSeconds"], signal["threshold"], "ANY")].append(signal)
                for (window, threshold, outcome), items in sorted(grouped.items()):
                    wins = sum(item["winner"] == item["outcome"] for item in items)
                    hold = [Decimal(str(item["holdPnlPerShare"])) for item in items]
                    rows.append({
                        "split": split,
                        "selection": selection,
                        "windowSeconds": window,
                        "threshold": threshold,
                        "outcome": outcome,
                        "momentumFloor": None if momentum_floor is None else str(momentum_floor),
                        "entries": len(items),
                        "wins": wins,
                        "winRate": wins / len(items),
                        "averageEntryVwap": float(average(Decimal(str(item["entryVwap"])) for item in items)),
                        "averageHoldPnlPerShare": float(average(hold)),
                        "totalHoldPnlPerShare": float(sum(hold)),
                    })
    return rows


def summarize_exits(signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for split in ("all", "train", "validation"):
        for selection in ("direction", "first_side"):
            grouped: dict[tuple[int, str, str, str], list[dict[str, Any]]] = defaultdict(list)
            for signal in signals:
                if split != "all" and signal["timeSplit"] != split:
                    continue
                if selection == "first_side" and not signal["isFirstSideCross"]:
                    continue
                for policy, result in signal["exitPolicies"].items():
                    grouped[(signal["windowSeconds"], signal["threshold"], signal["outcome"], policy)].append(result)
                    if selection == "first_side":
                        grouped[(signal["windowSeconds"], signal["threshold"], "ANY", policy)].append(result)
            for (window, threshold, outcome, policy), items in sorted(grouped.items()):
                pnls = [Decimal(str(item["pnl"])) for item in items]
                rows.append({
                    "split": split,
                    "selection": selection,
                    "windowSeconds": window,
                    "threshold": threshold,
                    "outcome": outcome,
                    "policy": policy,
                    "entries": len(items),
                    "positive": sum(value > 0 for value in pnls),
                    "positiveRate": sum(value > 0 for value in pnls) / len(pnls),
                    "exited": sum(bool(item["exited"]) for item in items),
                    "averagePnlPerShare": float(average(pnls)),
                    "medianPnlPerShare": float(Decimal(str(median(pnls)))),
                    "totalPnlPerShare": float(sum(pnls)),
                })
    return rows


def summarize_chainlink(signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for split in ("all", "train", "validation"):
        for selection in ("direction", "first_side"):
            grouped: dict[tuple[int, str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
            for signal in signals:
                if split != "all" and signal["timeSplit"] != split:
                    continue
                if selection == "first_side" and not signal["isFirstSideCross"]:
                    continue
                chainlink = signal.get("chainlink")
                if not chainlink:
                    continue
                for feature in ("terminal", "required"):
                    grouped[(
                        signal["windowSeconds"], signal["threshold"], signal["outcome"],
                        feature, chainlink[f"{feature}SupportBand"],
                    )].append(signal)
                    if selection == "first_side":
                        grouped[(
                            signal["windowSeconds"], signal["threshold"], "ANY",
                            feature, chainlink[f"{feature}SupportBand"],
                        )].append(signal)
            for (window, threshold, outcome, feature, band), items in sorted(grouped.items()):
                wins = sum(item["winner"] == item["outcome"] for item in items)
                pnl = [Decimal(str(item["holdPnlPerShare"])) for item in items]
                rows.append({
                    "split": split,
                    "selection": selection,
                    "windowSeconds": window,
                    "threshold": threshold,
                    "outcome": outcome,
                    "feature": feature,
                    "supportBand": band,
                    "entries": len(items),
                    "wins": wins,
                    "winRate": wins / len(items),
                    "averageHoldPnlPerShare": float(average(pnl)),
                    "averageAlignmentAgeMs": float(average(
                        Decimal(str(item["chainlink"]["alignmentAgeMs"])) for item in items
                    )),
                })
    return rows


def markdown_table(rows: list[dict[str, Any]], columns: list[tuple[str, str]]) -> list[str]:
    output = [
        "| " + " | ".join(label for _, label in columns) + " |",
        "|" + "|".join("---" for _ in columns) + "|",
    ]
    for row in rows:
        output.append("| " + " | ".join(str(row[key]) for key, _ in columns) + " |")
    return output


def write_report(
    path: Path,
    *,
    market_count: int,
    signals: list[dict[str, Any]],
    signal_summaries: list[dict[str, Any]],
    exit_summaries: list[dict[str, Any]],
    chainlink_summaries: list[dict[str, Any]],
    chainlink_path: Path,
    split_info: dict[str, Any],
    data_range: dict[str, Any],
) -> None:
    baseline = [
        row for row in signal_summaries
        if row["momentumFloor"] is None and row["selection"] == "first_side"
    ]
    level60_all = [
        row for row in baseline
        if row["split"] == "all" and row["threshold"] == "0.60"
    ]
    level60_validation = [
        row for row in baseline
        if row["split"] == "validation" and row["threshold"] == "0.60"
    ]
    top = sorted(
        [
            row for row in baseline
            if row["split"] == "validation" and row["outcome"] == "ANY"
            and row["entries"] >= 10
        ],
        key=lambda row: row["averageHoldPnlPerShare"], reverse=True,
    )[:15]
    exit60 = [
        row for row in exit_summaries
        if row["split"] == "validation" and row["selection"] == "first_side"
        and row["outcome"] == "ANY" and row["threshold"] == "0.60"
        and row["entries"] >= 8
    ]
    chain60 = [
        row for row in chainlink_summaries
        if row["split"] == "validation" and row["selection"] == "first_side"
        and row["outcome"] == "ANY" and row["threshold"] == "0.60"
        and row["entries"] >= 5
    ]
    momentum60 = [
        row for row in signal_summaries
        if row["split"] == "validation" and row["selection"] == "first_side"
        and row["outcome"] == "ANY" and row["threshold"] == "0.60"
        and row["entries"] >= 5
    ]
    def signal_view(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "window": row["windowSeconds"], "threshold": row["threshold"],
            "direction": row["outcome"], "entries": row["entries"],
            "win": f"{row['winRate']:.1%}", "entry": f"{row['averageEntryVwap']:.4f}",
            "pnl": f"{row['averageHoldPnlPerShare']:+.4f}",
        }
    def exit_view(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "window": row["windowSeconds"], "direction": row["outcome"],
            "policy": row["policy"], "entries": row["entries"],
            "positive": f"{row['positiveRate']:.1%}", "exited": row["exited"],
            "pnl": f"{row['averagePnlPerShare']:+.4f}",
        }
    def chain_view(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "window": row["windowSeconds"], "direction": row["outcome"],
            "feature": row["feature"], "band": row["supportBand"],
            "entries": row["entries"], "win": f"{row['winRate']:.1%}",
            "pnl": f"{row['averageHoldPnlPerShare']:+.4f}",
            "age": f"{row['averageAlignmentAgeMs']:.0f}",
        }
    def momentum_view(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "window": row["windowSeconds"],
            "floor": "仅严格突破" if row["momentumFloor"] is None else f">={row['momentumFloor']}",
            "entries": row["entries"], "win": f"{row['winRate']:.1%}",
            "pnl": f"{row['averageHoldPnlPerShare']:+.4f}",
        }
    lines = [
        "# E12：严格向上首次突破、退出与Chainlink锚定距离验证", "",
        "## 固定口径", "",
        f"- 数据：北京时间 {data_range['start']} 至 {data_range['end']}；刷新后的本地实时盘口缓存共 {market_count} 个已结算市场；生成 {len(signals)} 条方向突破信号，其中 {sum(bool(item['isFirstSideCross']) for item in signals)} 条为每个市场/窗口/阈值最先突破的一侧。",
        "- A 严格突破：10份可执行VWAP从阈值下方向上穿越；向下回落触碰不计；相邻有效快照不超过5秒，实际入场不允许比阈值高出0.05以上。",
        "- B 时间与力度：分别测试剩余90秒内、60秒内，阈值0.50至0.90（每0.05一档）；另保存入场前5秒动量≥0.01/0.03分组。",
        "- C 退出：持有结算、剩余约5秒退出、上涨0.03/0.05/0.10/0.20后卖出；未触发时分别比较持有结算和盘前退出。买卖两端均计手续费。",
        f"- D Chainlink：使用 `{chainlink_path}` 中同市场、入场前最后一个因果观察；最大对齐年龄35秒。按入场方向符号化终端锚定距离和所需剩余均价优势。",
        f"- 时间外验证：按有信号市场的最早入场时间70/30切分；训练 {split_info['trainMarkets']} 个市场，验证 {split_info['validationMarkets']} 个市场。所有候选排序只使用后30%验证段。",
        "- 同一市场可进入多个独立阈值/窗口实验；它们是参数对照，不代表实盘可重复下注。", "",
        "## 0.60最先向上突破的一侧：全样本", "",
        *markdown_table([signal_view(row) for row in level60_all], [
            ("window", "窗口秒"), ("direction", "方向"), ("entries", "笔数"),
            ("win", "胜率"), ("entry", "平均实际VWAP"), ("pnl", "平均净收益/份"),
        ]), "",
        "## 0.60最先向上突破的一侧：后30%时间外验证", "",
        *markdown_table([signal_view(row) for row in level60_validation], [
            ("window", "窗口秒"), ("direction", "方向"), ("entries", "笔数"),
            ("win", "胜率"), ("entry", "平均实际VWAP"), ("pnl", "平均净收益/份"),
        ]), "",
        "## 时间外全网格中平均净收益靠前的组合（至少10笔）", "",
        *markdown_table([signal_view(row) for row in top], [
            ("window", "窗口秒"), ("threshold", "突破阈值"), ("direction", "方向"),
            ("entries", "笔数"), ("win", "胜率"), ("entry", "平均实际VWAP"),
            ("pnl", "平均净收益/份"),
        ]), "",
        "## 0.60最先突破叠加5秒力度：时间外验证", "",
        *markdown_table([momentum_view(row) for row in momentum60], [
            ("window", "窗口秒"), ("floor", "5秒动量条件"), ("entries", "笔数"),
            ("win", "胜率"), ("pnl", "平均净收益/份"),
        ]), "",
        "## 0.60最先突破后的时间外退出对照（至少8笔）", "",
        *markdown_table([exit_view(row) for row in exit60], [
            ("window", "窗口秒"), ("direction", "方向"), ("policy", "退出规则"),
            ("entries", "笔数"), ("positive", "正收益率"), ("exited", "实际卖出数"),
            ("pnl", "平均净收益/份"),
        ]), "",
        "## 0.60最先突破的时间外Chainlink分层（至少5笔）", "",
        *markdown_table([chain_view(row) for row in chain60], [
            ("window", "窗口秒"), ("direction", "方向"), ("feature", "特征"),
            ("band", "方向化支持区间bps"), ("entries", "笔数"), ("win", "胜率"),
            ("pnl", "平均净收益/份"), ("age", "平均对齐年龄ms"),
        ]), "",
        "## 与旧候选A–D的关系", "",
        "- 旧T022候选A–D已经完成：A为0.80–0.85价格带，B为5/15秒动量，C为买入后反转退出，D为跨市场同向与领先。",
        "- 本轮E12的A–D是新的拆解：A严格向上突破、B 90/60秒窗口与力度、C退出、D Chainlink锚定距离。两轮没有互相替代，均保留独立报告。", "",
        "## 解释限制", "",
        "- 本轮严格区分向上突破与回落到阈值；因此它直接回答‘最后90/60秒价格正在涨进0.60或0.60多时买入’。",
        "- 突破来自可执行盘口快照，不要求同一时刻已经发生主动成交；主动成交确认应作为下一层独立过滤，不能混入本轮基线。",
        "- Chainlink对齐是入场前最近固定观察点，不是精确到入场毫秒的重新计算；报告保留对齐年龄，不能把未来观察倒灌到入场时点。",
        "- 所有排序均为探索性；少于100笔不得升级实盘硬条件，并需要按日期滚动做样本外验证。",
        "- 本报告不修改纸上交易或实盘策略。", "",
        "## 复现命令", "", "```bash",
        "PYTHONDONTWRITEBYTECODE=1 python3 scripts/analysis/analyze_strict_upcross_chainlink.py",
        "```",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, default=ROOT / "data/derived/live_strategy_cache.sqlite3")
    parser.add_argument(
        "--chainlink-observations", type=Path,
        default=ROOT / "outputs/research_tests/e12_strict_upcross_20260828/chainlink_e0_refresh/e0_observations.csv",
    )
    parser.add_argument(
        "--output-dir", type=Path,
        default=ROOT / "outputs/research_tests/e12_strict_upcross_20260828",
    )
    args = parser.parse_args()
    chainlink_rows = load_chainlink_rows(args.chainlink_observations)
    connection = sqlite3.connect(args.cache)
    min_snapshot_us, max_snapshot_us = connection.execute(
        "SELECT MIN(source_us),MAX(source_us) FROM snapshots"
    ).fetchone()
    china_tz = timezone(timedelta(hours=8))
    data_range = {
        "startUs": int(min_snapshot_us), "endUs": int(max_snapshot_us),
        "start": datetime.fromtimestamp(int(min_snapshot_us) / 1_000_000, china_tz).strftime("%Y-%m-%d %H:%M:%S"),
        "end": datetime.fromtimestamp(int(max_snapshot_us) / 1_000_000, china_tz).strftime("%Y-%m-%d %H:%M:%S"),
    }
    markets = list(connection.execute(
        "SELECT market_id,end_us,winner FROM markets WHERE winner IN ('UP','DOWN') AND end_us IS NOT NULL ORDER BY end_us"
    ))
    signals: list[dict[str, Any]] = []
    for index, (market_id, end_us, winner) in enumerate(markets, 1):
        snapshots = load_market_snapshots(connection, str(market_id))
        if not snapshots:
            continue
        market_signal_start = len(signals)
        for outcome in ("UP", "DOWN"):
            buy_points = build_buy_points(snapshots, int(end_us), outcome)
            sell_points = build_sell_points(snapshots, int(end_us), outcome)
            for window in WINDOW_SECONDS:
                for threshold in THRESHOLDS:
                    entry = first_strict_upcross(buy_points, threshold, window)
                    if entry is None:
                        continue
                    momentum = five_second_momentum(buy_points, entry)
                    aligned = latest_chainlink_before(chainlink_rows, str(market_id), int(entry["sourceUs"]))
                    chainlink = None
                    if aligned is not None:
                        terminal = signed_for_outcome(Decimal(str(aligned["terminalGapBps"])), outcome)
                        required = signed_for_outcome(Decimal(str(aligned["requiredMarginBps"])), outcome)
                        chainlink = {
                            "observationSourceUs": int(aligned["observationSourceUs"]),
                            "observationTargetSeconds": int(aligned["observationTargetSeconds"]),
                            "alignmentAgeMs": str(aligned["alignmentAgeMs"]),
                            "terminalSupportBps": str(terminal),
                            "terminalSupportBand": chainlink_support_band(terminal),
                            "requiredSupportBps": str(required),
                            "requiredSupportBand": chainlink_support_band(required),
                        }
                    policies = evaluate_exit_policies(
                        entry, sell_points, winner=str(winner), outcome=outcome,
                    )
                    signals.append({
                        "marketId": str(market_id), "windowSeconds": window,
                        "threshold": str(threshold), "outcome": outcome, "winner": str(winner),
                        "entryUs": int(entry["sourceUs"]), "entryRemainingSeconds": str(entry["remaining"]),
                        "priorVwap": str(entry["priorVwap"]), "entryVwap": str(entry["vwap"]),
                        "entryFeePerShare": str(entry["fee"]), "crossGapSeconds": str(entry["crossGapSeconds"]),
                        "crossDelta": str(entry["crossDelta"]),
                        "fiveSecondMomentum": None if momentum is None else str(momentum),
                        "holdPnlPerShare": str(policies["hold_resolution"]["pnl"]),
                        "chainlink": chainlink,
                        "exitPolicies": {
                            policy: {**value, "pnl": str(value["pnl"])}
                            for policy, value in policies.items()
                        },
                    })
        mark_first_side_crosses(signals[market_signal_start:])
        if index % 100 == 0:
            print(f"processed={index}/{len(markets)} signals={len(signals)}")
    connection.close()

    split_info = assign_time_split(signals)
    signal_summaries = summarize_signals(signals)
    exit_summaries = summarize_exits(signals)
    chainlink_summaries = summarize_chainlink(signals)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / "e12_strict_upcross_results.json"
    csv_path = args.output_dir / "e12_strict_upcross_signals.csv"
    report_path = args.output_dir / "E12_严格向上首次突破_退出与Chainlink验证.md"
    payload = {
        "schemaVersion": 1,
        "experiment": "E12_strict_upcross_exit_chainlink",
        "cache": str(args.cache),
        "chainlinkObservations": str(args.chainlink_observations),
        "settledMarkets": len(markets),
        "windows": WINDOW_SECONDS,
        "thresholds": [str(value) for value in THRESHOLDS],
        "maxCrossGapUs": MAX_CROSS_GAP_US,
        "maxOvershoot": str(MAX_OVERSHOOT),
        "dataRange": data_range,
        "timeSplit": split_info,
        "signals": signals,
        "signalSummaries": signal_summaries,
        "exitSummaries": exit_summaries,
        "chainlinkSummaries": chainlink_summaries,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    flat_rows = []
    for signal in signals:
        chainlink = signal.get("chainlink") or {}
        flat_rows.append({
            **{key: value for key, value in signal.items() if key not in {"chainlink", "exitPolicies"}},
            **{f"chainlink_{key}": value for key, value in chainlink.items()},
            **{f"{policy}_pnl": value["pnl"] for policy, value in signal["exitPolicies"].items()},
            **{f"{policy}_trigger": value["trigger"] for policy, value in signal["exitPolicies"].items()},
        })
    if flat_rows:
        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            fieldnames: list[str] = []
            seen: set[str] = set()
            for row in flat_rows:
                for key in row:
                    if key not in seen:
                        seen.add(key)
                        fieldnames.append(key)
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(flat_rows)
    write_report(
        report_path,
        market_count=len(markets), signals=signals, signal_summaries=signal_summaries,
        exit_summaries=exit_summaries, chainlink_summaries=chainlink_summaries,
        chainlink_path=args.chainlink_observations,
        split_info=split_info,
        data_range=data_range,
    )
    print(
        f"markets={len(markets)} signals={len(signals)} signalSummaries={len(signal_summaries)} "
        f"exitSummaries={len(exit_summaries)} chainlinkSummaries={len(chainlink_summaries)}"
    )
    print(json_path.resolve())
    print(report_path.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
