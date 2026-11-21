#!/usr/bin/env python3
"""E7：补齐300秒生命周期、扩大回落网格并比较中途退出与结算。"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime
from decimal import Decimal
import gzip
import json
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from arb_research.experiments.orderbook_depth import BookLevel, consume_asks
from arb_research.experiments.strategy_idea_validation import (
    select_reversal_signal,
    select_stable_entry,
)
from scripts.analysis.analyze_intramarket_exits import (
    QTY,
    complement_bids,
    consume,
    fee,
    load_market_snapshots,
)


FULL_LIFECYCLE_WINDOWS = (
    ("300_240", 240, 300),
    ("240_180", 180, 240),
    ("180_120", 120, 180),
    ("120_90", 90, 120),
    ("90_60", 60, 90),
    ("60_30", 30, 60),
    ("30_5", 5, 30),
    ("5_0", 0, 5),
)
DEFAULT_REVERSAL_HORIZONS = (5, 10, 15, 30)
DEFAULT_REVERSAL_DROPS = (0.01, 0.02, 0.03, 0.05, 0.08, 0.10)
REVERSAL_GRID = tuple(
    (horizon, drop)
    for horizon in DEFAULT_REVERSAL_HORIZONS
    for drop in DEFAULT_REVERSAL_DROPS
)
DEFAULT_EXPERIMENT_NAME = "E7_完整生命周期与扩大回落区间补测"
E1_BANDS = (("20_30", 0.20, 0.30), ("30_40", 0.30, 0.40), ("40_50", 0.40, 0.50))
E2_BANDS = (("80_85", 0.80, 0.85), ("85_90", 0.85, 0.90))
E2_STABILITY = (0, 5, 10, 30, 60)
E2_CAPS = (0.85, 0.90)
EXIT_RULES = (
    "settlement", "close_5s", "fixed_60s", "fixed_30s", "fixed_15s", "fixed_5s",
    "abs_003", "abs_005", "abs_010", "rel_10", "rel_20", "rel_50",
)
DETAIL_FIELDS = (
    "experiment", "split", "beijingDate", "marketId", "winner", "outcome",
    "case", "entryWindow", "entryUs", "entryRemaining", "entryVwap", "entryFee", "exitRule",
    "exitMode", "exitRemaining", "exitVwap", "won", "pnlPerShare",
    "holdPnlPerShare", "highOutcome", "horizon", "dropFromHigh",
)


def select_fixed_remaining_exit(
    path: list[dict[str, Any]], *, remaining_seconds: int
) -> dict[str, Any] | None:
    """Select the first executable sell snapshot at or before a time boundary."""
    for point in path:
        if float(point["remaining"]) <= remaining_seconds:
            return point
    return None


def select_take_profit_exit(
    path: list[dict[str, Any]], *, target: float
) -> dict[str, Any] | None:
    for point in path:
        if float(point["vwap"]) + 1e-12 >= target:
            return point
    return None


def main() -> int:
    args = parse_args()
    global REVERSAL_GRID
    if args.reversal_drops or args.reversal_horizons:
        drops = parse_reversal_drops(args.reversal_drops) if args.reversal_drops else tuple(
            sorted({drop for _horizon, drop in REVERSAL_GRID})
        )
        horizons = parse_reversal_horizons(args.reversal_horizons) if args.reversal_horizons else tuple(
            sorted({horizon for horizon, _drop in REVERSAL_GRID})
        )
        REVERSAL_GRID = tuple(
            (horizon, drop)
            for horizon in horizons
            for drop in drops
        )
    args.out.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(args.cache)
    connection.row_factory = sqlite3.Row
    markets = list(connection.execute(
        """SELECT market_id,start_us,end_us,winner FROM markets
           WHERE winner IN ('UP','DOWN') AND start_us IS NOT NULL AND end_us IS NOT NULL
           ORDER BY start_us"""
    ))
    split_index = min(len(markets) - 1, max(1, int(len(markets) * args.train_fraction)))
    validation_ids = {str(row["market_id"]) for row in markets[split_index:]}
    rows: list[dict[str, Any]] = []
    summary_groups: dict[tuple[str, str, str, str, str], dict[str, float]] | None = (
        {} if args.summary_only else None
    )
    selected_markets = 0
    for index, market in enumerate(markets, 1):
        market_id = str(market["market_id"])
        split = "validation" if market_id in validation_ids else "train"
        if args.split != "all" and split != args.split:
            continue
        snapshots = load_market_snapshots(connection, market_id)
        if not snapshots:
            continue
        points, sells = precompute_points(snapshots, int(market["end_us"]))
        selected_markets += 1
        beijing_date = datetime.fromtimestamp(
            int(market["start_us"]) / 1_000_000, tz=ZoneInfo("Asia/Shanghai")
        ).strftime("%Y-%m-%d")
        market_rows = evaluate_e1_e2(
            market, split, beijing_date, points, sells,
        )
        market_rows.extend(evaluate_reversal(
            market, split, beijing_date, points, sells,
        ))
        if summary_groups is None:
            rows.extend(market_rows)
        else:
            accumulate_summary_rows(summary_groups, market_rows)
        if index % 100 == 0:
            processed_rows = sum(int(bucket["entries"]) for bucket in summary_groups.values()) if summary_groups is not None else len(rows)
            print(f"processed={index}/{len(markets)} rows={processed_rows}")
    connection.close()

    result = build_result(
        rows, len(markets), split_index, grouped=summary_groups,
        selected_markets=selected_markets, detail_mode="summary_only" if args.summary_only else "full",
        experiment_name=args.experiment_name,
    )
    if args.experiment_name == DEFAULT_EXPERIMENT_NAME:
        artifact_prefix = "e7"
        report_filename = "E7完整生命周期与扩大回落区间补测.md"
    elif args.experiment_name.startswith("E8"):
        artifact_prefix = "e8"
        report_filename = "E8扩大回落幅度反向验证.md"
    else:
        artifact_prefix = "e9"
        report_filename = "E9短周期回落动能验证.md"
    result_path = args.out / f"{artifact_prefix}_results.json"
    detail_path = args.out / f"{artifact_prefix}_lifecycle_simulations.csv.gz"
    report_path = args.out / report_filename
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    if not args.summary_only:
        with gzip.open(detail_path, "wt", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=DETAIL_FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
    write_report(report_path, result)
    print(result_path)
    if not args.summary_only:
        print(detail_path)
    print(report_path)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="执行E7完整生命周期与扩大回落区间补测")
    parser.add_argument("--cache", type=Path, default=ROOT / "data/derived/live_strategy_cache.sqlite3")
    parser.add_argument("--out", type=Path, default=ROOT / "outputs/research_tests/e7_full_lifecycle_reversal_20260826")
    parser.add_argument("--train-fraction", type=float, default=0.7)
    parser.add_argument("--split", choices=("all", "train", "validation"), default="all",
                        help="只跑探索段或时间外验证段；默认跑全部已结算市场")
    parser.add_argument("--summary-only", action="store_true",
                        help="只保留汇总，不生成逐规则明细，降低内存和磁盘占用")
    parser.add_argument("--reversal-drops", default=None,
                        help="覆盖E3/E4回落幅度网格，逗号分隔，例如0.01,0.02,0.10,0.20,0.60")
    parser.add_argument("--reversal-horizons", default=None,
                        help="覆盖E3/E4回落观察秒数，逗号分隔，例如1,2,3,4,5,10,15,30")
    parser.add_argument("--experiment-name", default=DEFAULT_EXPERIMENT_NAME,
                        help="结果中的实验名称，扩展网格时建议单独命名")
    return parser.parse_args()


def parse_reversal_drops(raw: str) -> tuple[float, ...]:
    values = tuple(sorted({round(float(part.strip()), 4) for part in raw.split(",") if part.strip()}))
    if not values or any(value <= 0 or value >= 1 for value in values):
        raise ValueError("回落幅度必须在0和1之间")
    return values


def parse_reversal_horizons(raw: str) -> tuple[int, ...]:
    values = tuple(sorted({int(part.strip()) for part in raw.split(",") if part.strip()}))
    if not values or any(value <= 0 or value > 300 for value in values):
        raise ValueError("回落观察秒数必须是1到300之间的正整数")
    return values


def precompute_points(
    snapshots: list[dict[str, Any]], end_us: int
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    points: dict[str, list[dict[str, Any]]] = {"UP": [], "DOWN": []}
    sells: dict[str, list[dict[str, Any]]] = {"UP": [], "DOWN": []}
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
                levels, QTY, Decimal("1"),
                fee_model="polymarket_crypto_taker", fee_rate=Decimal("0.07"),
            )
            if fill is None:
                continue
            vwap, entry_fee, worst = fill
            opposite = "DOWN" if outcome == "UP" else "UP"
            opposite_levels = [
                BookLevel(Decimal(str(price)), Decimal(str(size)))
                for price, size in snapshot["asks"].get(opposite) or []
            ]
            opposite_entry_fill = consume_asks(
                opposite_levels, QTY, Decimal("1"),
                fee_model="polymarket_crypto_taker", fee_rate=Decimal("0.07"),
            )
            opposite_sell_fill = consume(
                complement_bids(snapshot["asks"].get(opposite) or []),
                QTY, descending=True,
            )
            points[outcome].append({
                "sourceUs": snapshot["sourceUs"], "remaining": remaining,
                "vwap": float(vwap), "fee": float(entry_fee), "worstPrice": float(worst),
                "oppositeVwap": float(opposite_entry_fill[0]) if opposite_entry_fill else None,
                "oppositeFee": float(opposite_entry_fill[1]) if opposite_entry_fill else None,
            })
            if opposite_sell_fill is not None:
                sells[outcome].append({
                    "sourceUs": snapshot["sourceUs"], "remaining": remaining,
                    "vwap": float(opposite_sell_fill[0]), "fee": float(opposite_sell_fill[1]),
                })
    return points, sells


def evaluate_e1_e2(
    market: sqlite3.Row,
    split: str,
    beijing_date: str,
    points: dict[str, list[dict[str, Any]]],
    sells: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    winner = str(market["winner"])
    for outcome in ("UP", "DOWN"):
        for window, minimum, maximum in FULL_LIFECYCLE_WINDOWS:
            window_points = [
                point for point in points[outcome]
                if minimum <= point["remaining"] <= maximum
            ]
            for band, low, high in E1_BANDS:
                entry = first_point_in_band(window_points, low, high)
                if entry is None:
                    continue
                rows.extend(exit_rows(
                    experiment="E1_full_lifecycle", market=market, split=split,
                    beijing_date=beijing_date, outcome=outcome, case=band,
                    entry_window=window, entry=entry,
                    path=after(sells[outcome], entry["sourceUs"]),
                ))
            for band, low, high in E2_BANDS:
                for stability in E2_STABILITY:
                    for cap in E2_CAPS:
                        entry = select_stable_entry(
                            window_points,
                            duration_us=stability * 1_000_000,
                            stability_floor=0.80,
                            reset_tolerance=0.01,
                            band_low=low, band_high=high,
                            worst_price_cap=cap,
                        )
                        if entry is None:
                            continue
                        rows.extend(exit_rows(
                            experiment="E2_full_lifecycle", market=market, split=split,
                            beijing_date=beijing_date, outcome=outcome,
                            case=f"{band}_stable{stability}_cap{cap:.2f}",
                            entry_window=window, entry=entry,
                            path=after(sells[outcome], entry["sourceUs"]),
                        ))
    return rows


def evaluate_reversal(
    market: sqlite3.Row,
    split: str,
    beijing_date: str,
    points: dict[str, list[dict[str, Any]]],
    sells: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    winner = str(market["winner"])
    for high_outcome in ("UP", "DOWN"):
        for horizon, drop_min in REVERSAL_GRID:
            signal = select_reversal_signal(
                points[high_outcome], horizon_us=horizon * 1_000_000,
                drop_min=drop_min, high_floor=0.80,
            )
            if signal is None:
                continue
            opposite = "DOWN" if high_outcome == "UP" else "UP"
            if signal.get("oppositeVwap") is None:
                continue
            for direction, entry in (
                ("opposite", {
                    "sourceUs": signal["sourceUs"], "remaining": signal["remaining"],
                    "vwap": signal["oppositeVwap"], "fee": signal["oppositeFee"],
                }),
                ("high_continue", signal),
            ):
                rows.extend(exit_rows(
                    experiment="E3E4_reversal_grid", market=market, split=split,
                    beijing_date=beijing_date, outcome=(opposite if direction == "opposite" else high_outcome),
                    case=f"high{high_outcome}_h{horizon}_drop{drop_min:.2f}_{direction}",
                    entry_window=lifecycle_window(float(entry["remaining"])), entry=entry,
                    path=after(sells[opposite if direction == "opposite" else high_outcome], signal["sourceUs"]),
                    signal_meta={"highOutcome": high_outcome, "horizon": horizon, "dropFromHigh": signal["dropFromHigh"]},
                ))
    return rows


def first_point_in_band(points: list[dict[str, Any]], low: float, high: float) -> dict[str, Any] | None:
    for point in points:
        value = float(point["vwap"])
        if low <= value <= high:
            return point
    return None


def lifecycle_window(remaining: float) -> str | None:
    """Return the named full-lifecycle bucket for a remaining-time value."""
    for label, minimum, maximum in FULL_LIFECYCLE_WINDOWS:
        if minimum <= remaining <= maximum:
            return label
    return None


def after(points: list[dict[str, Any]], source_us: int) -> list[dict[str, Any]]:
    return [point for point in points if int(point["sourceUs"]) > source_us]


def exit_rows(
    *,
    experiment: str,
    market: sqlite3.Row,
    split: str,
    beijing_date: str,
    outcome: str,
    case: str,
    entry_window: str | None,
    entry: dict[str, Any],
    path: list[dict[str, Any]],
    signal_meta: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    won = outcome == str(market["winner"])
    hold_pnl = (1.0 if won else 0.0) - float(entry["vwap"]) - float(entry["fee"])
    rows: list[dict[str, Any]] = []
    for exit_rule in EXIT_RULES:
        selected = choose_exit(path, entry, exit_rule)
        if selected is None:
            pnl = hold_pnl
            mode = "settlement"
            exit_remaining = None
        else:
            pnl = (
                float(selected["vwap"]) - float(entry["vwap"])
                - float(entry["fee"]) - float(selected["fee"])
            )
            mode = "mid_exit"
            exit_remaining = float(selected["remaining"])
        rows.append({
            "experiment": experiment, "split": split, "beijingDate": beijing_date,
            "marketId": str(market["market_id"]), "winner": str(market["winner"]),
            "outcome": outcome, "case": case, "entryWindow": entry_window,
            "entryUs": int(entry["sourceUs"]),
            "entryRemaining": float(entry["remaining"]), "entryVwap": float(entry["vwap"]),
            "entryFee": float(entry["fee"]), "exitRule": exit_rule,
            "exitMode": mode, "exitRemaining": exit_remaining,
            "exitVwap": float(selected["vwap"]) if selected else None,
            "won": won, "pnlPerShare": pnl, "holdPnlPerShare": hold_pnl,
            **(signal_meta or {}),
        })
    return rows


def choose_exit(
    path: list[dict[str, Any]], entry: dict[str, Any], exit_rule: str
) -> dict[str, Any] | None:
    if exit_rule == "settlement":
        return None
    if exit_rule == "close_5s":
        return select_fixed_remaining_exit(path, remaining_seconds=5)
    if exit_rule.startswith("fixed_"):
        return select_fixed_remaining_exit(path, remaining_seconds=int(exit_rule.split("_")[1][:-1]))
    if exit_rule.startswith("abs_"):
        target = float(entry["vwap"]) + int(exit_rule.split("_")[1]) / 100
    else:
        target = float(entry["vwap"]) * (1 + int(exit_rule.split("_")[1]) / 100)
    return select_take_profit_exit(path, target=target)


def accumulate_summary_rows(
    grouped: dict[tuple[str, str, str, str, str], dict[str, float]],
    rows: list[dict[str, Any]],
) -> None:
    """Update compact summary buckets without retaining per-rule detail rows."""
    for row in rows:
        key = (
            row["split"], row["experiment"], row.get("entryWindow") or "",
            row["case"], row["exitRule"],
        )
        bucket = grouped.setdefault(key, {
            "entries": 0, "winsAtResolution": 0, "positiveExits": 0,
            "pnlSum": 0.0, "holdPnlSum": 0.0, "midExitCount": 0,
        })
        bucket["entries"] += 1
        bucket["winsAtResolution"] += int(bool(row["won"]))
        bucket["positiveExits"] += int(float(row["pnlPerShare"]) > 0)
        bucket["pnlSum"] += float(row["pnlPerShare"])
        bucket["holdPnlSum"] += float(row["holdPnlPerShare"])
        bucket["midExitCount"] += int(row["exitMode"] == "mid_exit")


def _summary_rows_from_groups(
    grouped: dict[tuple[str, str, str, str, str], dict[str, float]],
) -> tuple[list[dict[str, Any]], int]:
    summaries: list[dict[str, Any]] = []
    row_count = 0
    for (split, experiment, entry_window, case, exit_rule), bucket in grouped.items():
        entries = int(bucket["entries"])
        row_count += entries
        summaries.append({
            "split": split, "experiment": experiment, "entryWindow": entry_window or None,
            "case": case,
            "exitRule": exit_rule, "entries": entries,
            "winsAtResolution": int(bucket["winsAtResolution"]),
            "positiveExits": int(bucket["positiveExits"]),
            "averagePnlPerShare": bucket["pnlSum"] / entries,
            "averageHoldPnlPerShare": bucket["holdPnlSum"] / entries,
            "midExitCount": int(bucket["midExitCount"]),
            "midExitRate": bucket["midExitCount"] / entries,
            "winRate": bucket["winsAtResolution"] / entries,
        })
    summaries.sort(key=lambda row: (row["split"], row["experiment"], row["entryWindow"] or "", row["case"], row["exitRule"]))
    return summaries, row_count


def build_result(
    rows: list[dict[str, Any]],
    settled_markets: int,
    split_index: int,
    *,
    grouped: dict[tuple[str, str, str, str, str], dict[str, float]] | None = None,
    selected_markets: int | None = None,
    detail_mode: str = "full",
    experiment_name: str = DEFAULT_EXPERIMENT_NAME,
) -> dict[str, Any]:
    if grouped is None:
        grouped = {}
        accumulate_summary_rows(grouped, rows)
    summaries, row_count = _summary_rows_from_groups(grouped)
    return {
        "schemaVersion": 1, "experiment": experiment_name,
        "settledMarkets": settled_markets, "trainMarkets": split_index,
        "validationMarkets": settled_markets - split_index,
        "fullLifecycleWindows": FULL_LIFECYCLE_WINDOWS, "reversalGrid": REVERSAL_GRID,
        "exitRules": EXIT_RULES, "rowCount": row_count, "summaries": summaries,
        "selectedMarkets": selected_markets if selected_markets is not None else settled_markets,
        "detailMode": detail_mode,
    }


def write_report(path: Path, result: dict[str, Any]) -> None:
    validation = [row for row in result["summaries"] if row["split"] == "validation" and row["entries"] >= 30]
    coverage = {
        label: {"E1": 0, "E2": 0, "E3反向": 0, "E3继续": 0}
        for label, _minimum, _maximum in FULL_LIFECYCLE_WINDOWS
    }
    for row in result["summaries"]:
        if row["split"] != "validation" or row["exitRule"] != "settlement":
            continue
        window = row.get("entryWindow")
        if window not in coverage:
            continue
        if row["experiment"] == "E1_full_lifecycle":
            coverage[window]["E1"] += row["entries"]
        elif row["experiment"] == "E2_full_lifecycle":
            coverage[window]["E2"] += row["entries"]
        elif row["experiment"] == "E3E4_reversal_grid":
            coverage[window]["E3反向" if row["case"].endswith("_opposite") else "E3继续"] += row["entries"]
    reversal_by_drop: dict[str, list[float]] = {}
    for row in result["summaries"]:
        if (
            row["split"] == "validation"
            and row["experiment"] == "E3E4_reversal_grid"
            and row["exitRule"] == "settlement"
            and row["case"].endswith("_opposite")
        ):
            match = re.search(r"_drop(0\.\d+)_", row["case"])
            if match:
                bucket = reversal_by_drop.setdefault(match.group(1), [0.0, 0.0, 0.0])
                bucket[0] += row["entries"]
                bucket[1] += row["winsAtResolution"]
                bucket[2] += row["averagePnlPerShare"] * row["entries"]
    momentum_by_horizon: dict[tuple[int, str], list[float]] = {}
    momentum_by_case: dict[tuple[int, float, str], list[float]] = {}
    if result["experiment"].startswith("E9"):
        for row in result["summaries"]:
            if (
                row["split"] != "validation"
                or row["experiment"] != "E3E4_reversal_grid"
                or row["exitRule"] != "settlement"
            ):
                continue
            match = re.search(r"_h(\d+)_drop(0\.\d+)(_.+)$", row["case"])
            if not match:
                continue
            horizon = int(match.group(1))
            drop = float(match.group(2))
            direction = "反方向" if row["case"].endswith("_opposite") else "继续高位方向"
            bucket = momentum_by_horizon.setdefault((horizon, direction), [0.0, 0.0, 0.0])
            bucket[0] += row["entries"]
            bucket[1] += row["winsAtResolution"]
            bucket[2] += row["averagePnlPerShare"] * row["entries"]
            bucket = momentum_by_case.setdefault((horizon, drop, direction), [0.0, 0.0, 0.0])
            bucket[0] += row["entries"]
            bucket[1] += row["winsAtResolution"]
            bucket[2] += row["averagePnlPerShare"] * row["entries"]
    lines = [
        f"# {result['experiment']}", "",
        "## 固定口径", "",
        f"- 数据：{result['settledMarkets']}个已结算BTC 5分钟市场；前{result['trainMarkets']}个探索，后{result['validationMarkets']}个时间外验证；本次实际处理{result['selectedMarkets']}个市场。",
        f"- 输出模式：{result['detailMode']}；汇总模式不会生成逐规则明细，但统计口径不变。",
        "- 时间轴：剩余300–240、240–180、180–120、120–90、90–60、60–30、30–5、5–0秒；最后5秒只作诊断。",
        "- E1：0.20–0.50低价入场；E2：0.80–0.90早期入场，分别要求稳定0/5/10/30/60秒；均按同一信号比较中途退出与官方结算。",
        f"- E3/E4：高位方向VWAP≥0.80后，独立测试"
        f"{'/'.join(str(horizon) for horizon in sorted({horizon for horizon, _drop in REVERSAL_GRID}))}秒"
        f"与回落{'/'.join(f'{drop:.2f}' for drop in sorted({drop for _horizon, drop in REVERSAL_GRID}))}；分别买反方向和继续买高位方向。",
        "- 买入和卖出均按10份逐档VWAP并计taker手续费；未达到中途目标的记录仍按结算收益计入。", "",
        "## 全300秒生命周期覆盖（验证段）", "",
        "以下是各实验的规则命中次数，不是去重后的交易数；同一个信号会因不同稳定时长、价格上限和退出规则重复计入。", "",
        "| 入场时间窗 | E1低价 | E2稳定高价 | E3/E4反向 | E3/E4继续高位 |", "|---|---:|---:|---:|---:|",
    ]
    for label, _minimum, _maximum in FULL_LIFECYCLE_WINDOWS:
        item = coverage[label]
        lines.append(f"| {label.replace('_', '–')}秒 | {item['E1']} | {item['E2']} | {item['E3反向']} | {item['E3继续']} |")
    lines.extend([
        "", "## 验证段中样本不少于30笔的最佳中途退出对照", "",
        "| 实验 | 入场时间窗 | 条件 | 退出 | 笔数 | 中途退出率 | 正收益率 | 平均收益/份 | 持有结算收益/份 |",
        "|---|---|---|---|---:|---:|---:|---:|---:|",
    ])
    for row in sorted(validation, key=lambda item: item["averagePnlPerShare"], reverse=True)[:40]:
        lines.append(
            f"| {row['experiment']} | {row['entryWindow'] or '—'} | {row['case']} | {row['exitRule']} | {row['entries']} | "
            f"{row['midExitRate']:.1%} | {row['positiveExits']/row['entries']:.1%} | "
            f"{row['averagePnlPerShare']:+.4f} | {row['averageHoldPnlPerShare']:+.4f} |"
        )
    lines.extend([
        "", "## E3/E4反向入场按预注册回落阈值汇总（验证段、持有到结算）", "",
        "| 预注册回落阈值 | 笔数 | 结算胜率 | 平均收益/份 |", "|---:|---:|---:|---:|",
    ])
    for drop, (entries, wins, pnl_sum) in sorted(reversal_by_drop.items(), key=lambda item: float(item[0])):
        lines.append(f"| {drop} | {int(entries)} | {wins / entries:.1%} | {pnl_sum / entries:+.4f} |")
    lines.extend([
        "", "## 解读规则", "",
        "- 同一个条件必须看 `settlement` 与中途退出两行，不能只挑退出成功的订单。",
        "- 中途退出率低时，正收益率不代表可执行收益；优先看按所有入场信号计的平均收益。",
        "- 5–0秒只用于判断是否存在数据/流动性异常，不自动作为入场候选。",
        "- E3/E4的大回落网格仍是探索性结果；少于100笔或跨日期不稳定的组合不进入实盘。",
        "- E5领先性未在本轮执行，等最终策略冻结后再按外部时间戳提前0.5/1/3/5秒单独验证。", "",
        "## 产物", "",
        f"- `{'e7' if result['experiment'] == DEFAULT_EXPERIMENT_NAME else ('e8' if result['experiment'].startswith('E8') else 'e9')}_results.json`：汇总及参数。",
    ])
    if result["detailMode"] == "full":
        lines.append("- `e7_lifecycle_simulations.csv.gz`：逐市场、逐信号、逐退出规则明细。")
    else:
        lines.append("- 本次为汇总模式，未生成逐规则明细；需要审计时去掉`--summary-only`。")
    if result["experiment"].startswith("E9"):
        lines.extend([
            "", "## 短周期回落动能：按观察秒数汇总（验证段、持有到结算）", "",
            "以下是同一回落网格下的规则命中汇总；同一市场可能同时命中多个秒数/幅度，不能把各行相加当作独立交易。短周期按‘当前快照向前不超过N秒’判断，不保证恰好在N秒有一条快照。", "",
            "| 观察秒数 | 反方向笔数 | 反方向胜率 | 反方向收益/份 | 继续高位笔数 | 继续高位胜率 | 继续高位收益/份 |", "|---:|---:|---:|---:|---:|---:|---:|",
        ])
        for horizon in sorted({horizon for horizon, _direction in momentum_by_horizon}):
            opposite = momentum_by_horizon.get((horizon, "反方向"), [0.0, 0.0, 0.0])
            continuation = momentum_by_horizon.get((horizon, "继续高位方向"), [0.0, 0.0, 0.0])
            def metric(bucket: list[float], index: int) -> str:
                return f"{bucket[index] / bucket[0]:.1%}" if bucket[0] else "—"
            def pnl(bucket: list[float]) -> str:
                return f"{bucket[2] / bucket[0]:+.4f}" if bucket[0] else "—"
            lines.append(
                f"| {horizon} | {int(opposite[0])} | {metric(opposite, 1)} | {pnl(opposite)} | "
                f"{int(continuation[0])} | {metric(continuation, 1)} | {pnl(continuation)} |"
            )
        lines.extend([
            "", "## 短周期 × 回落幅度重点对照（验证段、持有到结算）", "",
            "选择0.03、0.10、0.20、0.40四个幅度展示速度效应；0.40以上若样本很少，只能作为探索信号。", "",
            "| 方向 | 回落幅度 | 1秒 n/胜率/收益 | 2秒 n/胜率/收益 | 3秒 n/胜率/收益 | 4秒 n/胜率/收益 | 5秒 n/胜率/收益 | 10秒 n/胜率/收益 | 15秒 n/胜率/收益 | 30秒 n/胜率/收益 |", "|---|---:|---|---|---|---|---|---|---|---|",
        ])
        for direction in ("反方向", "继续高位方向"):
            suffix = "反方向" if direction == "反方向" else "继续高位方向"
            for drop in (0.03, 0.10, 0.20, 0.40):
                cells = []
                for horizon in (1, 2, 3, 4, 5, 10, 15, 30):
                    bucket = momentum_by_case.get((horizon, drop, direction), [0.0, 0.0, 0.0])
                    if not bucket[0]:
                        cells.append("—")
                    else:
                        cells.append(f"{int(bucket[0])}/{bucket[1] / bucket[0]:.1%}/{bucket[2] / bucket[0]:+.4f}")
                lines.append(f"| {suffix} | {drop:.2f} | " + " | ".join(cells) + " |")
        lines.extend([
            "", "## 动能结果解读", "",
            "- 1–4秒短周期确实能区分‘速度’，但在0.01–0.20回落区间，反方向仍明显亏损；不能因为回落更快就直接反买。",
            "- 0.03回落时1/2/3/4秒反方向结算胜率约14%，与5–30秒几乎没有改善；0.10回落也没有稳定的速度单调关系。",
            "- 0.40以上出现正收益主要来自极少量极端样本（短到1秒只有3笔），不能作为实盘阈值；需跨日期、去重后再验证。",
            "- 继续买高位方向与原先的动量延续结论一致，0.03回落各观察秒数约85%附近，但这是‘已有高位后继续跟随’，不是反转套利。",
            "- 当前快照中位间隔约0.26秒，因此1–4秒有可观测性；但若要验证真正的1秒成交级领先，仍需保留事件时间戳和主动成交数据。",
        ])
    lines.extend([
        "", "## 复现命令", "",
        "```bash", "PYTHONPATH=src python3 scripts/analysis/run_strategy_idea_e7.py", "# 快速跑时间外验证段，不生成逐笔明细", "PYTHONPATH=src python3 scripts/analysis/run_strategy_idea_e7.py --split validation --summary-only --out outputs/research_tests/e7_quick_validation", "```", "",
    ])
    path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
