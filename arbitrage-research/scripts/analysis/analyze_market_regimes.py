#!/usr/bin/env python3
"""分析标准80-85主动成交策略的时段、平稳度和输赢前差异。"""

from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import defaultdict
from decimal import Decimal
from datetime import datetime, timezone
import gzip
import json
from pathlib import Path
import sqlite3
from statistics import mean, median
from typing import Any, Iterable
from zoneinfo import ZoneInfo
import zlib

ROOT = Path(__file__).resolve().parents[2]
TARGET_CASES = {"trade_down_80_85_90_30_q10_size10", "trade_up_80_85_90_30_q10_size10"}
LOCAL_TZ = ZoneInfo("Asia/Shanghai")
QTY = Decimal("10")


def json_rows(paths: Iterable[Path]) -> Iterable[dict[str, Any]]:
    for path in sorted(paths):
        opener = gzip.open if path.suffix == ".gz" else open
        try:
            with opener(path, "rt", encoding="utf-8") as handle:
                while True:
                    try:
                        line = handle.readline()
                    except (EOFError, gzip.BadGzipFile, zlib.error):
                        break
                    if not line:
                        break
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(row, dict):
                        yield row
        except (EOFError, gzip.BadGzipFile, OSError, zlib.error):
            continue


def consume_asks(raw: str | list[list[str]], cap: Decimal = Decimal("0.85")) -> Decimal | None:
    levels = json.loads(raw) if isinstance(raw, str) else raw
    remaining, notional = QTY, Decimal("0")
    for price_raw, size_raw in sorted(levels, key=lambda x: Decimal(str(x[0]))):
        price, size = Decimal(str(price_raw)), Decimal(str(size_raw))
        if price > cap:
            break
        take = min(remaining, size)
        notional += take * price
        remaining -= take
        if remaining <= 0:
            return notional / QTY
    return None


def consume_complement_bids(raw_opposite_asks: str | list[list[str]]) -> Decimal | None:
    levels = json.loads(raw_opposite_asks) if isinstance(raw_opposite_asks, str) else raw_opposite_asks
    bids = sorted(((Decimal("1") - Decimal(str(p)), Decimal(str(s))) for p, s in levels), reverse=True)
    remaining, notional = QTY, Decimal("0")
    for price, size in bids:
        take = min(remaining, size)
        notional += take * price
        remaining -= take
        if remaining <= 0:
            return notional / QTY
    return None


def last_value(rows: list[sqlite3.Row], at_us: int, field: str = "best_ask") -> float | None:
    times = [int(row["source_us"]) for row in rows]
    pos = bisect_right(times, at_us) - 1
    if pos < 0 or rows[pos][field] is None:
        return None
    return float(rows[pos][field])


def classify_range(value: float | None) -> str:
    if value is None:
        return "缺失"
    if value < 0.02:
        return "平稳<0.02"
    if value < 0.05:
        return "一般0.02–0.05"
    return "剧烈≥0.05"


def classify_momentum(value: float | None) -> str:
    if value is None:
        return "缺失"
    if value >= 0.01:
        return "同方向上涨≥0.01"
    if value <= -0.01:
        return "入场前回落≤-0.01"
    return "近5秒平稳"


def load_cross(targets: set[str]) -> dict[str, list[tuple[int, dict[str, Any]]]]:
    result: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    paths = (ROOT / "data/cross_market_validation/venue_snapshots").glob("date=*/hour=*/part-*.ndjson*")
    for row in json_rows(paths):
        market_id = str(row.get("marketId") or "")
        if market_id in targets:
            result[market_id].append((int(row.get("sourceTimeUs") or row.get("receivedTimeUs") or 0), row))
    for values in result.values():
        values.sort(key=lambda x: x[0])
    return result


def cross_before(rows: list[tuple[int, dict[str, Any]]], at_us: int) -> dict[str, Any] | None:
    if not rows:
        return None
    pos = bisect_right([item[0] for item in rows], at_us) - 1
    return rows[pos][1] if pos >= 0 else None


def avg(values: list[float | None]) -> float | None:
    clean = [value for value in values if value is not None]
    return mean(clean) if clean else None


def med(values: list[float | None]) -> float | None:
    clean = [value for value in values if value is not None]
    return median(clean) if clean else None


