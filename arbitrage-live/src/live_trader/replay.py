from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Tuple

from .adapters import normalize_binance_kline, normalize_polymarket_message


@dataclass(frozen=True)
class ReplayResult:
    normalized_count: int
    order_attempts: int
    events: Tuple[Any, ...]


class ReadOnlyReplay:
    """Replay recorded market data without constructing or submitting orders."""

    def run(self, messages: Iterable[tuple[str, Mapping[str, Any]]]) -> ReplayResult:
        events = []
        for venue, message in messages:
            if venue == "polymarket":
                events.append(normalize_polymarket_message(message))
            elif venue == "binance":
                events.append(normalize_binance_kline(message))
            else:
                raise ValueError(f"unsupported replay venue: {venue}")
        return ReplayResult(len(events), 0, tuple(events))

