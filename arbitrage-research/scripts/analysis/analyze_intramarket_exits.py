#!/usr/bin/env python3
"""回测中途价格区间入场，以及止盈/止损/收盘前退出。

使用增量 SQLite 中的逐盘口快照；买入按同方向 asks 吃满10份，卖出按
相反方向 asks 的互补价格还原本方向 bids 后吃满10份。所有判断只使用当时
及以前数据，不修改实时监控。
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
from statistics import median
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
QTY = Decimal("10")
FEE_RATE = Decimal("0.07")
ZERO = Decimal("0")


@dataclass(frozen=True)
class Policy:
    policy_id: str
    take_profit: Decimal | None
    stop_loss: Decimal | None


POLICIES = (
    Policy("close_5s", None, None),
    Policy("tp03_sl03", Decimal("0.03"), Decimal("0.03")),
    Policy("tp03_sl05", Decimal("0.03"), Decimal("0.05")),
    Policy("tp05_sl03", Decimal("0.05"), Decimal("0.03")),
    Policy("tp05_sl05", Decimal("0.05"), Decimal("0.05")),
    Policy("tp05_sl08", Decimal("0.05"), Decimal("0.08")),
    Policy("tp08_sl05", Decimal("0.08"), Decimal("0.05")),
    Policy("tp08_sl08", Decimal("0.08"), Decimal("0.08")),
    Policy("tp10_sl08", Decimal("0.10"), Decimal("0.08")),
    Policy("tp05_only", Decimal("0.05"), None),
    Policy("tp10_only", Decimal("0.10"), None),
)
POLICY_LABELS = {
    "close_5s": "剩余约5秒退出",
    "tp03_sl03": "+0.03止盈/-0.03止损",
    "tp03_sl05": "+0.03止盈/-0.05止损",
    "tp05_sl03": "+0.05止盈/-0.03止损",
    "tp05_sl05": "+0.05止盈/-0.05止损",
    "tp05_sl08": "+0.05止盈/-0.08止损",
    "tp08_sl05": "+0.08止盈/-0.05止损",
    "tp08_sl08": "+0.08止盈/-0.08止损",
    "tp10_sl08": "+0.10止盈/-0.08止损",
    "tp05_only": "+0.05止盈；否则盘前退出",
    "tp10_only": "+0.10止盈；否则盘前退出",
}
WINDOWS = {"180_120": (120, 180), "120_90": (90, 120), "90_60": (60, 90), "60_30": (30, 60)}
BANDS = {
    "55_60": (Decimal("0.55"), Decimal("0.60")),
    "60_65": (Decimal("0.60"), Decimal("0.65")),
    "65_70": (Decimal("0.65"), Decimal("0.70")),
    "70_75": (Decimal("0.70"), Decimal("0.75")),
    "75_80": (Decimal("0.75"), Decimal("0.80")),
    "80_85": (Decimal("0.80"), Decimal("0.85")),
}


def fee(price: Decimal) -> Decimal:
    return FEE_RATE * price * (Decimal("1") - price)


def consume(levels: list[list[str]], quantity: Decimal, *, descending: bool = False, cap: Decimal | None = None) -> tuple[Decimal, Decimal] | None:
    remaining, notional, fees = quantity, ZERO, ZERO
    parsed = sorted(((Decimal(str(p)), Decimal(str(s))) for p, s in levels), key=lambda x: x[0], reverse=descending)
    for price, size in parsed:
        if cap is not None and not descending and price > cap:
            break
        take = min(remaining, size)
        if take <= 0:
            continue
        notional += take * price
        fees += take * fee(price)
        remaining -= take
        if remaining <= 0:
            return notional / quantity, fees / quantity
    return None


def complement_bids(opposite_asks: list[list[str]]) -> list[list[str]]:
    return [[str(Decimal("1") - Decimal(str(price))), str(size)] for price, size in opposite_asks]


def load_market_snapshots(connection: sqlite3.Connection, market_id: str) -> list[dict[str, Any]]:
    grouped: dict[int, dict[str, Any]] = {}
    for row in connection.execute(
        "SELECT source_us,outcome,asks_json FROM snapshots WHERE market_id=? ORDER BY source_us,outcome", (market_id,)
    ):
        item = grouped.setdefault(int(row[0]), {"sourceUs": int(row[0]), "asks": {}})
        item["asks"][str(row[1])] = json.loads(row[2])
    return [grouped[key] for key in sorted(grouped)]


def find_entry(snapshots: list[dict[str, Any]], end_us: int, outcome: str, minimum: int, maximum: int,
               low: Decimal, high: Decimal) -> dict[str, Any] | None:
    for snapshot in snapshots:
        remaining = Decimal(end_us - snapshot["sourceUs"]) / Decimal("1000000")
        if not Decimal(minimum) <= remaining <= Decimal(maximum):
            continue
        asks = snapshot["asks"].get(outcome) or []
        filled = consume(asks, QTY, cap=high)
        if filled is None:
            continue
        vwap, entry_fee = filled
        if low <= vwap < high or (high == Decimal("0.85") and vwap == high):
            return {"sourceUs": snapshot["sourceUs"], "remaining": remaining, "vwap": vwap, "fee": entry_fee}
    return None


def sell_path(snapshots: list[dict[str, Any]], end_us: int, outcome: str, after_us: int) -> list[dict[str, Any]]:
    opposite = "DOWN" if outcome == "UP" else "UP"
    result = []
    for snapshot in snapshots:
        if snapshot["sourceUs"] <= after_us or snapshot["sourceUs"] >= end_us:
            continue
        bids = complement_bids(snapshot["asks"].get(opposite) or [])
        filled = consume(bids, QTY, descending=True)
        if filled is None:
            continue
        vwap, exit_fee = filled
        result.append({"sourceUs": snapshot["sourceUs"], "remaining": Decimal(end_us - snapshot["sourceUs"]) / Decimal("1000000"),
                       "vwap": vwap, "fee": exit_fee})
    return result


def execute_policy(path: list[dict[str, Any]], entry: dict[str, Any], policy: Policy) -> dict[str, Any] | None:
    selected = None
    trigger = None
    for point in path:
        delta = point["vwap"] - entry["vwap"]
        if policy.take_profit is not None and delta >= policy.take_profit:
            selected, trigger = point, "TP"
            break
        if policy.stop_loss is not None and delta <= -policy.stop_loss:
            selected, trigger = point, "SL"
            break
        if point["remaining"] <= Decimal("5"):
            selected, trigger = point, "CLOSE_5S"
            break
    if selected is None:
        candidates = [point for point in path if point["remaining"] > 0]
        if not candidates:
            return None
        selected, trigger = candidates[-1], "LAST_EXECUTABLE"
    pnl = selected["vwap"] - entry["vwap"] - entry["fee"] - selected["fee"]
    return {"exitUs": selected["sourceUs"], "exitRemaining": selected["remaining"], "exitVwap": selected["vwap"],
            "exitFee": selected["fee"], "trigger": trigger, "pnlPerShare": pnl,
            "holdingSeconds": Decimal(selected["sourceUs"] - entry["sourceUs"]) / Decimal("1000000")}


def average(values: list[Decimal]) -> Decimal | None:
    return sum(values, ZERO) / len(values) if values else None


def fmt(value: Decimal | None, places: int = 6) -> str | None:
    if value is None:
        return None
    return f"{value:.{places}f}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, default=ROOT / "data/derived/live_strategy_cache.sqlite3")
    parser.add_argument("--output", type=Path, default=ROOT / "outputs/research_tests/intramarket_exit_results.json")
    parser.add_argument("--markdown", type=Path, default=ROOT / "outputs/research_tests/中途区间入场与盘前退出回测.md")
    args = parser.parse_args()
    connection = sqlite3.connect(args.cache)
    connection.row_factory = sqlite3.Row
    markets = list(connection.execute("SELECT market_id,end_us,winner FROM markets WHERE winner IN ('UP','DOWN') AND end_us IS NOT NULL ORDER BY end_us"))
    trades: list[dict[str, Any]] = []
    for index, market in enumerate(markets, 1):
        snapshots = load_market_snapshots(connection, str(market["market_id"]))
        if not snapshots:
            continue
        for outcome in ("UP", "DOWN"):
            for window_id, (minimum, maximum) in WINDOWS.items():
                for band_id, (low, high) in BANDS.items():
                    entry = find_entry(snapshots, int(market["end_us"]), outcome, minimum, maximum, low, high)
                    if entry is None:
                        continue
                    path = sell_path(snapshots, int(market["end_us"]), outcome, entry["sourceUs"])
                    won = outcome == str(market["winner"])
                    hold_pnl = (Decimal("1") if won else ZERO) - entry["vwap"] - entry["fee"]
                    for policy in POLICIES:
                        exit_row = execute_policy(path, entry, policy)
                        trades.append({"marketId": market["market_id"], "winner": market["winner"], "outcome": outcome,
                                       "wonAtResolution": won, "window": window_id, "band": band_id, "policy": policy.policy_id,
                                       "entryUs": entry["sourceUs"], "entryRemaining": fmt(entry["remaining"], 3),
                                       "entryVwap": fmt(entry["vwap"]), "entryFee": fmt(entry["fee"]),
                                       "holdPnlPerShare": fmt(hold_pnl), "exit": ({k: fmt(v, 6) if isinstance(v, Decimal) else v for k, v in exit_row.items()} if exit_row else None)})
        if index % 50 == 0:
            print(f"processed={index}/{len(markets)} trades={len(trades)}")
    connection.close()

    grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        grouped[(trade["window"], trade["band"], trade["outcome"], trade["policy"])].append(trade)
    summaries = []
    for (window, band, outcome, policy), items in grouped.items():
        exits = [item for item in items if item["exit"]]
        pnls = [Decimal(item["exit"]["pnlPerShare"]) for item in exits]
        hold = [Decimal(item["holdPnlPerShare"]) for item in items]
        positive = sum(value > 0 for value in pnls)
        triggers = defaultdict(int)
        for item in exits:
            triggers[item["exit"]["trigger"]] += 1
        summaries.append({"window": window, "band": band, "outcome": outcome, "policy": policy,
                          "entries": len(items), "completeExits": len(exits), "exitRate": len(exits) / len(items),
                          "positiveExits": positive, "positiveRate": positive / len(exits) if exits else None,
                          "averageExitPnlPerShare": fmt(average(pnls)), "medianExitPnlPerShare": fmt(Decimal(str(median(pnls))) if pnls else None),
                          "averageHoldPnlPerShare": fmt(average(hold)), "triggerCounts": dict(triggers)})
    summaries.sort(key=lambda row: (row["window"], row["band"], row["outcome"], row["policy"]))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"schemaVersion": 1, "settledMarkets": len(markets), "quantity": str(QTY),
                                      "windows": WINDOWS, "bands": {k: [str(a), str(b)] for k, (a, b) in BANDS.items()},
                                      "policies": [policy.__dict__ for policy in POLICIES], "summaries": summaries, "trades": trades},
                                     ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")

    eligible = [row for row in summaries if row["completeExits"] >= 12 and row["exitRate"] >= 0.8]
    best = sorted(eligible, key=lambda row: Decimal(row["averageExitPnlPerShare"] or "-99"), reverse=True)[:20]
    base_close = [row for row in summaries if row["policy"] == "close_5s" and row["completeExits"] >= 8]
    lines = ["# 中途价格区间入场与收盘前退出回测", "", "## 测试标准", "",
             f"- 数据：本地实时盘口缓存中的 {len(markets)} 个已结算 BTC 5分钟市场。", "- 方向：UP、DOWN 分开；每个市场、方向、时间窗和价格带取第一笔可成交机会。",
             "- 入场：按卖盘逐档吃满10份；不是只看卖一价。", "- 退出：按对手方向卖盘的互补价格还原本方向买盘，再逐档卖出10份；买卖两端都计入手续费。",
             "- 时间窗：剩余180–120、120–90、90–60、60–30秒。", "- 入场VWAP带：0.55–0.60、0.60–0.65、0.65–0.70、0.70–0.75、0.75–0.80、0.80–0.85。",
             "- 退出策略：固定在剩余5秒附近卖出，以及多组绝对概率变化止盈/止损；若5秒处不能完整卖10份，使用结束前最后一个可完整成交盘口并单独标记。", "",
             "## 目前表现最好的可完整退出组合", "", "仅展示至少12笔、退出完成率至少80%的组合；这是探索性排序，不是实盘建议。", "",
             "| 时间窗 | 入场带 | 方向 | 退出规则 | 入场 | 完整退出 | 正收益率 | 平均退出收益/份 | 持有到结算收益/份 |", "|---|---|---|---|---:|---:|---:|---:|---:|"]
    for row in best:
        lines.append(f"| {row['window'].replace('_','–')}秒 | 0.{row['band'][:2]}–0.{row['band'][3:]} | {row['outcome']} | {POLICY_LABELS[row['policy']]} | {row['entries']} | {row['completeExits']} | {(row['positiveRate'] or 0)*100:.1f}% | {row['averageExitPnlPerShare']} | {row['averageHoldPnlPerShare']} |")
    lines += ["", "## 仅在收盘前约5秒退出", "", "| 时间窗 | 入场带 | 方向 | 入场 | 完整退出 | 正收益率 | 平均退出收益/份 | 持有到结算收益/份 |", "|---|---|---|---:|---:|---:|---:|---:|"]
    for row in sorted(base_close, key=lambda r: (r["window"], r["band"], r["outcome"])):
        lines.append(f"| {row['window'].replace('_','–')}秒 | 0.{row['band'][:2]}–0.{row['band'][3:]} | {row['outcome']} | {row['entries']} | {row['completeExits']} | {(row['positiveRate'] or 0)*100:.1f}% | {row['averageExitPnlPerShare']} | {row['averageHoldPnlPerShare']} |")
    lines += ["", "## 解释限制", "", "- 盘口快照来自本机收到的数据；断线或稀疏时段不会被当作连续可交易行情。", "- 退出模拟要求10份全部成交，但没有模拟排队位置和网络下单延迟。", "- 5分钟二元市场临近结束时买盘可能消失，因此必须同时查看完整退出率，不能只看已退出样本收益。", "- 全量组合和逐笔路径保存在同目录 JSON，可继续改止盈止损数值而无需修改实时监控。",
              "", "## 可复现命令", "", "```bash", "PYTHONDONTWRITEBYTECODE=1 /Users/laochen/.local/bin/python3.12 \\", "  scripts/analysis/analyze_intramarket_exits.py", "```"]
    args.markdown.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"markets={len(markets)} tradeRows={len(trades)} summaryRows={len(summaries)}")
    print(args.output.resolve())
    print(args.markdown.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
