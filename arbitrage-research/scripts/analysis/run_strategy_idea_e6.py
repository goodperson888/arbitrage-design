#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import defaultdict
import csv
from dataclasses import dataclass
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from arb_research.experiments.orderbook_depth import BookLevel, consume_asks
from arb_research.experiments.strategy_idea_validation import (
    maker_limit_from_opposite_ask,
    maker_trade_fill_evidence,
)


CURRENT_CASE = "trade_down_vwap_0_80_0_85_momentum5_0_01_90_30_q10_size10"
WAIT_SECONDS = (0.5, 1.0, 2.0, 5.0)
LIMIT_SCHEMES = ("derived_best_bid", "fixed_0.80", "fixed_0.82", "fixed_0.84")
QUANTITY = 10.0


@dataclass
class Summary:
    offered: int = 0
    eligible: int = 0
    strict_fills: int = 0
    strict_wins: int = 0
    strict_pnl: float = 0.0
    touch_fills: int = 0
    touch_wins: int = 0
    touch_pnl: float = 0.0
    fallback_fills: int = 0
    hybrid_wins: int = 0
    hybrid_pnl: float = 0.0

    def add(self, row: dict[str, Any]) -> None:
        self.offered += 1
        self.eligible += int(row["postOnlyEligible"])
        if row["strictFill"]:
            self.strict_fills += 1
            self.strict_wins += int(row["won"])
            self.strict_pnl += float(row["makerPnlPerShare"])
        if row["touchFill"]:
            self.touch_fills += 1
            self.touch_wins += int(row["won"])
            self.touch_pnl += float(row["makerPnlPerShare"])
        if row["hybridFilled"]:
            self.fallback_fills += int(row["fallbackFilled"])
            self.hybrid_wins += int(row["won"])
            self.hybrid_pnl += float(row["hybridPnlPerShare"])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="执行E6 FAK与Post-only Maker成交对照")
    parser.add_argument(
        "--cache", type=Path,
        default=ROOT / "data/derived/live_strategy_cache.sqlite3",
    )
    parser.add_argument(
        "--signals", type=Path,
        default=ROOT / "outputs/research_tests/e6_maker_fak_20260826/current_strategy_signals/incremental_strategy_fills.csv",
    )
    parser.add_argument(
        "--out", type=Path,
        default=ROOT / "outputs/research_tests/e6_maker_fak_20260826",
    )
    parser.add_argument("--case-id", default=CURRENT_CASE,
                        help="从增量成交CSV中选择要做Maker/FAK对照的策略案例")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    signals = load_signals(args.signals, args.case_id)
    connection = sqlite3.connect(args.cache)
    connection.row_factory = sqlite3.Row
    try:
        market_meta = {
            str(row["market_id"]): dict(row)
            for row in connection.execute(
                "SELECT market_id,start_us,end_us,winner FROM markets WHERE winner IN ('UP','DOWN')"
            )
        }
        attach_split(signals, market_meta)
        rows = simulate(connection, signals, market_meta)
    finally:
        connection.close()

    summaries: defaultdict[tuple[str, str, float], Summary] = defaultdict(Summary)
    for row in rows:
        for scope in ("all", row["split"]):
            summaries[(scope, row["limitScheme"], row["waitSeconds"])].add(row)
    summary_rows = [summary_row(key, value) for key, value in summaries.items()]
    fak = fak_summary(signals)
    result = {
        "schemaVersion": 1,
        "experiment": "E6_FAK与PostOnly_Maker对照",
        "currentCase": args.case_id,
        "signalCount": len(signals),
        "marketCount": len({row["market_id"] for row in signals}),
        "makerAssumptions": {
            "quantity": QUANTITY,
            "makerFeePerShare": 0,
            "strictFill": "等待期内出现该方向SELL成交价严格低于挂单价",
            "touchUpperBound": "忽略排队位置，SELL成交价不高于挂单价的累计量达到10份",
            "hybrid": "严格穿价未成交则等待期结束撤单，并按当时真实盘口以0.85上限FAK",
        },
        "fakBaseline": fak,
        "summaries": sorted(
            summary_rows,
            key=lambda row: (row["scope"], row["limitScheme"], row["waitSeconds"]),
        ),
    }
    result_path = args.out / "e6_results.json"
    detail_path = args.out / "e6_maker_simulations.csv.gz"
    report_path = args.out / "E6_FAK与Maker成交对照.md"
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    import gzip
    with gzip.open(detail_path, "wt", newline="", encoding="utf-8") as handle:
        fields = list(rows[0]) if rows else []
        writer = csv.DictWriter(handle, fieldnames=fields)
        if fields:
            writer.writeheader()
            writer.writerows(rows)
    write_report(report_path, result)
    print(result_path)
    print(detail_path)
    print(report_path)
    return 0


