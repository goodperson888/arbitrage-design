#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from arb_research.cross_market.collectors import BinanceFuturesCollector


async def run(proxy: str | None, messages: int, stream: str | None) -> None:
    url = (
        f"wss://fstream.binance.com/stream?streams=btcusdt@{stream}"
        if stream else BinanceFuturesCollector.URLS[0]
    )
    counts: dict[str, int] = {}
    first_received: dict[str, dict] = {}
    async with websockets.connect(
        url, proxy=proxy if proxy else None, ping_interval=20, ping_timeout=20,
        max_size=2 * 1024 * 1024,
    ) as websocket:
        for _ in range(messages):
            raw = await asyncio.wait_for(websocket.recv(), timeout=10)
            received_us = time.time_ns() // 1_000
            parsed = BinanceFuturesCollector.parse_message(raw, received_us)
            if parsed:
                kind = str(parsed["kind"])
                counts[kind] = counts.get(kind, 0) + 1
                first_received.setdefault(kind, parsed)
    print(json.dumps({"url": url, "counts": counts, "first": first_received}, ensure_ascii=False))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--proxy")
    parser.add_argument("--messages", type=int, default=20)
    parser.add_argument("--stream", choices=("aggTrade", "trade", "bookTicker"))
    args = parser.parse_args()
    asyncio.run(run(args.proxy, args.messages, args.stream))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
