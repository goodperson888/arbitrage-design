from __future__ import annotations

import json
import gzip
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from .models import Fill, MarketEvent


def read_events(path: Path) -> Iterator[MarketEvent]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                row = json.loads(stripped)
                event = MarketEvent.from_dict(row)
            except Exception as exc:  # noqa: BLE001 - include file context for bad data.
                raise ValueError(f"{path}:{line_number}: invalid event: {exc}") from exc
            if event.schema_version != 1:
                raise ValueError(f"{path}:{line_number}: unsupported schemaVersion {event.schema_version}")
            yield event


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def write_fills(path: Path, fills: Iterable[Fill]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for fill in fills:
            handle.write(json.dumps(fill.to_dict(), ensure_ascii=False, sort_keys=True))
            handle.write("\n")


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_events(path: Path, events: Iterable[MarketEvent]) -> None:
    """Write MarketEvent objects to NDJSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for event in events:
            # Convert MarketEvent to dict (reverse of from_dict)
            event_dict = {
                "schemaVersion": event.schema_version,
                "eventId": event.event_id,
                "eventType": event.event_type,
                "venue": event.venue,
                "marketId": event.market_id,
                "instrumentId": event.instrument_id,
                "outcome": event.outcome,
                "sourceTimeUs": event.source_time_us,
                "receivedTimeUs": event.received_time_us,
                "sequence": event.sequence,
                "source": event.source,
                "payload": event.payload,
            }
            # Remove None values
            event_dict = {k: v for k, v in event_dict.items() if v is not None}
            handle.write(json.dumps(event_dict, ensure_ascii=False, sort_keys=True))
            handle.write("\n")
