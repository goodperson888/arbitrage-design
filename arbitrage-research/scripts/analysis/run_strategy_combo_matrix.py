#!/usr/bin/env python3
"""E14：成熟基线的预注册一/二/三因子组合对照。

只测试少量事先固定的因子阈值，避免把所有字段和阈值笛卡尔积后挑偶然最优。
"""

from __future__ import annotations

import argparse
import itertools
import json
from pathlib import Path
from statistics import mean
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = ROOT / "outputs/research_tests/e13_strategy_matrix_20260828/market_regime_results.json"
DEFAULT_OUT = ROOT / "outputs/research_tests/e14_factor_combo_matrix_20260828"


def read_rows(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [dict(row) for row in payload.get("rows", []) if isinstance(row, dict)]


def stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    wins = sum(bool(row.get("won")) for row in rows)
    return {
        "trades": len(rows),
        "wins": wins,
        "losses": len(rows) - wins,
        "winRate": wins / len(rows) if rows else None,
        "avgPnlPerShare": mean(float(row["pnlPerShare"]) for row in rows) if rows else None,
    }


def rate(value: float | None) -> str:
    return "—" if value is None else f"{value:.1%}"


def pnl(value: float | None) -> str:
    return "—" if value is None else f"{value:+.4f}"


def build_features(outcome: str) -> dict[str, tuple[str, Callable[[dict[str, Any]], bool]]]:
    def present(row: dict[str, Any], field: str) -> float | None:
        value = row.get(field)
        try:
            return None if value is None else float(value)
        except (TypeError, ValueError):
            return None

    return {
        "M5": ("5秒同方向动量≥0.01", lambda row: (present(row, "momentum5") or -999) >= 0.01),
        "M15": ("15秒同方向动量≥0.02", lambda row: (present(row, "momentum15") or -999) >= 0.02),
        "SPOT": ("Binance Spot最近5分钟同向", lambda row: str(row.get("spotSignal") or "") == outcome),
        "FLOW": ("10秒同方向主动买量≥反向买量", lambda row: (present(row, "sameBuyVolume10s") is not None and present(row, "oppositeBuyVolume10s") is not None and present(row, "sameBuyVolume10s") >= present(row, "oppositeBuyVolume10s"))),
        "QUIET": ("入场前15秒价格区间<0.20", lambda row: (present(row, "range15") is not None and present(row, "range15") < 0.20)),
        "SPREAD": ("预测价差≤0.01", lambda row: (present(row, "spread") is not None and present(row, "spread") <= 0.01)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="E14成熟基线因子组合矩阵")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--min-trades", type=int, default=20)
    args = parser.parse_args()

    source = read_rows(args.input)
    all_results: dict[str, list[dict[str, Any]]] = {}
    for outcome in ("UP", "DOWN"):
        baseline = [
            row for row in source
            if str(row.get("outcome", "")).upper() == outcome
            and 0.80 <= float(row.get("entryVwap", -1)) <= 0.85
            and 30 <= float(row.get("remainingSeconds", -1)) <= 90
        ]
        baseline.sort(key=lambda row: str(row.get("executionUs", "")))
        split = max(1, int(len(baseline) * 0.70))
        train, validation = baseline[:split], baseline[split:]
        features = build_features(outcome)
        results: list[dict[str, Any]] = []
        for size in (1, 2, 3):
            for names in itertools.combinations(features, size):
                kept = [row for row in baseline if all(features[name][1](row) for name in names)]
                valid_kept = [row for row in validation if all(features[name][1](row) for name in names)]
                item = {
                    "outcome": outcome,
                    "factors": list(names),
                    "label": " + ".join(features[name][0] for name in names),
                    "sample": stats(kept),
                    "train": stats([row for row in train if all(features[name][1](row) for name in names)]),
                    "validation": stats(valid_kept),
                    "keptRate": len(kept) / len(baseline) if baseline else None,
                }
                results.append(item)
        all_results[outcome] = results

    args.out.mkdir(parents=True, exist_ok=True)
    payload = {
        "schemaVersion": 1,
        "experiment": "E14_factor_combo_matrix",
        "source": str(args.input),
        "minTradesForRanking": args.min_trades,
        "factorDefinitions": {key: value[0] for key, value in build_features("UP").items()},
        "results": all_results,
    }
    (args.out / "e14_factor_combo_results.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = [
        "# E14 成熟策略一/二/三因子组合回测",
        "",
        "本实验只使用预先固定的6个因子阈值，批量计算单因子、二因子和三因子组合；不穷举所有字段/阈值，也不根据验证段结果反向改阈值。UP、DOWN分开，按执行时间前70%训练、后30%验证。",
        "",
        "## 固定基线与因子",
        "",
        "- 基线：实际VWAP 0.80–0.85、剩余90–30秒、官方结算标签；沿用源记录净收益口径。",
        "- 因子：5秒动量≥0.01、15秒动量≥0.02、Binance Spot同向、10秒主动买量同向不低于反向、15秒区间<0.20、预测价差≤0.01。",
        "- 每个组合都报告全样本、训练段和时间外验证段；少于20笔只列出，不作为排名依据。",
        "",
    ]
    for outcome in ("UP", "DOWN"):
        rows = all_results[outcome]
        baseline_count = sum(1 for row in source if str(row.get("outcome", "")).upper() == outcome and 0.80 <= float(row.get("entryVwap", -1)) <= 0.85 and 30 <= float(row.get("remainingSeconds", -1)) <= 90)
        lines += [f"## {outcome}（基线{baseline_count}笔）", "", "### 时间外验证段中样本≥20的组合（按平均净收益排序）", "", "| 因子组合 | 全样本n | 全样本胜率 | 全样本收益 | 验证n | 验证胜率 | 验证收益 | 保留率 |", "|---|---:|---:|---:|---:|---:|---:|---:|"]
        ranked = [row for row in rows if (row["validation"]["trades"] or 0) >= args.min_trades]
        ranked.sort(key=lambda row: (row["validation"]["avgPnlPerShare"] is not None, row["validation"]["avgPnlPerShare"] or -999), reverse=True)
        for row in ranked[:15]:
            s, v = row["sample"], row["validation"]
            lines.append(f"| {row['label']} | {s['trades']} | {rate(s['winRate'])} | {pnl(s['avgPnlPerShare'])} | {v['trades']} | {rate(v['winRate'])} | {pnl(v['avgPnlPerShare'])} | {rate(row['keptRate'])} |")
        lines += ["", "### 说明", "", "- 这里按验证段展示前15名只是便于阅读，完整单/双/三因子结果在JSON中；不要把排名第一理解为已确认策略。", "- 如果训练段改善而验证段不改善，说明组合可能过拟合；如果验证段样本少于30笔，只能作为观察候选。", ""]
    lines += ["## 结论与下一步", "", "- 这轮用于判断哪些因子有叠加价值，不自动改纸上交易或实盘。优先观察同时满足：验证段≥30笔、训练/验证方向一致、收益提升且保留率不过低。", "- E0 Chainlink概率和锚定距离、Futures/Hyperliquid/Deribit暂不放入同一笛卡尔组合：当前可用覆盖不足或目标是领先性记录，应在固定候选策略后单独做时间领先验证。", "- 下一轮应冻结最多1个二因子组合和1个三因子组合，用新增日期数据做样本外复验。", "", "## 复现", "", "```bash", "PYTHONDONTWRITEBYTECODE=1 python3 scripts/analysis/run_strategy_combo_matrix.py", "```", ""]
    (args.out / "E14成熟策略因子组合回测.md").write_text("\n".join(lines), encoding="utf-8")
    print(args.out / "E14成熟策略因子组合回测.md")
    print(args.out / "e14_factor_combo_results.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
