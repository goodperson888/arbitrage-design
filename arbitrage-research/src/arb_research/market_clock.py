"""Process-local exchange clock; never changes the computer's system time."""
from __future__ import annotations

import asyncio
import threading
import time


class MarketClock:
    MAX_RTT_US = 1_000_000
    MAX_AGE_SECONDS = 180
    URLS = ("https://api.binance.com/api/v3/time", "https://data-api.binance.vision/api/v3/time")

    def __init__(self):
        self._lock = threading.RLock()
        self._server_us = None
        self._midpoint_ns = None
        self._received_ns = None
        self._rtt_us = None
        self.error = None
        self.enabled = False

    def now_us(self):
        with self._lock:
            if self._server_us is None:
                return time.time_ns() // 1000
            return self._server_us + (time.monotonic_ns() - self._midpoint_ns) // 1000

    def accept_sample(self, server_ms, started_ns, received_ns):
        rtt_us = (received_ns - started_ns) // 1000
        if not isinstance(server_ms, int) or isinstance(server_ms, bool) or not 1_000_000_000_000 <= server_ms <= 4_102_444_800_000:
            raise ValueError("服务器时间格式无效")
        if not 0 <= rtt_us <= self.MAX_RTT_US:
            raise ValueError("校时往返超过1秒，未采用该样本")
        with self._lock:
            self._server_us = server_ms * 1000
            self._midpoint_ns = (started_ns + received_ns) // 2
            self._received_ns = received_ns
            self._rtt_us = rtt_us
            self.error = None

    def snapshot(self):
        with self._lock:
            age = None if self._received_ns is None else max(0, (time.monotonic_ns() - self._received_ns) / 1e9)
            status = "pending" if age is None else "synced" if age <= self.MAX_AGE_SECONDS else "stale"
            return {"status": status, "source": "Binance /api/v3/time", "enabled": self.enabled,
                    "localAheadMs": None if age is None else (time.time_ns() // 1000 - self.now_us()) / 1000,
                    "roundTripMs": None if self._rtt_us is None else self._rtt_us / 1000,
                    "uncertaintyMs": None if self._rtt_us is None else self._rtt_us / 2000 + 1,
                    "sampleAgeSeconds": age, "error": self.error}

    async def synchronize(self, proxy=None):
        import httpx
        self.enabled = True
        async with httpx.AsyncClient(proxy=proxy, timeout=3) as client:
            for url in self.URLS:
                try:
                    started = time.monotonic_ns()
                    response = await client.get(url)
                    received = time.monotonic_ns()
                    response.raise_for_status()
                    self.accept_sample(response.json()["serverTime"], started, received)
                    return True
                except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError) as exc:
                    with self._lock:
                        self.error = f"{type(exc).__name__}: {exc}"
        return False

    async def maintain(self, proxy=None):
        while True:
            await asyncio.sleep(60)
            await self.synchronize(proxy)


market_clock = MarketClock()
