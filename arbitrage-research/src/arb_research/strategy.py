from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .fees import taker_fee
from .models import Fill, MarketDefinition, MarketEvent
from .orderbook import OrderBookState


@dataclass(frozen=True)
class LastWindowThresholdConfig:
    strategy_id: str
    version: int
    window_seconds: int
    threshold_price: Decimal
    max_entry_price: Decimal
    order_size: Decimal
    min_ask_size: Decimal
    fee_model: str
    fee_rate: Decimal
    min_remaining_seconds: int = 0
    allowed_outcomes: tuple[str, ...] = ("UP", "DOWN")
    one_trade_per_market: bool = True

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> "LastWindowThresholdConfig":
        return cls(
            strategy_id=str(row.get("strategy_id", "prediction_last_window_threshold")),
            version=int(row.get("version", 1)),
            window_seconds=int(row["window_seconds"]),
            threshold_price=Decimal(str(row["threshold_price"])),
            max_entry_price=Decimal(str(row.get("max_entry_price", "0.99"))),
            order_size=Decimal(str(row.get("order_size", "1"))),
            min_ask_size=Decimal(str(row.get("min_ask_size", "0"))),
            fee_model=str(row.get("fee_model", "none")),
            fee_rate=Decimal(str(row.get("fee_rate", "0"))),
            min_remaining_seconds=int(row.get("min_remaining_seconds", 0)),
            allowed_outcomes=tuple(row.get("allowed_outcomes") or ("UP", "DOWN")),
            one_trade_per_market=bool(row.get("one_trade_per_market", True)),
        )


@dataclass
class LastWindowThresholdStrategy:
    config: LastWindowThresholdConfig

    def maybe_fill(
        self,
        market: MarketDefinition,
        book: OrderBookState,
        event: MarketEvent,
        already_filled: bool,
    ) -> Fill | None:
        if event.event_type not in {"PRICE_QUOTE", "ORDERBOOK_SNAPSHOT", "ORDERBOOK_DELTA"}:
            return None
        if self.config.one_trade_per_market and already_filled:
            return None

        remaining_us = market.event_end_us - event.source_time_us
        if remaining_us < 0:
            return None
        if remaining_us < self.config.min_remaining_seconds * 1_000_000:
            return None
        if remaining_us > self.config.window_seconds * 1_000_000:
            return None

        candidate_outcomes = self._candidate_outcomes(event)
        for outcome in candidate_outcomes:
            quote = book.quote(outcome)
            if quote is None or quote.best_ask is None:
                continue
            ask_size = quote.best_ask_size or Decimal("0")
            if ask_size < self.config.min_ask_size:
                continue
            if quote.best_ask < self.config.threshold_price:
                continue
            if quote.best_ask > self.config.max_entry_price:
                continue

            quantity = min(self.config.order_size, ask_size)
            fee = taker_fee(self.config.fee_model, quote.best_ask, quantity, self.config.fee_rate)
            gross_cost = quote.best_ask * quantity + fee
            return Fill(
                venue=market.venue,
                market_id=market.market_id,
                outcome=outcome,
                source_time_us=event.source_time_us,
                remaining_seconds=Decimal(remaining_us) / Decimal("1000000"),
                entry_price=quote.best_ask,
                quantity=quantity,
                fee=fee,
                gross_cost=gross_cost,
            )

        return None

    def _candidate_outcomes(self, event: MarketEvent) -> tuple[str, ...]:
        """Only evaluate outcomes refreshed by the current market event.

        PRICE_QUOTE and ORDERBOOK_DELTA update one outcome at a time. Looking at
        every cached outcome here can trade a stale quote and also gives the
        first configured outcome an artificial priority. A snapshot refreshes
        all outcomes atomically, so all configured outcomes remain eligible.
        """
        if event.event_type in {"PRICE_QUOTE", "ORDERBOOK_DELTA"}:
            updated_outcome = event.outcome or event.payload.get("outcome")
            if updated_outcome is None:
                return ()
            outcome = str(updated_outcome)
            return (outcome,) if outcome in self.config.allowed_outcomes else ()

        snapshot_outcomes = {
            str(item.get("outcome"))
            for item in event.payload.get("outcomes") or []
            if item.get("outcome") is not None
        }
        return tuple(
            outcome
            for outcome in self.config.allowed_outcomes
            if outcome in snapshot_outcomes
        )
