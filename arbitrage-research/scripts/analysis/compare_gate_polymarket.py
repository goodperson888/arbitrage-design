#!/usr/bin/env python3
"""按同一5分钟市场对齐 Gate 粗纸上信号与 Polymarket 盘口纸上成交。"""

from __future__ import annotations

import argparse
import gzip
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[2]


def rows(paths: Iterable[Path]) -> Iterable[dict[str, Any]]:
    for path in sorted(paths):
        opener = gzip.open if path.suffix == ".gz" else open
        try:
            with opener(path, "rt", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if isinstance(row, dict):
                        yield row
        except (EOFError, gzip.BadGzipFile, OSError):
            continue


def canonical(row: dict[str, Any]) -> str:
    market = str(row.get("marketId") or "")
    if market.startswith("btc-updown-5m-"):
        return market
    start = row.get("marketStartUs")
    if start:
        try:
            return f"btc-updown-5m-{int(start) // 1_000_000}"
        except (TypeError, ValueError):
            pass
    return market


def read_gate() -> tuple[list[dict[str, Any]], dict[tuple[str, str], dict[str, Any]]]:
    signals, settlements = [], {}
    for row in rows((ROOT / "data/gate_monitor/paper").glob("date=*/hour=*/part-*.ndjson.gz")):
        key = (canonical(row), str(row.get("outcome") or ""))
        if row.get("eventType") == "GATE_PAPER_SIGNAL":
            signals.append({**row, "canonicalMarketId": key[0]})
        elif row.get("eventType") == "GATE_PAPER_SETTLEMENT":
            settlements[key] = row
    return signals, settlements


def read_poly() -> tuple[list[dict[str, Any]], dict[tuple[str, str], dict[str, Any]]]:
    fills, settlements = [], {}
    for row in rows((ROOT / "data/live_monitor/paper").glob("date=*/hour=*/part-*.ndjson")):
        market = str(row.get("marketId") or "")
        outcome = str(row.get("outcome") or "")
        key = (market, outcome)
        if row.get("eventType") == "PAPER_FILL":
            payload = row.get("payload") or {}
            fills.append({**row, "canonicalMarketId": market, "entryVwap": payload.get("entryVwap"),
                          "strategyId": payload.get("strategyId")})
        elif row.get("eventType") == "PAPER_SETTLEMENT":
            settlements[key] = row
    return fills, settlements


def outcome(row: dict[str, Any], gate: bool) -> tuple[bool | None, str | None]:
    if gate:
        winner = row.get("winner")
        return ((winner == row.get("outcome")) if winner else None), winner
    winner = (row.get("payload") or {}).get("winnerOutcome")
    return ((winner == row.get("outcome")) if winner else None), winner


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "outputs/cross_market_validation/gate_polymarket_comparison.json")
    parser.add_argument("--markdown", type=Path, default=ROOT / "outputs/cross_market_validation/Gate与Polymarket对照分析_20260822.md")
    args = parser.parse_args()
    gate_signals, gate_settlements = read_gate()
    poly_fills, poly_settlements = read_poly()
    gate_closed = []
    for row in gate_signals:
        settlement = gate_settlements.get((row["canonicalMarketId"], str(row.get("outcome"))))
        if settlement:
            won, winner = outcome({**row, **settlement}, True)
            gate_closed.append({"marketId": row["canonicalMarketId"], "outcome": row["outcome"], "entryTimeUs": row.get("receivedTimeUs"),
                                "entryPrice": row.get("entryPrice"), "winner": winner, "won": won, "source": "Gate"})
    poly_closed = []
    for row in poly_fills:
        settlement = poly_settlements.get((row["canonicalMarketId"], str(row.get("outcome"))))
        if settlement:
            won, winner = outcome({**row, **settlement}, False)
            poly_closed.append({"marketId": row["canonicalMarketId"], "outcome": row["outcome"], "entryTimeUs": row.get("receivedTimeUs"),
                                "entryVwap": row.get("entryVwap"), "winner": winner, "won": won, "strategyId": row.get("strategyId"), "source": "Polymarket"})
    gmap = {(x["marketId"], x["outcome"]): x for x in gate_closed}
    pmap = {(x["marketId"], x["outcome"]): x for x in poly_closed}
    common = sorted(set(gmap) & set(pmap))
    rows_out = []
    for key in common:
        g, p = gmap[key], pmap[key]
        rows_out.append({"marketId": key[0], "outcome": key[1], "gateEntryTimeUs": g["entryTimeUs"], "polyEntryTimeUs": p["entryTimeUs"],
                         "entryTimeDiffMs": (int(g["entryTimeUs"]) - int(p["entryTimeUs"])) / 1000,
                         "gateEntryPrice": g["entryPrice"], "polyEntryVwap": p["entryVwap"], "winner": g["winner"],
                         "gateWon": g["won"], "polyWon": p["won"], "sameWinner": g["winner"] == p["winner"]})
    def stats(items: list[dict[str, Any]]) -> dict[str, Any]:
        wins = sum(bool(x["won"]) for x in items)
        return {"trades": len(items), "wins": wins, "losses": len(items) - wins, "winRate": wins / len(items) if items else None}
    summary = {"gateSignals": len(gate_signals), "gateClosed": stats(gate_closed), "polymarketFills": len(poly_fills),
               "polymarketClosed": stats(poly_closed), "matchedPairs": len(rows_out),
               "matchedGate": stats([gmap[k] for k in common]), "matchedPolymarket": stats([pmap[k] for k in common]),
               "directionCounts": {"gate": Counter(x["outcome"] for x in gate_closed), "polymarket": Counter(x["outcome"] for x in poly_closed)},
               "winnerDisagreement": sum(not x["sameWinner"] for x in rows_out), "pairs": rows_out}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=dict) + "\n")
    g, p, mg, mp = summary["gateClosed"], summary["polymarketClosed"], summary["matchedGate"], summary["matchedPolymarket"]
    unmatched_gate = [gmap[key] for key in set(gmap) - set(common)]
    unmatched_poly = [pmap[key] for key in set(pmap) - set(common)]
    unmatched_gate_stats = stats(unmatched_gate)
    unmatched_poly_stats = stats(unmatched_poly)
    lines = ["# Gate 与 Polymarket 同市场对照分析", "", "## 结论", "",
             "两边的全量次数不能直接比较：Gate 是粗粒度 1 份、观察卖一价的独立纸上信号；Polymarket 是 10 份逐档盘口成交模拟并扣模拟手续费。只有同一市场、同一方向的交集才可用于逐笔比较。", "",
             "## 全量已结算样本", "", "| 来源 | 入场记录 | 已结算 | 胜 | 负 | 胜率 |", "|---|---:|---:|---:|---:|---:|",
             f"| Gate | {len(gate_signals)} | {g['trades']} | {g['wins']} | {g['losses']} | {g['winRate']*100:.1f}% |",
             f"| Polymarket | {len(poly_fills)} | {p['trades']} | {p['wins']} | {p['losses']} | {p['winRate']*100:.1f}% |", "",
             "## 同一市场、同一方向交集", "", "| 来源 | 交集入场 | 胜 | 负 | 胜率 |", "|---|---:|---:|---:|---:|",
             f"| Gate | {mg['trades']} | {mg['wins']} | {mg['losses']} | {mg['winRate']*100:.1f}% |",
             f"| Polymarket | {mp['trades']} | {mp['wins']} | {mp['losses']} | {mp['winRate']*100:.1f}% |", "",
             f"交集市场数：{len(common)}；结算方向不一致：{summary['winnerDisagreement']} 笔。", "",
             f"交集之外：Gate 还有 {unmatched_gate_stats['trades']} 笔已结算（{unmatched_gate_stats['wins']}胜{unmatched_gate_stats['losses']}负），Polymarket 还有 {unmatched_poly_stats['trades']} 笔已结算（{unmatched_poly_stats['wins']}胜{unmatched_poly_stats['losses']}负）。这部分不是同一批市场，不能直接做胜率差归因。", "",
             "## 差异来源", "", "1. **样本时间不同**：Gate 直连采集从较晚时段开始，Polymarket 纸上文件包含更早时段；全量记录不是同一批市场。", "2. **入场定义不同**：Gate 的卖一价进入 `0.80–0.85` 即记录1份；Polymarket 需要10份在盘口上实际逐档吃满，并按实际 VWAP 和手续费计算，因此会漏掉深度不足或 VWAP 超限的 Gate 信号。", "3. **方向可重复**：同一市场可能先后触发 UP、DOWN；不能只按市场数统计，必须按“市场+方向”统计。", "4. **结算规则基本可比**：两边都使用各自平台的 UP/DOWN 结算标签；如果交集中的 winner 不一致，优先检查平台同步时间/市场映射，而不是直接认为策略反转。", "", "## 交集逐笔明细", "", "| 市场 | 方向 | Gate价格 | Poly VWAP | Gate结果 | Poly结果 | 入场时间差(ms) |", "|---|---|---:|---:|---|---|---:|"]
    for x in rows_out:
        lines.append(f"| {x['marketId']} | {x['outcome']} | {x['gateEntryPrice']} | {x['polyEntryVwap']} | {'胜' if x['gateWon'] else '负'} | {'胜' if x['polyWon'] else '负'} | {x['entryTimeDiffMs']:.1f} |")
    args.markdown.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ('gateSignals','gateClosed','polymarketFills','polymarketClosed','matchedPairs','matchedGate','matchedPolymarket','winnerDisagreement')}, ensure_ascii=False, indent=2, default=dict))
    print(args.output.resolve())
    print(args.markdown.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
