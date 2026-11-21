from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import math
import threading
from typing import Any


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _return(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None or current <= 0 or previous <= 0:
        return None
    return math.log(current / previous)


def _imbalance(bid_size: float | None, ask_size: float | None) -> float | None:
    if bid_size is None or ask_size is None or bid_size + ask_size <= 0:
        return None
    return (bid_size - ask_size) / (bid_size + ask_size)


MAX_PRICE_POINTS = 200_000
MAX_TRADE_POINTS = 100_000
MAX_OPTION_TRADES = 100_000
MAX_OPTION_TICKER_POINTS = 1_200


@dataclass(frozen=True)
class PricePoint:
    timestamp_us: int
    price: float


@dataclass(frozen=True)
class TradePoint:
    timestamp_us: int
    price: float
    quantity: float
    signed_quantity: float


@dataclass
class VenueState:
    prices: deque[PricePoint] = field(
        default_factory=lambda: deque(maxlen=MAX_PRICE_POINTS)
    )
    trades: deque[TradePoint] = field(
        default_factory=lambda: deque(maxlen=MAX_TRADE_POINTS)
    )
    bid: float | None = None
    ask: float | None = None
    bid_size: float | None = None
    ask_size: float | None = None
    updated_us: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DeribitInstrument:
    name: str
    expiration_us: int
    strike: float
    option_type: str


@dataclass(frozen=True)
class OptionTickerPoint:
    timestamp_us: int
    mark_iv: float | None
    open_interest: float | None
    delta: float | None
    mark_price: float | None


@dataclass(frozen=True)
class OptionTradePoint:
    timestamp_us: int
    instrument: str
    option_type: str
    direction: str
    amount: float
    premium_btc: float
    premium_usd: float
    iv: float | None


class CrossMarketState:
    """Bounded in-memory state used to produce point-in-time feature rows.

    All histories are pruned by source timestamp.  The service can therefore run
    indefinitely without message count translating into unbounded memory use.
    """

    def __init__(self, memory_window_seconds: int = 600) -> None:
        self.memory_window_us = memory_window_seconds * 1_000_000
        self.binance = VenueState()
        self.binance_futures = VenueState()
        self.hyperliquid = VenueState()
        self.hyperliquid_mark = VenueState()
        self.hyperliquid_oracle = VenueState()
        self.deribit_index = VenueState()
        self.deribit_perpetual = VenueState()
        self.deribit_dvol = VenueState()
        self.chainlink = VenueState()
        self.polymarket_binance = VenueState()
        self.deribit_options: dict[str, dict[str, Any]] = {}
        self.deribit_option_history: dict[str, deque[OptionTickerPoint]] = {}
        self.deribit_option_trades: deque[OptionTradePoint] = deque(
            maxlen=MAX_OPTION_TRADES
        )
        self.deribit_instruments: dict[str, DeribitInstrument] = {}
        self._lock = threading.RLock()
        self.message_counts = {
            "BINANCE": 0, "BINANCE_FUTURES": 0, "HYPERLIQUID": 0, "DERIBIT": 0,
            "CHAINLINK": 0, "POLYMARKET_BINANCE": 0,
        }

    @staticmethod
    def _append_price(venue: VenueState, timestamp_us: int, price: Any) -> None:
        numeric = _number(price)
        if numeric is None or numeric <= 0:
            return
        if venue.prices and timestamp_us < venue.prices[-1].timestamp_us:
            return
        venue.prices.append(PricePoint(timestamp_us, numeric))
        venue.updated_us = timestamp_us

    @staticmethod
    def _prune_venue(venue: VenueState, cutoff_us: int) -> None:
        while len(venue.prices) > 1 and venue.prices[1].timestamp_us < cutoff_us:
            venue.prices.popleft()
        while venue.trades and venue.trades[0].timestamp_us < cutoff_us:
            venue.trades.popleft()

    def _prune(self, timestamp_us: int) -> None:
        cutoff = timestamp_us - self.memory_window_us
        for venue in (
            self.binance,
            self.binance_futures,
            self.hyperliquid,
            self.hyperliquid_mark,
            self.hyperliquid_oracle,
            self.deribit_index,
            self.deribit_perpetual,
            self.deribit_dvol,
            self.chainlink,
            self.polymarket_binance,
        ):
            self._prune_venue(venue, cutoff)
        for points in self.deribit_option_history.values():
            while points and points[0].timestamp_us < cutoff:
                points.popleft()
        while (
            self.deribit_option_trades
            and self.deribit_option_trades[0].timestamp_us < cutoff
        ):
            self.deribit_option_trades.popleft()

    def update_binance(self, update: dict[str, Any]) -> None:
        self._update_binance_market(self.binance, "BINANCE", update)

    def update_binance_futures(self, update: dict[str, Any]) -> None:
        self._update_binance_market(self.binance_futures, "BINANCE_FUTURES", update)

    def _update_binance_market(
        self, venue: VenueState, source: str, update: dict[str, Any]
    ) -> None:
        with self._lock:
            self.message_counts[source] += 1
            timestamp_us = int(update["sourceTimeUs"])
            kind = update.get("kind")
            if kind == "trade":
                price = _number(update.get("price"))
                quantity = _number(update.get("quantity"))
                if price is not None and quantity is not None:
                    sign = -1.0 if update.get("isBuyerMaker") else 1.0
                    venue.trades.append(
                        TradePoint(timestamp_us, price, quantity, sign * quantity)
                    )
                    self._append_price(venue, timestamp_us, price)
            elif kind == "book":
                venue.bid = _number(update.get("bid"))
                venue.ask = _number(update.get("ask"))
                venue.bid_size = _number(update.get("bidSize"))
                venue.ask_size = _number(update.get("askSize"))
                mid = None
                if venue.bid and venue.ask:
                    mid = (venue.bid + venue.ask) / 2
                self._append_price(venue, timestamp_us, mid)
            elif kind == "kline":
                interval = str(update.get("interval", "1m"))
                venue.extra[f"kline_{interval}"] = dict(update)
                self._append_price(venue, timestamp_us, update.get("close"))
            self._prune(timestamp_us)

    def update_hyperliquid(self, update: dict[str, Any]) -> None:
        with self._lock:
            self.message_counts["HYPERLIQUID"] += 1
            timestamp_us = int(update["sourceTimeUs"])
            kind = update.get("kind")
            if kind == "trade":
                price = _number(update.get("price"))
                quantity = _number(update.get("quantity"))
                if price is not None and quantity is not None:
                    signed = quantity if str(update.get("side", "")).upper() == "B" else -quantity
                    self.hyperliquid.trades.append(
                        TradePoint(timestamp_us, price, quantity, signed)
                    )
                    self._append_price(self.hyperliquid, timestamp_us, price)
            elif kind == "book":
                self.hyperliquid.bid = _number(update.get("bid"))
                self.hyperliquid.ask = _number(update.get("ask"))
                self.hyperliquid.bid_size = _number(update.get("bidSize"))
                self.hyperliquid.ask_size = _number(update.get("askSize"))
                mid = None
                if self.hyperliquid.bid and self.hyperliquid.ask:
                    mid = (self.hyperliquid.bid + self.hyperliquid.ask) / 2
                self._append_price(self.hyperliquid, timestamp_us, mid)
            elif kind == "context":
                for key in ("markPrice", "oraclePrice", "midPrice", "openInterest", "funding"):
                    value = _number(update.get(key))
                    if value is not None:
                        self.hyperliquid.extra[key] = value
                price = update.get("midPrice") or update.get("markPrice")
                self._append_price(self.hyperliquid, timestamp_us, price)
                self._append_price(self.hyperliquid_mark, timestamp_us, update.get("markPrice"))
                self._append_price(self.hyperliquid_oracle, timestamp_us, update.get("oraclePrice"))
            elif kind == "mid":
                self._append_price(self.hyperliquid, timestamp_us, update.get("price"))
            elif kind == "kline":
                self.hyperliquid.extra["kline_1m"] = dict(update)
                self._append_price(self.hyperliquid, timestamp_us, update.get("close"))
            self._prune(timestamp_us)

    def update_reference(self, update: dict[str, Any]) -> None:
        with self._lock:
            source = str(update.get("source", ""))
            timestamp_us = int(update["sourceTimeUs"])
            if source == "CHAINLINK":
                self.message_counts["CHAINLINK"] += 1
                self._append_price(self.chainlink, timestamp_us, update.get("price"))
            elif source == "POLYMARKET_BINANCE":
                self.message_counts["POLYMARKET_BINANCE"] += 1
                self._append_price(self.polymarket_binance, timestamp_us, update.get("price"))
            self._prune(timestamp_us)

    def set_deribit_instruments(self, instruments: list[dict[str, Any]]) -> None:
        with self._lock:
            selected: dict[str, DeribitInstrument] = {}
            for raw in instruments:
                name = str(raw.get("instrument_name", ""))
                option_type = str(raw.get("option_type", "")).lower()
                strike = _number(raw.get("strike"))
                expiration = raw.get("expiration_timestamp")
                if name and option_type in {"call", "put"} and strike and expiration:
                    selected[name] = DeribitInstrument(
                        name=name,
                        expiration_us=int(expiration) * 1_000,
                        strike=strike,
                        option_type=option_type,
                    )
            # Deribit reconnects select a fresh nearest expiry. Replacing instead of
            # accumulating old expiries keeps both dictionaries bounded for long runs.
            self.deribit_instruments = selected
            self.deribit_options = {
                name: ticker for name, ticker in self.deribit_options.items()
                if name in selected
            }
            self.deribit_option_history = {
                name: points for name, points in self.deribit_option_history.items()
                if name in selected
            }

    def update_deribit(self, update: dict[str, Any]) -> None:
        with self._lock:
            self.message_counts["DERIBIT"] += 1
            timestamp_us = int(update["sourceTimeUs"])
            kind = update.get("kind")
            if kind == "index":
                self._append_price(self.deribit_index, timestamp_us, update.get("price"))
            elif kind == "perpetual":
                for key in ("markPrice", "indexPrice", "funding8h", "openInterest"):
                    value = _number(update.get(key))
                    if value is not None:
                        self.deribit_perpetual.extra[key] = value
                price = update.get("lastPrice") or update.get("markPrice")
                self._append_price(self.deribit_perpetual, timestamp_us, price)
            elif kind == "option":
                name = str(update.get("instrument", ""))
                if name:
                    self.deribit_options[name] = dict(update)
                    point = OptionTickerPoint(
                        timestamp_us=timestamp_us,
                        mark_iv=_number(update.get("markIv")),
                        open_interest=_number(update.get("openInterest")),
                        delta=_number(update.get("delta")),
                        mark_price=_number(update.get("markPrice")),
                    )
                    points = self.deribit_option_history.setdefault(
                        name, deque(maxlen=MAX_OPTION_TICKER_POINTS)
                    )
                    # Tickers arrive at 100ms; one observation per contract per second
                    # is sufficient for 15-120 second research and caps memory sharply.
                    if not points or timestamp_us - points[-1].timestamp_us >= 1_000_000:
                        points.append(point)
            elif kind == "option_trades":
                for raw in update.get("trades") or []:
                    name = str(raw.get("instrument", ""))
                    direction = str(raw.get("direction", "")).lower()
                    amount = _number(raw.get("amount"))
                    price = _number(raw.get("price"))
                    index_price = _number(raw.get("indexPrice"))
                    if not name or direction not in {"buy", "sell"}:
                        continue
                    if amount is None or price is None or index_price is None:
                        continue
                    instrument = self.deribit_instruments.get(name)
                    option_type = (
                        instrument.option_type if instrument is not None
                        else "call" if name.endswith("-C")
                        else "put" if name.endswith("-P")
                        else ""
                    )
                    if not option_type:
                        continue
                    trade_us = int(raw.get("timestampUs") or timestamp_us)
                    premium_btc = amount * price
                    self.deribit_option_trades.append(OptionTradePoint(
                        timestamp_us=trade_us,
                        instrument=name,
                        option_type=option_type,
                        direction=direction,
                        amount=amount,
                        premium_btc=premium_btc,
                        premium_usd=premium_btc * index_price,
                        iv=_number(raw.get("iv")),
                    ))
            elif kind == "volatility_index":
                value = _number(update.get("value"))
                if value is not None:
                    self.deribit_index.extra["dvol"] = value
                    self.deribit_index.updated_us = timestamp_us
                    self._append_price(self.deribit_dvol, timestamp_us, value)
            self._prune(timestamp_us)

    @staticmethod
    def _price_at(points: deque[PricePoint], timestamp_us: int) -> float | None:
        for point in reversed(points):
            if point.timestamp_us <= timestamp_us:
                return point.price
        return None

    @staticmethod
    def _latest(points: deque[PricePoint], as_of_us: int) -> float | None:
        return CrossMarketState._price_at(points, as_of_us)

    @staticmethod
    def _trade_features(venue: VenueState, as_of_us: int, seconds: int = 60) -> dict[str, Any]:
        cutoff = as_of_us - seconds * 1_000_000
        trades = [item for item in venue.trades if cutoff <= item.timestamp_us <= as_of_us]
        volume = sum(item.quantity for item in trades)
        signed = sum(item.signed_quantity for item in trades)
        buy = sum(item.signed_quantity for item in trades if item.signed_quantity > 0)
        return {
            f"tradeCount{seconds}s": len(trades),
            f"volume{seconds}s": volume,
            f"tradeImbalance{seconds}s": signed / volume if volume else None,
            f"takerBuyRatio{seconds}s": buy / volume if volume else None,
        }

    def _venue_features(
        self,
        venue: VenueState,
        *,
        start_us: int,
        as_of_us: int,
    ) -> dict[str, Any]:
        latest = self._latest(venue.prices, as_of_us)
        start = self._price_at(venue.prices, start_us)
        result: dict[str, Any] = {
            "available": latest is not None,
            "price": latest,
            "startPrice": start,
            "returnFromStart": _return(latest, start),
            "bestBid": venue.bid,
            "bestAsk": venue.ask,
            "bestBidSize": venue.bid_size,
            "bestAskSize": venue.ask_size,
            "bookImbalance": _imbalance(venue.bid_size, venue.ask_size),
            "sourceAgeMs": (as_of_us - venue.updated_us) / 1_000 if venue.updated_us else None,
        }
        for seconds in (1, 3, 5, 10, 15, 30, 60, 120):
            result[f"return{seconds}s"] = _return(
                latest, self._price_at(venue.prices, as_of_us - seconds * 1_000_000)
            )
        result.update(self._trade_features(venue, as_of_us))
        return result

    @staticmethod
    def _option_point_at(
        points: deque[OptionTickerPoint] | None, timestamp_us: int
    ) -> OptionTickerPoint | None:
        if not points:
            return None
        for point in reversed(points):
            if point.timestamp_us <= timestamp_us:
                return point
        return None

    def _option_flow_features(
        self, as_of_us: int, selected_names: set[str], seconds: int = 60
    ) -> dict[str, Any]:
        cutoff = as_of_us - seconds * 1_000_000
        trades = [
            item for item in self.deribit_option_trades
            if cutoff <= item.timestamp_us <= as_of_us
        ]

        def summarize(items: list[OptionTradePoint], prefix: str) -> dict[str, Any]:
            buckets = {
                (option_type, direction): sum(
                    item.premium_usd for item in items
                    if item.option_type == option_type and item.direction == direction
                )
                for option_type in ("call", "put")
                for direction in ("buy", "sell")
            }
            call_buy = buckets[("call", "buy")]
            call_sell = buckets[("call", "sell")]
            put_buy = buckets[("put", "buy")]
            put_sell = buckets[("put", "sell")]
            bullish = call_buy + put_sell
            bearish = put_buy + call_sell
            total = bullish + bearish
            return {
                f"{prefix}TradeCount{seconds}s": len(items),
                f"{prefix}CallBuyPremiumUsd{seconds}s": call_buy,
                f"{prefix}CallSellPremiumUsd{seconds}s": call_sell,
                f"{prefix}PutBuyPremiumUsd{seconds}s": put_buy,
                f"{prefix}PutSellPremiumUsd{seconds}s": put_sell,
                f"{prefix}BullishPremiumUsd{seconds}s": bullish,
                f"{prefix}BearishPremiumUsd{seconds}s": bearish,
                f"{prefix}FlowImbalance{seconds}s": (
                    (bullish - bearish) / total if total else None
                ),
            }

        result = summarize(trades, "option")
        selected = [item for item in trades if item.instrument in selected_names]
        result.update(summarize(selected, "rr25Selected"))
        return result

    def _deribit_option_features(self, as_of_us: int, index_price: float | None) -> dict[str, Any]:
        if index_price is None:
            return {"optionAvailable": False}
        candidates: list[tuple[DeribitInstrument, dict[str, Any]]] = []
        for name, ticker in self.deribit_options.items():
            instrument = self.deribit_instruments.get(name)
            if instrument is None or instrument.expiration_us <= as_of_us:
                continue
            age = as_of_us - int(ticker.get("sourceTimeUs", 0))
            if age < 0 or age > 30_000_000:
                continue
            candidates.append((instrument, ticker))
        if not candidates:
            return {"optionAvailable": False}
        nearest_expiry = min(item[0].expiration_us for item in candidates)
        expiry = [item for item in candidates if item[0].expiration_us == nearest_expiry]

        def mark_iv(item: tuple[DeribitInstrument, dict[str, Any]]) -> float | None:
            return _number(item[1].get("markIv"))

        def delta(item: tuple[DeribitInstrument, dict[str, Any]]) -> float | None:
            return _number(item[1].get("delta"))

        def oi(item: tuple[DeribitInstrument, dict[str, Any]] | None) -> float | None:
            return _number(item[1].get("openInterest")) if item else None

        def nearest_delta(
            items: list[tuple[DeribitInstrument, dict[str, Any]]], target: float
        ) -> tuple[DeribitInstrument, dict[str, Any]] | None:
            usable = [item for item in items if delta(item) is not None and mark_iv(item) is not None]
            return min(usable, key=lambda item: abs(float(delta(item)) - target), default=None)

        def previous(item: tuple[DeribitInstrument, dict[str, Any]] | None) -> OptionTickerPoint | None:
            if item is None:
                return None
            return self._option_point_at(
                self.deribit_option_history.get(item[0].name), as_of_us - 60_000_000
            )

        def difference(current: float | None, old: float | None) -> float | None:
            return current - old if current is not None and old is not None else None

        def ratio_change(current: float | None, old: float | None) -> float | None:
            return (current - old) / old if current is not None and old not in (None, 0) else None

        calls = [item for item in expiry if item[0].option_type == "call"]
        puts = [item for item in expiry if item[0].option_type == "put"]
        atm_call = min(calls, key=lambda item: abs(item[0].strike - index_price), default=None)
        atm_put = min(puts, key=lambda item: abs(item[0].strike - index_price), default=None)
        atm = [item for item in (atm_call, atm_put) if item is not None]
        call25 = nearest_delta(calls, 0.25)
        put25 = nearest_delta(puts, -0.25)
        atm_values = [value for value in (mark_iv(item) for item in atm) if value is not None]
        atm_iv = sum(atm_values) / len(atm_values) if atm_values else None
        call25_iv = mark_iv(call25) if call25 else None
        put25_iv = mark_iv(put25) if put25 else None
        rr25 = (
            call25_iv - put25_iv
            if call25_iv is not None and put25_iv is not None else None
        )
        call25_previous = previous(call25)
        put25_previous = previous(put25)
        rr25_previous = (
            call25_previous.mark_iv - put25_previous.mark_iv
            if call25_previous and put25_previous
            and call25_previous.mark_iv is not None and put25_previous.mark_iv is not None
            else None
        )
        previous_atm_values = [
            point.mark_iv for point in (previous(atm_call), previous(atm_put))
            if point is not None and point.mark_iv is not None
        ]
        atm_iv_previous = (
            sum(previous_atm_values) / len(previous_atm_values)
            if previous_atm_values else None
        )
        current_total_oi = sum(value for value in (oi(item) for item in expiry) if value is not None)
        previous_total_oi_values = [
            point.open_interest
            for point in (
                self._option_point_at(
                    self.deribit_option_history.get(item[0].name), as_of_us - 60_000_000
                )
                for item in expiry
            )
            if point is not None and point.open_interest is not None
        ]
        previous_total_oi = sum(previous_total_oi_values) if previous_total_oi_values else None
        call25_oi = oi(call25)
        put25_oi = oi(put25)
        call25_old_oi = call25_previous.open_interest if call25_previous else None
        put25_old_oi = put25_previous.open_interest if put25_previous else None
        oi_sum25 = (
            call25_oi + put25_oi
            if call25_oi is not None and put25_oi is not None else None
        )
        result = {
            "optionAvailable": bool(atm_values),
            "optionExpiryUs": nearest_expiry,
            "optionHoursToExpiry": (nearest_expiry - as_of_us) / 3_600_000_000,
            "atmIv": atm_iv,
            "atmIvChange60s": difference(atm_iv, atm_iv_previous),
            "atmCallInstrument": atm_call[0].name if atm_call else None,
            "atmPutInstrument": atm_put[0].name if atm_put else None,
            "atmCallOi": oi(atm_call),
            "atmPutOi": oi(atm_put),
            "call25Instrument": call25[0].name if call25 else None,
            "put25Instrument": put25[0].name if put25 else None,
            "call25Strike": call25[0].strike if call25 else None,
            "put25Strike": put25[0].strike if put25 else None,
            "call25Delta": delta(call25) if call25 else None,
            "put25Delta": delta(put25) if put25 else None,
            "call25Iv": call25_iv,
            "put25Iv": put25_iv,
            "call25IvChange60s": difference(
                call25_iv, call25_previous.mark_iv if call25_previous else None
            ),
            "put25IvChange60s": difference(
                put25_iv, put25_previous.mark_iv if put25_previous else None
            ),
            "rr25": rr25,
            "rr25Change60s": difference(rr25, rr25_previous),
            "butterfly25": (
                (call25_iv + put25_iv) / 2 - atm_iv
                if call25_iv is not None and put25_iv is not None and atm_iv is not None
                else None
            ),
            # Backward-compatible name. It now contains the nearest observed 25D RR,
            # not the former same-strike ATM approximation.
            "riskReversalProxy": rr25,
            "call25Oi": call25_oi,
            "put25Oi": put25_oi,
            "call25OiChange60s": difference(call25_oi, call25_old_oi),
            "put25OiChange60s": difference(put25_oi, put25_old_oi),
            "call25OiReturn60s": ratio_change(call25_oi, call25_old_oi),
            "put25OiReturn60s": ratio_change(put25_oi, put25_old_oi),
            "oiSkew25": (
                (call25_oi - put25_oi) / oi_sum25 if oi_sum25 else None
            ),
            "selectedExpiryOi": current_total_oi,
            "selectedExpiryOiChange60s": difference(current_total_oi, previous_total_oi),
            "selectedExpiryOiReturn60s": ratio_change(current_total_oi, previous_total_oi),
        }
        selected_names = {
            item[0].name for item in (call25, put25) if item is not None
        }
        result.update(self._option_flow_features(as_of_us, selected_names))
        return result

    @staticmethod
    def _direction_score(features: dict[str, Any], weights: tuple[tuple[str, float], ...]) -> float | None:
        values = [(features.get(key), weight) for key, weight in weights]
        present = [(float(value), weight) for value, weight in values if value is not None]
        if not present:
            return None
        return sum(value * weight for value, weight in present) / sum(abs(weight) for _, weight in present)

    def snapshot(self, *, market_id: str, start_us: int, end_us: int, as_of_us: int) -> dict[str, Any]:
        with self._lock:
            binance = self._venue_features(self.binance, start_us=start_us, as_of_us=as_of_us)
            binance_futures = self._venue_features(
                self.binance_futures, start_us=start_us, as_of_us=as_of_us
            )
            # Hyperliquid's primary direction is its mark price. Trades, BBO and
            # order flow remain auxiliary features and aren't mixed into the mark series.
            hyper = self._venue_features(
                self.hyperliquid_mark, start_us=start_us, as_of_us=as_of_us
            )
            hyper.update(self._trade_features(self.hyperliquid, as_of_us))
            hyper.update({
                "bestBid": self.hyperliquid.bid,
                "bestAsk": self.hyperliquid.ask,
                "bestBidSize": self.hyperliquid.bid_size,
                "bestAskSize": self.hyperliquid.ask_size,
                "bookImbalance": _imbalance(
                    self.hyperliquid.bid_size, self.hyperliquid.ask_size
                ),
                "tradeOrMidPrice": self._latest(self.hyperliquid.prices, as_of_us),
            })
            hyper.update({key: self.hyperliquid.extra.get(key) for key in (
                "markPrice", "oraclePrice", "midPrice", "openInterest", "funding"
            )})
            mark = hyper.get("markPrice")
            oracle = self._latest(self.hyperliquid_oracle.prices, as_of_us)
            hyper["oraclePrice"] = oracle
            hyper["markOracleBasis"] = (
                (mark - oracle) / oracle if mark is not None and oracle not in (None, 0) else None
            )
            binance_price = binance.get("price")
            hyper_price = hyper.get("price")
            hyper["binanceBasis"] = (
                (hyper_price - binance_price) / binance_price
                if hyper_price is not None and binance_price not in (None, 0)
                else None
            )

            deribit = self._venue_features(
                self.deribit_index, start_us=start_us, as_of_us=as_of_us
            )
            perpetual = self._venue_features(
                self.deribit_perpetual, start_us=start_us, as_of_us=as_of_us
            )
            deribit["perpetualPrice"] = perpetual.get("price")
            deribit["perpetualReturnFromStart"] = perpetual.get("returnFromStart")
            deribit.update({key: self.deribit_perpetual.extra.get(key) for key in (
                "markPrice", "funding8h", "openInterest"
            )})
            index_price = deribit.get("price")
            perp_price = perpetual.get("price")
            deribit["perpetualBasis"] = (
                (perp_price - index_price) / index_price
                if perp_price is not None and index_price not in (None, 0)
                else None
            )
            dvol = self._venue_features(
                self.deribit_dvol, start_us=start_us, as_of_us=as_of_us
            )
            deribit["dvol"] = dvol.get("price")
            deribit["dvolChangeFromStart"] = (
                dvol["price"] - dvol["startPrice"]
                if dvol.get("price") is not None and dvol.get("startPrice") is not None
                else None
            )
            for seconds in (15, 30, 60, 120):
                old_dvol = self._price_at(
                    self.deribit_dvol.prices, as_of_us - seconds * 1_000_000
                )
                deribit[f"dvolChange{seconds}s"] = (
                    dvol["price"] - old_dvol
                    if dvol.get("price") is not None and old_dvol is not None
                    else None
                )
            deribit.update(self._deribit_option_features(as_of_us, index_price))

            chainlink = self._venue_features(
                self.chainlink, start_us=start_us, as_of_us=as_of_us
            )
            chainlink["calculationRole"] = "polymarket_resolution_reference"
            polymarket_binance = self._venue_features(
                self.polymarket_binance, start_us=start_us, as_of_us=as_of_us
            )

            scores = {
                "binance": self._direction_score(binance, (
                    ("returnFromStart", 0.55), ("return30s", 0.20),
                    ("tradeImbalance60s", 0.0008), ("bookImbalance", 0.0002),
                )),
                "hyperliquid": self._direction_score(hyper, (
                    ("returnFromStart", 0.50), ("return30s", 0.20),
                    ("tradeImbalance60s", 0.0008), ("bookImbalance", 0.0002),
                    ("markOracleBasis", 0.10),
                )),
                "deribit": self._direction_score(deribit, (
                    ("returnFromStart", 0.65), ("perpetualReturnFromStart", 0.25),
                    ("perpetualBasis", 0.08), ("riskReversalProxy", 0.00002),
                )),
                "binanceFutures": self._direction_score(binance_futures, (
                    ("returnFromStart", 0.55), ("return30s", 0.20),
                    ("tradeImbalance60s", 0.0008), ("bookImbalance", 0.0002),
                )),
            }
            # Keep the existing consensus definition stable while collecting
            # futures as a separately testable feature.
            available_scores = [
                scores[key] for key in ("binance", "hyperliquid", "deribit")
                if scores[key] is not None
            ]
            scores["consensus"] = (
                sum(available_scores) / len(available_scores) if available_scores else None
            )
            signals = {
                key: ("UP" if value > 0 else "DOWN" if value < 0 else "NEUTRAL")
                if value is not None else "MISSING"
                for key, value in scores.items()
            }
            for key, features in (
                ("chainlink", chainlink),
                ("polymarketBinance", polymarket_binance),
            ):
                value = features.get("returnFromStart")
                signals[key] = (
                    "UP" if value is not None and value > 0
                    else "DOWN" if value is not None and value < 0
                    else "NEUTRAL" if value == 0
                    else "MISSING"
                )
            return {
                "schemaVersion": 1,
                "eventType": "CROSS_MARKET_FEATURE_SNAPSHOT",
                "marketId": market_id,
                "marketStartUs": str(start_us),
                "marketEndUs": str(end_us),
                "sourceTimeUs": str(as_of_us),
                "remainingSeconds": (end_us - as_of_us) / 1_000_000,
                "features": {
                    "binance": binance,
                    "binanceFutures": binance_futures,
                    "hyperliquid": hyper,
                    "deribit": deribit,
                    "chainlink": chainlink,
                    "polymarketBinance": polymarket_binance,
                },
                "scores": scores,
                "signals": signals,
            }

    def health(self, as_of_us: int) -> dict[str, Any]:
        with self._lock:
            def age(venue: VenueState) -> float | None:
                return (as_of_us - venue.updated_us) / 1_000 if venue.updated_us else None
            return {
                "messageCounts": dict(self.message_counts),
                "historyPoints": {
                    "binancePrices": len(self.binance.prices),
                    "binanceTrades": len(self.binance.trades),
                    "binanceFuturesPrices": len(self.binance_futures.prices),
                    "binanceFuturesTrades": len(self.binance_futures.trades),
                    "hyperliquidPrices": len(self.hyperliquid.prices),
                    "hyperliquidMarkPrices": len(self.hyperliquid_mark.prices),
                    "hyperliquidOraclePrices": len(self.hyperliquid_oracle.prices),
                    "hyperliquidTrades": len(self.hyperliquid.trades),
                    "deribitIndexPrices": len(self.deribit_index.prices),
                    "deribitPerpetualPrices": len(self.deribit_perpetual.prices),
                    "deribitOptionTickers": len(self.deribit_options),
                    "deribitDvolPoints": len(self.deribit_dvol.prices),
                    "deribitOptionTickerHistoryPoints": sum(
                        len(points) for points in self.deribit_option_history.values()
                    ),
                    "deribitOptionTrades": len(self.deribit_option_trades),
                    "chainlinkPrices": len(self.chainlink.prices),
                    "polymarketBinancePrices": len(self.polymarket_binance.prices),
                },
                "sourceAgeMs": {
                    "binance": age(self.binance),
                    "binanceFutures": age(self.binance_futures),
                    "hyperliquid": age(self.hyperliquid_mark),
                    "deribitIndex": age(self.deribit_index),
                    "deribitPerpetual": age(self.deribit_perpetual),
                    "deribitDvol": age(self.deribit_dvol),
                    "chainlink": age(self.chainlink),
                    "polymarketBinance": age(self.polymarket_binance),
                },
                "memoryWindowSeconds": self.memory_window_us // 1_000_000,
                "absoluteCaps": {
                    "pricePointsPerVenue": MAX_PRICE_POINTS,
                    "tradePointsPerVenue": MAX_TRADE_POINTS,
                    "optionTrades": MAX_OPTION_TRADES,
                    "optionTickerPointsPerInstrument": MAX_OPTION_TICKER_POINTS,
                },
            }
