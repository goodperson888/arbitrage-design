from __future__ import annotations

import csv
from datetime import datetime, timezone
import gzip
import json
import math
from pathlib import Path
from typing import Any, Iterable


def _read_rows(base_dir: Path, stream: str) -> Iterable[dict[str, Any]]:
    paths = sorted((base_dir / stream).glob("date=*/hour=*/part-*.ndjson*"))
    for path in paths:
        handle = (
            gzip.open(path, "rt", encoding="utf-8")
            if path.suffix == ".gz"
            else path.open(encoding="utf-8")
        )
        with handle:
            for line in handle:
                if line.strip():
                    yield json.loads(line)


def join_feature_labels(base_dir: Path) -> list[dict[str, Any]]:
    labels: dict[str, str] = {}
    for row in _read_rows(base_dir, "labels"):
        winner = str((row.get("payload") or {}).get("winnerOutcome", "")).upper()
        if winner in {"UP", "DOWN"}:
            labels[str(row.get("marketId"))] = winner
    output: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for feature in _read_rows(base_dir, "features"):
        market_id = str(feature.get("marketId"))
        target = int(feature.get("observationTargetSeconds", -1))
        key = (market_id, target)
        if market_id not in labels or key in seen:
            continue
        seen.add(key)
        flat: dict[str, Any] = {
            "marketId": market_id,
            "marketStartUs": feature.get("marketStartUs"),
            "observationTargetSeconds": target,
            "actualRemainingSeconds": feature.get("remainingSeconds"),
            "winner": labels[market_id],
        }
        for source in ("binance", "hyperliquid", "deribit"):
            values = (feature.get("features") or {}).get(source) or {}
            for name, value in values.items():
                if isinstance(value, (str, int, float, bool)) or value is None:
                    flat[f"{source}.{name}"] = value
        for source, signal in (feature.get("signals") or {}).items():
            flat[f"signal.{source}"] = signal
        for source, score in (feature.get("scores") or {}).items():
            flat[f"score.{source}"] = score
        output.append(flat)
    output.sort(key=lambda row: (int(row.get("marketStartUs") or 0), row["observationTargetSeconds"]))
    return output


def _wilson(wins: int, count: int) -> tuple[float, float]:
    if count == 0:
        return 0.0, 0.0
    z = 1.959963984540054
    p = wins / count
    denominator = 1 + z * z / count
    center = (p + z * z / (2 * count)) / denominator
    radius = z * math.sqrt((p * (1 - p) + z * z / (4 * count)) / count) / denominator
    return center - radius, center + radius


