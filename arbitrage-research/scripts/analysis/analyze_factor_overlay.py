#!/usr/bin/env python3
"""T058: overlay available depth, spread, volatility and flow factors.

This is intentionally a small registered comparison over the current mature
entry rows.  It does not search a large parameter grid and does not alter any
live or paper-trading configuration.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import mean
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = ROOT / "outputs/research_tests/t055_mature_reversal_overlay_20260901/mature_reversal_overlay_results.json"
DEFAULT_OUT = ROOT / "outputs/research_tests/t058_factor_overlay_20260901"


def summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    wins = sum(bool(row.get("won")) for row in rows)
    pnls = [float(row["pnlPerShare"]) for row in rows]
    return {
        "trades": len(rows),
        "wins": wins,
        "losses": len(rows) - wins,
        "winRate": wins / len(rows) if rows else None,
        "avgPnlPerShare": mean(pnls) if pnls else None,
    }


def number(row: dict[str, Any], key: str) -> float | None:
    value = row.get(key)
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    payload = json.loads(args.input.read_text(encoding="utf-8"))
    rows = [dict(row) for row in payload.get("rows", [])]
    # T055 already defines the mature entry: 0.80–0.85 VWAP, 90–30 seconds,
    # actual 10-share aggressive fill, fresh book, and 5s momentum >= 0.01.
    factors: list[tuple[str, str, Callable[[dict[str, Any]], bool]]] = [
        ("depthAt85≥10份", "盘口可执行深度至少10份", lambda r: (number(r, "depthAt85") or 0) >= 10),
        ("depthAt85≥100份", "盘口可执行深度至少100份", lambda r: (number(r, "depthAt85") or 0) >= 100),
        ("depthAt85≥500份", "盘口可执行深度至少500份", lambda r: (number(r, "depthAt85") or 0) >= 500),
        ("spread≤0.01", "买卖价差不超过0.01", lambda r: (number(r, "spread") is not None and number(r, "spread") <= 0.01)),
        ("range15<0.20", "前15秒价格幅度低于0.20", lambda r: (number(r, "range15") is not None and number(r, "range15") < 0.20)),
        ("range15≥0.20", "前15秒价格幅度至少0.20", lambda r: (number(r, "range15") is not None and number(r, "range15") >= 0.20)),
        ("sameFlow≥opposite", "10秒同向主动成交量不低于反向", lambda r: (number(r, "sameBuyVolume10s") is not None and number(r, "oppositeBuyVolume10s") is not None and number(r, "sameBuyVolume10s") >= number(r, "oppositeBuyVolume10s"))),
    ]
    results: list[dict[str, Any]] = []
    for outcome in ("UP", "DOWN"):
        base = [row for row in rows if str(row.get("outcome")) == outcome]
        for factor_id, label, predicate in factors:
            selected = [row for row in base if predicate(row)]
            results.append({"outcome": outcome, "factor": factor_id, "label": label,
                            **summary(selected), "retainedRate": len(selected) / len(base) if base else None,
                            "baseline": summary(base)})

    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    (out / "factor_overlay_results.json").write_text(json.dumps({
        "schemaVersion": 1, "experiment": "T058_factor_overlay", "source": str(args.input),
        "sourceRows": len(rows), "results": results,
        "unavailable": ["1秒/2秒/3秒/10秒动量：T055逐笔源记录没有这些独立字段；不能从5秒字段倒推。", "盘口连续稳定时长：当前T055行只有入场时盘口年龄和深度，未保存完整稳定窗口。", "MAE/入场后路径字段：只能做失败诊断，禁止作为入场过滤，已排除。"],
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def pct(value: float | None) -> str:
        return "—" if value is None else f"{value:.1%}"

    def pnl(value: float | None) -> str:
        return "—" if value is None else f"{value:+.4f}"

    lines = [
        "# T058 可用深度、价差、波动与成交流因子叠加验证", "",
        "## 口径", "",
        f"- 数据来自T055成熟策略逐笔记录，共{len(rows)}笔；UP/DOWN分开。",
        "- 成熟基线已包含：实际主动买入≥10份、VWAP 0.80–0.85、剩余90–30秒、盘口年龄≤2秒、入场前5秒同方向动量≥0.01。",
        "- 这里只做预先登记的少量因子对照，不搜索最佳阈值，不修改纸上交易或实盘。收益使用源记录的扣费后每份净收益。", "",
        "## 结果（仅作为增量参考）", "",
        "| 方向 | 因子 | 笔数 | 胜率 | 平均净收益/份 | 保留率 | 基线胜率 | 基线收益/份 |", "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in results:
        lines.append(f"| {row['outcome']} | {row['label']} | {row['trades']} | {pct(row['winRate'])} | {pnl(row['avgPnlPerShare'])} | {pct(row['retainedRate'])} | {pct(row['baseline']['winRate'])} | {pnl(row['baseline']['avgPnlPerShare'])} |")
    lines += [
        "", "## 解读", "",
        "- depthAt85是入场快照中0.85价格上限内可执行数量的字段；它能衡量10份成交的余量，但不是未来盘口稳定性的完整替代。",
        "- spread、range15和10秒成交量是相关状态变量，不能和5秒动量简单相加；需要看样本量、保留率和样本外稳定性。",
        "- 1/2/3/10秒动量和盘口连续稳定时长在当前T055逐笔源记录中没有独立字段，本轮不伪造计算；需要后续采集时保存原始快照后再测。",
        "- MAE等入场后的路径字段只用于失败诊断，不能作为入场条件；本轮已排除，避免未来信息泄漏。",
        "- 任何高于基线的分组，至少要跨新增日期、每方向约100笔后才考虑成为软过滤；少于30笔只标记为小样本。", "",
        "## 复现", "", "```bash", "PYTHONDONTWRITEBYTECODE=1 python3 scripts/analysis/analyze_factor_overlay.py", "```", "",
    ]
    (out / "T058可用因子叠加验证.md").write_text("\n".join(lines), encoding="utf-8")
    print(out / "T058可用因子叠加验证.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
