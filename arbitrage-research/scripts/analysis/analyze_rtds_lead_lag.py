#!/usr/bin/env python3
"""Preliminary lead/lag analysis for BTC references versus Polymarket UP odds."""

from __future__ import annotations

import argparse
import gzip
import json
import math
from bisect import bisect_right
from pathlib import Path
from statistics import median
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[2]


def json_lines(paths: Iterable[Path]) -> Iterable[dict[str, Any]]:
    for path in paths:
        opener = gzip.open if path.suffix == ".gz" else open
        try:
            with opener(path, "rt", encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(value, dict):
                        yield value
        except (EOFError, gzip.BadGzipFile, OSError):
            # The active gzip stream may not have written its footer yet. All
            # complete lines yielded before that point remain usable.
            continue


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[round((len(ordered) - 1) * fraction)]


def correlation(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3 or len(xs) != len(ys):
        return None
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    covariance = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    variance_x = sum((x - mean_x) ** 2 for x in xs)
    variance_y = sum((y - mean_y) ** 2 for y in ys)
    denominator = math.sqrt(variance_x * variance_y)
    return covariance / denominator if denominator else None


def market_start(market_id: str) -> int | None:
    try:
        return int(market_id.rsplit("-", 1)[-1])
    except (TypeError, ValueError):
        return None


def up_mid(payload: dict[str, Any]) -> float | None:
    outcomes = {
        str(item.get("outcome")): item
        for item in payload.get("outcomes", [])
        if isinstance(item, dict)
    }
    up = outcomes.get("UP", {})
    down = outcomes.get("DOWN", {})
    bid = up.get("bestBid")
    ask = up.get("bestAsk")
    if bid is None and down.get("bestAsk") is not None:
        bid = 1 - float(down["bestAsk"])
    if ask is None and down.get("bestBid") is not None:
        ask = 1 - float(down["bestBid"])
    if bid is not None and ask is not None:
        return (float(bid) + float(ask)) / 2
    if bid is not None:
        return float(bid)
    if ask is not None:
        return float(ask)
    return None


def last_at_or_before(
    seconds: list[int], values: dict[int, float], second: int, max_age: int = 2
) -> float | None:
    position = bisect_right(seconds, second) - 1
    if position < 0 or second - seconds[position] > max_age:
        return None
    return values[seconds[position]]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since-us", type=int)
    parser.add_argument("--min-remaining", type=int, default=30)
    parser.add_argument("--max-remaining", type=int, default=270)
    parser.add_argument(
        "--move-threshold-bps", type=float, default=0.25,
        help="Only score source moves at least this large over one second.",
    )
    args = parser.parse_args()

    cross_dir = ROOT / "data" / "cross_market_validation"
    live_dir = ROOT / "data" / "live_monitor"
    if args.since_us is None:
        state = json.loads((cross_dir / "state" / "latest.json").read_text())
        args.since_us = int(state["service"]["startedAtUs"])

    latency_ms: dict[str, list[float]] = {
        "BINANCE": [], "BINANCE_FUTURES": [], "HYPERLIQUID": [],
        "POLYMARKET_BINANCE": [], "CHAINLINK": []
    }
    source_ticks: dict[str, dict[int, tuple[int, float]]] = {
        key: {} for key in latency_ms
    }
    lead_paths = sorted((cross_dir / "lead_lag").glob("date=*/hour=*/part-*.ndjson*"))
    for row in json_lines(lead_paths):
        received = int(row.get("receivedTimeUs") or 0)
        if received < args.since_us:
            continue
        source = str(row.get("source"))
        if source not in source_ticks:
            continue
        source_time = int(row.get("sourceTimeUs") or received)
        latency_ms[source].append((received - source_time) / 1_000)
        payload = row.get("payload") or {}
        price: float | None = None
        if source in {"BINANCE", "BINANCE_FUTURES", "HYPERLIQUID"} and row.get("kind") == "book":
            bid, ask = payload.get("bid"), payload.get("ask")
            if bid is not None and ask is not None:
                price = (float(bid) + float(ask)) / 2
        elif source == "HYPERLIQUID":
            candidate = payload.get("price") or payload.get("midPrice") or payload.get("markPrice")
            if candidate is not None:
                price = float(candidate)
        elif source != "BINANCE" and payload.get("price") is not None:
            price = float(payload["price"])
        if price is not None:
            second = received // 1_000_000
            previous = source_ticks[source].get(second)
            if previous is None or received >= previous[0]:
                source_ticks[source][second] = (received, price)

    poly_ticks: dict[str, dict[int, tuple[int, float]]] = {}
    event_paths = sorted((live_dir / "events").glob("date=*/hour=*/part-*.ndjson*"))
    for row in json_lines(event_paths):
        received = int(row.get("receivedTimeUs") or 0)
        if received < args.since_us or row.get("eventType") != "ORDERBOOK_SNAPSHOT":
            continue
        market_id = str(row.get("marketId") or "")
        midpoint = up_mid(row.get("payload") or {})
        if not market_id or midpoint is None:
            continue
        second = received // 1_000_000
        market = poly_ticks.setdefault(market_id, {})
        previous = market.get(second)
        if previous is None or received >= previous[0]:
            market[second] = (received, midpoint)

    print(f"sinceUs={args.since_us} markets={len(poly_ticks)}")
    for source in ("BINANCE", "BINANCE_FUTURES", "HYPERLIQUID", "POLYMARKET_BINANCE", "CHAINLINK"):
        values = latency_ms[source]
        print(
            f"latency {source}: n={len(values)} "
            f"p50={percentile(values, .50):.1f}ms "
            f"p90={percentile(values, .90):.1f}ms"
            if values else f"latency {source}: n=0"
        )

    for source in ("BINANCE", "BINANCE_FUTURES", "HYPERLIQUID", "POLYMARKET_BINANCE", "CHAINLINK"):
        values = {second: item[1] for second, item in source_ticks[source].items()}
        seconds = sorted(values)
        source_moves: dict[tuple[str, int], float] = {}
        poly_moves: dict[tuple[str, int], float] = {}
        for market_id, ticks in poly_ticks.items():
            start = market_start(market_id)
            if start is None:
                continue
            poly_values = {second: item[1] for second, item in ticks.items()}
            poly_seconds = sorted(poly_values)
            for second in range(start + 1, start + 300):
                remaining = start + 300 - second
                if not args.min_remaining <= remaining <= args.max_remaining:
                    continue
                current_poly = last_at_or_before(poly_seconds, poly_values, second)
                previous_poly = last_at_or_before(poly_seconds, poly_values, second - 1)
                current_source = last_at_or_before(seconds, values, second)
                previous_source = last_at_or_before(seconds, values, second - 1)
                if None in (current_poly, previous_poly, current_source, previous_source):
                    continue
                if float(current_source) <= 0 or float(previous_source) <= 0:
                    continue
                key = (market_id, second)
                poly_moves[key] = float(current_poly) - float(previous_poly)
                source_moves[key] = math.log(float(current_source) / float(previous_source)) * 10_000

        rows: list[tuple[int, int, float | None]] = []
        for lag in range(-5, 6):
            xs: list[float] = []
            ys: list[float] = []
            for (market_id, second), poly_move in poly_moves.items():
                source_move = source_moves.get((market_id, second - lag))
                if source_move is not None:
                    xs.append(source_move)
                    ys.append(poly_move)
            rows.append((lag, len(xs), correlation(xs, ys)))
        valid = [row for row in rows if row[2] is not None]
        best = max(valid, key=lambda row: row[2]) if valid else None
        formatted = " ".join(
            f"{lag:+d}s:{corr:+.3f}" for lag, _, corr in rows if corr is not None
        )
        print(f"correlation {source}: {formatted}")
        if best:
            print(
                f"best {source}: lag={best[0]:+d}s n={best[1]} corr={best[2]:+.3f} "
                "(positive lag means source leads Polymarket)"
            )
        for horizon in (1, 2, 3, 5):
            correct = 0
            scored = 0
            future_moves: list[float] = []
            for market_id, ticks in poly_ticks.items():
                start = market_start(market_id)
                if start is None:
                    continue
                poly_values = {second: item[1] for second, item in ticks.items()}
                poly_seconds = sorted(poly_values)
                for second in range(start + 1, start + 300 - horizon):
                    remaining = start + 300 - second
                    if not args.min_remaining <= remaining <= args.max_remaining:
                        continue
                    source_now = last_at_or_before(seconds, values, second)
                    source_before = last_at_or_before(seconds, values, second - 1)
                    poly_now = last_at_or_before(poly_seconds, poly_values, second)
                    poly_future = last_at_or_before(
                        poly_seconds, poly_values, second + horizon
                    )
                    if None in (source_now, source_before, poly_now, poly_future):
                        continue
                    if float(source_now) <= 0 or float(source_before) <= 0:
                        continue
                    move_bps = math.log(float(source_now) / float(source_before)) * 10_000
                    if abs(move_bps) < args.move_threshold_bps:
                        continue
                    future_move = float(poly_future) - float(poly_now)
                    if future_move == 0:
                        continue
                    scored += 1
                    future_moves.append(future_move)
                    if (move_bps > 0) == (future_move > 0):
                        correct += 1
            accuracy = correct / scored if scored else None
            average_move = sum(future_moves) / len(future_moves) if future_moves else None
            print(
                f"forecast {source} +{horizon}s: n={scored} "
                f"directionAccuracy={accuracy:.3f} avgPolyMove={average_move:+.5f}"
                if accuracy is not None and average_move is not None
                else f"forecast {source} +{horizon}s: n=0"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