def summarize(items: list[dict[str, Any]], label: str) -> dict[str, Any]:
    wins = sum(bool(item["won"]) for item in items)
    return {"label": label, "trades": len(items), "wins": wins, "losses": len(items) - wins,
            "winRate": wins / len(items) if items else None,
            "avgPnlPerShare": avg([item.get("pnlPerShare") for item in items]),
            "avgEntryVwap": avg([item.get("entryVwap") for item in items]),
            "avgRemaining": avg([item.get("remainingSeconds") for item in items]),
            "avgRange15": avg([item.get("range15") for item in items]),
            "avgMomentum5": avg([item.get("momentum5") for item in items]),
            "avgSpread": avg([item.get("spread") for item in items]),
            "avgMfe": avg([item.get("mfe") for item in items]), "avgMae": avg([item.get("mae") for item in items])}


def group_summary(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get(key, "缺失"))].append(row)
    return [summarize(items, label) for label, items in sorted(grouped.items())]


def f(value: float | None, digits: int = 3) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def rate(value: float | None) -> str:
    return "—" if value is None else f"{value*100:.1f}%"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, default=ROOT / "data/derived/live_strategy_cache.sqlite3")
    parser.add_argument("--case-results", type=Path, default=ROOT / "outputs/live_incremental_strategy_validation/incremental_strategy_results.json")
    parser.add_argument("--output", type=Path, default=ROOT / "outputs/research_tests/market_regime_results.json")
    parser.add_argument("--markdown", type=Path, default=ROOT / "outputs/research_tests/时段平稳度与失败样本差异.md")
    parser.add_argument("--case-id", action="append", help="只分析指定策略案例；可重复传入。")
    args = parser.parse_args()
    target_cases = set(args.case_id or TARGET_CASES)
    connection = sqlite3.connect(args.cache)
    connection.row_factory = sqlite3.Row
    cases = {}
    current = json.loads(args.case_results.read_text(encoding="utf-8"))
    for row in current.get("results") or []:
        if row.get("case_id") in target_cases and row.get("caseHash"):
            cases[str(row["caseHash"])] = str(row["case_id"])
    placeholders = ",".join("?" for _ in cases)
    base = list(connection.execute(
        f"""SELECT r.*,m.end_us,s.case_id FROM case_results r JOIN markets m USING(market_id)
             JOIN case_specs s USING(case_hash) WHERE r.case_hash IN ({placeholders}) AND r.filled=1
             AND CAST(r.entry_vwap AS REAL) BETWEEN 0.80 AND 0.85
             ORDER BY r.execution_us""", tuple(cases)
    )) if cases else []
    targets = {str(row["market_id"]) for row in base}
    cross = load_cross(targets)
    analyzed = []
    for index, row in enumerate(base, 1):
        market_id, outcome = str(row["market_id"]), str(row["outcome"])
        opposite = "DOWN" if outcome == "UP" else "UP"
        execution_us, end_us = int(row["execution_us"]), int(row["end_us"])
        history = list(connection.execute(
            "SELECT source_us,best_ask,asks_json FROM snapshots WHERE market_id=? AND outcome=? AND source_us BETWEEN ? AND ? ORDER BY source_us",
            (market_id, outcome, execution_us - 30_000_000, execution_us),
        ))
        current_ask = last_value(history, execution_us)
        prior5, prior15, prior30 = (last_value(history, execution_us - seconds * 1_000_000) for seconds in (5, 15, 30))
        last15 = [float(item["best_ask"]) for item in history if item["best_ask"] is not None and int(item["source_us"]) >= execution_us - 15_000_000]
        last30 = [float(item["best_ask"]) for item in history if item["best_ask"] is not None]
        range15 = max(last15) - min(last15) if last15 else None
        range30 = max(last30) - min(last30) if last30 else None
        momentum5 = current_ask - prior5 if current_ask is not None and prior5 is not None else None
        momentum15 = current_ask - prior15 if current_ask is not None and prior15 is not None else None
        current_row = history[-1] if history else None
        depth85 = None
        if current_row:
            depth85 = sum(float(size) for price, size in json.loads(current_row["asks_json"]) if float(price) <= 0.85)
        opposite_row = connection.execute(
            "SELECT best_ask,asks_json FROM snapshots WHERE market_id=? AND outcome=? AND source_us<=? ORDER BY source_us DESC LIMIT 1",
            (market_id, opposite, execution_us),
        ).fetchone()
        derived_bid = 1 - float(opposite_row["best_ask"]) if opposite_row and opposite_row["best_ask"] is not None else None
        spread = current_ask - derived_bid if current_ask is not None and derived_bid is not None else None
        flows = {}
        for side_outcome in (outcome, opposite):
            flow = connection.execute(
                "SELECT COUNT(*),COALESCE(SUM(CAST(size AS REAL)),0) FROM trades WHERE market_id=? AND outcome=? AND side='BUY' AND source_us BETWEEN ? AND ?",
                (market_id, side_outcome, execution_us - 10_000_000, execution_us),
            ).fetchone()
            flows[side_outcome] = {"count": int(flow[0]), "volume": float(flow[1])}
        path_rows = list(connection.execute(
            "SELECT source_us,asks_json FROM snapshots WHERE market_id=? AND outcome=? AND source_us>? AND source_us<? ORDER BY source_us",
            (market_id, opposite, execution_us, end_us),
        ))
        entry_vwap = float(row["entry_vwap"])
        deltas = []
        reversal_us = None
        for path_row in path_rows:
            bid_vwap = consume_complement_bids(path_row["asks_json"])
            if bid_vwap is None:
                continue
            delta = float(bid_vwap) - entry_vwap
            deltas.append(delta)
            if reversal_us is None and delta <= -0.05:
                reversal_us = int(path_row["source_us"])
        cross_row = cross_before(cross.get(market_id, []), execution_us) or {}
        features, signals = cross_row.get("features") or {}, cross_row.get("signals") or {}
        spot, futures = features.get("binance") or {}, features.get("binanceFutures") or {}
        local_dt = datetime.fromtimestamp(execution_us / 1_000_000, timezone.utc).astimezone(LOCAL_TZ)
        remaining = float(row["remaining_seconds"])
        remaining_bucket = next((label for label, low, high in (("75–90秒",75,90),("60–75秒",60,75),("45–60秒",45,60),("30–45秒",30,45)) if low <= remaining <= high), "其他")
        analyzed.append({"marketId": market_id, "caseId": row["case_id"], "outcome": outcome, "winner": row["winner"],
                         "won": outcome == row["winner"], "executionUs": execution_us, "localTime": local_dt.isoformat(),
                         "localHour": f"{local_dt.hour:02d}:00–{(local_dt.hour+1)%24:02d}:00", "remainingSeconds": remaining,
                         "remainingBucket": remaining_bucket, "entryVwap": entry_vwap, "pnlPerShare": float(row["pnl_per_share"]),
                         "bestAsk": current_ask, "prior5Ask": prior5, "prior15Ask": prior15, "prior30Ask": prior30,
                         "momentum5": momentum5, "momentum15": momentum15, "momentumClass": classify_momentum(momentum5),
                         "range15": range15, "range30": range30, "rangeClass": classify_range(range15),
                         "depthAt85": depth85, "derivedBestBid": derived_bid, "spread": spread,
                         "sameBuyCount10s": flows[outcome]["count"], "sameBuyVolume10s": flows[outcome]["volume"],
                         "oppositeBuyCount10s": flows[opposite]["count"], "oppositeBuyVolume10s": flows[opposite]["volume"],
                         "mfe": max(deltas) if deltas else None, "mae": min(deltas) if deltas else None,
                         "fellFivePoints": reversal_us is not None, "secondsToFivePointFall": ((reversal_us-execution_us)/1e6 if reversal_us else None),
                         "spotSignal": signals.get("binance"), "futuresSignal": signals.get("binanceFutures"), "consensusSignal": signals.get("consensus"),
                         "spotReturn1s": spot.get("return1s"), "spotReturn5s": spot.get("return5s"), "spotReturn15s": spot.get("return15s"),
                         "futuresReturn1s": futures.get("return1s"), "futuresReturn5s": futures.get("return5s"), "crossSnapshotUs": cross_row.get("sourceTimeUs")})
        if index % 50 == 0:
            print(f"analyzed={index}/{len(base)}")
    connection.close()

    summaries = {
        "overall": summarize(analyzed, "全部"), "byOutcome": group_summary(analyzed, "outcome"),
        "byRemainingBucket": group_summary(analyzed, "remainingBucket"), "byLocalHour": group_summary(analyzed, "localHour"),
        "byRangeClass": group_summary(analyzed, "rangeClass"), "byMomentumClass": group_summary(analyzed, "momentumClass"),
        "winnersVsLosers": [summarize([row for row in analyzed if row["won"]], "胜"), summarize([row for row in analyzed if not row["won"]], "负")],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"schemaVersion": 1, "strategyCases": sorted(target_cases), "summaries": summaries, "rows": analyzed}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def table_rows(values: list[dict[str, Any]], min_n: int = 1) -> list[str]:
        result = []
        for item in values:
            if item["trades"] < min_n:
                continue
            result.append(f"| {item['label']} | {item['trades']} | {item['wins']} | {item['losses']} | {rate(item['winRate'])} | {f(item['avgPnlPerShare'],4)} | {f(item['avgRange15'])} | {f(item['avgMomentum5'])} | {f(item['avgMae'])} |")
        return result

    failures = [row for row in analyzed if not row["won"]]
    failures.sort(key=lambda row: row.get("mae") if row.get("mae") is not None else 99)
    lines = ["# 时段、平稳度与失败样本差异分析", "", "## 测试标准", "",
             "- 使用统一策略：UP/DOWN 分开，主动买入成交不少于10份，剩余90–30秒，实际10份VWAP在0.80–0.85，持有到官方结算。",
             f"- 当前可分析成交：{len(analyzed)}笔；来源于本地增量盘口缓存，不和351市场的分钟级历史数据混合。",
             "- 时区：小时分组使用北京时间（Asia/Shanghai）。", "- 平稳度：入场前15秒本方向卖一价格范围；小于0.02为平稳，0.02–0.05为一般，大于等于0.05为剧烈。",
             "- 短时动量：入场时卖一相对5秒前变化；上涨至少0.01、平稳、或回落至少0.01。", "- MFE/MAE：入场后按10份可执行卖出VWAP计算的最大有利/不利变化，不使用未来数据决定入场。", "",
             "## 总体与方向", "", "| 分组 | 笔数 | 胜 | 负 | 胜率 | 平均PnL/份 | 入场前15秒范围 | 5秒动量 | 入场后最大不利变化 |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
             *table_rows([summaries["overall"], *summaries["byOutcome"]]), "", "## 入场剩余时间", "", "| 时间段 | 笔数 | 胜 | 负 | 胜率 | 平均PnL/份 | 15秒范围 | 5秒动量 | MAE |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|", *table_rows(summaries["byRemainingBucket"], 8),
             "", "## 北京时间小时段", "", "只展示至少5笔的小时段，避免一两笔样本造成假规律。", "", "| 小时 | 笔数 | 胜 | 负 | 胜率 | 平均PnL/份 | 15秒范围 | 5秒动量 | MAE |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|", *table_rows(summaries["byLocalHour"], 5),
             "", "## 入场前平稳度", "", "| 平稳度 | 笔数 | 胜 | 负 | 胜率 | 平均PnL/份 | 15秒范围 | 5秒动量 | MAE |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|", *table_rows(summaries["byRangeClass"], 5),
             "", "## 入场前5秒动量", "", "| 动量 | 笔数 | 胜 | 负 | 胜率 | 平均PnL/份 | 15秒范围 | 5秒动量 | MAE |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|", *table_rows(summaries["byMomentumClass"], 5),
             "", "## 成功与失败的平均差异", "", "| 结果 | 笔数 | 胜 | 负 | 胜率 | 平均PnL/份 | 15秒范围 | 5秒动量 | MAE |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|", *table_rows(summaries["winnersVsLosers"]),
             "", "## 失败样本逐笔检查", "", "| 市场 | 方向 | 北京时间 | 剩余秒 | 入场VWAP | 15秒范围 | 5秒动量 | MFE | MAE | 跌5点耗时 | Spot/Futures |", "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|"]
    for row in failures:
        cross_text = f"{row.get('spotSignal') or '—'}/{row.get('futuresSignal') or '—'}"
        lines.append(f"| {row['marketId']} | {row['outcome']} | {row['localTime'][11:19]} | {row['remainingSeconds']:.1f} | {row['entryVwap']:.4f} | {f(row['range15'])} | {f(row['momentum5'])} | {f(row['mfe'])} | {f(row['mae'])} | {f(row['secondsToFivePointFall'],1)} | {cross_text} |")
    lines += ["", "## 解读边界", "", "- 小时差异可能只是当天行情方向和波动状态造成，至少要跨多个日期重复出现才可作为策略条件。",
              "- UP/DOWN 同时分析时要分方向看；当天BTC单边下跌会让DOWN看起来普遍更强。", "- 入场后路径特征用于解释失败，不能倒过来作为入场条件，否则会产生未来数据泄漏。",
              "- Spot/Futures字段缺失表示当时尚无可匹配快照，不等于反向。完整逐笔数据在同目录JSON中。",
              "", "## 可复现命令", "", "```bash", "PYTHONDONTWRITEBYTECODE=1 /Users/laochen/.local/bin/python3.12 \\", "  scripts/analysis/analyze_market_regimes.py", "```"]
    args.markdown.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"fills={len(analyzed)} losses={len(failures)} crossMarkets={len(cross)}")
    print(args.output.resolve())
    print(args.markdown.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
