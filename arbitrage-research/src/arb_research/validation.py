from __future__ import annotations

import csv
import json
import math
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Iterable

from .backtest import run_backtest
from .models import MarketDefinition, MarketEvent
from .strategy import LastWindowThresholdConfig, LastWindowThresholdStrategy


QUOTE_EVENT_TYPES = {"PRICE_QUOTE", "ORDERBOOK_SNAPSHOT", "ORDERBOOK_DELTA"}
OUTCOMES = ("ALL", "UP", "DOWN")
BUCKET_EPSILON = Decimal("0.000000001")


def build_last_window_report(
    events: Iterable[MarketEvent],
    *,
    windows_seconds: tuple[int, ...] = (120, 180),
    price_min: Decimal = Decimal("0.70"),
    price_max: Decimal = Decimal("0.88"),
    bucket_size: Decimal = Decimal("0.01"),
    order_size: Decimal = Decimal("1"),
    min_ask_size: Decimal = Decimal("1"),
    fee_model: str = "none",
    fee_rate: Decimal = Decimal("0"),
    max_quote_gap_seconds: Decimal = Decimal("5"),
    price_source: str = "best_ask",
    min_remaining_seconds: int = 0,
) -> dict[str, object]:
    """Build a reproducible final-window win-rate report.

    Each row represents the first eligible taker entry per market for the
    specified window, direction, and price range. Exact-price buckets are
    represented as half-open intervals, except the last bucket which includes
    ``price_max``.
    """
    raw_event_list = sorted(
        events,
        key=lambda item: (item.source_time_us, item.received_time_us, item.sequence or ""),
    )
    if price_source not in {"best_ask", "trade"}:
        raise ValueError("price_source must be 'best_ask' or 'trade'")
    event_list = (
        _trade_events_as_quotes(raw_event_list)
        if price_source == "trade"
        else raw_event_list
    )
    _validate_arguments(
        windows_seconds,
        price_min,
        price_max,
        bucket_size,
        max_quote_gap_seconds,
        min_remaining_seconds,
    )

    definitions: dict[str, MarketDefinition] = {}
    trade_history_complete: dict[str, bool] = {}
    settlements: dict[str, str] = {}
    quote_times: dict[str, set[int]] = defaultdict(set)
    for event in event_list:
        if event.event_type == "MARKET_DEFINITION":
            definitions[event.market_id] = MarketDefinition.from_event(event)
            trade_history_complete[event.market_id] = (
                event.payload.get("tradeHistoryComplete") is True
            )
        elif event.event_type == "SETTLEMENT" and event.payload.get("winnerOutcome") is not None:
            settlements[event.market_id] = str(event.payload["winnerOutcome"])
        elif event.event_type in QUOTE_EVENT_TYPES:
            quote_times[event.market_id].add(event.source_time_us)

    eligible_market_ids = sorted(set(definitions) & set(settlements))
    if price_source == "trade":
        coverage_rows = _build_trade_coverage_rows(
            definitions,
            settlements,
            quote_times,
            windows_seconds,
            trade_history_complete,
            min_remaining_seconds,
        )
    else:
        coverage_rows = _build_coverage_rows(
            definitions,
            settlements,
            quote_times,
            windows_seconds,
            max_quote_gap_seconds,
            min_remaining_seconds,
        )
    complete_by_window = {
        window: sum(
            1
            for row in coverage_rows
            if row["windowSeconds"] == window and row["complete"]
        )
        for window in windows_seconds
    }
    complete_market_ids_by_window = {
        window: {
            str(row["marketId"])
            for row in coverage_rows
            if row["windowSeconds"] == window and row["complete"]
        }
        for window in windows_seconds
    }
    dataset_complete = bool(eligible_market_ids) and all(
        complete_by_window[window] == len(eligible_market_ids)
        for window in windows_seconds
    ) and len(eligible_market_ids) == len(definitions)
    analysis_complete = all(complete_market_ids_by_window[window] for window in windows_seconds)

    result_rows: list[dict[str, object]] = []
    price_ranges = [("RANGE", f"{price_min}-{price_max}", price_min, price_max)]
    price_ranges.extend(_price_buckets(price_min, price_max, bucket_size))

    for window in windows_seconds:
        analysis_market_ids = complete_market_ids_by_window[window]
        window_events = [
            event for event in event_list
            if event.market_id in analysis_market_ids
        ]
        for bucket_type, bucket_label, lower, upper in price_ranges:
            for outcome in OUTCOMES:
                allowed_outcomes = ("UP", "DOWN") if outcome == "ALL" else (outcome,)
                config = LastWindowThresholdConfig(
                    strategy_id="prediction_last_window_threshold_validation",
                    version=2,
                    window_seconds=window,
                    threshold_price=lower,
                    max_entry_price=upper,
                    order_size=order_size,
                    min_ask_size=min_ask_size,
                    fee_model=fee_model,
                    fee_rate=fee_rate,
                    min_remaining_seconds=min_remaining_seconds,
                    allowed_outcomes=allowed_outcomes,
                    one_trade_per_market=True,
                )
                backtest_result = run_backtest(
                    window_events,
                    LastWindowThresholdStrategy(config),
                    events_are_sorted=True,
                )
                metrics = backtest_result.metrics
                settled_count = int(metrics["settledFillCount"])
                wins = int(metrics["winCount"])
                ci_low, ci_high = _wilson_interval(wins, settled_count)
                denominator = len(analysis_market_ids)
                baseline_win_rate = None
                win_rate_lift = None
                if outcome != "ALL" and denominator:
                    baseline_wins = sum(
                        settlements[market_id] == outcome
                        for market_id in analysis_market_ids
                    )
                    baseline_win_rate = _ratio(baseline_wins, denominator)
                    if settled_count:
                        win_rate_lift = _fmt(
                            Decimal(str(metrics["winRate"]))
                            - Decimal(baseline_win_rate)
                        )
                result_rows.append({
                    "windowSeconds": window,
                    "bucketType": bucket_type,
                    "bucket": bucket_label,
                    "priceMin": _fmt(lower),
                    "priceMax": _fmt(upper),
                    "outcome": outcome,
                    "eligibleSettledMarketCount": denominator,
                    "completeCoverageMarketCount": complete_by_window[window],
                    "settledTradeCount": settled_count,
                    "noSignalMarketCount": max(denominator - settled_count, 0),
                    "winCount": wins,
                    "lossCount": settled_count - wins,
                    "winRate": metrics["winRate"],
                    "winRateCi95Low": ci_low,
                    "winRateCi95High": ci_high,
                    "baselineOutcomeWinRate": baseline_win_rate,
                    "winRateLiftVsUnconditional": win_rate_lift,
                    "opportunityRate": _ratio(settled_count, denominator),
                    "averageEntryPrice": metrics["averageEntryPrice"],
                    "breakevenWinRate": metrics["breakevenWinRate"],
                    "totalNetPnl": metrics["totalNetPnl"],
                    "netPnlPerShare": metrics["netPnlPerShare"],
                    "unsettledFillCount": metrics["unsettledFillCount"],
                    "provisional": not analysis_complete,
                })

    warnings: list[str] = []
    if not eligible_market_ids:
        warnings.append("No markets contain both MARKET_DEFINITION and SETTLEMENT events.")
    if len(eligible_market_ids) != len(definitions):
        warnings.append("Some defined markets are missing settlement events.")
    for window in windows_seconds:
        missing = len(eligible_market_ids) - complete_by_window[window]
        if missing:
            warnings.append(
                f"Excluded {missing}/{len(eligible_market_ids)} settled markets with incomplete "
                f"quote coverage in the final {window} seconds."
            )

    return {
        "schemaVersion": 1,
        "reportType": "prediction_last_window_win_rate",
        "datasetComplete": dataset_complete,
        "analysisComplete": analysis_complete,
        "provisional": not analysis_complete,
        "warnings": warnings,
        "assumptions": {
            "entryPrice": (
                "historical taker BUY trade price; not a continuous quote"
                if price_source == "trade"
                else "best ask (taker buy)"
            ),
            "entryRule": (
                "first eligible refreshed quote per market between "
                f"window start and {min_remaining_seconds} seconds remaining"
            ),
            "rangeBounds": "inclusive for RANGE; exact buckets are half-open except final bucket",
            "tradeLimit": "one trade per market for each independently evaluated report row",
            "coverageSelection": "only markets passing the quote coverage rule enter result rows",
            "settlementRule": "hold to settlement",
            "dataQuality": "L2 trade+settlement" if price_source == "trade" else "L3 quote+settlement",
            "quoteCoverageRule": (
                "trade-history request completed; sparse/no trades means no observed opportunity"
                if price_source == "trade"
                else "maximum gap from window start through market end must not exceed "
                f"{_fmt(max_quote_gap_seconds)} seconds"
            ),
        },
        "parameters": {
            "windowsSeconds": list(windows_seconds),
            "priceMin": _fmt(price_min),
            "priceMax": _fmt(price_max),
            "bucketSize": _fmt(bucket_size),
            "orderSize": _fmt(order_size),
            "minAskSize": _fmt(min_ask_size),
            "feeModel": fee_model,
            "feeRate": _fmt(fee_rate),
            "maxQuoteGapSeconds": _fmt(max_quote_gap_seconds),
            "priceSource": price_source,
            "minRemainingSeconds": min_remaining_seconds,
        },
        "dataset": {
            "eventCount": len(raw_event_list),
            "definedMarketCount": len(definitions),
            "settledMarketCount": len(eligible_market_ids),
            "unsettledMarketCount": len(set(definitions) - set(settlements)),
            "winnerDistribution": {
                outcome: sum(winner == outcome for winner in settlements.values())
                for outcome in ("UP", "DOWN")
            },
            "analysisMarketCountByWindow": {
                str(window): len(complete_market_ids_by_window[window])
                for window in windows_seconds
            },
        },
        "coverage": coverage_rows,
        "results": result_rows,
    }


