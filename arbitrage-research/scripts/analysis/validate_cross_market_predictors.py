#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from arb_research.cross_market.validation import write_validation_report


def main() -> int:
    parser = argparse.ArgumentParser(description="统计实时跨市场采集结果与官方结算的预测关系")
    parser.add_argument(
        "--data-dir", type=Path, default=ROOT / "data/cross_market_validation"
    )
    parser.add_argument(
        "--output-dir", type=Path, default=ROOT / "outputs/cross_market_validation/realtime"
    )
    args = parser.parse_args()
    for path in write_validation_report(args.data_dir, args.output_dir):
        print(path.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
