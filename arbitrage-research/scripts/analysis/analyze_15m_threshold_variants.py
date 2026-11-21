#!/usr/bin/env python3
"""15m adaptation grid for the mature 5m timing/distance idea.

This is a direction-assistance backtest, not an executable Polymarket fill
backtest: the 15m collector currently has no independent 15m VWAP/order rows.
Thresholds are frozen before scoring on the later 30% of markets.
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path
from statistics import mean


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = ROOT / "outputs/research_tests/e15_15m_binance_distance_probability_20260902/e15_probability_validation_rows.csv"
DEFAULT_OUT = ROOT / "outputs/research_tests/e15_15m_binance_distance_probability_20260902"
TIME_WINDOWS = {
    "180–60秒（按5分钟90–30同比例）": {180, 120, 90, 60},
    "120–30秒": {120, 90, 60, 30},
    "90–30秒": {90, 60, 30},
    "60–30秒": {60, 30},
}
DISTANCE_THRESHOLDS = (0, 5, 10, 15, 20)
MOMENTUM_THRESHOLDS = (0, 1, 2, 3, 5)


def num(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


def load(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for raw in csv.DictReader(handle):
            distance = num(raw.get("distanceBps"))
            momentum = num(raw.get("return5sBps"))
            target = num(raw.get("targetSeconds"))
            label = int(raw["labelUp"])
            if distance is None or target is None:
                continue
            predicted_distance = 1 if distance >= 0 else 0
            momentum_same = (
                momentum is not None
                and abs(momentum) > 0
                and (momentum >= 0) == (predicted_distance == 1)
            )
            rows.append({
                "marketId": raw.get("marketId"),
                "targetSeconds": int(target),
                "labelUp": label,
                "distanceBps": distance,
                "momentum5Bps": momentum,
                "predictedDistance": predicted_distance,
                "distanceCorrect": predicted_distance == label,
                "momentumSame": momentum_same,
            })
    return rows


def summary(rows: list[dict], label: str, threshold: float | None = None, momentum_threshold: float | None = None) -> dict:
    correct = sum(bool(row["distanceCorrect"]) for row in rows)
    return {
        "window": label,
        "distanceThresholdBps": threshold,
        "momentumThresholdBps": momentum_threshold,
        "samples": len(rows),
        "correct": correct,
        "accuracy": correct / len(rows) if rows else None,
        "upPredictions": sum(int(row["predictedDistance"]) for row in rows),
        "downPredictions": sum(int(not row["predictedDistance"]) for row in rows),
    }


def fmt_rate(value: float | None) -> str:
    return "—" if value is None else f"{value:.1%}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    rows = load(args.input)
    result_rows: list[dict] = []
    combo_rows: list[dict] = []
    for window, targets in TIME_WINDOWS.items():
        base = [row for row in rows if row["targetSeconds"] in targets]
        for threshold in DISTANCE_THRESHOLDS:
            selected = [row for row in base if abs(row["distanceBps"]) >= threshold]
            result_rows.append(summary(selected, window, threshold, None))
            for momentum_threshold in MOMENTUM_THRESHOLDS[1:]:
                combined = [
                    row for row in selected
                    if row["momentumSame"] and row["momentum5Bps"] is not None
                    and abs(row["momentum5Bps"]) >= momentum_threshold
                ]
                combo_rows.append(summary(combined, window, threshold, momentum_threshold))

    args.out.mkdir(parents=True, exist_ok=True)
    detail_path = args.out / "15m_threshold_grid.csv"
    with detail_path.open("w", encoding="utf-8-sig", newline="") as handle:
        fields = list(result_rows[0])
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(result_rows)
    combo_path = args.out / "15m_threshold_momentum_grid.csv"
    with combo_path.open("w", encoding="utf-8-sig", newline="") as handle:
        fields = list(combo_rows[0])
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(combo_rows)

    lines = [
        "# 15分钟市场：成熟5分钟策略阈值适配回测", "",
        "## 口径", "",
        "5分钟成熟策略的90–30秒约占一个5分钟市场最后20%的时间，因此15分钟先测试180–60秒。Binance Spot距离阈值按0/5/10/15/20 bps逐级提高；在此基础上再要求5秒动量与距离方向一致，并测试1/2/3/5 bps。所有阈值预先固定，在E15后30%市场做样本外评分。", "",
        "本轮只有官方UP/DOWN标签和跨市场特征，没有独立的15分钟Polymarket VWAP、盘口深度、主动成交和纸上订单，因此结果是方向辅助准确率，不是实际成交收益率。", "",
        f"- 输入验证行：{len(rows)}；来源：`{args.input}`。", "",
        "## 一、只提高Binance距离阈值", "",
        "| 时间窗口 | 距离阈值 | 样本 | 准确率 | UP预测 | DOWN预测 |", "|---|---:|---:|---:|---:|---:|",
    ]
    for row in result_rows:
        lines.append(f"| {row['window']} | ≥{row['distanceThresholdBps']} bps | {row['samples']} | {fmt_rate(row['accuracy'])} | {row['upPredictions']} | {row['downPredictions']} |")
    lines += ["", "## 二、距离阈值 + 5秒同向动量", "", "这里的“同向”是动量符号与距离判断出的方向一致；实际使用时不需要知道最终赢家。", "", "| 时间窗口 | 距离阈值 | 5秒动量阈值 | 样本 | 准确率 |", "|---|---:|---:|---:|---:|"]
    for row in combo_rows:
        if row["distanceThresholdBps"] in {0, 5, 10, 15} and row["momentumThresholdBps"] in {1, 2, 3}:
            lines.append(f"| {row['window']} | ≥{row['distanceThresholdBps']} bps | ≥{row['momentumThresholdBps']} bps | {row['samples']} | {fmt_rate(row['accuracy'])} |")
    lines += ["", "## 三、初步解读", "", "- 时间比例映射的首选观察窗口是180–60秒；它与5分钟成熟策略的90–30秒处在相近的生命周期位置。", "- 阈值从0提高到5或10 bps，通常会减少贴线噪声，但会明显降低样本量；必须同时看准确率和保留样本，不能只挑最高百分比。", "- 5秒动量只适合做确认/加分：如果叠加后样本过少，不能称为收益改善。", "- 15分钟没有可成交的Polymarket价格记录，所以目前不能回答‘0.85–0.90入场是否比0.80–0.85更赚钱’；需要先让15分钟纸上监控记录实际成交。", "- 本报告不修改任何纸上交易或实盘配置。", "", "## 建议", "", "先把180–60秒作为15分钟盘中参考窗口，显示Binance距离分档；距离≥5 bps、且5秒动量同向，可作为较强提示。是否升级为硬过滤，要等独立15分钟成交记录和更多日期样本外复核。", "", "## 复现", "", "```bash", "cd /Users/laochen/Documents/lianghua/arbitrage-design/arbitrage-research", "PYTHONDONTWRITEBYTECODE=1 python3 scripts/analysis/analyze_15m_threshold_variants.py", "```", ""]
    report = args.out / "15分钟成熟策略阈值适配回测.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    print(report)
    print(detail_path)
    print(combo_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
