#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from arb_research.live.log_storage import read_partition_rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export partitioned live-monitor streams to a replayable NDJSON file."
    )
    parser.add_argument("--data-dir", type=Path, default=Path("data/live_monitor"))
    parser.add_argument(
        "--stream",
        choices=["events", "signals", "paper", "sessions", "health", "raw"],
        default="events",
    )
    parser.add_argument("--date-from", help="Inclusive UTC date, YYYY-MM-DD")
    parser.add_argument("--date-to", help="Inclusive UTC date, YYYY-MM-DD")
    parser.add_argument("--out", required=True, type=Path)
    return parser.parse_args()


def selected_paths(data_dir: Path, stream: str, date_from: str | None, date_to: str | None) -> list[Path]:
    paths = sorted((data_dir / stream).glob("date=*/hour=*/part-*.ndjson*"))
    result: list[Path] = []
    for path in paths:
        day = next((part.split("=", 1)[1] for part in path.parts if part.startswith("date=")), "")
        if date_from and day < date_from:
            continue
        if date_to and day > date_to:
            continue
        result.append(path)
    return result


def rows_from_path(path: Path) -> Iterator[dict[str, Any]]:
    yield from read_partition_rows(path)


def event_sort_key(row: dict[str, Any]) -> tuple[int, int, str]:
    source_us = int(row.get("sourceTimeUs") or row.get("receivedTimeUs") or 0)
    received_us = int(row.get("receivedTimeUs") or source_us)
    return source_us, received_us, str(row.get("eventId") or "")


def main() -> int:
    args = parse_args()
    if args.out.exists():
        raise SystemExit(f"refusing to overwrite existing output: {args.out}")
    paths = selected_paths(args.data_dir, args.stream, args.date_from, args.date_to)
    if not paths:
        raise SystemExit("no monitor partition files matched")

    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    duplicate_count = 0
    for path in paths:
        for row in rows_from_path(path):
            event_id = str(row.get("eventId") or "")
            if event_id and event_id in seen_ids:
                duplicate_count += 1
                continue
            if event_id:
                seen_ids.add(event_id)
            rows.append(row)
    if args.stream != "raw":
        rows.sort(key=event_sort_key)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with args.out.open("xb") as handle:
        for row in rows:
            line = (json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n").encode()
            handle.write(line)
            digest.update(line)

    manifest = {
        "schemaVersion": 1,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "sourceDirectory": str(args.data_dir.resolve()),
        "stream": args.stream,
        "dateFrom": args.date_from,
        "dateTo": args.date_to,
        "inputFiles": [str(path) for path in paths],
        "inputFileCount": len(paths),
        "rowCount": len(rows),
        "duplicatesRemoved": duplicate_count,
        "output": str(args.out),
        "sha256": digest.hexdigest(),
    }
    manifest_path = args.out.with_suffix(args.out.suffix + ".manifest.json")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(f"Exported {len(rows)} rows from {len(paths)} files: {args.out}")
    print(f"Manifest: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
