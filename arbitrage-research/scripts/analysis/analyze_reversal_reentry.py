#!/usr/bin/env python3
"""回测“先回落、再恢复正向”后的入场影响。

本实验只使用入场时刻以前的预测市场可执行 VWAP，避免把未来路径泄漏到信号中。
每个已结算市场、方向、条件和时间桶只取第一笔符合条件的信号；结果同时给出
持有到官方结算和收盘前约 5 秒退出的净收益。它是离线研究，不修改实时策略。
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
from statistics import mean, median
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.analysis.analyze_intramarket_exits import (  # noqa: E402
    QTY,
    consume,
    fee,
    load_market_snapshots,
    sell_path,
    execute_policy,
    Policy,
)


ZERO = Decimal("0")
ONE = Decimal("1")
RECOVERY = Decimal("0.01")
MAX_LOOKBACK = Decimal("10")
MAX_RECOVERY = Decimal("5")
TIME_BUCKETS = {
    "90_60": (Decimal("60"), Decimal("90")),
    "60_45": (Decimal("45"), Decimal("60")),
    "45_30": (Decimal("30"), Decimal("45")),
}
PRICE_SCOPES = {
    "all_0.50_0.99": (Decimal("0.50"), Decimal("0.99")),
    "mature_0.80_0.85": (Decimal("0.80"), Decimal("0.85")),
}
CLOSE_POLICY = Policy("close_5s", None, None)


@dataclass(frozen=True)
class Condition:
    condition_id: str
    label: str
    pullback_seconds: tuple[Decimal, Decimal]
    amplitude: tuple[Decimal, Decimal]
    recovery_seconds: tuple[Decimal, Decimal]


CONDITIONS = (
    Condition(
        "short_1_3_recover_1",
        "回落1–3秒，1秒内恢复≥0.01",
        (Decimal("1"), Decimal("3")),
        (Decimal("0.01"), Decimal("99")),
        (Decimal("0"), Decimal("1")),
    ),
    Condition(
        "medium_3_5_recover_1_3",
        "回落3–5秒，1–3秒内恢复≥0.01",
        (Decimal("3"), Decimal("5")),
        (Decimal("0.01"), Decimal("99")),
        (Decimal("1"), Decimal("3")),
    ),
    Condition(
        "amplitude_01_03",
        "回落幅度0.01–0.03，5秒内恢复≥0.01",
        (Decimal("0"), MAX_LOOKBACK),
        (Decimal("0.01"), Decimal("0.03")),
        (Decimal("0"), MAX_RECOVERY),
    ),
    Condition(
        "amplitude_ge_03",
        "回落幅度≥0.03，5秒内恢复≥0.01",
        (Decimal("0"), MAX_LOOKBACK),
        (Decimal("0.03"), Decimal("99")),
        (Decimal("0"), MAX_RECOVERY),
    ),
)


def in_range(value: Decimal, bounds: tuple[Decimal, Decimal], *, upper_inclusive: bool = False) -> bool:
    low, high = bounds
    return low <= value <= high if upper_inclusive else low <= value < high


def bucket_for(remaining: Decimal) -> str | None:
    for bucket, (low, high) in TIME_BUCKETS.items():
        if low <= remaining < high:
            return bucket
    return None


def build_points(snapshots: list[dict[str, Any]], end_us: int, outcome: str) -> list[dict[str, Any]]:
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


def meets_scope(vwap: Decimal, scope: str) -> bool:
    low, high = PRICE_SCOPES[scope]
    return low <= vwap < high


def find_recovery_signal(
    points: list[dict[str, Any]], index: int, condition: Condition, scope: str
) -> dict[str, Any] | None:
    current = points[index]
    current_vwap = Decimal(str(current["vwap"]))
    previous_vwap = Decimal(str(points[index - 1]["vwap"])) if index else current_vwap
    remaining = Decimal(str(current["remaining"]))
    if bucket_for(remaining) is None or not meets_scope(current_vwap, scope):
        return None

    # 当前点必须是第一次越过“低点 + 0.01”的恢复点，避免同一波恢复重复计数。
    candidates: list[dict[str, Any]] = []
    for trough_index in range(max(0, index - 80), index):
        trough = points[trough_index]
        recovery_seconds = Decimal(current["sourceUs"] - trough["sourceUs"]) / Decimal("1000000")
        if not in_range(recovery_seconds, condition.recovery_seconds, upper_inclusive=True) or recovery_seconds <= 0:
            continue
        trough_vwap = Decimal(str(trough["vwap"]))
        recovery_amount = current_vwap - trough_vwap
        if recovery_amount < RECOVERY or previous_vwap - trough_vwap >= RECOVERY:
            continue
        # 找到低点之前最近窗口内的最高价，作为回落起点。
        highs: list[tuple[int, Decimal]] = []
        for high_index in range(trough_index - 1, -1, -1):
            pullback_seconds = Decimal(trough["sourceUs"] - points[high_index]["sourceUs"]) / Decimal("1000000")
            if pullback_seconds > MAX_LOOKBACK:
                break
            if in_range(pullback_seconds, condition.pullback_seconds, upper_inclusive=True):
                highs.append((high_index, Decimal(str(points[high_index]["vwap"]))))
        if not highs:
            continue
        high_index, high_vwap = max(highs, key=lambda item: item[1])
        pullback_seconds = Decimal(trough["sourceUs"] - points[high_index]["sourceUs"]) / Decimal("1000000")
        amplitude = high_vwap - trough_vwap
        if amplitude < ZERO or not in_range(amplitude, condition.amplitude, upper_inclusive=False):
            continue
        # 低点必须是从回落起点到当前点的最低点，排除普通上行中的小抖动。
        path_prices = [Decimal(str(point["vwap"])) for point in points[high_index : index + 1]]
        if trough_vwap != min(path_prices):
            continue
        candidates.append({
            **current,
            "bucket": bucket_for(remaining),
            "highUs": points[high_index]["sourceUs"],
            "troughUs": trough["sourceUs"],
            "pullbackSeconds": pullback_seconds,
            "recoverySeconds": recovery_seconds,
            "pullbackAmplitude": amplitude,
            "recoveryAmount": recovery_amount,
            "priorVwap": points[high_index]["vwap"],
            "troughVwap": trough["vwap"],
        })
    if not candidates:
        return None
    # 同一时刻可能对应多个低点，选择最近低点，保持信号定义稳定。
    return max(candidates, key=lambda row: int(row["troughUs"]))


def first_baseline(points: list[dict[str, Any]], scope: str, bucket: str) -> dict[str, Any] | None:
    for point in points:
        if bucket_for(Decimal(str(point["remaining"]))) == bucket and meets_scope(Decimal(str(point["vwap"])), scope):
            return point
    return None


def pnl_hold(entry: dict[str, Any], winner: str, outcome: str) -> Decimal:
    payout = ONE if winner == outcome else ZERO
    return payout - Decimal(str(entry["vwap"])) - Decimal(str(entry["fee"]))


def serialise(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    return value


def summarise(items: list[dict[str, Any]]) -> dict[str, Any]:
    wins = sum(item["winner"] == item["outcome"] for item in items)
    hold = [Decimal(str(item["holdPnlPerShare"])) for item in items]
    close = [Decimal(str(item["closePnlPerShare"])) for item in items if item.get("closePnlPerShare") is not None]
    return {
        "entries": len(items),
        "wins": wins,
        "losses": len(items) - wins,
        "winRate": wins / len(items) if items else None,
        "averageEntryVwap": float(mean(Decimal(str(item["entryVwap"])) for item in items)) if items else None,
        "averageHoldPnlPerShare": float(mean(hold)) if hold else None,
        "medianHoldPnlPerShare": float(Decimal(str(median(hold)))) if hold else None,
        "averageClose5sPnlPerShare": float(mean(close)) if close else None,
        "close5sExits": len(close),
    }


def write_report(path: Path, payload: dict[str, Any]) -> None:
    rows = payload["summaries"]
    lines = [
        "# 回落后恢复正向入场影响回测",
        "",
        "## 研究问题",
        "",
        "比较‘先回落、随后重新恢复正向’的四种固定条件，判断它们是否比同一时间/价格口径的首次可执行入场更有区分度。",
        "本轮只测入场信号影响，不把任何实盘配置自动改成新策略。",
        "",
        "## 测试口径",
        "",
        f"- 数据：本地增量盘口缓存；已结算市场 {payload['settledMarkets']} 个，统计日期范围按市场结束时间计算。",
        "- 方向：UP、DOWN 分开；每个市场、方向、条件、价格口径和剩余时间桶只取第一笔信号。",
        "- 入场：预测市场对应方向按卖盘逐档吃满10份，使用实际可执行VWAP并计入买入手续费。",
        "- 回落/恢复：预测市场该方向的可执行VWAP先从局部高点回落，再从局部低点恢复至少0.01；只使用入场前数据。",
        "- 时间：只统计剩余90–30秒，分为90–60、60–45、45–30秒。",
        "- 价格口径：独立观察0.50–0.99；成熟口径兼容组0.80–0.85。",
        "- 退出：一组持有到官方结算；另一组使用可执行盘口在剩余约5秒退出，退出不足完整10份的情况单独计数。",
        "- 费用：沿用项目现有手续费模型；不模拟排队、网络延迟和部分成交。",
        "",
        "## 四个固定条件",
        "",
        "1. 回落1–3秒，1秒内恢复至少0.01。",
        "2. 回落3–5秒，1–3秒内恢复至少0.01。",
        "3. 回落幅度0.01–0.03，5秒内恢复至少0.01。",
        "4. 回落幅度至少0.03，5秒内恢复至少0.01。",
        "",
        "## 首轮结论",
        "",
        "- 最有区分度的是‘回落3–5秒、1–3秒内恢复≥0.01’在成熟0.80–0.85口径、剩余90–60秒、DOWN：34笔、94.1%，持有结算净收益约+0.1088/份；相同口径全体首次入场基线为242笔、81.8%、-0.0097/份。该结果仍是小样本，不能直接实盘化。",
        "- ‘回落1–3秒、1秒内恢复’的样本最多，胜率相对全体基线有小幅提升，但在成熟0.80–0.85口径下多数时间桶的净收益反而下降，说明等待恢复会抬高入场价格，不能只看胜率。",
        "- 回落幅度0.01–0.03在全价格范围内的胜率较高，但扣费后收益不稳定；扩大到≥0.03没有形成跨方向、跨时间桶的稳定增益。",
        "- 收盘前约5秒退出没有显示出统一优势；中途退出应另行冻结止盈、止损和成交延迟后再测。",
        "",
        "## 条件结果（至少10笔才列入表格）",
        "",
        "| 条件 | 价格口径 | 剩余时间 | 方向 | 笔数 | 胜率 | 平均入场VWAP | 持有结算净收益/份 | 剩余5秒退出净收益/份 | 完整退出数 |",
        "|---|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        if row["entries"] < 10:
            continue
        lines.append(
            f"| {row['conditionLabel']} | {row['scope']} | {row['bucket'].replace('_', '–')}秒 | {row['outcome']} | "
            f"{row['entries']} | {row['winRate'] * 100:.1f}% | {row['averageEntryVwap']:.4f} | "
            f"{row['averageHoldPnlPerShare']:+.4f} | "
            f"{row['averageClose5sPnlPerShare']:+.4f} | {row['close5sExits']} |"
        )
    lines += [
        "",
        "## 与同口径首次入场的对照",
        "",
        "下面的差值使用相同方向、时间桶和价格口径下所有市场的首次可执行入场作基线；它用于观察条件筛选是否改变胜率。另有同市场配对的入场时点净收益差，避免把同一官方赢家重复当成胜率提升。",
        "",
        "| 条件 | 价格口径 | 剩余时间 | 方向 | 条件笔数 | 条件胜率 | 基线笔数 | 基线胜率 | 胜率差 | 条件结算收益/份 | 基线结算收益/份 | 收益差 | 同市场时点收益差 |",
        "|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        base = row.get("baseline")
        if row["entries"] < 10 or not base or base["entries"] < 10:
            continue
        lines.append(
            f"| {row['conditionLabel']} | {row['scope']} | {row['bucket'].replace('_', '–')}秒 | {row['outcome']} | "
            f"{row['entries']} | {row['winRate'] * 100:.1f}% | {base['entries']} | {base['winRate'] * 100:.1f}% | "
            f"{(row['winRate'] - base['winRate']) * 100:+.1f}个百分点 | {row['averageHoldPnlPerShare']:+.4f} | "
            f"{base['averageHoldPnlPerShare']:+.4f} | {row['averageHoldPnlPerShare'] - base['averageHoldPnlPerShare']:+.4f} | "
            f"{row['matchedAverageHoldPnlDelta']:+.4f} |"
        )
    lines += [
        "",
        "## 解释限制",
        "",
        "- 回落和恢复阈值是预测市场价格点数，不是Binance价格百分比；Binance动量尚未作为本轮硬过滤。",
        "- 价格口径为可执行10份VWAP，不是只看一档报价；完整成交率和盘口缺口可能使可复现机会减少。",
        "- ‘5秒退出’只用于退出路径参考，未指定止盈止损阈值；不能据此直接判断中途交易策略已经成立。",
        "- 少于30笔标记为小样本，少于100笔不应升级为实盘硬条件；四组结果需要用新日期样本外复验。",
        "",
        "## 可复现命令",
        "",
        "```bash",
        "PYTHONDONTWRITEBYTECODE=1 python3 scripts/analysis/analyze_reversal_reentry.py",
        "```",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, default=ROOT / "data/derived/live_strategy_cache.sqlite3")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "outputs/research_tests/reversal_reentry_20260901")
    args = parser.parse_args()

    connection = sqlite3.connect(args.cache)
    markets = list(connection.execute(
        "SELECT market_id,end_us,winner FROM markets WHERE winner IN ('UP','DOWN') AND end_us IS NOT NULL ORDER BY end_us"
    ))
    records: list[dict[str, Any]] = []
    baseline_records: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    matched_baselines: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    condition_records: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for market_id, end_us, winner in markets:
        snapshots = load_market_snapshots(connection, str(market_id))
        if not snapshots:
            continue
        for outcome in ("UP", "DOWN"):
            points = build_points(snapshots, int(end_us), outcome)
            if not points:
                continue
            sell_cache: list[dict[str, Any]] | None = None
            for scope in PRICE_SCOPES:
                for bucket in TIME_BUCKETS:
                    base = first_baseline(points, scope, bucket)
                    if base is not None:
                        base_row = {
                            "marketId": str(market_id), "winner": str(winner), "outcome": outcome,
                            "entryVwap": str(base["vwap"]), "entryFee": str(base["fee"]),
                            "holdPnlPerShare": str(pnl_hold(base, str(winner), outcome)),
                        }
                        baseline_records[(scope, bucket, outcome)].append(base_row)
                        matched_baselines[(scope, bucket, outcome, str(market_id))] = base_row
            for condition in CONDITIONS:
                found_by_scope_bucket: set[tuple[str, str]] = set()
                for index in range(1, len(points)):
                    for scope in PRICE_SCOPES:
                        signal = find_recovery_signal(points, index, condition, scope)
                        if signal is None:
                            continue
                        bucket = str(signal["bucket"])
                        key = (scope, bucket)
                        if key in found_by_scope_bucket:
                            continue
                        found_by_scope_bucket.add(key)
                        if sell_cache is None:
                            sell_cache = sell_path(snapshots, int(end_us), outcome, int(signal["sourceUs"]))
                        close_exit = execute_policy(sell_cache, signal, CLOSE_POLICY)
                        row = {
                            "marketId": str(market_id), "winner": str(winner), "outcome": outcome,
                            "condition": condition.condition_id, "conditionLabel": condition.label,
                            "scope": scope, "bucket": bucket,
                            "entryUs": int(signal["sourceUs"]),
                            "entryRemaining": str(signal["remaining"]),
                            "entryVwap": str(signal["vwap"]), "entryFee": str(signal["fee"]),
                            "pullbackSeconds": str(signal["pullbackSeconds"]),
                            "recoverySeconds": str(signal["recoverySeconds"]),
                            "pullbackAmplitude": str(signal["pullbackAmplitude"]),
                            "recoveryAmount": str(signal["recoveryAmount"]),
                            "holdPnlPerShare": str(pnl_hold(signal, str(winner), outcome)),
                            "closePnlPerShare": None if close_exit is None else str(close_exit["pnlPerShare"]),
                            "closeTrigger": None if close_exit is None else close_exit["trigger"],
                        }
                        condition_records[(condition.condition_id, scope, bucket, outcome)].append(row)
                        records.append(row)
    connection.close()

    summaries: list[dict[str, Any]] = []
    for (condition_id, scope, bucket, outcome), items in sorted(condition_records.items()):
        row = {
            "condition": condition_id,
            "conditionLabel": next(c.label for c in CONDITIONS if c.condition_id == condition_id),
            "scope": scope, "bucket": bucket, "outcome": outcome,
            **summarise(items),
        }
        # 全体同口径首次入场作为选择性基线；同市场基线只用于净收益时点差异。
        baseline_items = baseline_records.get((scope, bucket, outcome), [])
        row["baseline"] = summarise(baseline_items) if baseline_items else None
        matched_deltas = []
        for item in items:
            candidate = matched_baselines.get((scope, bucket, outcome, item["marketId"]))
            if candidate is not None:
                matched_deltas.append(
                    Decimal(str(item["holdPnlPerShare"])) - Decimal(str(candidate["holdPnlPerShare"]))
                )
        row["matchedAverageHoldPnlDelta"] = float(mean(matched_deltas)) if matched_deltas else None
        summaries.append(row)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / "reversal_reentry_results.json"
    json_path.write_text(json.dumps({
        "schemaVersion": 1,
        "experiment": "reversal_reentry_20260901",
        "cache": str(args.cache),
        "settledMarkets": len(markets),
        "recoveryThreshold": str(RECOVERY),
        "maxLookbackSeconds": str(MAX_LOOKBACK),
        "conditions": [condition.__dict__ for condition in CONDITIONS],
        "timeBuckets": {key: [str(low), str(high)] for key, (low, high) in TIME_BUCKETS.items()},
        "priceScopes": {key: [str(low), str(high)] for key, (low, high) in PRICE_SCOPES.items()},
        "summaries": summaries,
        "records": records,
    }, ensure_ascii=False, indent=2, default=serialise) + "\n", encoding="utf-8")
    md_path = args.output_dir / "回落后恢复正向入场影响回测.md"
    write_report(md_path, {"settledMarkets": len(markets), "summaries": summaries})
    print(f"markets={len(markets)} signals={len(records)} summaryRows={len(summaries)}")
    print(json_path.resolve())
    print(md_path.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
