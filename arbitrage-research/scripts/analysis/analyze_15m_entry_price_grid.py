#!/usr/bin/env python3
"""Backtest 15m Polymarket entry price and remaining-time buckets.

Uses the compact one-second Polymarket snapshots written by cross15.  A row is
actionable only when the 10-share buy VWAP and worst price are present, the
book is <=2s old, and the worst executable price is below 0.99.  This avoids
mistaking a 0.99/1.00 stale or nearly locked quote for a realistic entry.
"""

from __future__ import annotations

import csv
import gzip
import json
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BASE = ROOT / "data/cross_market_validation_15m"
DEFAULT_OUT = ROOT / "outputs/research_tests/e19_15m_entry_price_grid_20260911"
TARGETS = (720, 600, 480, 360, 300, 240, 180, 120, 90, 60, 30)
TOLERANCE_SECONDS = 2.5
FEE_RATE = 0.07
PRICE_BANDS = (
    (0.50, 0.60), (0.60, 0.70), (0.70, 0.75), (0.75, 0.80),
    (0.80, 0.85), (0.85, 0.90), (0.90, 0.95), (0.95, 0.98), (0.98, 0.99),
)


def rows_from(paths: Iterable[Path]) -> Iterable[dict[str, Any]]:
    for path in sorted(paths):
        try:
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except (json.JSONDecodeError, OSError):
                        continue
                    if isinstance(row, dict):
                        yield row
        except (OSError, EOFError, gzip.BadGzipFile):
            continue