def load_signals(path: Path, case_id: str = CURRENT_CASE) -> list[dict[str, Any]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return [row for row in csv.DictReader(handle) if row["caseId"] == case_id]


def attach_split(signals: list[dict[str, Any]], markets: dict[str, dict[str, Any]]) -> None:
    ordered = sorted(
        {row["market_id"] for row in signals},
        key=lambda market_id: int(markets[market_id]["start_us"]),
    )
    cut = max(1, min(len(ordered) - 1, int(len(ordered) * 0.7)))
    train_ids = set(ordered[:cut])
    for row in signals:
        row["split"] = "train" if row["market_id"] in train_ids else "validation"


def simulate(
    connection: sqlite3.Connection,
    signals: list[dict[str, Any]],
    markets: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for signal in signals:
        market_id = signal["market_id"]
        outcome = signal["outcome"]
        opposite = "UP" if outcome == "DOWN" else "DOWN"
        order_us = int(signal["execution_us"])
        own_snapshot = latest_snapshot(connection, market_id, outcome, order_us)
        opposite_snapshot = latest_snapshot(connection, market_id, opposite, order_us)
        trades = [
            {
                "receivedUs": int(row["received_us"]), "side": row["side"],
                "price": float(row["price"]), "size": float(row["size"]),
            }
            for row in connection.execute(
                """SELECT received_us,side,price,size FROM trades
                   WHERE market_id=? AND outcome=? AND received_us>? AND received_us<=?
                   ORDER BY received_us""",
                (market_id, outcome, order_us, order_us + 5_000_000),
            )
        ]
        for wait_seconds in WAIT_SECONDS:
            wait_us = int(wait_seconds * 1_000_000)
            for scheme in LIMIT_SCHEMES:
                limit = limit_price(scheme, opposite_snapshot)
                eligible = (
                    own_snapshot is not None and limit is not None
                    and 0 < limit < float(own_snapshot["best_ask"]) - 1e-12
                )
                evidence = (
                    maker_trade_fill_evidence(
                        trades,
                        order_received_us=order_us,
                        wait_us=wait_us,
                        limit_price=float(limit),
                        quantity=QUANTITY,
                    )
                    if eligible else {
                        "touchFill": False, "touchFillUs": None, "touchVolume": 0,
                        "tradeThroughFill": False, "tradeThroughFillUs": None,
                    }
                )
                won = outcome == markets[market_id]["winner"]
                maker_pnl = (1.0 if won else 0.0) - float(limit or 0)
                fallback = None
                if eligible and not evidence["tradeThroughFill"]:
                    fallback = fallback_fill(
                        connection, market_id, outcome, order_us + wait_us
                    )
                strict_fill = bool(evidence["tradeThroughFill"])
                fallback_filled = fallback is not None
                hybrid_filled = strict_fill or fallback_filled
                hybrid_pnl = (
                    maker_pnl if strict_fill else (
                        (1.0 if won else 0.0) - fallback["totalCost"]
                        if fallback is not None else 0.0
                    )
                )
                output.append({
                    "marketId": market_id, "split": signal["split"],
                    "outcome": outcome, "winner": markets[market_id]["winner"],
                    "won": won, "orderReceivedUs": order_us,
                    "remainingSeconds": signal["remaining_seconds"],
                    "fakEntryVwap": signal["entry_vwap"],
                    "fakFeePerShare": signal["fee_per_share"],
                    "fakPnlPerShare": signal["pnl_per_share"],
                    "currentAsk": float(own_snapshot["best_ask"]) if own_snapshot else None,
                    "oppositeAsk": float(opposite_snapshot["best_ask"]) if opposite_snapshot else None,
                    "limitScheme": scheme, "limitPrice": limit,
                    "waitSeconds": wait_seconds, "postOnlyEligible": eligible,
                    "strictFill": strict_fill,
                    "strictFillUs": evidence["tradeThroughFillUs"],
                    "touchFill": bool(evidence["touchFill"]),
                    "touchFillUs": evidence["touchFillUs"],
                    "touchVolume": evidence["touchVolume"],
                    "makerPnlPerShare": maker_pnl,
                    "fallbackFilled": fallback_filled,
                    "fallbackVwap": fallback["vwap"] if fallback else None,
                    "fallbackFeePerShare": fallback["fee"] if fallback else None,
                    "hybridFilled": hybrid_filled,
                    "hybridPnlPerShare": hybrid_pnl,
                })
    return output


def latest_snapshot(
    connection: sqlite3.Connection, market_id: str, outcome: str, received_us: int
) -> sqlite3.Row | None:
    row = connection.execute(
        """SELECT received_us,best_ask,asks_json FROM snapshots
           WHERE market_id=? AND outcome=? AND received_us<=? AND best_ask IS NOT NULL
           ORDER BY received_us DESC LIMIT 1""",
        (market_id, outcome, received_us),
    ).fetchone()
    if row is None or received_us - int(row["received_us"]) > 2_000_000:
        return None
    return row


def limit_price(scheme: str, opposite_snapshot: sqlite3.Row | None) -> float | None:
    if scheme == "derived_best_bid":
        if opposite_snapshot is None:
            return None
        return maker_limit_from_opposite_ask(float(opposite_snapshot["best_ask"]))
    return float(scheme.removeprefix("fixed_"))


def fallback_fill(
    connection: sqlite3.Connection, market_id: str, outcome: str, received_us: int
) -> dict[str, float] | None:
    snapshot = latest_snapshot(connection, market_id, outcome, received_us)
    if snapshot is None:
        return None
    levels = [
        BookLevel(Decimal(str(price)), Decimal(str(size)))
        for price, size in json.loads(snapshot["asks_json"])
    ]
    fill = consume_asks(
        levels, Decimal("10"), Decimal("0.85"),
        fee_model="polymarket_crypto_taker", fee_rate=Decimal("0.07"),
    )
    if fill is None or fill[0] < Decimal("0.80"):
        return None
    return {"vwap": float(fill[0]), "fee": float(fill[1]), "totalCost": float(fill[0] + fill[1])}


def summary_row(key: tuple[str, str, float], summary: Summary) -> dict[str, Any]:
    scope, scheme, wait = key
    offered = summary.offered or 1
    return {
        "scope": scope, "limitScheme": scheme, "waitSeconds": wait,
        "offeredSignals": summary.offered, "postOnlyEligible": summary.eligible,
        "strictFillCount": summary.strict_fills,
        "strictFillRateOffered": summary.strict_fills / offered,
        "strictWinRate": summary.strict_wins / summary.strict_fills if summary.strict_fills else None,
        "strictPnlPerFilledShare": summary.strict_pnl / summary.strict_fills if summary.strict_fills else None,
        "strictPnlPerOfferedSignalShare": summary.strict_pnl / offered,
        "touchFillCount": summary.touch_fills,
        "touchFillRateOffered": summary.touch_fills / offered,
        "touchWinRate": summary.touch_wins / summary.touch_fills if summary.touch_fills else None,
        "touchPnlPerFilledShare": summary.touch_pnl / summary.touch_fills if summary.touch_fills else None,
        "touchPnlPerOfferedSignalShare": summary.touch_pnl / offered,
        "fallbackFillCount": summary.fallback_fills,
        "hybridFillCount": summary.strict_fills + summary.fallback_fills,
        "hybridFillRateOffered": (summary.strict_fills + summary.fallback_fills) / offered,
        "hybridWinRate": summary.hybrid_wins / (summary.strict_fills + summary.fallback_fills)
        if summary.strict_fills + summary.fallback_fills else None,
        "hybridPnlPerOfferedSignalShare": summary.hybrid_pnl / offered,
    }


def fak_summary(signals: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for scope in ("all", "train", "validation"):
        rows = [row for row in signals if scope == "all" or row["split"] == scope]
        wins = sum(row["winner"] == row["outcome"] for row in rows)
        result[scope] = {
            "signals": len(rows), "wins": wins, "losses": len(rows) - wins,
            "winRate": wins / len(rows) if rows else None,
            "pnlPerShare": sum(float(row["pnl_per_share"]) for row in rows) / len(rows)
            if rows else None,
        }
    return result


def write_report(path: Path, result: dict[str, Any]) -> None:
    lines = [
        "# E6：FAK 与 Post-only Maker 成交对照", "",
        "## 结论", "",
        "本轮只比较执行方式，不重新挑选方向或入场信号。基准信号固定为当前候选策略：",
        "DOWN、剩余90–30秒、10份、实际VWAP 0.80–0.85、入场前5秒同方向动量至少0.01。", "",
    ]
    validation = [row for row in result["summaries"] if row["scope"] == "validation"]
    best = max(validation, key=lambda row: row["hybridPnlPerOfferedSignalShare"])
    fak = result["fakBaseline"]["validation"]
    lines.extend([
        f"- 最新固定策略共 {result['signalCount']} 笔；时间顺序验证段 {fak['signals']} 笔。",
        f"- 验证段直接 FAK：胜率 {fak['winRate']:.2%}，净收益 {fak['pnlPerShare']:+.4f}/份。",
        f"- 验证段最佳 Maker→FAK 组合为 `{best['limitScheme']}`、等待 {best['waitSeconds']:g} 秒："
        f"成交率 {best['hybridFillRateOffered']:.2%}，按所有原始信号计净收益 "
        f"{best['hybridPnlPerOfferedSignalShare']:+.4f}/份。",
        "- 只有严格穿价才作为较可信 Maker 成交；“触价成交”只是忽略排队位置的乐观上界，不能当成实盘结论。",
        "- Maker 结果未计返佣；历史数据也没有本账户的真实排队位置。因此 E6 只能判断执行方案是否值得实盘记录，不能证明挂单必成交。", "",
        "## 时间顺序验证段", "",
        "| 挂单价 | 等待 | 可挂 | 严格成交 | 严格成交率 | 严格每信号收益 | 触价上界成交 | Maker→FAK成交率 | Maker→FAK每信号收益 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for row in validation:
        lines.append(
            f"| {row['limitScheme']} | {row['waitSeconds']:g}s | {row['postOnlyEligible']} | "
            f"{row['strictFillCount']} | {row['strictFillRateOffered']:.1%} | "
            f"{row['strictPnlPerOfferedSignalShare']:+.4f} | {row['touchFillCount']} | "
            f"{row['hybridFillRateOffered']:.1%} | {row['hybridPnlPerOfferedSignalShare']:+.4f} |"
        )
    lines.extend([
        "", "## 口径", "",
        "- 下单时刻：原 BUY 主动成交信号抵达本机、且原 FAK 能读取盘口的时刻。",
        "- `derived_best_bid`：用另一结果的最低卖价推导本结果最高买价，即 `1 - opposite ask`。",
        "- Post-only 有效：挂单价必须严格低于本方向当时最低卖价，否则视为会吃单/被拒绝。",
        "- 严格成交：等待期内出现本方向 SELL 成交，且成交价严格低于挂单价；价格优先规则下，这比单纯触价更强。",
        "- 触价上界：SELL 成交价不高于挂单价的累计量达到10份，但完全不知道前方队列，所以只作上界。",
        "- Maker→FAK：严格成交未发生时，等待0.5/1/2/5秒后撤单；若最新盘口仍满足10份、VWAP 0.80–0.85，则按原手续费模型吃单。",
        "- 未成交信号按收益0计入‘每原始信号收益’，从而不会只看已成交样本夸大结果。", "",
        "## 是否通过", "",
        "E6 不设置‘胜率通过’门槛，因为它研究的是执行质量。若最佳组合只在触价上界好看、严格成交率过低，"
        "就不能替代 FAK；后续实盘必须记录 order_id、挂单时长、成交回报、部分成交和撤单结果，才能做真实 Maker 对照。", "",
        "## 产物", "",
        "- `e6_results.json`：汇总结果。",
        "- `e6_maker_simulations.csv.gz`：逐信号、逐挂单价、逐等待时间明细。",
        "- `current_strategy_signals/`：本次用最新事件缓存重算的原 FAK 信号与基准报告。", "",
    ])
    path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