def evaluate_directional_signals(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    targets = sorted({int(row["observationTargetSeconds"]) for row in rows}, reverse=True)
    for target in targets:
        group = [row for row in rows if int(row["observationTargetSeconds"]) == target]
        for source in ("binance", "hyperliquid", "deribit", "consensus"):
            signal_key = f"signal.{source}"
            usable = [row for row in group if row.get(signal_key) in {"UP", "DOWN"}]
            wins = sum(row[signal_key] == row["winner"] for row in usable)
            low, high = _wilson(wins, len(usable))
            output.append({
                "observationTargetSeconds": target, "source": source,
                "samples": len(usable), "wins": wins,
                "accuracy": wins / len(usable) if usable else None,
                "wilson95Low": low, "wilson95High": high,
                "upPredictions": sum(row.get(signal_key) == "UP" for row in usable),
                "downPredictions": sum(row.get(signal_key) == "DOWN" for row in usable),
            })
    return output


def _sign_signal(value: Any) -> str | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return "UP" if numeric > 0 else "DOWN" if numeric < 0 else None


def evaluate_deribit_option_signals(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Evaluate option signals independently; they are not folded into consensus yet."""
    output: list[dict[str, Any]] = []
    targets = sorted({int(row["observationTargetSeconds"]) for row in rows}, reverse=True)
    definitions = (
        ("RR25水平", "deribit.rr25"),
        ("RR25近60秒变化", "deribit.rr25Change60s"),
        ("期权主动资金流60秒", "deribit.optionFlowImbalance60s"),
    )
    for target in targets:
        group = [row for row in rows if int(row["observationTargetSeconds"]) == target]
        for label, key in definitions:
            predicted = [(row, _sign_signal(row.get(key))) for row in group]
            usable = [(row, signal) for row, signal in predicted if signal is not None]
            wins = sum(signal == row["winner"] for row, signal in usable)
            low, high = _wilson(wins, len(usable))
            output.append({
                "observationTargetSeconds": target,
                "signal": label,
                "samples": len(usable),
                "wins": wins,
                "accuracy": wins / len(usable) if usable else None,
                "wilson95Low": low,
                "wilson95High": high,
                "upPredictions": sum(signal == "UP" for _, signal in usable),
                "downPredictions": sum(signal == "DOWN" for _, signal in usable),
            })

        confirmed: list[tuple[dict[str, Any], str]] = []
        for row in group:
            flow = _sign_signal(row.get("deribit.optionFlowImbalance60s"))
            if flow == "UP":
                oi_change = row.get("deribit.call25OiChange60s")
                iv_change = row.get("deribit.call25IvChange60s")
            elif flow == "DOWN":
                oi_change = row.get("deribit.put25OiChange60s")
                iv_change = row.get("deribit.put25IvChange60s")
            else:
                continue
            try:
                confirmed_by_positioning = float(oi_change) > 0 and float(iv_change) > 0
            except (TypeError, ValueError):
                confirmed_by_positioning = False
            if confirmed_by_positioning:
                confirmed.append((row, flow))
        wins = sum(signal == row["winner"] for row, signal in confirmed)
        low, high = _wilson(wins, len(confirmed))
        output.append({
            "observationTargetSeconds": target,
            "signal": "资金流+对应25D OI与IV同升",
            "samples": len(confirmed),
            "wins": wins,
            "accuracy": wins / len(confirmed) if confirmed else None,
            "wilson95Low": low,
            "wilson95High": high,
            "upPredictions": sum(signal == "UP" for _, signal in confirmed),
            "downPredictions": sum(signal == "DOWN" for _, signal in confirmed),
        })
    return output


def evaluate_dvol_regimes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    targets = sorted({int(row["observationTargetSeconds"]) for row in rows}, reverse=True)
    for target in targets:
        group = [row for row in rows if int(row["observationTargetSeconds"]) == target]
        for label, predicate in (
            ("DVOL近60秒上升", lambda value: value > 0),
            ("DVOL近60秒不升", lambda value: value <= 0),
        ):
            selected = []
            for row in group:
                try:
                    value = float(row.get("deribit.dvolChange60s"))
                except (TypeError, ValueError):
                    continue
                signal = row.get("signal.consensus")
                if predicate(value) and signal in {"UP", "DOWN"}:
                    selected.append(row)
            wins = sum(row["signal.consensus"] == row["winner"] for row in selected)
            low, high = _wilson(wins, len(selected))
            output.append({
                "observationTargetSeconds": target,
                "regime": label,
                "samples": len(selected),
                "wins": wins,
                "consensusAccuracy": wins / len(selected) if selected else None,
                "wilson95Low": low,
                "wilson95High": high,
            })
    return output


def write_validation_report(base_dir: Path, output_dir: Path) -> tuple[Path, Path, Path]:
    rows = join_feature_labels(base_dir)
    results = evaluate_directional_signals(rows)
    option_results = evaluate_deribit_option_signals(rows)
    dvol_results = evaluate_dvol_regimes(rows)
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "realtime_cross_market_labeled_rows.csv"
    fields = sorted({key for row in rows for key in row})
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        if fields:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
    json_path = output_dir / "realtime_cross_market_results.json"
    json_path.write_text(json.dumps({
        "schemaVersion": 1, "generatedAt": datetime.now(timezone.utc).isoformat(),
        "dataDir": str(base_dir), "labeledRows": len(rows), "results": results,
        "deribitOptionSignals": option_results, "dvolRegimes": dvol_results,
    }, ensure_ascii=False, indent=2) + "\n")
    report_path = output_dir / "实时跨市场预测验证报告.md"
    lines = [
        "# 实时跨市场预测验证报告", "",
        f"- 已结算且成功对齐的特征行：{len(rows)}",
        f"- 独立市场数：{len({row['marketId'] for row in rows})}", "",
        "| 剩余秒数 | 来源 | 样本 | 命中 | 胜率 | 95% Wilson区间 | UP预测 | DOWN预测 |",
        "|---:|---|---:|---:|---:|---:|---:|---:|",
    ]
    labels = {"binance": "Binance", "hyperliquid": "Hyperliquid",
              "deribit": "Deribit", "consensus": "三方组合"}
    for result in results:
        accuracy = "—" if result["accuracy"] is None else f"{result['accuracy']:.2%}"
        lines.append(
            f"| {result['observationTargetSeconds']} | {labels[result['source']]} | "
            f"{result['samples']} | {result['wins']} | {accuracy} | "
            f"{result['wilson95Low']:.2%}–{result['wilson95High']:.2%} | "
            f"{result['upPredictions']} | {result['downPredictions']} |"
        )
    lines.extend([
        "", "## Deribit 期权方向指标（独立观察，暂不混入三方组合）", "",
        "| 剩余秒数 | 指标 | 样本 | 命中 | 胜率 | 95% Wilson区间 | UP预测 | DOWN预测 |",
        "|---:|---|---:|---:|---:|---:|---:|---:|",
    ])
    for result in option_results:
        accuracy = "—" if result["accuracy"] is None else f"{result['accuracy']:.2%}"
        lines.append(
            f"| {result['observationTargetSeconds']} | {result['signal']} | "
            f"{result['samples']} | {result['wins']} | {accuracy} | "
            f"{result['wilson95Low']:.2%}–{result['wilson95High']:.2%} | "
            f"{result['upPredictions']} | {result['downPredictions']} |"
        )
    lines.extend([
        "", "## DVOL 波动环境", "",
        "DVOL不直接预测涨跌；这里比较它上升/不升时，原三方组合方向的命中率是否变化。", "",
        "| 剩余秒数 | DVOL状态 | 样本 | 命中 | 组合胜率 | 95% Wilson区间 |",
        "|---:|---|---:|---:|---:|---:|",
    ])
    for result in dvol_results:
        accuracy = (
            "—" if result["consensusAccuracy"] is None
            else f"{result['consensusAccuracy']:.2%}"
        )
        lines.append(
            f"| {result['observationTargetSeconds']} | {result['regime']} | "
            f"{result['samples']} | {result['wins']} | {accuracy} | "
            f"{result['wilson95Low']:.2%}–{result['wilson95High']:.2%} |"
        )
    lines.extend([
        "", "说明：只有官方已结算市场进入统计；每个市场、每个观察时点只保留一行。",
        "样本少时不要只看点估计胜率，应同时看 95% Wilson 区间和 UP/DOWN 是否失衡。", "",
    ])
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return csv_path, json_path, report_path
