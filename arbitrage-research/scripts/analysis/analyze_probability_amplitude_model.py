#!/usr/bin/env python3
"""E17: causal probability model with historical and current-session amplitude.

This deliberately uses a small, registered model ladder rather than searching
hundreds of thresholds.  E15's isotonic time/distance probability is M0; M1
adds a causal historical-amplitude normalization, M2 adds the current-session
range, and M3 adds short returns and trade imbalance.  All models are fit on
the first 70% of market starts and scored on the later 30%.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
import json
import math
from pathlib import Path
from statistics import mean, median
import sys
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.analysis.analyze_binance_distance_probability import (
    DEFAULT_CROSS,
    DEFAULT_REGIME,
    entry_cost_per_share,
    fit_probability_tables,
    join_mature_entries,
    load_features,
    load_labels,
    predict_probability,
)


DEFAULT_OUT = ROOT / "outputs/research_tests/e16_positive_upcross_probability_20260830/e17_amplitude_probability"
MODEL_LAYERS = {
    "M0 时间+距离": (),
    "M1 +历史幅度": ("normalizedDistanceBps",),
    "M2 +本场幅度": ("normalizedDistanceBps", "currentRangeRatio", "currentPosition"),
    "M3 +即时状态": (
        "normalizedDistanceBps", "currentRangeRatio", "currentPosition",
        "return5sBps", "return15sBps", "return60sBps", "tradeImbalance60s",
    ),
}


def _logit(probability: float) -> float:
    p = min(1 - 1e-6, max(1e-6, probability))
    return math.log(p / (1 - p))


def augment(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add amplitude features using only observations at or before each row."""
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["marketId"])].append(dict(row))
    market_stats: list[tuple[int, str, float, float]] = []
    for market_id, values in grouped.items():
        values.sort(key=lambda item: int(item["sourceUs"]))
        distances = [float(item["distanceBps"]) for item in values]
        close_proxy = distances[-1]
        full_range = max([0.0, *distances]) - min([0.0, *distances])
        market_stats.append((int(values[0]["marketStartUs"]), market_id, abs(close_proxy), full_range))
        low, high = 0.0, 0.0
        for item, distance in zip(values, distances):
            low, high = min(low, distance), max(high, distance)
            item["currentRangeBps"] = high - low
            item["currentPosition"] = (distance - low) / (high - low) if high > low else 0.5
        grouped[market_id] = values
    market_stats.sort()
    result: list[dict[str, Any]] = []
    for index, (start_us, market_id, _, _) in enumerate(market_stats):
        prior = market_stats[max(0, index - 12):index]
        history_count = len(prior)
        hist_move = median(item[2] for item in prior) if prior else None
        hist_range = median(item[3] for item in prior) if prior else None
        for item in grouped[market_id]:
            item["historyCount"] = history_count
            item["historicalMoveBps"] = hist_move
            item["historicalRangeBps"] = hist_range
            if hist_move is None or hist_range is None:
                item["normalizedDistanceBps"] = None
                item["currentRangeRatio"] = None
            else:
                denominator = max(1.0, float(hist_move))
                range_denominator = max(1.0, float(hist_range))
                item["normalizedDistanceBps"] = float(item["distanceBps"]) / denominator
                item["currentRangeRatio"] = float(item["currentRangeBps"]) / range_denominator
            result.append(item)
    return sorted(result, key=lambda item: (int(item["marketStartUs"]), int(item["sourceUs"])))


def split_rows(rows: list[dict[str, Any]], fraction: float = 0.70) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    starts = sorted({int(row["marketStartUs"]) for row in rows})
    split_index = max(1, int(len(starts) * fraction))
    split_start = starts[split_index]
    train = [row for row in rows if int(row["marketStartUs"]) < split_start]
    validation = [row for row in rows if int(row["marketStartUs"]) >= split_start]
    return train, validation, split_start


