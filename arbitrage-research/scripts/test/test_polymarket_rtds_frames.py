#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import websockets

from arb_research.cross_market.collectors import PolymarketReferenceCollector


async def run(proxy: str | None, messages: int) -> None:
    async with websockets.connect(
        PolymarketReferenceCollector.URL,
        proxy=proxy if proxy else True,
        ping_interval=None,
    ) as websocket:
        await websocket.send(json.dumps(PolymarketReferenceCollector.SUBSCRIPTION))
        for _ in range(messages):
            raw = await asyncio.wait_for(websocket.recv(), timeout=5)
            print(repr(raw))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--proxy")
    parser.add_argument("--messages", type=int, default=12)
    args = parser.parse_args()
    asyncio.run(run(args.proxy, args.messages))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
