from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
from statistics import median
from typing import Iterable

from ..fees import taker_fee
from .candidate_stress import MarketTradeSeries, wilson_interval


@dataclass(frozen=True)
class BookLevel:
    price: Decimal
    size: Decimal


@dataclass(frozen=True)
class BookSnapshot:
    time_us: int
    asks: dict[str, tuple[BookLevel, ...]]


@dataclass
class BookMarket:
    market_id: str
    end_us: int
    winner: str
    snapshots: list[BookSnapshot]


@dataclass(frozen=True)
class DepthCase:
    case_id: str
    price_min: Decimal
    price_max: Decimal
    outcome: str
    quantity: Decimal
    max_remaining_seconds: int = 90
    min_remaining_seconds: int = 0
    delay_seconds: int = 0
    path: str = "any"
    path_lookback_seconds: int = 10


@dataclass(frozen=True)
class DepthFill:
    market_id: str
    winner: str
    outcome: str
    quantity: Decimal
    signal_time_us: int
    execution_time_us: int
    remaining_seconds: Decimal
    vwap: Decimal
    worst_price: Decimal
    fee_per_share: Decimal

    @property
    def won(self) -> bool:
        return self.outcome == self.winner

    @property
    def pnl_per_share(self) -> Decimal:
        payout = Decimal("1") if self.won else Decimal("0")
        return payout - self.vwap - self.fee_per_share


def has_book_coverage(
    market: BookMarket,
    *,
    outcome: str | None = None,
    max_remaining_seconds: int = 90,
    min_remaining_seconds: int = 0,
    max_gap_seconds: Decimal = Decimal("5"),
) -> bool:
    band_start = market.end_us - max_remaining_seconds * 1_000_000
    band_end = market.end_us - min_remaining_seconds * 1_000_000
    tolerance_us = int(max_gap_seconds * Decimal("1000000"))
    outcomes = (outcome,) if outcome is not None else ("UP", "DOWN")
    times = [
        snapshot.time_us
        for snapshot in market.snapshots
        if band_start - tolerance_us <= snapshot.time_us <= band_end + tolerance_us
        and all(snapshot.asks.get(item) for item in outcomes)
    ]
    if not times:
        return False
    if times[0] > band_start + tolerance_us or times[-1] < band_end - tolerance_us:
        return False
    relevant = [time_us for time_us in times if band_start <= time_us <= band_end]
    boundary_times = [band_start, *relevant, band_end]
    return all(
        later - earlier <= tolerance_us
        for earlier, later in zip(boundary_times, boundary_times[1:])
    )


def load_book_markets(
    paths: Iterable[Path],
    official_markets: dict[str, MarketTradeSeries],
) -> dict[str, BookMarket]:
    # Multiple downloads can contain the same market and snapshot. Keep one
    # observation per source timestamp so duplicated files cannot add weight.
    snapshots: dict[str, dict[int, BookSnapshot]] = {}
    raw_metadata: dict[str, tuple[int, str]] = {}
    for path in sorted(paths):
        payload = json.loads(path.read_text())
        if not isinstance(payload, dict) or "market" not in payload or "snapshots" not in payload:
            continue
        metadata = payload["market"]
        market_id = str(metadata["slug"])
        raw_metadata[market_id] = (
            _iso_to_us(str(metadata["end_time"])),
            str(metadata.get("winner", "")).upper(),
        )
        market_snapshots = snapshots.setdefault(market_id, {})
        for raw in payload.get("snapshots", []):
            time_us = _iso_to_us(str(raw["time"]))
            asks: dict[str, tuple[BookLevel, ...]] = {}
            for outcome, key in (("UP", "orderbook_up"), ("DOWN", "orderbook_down")):
                levels = tuple(
                    sorted(
                        (
                            BookLevel(
                                price=Decimal(str(level["price"])),
                                size=Decimal(str(level["size"])),
                            )
                            for level in raw.get(key, {}).get("asks", [])
                            if Decimal(str(level["size"])) > 0
                        ),
                        key=lambda level: level.price,
                    )
                )
                asks[outcome] = levels
            market_snapshots[time_us] = BookSnapshot(time_us=time_us, asks=asks)

    result: dict[str, BookMarket] = {}
    for market_id, by_time in snapshots.items():
        official = official_markets.get(market_id)
        raw_end_us, raw_winner = raw_metadata[market_id]
        winner = official.winner if official is not None else raw_winner
        end_us = official.end_us if official is not None else raw_end_us
        if winner not in {"UP", "DOWN"}:
            continue
        result[market_id] = BookMarket(
            market_id=market_id,
            end_us=end_us,
            winner=winner,
            snapshots=sorted(by_time.values(), key=lambda snapshot: snapshot.time_us),
        )
    return result


def consume_asks(
    asks: Iterable[BookLevel],
    quantity: Decimal,
    price_cap: Decimal,
    *,
    fee_model: str = "polymarket_crypto_taker",
    fee_rate: Decimal = Decimal("0.07"),
) -> tuple[Decimal, Decimal, Decimal] | None:
    remaining = quantity
    notional = Decimal("0")
    total_fee = Decimal("0")
    worst_price = Decimal("0")
    for level in sorted(asks, key=lambda item: item.price):
        if level.price > price_cap:
            break
        take = min(remaining, level.size)
        if take <= 0:
            continue
        notional += take * level.price
        total_fee += taker_fee(fee_model, level.price, take, fee_rate)
        worst_price = level.price
        remaining -= take
        if remaining <= 0:
            return notional / quantity, total_fee / quantity, worst_price
    return None