def _matrix(rows: list[dict[str, Any]], columns: tuple[str, ...], medians: dict[str, float] | None = None) -> tuple[np.ndarray, dict[str, float]]:
    if medians is None:
        medians = {}
        for column in columns:
            values = [float(row[column]) for row in rows if row.get(column) is not None and math.isfinite(float(row[column]))]
            medians[column] = median(values) if values else 0.0
    values = []
    for row in rows:
        values.append([float(row[column]) if row.get(column) is not None else medians[column] for column in columns])
    return np.asarray(values, dtype=float), medians


def fit_logistic(train: list[dict[str, Any]], columns: tuple[str, ...], ridge: float = 3.0) -> dict[str, Any]:
    raw, medians = _matrix(train, columns)
    means = raw.mean(axis=0) if raw.size else np.zeros(len(columns))
    scales = raw.std(axis=0) if raw.size else np.ones(len(columns))
    scales = np.where(scales < 1e-8, 1.0, scales)
    # Robustify the small registered model against sparse/separable tails.  A
    # few amplitude observations can be much larger than the typical range;
    # clipping the standardized feature is preferable to allowing Newton
    # updates to overflow and silently produce NaN probabilities.
    standardized = np.clip((raw - means) / scales, -10.0, 10.0)
    base = np.asarray([_logit(float(row["probabilityM0"])) for row in train])
    X = np.column_stack([np.ones(len(train)), base, standardized])
    y = np.asarray([int(row["labelUp"]) for row in train], dtype=float)
    beta = np.zeros(X.shape[1], dtype=float)
    for _ in range(80):
        beta = np.nan_to_num(beta, nan=0.0, posinf=20.0, neginf=-20.0)
        safe_beta = np.clip(beta, -20.0, 20.0)
        logits = np.clip(np.sum(X * safe_beta, axis=1), -30, 30)
        p = 1 / (1 + np.exp(-logits))
        weights = np.maximum(p * (1 - p), 1e-5)
        penalty = np.diag(np.r_[0.0, np.full(X.shape[1] - 1, ridge)])
        hessian = X.T @ (weights[:, None] * X) + penalty
        gradient = X.T @ (p - y) + penalty @ safe_beta
        try:
            step = np.linalg.solve(hessian, gradient)
        except np.linalg.LinAlgError:
            break
        if not np.all(np.isfinite(step)):
            # Numerical failure in a very sparse/separable feature tail:
            # retain the last finite iterate instead of propagating NaNs.
            break
        beta = np.clip(beta - step, -20.0, 20.0)
        if float(np.max(np.abs(step))) < 1e-7:
            break
    return {"columns": columns, "medians": medians, "means": means.tolist(), "scales": scales.tolist(), "beta": beta.tolist(), "ridge": ridge}


def predict_model(rows: list[dict[str, Any]], model: dict[str, Any]) -> list[float]:
    columns = tuple(model["columns"])
    raw, _ = _matrix(rows, columns, dict(model["medians"]))
    standardized = np.clip((raw - np.asarray(model["means"])) / np.asarray(model["scales"]), -10.0, 10.0)
    base = np.asarray([_logit(float(row["probabilityM0"])) for row in rows])
    X = np.column_stack([np.ones(len(rows)), base, standardized])
    beta = np.nan_to_num(np.asarray(model["beta"], dtype=float), nan=0.0, posinf=20.0, neginf=-20.0)
    logits = np.clip(np.sum(X * np.clip(beta, -20.0, 20.0), axis=1), -30, 30)
    return (1 / (1 + np.exp(-logits))).tolist()


def score(rows: list[dict[str, Any]], probabilities: list[float]) -> dict[str, Any]:
    if not rows:
        return {"samples": 0, "accuracy": None, "brier": None, "logLoss": None}
    eps = 1e-9
    y = [int(row["labelUp"]) for row in rows]
    return {
        "samples": len(rows),
        "accuracy": mean(int((p >= 0.5) == bool(label)) for p, label in zip(probabilities, y)),
        "brier": mean((p - label) ** 2 for p, label in zip(probabilities, y)),
        "logLoss": mean(-(label * math.log(max(eps, p)) + (1 - label) * math.log(max(eps, 1 - p))) for p, label in zip(probabilities, y)),
    }


