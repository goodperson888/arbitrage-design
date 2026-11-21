#!/usr/bin/env python3
"""E10：以当前DOWN候选为基线，逐一加入辅助条件并检查失败单过滤效果。"""

from __future__ import annotations

import argparse
from collections import defaultdict
import csv
import json
from pathlib import Path
from statistics import mean
from typing import Any, Callable, Iterable

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = ROOT / "outputs/research_tests/market_regime_results.json"
DEFAULT_E0 = ROOT / "outputs/research_tests/e0_anchor_probability_20260826/e0_observations.csv"
DEFAULT_OUT = ROOT / "outputs/research_tests/e10_fixed_strategy_ablation_20260826"


def summarize_variant(rows: list[dict[str, Any]]) -> dict[str, Any]:
    wins = sum(bool(row.get("won")) for row in rows)
    return {
        "trades": len(rows),
        "wins": wins,
        "losses": len(rows) - wins,
        "winRate": wins / len(rows) if rows else None,
        "avgPnlPerShare": mean(float(row["pnlPerShare"]) for row in rows) if rows else None,
    }


def failure_coverage(
    baseline: list[dict[str, Any]], kept: list[dict[str, Any]]
) -> dict[str, Any]:
    kept_ids = {id(row) for row in kept}
    excluded = [row for row in baseline if id(row) not in kept_ids]
    baseline_losses = sum(not bool(row.get("won")) for row in baseline)
    baseline_wins = sum(bool(row.get("won")) for row in baseline)
    excluded_losses = sum(not bool(row.get("won")) for row in excluded)
    excluded_wins = sum(bool(row.get("won")) for row in excluded)
    return {
        "excludedTrades": len(excluded),
        "excludedLosses": excluded_losses,
        "excludedWins": excluded_wins,
        "excludedLossRate": excluded_losses / baseline_losses if baseline_losses else None,
        "excludedWinRate": excluded_wins / baseline_wins if baseline_wins else None,
        "keptTradeRate": len(kept) / len(baseline) if baseline else None,
    }


def enrich_e0(
    rows: list[dict[str, Any]],
    observations: Iterable[dict[str, Any]],
    *,
    max_age_seconds: float = 45.0,
) -> list[dict[str, Any]]:
    by_market: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for observation in observations:
        try:
            if not observation.get("combinedProbabilityUp"):
                continue
            observation = dict(observation)
            observation["observationSourceUs"] = int(observation["observationSourceUs"])
            observation["combinedProbabilityUp"] = float(observation["combinedProbabilityUp"])
        except (KeyError, TypeError, ValueError):
            continue
        by_market[str(observation.get("marketId") or "")].append(observation)
    for values in by_market.values():
        values.sort(key=lambda item: item["observationSourceUs"])

    enriched: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["e0ProbabilityUp"] = None
        item["e0ProbabilityForEntry"] = None
        item["e0AgeSeconds"] = None
        try:
            execution_us = int(row["executionUs"])
        except (KeyError, TypeError, ValueError):
            enriched.append(item)
            continue
        candidates = [
            value for value in by_market.get(str(row.get("marketId") or ""), [])
            if value["observationSourceUs"] <= execution_us
        ]
        if candidates:
            selected = candidates[-1]
            age_seconds = (execution_us - selected["observationSourceUs"]) / 1_000_000.0
            if age_seconds <= max_age_seconds:
                probability_up = float(selected["combinedProbabilityUp"])
                item["e0ProbabilityUp"] = probability_up
                item["e0ProbabilityForEntry"] = (
                    probability_up if row.get("outcome") == "UP" else 1.0 - probability_up
                )
                item["e0AgeSeconds"] = age_seconds
        enriched.append(item)
    return enriched


