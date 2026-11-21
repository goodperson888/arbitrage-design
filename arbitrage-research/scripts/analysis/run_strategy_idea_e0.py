#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import defaultdict
import csv
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import sys
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from arb_research.experiments.strategy_idea_validation import (
    LogisticProbabilityModel,
    chronological_market_split,
    e0_feature_values,
    latest_polymarket_state,
    probability_metrics,
    read_partition_rows,
    realized_volatility_bps,
    time_weighted_mean,
)


TARGET_SECONDS = (240, 180, 120, 100, 90, 60, 30)
MODEL_FEATURES = {
    "terminal": ("terminalGapBps", "realizedVolBps", "remainingSeconds"),
    "cumulative": (
        "requiredMarginBps",
        "elapsedTwapGapBps",
        "realizedVolBps",
        "remainingSeconds",
    ),
    "combined": (
        "terminalGapBps",
        "requiredMarginBps",
        "elapsedTwapGapBps",
        "realizedVolBps",
        "remainingSeconds",
    ),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="执行E0 Chainlink锚定概率模型验证")
    parser.add_argument(
        "--cross-dir", type=Path,
        default=ROOT / "data/cross_market_validation",
    )
    parser.add_argument(
        "--cache", type=Path,
        default=ROOT / "data/derived/live_strategy_cache.sqlite3",
    )
    parser.add_argument(
        "--out", type=Path,
        default=ROOT / "outputs/research_tests/e0_anchor_probability_20260826",
    )
    parser.add_argument("--train-fraction", type=float, default=0.7)
    parser.add_argument("--max-path-gap-seconds", type=float, default=15.0)
    parser.add_argument("--max-book-age-ms", type=float, default=2_000.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    generated_at = datetime.now(timezone.utc).isoformat()
    label_paths = sorted((args.cross_dir / "labels").glob("date=*/hour=*/part-*.ndjson*"))
    feature_paths = sorted((args.cross_dir / "features").glob("date=*/hour=*/part-*.ndjson*"))
    lead_lag_paths = sorted((args.cross_dir / "lead_lag").glob("date=*/hour=*/part-*.ndjson*"))

    labels: dict[str, dict[str, Any]] = {}
    for row in read_partition_rows(label_paths):
        winner = str((row.get("payload") or {}).get("winnerOutcome", "")).upper()
        if winner in {"UP", "DOWN"}:
            labels[str(row["marketId"])] = {
                "winner": winner,
                "sourceTimeUs": int(row.get("sourceTimeUs") or 0),
            }
    if not labels:
        raise RuntimeError("没有可用的官方结算标签")

    chainlink_paths: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for row in read_partition_rows(lead_lag_paths):
        if str(row.get("source", "")).upper() != "CHAINLINK":
            continue
        market_id = str(row.get("marketId", ""))
        price = (row.get("payload") or {}).get("price")
        if market_id in labels and price is not None:
            chainlink_paths[market_id].append(
                (int(row.get("sourceTimeUs") or 0), float(price))
            )
    for market_id in chainlink_paths:
        chainlink_paths[market_id] = sorted(set(chainlink_paths[market_id]))

    connection = sqlite3.connect(args.cache)
    connection.row_factory = sqlite3.Row
    rows: list[dict[str, Any]] = []
    exclusions: defaultdict[str, int] = defaultdict(int)
    try:
        for feature in read_partition_rows(feature_paths):
            market_id = str(feature.get("marketId", ""))
            if market_id not in labels:
                continue
            target = int(feature.get("observationTargetSeconds", -1))
            if target not in TARGET_SECONDS:
                continue
            chainlink = (feature.get("features") or {}).get("chainlink") or {}
            try:
                market_start_us = int(feature["marketStartUs"])
                observation_us = int(feature.get("sourceTimeUs") or 0)
                decision_received_us = int(feature.get("receivedTimeUs") or 0)
                anchor = float(chainlink["startPrice"])
                current = float(chainlink["price"])
            except (KeyError, TypeError, ValueError):
                exclusions["missingChainlinkFeature"] += 1
                continue
            elapsed_seconds = (observation_us - market_start_us) / 1_000_000.0
            if not 0 < elapsed_seconds < 300:
                exclusions["invalidObservationTime"] += 1
                continue
            path = [
                item for item in chainlink_paths.get(market_id, [])
                if market_start_us <= item[0] < observation_us
            ]
            if len(path) < 5:
                exclusions["insufficientChainlinkPath"] += 1
                continue
            boundaries = [market_start_us, *(timestamp for timestamp, _ in path), observation_us]
            max_gap_seconds = max(
                later - earlier for earlier, later in zip(boundaries, boundaries[1:])
            ) / 1_000_000.0
            if max_gap_seconds > args.max_path_gap_seconds:
                exclusions["chainlinkPathGap"] += 1
                continue
            elapsed_twap = time_weighted_mean(
                start_us=market_start_us,
                observation_us=observation_us,
                anchor_price=anchor,
                samples=path,
            )
            volatility = realized_volatility_bps(
                samples=path, observation_us=observation_us
            )
            values = e0_feature_values(
                anchor_price=anchor,
                current_price=current,
                elapsed_twap=elapsed_twap,
                elapsed_seconds=elapsed_seconds,
                realized_vol_bps=volatility,
            )
            state = latest_polymarket_state(
                connection,
                market_id=market_id,
                decision_received_us=decision_received_us,
                quantity=10.0,
                max_age_us=int(args.max_book_age_ms * 1_000),
            )
            if state is None:
                exclusions["noFreshPolymarketBook"] += 1
                state = {
                    "upAsk": None, "downAsk": None, "marketProbabilityUp": None,
                    "bookAgeMs": None, "upVwap": None, "downVwap": None,
                    "upFeePerShare": None, "downFeePerShare": None,
                    "upTotalCost": None, "downTotalCost": None,
                    "upWorstPrice": None, "downWorstPrice": None,
                }
            rows.append({
                "marketId": market_id,
                "marketStartUs": market_start_us,
                "observationSourceUs": observation_us,
                "observationReceivedUs": decision_received_us,
                "observationTargetSeconds": target,
                "remainingSeconds": 300.0 - elapsed_seconds,
                "winner": labels[market_id]["winner"],
                "labelUp": 1 if labels[market_id]["winner"] == "UP" else 0,
                "anchorPrice": anchor,
                "currentChainlinkPrice": current,
                "elapsedTwap": elapsed_twap,
                "chainlinkSourceAgeMs": chainlink.get("sourceAgeMs"),
                "chainlinkPathSamples": len(path),
                "chainlinkMaxGapSeconds": max_gap_seconds,
                **values,
                **state,
            })
    finally:
        connection.close()

    if len({row["marketId"] for row in rows}) < 10:
        raise RuntimeError("E0可用市场不足10个，不能建模")
    train_rows, validation_rows = chronological_market_split(
        rows, train_fraction=args.train_fraction
    )
    train_labels = np.asarray([row["labelUp"] for row in train_rows], dtype=float)
    validation_labels = np.asarray(
        [row["labelUp"] for row in validation_rows], dtype=float
    )

    model_metrics: dict[str, dict[str, Any]] = {}
    predictions: dict[str, np.ndarray] = {}
    for name, columns in MODEL_FEATURES.items():
        train_matrix = np.asarray(
            [[float(row[column]) for column in columns] for row in train_rows]
        )
        validation_matrix = np.asarray(
            [[float(row[column]) for column in columns] for row in validation_rows]
        )
        model = LogisticProbabilityModel(l2=1.0).fit(train_matrix, train_labels)
        probability = model.predict_proba(validation_matrix)
        predictions[name] = probability
        metrics = probability_metrics(
            probabilities=probability, labels=validation_labels
        )
        metrics["features"] = list(columns)
        metrics["calibration"] = calibration_rows(probability, validation_labels)
        metrics["byRemainingSeconds"] = by_remaining_metrics(
            validation_rows, probability
        )
        model_metrics[name] = metrics
        for row, value in zip(validation_rows, probability):
            row[f"{name}ProbabilityUp"] = float(value)

    market_indices = [
        index for index, row in enumerate(validation_rows)
        if row.get("marketProbabilityUp") is not None
    ]
    market_probability = np.asarray(
        [validation_rows[index]["marketProbabilityUp"] for index in market_indices],
        dtype=float,
    )
    market_labels = validation_labels[market_indices]
    market_metrics = (
        probability_metrics(probabilities=market_probability, labels=market_labels)
        if len(market_indices) else None
    )
    matched_model_metrics = {
        name: probability_metrics(
            probabilities=probability[market_indices], labels=market_labels
        )
        for name, probability in predictions.items()
        if len(market_indices)
    }
    edge_results = evaluate_edge_entries(validation_rows, MODEL_FEATURES)

    output = {
        "schemaVersion": 1,
        "experiment": "E0_Chainlink锚定距离概率模型",
        "generatedAt": generated_at,
        "dataFreeze": {
            "labelFiles": len(label_paths),
            "featureFiles": len(feature_paths),
            "leadLagFiles": len(lead_lag_paths),
            "officialLabels": len(labels),
            "maxLabelSourceTimeUs": max(value["sourceTimeUs"] for value in labels.values()),
            "maxMarketStartUs": max(int(mid.rsplit("-", 1)[-1]) * 1_000_000 for mid in labels),
        },
        "quality": {
            "usableRows": len(rows),
            "usableMarkets": len({row["marketId"] for row in rows}),
            "exclusions": dict(exclusions),
        },
        "split": {
            "trainFraction": args.train_fraction,
            "trainRows": len(train_rows),
            "trainMarkets": len({row["marketId"] for row in train_rows}),
            "validationRows": len(validation_rows),
            "validationMarkets": len({row["marketId"] for row in validation_rows}),
            "trainMaxStartUs": max(row["marketStartUs"] for row in train_rows),
            "validationMinStartUs": min(row["marketStartUs"] for row in validation_rows),
        },
        "models": model_metrics,
        "marketBaseline": market_metrics,
        "matchedModelMetrics": matched_model_metrics,
        "edgeBacktest": edge_results,
    }
    args.out.mkdir(parents=True, exist_ok=True)
    json_path = args.out / "e0_results.json"
    csv_path = args.out / "e0_observations.csv"
    report_path = args.out / "E0_锚定距离概率模型验证.md"
    json_path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    write_csv(csv_path, rows)
    write_report(report_path, output)
    print(json_path)
    print(csv_path)
    print(report_path)
    return 0


def calibration_rows(
    probabilities: np.ndarray, labels: np.ndarray
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for lower in np.arange(0.0, 1.0, 0.1):
        upper = lower + 0.1
        mask = (probabilities >= lower) & (
            probabilities <= upper if upper >= 1.0 else probabilities < upper
        )
        if not np.any(mask):
            continue
        output.append({
            "lower": float(lower),
            "upper": float(upper),
            "samples": int(np.sum(mask)),
            "meanPredicted": float(np.mean(probabilities[mask])),
            "actualUpRate": float(np.mean(labels[mask])),
        })
    return output


def by_remaining_metrics(
    rows: list[dict[str, Any]], probabilities: np.ndarray
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    labels = np.asarray([row["labelUp"] for row in rows], dtype=float)
    for target in TARGET_SECONDS:
        indices = [
            index for index, row in enumerate(rows)
            if int(row["observationTargetSeconds"]) == target
        ]
        if not indices:
            continue
        metrics = probability_metrics(
            probabilities=probabilities[indices], labels=labels[indices]
        )
        output.append({"remainingSeconds": target, **metrics})
    return output


def evaluate_edge_entries(
    rows: list[dict[str, Any]], models: dict[str, tuple[str, ...]]
) -> list[dict[str, Any]]:
    by_market: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_market[row["marketId"]].append(row)
    output: list[dict[str, Any]] = []
    for model_name in models:
        probability_key = f"{model_name}ProbabilityUp"
        for threshold in (0.0, 0.01, 0.02, 0.03, 0.05):
            entries: list[dict[str, Any]] = []
            for market_rows in by_market.values():
                for row in sorted(
                    market_rows, key=lambda item: float(item["remainingSeconds"]),
                    reverse=True,
                ):
                    up_cost = row.get("upTotalCost")
                    down_cost = row.get("downTotalCost")
                    if up_cost is None or down_cost is None:
                        continue
                    probability_up = float(row[probability_key])
                    candidates = [
                        (probability_up - float(up_cost), "UP", float(up_cost)),
                        ((1.0 - probability_up) - float(down_cost), "DOWN", float(down_cost)),
                    ]
                    edge, direction, cost = max(candidates)
                    if edge < threshold:
                        continue
                    won = direction == row["winner"]
                    entries.append({
                        "direction": direction,
                        "won": won,
                        "pnlPerShare": (1.0 if won else 0.0) - cost,
                    })
                    break
            for direction in ("ALL", "UP", "DOWN"):
                selected = (
                    entries if direction == "ALL"
                    else [entry for entry in entries if entry["direction"] == direction]
                )
                output.append({
                    "model": model_name,
                    "minimumNetEdge": threshold,
                    "direction": direction,
                    "entries": len(selected),
                    "wins": sum(entry["won"] for entry in selected),
                    "losses": sum(not entry["won"] for entry in selected),
                    "winRate": (
                        sum(entry["won"] for entry in selected) / len(selected)
                        if selected else None
                    ),
                    "averageNetPnlPerShare": (
                        sum(entry["pnlPerShare"] for entry in selected) / len(selected)
                        if selected else None
                    ),
                    "totalNetPnlPerShare": sum(
                        entry["pnlPerShare"] for entry in selected
                    ),
                })
    return output


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = sorted({key for row in rows for key in row})
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def write_report(path: Path, result: dict[str, Any]) -> None:
    labels = {"terminal": "终点TWAP流值", "cumulative": "累计区间", "combined": "两者组合"}
    split = result["split"]
    quality = result["quality"]
    lines = [
        "# E0：Chainlink锚定距离概率模型验证", "",
        "## 本轮固定口径", "",
        f"- 官方结算标签：{result['dataFreeze']['officialLabels']}个；可用路径市场：{quality['usableMarkets']}个。",
        f"- 可用观察行：{quality['usableRows']}；训练 {split['trainMarkets']} 个市场/{split['trainRows']}行，验证 {split['validationMarkets']} 个市场/{split['validationRows']}行。",
        "- 按市场开始时间前70%训练、后30%验证；同一市场的多个观察时点不会跨集合。",
        "- 盘口只读取观察时本机已经收到且年龄不超过2秒的快照；成交成本按10份逐档VWAP并计taker手续费。",
        "- 同时验证两种解释：终点60秒TWAP流值相对起点，以及对5分钟路径再次累计平均。", "",
        "## 概率预测结果（后30%时间外验证）", "",
        "| 模型 | 样本 | Brier（低好） | Log Loss（低好） | 方向准确率 |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, metrics in result["models"].items():
        lines.append(
            f"| {labels[name]} | {metrics['samples']} | {metrics['brierScore']:.4f} | "
            f"{metrics['logLoss']:.4f} | {metrics['accuracy']:.2%} |"
        )
    market = result.get("marketBaseline")
    if market:
        lines.append(
            f"| Polymarket互补盘口基线 | {market['samples']} | {market['brierScore']:.4f} | "
            f"{market['logLoss']:.4f} | {market['accuracy']:.2%} |"
        )
    lines.extend(["", "### 与盘口同样本比较", "",
                  "| 模型 | 同样本数 | Brier | Log Loss | 准确率 |",
                  "|---|---:|---:|---:|---:|"])
    for name, metrics in result["matchedModelMetrics"].items():
        lines.append(
            f"| {labels[name]} | {metrics['samples']} | {metrics['brierScore']:.4f} | "
            f"{metrics['logLoss']:.4f} | {metrics['accuracy']:.2%} |"
        )
    lines.extend(["", "## 分剩余秒数", ""])
    for name, metrics in result["models"].items():
        lines.extend([
            f"### {labels[name]}", "",
            "| 剩余秒数 | 样本 | Brier | Log Loss | 准确率 |",
            "|---:|---:|---:|---:|---:|",
        ])
        for row in metrics["byRemainingSeconds"]:
            lines.append(
                f"| {row['remainingSeconds']} | {row['samples']} | {row['brierScore']:.4f} | "
                f"{row['logLoss']:.4f} | {row['accuracy']:.2%} |"
            )
        lines.append("")
    lines.extend([
        "## 扣除实际买入成本后的首次信号回测", "",
        "每个市场只取从早到晚第一个达到净边际阈值的时点，避免同一市场重复记单。", "",
        "| 模型 | 最低净边际 | 方向 | 笔数 | 胜/负 | 胜率 | 平均净收益/份 |",
        "|---|---:|---|---:|---:|---:|---:|",
    ])
    for row in result["edgeBacktest"]:
        if row["minimumNetEdge"] not in {0.01, 0.02, 0.03}:
            continue
        rate = "—" if row["winRate"] is None else f"{row['winRate']:.2%}"
        pnl = (
            "—" if row["averageNetPnlPerShare"] is None
            else f"{row['averageNetPnlPerShare']:+.4f}"
        )
        lines.append(
            f"| {labels[row['model']]} | {row['minimumNetEdge']:.0%} | {row['direction']} | "
            f"{row['entries']} | {row['wins']}/{row['losses']} | {rate} | {pnl} |"
        )
    matched = result.get("matchedModelMetrics") or {}
    market_brier = market["brierScore"] if market else math.inf
    best_name = min(matched, key=lambda name: matched[name]["brierScore"]) if matched else None
    lines.extend(["", "## 结论", ""])
    if best_name is None:
        lines.append("本轮缺少可与盘口同步比较的验证样本，E0不能判定通过。")
    elif matched[best_name]["brierScore"] < market_brier:
        improvement = market_brier - matched[best_name]["brierScore"]
        lines.append(
            f"验证集中表现最好的是“{labels[best_name]}”，同样本Brier比盘口低 {improvement:.4f}。"
            "它可以进入E1/E2作为候选概率字段，但仍不能直接升级为实盘硬条件。"
        )
    else:
        lines.append(
            f"表现最好的“{labels[best_name]}”仍未降低Polymarket盘口的Brier误差；"
            "E0暂不作为入场硬条件，只保留为分析字段。"
        )
    lines.extend([
        "", "## 数据限制", "",
        f"- 排除统计：`{json.dumps(quality['exclusions'], ensure_ascii=False)}`。",
        "- RTDS/Chainlink历史压缩分片存在少量尾部损坏；读取器保留损坏前的完整行，并用最大路径间隔过滤缺口市场。",
        "- 官方规则确认使用Chainlink BTC/USD 60秒TWAP数据流；由于文字仍可能对‘整段TWAP’产生解释歧义，本轮保留两种计算并以官方标签的样本外误差判别。",
        "- 本报告是研究回测，不构成稳赚或实盘收益保证。", "",
    ])
    path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