def load_labels(base: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in (base / "labels").glob("date=*/hour=*/part-*.ndjson*"):
        opener = gzip.open if path.suffix == ".gz" else open
        try:
            with opener(path, "rt", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                        payload = row.get("payload") or {}
                        winner = str(payload.get("winnerOutcome") or "").upper()
                        market_id = str(row.get("marketId") or "")
                        if market_id and winner in {"UP", "DOWN"}:
                            result[market_id] = winner
                    except (json.JSONDecodeError, OSError):
                        continue
        except (OSError, EOFError, gzip.BadGzipFile):
            continue
    return result


def num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def band_for(value: float) -> str | None:
    for low, high in PRICE_BANDS:
        if low <= value < high:
            return f"{low:.2f}–{high:.2f}"
    return None


def fee(price: float) -> float:
    return FEE_RATE * price * (1 - price)


def pnl(price: float, outcome: str, winner: str) -> float:
    return (1.0 - price if outcome == winner else -price) - fee(price)


def load_snapshots(base: Path, labels: dict[str, str]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    # Keep one closest record per market/outcome/target.  This prevents a
    # high-frequency stream from weighting a market more than another market.
    best: dict[tuple[str, str, int], dict[str, Any]] = {}
    quality = defaultdict(int)
    paths = (base / "polymarket_snapshots").glob("date=*/hour=*/part-*.ndjson.gz")
    for row in rows_from(paths):
        market_id = str(row.get("marketId") or "")
        if market_id not in labels or row.get("completeOutcomeBooks") is not True:
            continue
        remaining = num(row.get("remainingSeconds"))
        if remaining is None:
            continue
        for target in TARGETS:
            distance = abs(remaining - target)
            if distance > TOLERANCE_SECONDS:
                continue
            for outcome, data in (row.get("outcomes") or {}).items():
                outcome = str(outcome).upper()
                if outcome not in {"UP", "DOWN"}:
                    continue
                key = (market_id, outcome, target)
                candidate = {"marketId": market_id, "outcome": outcome,
                             "winner": labels[market_id], "target": target,
                             "remaining": remaining, "sourceUs": row.get("sourceTimeUs"),
                             "bookAgeMs": num(data.get("bookAgeMs")),
                             "vwap": num((data.get("buy") or {}).get("vwap")),
                             "worst": num((data.get("buy") or {}).get("worstPrice")),
                             "bestAsk": num(data.get("bestAsk")),
                             "nearLocked": False}
                old = best.get(key)
                if old is None or distance < old["distance"]:
                    candidate["distance"] = distance
                    best[key] = candidate
    rows: list[dict[str, Any]] = []
    for row in best.values():
        vwap, worst, age = row["vwap"], row["worst"], row["bookAgeMs"]
        if vwap is None or worst is None:
            quality["missing_10_share_vwap"] += 1
            continue
        if age is None or age > 2_000:
            quality["stale_book_over_2s"] += 1
            continue
        if worst >= 0.99:
            quality["near_locked_0.99_or_higher"] += 1
            row["nearLocked"] = True
            continue
        band = band_for(vwap)
        if band is None:
            quality["outside_0.50_0.99_grid"] += 1
            continue
        row["band"] = band
        row["won"] = row["outcome"] == row["winner"]
        row["pnlPerShare"] = pnl(vwap, row["outcome"], row["winner"])
        rows.append(row)
    return rows, dict(quality)


def summarize(rows: list[dict[str, Any]], group: tuple[str, str]) -> dict[str, Any]:
    selected = [row for row in rows if (str(row["target"]), row["band"]) == group]
    wins = sum(bool(row["won"]) for row in selected)
    return {
        "remainingSeconds": int(group[0]), "priceBand": group[1],
        "samples": len(selected), "wins": wins, "losses": len(selected) - wins,
        "winRate": wins / len(selected) if selected else None,
        "avgEntryVwap": mean(row["vwap"] for row in selected) if selected else None,
        "avgWorstPrice": mean(row["worst"] for row in selected) if selected else None,
        "avgPnlPerShare": mean(row["pnlPerShare"] for row in selected) if selected else None,
        "markets": len({row["marketId"] for row in selected}),
    }


def fmt(value: float | None, percent: bool = False) -> str:
    if value is None:
        return "—"
    return f"{value:.1%}" if percent else f"{value:.4f}"


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path, default=DEFAULT_BASE)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    labels = load_labels(args.base)
    rows, quality = load_snapshots(args.base, labels)
    result = [summarize(rows, (str(target), f"{low:.2f}–{high:.2f}"))
              for target in TARGETS for low, high in PRICE_BANDS]
    args.out.mkdir(parents=True, exist_ok=True)
    detail = args.out / "15m_entry_price_grid.csv"
    with detail.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(result[0]))
        writer.writeheader(); writer.writerows(result)
    report = args.out / "15分钟入场价与剩余时间可成交回测.md"
    lines = [
        "# 15分钟市场：入场价 × 剩余时间可成交回测", "",
        "## 结论口径", "",
        "本轮使用 Polymarket 官方结算标签和 15 分钟逐秒压缩盘口快照。每个市场、方向、目标剩余秒数只取最接近目标的一条记录，避免同一市场高频重复计权。只有 10 份买入 VWAP/最坏成交价存在、盘口年龄不超过2秒、最坏成交价低于0.99的记录才计入可成交样本。手续费按 `0.07 × p × (1-p)`/份计算，持有到官方结算。", "",
        f"- 有结算标签的市场：{len(labels)}；进入逐价网格的可成交观察：{len(rows)}。",
        f"- 价格0.99/1.00或盘口不可覆盖的记录不会被伪装成可交易机会；剔除统计：{json.dumps(quality, ensure_ascii=False)}。", "",
        "## 结果（UP/DOWN合并；方向另有字段可重新拆分）", "",
        "| 剩余秒 | 入场VWAP区间 | 市场数 | 样本 | 胜率 | 平均VWAP | 平均最坏价 | 平均净收益/份 |", "|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in result:
        if item["samples"]:
            lines.append(f"| {item['remainingSeconds']} | {item['priceBand']} | {item['markets']} | {item['samples']} | {fmt(item['winRate'], True)} | {fmt(item['avgEntryVwap'])} | {fmt(item['avgWorstPrice'])} | {fmt(item['avgPnlPerShare'])} |")
    lines += [
        "", "## 如何解释99/100价格", "",
        "价格到0.99或1.00不等于还可以按这个价格完成10份买入。此时经常是对手盘极薄、报价陈旧或市场接近封盘；即使有少量卖盘，手续费后的剩余收益也很小。本回测把最坏成交价≥0.99单独剔除，并在上面的剔除统计中报告。以后若要研究临界封盘，只能单独做‘是否实际成交、成交多少、成交延迟’实验，不能和普通入场价混在一起。", "",
        "## 当前数据是否够用", "",
        "354个15分钟市场、约20.4万条逐秒快照足够做第一轮价格/时间筛选，也足够发现明显不可交易区间；但0.85以上、特定剩余秒数和单方向的细分格子仍可能样本少。正式固定策略建议继续积累到至少800–1000个已结算市场，并按日期做后30%样本外复核。", "",
        "## 复现", "", "```bash",
        "cd /Users/laochen/Documents/lianghua/arbitrage-design/arbitrage-research",
        "PYTHONDONTWRITEBYTECODE=1 python3 scripts/analysis/analyze_15m_entry_price_grid.py", "```", "",
        f"明细：`{detail}`",
    ]
    report.write_text("\n".join(lines), encoding="utf-8")
    print(report)
    print(detail)
    print(json.dumps({"labels": len(labels), "actionableRows": len(rows), "quality": quality}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
