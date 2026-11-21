#!/usr/bin/env python3
"""把失败样本中可在入场前观察到的信号带入回测。

只使用入场前的 5 秒动量和剩余时间；入场后的 MAE/MFE 仅用于解释，绝不参与筛选。
"""

from __future__ import annotations

import json
import argparse
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[2]
INPUT = ROOT / "outputs/research_tests/market_regime_results.json"
OUTPUT_JSON = ROOT / "outputs/research_tests/failure_filter_results.json"
OUTPUT_MD = ROOT / "outputs/research_tests/失败原因过滤回测.md"


def summarize(rows: list[dict], label: str) -> dict:
    wins = sum(bool(row.get("won")) for row in rows)
    return {
        "label": label,
        "trades": len(rows),
        "wins": wins,
        "losses": len(rows) - wins,
        "winRate": wins / len(rows) if rows else None,
        "avgPnlPerShare": mean(float(row["pnlPerShare"]) for row in rows) if rows else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=INPUT)
    parser.add_argument("--output-json", type=Path, default=OUTPUT_JSON)
    parser.add_argument("--output-md", type=Path, default=OUTPUT_MD)
    args = parser.parse_args()
    rows = json.loads(args.input.read_text(encoding="utf-8"))["rows"]
    variants = [
        ("基线：不加失败过滤", lambda row: True),
        ("排除入场前逆向回落", lambda row: row.get("momentum5") is not None and row["momentum5"] > -0.01),
        ("只保留入场前同方向上涨≥0.01", lambda row: row.get("momentum5") is not None and row["momentum5"] >= 0.01),
        ("同方向上涨≥0.01且剩余45–60秒", lambda row: row.get("momentum5") is not None and row["momentum5"] >= 0.01 and 45 <= float(row["remainingSeconds"]) < 60),
    ]
    result = {"schemaVersion": 1, "source": str(args.input), "totalRows": len(rows), "variants": []}
    for label, predicate in variants:
        all_rows = [row for row in rows if predicate(row)]
        by_outcome = [summarize([row for row in all_rows if row["outcome"] == outcome], outcome) for outcome in ("DOWN", "UP")]
        item = summarize(all_rows, label)
        item["excludedLosses"] = sum(not row.get("won") for row in rows if not predicate(row))
        item["byOutcome"] = by_outcome
        result["variants"].append(item)
    args.output_json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    by_label = {item["label"]: item for item in result["variants"]}
    def describe(label: str) -> str:
        item = by_label[label]
        rate = "—" if item["winRate"] is None else f"{item['winRate'] * 100:.1f}%"
        pnl_value = item["avgPnlPerShare"]
        pnl_text = "—" if pnl_value is None else f"{pnl_value:+.4f}"
        return f"{item['trades']} 笔、{item['wins']} 胜 {item['losses']} 负，胜率 {rate}，平均 PnL/份 {pnl_text}"

    base = by_label["基线：不加失败过滤"]
    momentum = by_label["只保留入场前同方向上涨≥0.01"]
    momentum_45_60 = by_label["同方向上涨≥0.01且剩余45–60秒"]

    def pct(value):
        return "—" if value is None else f"{value * 100:.1f}%"

    def pnl(value):
        return "—" if value is None else f"{value:+.4f}"

    lines = [
        "# 失败原因过滤回测",
        "",
        "## 测试口径",
        "",
        f"- 基于当前 0.80–0.85、实际 10 份成交、剩余 90–30 秒的 {len(rows)} 笔可分析成交。",
        "- 只把入场前 5 秒动量和剩余时间带入筛选；入场后的 MAE/MFE 不参与入场，避免未来数据泄漏。",
        "- 这是探索性回测，过滤阈值不能用这批数据反复调到最优后直接视为已验证。",
        "",
        "## 总体结果",
        "",
        "| 过滤方案 | 笔数 | 胜 | 负 | 胜率 | 平均PnL/份 | 被排除的失败笔数 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for item in result["variants"]:
        lines.append(f"| {item['label']} | {item['trades']} | {item['wins']} | {item['losses']} | {pct(item['winRate'])} | `{pnl(item['avgPnlPerShare'])}` | {item['excludedLosses']} |")
    lines += [
        "",
        "## 按方向结果",
        "",
        "| 过滤方案 | 方向 | 笔数 | 胜 | 负 | 胜率 | 平均PnL/份 |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for item in result["variants"]:
        for row in item["byOutcome"]:
            lines.append(f"| {item['label']} | {row['label']} | {row['trades']} | {row['wins']} | {row['losses']} | {pct(row['winRate'])} | `{pnl(row['avgPnlPerShare'])}` |")
    lines += [
        "",
        "## 当前解读",
        "",
        f"- 不加过滤基线：{describe('基线：不加失败过滤')}。",
        f"- 排除入场前逆向回落：{describe('排除入场前逆向回落')}，被排除的失败笔数为 {by_label['排除入场前逆向回落']['excludedLosses']}。",
        f"- 只保留入场前同方向上涨≥0.01：{describe('只保留入场前同方向上涨≥0.01')}，被排除的失败笔数为 {momentum['excludedLosses']}。",
        f"- 再叠加剩余45–60秒：{describe('同方向上涨≥0.01且剩余45–60秒')}；样本仍少于100笔，不能直接升级为最终策略。",
        "- 这些过滤结果只能作为探索性结果；应继续用未参与调参的新日期做样本外验证，并把 UP/DOWN 分开评估。",
        "",
        "## 可复现命令",
        "",
        "```bash",
        "PYTHONDONTWRITEBYTECODE=1 /Users/laochen/.local/bin/python3.12 scripts/analysis/analyze_failure_filters.py",
        "```",
        "",
    ]
    args.output_md.write_text("\n".join(lines), encoding="utf-8")
    print(f"rows={len(rows)} variants={len(result['variants'])}")
    print(args.output_json)
    print(args.output_md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
