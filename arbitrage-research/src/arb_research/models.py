from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


def decimal_or_none(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def int_us(value: Any) -> int:
    if value is None:
        raise ValueError("timestamp is required")
    return int(value)


@dataclass(frozen=True)
class MarketEvent:
    schema_version: int
    event_type: str
    venue: str
    market_id: str
    source_time_us: int
    received_time_us: int
    payload: dict[str, Any]
    event_id: str | None = None
    instrument_id: str | None = None
    outcome: str | None = None
    sequence: str | None = None
    source: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> "MarketEvent":
        return cls(
            schema_version=int(row["schemaVersion"]),
            event_id=row.get("eventId"),
            event_type=str(row["eventType"]),
            venue=str(row["venue"]),
            market_id=str(row["marketId"]),
            instrument_id=row.get("instrumentId"),
            outcome=row.get("outcome"),
            source_time_us=int_us(row["sourceTimeUs"]),
            received_time_us=int_us(row["receivedTimeUs"]),
            sequence=str(row["sequence"]) if row.get("sequence") is not None else None,
            source=dict(row.get("source") or {}),
            payload=dict(row.get("payload") or {}),
        )


@dataclass
class MarketDefinition:
    venue: str
    market_id: str
    event_start_us: int
    event_end_us: int
    underlying: str | None = None
    outcomes: tuple[str, ...] = ("UP", "DOWN")

    @classmethod
    def from_event(cls, event: MarketEvent) -> "MarketDefinition":
        payload = event.payload
        return cls(
            venue=event.venue,
            market_id=event.market_id,
            event_start_us=int_us(payload["eventStartUs"]),
            event_end_us=int_us(payload["eventEndUs"]),
            underlying=payload.get("underlying"),
            outcomes=tuple(payload.get("outcomes") or ("UP", "DOWN")),
        )


@dataclass
class Quote:
    outcome: str
    best_bid: Decimal | None = None
    best_bid_size: Decimal | None = None
    best_ask: Decimal | None = None
    best_ask_size: Decimal | None = None


@dataclass
class Fill:
    venue: str
    market_id: str
    outcome: str
    source_time_us: int
    remaining_seconds: Decimal
    entry_price: Decimal
    quantity: Decimal
    fee: Decimal
    gross_cost: Decimal
    winner_outcome: str | None = None
    payout: Decimal | None = None
    net_pnl: Decimal | None = None

    @property
    def settled(self) -> bool:
        return self.winner_outcome is not None

    @property
    def won(self) -> bool:
        return self.winner_outcome == self.outcome

    def to_dict(self) -> dict[str, str | int | bool | None]:
        return {
            "venue": self.venue,
            "marketId": self.market_id,
            "outcome": self.outcome,
            "sourceTimeUs": str(self.source_time_us),
            "remainingSeconds": str(self.remaining_seconds),
            "entryPrice": str(self.entry_price),
            "quantity": str(self.quantity),
            "fee": str(self.fee),
            "grossCost": str(self.gross_cost),
            "winnerOutcome": self.winner_outcome,
            "won": self.won if self.settled else None,
            "payout": str(self.payout) if self.payout is not None else None,
            "netPnl": str(self.net_pnl) if self.net_pnl is not None else None,
        }