def consume_bids(
    bids: Iterable[BookLevel],
    quantity: Decimal,
    price_floor: Decimal,
    *,
    fee_model: str = "polymarket_crypto_taker",
    fee_rate: Decimal = Decimal("0.07"),
) -> tuple[Decimal, Decimal, Decimal] | None:
    """Simulate an IOC sale into bids at or above ``price_floor``.

    Returns sale VWAP, fee per share, and the worst bid consumed.  ``None``
    means the displayed executable depth cannot sell the full quantity.
    """
    remaining = quantity
    notional = Decimal("0")
    total_fee = Decimal("0")
    worst_price = Decimal("1")
    for level in sorted(bids, key=lambda item: item.price, reverse=True):
        if level.price < price_floor:
            break
        take = min(remaining, level.size)
        if take <= 0:
            continue
        notional += take * level.price
        total_fee += taker_fee(fee_model, level.price, take, fee_rate)
        worst_price = level.price
        remaining -= take
        if remaining <= 0:
            return notional / quantity, total_fee / quantity, worst_price
    return None


def run_depth_case(
    markets: dict[str, BookMarket],
    case: DepthCase,
    *,
    fee_model: str = "polymarket_crypto_taker",
    fee_rate: Decimal = Decimal("0.07"),
) -> dict[str, object]:
    fills = [
        fill
        for market in markets.values()
        if (fill := _find_fill(market, case, fee_model, fee_rate)) is not None
    ]
    count = len(fills)
    wins = sum(fill.won for fill in fills)
    avg_vwap = _average((fill.vwap for fill in fills), count)
    avg_fee = _average((fill.fee_per_share for fill in fills), count)
    avg_pnl = _average((fill.pnl_per_share for fill in fills), count)
    remaining = [fill.remaining_seconds for fill in fills]
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
        "averageVwap": _fmt(avg_vwap),
        "averageFeePerShare": _fmt(avg_fee),
        "breakevenWinRate": _fmt(avg_vwap + avg_fee),
        "netPnlPerShare": _fmt(avg_pnl),
        "averageRemainingSeconds": _fmt(_average(iter(remaining), count)),
        "medianRemainingSeconds": _fmt(
            Decimal(str(median(remaining))) if remaining else Decimal("0")
        ),
    }
    return row


def _find_fill(
    market: BookMarket,
    case: DepthCase,
    fee_model: str,
    fee_rate: Decimal,
) -> DepthFill | None:
    band_start = market.end_us - case.max_remaining_seconds * 1_000_000
    band_end = market.end_us - case.min_remaining_seconds * 1_000_000
    previous_best: Decimal | None = None
    previous_time_us: int | None = None
    snapshots = market.snapshots

    for index, snapshot in enumerate(snapshots):
        asks = snapshot.asks.get(case.outcome, ())
        best = asks[0].price if asks else None
        if snapshot.time_us < band_start:
            if best is not None:
                previous_best = best
                previous_time_us = snapshot.time_us
            continue
        if snapshot.time_us > band_end:
            break
        if best is None:
            continue
        inside = case.price_min <= best <= case.price_max
        path_ok = _path_matches(
            previous_best,
            previous_time_us,
            best,
            snapshot.time_us,
            case,
        )
        previous_best = best
        previous_time_us = snapshot.time_us
        if not inside or not path_ok:
            continue

        target_us = snapshot.time_us + case.delay_seconds * 1_000_000
        execution = _first_snapshot_at_or_after(snapshots, index, target_us, band_end)
        if execution is None:
            return None
        consumed = consume_asks(
            execution.asks.get(case.outcome, ()),
            case.quantity,
            case.price_max,
            fee_model=fee_model,
            fee_rate=fee_rate,
        )
        if consumed is None:
            # This models an immediate-or-cancel order at the requested size.
            return None
        vwap, fee_per_share, worst_price = consumed
        return DepthFill(
            market_id=market.market_id,
            winner=market.winner,
            outcome=case.outcome,
            quantity=case.quantity,
            signal_time_us=snapshot.time_us,
            execution_time_us=execution.time_us,
            remaining_seconds=Decimal(market.end_us - execution.time_us) / Decimal("1000000"),
            vwap=vwap,
            worst_price=worst_price,
            fee_per_share=fee_per_share,
        )
    return None


def _path_matches(
    previous: Decimal | None,
    previous_time_us: int | None,
    current: Decimal,
    current_time_us: int,
    case: DepthCase,
) -> bool:
    if case.path == "any":
        return True
    if previous is None or previous_time_us is None:
        return False
    if current_time_us - previous_time_us > case.path_lookback_seconds * 1_000_000:
        return False
    if case.path == "up_cross":
        return previous < case.price_min <= current
    if case.path == "down_cross":
        return previous > case.price_max >= current
    raise ValueError(f"unsupported path: {case.path}")


def _first_snapshot_at_or_after(
    snapshots: list[BookSnapshot],
    start_index: int,
    target_us: int,
    band_end_us: int,
) -> BookSnapshot | None:
    for snapshot in snapshots[start_index:]:
        if snapshot.time_us < target_us:
            continue
        return snapshot if snapshot.time_us <= band_end_us else None
    return None


def _iso_to_us(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return int(parsed.timestamp() * 1_000_000)


def _average(values: Iterable[Decimal], count: int) -> Decimal:
    return sum(values, Decimal("0")) / count if count else Decimal("0")


def _ratio(numerator: int, denominator: int) -> str:
    return _fmt(Decimal(numerator) / Decimal(denominator)) if denominator else "0"


def _fmt(value: Decimal) -> str:
    return format(value, "f")
