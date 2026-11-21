from __future__ import annotations

from collections.abc import Iterable, Sequence
from decimal import Decimal
import gzip
import json
from pathlib import Path
import sqlite3
from typing import Any
import zlib

import numpy as np

from .orderbook_depth import BookLevel, consume_asks


def read_partition_rows(paths: Iterable[Path]) -> Iterable[dict[str, Any]]:
    """Yield complete JSON rows while tolerating a damaged active gzip tail."""
    for path in paths:
        opener = gzip.open if path.suffix == ".gz" else open
        try:
            with opener(path, "rt", encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue
        except (EOFError, OSError, zlib.error):
            continue


def time_weighted_mean(
    *,
    start_us: int,
    observation_us: int,
    anchor_price: float,
    samples: Iterable[tuple[int, float]],
) -> float:
    """Return a causal, piecewise-constant mean up to ``observation_us``."""
    if observation_us <= start_us:
        raise ValueError("observation_us must be after start_us")
    if anchor_price <= 0:
        raise ValueError("anchor_price must be positive")

    current_us = start_us
    current_price = float(anchor_price)
    weighted_sum = 0.0
    for sample_us, sample_price in sorted(samples):
        if sample_us <= start_us:
            current_price = float(sample_price)
            continue
        if sample_us >= observation_us:
            break
        weighted_sum += (sample_us - current_us) * current_price
        current_us = sample_us
        current_price = float(sample_price)
    weighted_sum += (observation_us - current_us) * current_price
    return weighted_sum / (observation_us - start_us)


def required_remaining_average(
    *,
    anchor_price: float,
    elapsed_twap: float,
    elapsed_seconds: float,
    total_seconds: float = 300.0,
) -> float:
    if not 0 < elapsed_seconds < total_seconds:
        raise ValueError("elapsed_seconds must be inside the full interval")
    remaining_seconds = total_seconds - elapsed_seconds
    return (
        total_seconds * anchor_price - elapsed_seconds * elapsed_twap
    ) / remaining_seconds


def chronological_market_split(
    rows: Sequence[dict[str, Any]], *, train_fraction: float = 0.7
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not 0 < train_fraction < 1:
        raise ValueError("train_fraction must be between zero and one")
    ordered_markets = sorted(
        {
            (str(row["marketId"]), int(row["marketStartUs"]))
            for row in rows
        },
        key=lambda item: (item[1], item[0]),
    )
    if len(ordered_markets) < 2:
        raise ValueError("at least two markets are required")
    train_count = min(
        len(ordered_markets) - 1,
        max(1, int(len(ordered_markets) * train_fraction)),
    )
    train_ids = {market_id for market_id, _ in ordered_markets[:train_count]}
    train = [row for row in rows if str(row["marketId"]) in train_ids]
    validation = [row for row in rows if str(row["marketId"]) not in train_ids]
    return train, validation


def complement_mid_probability(*, up_ask: float, down_ask: float) -> float:
    if not 0 <= up_ask <= 1 or not 0 <= down_ask <= 1:
        raise ValueError("asks must be probabilities between zero and one")
    return min(1.0, max(0.0, (up_ask + (1.0 - down_ask)) / 2.0))


def e0_feature_values(
    *,
    anchor_price: float,
    current_price: float,
    elapsed_twap: float,
    elapsed_seconds: float,
    realized_vol_bps: float,
    total_seconds: float = 300.0,
) -> dict[str, float]:
    required = required_remaining_average(
        anchor_price=anchor_price,
        elapsed_twap=elapsed_twap,
        elapsed_seconds=elapsed_seconds,
        total_seconds=total_seconds,
    )
    return {
        "terminalGapBps": 10_000.0 * (current_price - anchor_price) / anchor_price,
        "elapsedTwapGapBps": 10_000.0 * (elapsed_twap - anchor_price) / anchor_price,
        "requiredRemainingAverage": required,
        "requiredMarginBps": 10_000.0 * (current_price - required) / anchor_price,
        "realizedVolBps": float(realized_vol_bps),
        "remainingSeconds": total_seconds - elapsed_seconds,
    }


def realized_volatility_bps(
    *,
    samples: Iterable[tuple[int, float]],
    observation_us: int,
    lookback_us: int = 60_000_000,
) -> float:
    prices = np.asarray(
        [
            float(price)
            for timestamp_us, price in sorted(samples)
            if observation_us - lookback_us <= timestamp_us < observation_us
            and float(price) > 0
        ],
        dtype=float,
    )
    if len(prices) < 2:
        return 0.0
    returns = np.diff(np.log(prices))
    return float(np.std(returns) * 10_000.0)


def select_recovery_entry(
    points: Sequence[dict[str, Any]], *, recovery: float
) -> dict[str, Any] | None:
    if not points:
        return None
    if recovery <= 0:
        return points[0]
    running_low = float(points[0]["vwap"])
    for point in points[1:]:
        price = float(point["vwap"])
        running_low = min(running_low, price)
        if price - running_low + 1e-12 >= recovery:
            return point
    return None


def select_target_exit(
    path: Sequence[dict[str, Any]], *, target: float, delay_us: int = 0
) -> dict[str, Any] | None:
    trigger_us: int | None = None
    for point in path:
        if trigger_us is None and float(point["vwap"]) >= target:
            trigger_us = int(point["sourceUs"])
        if trigger_us is not None and int(point["sourceUs"]) >= trigger_us + delay_us:
            return point
    return None


def select_stable_entry(
    points: Sequence[dict[str, Any]],
    *,
    duration_us: int,
    stability_floor: float,
    reset_tolerance: float,
    band_low: float,
    band_high: float,
    worst_price_cap: float,
) -> dict[str, Any] | None:
    stable_since_us: int | None = None
    for point in points:
        price = float(point["vwap"])
        timestamp_us = int(point["sourceUs"])
        if price < stability_floor - reset_tolerance:
            stable_since_us = None
            continue
        if price >= stability_floor and stable_since_us is None:
            stable_since_us = timestamp_us
        if stable_since_us is None or timestamp_us - stable_since_us < duration_us:
            continue
        if (
            band_low <= price <= band_high
            and float(point["worstPrice"]) <= worst_price_cap
        ):
            return point
    return None


def select_reversal_signal(
    points: Sequence[dict[str, Any]],
    *,
    horizon_us: int,
    drop_min: float,
    high_floor: float,
) -> dict[str, Any] | None:
    recent: list[dict[str, Any]] = []
    for point in points:
        timestamp_us = int(point["sourceUs"])
        recent = [
            prior for prior in recent
            if timestamp_us - int(prior["sourceUs"]) <= horizon_us
        ]
        eligible_highs = [
            prior for prior in recent if float(prior["vwap"]) >= high_floor
        ]
        if eligible_highs:
            high = max(eligible_highs, key=lambda item: float(item["vwap"]))
            drop = float(high["vwap"]) - float(point["vwap"])
            if drop + 1e-12 >= drop_min:
                return {
                    **point,
                    "highSourceUs": int(high["sourceUs"]),
                    "highVwap": float(high["vwap"]),
                    "dropFromHigh": drop,
                }
        recent.append(point)
    return None


def latest_polymarket_state(
    connection: sqlite3.Connection,
    *,
    market_id: str,
    decision_received_us: int,
    quantity: float = 10.0,
    max_age_us: int = 2_000_000,
) -> dict[str, Any] | None:
    snapshots: dict[str, sqlite3.Row] = {}
    for outcome in ("UP", "DOWN"):
        row = connection.execute(
            """SELECT received_us,best_ask,asks_json FROM snapshots
               WHERE market_id=? AND outcome=? AND received_us<=? AND best_ask IS NOT NULL
               ORDER BY received_us DESC LIMIT 1""",
            (market_id, outcome, decision_received_us),
        ).fetchone()
        if row is None or decision_received_us - int(row["received_us"]) > max_age_us:
            return None
        snapshots[outcome] = row

    up_ask = float(snapshots["UP"]["best_ask"])
    down_ask = float(snapshots["DOWN"]["best_ask"])
    result: dict[str, Any] = {
        "upAsk": up_ask,
        "downAsk": down_ask,
        "marketProbabilityUp": complement_mid_probability(
            up_ask=up_ask, down_ask=down_ask
        ),
        "bookAgeMs": max(
            (decision_received_us - int(row["received_us"])) / 1_000.0
            for row in snapshots.values()
        ),
    }
    for outcome in ("UP", "DOWN"):
        levels = [
            BookLevel(Decimal(str(price)), Decimal(str(size)))
            for price, size in json.loads(snapshots[outcome]["asks_json"])
        ]
        fill = consume_asks(
            levels,
            Decimal(str(quantity)),
            Decimal("1"),
            fee_model="polymarket_crypto_taker",
            fee_rate=Decimal("0.07"),
        )
        prefix = outcome.lower()
        if fill is None:
            result[f"{prefix}Vwap"] = None
            result[f"{prefix}FeePerShare"] = None
            result[f"{prefix}TotalCost"] = None
            result[f"{prefix}WorstPrice"] = None
        else:
            vwap, fee, worst = fill
            result[f"{prefix}Vwap"] = float(vwap)
            result[f"{prefix}FeePerShare"] = float(fee)
            result[f"{prefix}TotalCost"] = float(vwap + fee)
            result[f"{prefix}WorstPrice"] = float(worst)
    return result


def probability_metrics(
    *, probabilities: np.ndarray, labels: np.ndarray
) -> dict[str, float | int]:
    predicted = np.asarray(probabilities, dtype=float)
    target = np.asarray(labels, dtype=float)
    if predicted.shape != target.shape or predicted.ndim != 1 or len(predicted) == 0:
        raise ValueError("probabilities and labels must be non-empty matching vectors")
    clipped = np.clip(predicted, 1e-12, 1.0 - 1e-12)
    return {
        "samples": int(len(target)),
        "brierScore": float(np.mean((predicted - target) ** 2)),
        "logLoss": float(-np.mean(target * np.log(clipped) + (1 - target) * np.log(1 - clipped))),
        "correct": int(np.sum((predicted >= 0.5) == (target >= 0.5))),
        "accuracy": float(np.mean((predicted >= 0.5) == (target >= 0.5))),
    }


def maker_limit_from_opposite_ask(opposite_ask: float) -> float:
    """Infer this outcome's highest executable bid from its complement ask."""
    if not 0 <= opposite_ask <= 1:
        raise ValueError("opposite ask must be between zero and one")
    return round(1.0 - float(opposite_ask), 10)


def maker_trade_fill_evidence(
    trades: Iterable[dict[str, Any]],
    *,
    order_received_us: int,
    wait_us: int,
    limit_price: float,
    quantity: float,
) -> dict[str, Any]:
    """Estimate maker fill evidence without pretending queue position is known.

    ``tradeThroughFill`` is the stricter observation: a later aggressive sell
    trades below our bid, which implies bids at our price were exhausted first.
    ``touchFill`` is only an optimistic upper bound: reported sell volume at or
    below our bid reaches our quantity, while unknown queue-ahead is ignored.
    """
    if wait_us <= 0 or quantity <= 0:
        raise ValueError("wait_us and quantity must be positive")
    end_us = order_received_us + wait_us
    touch_volume = 0.0
    touch_fill_us: int | None = None
    trade_through_fill_us: int | None = None
    for trade in sorted(trades, key=lambda row: int(row["receivedUs"])):
        received_us = int(trade["receivedUs"])
        if received_us <= order_received_us or received_us > end_us:
            continue
        if str(trade.get("side", "")).upper() != "SELL":
            continue
        price = float(trade["price"])
        if price <= limit_price + 1e-12:
            touch_volume += float(trade["size"])
            if touch_fill_us is None and touch_volume + 1e-12 >= quantity:
                touch_fill_us = received_us
        if trade_through_fill_us is None and price < limit_price - 1e-12:
            trade_through_fill_us = received_us
    return {
        "touchFill": touch_fill_us is not None,
        "touchFillUs": touch_fill_us,
        "touchVolume": touch_volume,
        "tradeThroughFill": trade_through_fill_us is not None,
        "tradeThroughFillUs": trade_through_fill_us,
    }


class LogisticProbabilityModel:
    def __init__(self, *, l2: float = 1.0, max_iter: int = 100) -> None:
        if l2 < 0:
            raise ValueError("l2 must not be negative")
        self.l2 = float(l2)
        self.max_iter = int(max_iter)
        self.mean_: np.ndarray | None = None
        self.scale_: np.ndarray | None = None
        self.coef_: np.ndarray | None = None

    def fit(
        self, features: np.ndarray, labels: np.ndarray
    ) -> "LogisticProbabilityModel":
        matrix = np.asarray(features, dtype=float)
        target = np.asarray(labels, dtype=float)
        if matrix.ndim != 2 or target.ndim != 1 or len(matrix) != len(target):
            raise ValueError("features and labels have incompatible shapes")
        if len(matrix) < 2 or set(np.unique(target)) != {0.0, 1.0}:
            raise ValueError("training data must contain both outcomes")

        self.mean_ = matrix.mean(axis=0)
        scale = matrix.std(axis=0)
        self.scale_ = np.where(scale > 1e-12, scale, 1.0)
        standardized = (matrix - self.mean_) / self.scale_
        design = np.column_stack([np.ones(len(matrix)), standardized])
        coefficients = np.zeros(design.shape[1], dtype=float)
        penalty = np.eye(design.shape[1]) * self.l2
        penalty[0, 0] = 0.0

        for _ in range(self.max_iter):
            scores = np.clip(design @ coefficients, -35.0, 35.0)
            probabilities = 1.0 / (1.0 + np.exp(-scores))
            weights = np.maximum(probabilities * (1.0 - probabilities), 1e-9)
            gradient = design.T @ (probabilities - target) + penalty @ coefficients
            hessian = design.T @ (design * weights[:, None]) + penalty
            step = np.linalg.solve(hessian, gradient)
            coefficients -= step
            if float(np.max(np.abs(step))) < 1e-9:
                break
        self.coef_ = coefficients
        return self

    def predict_proba(self, features: np.ndarray) -> np.ndarray:
        if self.mean_ is None or self.scale_ is None or self.coef_ is None:
            raise RuntimeError("model must be fitted before prediction")
        matrix = np.asarray(features, dtype=float)
        if matrix.ndim != 2 or matrix.shape[1] != len(self.mean_):
            raise ValueError("features have an incompatible shape")
        standardized = (matrix - self.mean_) / self.scale_
        design = np.column_stack([np.ones(len(matrix)), standardized])
        scores = np.clip(design @ self.coef_, -35.0, 35.0)
        return 1.0 / (1.0 + np.exp(-scores))
