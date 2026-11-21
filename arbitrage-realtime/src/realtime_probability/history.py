"""Small, append-only recorder for sparse real-time fact snapshots.

The recorder deliberately stores one compact row per market and time bucket,
not every WebSocket message.  Rows are keyed by ``market_id`` and can later be
joined with an official settlement-label file for factor hit-rate analysis.
"""

from __future__ import annotations

import json
from pathlib import Path
from threading import RLock
from typing import Any


class LightweightFactRecorder:
    """Persist sparse, strategy-agnostic feature snapshots.

    This class never invents a settlement result.  It records only facts known
    at observation time; official outcome labels can be joined later by
    ``market_id`` without mixing them into the real-time path.
    """

    FIELDS = (
        "market_id",
        "observed_at",
        "remaining_seconds",
        "current_price",
        "open_price",
        "distance_from_open_bps",
        "range_bps",
        "return_1s_bps",
        "return_3s_bps",
        "return_5s_bps",
        "return_10s_bps",
        "best_bid",
        "best_ask",
        "best_bid_size",
        "best_ask_size",
        "book_imbalance",
        "spread_bps",
        "microprice_edge_bps",
        "trade_imbalance_60s",
        "taker_buy_ratio_60s",
        "trade_count_60s",
        "trade_rate_10s",
        "probability_up",
        "probability_down",
        "signal",
        "data_quality",
    )

    def __init__(self, path: str | Path, interval_seconds: float = 10.0) -> None:
        self.path = Path(path).expanduser()
        self.interval_seconds = max(1.0, float(interval_seconds))
        self._last_bucket: dict[str, int] = {}
        self._lock = RLock()
        self.records_written = 0

    def record(self, result: dict[str, Any]) -> bool:
        market_id = str(result.get("market_id") or "")
        observed_at = result.get("observed_at")
        if not market_id or observed_at is None:
            return False
        try:
            bucket = int(float(observed_at) // self.interval_seconds)
        except (TypeError, ValueError, OverflowError):
            return False
        with self._lock:
            if self._last_bucket.get(market_id) == bucket:
                return False
            row = {field: result.get(field) for field in self.FIELDS}
            row["recorded_at"] = float(observed_at)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            self._last_bucket[market_id] = bucket
            self.records_written += 1
            return True

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "path": str(self.path),
                "interval_seconds": self.interval_seconds,
                "records_written": self.records_written,
            }