def attach_mature_models(
    path: Path, cross: Path, tables: dict[int, list[float]], split_start_us: int,
    augmented: list[dict[str, Any]], models: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    mature = join_mature_entries(path, cross, tables, split_start_us)
    by_market: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in augmented:
        by_market[str(row["marketId"])].append(row)
    for values in by_market.values():
        values.sort(key=lambda item: int(item["sourceUs"]))
    output = []
    for row in mature:
        values = by_market.get(str(row["marketId"]), [])
        times = [int(item["sourceUs"]) for item in values]
        position = bisect_right(times, int(row["executionUs"])) - 1
        if position < 0:
            continue
        selected = values[position]
        age_ms = (int(row["executionUs"]) - int(selected["sourceUs"])) / 1000
        if age_ms < 0 or age_ms > 35_000:
            continue
        item = dict(row)
        item["amplitudeAlignmentAgeMs"] = age_ms
        item["amplitudeObservationTargetSeconds"] = selected.get("targetSeconds")
        item["probabilityM0"] = float(row["binanceProbabilityUp"])
        for name, model in models.items():
            item[name] = predict_model([selected], model)[0] if name != "M0 时间+距离" else item["probabilityM0"]
        output.append(item)
    return output


def strategy_summary(rows: list[dict[str, Any]], probability_key: str, threshold: float) -> dict[str, Any]:
    selected = []
    for row in rows:
        p_up = float(row[probability_key])
        p_entry = p_up if row.get("outcome") == "UP" else 1 - p_up
        if p_entry >= threshold:
            selected.append(row)
    wins = sum(bool(row.get("won")) for row in selected)
    return {
        "trades": len(selected), "wins": wins, "losses": len(selected) - wins,
        "winRate": wins / len(selected) if selected else None,
        "avgPnlPerShare": mean(float(row["pnlPerShare"]) for row in selected) if selected else None,
    }


def fmt(value: float | None, digits: int = 4) -> str:
    return "—" if value is None else f"{value:+.{digits}f}"


def main() -> int:
    parser = __import__("argparse").ArgumentParser(description="E17历史幅度与本场幅度概率计算器")
    parser.add_argument("--cross-dir", type=Path, default=DEFAULT_CROSS)
    parser.add_argument("--regime-input", type=Path, default=DEFAULT_REGIME)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    labels = load_labels(args.cross_dir)
    base_rows = load_features(args.cross_dir, labels)
    rows = augment(base_rows)
    rows = [row for row in rows if int(row["historyCount"]) >= 3]
    train, validation, split_start_us = split_rows(rows)
    tables = fit_probability_tables(train)
    for collection in (train, validation):
        for row in collection:
            row["probabilityM0"], row["modelTargetSeconds"] = predict_probability(float(row["distanceBps"]), float(row["remainingSeconds"]), tables)
    models = {name: fit_logistic(train, columns) for name, columns in MODEL_LAYERS.items() if columns}
    model_probabilities: dict[str, dict[str, list[float]]] = {"M0 时间+距离": {"train": [float(row["probabilityM0"]) for row in train], "validation": [float(row["probabilityM0"]) for row in validation]}}
    for name, model in models.items():
        model_probabilities[name] = {"train": predict_model(train, model), "validation": predict_model(validation, model)}
    metrics = {name: {"train": score(train, values["train"]), "validation": score(validation, values["validation"])} for name, values in model_probabilities.items()}

    mature = attach_mature_models(args.regime_input, args.cross_dir, tables, split_start_us, rows, models)
    strategy_rows = []
    for threshold in (0.55, 0.60, 0.65, 0.70):
        for name in MODEL_LAYERS:
            key = "probabilityM0" if name == "M0 时间+距离" else name
            if mature:
                strategy_rows.append({"model": name, "threshold": threshold, **strategy_summary(mature, key, threshold)})

    def local(us: int) -> str:
        from datetime import datetime, timezone, timedelta
        return datetime.fromtimestamp(us / 1_000_000, timezone(timedelta(hours=8))).isoformat()

    payload = {
        "schemaVersion": 1, "experiment": "E17_amplitude_probability",
        "data": {"labels": len(labels), "baseRows": len(base_rows), "rows": len(rows), "trainRows": len(train), "validationRows": len(validation), "trainMarkets": len({r['marketId'] for r in train}), "validationMarkets": len({r['marketId'] for r in validation}), "splitStartLocal": local(split_start_us)},
        "featureDefinitions": {"historicalMove": "前12个市场的|距离开盘到近收盘代理值|中位数", "historicalRange": "前12个市场的距离路径高低幅中位数", "currentRange": "本市场截至当前特征快照的距离高低幅", "note": "E17首版使用E15离散特征快照，因此本场幅度是粗粒度代理；不读取未来路径"},
        "metrics": metrics, "strategyValidation": strategy_rows,
    }
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "e17_amplitude_probability.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (args.out / "e17_amplitude_rows.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = [
        "# E17 历史幅度与本场幅度概率计算器验证", "",
        "## 数据和防泄漏", "",
        f"- 官方标签{len(labels)}个；E15特征{len(base_rows)}行；因历史幅度至少需要3个先前市场，最终使用{len(rows)}行。",
        f"- 训练：{len(train)}行/{len({r['marketId'] for r in train})}个市场；时间外验证：{len(validation)}行/{len({r['marketId'] for r in validation})}个市场。",
        "- 历史幅度只取当前市场开始前的12个市场；本场幅度只取当前快照及之前的路径；结算标签只用于评分。",
        "- E17首版使用E15的离散特征快照，本场高低幅是可复核的粗粒度代理，不冒充逐秒高低价。", "",
        "## 一、模型层级", "",
        "| 模型 | 新增特征 | 时间外准确率 | Brier | LogLoss |",
        "|---|---|---:|---:|---:|",
    ]
    for name in MODEL_LAYERS:
        metric = metrics[name]["validation"]
        added = "E15基线" if name == "M0 时间+距离" else "、".join(MODEL_LAYERS[name])
        lines.append(f"| {name} | {added} | {metric['accuracy']:.1%} | {metric['brier']:.4f} | {metric['logLoss']:.4f} |")
    lines += ["", "## 二、成熟策略上的概率门槛", "", "仅统计能在入场前35秒内对齐到E17快照的成熟策略行；门槛只是离线对照，不修改纸上交易。", "", "| 模型 | 概率门槛 | 笔数 | 胜率 | 平均净收益/份 |", "|---|---:|---:|---:|---:|"]
    for row in strategy_rows:
        win_rate = "—" if row["winRate"] is None else f"{row['winRate']:.1%}"
        lines.append(f"| {row['model']} | {row['threshold']:.2f} | {row['trades']} | {win_rate} | {fmt(row['avgPnlPerShare'])} |")
    lines += ["", "## 三、如何使用", "", "- M0是当前E15基线；只有M1/M2/M3在时间外同时改善Brier、LogLoss和扣费净收益，才有资格成为辅助因子。", "- 若模型只提高胜率却减少大量机会，优先显示净概率边际，不直接设硬门槛。", "- E17的历史幅度仍是粗粒度代理；下一版可用Binance逐秒快照计算真实本场高低幅，再与E16正向突破按同一入场时点连接。", "- 任何100%行都必须同时查看样本数、时间外稳定性和Wilson下界。", "", "## 复现", "", "```bash", "PYTHONDONTWRITEBYTECODE=1 python3 scripts/analysis/analyze_probability_amplitude_model.py", "```", ""]
    (args.out / "E17_历史幅度与本场幅度概率计算器验证.md").write_text("\n".join(lines), encoding="utf-8")
    print(args.out / "E17_历史幅度与本场幅度概率计算器验证.md")
    print(args.out / "e17_amplitude_probability.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
