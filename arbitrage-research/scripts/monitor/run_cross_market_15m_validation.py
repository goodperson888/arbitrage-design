#!/usr/bin/env python3
from __future__ import annotations

import asyncio
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from run_cross_market_5m_validation import parse_args, run


def main() -> int:
    args = parse_args(ROOT / "configs/research/cross_market_15m_validation_v1.json")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
