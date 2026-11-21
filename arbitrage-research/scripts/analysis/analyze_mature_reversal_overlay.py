#!/usr/bin/env python3
"""T055：在完整成熟策略基线上逐一叠加四个回落恢复因子。"""

from __future__ import annotations

import argparse
from collections import defaultdict
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
from statistics import mean
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.analysis.analyze_intramarket_exits import load_market_snapshots  # noqa: E402
from scripts.analysis.analyze_reversal_reentry import (  # noqa: E402
    CONDITIONS,
    MAX_LOOKBACK,
    RECOVERY,
    build_points,
    in_range,
)


def number(row: dict[str, Any], field: str) -> float | None:
    try:
        value = row.get(field)
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    wins = sum(bool(row.get("won")) for row in rows)
    return {
        "trades": len(rows),
        "wins": wins,
        "losses": len(rows) - wins,
        "winRate": wins / len(rows) if rows else None,
        "avgPnlPerShare": mean(float(row["pnlPerShare"]) for row in rows) if rows else None,
        "totalPnlPerShare": sum(float(row["pnlPerShare"]) for row in rows),
    }


def qualify_at_execution(
    points: list[dict[str, Any]], execution_us: int, condition_id: str
) -> dict[str, Any] | None:
    condition = next(item for item in CONDITIONS if item.condition_id == condition_id)
    eligible = [index for index, point in enumerate(points) if int(point["sourceUs"]) <= execution_us]
    if not eligible:
        return None
    index = eligible[-1]
    current = points[index]
    if execution_us - int(current["sourceUs"]) > 2_000_000:
        return None
    current_vwap = Decimal(str(current["vwap"]))
    candidates: list[dict[str, Any]] = []
    for trough_index in range(max(0, index - 100), index):
        trough = points[trough_index]
        recovery_seconds = Decimal(current["sourceUs"] - trough["sourceUs"]) / Decimal("1000000")
        if recovery_seconds <= 0 or not in_range(
            recovery_seconds, condition.recovery_seconds, upper_inclusive=True
        ):
            continue
        trough_vwap = Decimal(str(trough["vwap"]))
        recovery_amount = current_vwap - trough_vwap
        if recovery_amount < RECOVERY:
            continue
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
        if amplitude < 0 or not in_range(amplitude, condition.amplitude):
            continue
        path_prices = [Decimal(str(point["vwap"])) for point in points[high_index : index + 1]]
        if trough_vwap != min(path_prices):
            continue
        candidates.append({
            "currentVwap": float(current_vwap),
            "snapshotAgeMs": (execution_us - int(current["sourceUs"])) / 1000,
            "pullbackSeconds": float(pullback_seconds),
            "recoverySeconds": float(recovery_seconds),
            "pullbackAmplitude": float(amplitude),
            "recoveryAmount": float(recovery_amount),
        })
    if not candidates:
        return None
    return min(candidates, key=lambda row: (row["recoverySeconds"], -row["pullbackAmplitude"]))


def fmt_rate(value: float | None) -> str:
    return "—" if value is None else f"{value:.1%}"


