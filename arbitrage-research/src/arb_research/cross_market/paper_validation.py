from __future__ import annotations

from bisect import bisect_right
import csv
from datetime import datetime, timezone
from decimal import Decimal
import gzip
import json
from pathlib import Path
from typing import Any, Iterable


def _read_ndjson(paths: Iterable[Path]) -> Iterable[dict[str, Any]]:
    for path in paths:
        handle = gzip.open(path, "rt", encoding="utf-8") if path.suffix == ".gz" else path.open(encoding="utf-8")
        with handle:
            while True:
                try:
                    line = handle.readline()
                except EOFError:
                    # The current hourly gzip may still be open in the live collector.
                    break
                if not line:
                    break
                if line.strip():
                    yield json.loads(line)


def _paper_events(data_dir: Path) -> Iterable[dict[str, Any]]:
    return _read_ndjson(sorted((data_dir / "paper").glob("date=*/hour=*/part-*.ndjson")))


def _snapshot_events(data_dir: Path) -> Iterable[dict[str, Any]]:
    return _read_ndjson(sorted((data_dir / "venue_snapshots").glob("date=*/hour=*/part-*.ndjson*")))


def build_paper_cross_rows(
    paper_data_dir: Path,
    cross_data_dir: Path,
    *,
    max_snapshot_age_ms: float = 3_000,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    fills: dict[tuple[str, str], dict[str, Any]] = {}
    settlements: dict[tuple[str, str], dict[str, Any]] = {}
    for event in _paper_events(paper_data_dir):
        key = (str(event.get("marketId", "")), str(event.get("outcome", "")).upper())
        if event.get("eventType") == "PAPER_FILL":
            fills.setdefault(key, event)
        elif event.get("eventType") == "PAPER_SETTLEMENT":
            settlements[key] = event

    complete_keys = {key for key in fills if key in settlements}
    snapshots: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    wanted_markets = {key[0] for key in complete_keys}
    for snapshot in _snapshot_events(cross_data_dir):
        market_id = str(snapshot.get("marketId", ""))
        if market_id not in wanted_markets:
            continue
        timestamp_us = int(snapshot.get("receivedTimeUs") or snapshot.get("sourceTimeUs") or 0)
        snapshots.setdefault(market_id, []).append((timestamp_us, snapshot))
    for values in snapshots.values():
        values.sort(key=lambda item: item[0])

    counters = {
        "paperFills": len(fills),
        "settledPaperFills": len(complete_keys),
        "matched": 0,
        "unmatchedNoSnapshot": 0,
        "unmatchedStaleSnapshot": 0,
    }
    output: list[dict[str, Any]] = []
    for key in sorted(complete_keys, key=lambda item: int(fills[item]["receivedTimeUs"])):
        fill = fills[key]
        settlement = settlements[key]
        fill_us = int(fill["receivedTimeUs"])
        candidates = snapshots.get(key[0]) or []
        timestamps = [item[0] for item in candidates]
        index = bisect_right(timestamps, fill_us) - 1
        if index < 0:
            counters["unmatchedNoSnapshot"] += 1
            continue
        snapshot_us, snapshot = candidates[index]
        age_ms = (fill_us - snapshot_us) / 1_000
        if age_ms > max_snapshot_age_ms:
            counters["unmatchedStaleSnapshot"] += 1
            continue

        outcome = key[1]
        source_signals = dict(snapshot.get("signals") or {})
        directional = {
            source: str(source_signals.get(source, "MISSING")).upper()
            for source in ("binance", "hyperliquid", "deribit")
        }
        available = [signal for signal in directional.values() if signal in {"UP", "DOWN"}]
        agreements = {source: signal == outcome for source, signal in directional.items()}
        agreement_count = sum(agreements.values())
        majority = (
            "UP" if available.count("UP") >= 2 else
            "DOWN" if available.count("DOWN") >= 2 else "NEUTRAL"
        )
        fill_payload = fill.get("payload") or {}
        settle_payload = settlement.get("payload") or {}
        quantity = Decimal(str(settle_payload.get("quantity") or fill_payload.get("quantity") or 0))
        net_pnl = Decimal(str(settle_payload.get("netPnl") or 0))
        row: dict[str, Any] = {
            "marketId": key[0], "outcome": outcome,
            "fillReceivedTimeUs": str(fill_us), "snapshotReceivedTimeUs": str(snapshot_us),
            "snapshotAgeMs": age_ms,
            "remainingSeconds": fill_payload.get("remainingSeconds"),
            "entryVwap": fill_payload.get("entryVwap"),
            "signalTradePrice": fill_payload.get("signalTradePrice"),
            "signalTradeSize": fill_payload.get("signalTradeSize"),
            "quantity": str(quantity), "won": bool(settle_payload.get("won")),
            "winnerOutcome": settle_payload.get("winnerOutcome"),
            "netPnl": str(net_pnl),
            "netPnlPerShare": str(net_pnl / quantity) if quantity else None,
            "binanceSignal": directional["binance"],
            "hyperliquidSignal": directional["hyperliquid"],
            "deribitSignal": directional["deribit"],
            "binanceAgrees": agreements["binance"],
            "hyperliquidAgrees": agreements["hyperliquid"],
            "deribitAgrees": agreements["deribit"],
            "availableSourceCount": len(available),
            "agreementCount": agreement_count,
            "majoritySignal": majority,
            "majorityAgrees": majority == outcome,
            "allThreeAgree": len(available) == 3 and agreement_count == 3,
            "allThreeAvailable": len(available) == 3,
        }
        for source in ("binance", "hyperliquid", "deribit"):
            features = (snapshot.get("features") or {}).get(source) or {}
            for name in (
                "returnFromStart", "return15s", "return30s", "return60s",
                "tradeImbalance60s", "bookImbalance", "sourceAgeMs",
                "markOracleBasis", "binanceBasis", "openInterest", "funding",
                "perpetualBasis", "funding8h", "atmIv", "riskReversalProxy", "dvol",
                "dvolChange15s", "dvolChange30s", "dvolChange60s", "dvolChange120s",
                "atmIvChange60s", "rr25", "rr25Change60s", "butterfly25",
                "call25IvChange60s", "put25IvChange60s",
                "call25Delta", "put25Delta", "call25Strike", "put25Strike",
                "call25Oi", "put25Oi", "call25OiChange60s", "put25OiChange60s",
                "call25OiReturn60s", "put25OiReturn60s", "oiSkew25",
                "selectedExpiryOi", "selectedExpiryOiChange60s",
                "selectedExpiryOiReturn60s", "optionTradeCount60s",
                "optionCallBuyPremiumUsd60s", "optionCallSellPremiumUsd60s",
                "optionPutBuyPremiumUsd60s", "optionPutSellPremiumUsd60s",
                "optionBullishPremiumUsd60s", "optionBearishPremiumUsd60s",
                "optionFlowImbalance60s", "rr25SelectedTradeCount60s",
                "rr25SelectedFlowImbalance60s",
            ):
                if name in features:
                    row[f"{source}.{name}"] = features[name]
        output.append(row)
        counters["matched"] += 1
    return output, counters


def _cohort(rows: list[dict[str, Any]], name: str, predicate: Any) -> dict[str, Any]:
    selected = [row for row in rows if predicate(row)]
    wins = sum(bool(row["won"]) for row in selected)
    pnl = sum((Decimal(str(row["netPnl"])) for row in selected), Decimal("0"))
    per_share = sum(
        (Decimal(str(row["netPnlPerShare"])) for row in selected), Decimal("0")
    )
    return {
        "cohort": name, "trades": len(selected), "wins": wins,
        "losses": len(selected) - wins,
        "winRate": wins / len(selected) if selected else None,
        "totalNetPnl": str(pnl),
        "averageNetPnlPerShare": str(per_share / len(selected)) if selected else None,
        "upTrades": sum(row["outcome"] == "UP" for row in selected),
        "downTrades": sum(row["outcome"] == "DOWN" for row in selected),
    }


def evaluate_paper_cross_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def number(row: dict[str, Any], key: str) -> float | None:
        try:
            return float(row.get(key))
        except (TypeError, ValueError):
            return None

    def sign_agrees(row: dict[str, Any], key: str) -> bool:
        value = number(row, key)
        return value is not None and (
            (value > 0 and row["outcome"] == "UP")
            or (value < 0 and row["outcome"] == "DOWN")
        )

    def oi_iv_flow_confirms(row: dict[str, Any]) -> bool:
        if not sign_agrees(row, "deribit.optionFlowImbalance60s"):
            return False
        prefix = "call25" if row["outcome"] == "UP" else "put25"
        oi_change = number(row, f"deribit.{prefix}OiChange60s")
        iv_change = number(row, f"deribit.{prefix}IvChange60s")
        return (
            oi_change is not None and oi_change > 0
            and iv_change is not None and iv_change > 0
        )

    return [
        _cohort(rows, "原纸上策略（已匹配样本）", lambda row: True),
        _cohort(rows, "Binance方向同意", lambda row: row["binanceAgrees"]),
        _cohort(rows, "Hyperliquid方向同意", lambda row: row["hyperliquidAgrees"]),
        _cohort(rows, "Deribit方向同意", lambda row: row["deribitAgrees"]),
        _cohort(rows, "至少两家方向同意", lambda row: row["agreementCount"] >= 2),
        _cohort(rows, "三家方向全部同意", lambda row: row["allThreeAgree"]),
        _cohort(
            rows, "三家齐全且多数方向反对",
            lambda row: row["allThreeAvailable"] and not row["majorityAgrees"],
        ),
        _cohort(
            rows, "DVOL近60秒上升（波动风险组）",
            lambda row: (number(row, "deribit.dvolChange60s") or 0) > 0,
        ),
        _cohort(
            rows, "DVOL近60秒不升（波动稳定组）",
            lambda row: number(row, "deribit.dvolChange60s") is not None
            and number(row, "deribit.dvolChange60s") <= 0,
        ),
        _cohort(rows, "RR25水平同意入场方向", lambda row: sign_agrees(row, "deribit.rr25")),
        _cohort(
            rows, "RR25近60秒变化同意入场方向",
            lambda row: sign_agrees(row, "deribit.rr25Change60s"),
        ),
        _cohort(
            rows, "期权主动资金流同意入场方向",
            lambda row: sign_agrees(row, "deribit.optionFlowImbalance60s"),
        ),
        _cohort(rows, "资金流+对应25D OI与IV同升确认", oi_iv_flow_confirms),
    ]


def write_paper_cross_report(
    paper_data_dir: Path, cross_data_dir: Path, output_dir: Path,
    *, max_snapshot_age_ms: float = 3_000,
) -> tuple[Path, Path, Path]:
    rows, counters = build_paper_cross_rows(
        paper_data_dir, cross_data_dir, max_snapshot_age_ms=max_snapshot_age_ms
    )
    cohorts = evaluate_paper_cross_rows(rows)
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "paper_cross_market_rows.csv"
    fields = sorted({key for row in rows for key in row})
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        if fields:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
    json_path = output_dir / "paper_cross_market_results.json"
    json_path.write_text(json.dumps({
        "schemaVersion": 1, "generatedAt": datetime.now(timezone.utc).isoformat(),
        "maxSnapshotAgeMs": max_snapshot_age_ms, "counters": counters, "cohorts": cohorts,
    }, ensure_ascii=False, indent=2) + "\n")

    def percent(value: Any) -> str:
        return "—" if value is None else f"{float(value):.2%}"
    report_path = output_dir / "纸上交易与跨市场指标交叉验证.md"
    lines = [
        "# 纸上交易与跨市场指标交叉验证", "",
        f"- 已结算纸上成交：{counters['settledPaperFills']}",
        f"- 成功匹配入场前快照：{counters['matched']}",
        f"- 入场前无快照：{counters['unmatchedNoSnapshot']}",
        f"- 快照超过 {max_snapshot_age_ms:.0f}ms：{counters['unmatchedStaleSnapshot']}", "",
        "| 过滤条件 | 交易 | 胜 | 负 | 胜率 | 总净收益 | 平均每份净收益 | UP/DOWN |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in cohorts:
        lines.append(
            f"| {item['cohort']} | {item['trades']} | {item['wins']} | {item['losses']} | "
            f"{percent(item['winRate'])} | {item['totalNetPnl']} | "
            f"{item['averageNetPnlPerShare'] or '—'} | {item['upTrades']}/{item['downTrades']} |"
        )
    lines.extend([
        "", "## 防止未来数据泄漏", "",
        "每笔纸上成交只匹配本地接收时间不晚于入场时间的最后一份跨市场快照；",
        "默认快照最大年龄为3秒。新采集启动前发生的旧纸上成交保持未匹配，不用分钟历史数据冒充实时值。", "",
    ])
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return csv_path, json_path, report_path
