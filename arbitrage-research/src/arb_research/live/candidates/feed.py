"""One public Binance Spot connection shared by all candidate paper accounts."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import time

import httpx
import websockets

from .indicators import Supertrend, MINUTE

REST = "https://api.binance.com/api/v3/klines"
WS = "wss://stream.binance.com:9443/stream?streams=btcusdt@trade/btcusdt@kline_1m"


def clock_us():
    return time.time_ns() // 1000


class BinanceFeed:
    def __init__(self, data_dir: Path, proxy: str | None):
        self.path = data_dir / "binance_state.json"
        self.proxy = proxy
        self.trends = {str(n): Supertrend(n) for n in (10, 14)}
        self.indicators = {}
        self.opens = {}
        self.last_end = 0
        self.spot = None
        self.status = "starting"
        self.error = None
        self.reconnections = 0
        if self.path.exists():
            saved = json.loads(self.path.read_text())
            self.trends = {str(n): Supertrend(n, saved["trends"][str(n)]) for n in (10, 14)}
            self.indicators = saved["indicators"]
            self.opens = saved["opens"]
            self.last_end = saved["lastEnd"]

    def accept_bar(self, bar: dict, received: int, closed: bool):
        start = bar["openUs"]
        # Exact opening minute only. Never substitute a nearby trade/candle.
        if start % (5 * MINUTE) == 0:
            self.opens[str(start)] = {"price": bar["open"], "receivedUs": received}
            self.opens = {k: v for k, v in self.opens.items() if int(k) >= start - 24 * 60 * MINUTE}
        if closed and bar["endUs"] > self.last_end:
            self.indicators = {key: {**trend.push(bar), "availableUs": max(received, bar["endUs"] + 1_000_000)}
                               for key, trend in self.trends.items()}
            self.last_end = bar["endUs"]

    async def checkpoint(self):
        saved = {"trends": {key: trend.state for key, trend in self.trends.items()},
                 "indicators": self.indicators, "opens": self.opens, "lastEnd": self.last_end}
        def write():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps(saved, ensure_ascii=False))
            temp.replace(self.path)
        await asyncio.to_thread(write)

    async def bootstrap(self):
        async with httpx.AsyncClient(proxy=self.proxy, timeout=15) as client:
            response = await client.get(REST, params={"symbol": "BTCUSDT", "interval": "1m", "limit": 500})
            response.raise_for_status()
            received = clock_us()
            for row in response.json():
                bar = {"openUs": int(row[0]) * 1000, "endUs": (int(row[0]) + 60_000) * 1000,
                       "open": float(row[1]), "high": float(row[2]),
                       "low": float(row[3]), "close": float(row[4])}
                self.accept_bar(bar, received, bar["endUs"] <= received)
        await self.checkpoint()

    async def run(self):
        delay = 1
        while True:
            try:
                self.status = "warming"
                await self.bootstrap()
                async with websockets.connect(WS, proxy=self.proxy or True, open_timeout=15,
                                              ping_interval=20, ping_timeout=20, max_queue=1024) as ws:
                    self.status, self.error = "live", None
                    delay = 1
                    yielded_at = time.monotonic()
                    async for raw in ws:
                        value = json.loads(raw).get("data", {})
                        received = clock_us()
                        if value.get("e") == "trade":
                            source = int(value["T"]) * 1000
                            if self.spot is None or source >= self.spot["sourceUs"]:
                                self.spot = {"price": float(value["p"]), "sourceUs": source, "receivedUs": received}
                        elif value.get("e") == "kline":
                            k = value["k"]
                            self.accept_bar({"openUs": int(k["t"]) * 1000,
                                             "endUs": (int(k["t"]) + 60_000) * 1000,
                                             "open": float(k["o"]), "high": float(k["h"]),
                                             "low": float(k["l"]), "close": float(k["c"])}, received, bool(k["x"]))
                            if k["x"]:
                                await self.checkpoint()
                        # recv() may return buffered trades without suspending.
                        # Give decision timers a turn during a sustained burst.
                        if time.monotonic() - yielded_at >= .005:
                            await asyncio.sleep(0)
                            yielded_at = time.monotonic()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.status, self.error = "reconnecting", f"{type(exc).__name__}: {exc}"
                self.reconnections += 1
                self.spot = None
                await asyncio.sleep(delay)
                delay = min(60, delay * 2)

    def snapshot(self):
        # Replaced objects, not references to mutable recursive state.
        return {"status": self.status, "error": self.error, "reconnections": self.reconnections,
                "spot": self.spot, "indicators": dict(self.indicators), "opens": dict(self.opens)}
