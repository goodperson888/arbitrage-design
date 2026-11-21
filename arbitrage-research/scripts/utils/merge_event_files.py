#!/usr/bin/env python3
"""Merge normalized NDJSON event files with collision checks and de-duplication."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Merge MarketEvent NDJSON files")
    parser.add_argument("--inputs", required=True, nargs="+", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    input_paths = {path.resolve() for path in args.inputs}
    if args.out.resolve() in input_paths:
        parser.error("--out must differ from every input")

    events_by_id: dict[str, dict[str, object]] = {}
    duplicate_count = 0
    market_ids: set[str] = set()
    input_event_counts: dict[str, int] = {}

    for path in args.inputs:
        count = 0
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                count += 1
                event = json.loads(line)
                event_id = str(event["eventId"])
                previous = events_by_id.get(event_id)
                if previous is not None:
                    if previous != event:
                        raise RuntimeError(f"conflicting eventId collision: {event_id}")
                    duplicate_count += 1
                    continue
                events_by_id[event_id] = event
                market_ids.add(str(event["marketId"]))
        input_event_counts[str(path)] = count

    events = sorted(
        events_by_id.values(),
        key=lambda event: (int(event["sourceTimeUs"]), str(event["eventId"])),
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True))
            handle.write("\n")

    manifest = {
        "inputs": input_event_counts,
        "output": str(args.out),
        "outputEventCount": len(events),
        "marketCount": len(market_ids),
        "duplicateEventCount": duplicate_count,
    }
    manifest_path = args.out.with_suffix(".manifest.json")
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