def write_last_window_report(output_dir: Path, report: dict[str, object]) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "last_window_report.json"
    csv_path = output_dir / "last_window_results.csv"

    with json_path.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")

    rows = report.get("results") or []
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        if rows:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

    return json_path, csv_path


def _build_coverage_rows(
    definitions: dict[str, MarketDefinition],
    settlements: dict[str, str],
    quote_times: dict[str, set[int]],
    windows_seconds: tuple[int, ...],
    max_quote_gap_seconds: Decimal,
    min_remaining_seconds: int,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for market_id in sorted(definitions):
        definition = definitions[market_id]
        for window in windows_seconds:
            window_start_us = definition.event_end_us - window * 1_000_000
            window_end_us = definition.event_end_us - min_remaining_seconds * 1_000_000
            inside = sorted(
                timestamp
                for timestamp in quote_times.get(market_id, set())
                if window_start_us <= timestamp <= window_end_us
            )
            if not inside:
                rows.append({
                    "marketId": market_id,
                    "windowSeconds": window,
                    "settled": market_id in settlements,
                    "quoteTimestampCount": 0,
                    "firstQuoteRemainingSeconds": None,
                    "lastQuoteRemainingSeconds": None,
                    "maxQuoteGapSeconds": None,
                    "complete": False,
                })
                continue

            gaps_us = [inside[0] - window_start_us]
            gaps_us.extend(right - left for left, right in zip(inside, inside[1:]))
            gaps_us.append(window_end_us - inside[-1])
            max_gap = Decimal(max(gaps_us)) / Decimal("1000000")
            first_remaining = Decimal(definition.event_end_us - inside[0]) / Decimal("1000000")
            last_remaining = Decimal(definition.event_end_us - inside[-1]) / Decimal("1000000")
            rows.append({
                "marketId": market_id,
                "windowSeconds": window,
                "settled": market_id in settlements,
                "quoteTimestampCount": len(inside),
                "firstQuoteRemainingSeconds": _fmt(first_remaining),
                "lastQuoteRemainingSeconds": _fmt(last_remaining),
                "maxQuoteGapSeconds": _fmt(max_gap),
                "complete": market_id in settlements and max_gap <= max_quote_gap_seconds,
            })
    return rows


def _build_trade_coverage_rows(
    definitions: dict[str, MarketDefinition],
    settlements: dict[str, str],
    quote_times: dict[str, set[int]],
    windows_seconds: tuple[int, ...],
    trade_history_complete: dict[str, bool],
    min_remaining_seconds: int,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for market_id in sorted(definitions):
        definition = definitions[market_id]
        for window in windows_seconds:
            window_start_us = definition.event_end_us - window * 1_000_000
            window_end_us = definition.event_end_us - min_remaining_seconds * 1_000_000
            inside = [
                timestamp
                for timestamp in quote_times.get(market_id, set())
                if window_start_us <= timestamp <= window_end_us
            ]
            rows.append({
                "marketId": market_id,
                "windowSeconds": window,
                "settled": market_id in settlements,
                "quoteTimestampCount": len(inside),
                "firstQuoteRemainingSeconds": None,
                "lastQuoteRemainingSeconds": None,
                "maxQuoteGapSeconds": None,
                "complete": (
                    market_id in settlements
                    and trade_history_complete.get(market_id, False)
                ),
                "coverageMethod": "complete_trade_history_request",
            })
    return rows


def _trade_events_as_quotes(events: list[MarketEvent]) -> list[MarketEvent]:
    converted: list[MarketEvent] = []
    for event in events:
        if event.event_type != "TRADE":
            converted.append(event)
            continue
        if str(event.payload.get("side", "")).upper() != "BUY":
            continue
        price = event.payload.get("price")
        size = event.payload.get("size")
        outcome = event.outcome or event.payload.get("outcome")
        if price is None or size is None or outcome is None:
            continue
        converted.append(MarketEvent(
            schema_version=event.schema_version,
            event_type="PRICE_QUOTE",
            venue=event.venue,
            market_id=event.market_id,
            source_time_us=event.source_time_us,
            received_time_us=event.received_time_us,
            payload={
                "outcome": str(outcome),
                "bestAsk": str(price),
                "bestAskSize": str(size),
            },
            event_id=event.event_id,
            instrument_id=event.instrument_id,
            outcome=str(outcome),
            sequence=event.sequence,
            source=event.source,
        ))
    return converted


def _price_buckets(
    price_min: Decimal,
    price_max: Decimal,
    bucket_size: Decimal,
) -> list[tuple[str, str, Decimal, Decimal]]:
    buckets: list[tuple[str, str, Decimal, Decimal]] = []
    lower = price_min
    while lower <= price_max:
        next_lower = lower + bucket_size
        if next_lower > price_max:
            upper = price_max
            label = f"{_fmt(lower)}-{_fmt(upper)}"
        else:
            upper = next_lower - BUCKET_EPSILON
            label = f"{_fmt(lower)}-<{_fmt(next_lower)}"
        buckets.append(("PRICE_BUCKET", label, lower, upper))
        lower = next_lower
    return buckets


def _wilson_interval(wins: int, total: int) -> tuple[str | None, str | None]:
    if total == 0:
        return None, None
    z = 1.959963984540054
    proportion = wins / total
    denominator = 1 + z * z / total
    center = (proportion + z * z / (2 * total)) / denominator
    half_width = (
        z
        * math.sqrt(proportion * (1 - proportion) / total + z * z / (4 * total * total))
        / denominator
    )
    return f"{max(0.0, center - half_width):.6f}", f"{min(1.0, center + half_width):.6f}"


def _ratio(numerator: int, denominator: int) -> str:
    if not denominator:
        return "0"
    return _fmt(Decimal(numerator) / Decimal(denominator))


def _fmt(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _validate_arguments(
    windows_seconds: tuple[int, ...],
    price_min: Decimal,
    price_max: Decimal,
    bucket_size: Decimal,
    max_quote_gap_seconds: Decimal,
    min_remaining_seconds: int,
) -> None:
    if not windows_seconds or any(window <= 0 for window in windows_seconds):
        raise ValueError("windows_seconds must contain positive values")
    if price_min < 0 or price_max > 1 or price_min > price_max:
        raise ValueError("price range must satisfy 0 <= price_min <= price_max <= 1")
    if bucket_size <= 0:
        raise ValueError("bucket_size must be positive")
    if max_quote_gap_seconds < 0:
        raise ValueError("max_quote_gap_seconds must be non-negative")
    if min_remaining_seconds < 0 or any(
        min_remaining_seconds >= window for window in windows_seconds
    ):
        raise ValueError("min_remaining_seconds must satisfy 0 <= value < every window")