def read_json_rows(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("rows") if isinstance(payload, dict) else payload
    return [dict(row) for row in rows or [] if isinstance(row, dict)]


def read_e0_rows(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def format_rate(value: float | None) -> str:
    return "—" if value is None else f"{value:.1%}"


def format_pnl(value: float | None) -> str:
    return "—" if value is None else f"{value:+.4f}"


def main() -> int:
    parser = argparse.ArgumentParser(description="执行E10固定策略交叉对照")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--e0-observations", type=Path, default=DEFAULT_E0)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--outcome", choices=("DOWN", "UP"), default="DOWN")
    parser.add_argument("--e0-max-age-seconds", type=float, default=45.0)
    args = parser.parse_args()

    source_rows = read_json_rows(args.input)
    baseline = [
        row for row in source_rows
        if str(row.get("outcome", "")).upper() == args.outcome
        and 0.80 <= float(row.get("entryVwap", -1)) <= 0.85
        and 30 <= float(row.get("remainingSeconds", -1)) <= 90
    ]
    rows = enrich_e0(baseline, read_e0_rows(args.e0_observations), max_age_seconds=args.e0_max_age_seconds)

    variants: list[tuple[str, Callable[[dict[str, Any]], bool]]] = [
        ("A 基线：当前策略", lambda row: True),
        ("B + 入场前5秒同方向动量≥0.01", lambda row: row.get("momentum5") is not None and float(row["momentum5"]) >= 0.01),
        ("C + 入场前15秒同方向动量≥0.02", lambda row: row.get("momentum15") is not None and float(row["momentum15"]) >= 0.02),
        ("D + 5秒≥0.01且15秒≥0.02", lambda row: row.get("momentum5") is not None and row.get("momentum15") is not None and float(row["momentum5"]) >= 0.01 and float(row["momentum15"]) >= 0.02),
        ("E + Binance Spot同方向", lambda row: str(row.get("spotSignal") or "") == str(row.get("outcome") or "")),
        ("F + E0方向概率≥0.55", lambda row: row.get("e0ProbabilityForEntry") is not None and float(row["e0ProbabilityForEntry"]) >= 0.55),
    ]
    result_variants: list[dict[str, Any]] = []
    for label, predicate in variants:
        kept = [row for row in rows if predicate(row)]
        item = {"label": label, **summarize_variant(kept), **failure_coverage(rows, kept)}
        item["e0Available"] = sum(row.get("e0ProbabilityForEntry") is not None for row in kept)
        result_variants.append(item)

    output = {
        "schemaVersion": 1,
        "experiment": "E10_固定策略交叉对照",
        "source": str(args.input),
        "outcome": args.outcome,
        "sourceRows": len(source_rows),
        "baselineRows": len(rows),
        "e0MaxAgeSeconds": args.e0_max_age_seconds,
        "variants": result_variants,
    }
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "e10_results.json").write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = [
        "# E10 固定策略交叉对照与失败单过滤",
        "",
        "## 测试目的",
        "",
        "以当前最稳的DOWN策略为基线，一次只加入一个辅助条件，比较它提高净收益的能力，以及它能过滤多少失败单、误杀多少成功单。",
        "",
        "## 固定口径",
        "",
        f"- 数据来源：`{args.input}`；当前源文件共{len(source_rows)}条，筛出DOWN、实际VWAP 0.80–0.85、剩余90–30秒的基线{len(rows)}笔。",
        "- 结果使用源记录中的官方结算标签和已扣成本的`pnlPerShare`，不重新估算手续费。",
        "- 每个变体只改变一个入场过滤条件；不改变成交模型，不把多个变体的结果相加。",
        "- E0只使用执行时间之前的最近模型观测，且观测年龄不超过45秒；没有可用E0观测的记录不会被F保留。",
        "- 这些是同一批历史样本上的固定对照，不能替代新增日期的样本外验证。",
        "",
        "## 变体结果",
        "",
        "| 变体 | 笔数 | 胜/负 | 胜率 | 平均净收益/份 | 保留率 | 排除失败 | 排除成功 | 失败排除率 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in result_variants:
        lines.append(
            f"| {item['label']} | {item['trades']} | {item['wins']}/{item['losses']} | {format_rate(item['winRate'])} | "
            f"{format_pnl(item['avgPnlPerShare'])} | {format_rate(item['keptTradeRate'])} | {item['excludedLosses']} | "
            f"{item['excludedWins']} | {format_rate(item['excludedLossRate'])} |"
        )
    lines.extend([
        "", "## 按北京时间日期复核（只列A–E，F覆盖不足）", "",
        "同一条件如果只在累计结果好、最近日期转负，不能直接升级。这里的日期统计仍是历史回放，不代表未来收益。", "",
        "| 日期 | A基线 n/胜率/收益 | B 5秒 n/胜率/收益 | C 15秒 n/胜率/收益 | D 双动量 n/胜率/收益 | E Spot n/胜率/收益 |", "|---|---|---|---|---|---|",
    ])
    predicates = [predicate for _label, predicate in variants[:5]]
    labels = ["A", "B", "C", "D", "E"]
    dates = sorted({str(row.get("localTime", ""))[:10] for row in rows if row.get("localTime")})
    for date in dates:
        cells: list[str] = []
        date_rows = [row for row in rows if str(row.get("localTime", ""))[:10] == date]
        for predicate in predicates:
            kept = [row for row in date_rows if predicate(row)]
            summary = summarize_variant(kept)
            cells.append("—" if not kept else f"{summary['trades']}/{format_rate(summary['winRate'])}/{format_pnl(summary['avgPnlPerShare'])}")
        lines.append(f"| {date} | " + " | ".join(cells) + " |")
    lines.extend([
        "", "## 失败单过滤怎么理解", "",
        "- `排除失败`越多不一定越好；如果同时排除更多成功单，机会成本可能更大。",
        "- 只有在保留率、平均净收益和跨日期稳定性同时改善时，过滤条件才有资格进入下一轮。",
        "- E0变体的样本覆盖率单独列出；覆盖不足时，即使点估计好看，也不能与其他变体公平比较。",
        "- 入场后的MAE/MFE只用于解释，不参与上述入场过滤，避免未来数据泄漏。",
        "",
        "## 当前首轮结论",
        "",
        "- 先看B、C、D的动量增量，再看E的Binance Spot增量；F的E0结果只能作为低覆盖率探索，不直接升级实盘。",
        "- 这轮只回答‘辅助条件是否值得继续验证’，不修改纸上交易或实盘策略。",
        "",
        "## 复现命令",
        "",
        "```bash",
        "PYTHONPATH=src python3 scripts/analysis/run_strategy_idea_e10.py",
        "```",
        "",
    ])
    (args.out / "E10固定策略交叉对照与失败单过滤.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"sourceRows={len(source_rows)} baselineRows={len(rows)} variants={len(result_variants)}")
    print(args.out / "e10_results.json")
    print(args.out / "E10固定策略交叉对照与失败单过滤.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
