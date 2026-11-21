from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable

from .models import Fill, MarketDefinition, MarketEvent
from .orderbook import OrderBookState
from .strategy import LastWindowThresholdStrategy


@dataclass
class MarketReplayState:
    definition: MarketDefinition | None = None
    book: OrderBookState = field(default_factory=OrderBookState)
    fills: list[Fill] = field(default_factory=list)


@dataclass
class BacktestResult:
    fills: list[Fill]
    metrics: dict[str, object]


def run_backtest(
    events: Iterable[MarketEvent],
    strategy: LastWindowThresholdStrategy,
    *,
    events_are_sorted: bool = False,
) -> BacktestResult:
    states: dict[str, MarketReplayState] = {}
    fills: list[Fill] = []

    ordered_events = events if events_are_sorted else sorted(
        events,
        key=lambda item: (item.source_time_us, item.received_time_us, item.sequence or ""),
    )
    for event in ordered_events:
        state = states.setdefault(event.market_id, MarketReplayState())

        if event.event_type == "MARKET_DEFINITION":
            state.definition = MarketDefinition.from_event(event)
            continue

        if event.event_type in {"PRICE_QUOTE", "ORDERBOOK_SNAPSHOT", "ORDERBOOK_DELTA"}:
            state.book.apply(event)
            if state.definition is None:
                continue
            fill = strategy.maybe_fill(
                market=state.definition,
                book=state.book,
                event=event,
                already_filled=bool(state.fills),
            )
            if fill is not None:
                state.fills.append(fill)
                fills.append(fill)
            continue

        if event.event_type == "SETTLEMENT":
            winner = event.payload.get("winnerOutcome")
            if winner is None:
                continue
            for fill in state.fills:
                if fill.winner_outcome is not None:
                    continue
                fill.winner_outcome = str(winner)
                fill.payout = fill.quantity if fill.outcome == winner else Decimal("0")
                fill.net_pnl = fill.payout - fill.gross_cost

    return BacktestResult(fills=fills, metrics=build_metrics(fills))


def build_metrics(fills: list[Fill]) -> dict[str, object]:
    settled = [fill for fill in fills if fill.settled]
    wins = [fill for fill in settled if fill.won]

    total_quantity = sum((fill.quantity for fill in fills), Decimal("0"))
    total_gross_cost = sum((fill.gross_cost for fill in fills), Decimal("0"))
    total_fee = sum((fill.fee for fill in fills), Decimal("0"))
    total_net_pnl = sum((fill.net_pnl or Decimal("0") for fill in settled), Decimal("0"))
    total_payout = sum((fill.payout or Decimal("0") for fill in settled), Decimal("0"))

    avg_entry_price = _weighted_average(
        [(fill.entry_price, fill.quantity) for fill in fills],
    )
    avg_fee_per_share = (total_fee / total_quantity) if total_quantity else Decimal("0")
    breakeven_win_rate = avg_entry_price + avg_fee_per_share
    win_rate = Decimal(len(wins)) / Decimal(len(settled)) if settled else Decimal("0")
    net_pnl_per_share = (total_net_pnl / total_quantity) if total_quantity else Decimal("0")

    return {
        "fillCount": len(fills),
        "settledFillCount": len(settled),
        "winCount": len(wins),
        "winRate": _fmt(win_rate),
        "averageEntryPrice": _fmt(avg_entry_price),
        "averageFeePerShare": _fmt(avg_fee_per_share),
        "breakevenWinRate": _fmt(breakeven_win_rate),
        "totalQuantity": _fmt(total_quantity),
        "totalGrossCost": _fmt(total_gross_cost),
        "totalPayout": _fmt(total_payout),
        "totalNetPnl": _fmt(total_net_pnl),
        "netPnlPerShare": _fmt(net_pnl_per_share),
        "unsettledFillCount": len(fills) - len(settled),
    }


def _weighted_average(items: list[tuple[Decimal, Decimal]]) -> Decimal:
    total_weight = sum((weight for _, weight in items), Decimal("0"))
    if not total_weight:
        return Decimal("0")
    return sum((value * weight for value, weight in items), Decimal("0")) / total_weight


def _fmt(value: Decimal) -> str:
    return format(value.normalize(), "f")
