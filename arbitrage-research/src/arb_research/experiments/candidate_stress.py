from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
import math
from pathlib import Path
from statistics import median
from typing import Iterable

from ..fees import taker_fee_per_share
from ..io import read_events
from ..models import MarketEvent


@dataclass(frozen=True)
class TradeTick:
    time_us: int
    price: Decimal
    size: Decimal


@dataclass
class MarketTradeSeries:
    market_id: str
    end_us: int
    winner: str
    trades: dict[str, list[TradeTick]]


@dataclass(frozen=True)
class StressCase:
    case_id: str
    price_min: Decimal
    price_max: Decimal
    outcome: str
    max_remaining_seconds: int = 90
    min_remaining_seconds: int = 0
    path: str = "any"
    consecutive_trades: int = 1
    hold_seconds: int = 0
    delay_seconds: int = 0
    min_trade_size: Decimal = Decimal("1")
    execution_price_max: Decimal | None = None
    path_lookback_seconds: int = 10


@dataclass(frozen=True)
class SimulatedFill:
    market_id: str
    outcome: str
    winner: str
    signal_time_us: int
    execution_time_us: int
    remaining_seconds: Decimal
    price: Decimal
    fee: Decimal

    @property
    def won(self) -> bool:
        return self.outcome == self.winner

    @property
    def pnl(self) -> Decimal:
        return (Decimal("1") if self.won else Decimal("0")) - self.price - self.fee


def load_trade_series(path: Path) -> dict[str, MarketTradeSeries]:
    return build_trade_series(read_events(path))


def build_trade_series(events: Iterable[MarketEvent]) -> dict[str, MarketTradeSeries]:
    end_times: dict[str, int] = {}
    winners: dict[str, str] = {}
    trades: dict[str, dict[str, list[TradeTick]]] = {}
    for event in events:
        if event.event_type == "MARKET_DEFINITION":
            end_times[event.market_id] = int(event.payload["eventEndUs"])
        elif event.event_type == "SETTLEMENT":
            winner = event.payload.get("winnerOutcome")
            if winner is not None:
                winners[event.market_id] = str(winner)
        elif event.event_type == "TRADE":
            if str(event.payload.get("side", "")).upper() != "BUY":
                continue
            outcome = str(event.outcome or event.payload.get("outcome") or "")
            if outcome not in {"UP", "DOWN"}:
                continue
            trades.setdefault(event.market_id, {}).setdefault(outcome, []).append(
                TradeTick(
                    time_us=event.source_time_us,
                    price=Decimal(str(event.payload["price"])),
                    size=Decimal(str(event.payload["size"])),
                )
            )

    result: dict[str, MarketTradeSeries] = {}
    for market_id in sorted(set(end_times) & set(winners)):
        by_outcome = trades.get(market_id, {})
        for values in by_outcome.values():
            values.sort(key=lambda tick: tick.time_us)
        result[market_id] = MarketTradeSeries(
            market_id=market_id,
            end_us=end_times[market_id],
            winner=winners[market_id],
            trades=by_outcome,
        )
    return result


def run_stress_case(
    markets: dict[str, MarketTradeSeries],
    case: StressCase,
    *,
    fee_model: str = "polymarket_crypto_taker",
    fee_rate: Decimal = Decimal("0.07"),
) -> dict[str, object]:
    fills: list[SimulatedFill] = []
    for market in markets.values():
        fill = _find_fill(market, case, fee_model, fee_rate)
        if fill is not None:
            fills.append(fill)

    wins = sum(fill.won for fill in fills)
    count = len(fills)
    prices = [fill.price for fill in fills]
    remaining = [fill.remaining_seconds for fill in fills]
    avg_price = sum(prices, Decimal("0")) / count if count else Decimal("0")
    avg_fee = sum((fill.fee for fill in fills), Decimal("0")) / count if count else Decimal("0")
    pnl = sum((fill.pnl for fill in fills), Decimal("0")) / count if count else Decimal("0")
    ci_low, ci_high = wilson_interval(wins, count)
    row: dict[str, object] = {
        **{
            key: _fmt(value) if isinstance(value, Decimal) else value
            for key, value in asdict(case).items()
        },
        "marketCount": len(markets),
        "fillCount": count,
        "winCount": wins,
        "winRate": _ratio(wins, count),
        "winRateCi95Low": ci_low,
        "winRateCi95High": ci_high,
        "opportunityRate": _ratio(count, len(markets)),
        "averageEntryPrice": _fmt(avg_price),
        "averageFeePerShare": _fmt(avg_fee),
        "breakevenWinRate": _fmt(avg_price + avg_fee),
        "netPnlPerShare": _fmt(pnl),
        "averageRemainingSeconds": _fmt(
            sum(remaining, Decimal("0")) / count if count else Decimal("0")
        ),
        "medianRemainingSeconds": _fmt(
            Decimal(str(median(remaining))) if remaining else Decimal("0")
        ),
    }
    return row


