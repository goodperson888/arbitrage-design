#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T050：DOWN成熟策略中，5秒动量与Binance五分钟方向的严格对照。"""

from __future__ import annotations

import gzip
import json
from bisect import bisect_right
from pathlib import Path
from statistics import mean
from typing import Any, Iterable
import zlib

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = ROOT / "outputs/research_tests/e13_strategy_matrix_20260828/market_regime_results.json"
DEFAULT_CROSS = ROOT / "data/cross_market_validation/venue_snapshots"
DEFAULT_OUT = ROOT / "outputs/research_tests/t050_down_momentum_binance_20260828"


def read_rows(paths: Iterable[Path]) -> Iterable[dict[str, Any]]:
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


def stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    wins = sum(bool(row.get("won")) for row in rows)
    return {
        "trades": len(rows),
        "wins": wins,
        "losses": len(rows) - wins,
        "winRate": wins / len(rows) if rows else None,
        "avgPnlPerShare": mean(float(row["pnlPerShare"]) for row in rows) if rows else None,
    }


def fmt_rate(value: float | None) -> str:
    return "—" if value is None else f"{value:.1%}"


def fmt_pnl(value: float | None) -> str:
    return "—" if value is None else f"{value:+.4f}"


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="UP/DOWN动量与Binance五分钟方向严格对照")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--cross-dir", type=Path, default=DEFAULT_CROSS)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    payload = json.loads(args.input.read_text(encoding="utf-8"))
    baseline = [
        dict(row) for row in payload.get("rows", [])
        if str(row.get("outcome", "")).upper() in {"UP", "DOWN"}
        and 0.80 <= float(row.get("entryVwap", -1)) <= 0.85
        and 30 <= float(row.get("remainingSeconds", -1)) <= 90
    ]
    baseline.sort(key=lambda row: int(row["executionUs"]))
    market_ids = {str(row["marketId"]) for row in baseline}
    snapshots: dict[str, list[tuple[int, float | None]]] = {market_id: [] for market_id in market_ids}
    paths = args.cross_dir.glob("date=*/hour=*/part-*.ndjson*")
    for item in read_rows(paths):
        market_id = str(item.get("marketId") or "")
        if market_id not in snapshots:
            continue
        values = (item.get("features") or {}).get("binance") or {}
        try:
            received_us = int(item.get("receivedTimeUs") or 0)
            distance = values.get("returnFromStart")
            distance = None if distance is None else float(distance)
        except (TypeError, ValueError):
            continue
        snapshots[market_id].append((received_us, distance))
    for values in snapshots.values():
        values.sort()

    aligned: list[dict[str, Any]] = []
    for row in baseline:
        values = snapshots.get(str(row["marketId"]), [])
        times = [value[0] for value in values]
        position = bisect_right(times, int(row["executionUs"])) - 1
        item = dict(row)
        item["binancePure5mDistance"] = None
        item["binancePure5mDirection"] = None
        item["binanceAlignmentAgeMs"] = None
        if position >= 0:
            received_us, distance = values[position]
            age_us = int(row["executionUs"]) - received_us
            if 0 <= age_us <= 2_000_000 and distance is not None:
                item["binancePure5mDistance"] = distance
                item["binancePure5mDirection"] = "DOWN" if distance < 0 else "UP" if distance > 0 else "NEUTRAL"
                item["binanceAlignmentAgeMs"] = age_us / 1_000
        aligned.append(item)

    def m5_ge_zero(row: dict[str, Any]) -> bool:
        return row.get("momentum5") is not None and float(row["momentum5"]) >= 0

    def m5_ge_001(row: dict[str, Any]) -> bool:
        return row.get("momentum5") is not None and float(row["momentum5"]) >= 0.01

    def pure_same(row: dict[str, Any]) -> bool:
        return row.get("binancePure5mDirection") == row.get("outcome")

    variants_by_outcome: dict[str, list[dict[str, Any]]] = {}
    split = max(1, int(len(aligned) * 0.70))
    train = aligned[:split]
    validation = aligned[split:]
    for outcome in ("UP", "DOWN"):
        outcome_rows = [row for row in aligned if row.get("outcome") == outcome]
        outcome_split = max(1, int(len(outcome_rows) * 0.70))
        outcome_train = outcome_rows[:outcome_split]
        outcome_validation = outcome_rows[outcome_split:]
        variants = [
            ("A 5秒动量≥0.01", m5_ge_001),
            ("B 5秒动量不回落(≥0)+严格Binance 5分钟同向", lambda row: m5_ge_zero(row) and pure_same(row)),
            ("C 5秒动量不回落(≥0)+历史Binance综合信号同向", lambda row: m5_ge_zero(row) and row.get("spotSignal") == outcome),
            ("D 5秒动量≥0.01+严格Binance 5分钟同向", lambda row: m5_ge_001(row) and pure_same(row)),
            ("E 仅5秒动量不回落(≥0)", m5_ge_zero),
        ]
        results: list[dict[str, Any]] = []
        for label, predicate in variants:
            selected = [row for row in outcome_rows if predicate(row)]
            selected_train = [row for row in outcome_train if predicate(row)]
            selected_validation = [row for row in outcome_validation if predicate(row)]
            results.append({
                "outcome": outcome, "label": label,
                "all": stats(selected), "train": stats(selected_train), "validation": stats(selected_validation),
                "retainedRate": len(selected) / len(outcome_rows) if outcome_rows else None,
                "alignedPureBinanceRate": sum(row.get("binancePure5mDirection") in {"UP", "DOWN"} for row in selected) / len(selected) if selected else None,
            })
        variants_by_outcome[outcome] = results

    aligned_count = sum(row.get("binancePure5mDirection") in {"UP", "DOWN"} for row in aligned)
    output = {
        "schemaVersion": 1,
        "experiment": "T050_momentum_binance_bidirectional",
        "source": str(args.input),
        "definition": {
            "baseline": "UP/DOWN + buy_trade单笔主动成交≥10份 + 实际VWAP 0.80–0.85 + 剩余90–30秒 + 0.85价格上限内10份完整可执行成交 + Polymarket盘口年龄≤2秒",
            "strictBinanceDirection": "入场前最新可用Binance Spot快照的returnFromStart与入场方向同向；快照在入场前且年龄≤2秒",
            "momentumNoFall": "对应方向token卖一价格相对5秒前变化≥0",
            "momentum001": "对应方向token卖一价格相对5秒前变化≥0.01",
        },
        "data": {"baseline": len(aligned), "baselineByOutcome": {o: sum(row.get("outcome") == o for row in aligned) for o in ("UP", "DOWN")}, "pureBinanceAligned": aligned_count, "train": len(train), "validation": len(validation)},
        "variantsByOutcome": variants_by_outcome,
        "rows": aligned,
    }
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "t050_down_momentum_binance.json").write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (args.out / "t050_down_momentum_binance_rows.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        import csv
        fields = list(aligned[0]) if aligned else []
        writer = csv.DictWriter(handle, fieldnames=fields)
        if fields:
            writer.writeheader(); writer.writerows(aligned)

    lines = [
        "# T050 UP/DOWN动量与Binance 5分钟方向严格对照", "",
        "## 结论先行", "",
        "本实验把‘Binance Spot最近5分钟同向’与‘Binance本轮5分钟相对开盘价严格同向’分开，并对UP、DOWN分别计算。A是当前5秒动量≥0.01基准；B是用户提出的5秒不回落(≥0)+严格Binance同向；C保留历史E10/E14中使用的Binance综合信号同向，便于核对之前的结果。", "",
        f"- 成熟基线：{len(aligned)}笔（UP {sum(row.get('outcome') == 'UP' for row in aligned)}，DOWN {sum(row.get('outcome') == 'DOWN' for row in aligned)}）；严格Binance快照在入场前≤2秒成功对齐：{aligned_count}笔（{fmt_rate(aligned_count / len(aligned) if aligned else None)}）。", 
        "- 所有样本沿用既有成交语义：真实主动买入≥10份、实际10份VWAP 0.80–0.85、最坏成交价≤0.85、Polymarket盘口年龄≤2秒、手续费已计入源记录净收益。", "- 70%按执行时间训练、后30%时间外复核；不使用入场后的路径信息。", "",
        "## 一、全样本对照", "",
        "| 变体 | 笔数 | 胜/负 | 胜率 | 平均净收益/份 | 保留率 | 严格Binance可对齐率 |", "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for outcome in ("UP", "DOWN"):
        lines += [f"### {outcome}全样本", "", "| 变体 | 笔数 | 胜/负 | 胜率 | 平均净收益/份 | 保留率 | 严格Binance可对齐率 |", "|---|---:|---:|---:|---:|---:|---:|"]
        for item in variants_by_outcome[outcome]:
            s = item["all"]
            lines.append(f"| {item['label']} | {s['trades']} | {s['wins']}/{s['losses']} | {fmt_rate(s['winRate'])} | {fmt_pnl(s['avgPnlPerShare'])} | {fmt_rate(item['retainedRate'])} | {fmt_rate(item['alignedPureBinanceRate'])} |")
        lines += ["", f"### {outcome}时间外验证段", "", "| 变体 | 笔数 | 胜/负 | 胜率 | 平均净收益/份 |", "|---|---:|---:|---:|---:|"]
        for item in variants_by_outcome[outcome]:
            s = item["validation"]
            lines.append(f"| {item['label']} | {s['trades']} | {s['wins']}/{s['losses']} | {fmt_rate(s['winRate'])} | {fmt_pnl(s['avgPnlPerShare'])} |")
    lines += ["", "## 三、如何回答‘之前测过吗’", "", "- E10/E14确实测过‘5秒动量≥0.01 + Binance Spot最近5分钟综合方向同向’；DOWN全样本88笔、90.9%、+0.0780/份，时间外19笔、89.5%、+0.0639/份。", "- 但E10/E14里的`spotSignal`是采集器综合方向分数，不是纯粹的Binance本轮5分钟相对开盘方向；本报告B/D才是严格的`returnFromStart`口径。", "- B中‘不回落≥0’与A的‘上涨≥0.01’不是同一个条件；B放宽动量后再叠加严格Binance方向，UP、DOWN都必须分别看时间外结果。", "", "## 四、边界与建议", "", "- ‘不回落’按≥0实现；若改成>0或允许最多回落0.01，需要另开参数，不能和本次结果混用。", "- Binance严格方向要求入场前快照不超过2秒；没有成功对齐的记录不强行判为同向。", "- 结果只说明历史样本中的增量，不保证未来；时间外样本不足时继续纸上并行记录，暂不替换当前实盘条件。", "- 本轮只生成研究报告，不修改纸上交易或实盘。", "", "## 复现", "", "```bash", "PYTHONDONTWRITEBYTECODE=1 python3 scripts/analysis/compare_down_momentum_binance.py", "```", "",
    ]
    (args.out / "T050_DOWN动量与Binance5分钟方向对照.md").write_text("\n".join(lines), encoding="utf-8")
    print(args.out / "T050_DOWN动量与Binance5分钟方向对照.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
