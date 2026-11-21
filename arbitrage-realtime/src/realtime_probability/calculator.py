from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from math import erf, isfinite, log, sqrt
from typing import Deque, Dict, Optional, Tuple


@dataclass(frozen=True)
class CalculatorConfig:
    rolling_window_seconds: float = 60.0
    minimum_volatility_per_sqrt_second: float = 0.00005
    stale_after_seconds: float = 2.0
    probability_floor: float = 0.02
    probability_ceiling: float = 0.98
    minimum_distance_bps: float = 0.0
    minimum_probability: float = 0.55


@dataclass(frozen=True)
class RealtimeObservation:
    market_id: str
    market_start: float
    market_end: float
    price: float
    observed_at: float
    open_price: Optional[float] = None
    source_age_ms: Optional[float] = None
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None
    best_bid_size: Optional[float] = None
    best_ask_size: Optional[float] = None
    book_imbalance: Optional[float] = None
    trade_side: Optional[str] = None
    trade_quantity: Optional[float] = None


@dataclass
class _MarketState:
    market_start: float
    market_end: float
    open_price: Optional[float] = None
    session_high: Optional[float] = None
    session_low: Optional[float] = None
    samples: Deque[Tuple[float, float]] = field(default_factory=deque)
    trades: Deque[Tuple[float, float]] = field(default_factory=deque)
    last_observed_at: Optional[float] = None
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None
    best_bid_size: Optional[float] = None
    best_ask_size: Optional[float] = None
    book_imbalance: Optional[float] = None
    book_observed_at: Optional[float] = None


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + erf(value / sqrt(2.0)))


