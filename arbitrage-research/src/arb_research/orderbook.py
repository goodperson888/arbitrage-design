from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .models import MarketEvent, Quote, decimal_or_none


def _levels(items: list[list[str]] | list[tuple[str, str]] | None) -> dict[Decimal, Decimal]:
    out: dict[Decimal, Decimal] = {}
    for item in items or []:
        if len(item) != 2:
            continue
        price = Decimal(str(item[0]))
        size = Decimal(str(item[1]))
        if size > 0:
            out[price] = size
    return out


@dataclass
class OutcomeBook:
    bids: dict[Decimal, Decimal] = field(default_factory=dict)
    asks: dict[Decimal, Decimal] = field(default_factory=dict)
    quote_override: Quote | None = None

    def best_quote(self, outcome: str) -> Quote:
        if self.quote_override is not None:
            return self.quote_override

        best_bid = max(self.bids) if self.bids else None
        best_ask = min(self.asks) if self.asks else None
        return Quote(
            outcome=outcome,
            best_bid=best_bid,
            best_bid_size=self.bids.get(best_bid) if best_bid is not None else None,
            best_ask=best_ask,
            best_ask_size=self.asks.get(best_ask) if best_ask is not None else None,
        )


@dataclass
class OrderBookState:
    outcomes: dict[str, OutcomeBook] = field(default_factory=dict)

    def apply(self, event: MarketEvent) -> None:
        if event.event_type == "PRICE_QUOTE":
            self._apply_price_quote(event)
        elif event.event_type == "ORDERBOOK_SNAPSHOT":
            self._apply_snapshot(event)
        elif event.event_type == "ORDERBOOK_DELTA":
            self._apply_delta(event)

    def quote(self, outcome: str) -> Quote | None:
        book = self.outcomes.get(outcome)
        if book is None:
            return None
        return book.best_quote(outcome)

    def _book(self, outcome: str) -> OutcomeBook:
        return self.outcomes.setdefault(outcome, OutcomeBook())

    def _apply_price_quote(self, event: MarketEvent) -> None:
        outcome = event.outcome or event.payload.get("outcome")
        if not outcome:
            raise ValueError("PRICE_QUOTE requires outcome")
        self._book(str(outcome)).quote_override = Quote(
            outcome=str(outcome),
            best_bid=decimal_or_none(event.payload.get("bestBid")),
            best_bid_size=decimal_or_none(event.payload.get("bestBidSize")),
            best_ask=decimal_or_none(event.payload.get("bestAsk")),
            best_ask_size=decimal_or_none(event.payload.get("bestAskSize")),
        )

    def _apply_snapshot(self, event: MarketEvent) -> None:
        for item in event.payload.get("outcomes") or []:
            outcome = str(item["outcome"])
            self.outcomes[outcome] = OutcomeBook(
                bids=_levels(item.get("bids")),
                asks=_levels(item.get("asks")),
            )

    def _apply_delta(self, event: MarketEvent) -> None:
        outcome = str(event.outcome or event.payload.get("outcome"))
        side = str(event.payload["side"]).upper()
        price = Decimal(str(event.payload["price"]))
        size = Decimal(str(event.payload["size"]))
        book = self._book(outcome)
        book.quote_override = None
        levels = book.asks if side == "ASK" else book.bids
        if size <= 0:
            levels.pop(price, None)
        else:
            levels[price] = size
