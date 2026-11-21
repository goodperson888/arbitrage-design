#!/usr/bin/env python3
"""Losslessly archive paper events/signals, one verified partition at a time."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from arb_research.live.log_storage import archive_candidates, archive_partition
from arb_research.platform_io import exclusive_lock


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data/live_monitor"))
    parser.add_argument("--apply", action="store_true", help="verify gzip, then replace plain files")
    args = parser.parse_args()
    root = args.data_dir.absolute()
    paths = archive_candidates(root)
    print(json.dumps({"mode": "apply" if args.apply else "preview", "files": len(paths),
                      "sourceBytes": sum(path.stat().st_size for path in paths),
                      "first": str(paths[0]) if paths else None,
                      "last": str(paths[-1]) if paths else None}), flush=True)
    if not args.apply:
        return 0
    with exclusive_lock(root / ".storage-archive.lock"):
        saved = archived = active = 0
        for index, path in enumerate(paths, 1):
            entry = archive_partition(root, path)
            if entry["status"] == "archived":
                archived += 1
                saved += entry["sourceBytes"] - entry["gzipBytes"]
            else:
                active += 1
            if index % 20 == 0 or index == len(paths):
                print(json.dumps({"processed": index, "total": len(paths), "archived": archived,
                                  "activeSkipped": active, "savedBytes": saved}), flush=True)
        print(json.dumps({"complete": True, "archived": archived, "activeSkipped": active,
                          "savedBytes": saved, "manifest": str(root / "storage_archive_manifest.ndjson")}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