class RealtimeProbabilityEngine:
    """A bounded, history-free real-time probability engine.

    The only state retained is the current market and its short rolling price
    queue.  No customer history or model-training file is read.
    """

    def __init__(self, config: CalculatorConfig | None = None) -> None:
        self.config = config or CalculatorConfig()
        self._markets: Dict[str, _MarketState] = {}

    def _state_for(self, observation: RealtimeObservation) -> _MarketState:
        state = self._markets.get(observation.market_id)
        if state is None or state.market_start != observation.market_start:
            state = _MarketState(observation.market_start, observation.market_end)
            self._markets[observation.market_id] = state
        return state

    def update(self, observation: RealtimeObservation) -> dict:
        if not observation.market_id:
            raise ValueError("market_id is required")
        values = (observation.market_start, observation.market_end, observation.price, observation.observed_at)
        if not all(isfinite(float(value)) for value in values) or observation.price <= 0:
            raise ValueError("market times and price must be finite; price must be positive")
        state = self._state_for(observation)
        if state.last_observed_at is not None and observation.observed_at < state.last_observed_at:
            return self._result(observation, state, rejected="out_of_order")
        state.last_observed_at = observation.observed_at
        if state.open_price is None:
            state.open_price = observation.open_price if observation.open_price and observation.open_price > 0 else observation.price
        if observation.open_price and observation.open_price > 0 and not state.samples:
            state.open_price = observation.open_price
        self._update_book(state, observation)
        self._update_trades(state, observation)
        state.session_high = observation.price if state.session_high is None else max(state.session_high, observation.price)
        state.session_low = observation.price if state.session_low is None else min(state.session_low, observation.price)
        state.samples.append((observation.observed_at, observation.price))
        cutoff = observation.observed_at - self.config.rolling_window_seconds
        while state.samples and state.samples[0][0] < cutoff:
            state.samples.popleft()
        while state.trades and state.trades[0][0] < observation.observed_at - 60.0:
            state.trades.popleft()
        return self._result(observation, state)

    @staticmethod
    def _update_book(state: _MarketState, observation: RealtimeObservation) -> None:
        supplied = (observation.best_bid, observation.best_ask, observation.best_bid_size, observation.best_ask_size)
        if any(value is not None for value in supplied):
            if observation.best_bid is not None and observation.best_bid > 0:
                state.best_bid = observation.best_bid
            if observation.best_ask is not None and observation.best_ask > 0:
                state.best_ask = observation.best_ask
            if observation.best_bid_size is not None and observation.best_bid_size >= 0:
                state.best_bid_size = observation.best_bid_size
            if observation.best_ask_size is not None and observation.best_ask_size >= 0:
                state.best_ask_size = observation.best_ask_size
            state.book_observed_at = observation.observed_at
        if observation.book_imbalance is not None and isfinite(float(observation.book_imbalance)):
            state.book_imbalance = max(-1.0, min(1.0, float(observation.book_imbalance)))
        elif state.best_bid_size is not None and state.best_ask_size is not None:
            total = state.best_bid_size + state.best_ask_size
            if total > 0:
                state.book_imbalance = (state.best_bid_size - state.best_ask_size) / total

    @staticmethod
    def _update_trades(state: _MarketState, observation: RealtimeObservation) -> None:
        quantity = observation.trade_quantity
        side = (observation.trade_side or "").upper()
        if quantity is None or quantity <= 0 or side not in {"BUY", "SELL"}:
            return
        state.trades.append((observation.observed_at, quantity if side == "BUY" else -quantity))

    def _result(self, observation: RealtimeObservation, state: _MarketState, rejected: str = "") -> dict:
        open_price = state.open_price or observation.price
        remaining = max(0.0, observation.market_end - observation.observed_at)
        distance_bps = (observation.price / open_price - 1.0) * 10000.0 if open_price else 0.0
        range_bps = ((state.session_high or observation.price) - (state.session_low or observation.price)) / open_price * 10000.0 if open_price else 0.0
        drift_rate, variance_rate = self._rolling_moments(state.samples)
        sigma = max(sqrt(max(variance_rate, 0.0)), self.config.minimum_volatility_per_sqrt_second)
        denominator = sigma * sqrt(max(remaining, 1.0))
        log_distance = log(observation.price / open_price) if open_price else 0.0
        z = (log_distance + drift_rate * remaining) / denominator if open_price else 0.0
        probability_up = min(self.config.probability_ceiling, max(self.config.probability_floor, _normal_cdf(z)))
        age_seconds = (observation.source_age_ms or 0.0) / 1000.0
        stale = observation.source_age_ms is not None and age_seconds > self.config.stale_after_seconds
        book_age_seconds = None
        if state.book_observed_at is not None:
            book_age_seconds = max(0.0, observation.observed_at - state.book_observed_at)
        book_stale = book_age_seconds is not None and book_age_seconds > self.config.stale_after_seconds
        quality = "stale" if stale else ("warming_up" if len(state.samples) < 3 else "good")
        entry_allowed = (
            not stale
            and not rejected
            and len(state.samples) >= 3
            and abs(distance_bps) >= self.config.minimum_distance_bps
            and max(probability_up, 1.0 - probability_up) >= self.config.minimum_probability
        )
        if remaining <= 0:
            entry_allowed = False
            quality = "closed"
        return {
            "market_id": observation.market_id,
            "observed_at": observation.observed_at,
            "remaining_seconds": round(remaining, 3),
            "current_price": observation.price,
            "open_price": open_price,
            "session_high": state.session_high,
            "session_low": state.session_low,
            "range_bps": round(range_bps, 4),
            "distance_from_open_bps": round(distance_bps, 4),
            "momentum_bps": round(self._lookback_return_bps(state.samples, observation.observed_at, observation.price, 5.0), 4),
            "return_1s_bps": round(self._lookback_return_bps(state.samples, observation.observed_at, observation.price, 1.0), 4),
            "return_3s_bps": round(self._lookback_return_bps(state.samples, observation.observed_at, observation.price, 3.0), 4),
            "return_5s_bps": round(self._lookback_return_bps(state.samples, observation.observed_at, observation.price, 5.0), 4),
            "return_10s_bps": round(self._lookback_return_bps(state.samples, observation.observed_at, observation.price, 10.0), 4),
            "best_bid": state.best_bid,
            "best_ask": state.best_ask,
            "best_bid_size": state.best_bid_size,
            "best_ask_size": state.best_ask_size,
            "book_imbalance": round(state.book_imbalance, 6) if state.book_imbalance is not None else None,
            "book_age_seconds": round(book_age_seconds, 3) if book_age_seconds is not None else None,
            "book_stale": book_stale,
            "spread_bps": round((state.best_ask - state.best_bid) / ((state.best_ask + state.best_bid) / 2) * 10000.0, 4)
            if state.best_bid is not None and state.best_ask is not None and state.best_ask >= state.best_bid and state.best_bid > 0 else None,
            "microprice_edge_bps": self._microprice_edge_bps(state),
            "trade_imbalance_60s": self._trade_imbalance(state),
            "taker_buy_ratio_60s": self._taker_buy_ratio(state),
            "trade_count_60s": len(state.trades),
            "trade_rate_10s": self._trade_rate(state, observation.observed_at, 10.0),
            "log_distance": log_distance,
            "drift_bps_per_second": round(drift_rate * 10000.0, 6),
            "volatility_bps_per_sqrt_second": round(sigma * 10000.0, 6),
            "z_score": round(z, 6),
            "probability_up": round(probability_up, 6),
            "probability_down": round(1.0 - probability_up, 6),
            # Avoid showing a directional label for tiny numerical deviations
            # that still round to 50.0% on the page.
            "signal": "UP" if probability_up > 0.505 else "DOWN" if probability_up < 0.495 else "NEUTRAL",
            "data_quality": "book_stale" if book_stale else quality,
            "entry_allowed": entry_allowed,
            "rejection": rejected or None,
        }

    @staticmethod
    def _microprice_edge_bps(state: _MarketState) -> float | None:
        if None in (state.best_bid, state.best_ask, state.best_bid_size, state.best_ask_size):
            return None
        total = float(state.best_bid_size + state.best_ask_size)  # type: ignore[operator]
        if total <= 0 or state.best_bid <= 0 or state.best_ask < state.best_bid:  # type: ignore[operator]
            return None
        micro = (state.best_ask * state.best_bid_size + state.best_bid * state.best_ask_size) / total  # type: ignore[operator]
        mid = (state.best_bid + state.best_ask) / 2  # type: ignore[operator]
        return round((micro / mid - 1.0) * 10000.0, 4) if mid else None

    @staticmethod
    def _trade_imbalance(state: _MarketState) -> float | None:
        if not state.trades:
            return None
        total = sum(abs(quantity) for _, quantity in state.trades)
        return round(sum(quantity for _, quantity in state.trades) / total, 6) if total else None

    @staticmethod
    def _taker_buy_ratio(state: _MarketState) -> float | None:
        if not state.trades:
            return None
        buys = sum(quantity for _, quantity in state.trades if quantity > 0)
        total = sum(abs(quantity) for _, quantity in state.trades)
        return round(buys / total, 6) if total else None

    @staticmethod
    def _trade_rate(state: _MarketState, observed_at: float, window: float) -> float | None:
        if not state.trades:
            return None
        count = sum(1 for timestamp, _ in state.trades if timestamp >= observed_at - window)
        return round(count / window, 4)

    @staticmethod
    def _rolling_moments(samples: Deque[Tuple[float, float]]) -> Tuple[float, float]:
        if len(samples) < 2:
            return 0.0, 0.0
        increments = []
        for (time_a, price_a), (time_b, price_b) in zip(samples, list(samples)[1:]):
            delta = max(time_b - time_a, 1e-3)
            if price_a > 0 and price_b > 0:
                increments.append((log(price_b / price_a), delta))
        if not increments:
            return 0.0, 0.0
        total_time = sum(delta for _, delta in increments)
        drift = sum(value for value, _ in increments) / total_time
        variance = sum((value - drift * delta) ** 2 for value, delta in increments) / total_time
        return drift, variance

    @staticmethod
    def _lookback_return_bps(
        samples: Deque[Tuple[float, float]],
        observed_at: float,
        current_price: float,
        lookback_seconds: float,
    ) -> float:
        if len(samples) < 2:
            return 0.0
        target = observed_at - lookback_seconds
        candidate_time, candidate_price = min(samples, key=lambda item: abs(item[0] - target))
        if candidate_time >= observed_at or candidate_price <= 0:
            return 0.0
        return (current_price / candidate_price - 1.0) * 10000.0

    def active_market_count(self) -> int:
        return len(self._markets)
