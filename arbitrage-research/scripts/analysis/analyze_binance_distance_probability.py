#!/usr/bin/env python3
"""E15：Binance相对五分钟开盘距离、剩余时间与官方结算概率验证。"""

from __future__ import annotations

import argparse
from bisect import bisect_right
import csv
from datetime import datetime
import gzip
import json
import math
from pathlib import Path
from statistics import mean
from typing import Any, Iterable
import zlib
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CROSS = ROOT / "data/cross_market_validation"
DEFAULT_REGIME = ROOT / "outputs/research_tests/e13_strategy_matrix_20260828/market_regime_results.json"
DEFAULT_OUT = ROOT / "outputs/research_tests/e15_binance_distance_probability_20260828"
DISTANCE_BINS = (-math.inf, -50, -25, -10, -5, 0, 5, 10, 25, 50, math.inf)
PROBABILITY_THRESHOLDS = (0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90)
PROBABILITY_EDGE_THRESHOLDS = (0.00, 0.01, 0.02, 0.03, 0.05)
POLYMARKET_CRYPTO_TAKER_FEE_RATE = 0.07


def read_partition_rows(paths: Iterable[Path]) -> Iterable[dict[str, Any]]:
    for path in sorted(paths):
        opener = gzip.open if path.suffix == ".gz" else open
        try:
            with opener(path, "rt", encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                    except (json.JSONDecodeError, OSError, EOFError):
                        continue
                    if isinstance(row, dict):
                        yield row
        except (OSError, EOFError, gzip.BadGzipFile, zlib.error):
            continue


def distance_bin(value: float) -> int:
    for index in range(len(DISTANCE_BINS) - 1):
        if DISTANCE_BINS[index] <= value < DISTANCE_BINS[index + 1]:
            return index
    raise ValueError(value)


def pava(values: list[float], weights: list[float]) -> list[float]:
    """Weighted non-decreasing isotonic fit."""
    blocks: list[dict[str, Any]] = []
    for index, (value, weight) in enumerate(zip(values, weights)):
        blocks.append({"start": index, "end": index, "sum": value * weight, "weight": weight})
        while len(blocks) >= 2:
            left, right = blocks[-2], blocks[-1]
            if left["sum"] / left["weight"] <= right["sum"] / right["weight"]:
                break
            blocks[-2:] = [{
                "start": left["start"], "end": right["end"],
                "sum": left["sum"] + right["sum"],
                "weight": left["weight"] + right["weight"],
            }]
    fitted = [0.5] * len(values)
    for block in blocks:
        value = block["sum"] / block["weight"]
        for index in range(block["start"], block["end"] + 1):
            fitted[index] = value
    return fitted


def load_labels(base: Path) -> dict[str, str]:
    labels: dict[str, str] = {}
    paths = (base / "labels").glob("date=*/hour=*/part-*.ndjson*")
    for row in read_partition_rows(paths):
        winner = str((row.get("payload") or {}).get("winnerOutcome") or "").upper()
        if winner in {"UP", "DOWN"}:
            labels[str(row.get("marketId"))] = winner
    return labels


def load_features(base: Path, labels: dict[str, str]) -> list[dict[str, Any]]:
    output: dict[tuple[str, int], dict[str, Any]] = {}
    paths = (base / "features").glob("date=*/hour=*/part-*.ndjson*")
    for row in read_partition_rows(paths):
        market_id = str(row.get("marketId") or "")
        winner = labels.get(market_id)
        values = (row.get("features") or {}).get("binance") or {}
        try:
            target = int(row["observationTargetSeconds"])
            remaining = float(row.get("remainingSeconds", target))
            distance_bps = float(values["returnFromStart"]) * 10_000
            source_age_ms = float(values.get("sourceAgeMs") or 0)
            market_start_us = int(row["marketStartUs"])
            source_us = int(row.get("sourceTimeUs") or row.get("receivedTimeUs") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if winner not in {"UP", "DOWN"} or abs(source_age_ms) > 5_000:
            continue
        item = {
            "marketId": market_id, "marketStartUs": market_start_us,
            "sourceUs": source_us, "targetSeconds": target,
            "remainingSeconds": remaining, "winner": winner,
            "labelUp": 1 if winner == "UP" else 0,
            "distanceBps": distance_bps,
            "return5sBps": _scaled(values.get("return5s")),
            "return15sBps": _scaled(values.get("return15s")),
            "return60sBps": _scaled(values.get("return60s")),
            "tradeImbalance60s": _number(values.get("tradeImbalance60s")),
            "sourceAgeMs": source_age_ms,
        }
        key = (market_id, target)
        prior = output.get(key)
        if prior is None or abs(remaining - target) < abs(float(prior["remainingSeconds"]) - target):
            output[key] = item
    return sorted(output.values(), key=lambda row: (row["marketStartUs"], row["targetSeconds"]))


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _scaled(value: Any) -> float | None:
    number = _number(value)
    return None if number is None else number * 10_000


def fit_probability_tables(rows: list[dict[str, Any]]) -> dict[int, list[float]]:
    targets = sorted({int(row["targetSeconds"]) for row in rows})
    global_up = (sum(int(row["labelUp"]) for row in rows) + 2) / (len(rows) + 4)
    tables: dict[int, list[float]] = {}
    for target in targets:
        target_rows = [row for row in rows if int(row["targetSeconds"]) == target]
        raw, weights = [], []
        for index in range(len(DISTANCE_BINS) - 1):
            selected = [row for row in target_rows if distance_bin(float(row["distanceBps"])) == index]
            # Weak symmetric prior prevents 0/1 probabilities in sparse tails.
            wins = sum(int(row["labelUp"]) for row in selected)
            raw.append((wins + 2 * global_up) / (len(selected) + 2))
            weights.append(max(1.0, float(len(selected))))
        tables[target] = pava(raw, weights)
    return tables


def predict_probability(distance_bps: float, remaining_seconds: float, tables: dict[int, list[float]]) -> tuple[float, int]:
    target = min(tables, key=lambda value: abs(value - remaining_seconds))
    return tables[target][distance_bin(distance_bps)], target


def metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"samples": 0, "accuracy": None, "signAccuracy": None, "brier": None, "logLoss": None}
    eps = 1e-9
    return {
        "samples": len(rows),
        "accuracy": mean(int((float(row["probabilityUp"]) >= 0.5) == bool(row["labelUp"])) for row in rows),
        "signAccuracy": mean(int((float(row["distanceBps"]) >= 0) == bool(row["labelUp"])) for row in rows),
        "brier": mean((float(row["probabilityUp"]) - int(row["labelUp"])) ** 2 for row in rows),
        "logLoss": mean(-(int(row["labelUp"]) * math.log(max(eps, float(row["probabilityUp"]))) + (1 - int(row["labelUp"])) * math.log(max(eps, 1 - float(row["probabilityUp"])))) for row in rows),
    }


def summarize_strategy(rows: list[dict[str, Any]]) -> dict[str, Any]:
    wins = sum(bool(row.get("won")) for row in rows)
    return {
        "trades": len(rows), "wins": wins, "losses": len(rows) - wins,
        "winRate": wins / len(rows) if rows else None,
        "avgPnlPerShare": mean(float(row["pnlPerShare"]) for row in rows) if rows else None,
    }


def entry_cost_per_share(entry_vwap: float) -> float:
    """Entry VWAP plus the crypto-market taker fee used by the backtests."""
    return entry_vwap + POLYMARKET_CRYPTO_TAKER_FEE_RATE * entry_vwap * (1 - entry_vwap)


def join_mature_entries(path: Path, base: Path, tables: dict[int, list[float]], split_start_us: int) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    source = [
        dict(row) for row in payload.get("rows", [])
        if int(row.get("executionUs") or 0) >= split_start_us
        and 0.80 <= float(row.get("entryVwap", -1)) <= 0.85
        and 30 <= float(row.get("remainingSeconds", -1)) <= 90
    ]
    market_ids = {str(row.get("marketId")) for row in source}
    snapshots: dict[str, list[dict[str, Any]]] = {market_id: [] for market_id in market_ids}
    paths = (base / "venue_snapshots").glob("date=*/hour=*/part-*.ndjson*")
    for row in read_partition_rows(paths):
        market_id = str(row.get("marketId") or "")
        if market_id not in snapshots:
            continue
        values = (row.get("features") or {}).get("binance") or {}
        try:
            received_us = int(row.get("receivedTimeUs") or 0)
            distance_bps = float(values["returnFromStart"]) * 10_000
        except (KeyError, TypeError, ValueError):
            continue
        snapshots[market_id].append({
            "receivedUs": received_us, "distanceBps": distance_bps,
            "return5sBps": _scaled(values.get("return5s")),
            "return15sBps": _scaled(values.get("return15s")),
            "sourceAgeMs": _number(values.get("sourceAgeMs")),
        })
    for values in snapshots.values():
        values.sort(key=lambda row: row["receivedUs"])
    joined: list[dict[str, Any]] = []
    for row in source:
        values = snapshots.get(str(row.get("marketId")), [])
        times = [int(value["receivedUs"]) for value in values]
        position = bisect_right(times, int(row["executionUs"])) - 1
        if position < 0:
            continue
        selected = values[position]
        age_us = int(row["executionUs"]) - int(selected["receivedUs"])
        if age_us < 0 or age_us > 3_000_000:
            continue
        probability_up, model_target = predict_probability(float(selected["distanceBps"]), float(row["remainingSeconds"]), tables)
        probability_for_entry = probability_up if row.get("outcome") == "UP" else 1 - probability_up
        entry_cost = entry_cost_per_share(float(row["entryVwap"]))
        joined.append({
            **row, "binanceDistanceBps": selected["distanceBps"],
            "binanceReturn5sBps": selected["return5sBps"],
            "binanceReturn15sBps": selected["return15sBps"],
            "binanceAlignmentAgeMs": age_us / 1_000,
            "binanceProbabilityUp": probability_up,
            "binanceProbabilityForEntry": probability_for_entry,
            "entryCostPerShare": entry_cost,
            "binanceProbabilityEdge": probability_for_entry - entry_cost,
            "probabilityModelTargetSeconds": model_target,
        })
    return joined


def fmt_rate(value: float | None) -> str:
    return "—" if value is None else f"{value:.1%}"


def fmt_number(value: float | None) -> str:
    return "—" if value is None else f"{value:.4f}"


def fmt_pnl(value: float | None) -> str:
    return "—" if value is None else f"{value:+.4f}"


def local_time(source_us: int) -> str:
    return datetime.fromtimestamp(source_us / 1_000_000, tz=ZoneInfo("Asia/Shanghai")).isoformat()


def bin_label(index: int) -> str:
    low, high = DISTANCE_BINS[index], DISTANCE_BINS[index + 1]
    left = "−∞" if math.isinf(low) else f"{low:g}"
    right = "+∞" if math.isinf(high) else f"{high:g}"
    return f"{left}–{right}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Binance距离-时间概率模型与成熟策略增量验证")
    parser.add_argument("--cross-dir", type=Path, default=DEFAULT_CROSS)
    parser.add_argument("--regime-input", type=Path, default=DEFAULT_REGIME)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    labels = load_labels(args.cross_dir)
    feature_rows = load_features(args.cross_dir, labels)
    market_starts = sorted({int(row["marketStartUs"]) for row in feature_rows})
    split_index = max(1, int(len(market_starts) * 0.70))
    split_start_us = market_starts[split_index]
    train = [row for row in feature_rows if int(row["marketStartUs"]) < split_start_us]
    validation = [dict(row) for row in feature_rows if int(row["marketStartUs"]) >= split_start_us]
    tables = fit_probability_tables(train)
    for row in validation:
        row["probabilityUp"], row["probabilityModelTargetSeconds"] = predict_probability(float(row["distanceBps"]), float(row["remainingSeconds"]), tables)

    overall = metrics(validation)
    by_target = []
    for target in sorted({int(row["targetSeconds"]) for row in validation}, reverse=True):
        selected = [row for row in validation if int(row["targetSeconds"]) == target]
        by_target.append({"targetSeconds": target, **metrics(selected)})
    distance_rows = []
    for target in sorted({int(row["targetSeconds"]) for row in validation}, reverse=True):
        for index in range(len(DISTANCE_BINS) - 1):
            selected = [row for row in validation if int(row["targetSeconds"]) == target and distance_bin(float(row["distanceBps"])) == index]
            if not selected:
                continue
            distance_rows.append({
                "targetSeconds": target, "distanceBin": bin_label(index), "samples": len(selected),
                "avgDistanceBps": mean(float(row["distanceBps"]) for row in selected),
                "predictedProbabilityUp": mean(float(row["probabilityUp"]) for row in selected),
                "actualUpRate": mean(int(row["labelUp"]) for row in selected),
            })
    probability_rows = []
    for low in [i / 10 for i in range(10)]:
        high = low + 0.1
        selected = [row for row in validation if low <= float(row["probabilityUp"]) < high or (high == 1.0 and float(row["probabilityUp"]) == 1.0)]
        if selected:
            probability_rows.append({
                "probabilityBand": f"{low:.1f}–{high:.1f}", "samples": len(selected),
                "avgPredictedProbabilityUp": mean(float(row["probabilityUp"]) for row in selected),
                "actualUpRate": mean(int(row["labelUp"]) for row in selected),
            })

    mature = join_mature_entries(args.regime_input, args.cross_dir, tables, split_start_us)
    strategy_rows = []
    for outcome in ("UP", "DOWN"):
        baseline = [row for row in mature if row.get("outcome") == outcome]
        current = [row for row in baseline if row.get("momentum5") is not None and float(row["momentum5"]) >= 0.01]
        base_summary = summarize_strategy(baseline)
        strategy_rows.append({"outcome": outcome, "variant": "A 时间外基线", "criterion": "—", "threshold": None, "retainedRate": 1.0 if baseline else None, **base_summary})
        current_summary = summarize_strategy(current)
        strategy_rows.append({"outcome": outcome, "variant": "B 当前5秒动量候选", "criterion": "—", "threshold": None, "retainedRate": len(current) / len(baseline) if baseline else None, **current_summary})
        for threshold in PROBABILITY_THRESHOLDS:
            probability_only = [row for row in baseline if float(row["binanceProbabilityForEntry"]) >= threshold]
            combined = [row for row in current if float(row["binanceProbabilityForEntry"]) >= threshold]
            for variant, selected, denominator in (("C Binance概率", probability_only, baseline), ("D 5秒动量+Binance概率", combined, current)):
                strategy_rows.append({
                    "outcome": outcome, "variant": variant, "criterion": "入场方向概率≥", "threshold": threshold,
                    "retainedRate": len(selected) / len(denominator) if denominator else None,
                    **summarize_strategy(selected),
                })
        for threshold in PROBABILITY_EDGE_THRESHOLDS:
            edge_only = [row for row in baseline if float(row["binanceProbabilityEdge"]) >= threshold]
            combined_edge = [row for row in current if float(row["binanceProbabilityEdge"]) >= threshold]
            for variant, selected, denominator in (("E Binance净概率边际", edge_only, baseline), ("F 5秒动量+净概率边际", combined_edge, current)):
                strategy_rows.append({
                    "outcome": outcome, "variant": variant, "criterion": "概率−含费成本≥", "threshold": threshold,
                    "retainedRate": len(selected) / len(denominator) if denominator else None,
                    **summarize_strategy(selected),
                })

    args.out.mkdir(parents=True, exist_ok=True)
    result = {
        "schemaVersion": 1, "experiment": "E15_binance_distance_probability",
        "data": {"labels": len(labels), "featureRows": len(feature_rows), "markets": len(market_starts), "trainRows": len(train), "validationRows": len(validation), "startUs": market_starts[0], "endUs": market_starts[-1], "splitStartUs": split_start_us, "startLocal": local_time(market_starts[0]), "endLocal": local_time(market_starts[-1]), "splitStartLocal": local_time(split_start_us), "matureAlignedValidationEntries": len(mature)},
        "distanceBinsBps": [None if math.isinf(value) else value for value in DISTANCE_BINS],
        "probabilityThresholds": list(PROBABILITY_THRESHOLDS),
        "probabilityEdgeThresholds": list(PROBABILITY_EDGE_THRESHOLDS),
        "overallValidation": overall, "byTargetValidation": by_target,
        "distanceValidation": distance_rows, "calibrationValidation": probability_rows,
        "strategyValidation": strategy_rows,
        "probabilityTables": {str(target): values for target, values in tables.items()},
    }
    (args.out / "e15_binance_distance_probability.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (args.out / "e15_probability_validation_rows.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        fields = list(validation[0]) if validation else []
        writer = csv.DictWriter(handle, fieldnames=fields)
        if fields:
            writer.writeheader(); writer.writerows(validation)
    with (args.out / "e15_mature_strategy_aligned_rows.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        fields = list(mature[0]) if mature else []
        writer = csv.DictWriter(handle, fieldnames=fields)
        if fields:
            writer.writeheader(); writer.writerows(mature)

    lines = [
        "# E15 Binance距离开盘与剩余时间概率验证", "",
        "## 问题与防泄漏口径", "",
        "用Binance Spot当前价格相对本轮五分钟开盘价的距离、剩余时间，估计最终官方UP概率；Binance只作预测特征，赢家仍使用Polymarket官方结算标签。模型按市场开始时间前70%训练、后30%验证，验证市场不参与概率表拟合。", "",
        f"- 数据时间：北京时间 {local_time(market_starts[0])} 至 {local_time(market_starts[-1])}；时间外验证从 {local_time(split_start_us)} 开始。",
        f"- 官方标签：{len(labels)}个；可用特征：{len(feature_rows)}行/{len(market_starts)}个市场。",
        f"- 训练：{len(train)}行；时间外验证：{len(validation)}行；成熟策略时间外且成功对齐：{len(mature)}笔。",
        "- 距离按bps分箱，并在每个剩余时间点用加权保序校准，保证距离越高，预测UP概率不下降。",
        "- 当前模型先只回答距离+时间概率；短动量、波动和成交量作为后续独立增量，避免一次堆入太多参数。", "",
        "## 一、概率模型时间外准确性", "",
        f"- 全验证样本：{overall['samples']}；概率分类准确率 {fmt_rate(overall['accuracy'])}；仅看距离正负准确率 {fmt_rate(overall['signAccuracy'])}；Brier `{fmt_number(overall['brier'])}`；Log Loss `{fmt_number(overall['logLoss'])}`。", "",
        "| 剩余秒数 | 样本 | 概率准确率 | 距离正负准确率 | Brier | Log Loss |", "|---:|---:|---:|---:|---:|---:|",
    ]
    for row in by_target:
        lines.append(f"| {row['targetSeconds']} | {row['samples']} | {fmt_rate(row['accuracy'])} | {fmt_rate(row['signAccuracy'])} | {fmt_number(row['brier'])} | {fmt_number(row['logLoss'])} |")
    lines += ["", "### 概率校准", "", "| 预测UP概率档 | 样本 | 平均预测UP概率 | 实际UP率 |", "|---:|---:|---:|---:|"]
    for row in probability_rows:
        lines.append(f"| {row['probabilityBand']} | {row['samples']} | {fmt_rate(row['avgPredictedProbabilityUp'])} | {fmt_rate(row['actualUpRate'])} |")
    lines += ["", "## 二、距离分档的实际UP率（时间外）", "", "以下分档直接回答‘价格越高于开盘，UP概率是否越大’。", "", "| 剩余秒数 | Binance距离bps | 样本 | 平均距离bps | 预测UP概率 | 实际UP率 |", "|---:|---:|---:|---:|---:|---:|"]
    for row in distance_rows:
        if row["targetSeconds"] in {240, 120, 90, 60, 30}:
            lines.append(f"| {row['targetSeconds']} | {row['distanceBin']} | {row['samples']} | {row['avgDistanceBps']:+.1f} | {fmt_rate(row['predictedProbabilityUp'])} | {fmt_rate(row['actualUpRate'])} |")
    lines += ["", "## 三、加入成熟策略：后30%时间外验证", "", "A是价格/时间成熟基线；B是当前5秒动量候选；C只增加Binance概率门槛；D是在5秒动量候选上再增加概率门槛。E/F进一步要求模型概率高过实际VWAP与预测市场加密品类taker手续费之和，因此比固定概率门槛更直接对应正期望。门槛全部预先列出，不按本轮结果自动修改实盘。", "", "| 方向 | 变体 | 门槛含义 | 门槛 | 笔数 | 胜率 | 平均净收益/份 | 相对本变体基线保留率 |", "|---|---|---|---:|---:|---:|---:|---:|"]
    for row in strategy_rows:
        if row["threshold"] is None or row["trades"] >= 5 or row["variant"].startswith(("E ", "F ")):
            threshold = "—" if row["threshold"] is None else f"{row['threshold']:.2f}"
            lines.append(f"| {row['outcome']} | {row['variant']} | {row['criterion']} | {threshold} | {row['trades']} | {fmt_rate(row['winRate'])} | {fmt_pnl(row['avgPnlPerShare'])} | {fmt_rate(row['retainedRate'])} |")
    lines += ["", "## 四、解释", "", "- 同样的距离在剩余30秒时应比剩余240秒更有确定性；因此不能用一个固定美元阈值跨时间判断。", "- Binance和Chainlink存在基差，模型必须始终用官方结算标签校准；本报告不把Binance结果当成结算结果。", "- 净概率边际 = 入场方向预测概率 −（实际VWAP + taker手续费/份）；它是筛选指标，不等同于保证盈利。", "- 概率门槛若提高胜率却只剩很少交易，不能直接升级；至少需要新增日期每方向100笔左右的有效对齐样本。", "- 下一阶段可在冻结的距离+时间概率上，逐一增加5秒动量、60秒波动和主动成交量，比较真正的样本外增量。", "- 本轮不修改纸上交易或实盘配置。", "", "## 复现", "", "```bash", "PYTHONDONTWRITEBYTECODE=1 python3 scripts/analysis/analyze_binance_distance_probability.py", "```", ""]
    report = args.out / "E15_Binance距离开盘与剩余时间概率验证.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    print(report)
    print(args.out / "e15_binance_distance_probability.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