def fmt_pnl(value: float | None) -> str:
    return "—" if value is None else f"{value:+.4f}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input",
        type=Path,
        default=ROOT / "outputs/research_tests/t055_mature_reversal_overlay_20260901/mature_baseline_rows.json",
    )
    parser.add_argument("--cache", type=Path, default=ROOT / "data/derived/live_strategy_cache.sqlite3")
    parser.add_argument(
        "--out",
        type=Path,
        default=ROOT / "outputs/research_tests/t055_mature_reversal_overlay_20260901",
    )
    args = parser.parse_args()

    source = json.loads(args.input.read_text(encoding="utf-8"))
    source_rows = [dict(row) for row in source.get("rows", [])]
    mature = [
        row for row in source_rows
        if number(row, "momentum5") is not None
        and float(row["momentum5"]) >= 0.01
        and 0.80 <= float(row["entryVwap"]) <= 0.85
        and 30 <= float(row["remainingSeconds"]) <= 90
    ]
    mature.sort(key=lambda row: int(row["executionUs"]))

    connection = sqlite3.connect(args.cache)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in mature:
        grouped[str(row["marketId"])].append(row)
    path_cache: dict[tuple[str, str], list[dict[str, Any]]] = {}
    end_cache = {
        str(market_id): int(end_us)
        for market_id, end_us in connection.execute(
            "SELECT market_id,end_us FROM markets WHERE end_us IS NOT NULL"
        )
    }
    for market_id, rows in grouped.items():
        snapshots = load_market_snapshots(connection, market_id)
        end_us = end_cache.get(market_id)
        if end_us is None:
            continue
        for outcome in {str(row["outcome"]) for row in rows}:
            path_cache[(market_id, outcome)] = build_points(snapshots, end_us, outcome)
    connection.close()

    enriched: list[dict[str, Any]] = []
    for row in mature:
        item = dict(row)
        points = path_cache.get((str(row["marketId"]), str(row["outcome"])), [])
        item["reversalFactors"] = {
            condition.condition_id: qualify_at_execution(points, int(row["executionUs"]), condition.condition_id)
            for condition in CONDITIONS
        }
        enriched.append(item)

    results: dict[str, Any] = {}
    for outcome in ("UP", "DOWN"):
        baseline = [row for row in enriched if row["outcome"] == outcome]
        split = max(1, int(len(baseline) * 0.70))
        train, validation = baseline[:split], baseline[split:]
        variants = []
        for condition in CONDITIONS:
            predicate = lambda row, cid=condition.condition_id: row["reversalFactors"].get(cid) is not None
            kept = [row for row in baseline if predicate(row)]
            kept_train = [row for row in train if predicate(row)]
            kept_validation = [row for row in validation if predicate(row)]
            base_stats, kept_stats = stats(baseline), stats(kept)
            variants.append({
                "condition": condition.condition_id,
                "label": condition.label,
                "all": kept_stats,
                "train": stats(kept_train),
                "validation": stats(kept_validation),
                "retainedRate": len(kept) / len(baseline) if baseline else None,
                "filteredWins": base_stats["wins"] - kept_stats["wins"],
                "avoidedLosses": base_stats["losses"] - kept_stats["losses"],
                "winRateDelta": (
                    kept_stats["winRate"] - base_stats["winRate"]
                    if kept_stats["winRate"] is not None and base_stats["winRate"] is not None else None
                ),
                "avgPnlDelta": (
                    kept_stats["avgPnlPerShare"] - base_stats["avgPnlPerShare"]
                    if kept_stats["avgPnlPerShare"] is not None and base_stats["avgPnlPerShare"] is not None else None
                ),
            })
        results[outcome] = {
            "baseline": stats(baseline),
            "trainBaseline": stats(train),
            "validationBaseline": stats(validation),
            "variants": variants,
        }

    args.out.mkdir(parents=True, exist_ok=True)
    json_path = args.out / "mature_reversal_overlay_results.json"
    json_path.write_text(json.dumps({
        "schemaVersion": 1,
        "experiment": "T055_mature_reversal_overlay",
        "definition": {
            "baseline": "真实主动买入≥10份 + 10份VWAP 0.80–0.85 + 剩余90–30秒 + 盘口年龄≤2秒 + 5秒同方向动量≥0.01",
            "reversalPath": "预测市场10份可执行VWAP在成熟策略实际执行时刻以前的回落-恢复路径",
            "split": "每个方向按执行时间前70%/后30%",
        },
        "sourceRows": len(source_rows),
        "matureRows": len(mature),
        "results": results,
        "rows": enriched,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = [
        "# T055：成熟策略叠加回落恢复因子",
        "",
        "## 口径",
        "",
        "- 完整成熟基线：真实主动买入≥10份、实际10份VWAP 0.80–0.85、剩余90–30秒、盘口年龄≤2秒、入场前5秒同方向动量≥0.01。",
        f"- 最新可分析主动成交基线 {len(source_rows)} 笔；加入5秒动量后的成熟信号 {len(mature)} 笔。",
        "- 四个回落恢复因子逐一加入，不互相叠加；UP/DOWN分开，并按执行时间前70%/后30%复核。",
        "- 回落恢复路径来自入场前预测市场10份可执行VWAP，不使用入场后的未来价格。",
        "- 净收益沿用成熟基线源记录，已计入买入手续费并持有到官方结算。",
        "",
        "## 结论先行",
        "",
        "- UP没有任何一个回落恢复因子在全样本中同时提高胜率和平均净收益；不建议加入UP成熟策略。",
        "- DOWN最实用的候选是‘回落幅度≥0.03、5秒内恢复≥0.01’：67笔、94.0%、+0.1130/份，优于DOWN基线143笔、91.6%、+0.0870/份；后30%为19笔全胜、+0.1726/份。样本仍不足100笔，先作为高置信度标签。",
        "- DOWN最强但最稀疏的是‘回落3–5秒、1–3秒内恢复≥0.01’：22笔全胜、+0.1701/份，后30%仅3笔；只能列为探索候选。",
        "- 两个候选都会过滤大量原本盈利订单：大幅回落组只保留46.9%，中等时长组只保留15.4%。它们提高的是单笔质量，不会自动提高同一时期的累计总收益。",
        "- 因此不建议把成熟策略改成‘必须满足回落恢复’；更合理的是保留原策略，把满足候选的DOWN订单标记为高置信度，继续纸上/小额样本外记录。",
        "",
        "## 全样本增量对照",
        "",
    ]
    for outcome in ("UP", "DOWN"):
        base = results[outcome]["baseline"]
        lines += [
            f"### {outcome}基线：{base['trades']}笔，{fmt_rate(base['winRate'])}，{fmt_pnl(base['avgPnlPerShare'])}/份",
            "",
            "| 附加条件 | 笔数 | 胜/负 | 胜率 | 平均净收益/份 | 保留率 | 胜率变化 | 收益变化 | 过滤胜单 | 避开败单 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for item in results[outcome]["variants"]:
            s = item["all"]
            lines.append(
                f"| {item['label']} | {s['trades']} | {s['wins']}/{s['losses']} | {fmt_rate(s['winRate'])} | "
                f"{fmt_pnl(s['avgPnlPerShare'])} | {fmt_rate(item['retainedRate'])} | "
                f"{fmt_rate(item['winRateDelta'])} | {fmt_pnl(item['avgPnlDelta'])} | "
                f"{item['filteredWins']} | {item['avoidedLosses']} |"
            )
        lines += ["", "### 后30%时间段", "", "| 附加条件 | 笔数 | 胜/负 | 胜率 | 平均净收益/份 |", "|---|---:|---:|---:|---:|"]
        vb = results[outcome]["validationBaseline"]
        lines.append(f"| 成熟基线 | {vb['trades']} | {vb['wins']}/{vb['losses']} | {fmt_rate(vb['winRate'])} | {fmt_pnl(vb['avgPnlPerShare'])} |")
        for item in results[outcome]["variants"]:
            s = item["validation"]
            lines.append(f"| {item['label']} | {s['trades']} | {s['wins']}/{s['losses']} | {fmt_rate(s['winRate'])} | {fmt_pnl(s['avgPnlPerShare'])} |")
        lines.append("")
    lines += [
        "## 判定原则",
        "",
        "- 只有全样本与后30%时间段方向一致、每方向新增样本达到100笔，并且净收益提升而不是只提高胜率，才考虑写入实盘硬条件。",
        "- 保留率过低会过滤大量原本盈利订单；必须同时看过滤胜单和避开败单，不能只看筛选后的胜率。",
        "- 本报告只做离线叠加回测，不修改纸上交易、实盘或通用实时指标网页。",
        "",
        "## 复现命令",
        "",
        "```bash",
        "PYTHONDONTWRITEBYTECODE=1 python3 scripts/analysis/analyze_mature_reversal_overlay.py",
        "```",
    ]
    md_path = args.out / "成熟策略叠加回落恢复因子.md"
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"source={len(source_rows)} mature={len(mature)}")
    print(json_path.resolve())
    print(md_path.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
