#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T051：当前成熟策略的高区分度因子及时段参考分层。"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = ROOT / "outputs/research_tests/e13_strategy_matrix_20260828/market_regime_results.json"
DEFAULT_OUT = ROOT / "outputs/research_tests/t051_strategy_time_factors_20260828"
DEFAULT_PROBABILITY = ROOT / "outputs/research_tests/e15_binance_distance_probability_20260828/e15_binance_distance_probability.json"


def stats(rows: list[dict]) -> dict:
    wins = sum(bool(row.get("won")) for row in rows)
    return {"trades": len(rows), "wins": wins, "losses": len(rows) - wins,
            "winRate": wins / len(rows) if rows else None,
            "avgPnlPerShare": mean(float(row["pnlPerShare"]) for row in rows) if rows else None}


def rate(value: float | None) -> str:
    return "—" if value is None else f"{value:.1%}"


def pnl(value: float | None) -> str:
    return "—" if value is None else f"{value:+.4f}"


def main() -> int:
    parser = argparse.ArgumentParser(description="当前成熟策略因子和北京时间时段分层")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--probability", type=Path, default=DEFAULT_PROBABILITY)
    args = parser.parse_args()
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    source = [dict(row) for row in payload.get("rows", [])]
    base = [row for row in source if 0.80 <= float(row.get("entryVwap", -1)) <= 0.85 and 30 <= float(row.get("remainingSeconds", -1)) <= 90]
    candidate = [row for row in base if row.get("outcome") == "DOWN" and row.get("momentum5") is not None and float(row["momentum5"]) >= 0.01]

    factor_rows = []
    for outcome in ("DOWN", "UP"):
        group = [row for row in base if row.get("outcome") == outcome]
        factors = [
            ("基线", lambda row: True),
            ("5秒同方向动量≥0.01", lambda row: row.get("momentum5") is not None and float(row["momentum5"]) >= 0.01),
            ("5秒动量不回落≥0", lambda row: row.get("momentum5") is not None and float(row["momentum5"]) >= 0),
            ("Binance Spot综合信号同向", lambda row: row.get("spotSignal") == outcome),
            ("10秒同方向主动买量≥反向", lambda row: row.get("sameBuyVolume10s") is not None and row.get("oppositeBuyVolume10s") is not None and float(row["sameBuyVolume10s"]) >= float(row["oppositeBuyVolume10s"])),
            ("入场前15秒价格区间<0.20", lambda row: row.get("range15") is not None and float(row["range15"]) < 0.20),
            ("预测价差≤0.01", lambda row: row.get("spread") is not None and float(row["spread"]) <= 0.01),
        ]
        for label, predicate in factors:
            selected = [row for row in group if predicate(row)]
            factor_rows.append({"outcome": outcome, "factor": label, **stats(selected), "retainedRate": len(selected) / len(group) if group else None})

    def dt(row: dict) -> datetime:
        return datetime.fromisoformat(str(row["localTime"]))

    def time_groups(rows: list[dict]) -> list[dict]:
        def decimal_hour(row: dict) -> float:
            value = dt(row)
            return value.hour + value.minute / 60 + value.second / 3600

        groups: list[tuple[str, list[dict]]] = []
        groups.append(("中国白天 08:00–20:00", [r for r in rows if 8 <= dt(r).hour < 20]))
        groups.append(("北京时间夜间/美国白盘宽口径 20:00–08:00", [r for r in rows if dt(r).hour >= 20 or dt(r).hour < 8]))
        groups.append(("美国白盘参考 21:30–04:30", [r for r in rows if decimal_hour(r) >= 21.5 or decimal_hour(r) < 4.5]))
        groups.append(("非美国白盘参考", [r for r in rows if not (decimal_hour(r) >= 21.5 or decimal_hour(r) < 4.5)]))
        groups.append(("工作日", [r for r in rows if dt(r).weekday() < 5]))
        groups.append(("周末", [r for r in rows if dt(r).weekday() >= 5]))
        for weekday in range(7):
            groups.append((("周一", "周二", "周三", "周四", "周五", "周六", "周日")[weekday], [r for r in rows if dt(r).weekday() == weekday]))
        return [{"group": label, **stats(selected)} for label, selected in groups]

    hour_rows = []
    for hour in range(24):
        selected = [row for row in candidate if dt(row).hour == hour]
        if selected:
            hour_rows.append({"group": f"{hour:02d}:00–{(hour + 1) % 24:02d}:00", **stats(selected)})
    output = {"schemaVersion": 1, "experiment": "T051_strategy_time_factors", "source": str(args.input),
              "data": {"sourceRows": len(source), "baseline": len(base), "downMomentumCandidate": len(candidate)},
              "factorRows": factor_rows, "baselineTimeGroups": time_groups(base), "downCandidateTimeGroups": time_groups(candidate), "downCandidateHourly": hour_rows}
    probability = None
    if args.probability.exists():
        try:
            probability = json.loads(args.probability.read_text(encoding="utf-8"))
            output["binanceDistanceProbability"] = probability
        except (OSError, json.JSONDecodeError):
            probability = None
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "t051_strategy_time_factors.json").write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = ["# T051 当前成熟策略的高区分度因子与时段参考", "", "## 口径", "",
             f"- 数据来自E13刷新后的成熟基线，共{len(base)}笔：实际VWAP 0.80–0.85、剩余90–30秒、真实主动买入≥10份、0.85上限内10份完整可执行成交、盘口年龄≤2秒，收益沿用已扣手续费的`pnlPerShare`。",
             f"- 当前主候选定义为DOWN且入场前5秒动量≥0.01，共{len(candidate)}笔。",
             "- 这些是历史分层参考；不同日期、方向和时段样本不均衡，不把点估计直接当成硬过滤。", "", "## 一、值得盘中参考的因子", "",
             "| 方向 | 因子 | 笔数 | 胜率 | 平均净收益/份 | 保留率 |", "|---|---|---:|---:|---:|---:|"]
    for row in factor_rows:
        lines.append(f"| {row['outcome']} | {row['factor']} | {row['trades']} | {rate(row['winRate'])} | {pnl(row['avgPnlPerShare'])} | {rate(row['retainedRate'])} |")
    lines += ["", "## 二、Binance Spot距离+时间概率（单独报告，不与分类因子混表）", "", "Binance Spot相对本轮五分钟开盘价的距离，结合剩余时间，已经做成时间外概率模型；它不是单一的‘同向/不同向’标签，所以单独列出。", ""]
    if probability:
        overall = probability.get("overallValidation") or {}
        lines.append(f"- 后30%市场时间外：{overall.get('samples', '—')}条观察，分类准确率{rate(overall.get('accuracy'))}，Brier `{overall.get('brier'):.4f}`。" if overall.get("brier") is not None else f"- 后30%市场时间外：{overall.get('samples', '—')}条观察，分类准确率{rate(overall.get('accuracy'))}。")
        by_target = {int(row["targetSeconds"]): row for row in probability.get("byTargetValidation", [])}
        for target in (120, 90, 60, 30):
            row = by_target.get(target)
            if row:
                lines.append(f"- 剩余{target}秒：准确率{rate(row.get('accuracy'))}，仅看距离正负为{rate(row.get('signAccuracy'))}。")
        lines += ["- 这个概率可以用于盘中显示‘方向置信度’，尤其在剩余60秒或30秒；但在当前0.80–0.85成熟策略中，Binance概率与预测市场价格本身有重复信息，不能直接当成UP/DOWN统一硬过滤。"]
    else:
        lines.append("- E15概率文件未找到；请先运行E15命令生成。")
    lines += ["", "## 三、中国白天、美国白盘、工作日与周末", "", "### 成熟基线（UP/DOWN合计）", "", "| 分组 | 笔数 | 胜率 | 平均净收益/份 |", "|---|---:|---:|---:|"]
    for row in output["baselineTimeGroups"]:
        lines.append(f"| {row['group']} | {row['trades']} | {rate(row['winRate'])} | {pnl(row['avgPnlPerShare'])} |")
    lines += ["", "### 当前主候选（DOWN + 5秒动量≥0.01）", "", "| 分组 | 笔数 | 胜率 | 平均净收益/份 |", "|---|---:|---:|---:|"]
    for row in output["downCandidateTimeGroups"]:
        lines.append(f"| {row['group']} | {row['trades']} | {rate(row['winRate'])} | {pnl(row['avgPnlPerShare'])} |")
    lines += ["", "### 主候选按北京时间小时（仅展示全部已有小时）", "", "| 小时 | 笔数 | 胜率 | 平均净收益/份 |", "|---|---:|---:|---:|"]
    for row in hour_rows:
        lines.append(f"| {row['group']} | {row['trades']} | {rate(row['winRate'])} | {pnl(row['avgPnlPerShare'])} |")
    lines += ["", "## 四、当前能得到的实际结论", "", "- 最值得盘中参考的单一因子仍是入场前5秒同方向动量≥0.01：DOWN全样本124笔、91.1%、+0.0808/份；UP为101笔、89.1%、+0.0615/份。它不是保证，只表示入场前价格确实在向所选方向继续走。",
              "- Binance Spot综合方向同向也有参考价值：DOWN 135笔、89.6%、+0.0631/份；UP 133笔、85.7%、+0.0247/份，但它过滤掉不少机会，且不是每个时间外分段都稳定。",
              "- 5秒动量不回落≥0比≥0.01宽松，整体结果明显弱于≥0.01；不要为了增加信号而放宽。",
              "- 10秒主动买量、15秒平稳、价差≤0.01可作为辅助观察，但目前覆盖率或方向一致性不足，不能单独决定下单。",
              "- 时间上，当前样本的中国白天(08–20)基线胜率和收益高于北京时间夜间/美国白盘宽口径；但美国白盘精确窗口只有少量样本，星期/周末差异也未跨足够多日期，暂不建议按时段停做。",
              "- 盘中可把‘主候选 + 5秒动量 + Binance方向 + 时段’作为信号强弱分层：主候选满足且5秒动量≥0.01为强；Binance同向作为加分；时段只作风险提示。", "",
              "## 五、为什么没有把小样本高点估计混入主表", "", "- 不是遗漏，而是分层：主表优先展示约100笔量级、且跨时间外仍可复核的因子；小样本高胜率单独作为观察候选。", "- 例如UP在E15概率≥0.70的成熟策略子样本为20笔、100%、+0.1711/份；T050严格Binance同向+5秒不回落时间外为32笔、96.9%、+0.1392/份。这些值得盘中参考，但样本仍小，不能与124/217笔的主结果同等可信。", "- 另一类是改变入场语义的独立策略，例如E12最先突破0.65、剩余90秒内67笔、73.1%、+0.0546/份；它不能直接当作当前90–30秒成熟策略的因子。", "- 这些候选已保留在E12、E13、E15和T050报告中，后续新增数据会继续复验。", "", "## 六、风险边界", "", "- 历史胜率不是未来胜率；本报告没有把时段或因子加入实盘。", "- 所有平均收益都是每份净收益，不等于固定5U下的每日收益；还会受成交数量、滑点和实际卖出影响。", "- 需要至少跨更多自然日、每个主要方向新增约100笔，才能判断时段差异是否真实。", "", "## 复现", "", "```bash", "PYTHONDONTWRITEBYTECODE=1 python3 scripts/analysis/analyze_current_strategy_time_factors.py", "```", ""]
    (args.out / "T051_当前策略因子与时段参考.md").write_text("\n".join(lines), encoding="utf-8")
    print(args.out / "T051_当前策略因子与时段参考.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
