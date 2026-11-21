#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from arb_research.cross_market.paper_validation import write_paper_cross_report


def main() -> int:
    parser = argparse.ArgumentParser(description="把现有纸上成交与入场前跨市场指标做无未来数据交叉验证")
    parser.add_argument("--paper-data-dir", type=Path, default=ROOT / "data/live_monitor")
    parser.add_argument("--cross-data-dir", type=Path, default=ROOT / "data/cross_market_validation")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "outputs/cross_market_validation/paper")
    parser.add_argument("--max-snapshot-age-ms", type=float, default=3_000)
    args = parser.parse_args()
    for path in write_paper_cross_report(
        args.paper_data_dir, args.cross_data_dir, args.output_dir,
        max_snapshot_age_ms=args.max_snapshot_age_ms,
    ):
        print(path.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