def _find_fill(
    market: MarketTradeSeries,
    case: StressCase,
    fee_model: str,
    fee_rate: Decimal,
) -> SimulatedFill | None:
    values = market.trades.get(case.outcome, [])
    if not values:
        return None
    band_start = market.end_us - case.max_remaining_seconds * 1_000_000
    band_end = market.end_us - case.min_remaining_seconds * 1_000_000
    active = False
    active_start_us = 0
    run_count = 0
    execution_cap = case.execution_price_max or case.price_max

    for index, tick in enumerate(values):
        if tick.time_us < band_start:
            continue
        if tick.time_us > band_end:
            break
        inside = case.price_min <= tick.price <= case.price_max
        if not inside:
            active = False
            run_count = 0
            continue

        if not active:
            previous = values[index - 1] if index else None
            if not _path_matches(previous, tick, case):
                continue
            active = True
            active_start_us = tick.time_us
            run_count = 1
        else:
            run_count += 1

        held_us = tick.time_us - active_start_us
        if run_count < case.consecutive_trades:
            continue
        if held_us < case.hold_seconds * 1_000_000:
            continue

        target_time = tick.time_us + case.delay_seconds * 1_000_000
        execution = _execution_tick(
            values,
            index,
            target_time,
            band_end,
            case.price_min,
            execution_cap,
            case.min_trade_size,
        )
        if execution is None:
            return None
        fee = taker_fee_per_share(fee_model, execution.price, fee_rate)
        return SimulatedFill(
            market_id=market.market_id,
            outcome=case.outcome,
            winner=market.winner,
            signal_time_us=tick.time_us,
            execution_time_us=execution.time_us,
            remaining_seconds=(Decimal(market.end_us - execution.time_us) / Decimal("1000000")),
            price=execution.price,
            fee=fee,
        )
    return None


def _path_matches(previous: TradeTick | None, current: TradeTick, case: StressCase) -> bool:
    if case.path == "any":
        return True
    if previous is None:
        return False
    if current.time_us - previous.time_us > case.path_lookback_seconds * 1_000_000:
        return False
    if case.path == "up_cross":
        return previous.price < case.price_min <= current.price
    if case.path == "down_cross":
        return previous.price > case.price_max >= current.price
    raise ValueError(f"unsupported path: {case.path}")


def _execution_tick(
    values: list[TradeTick],
    confirmation_index: int,
    target_time_us: int,
    band_end_us: int,
    price_min: Decimal,
    price_max: Decimal,
    min_trade_size: Decimal,
) -> TradeTick | None:
    for tick in values[confirmation_index:]:
        if tick.time_us < target_time_us:
            continue
        if tick.time_us > band_end_us:
            return None
        # A delayed limit order is cancelled if the first observable execution
        # is no longer inside the permitted price band or lacks enough size.
        if not (price_min <= tick.price <= price_max):
            return None
        if tick.size < min_trade_size:
            return None
        return tick
    return None


def wilson_interval(wins: int, total: int) -> tuple[str | None, str | None]:
    if total == 0:
        return None, None
    z = 1.959963984540054
    proportion = wins / total
    denominator = 1 + z * z / total
    center = (proportion + z * z / (2 * total)) / denominator
    half_width = z * math.sqrt(
        proportion * (1 - proportion) / total + z * z / (4 * total * total)
    ) / denominator
    return f"{max(0.0, center - half_width):.6f}", f"{min(1.0, center + half_width):.6f}"


def _ratio(numerator: int, denominator: int) -> str:
    return _fmt(Decimal(numerator) / Decimal(denominator)) if denominator else "0"


def _fmt(value: Decimal) -> str:
    return format(value.normalize(), "f")
