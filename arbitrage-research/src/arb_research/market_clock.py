"""Process-local exchange clock; never changes the computer's system time."""
from __future__ import annotations

import asyncio
import threading
import time


class MarketClock:
    MAX_RTT_US = 1_000_000
    MAX_AGE_SECONDS = 180
    REFRESH_SECONDS = 60
    RETRY_SECONDS = 5
    REBUILD_AFTER_FAILURES = 3
    CLIENT_CLOSE_SECONDS = 3
    SAMPLES_PER_ENDPOINT = 3
    URLS = ("https://api.binance.com/api/v3/time", "https://data-api.binance.vision/api/v3/time")

    def __init__(self):
        self._lock = threading.RLock()
        self._server_us = None
        self._midpoint_ns = None
        self._received_ns = None
        self._rtt_us = None
        self._attempted_ns = None
        self._next_attempt_ns = None
        self._endpoint = None
        self._consecutive_failures = 0
        self._pool_rebuilds = 0
        self._pool_rebuilt_ns = None
        self._pool_cleanup_error = None
        self.error = None
        self.enabled = False

    def now_us(self):
        with self._lock:
            if self._server_us is None:
                return time.time_ns() // 1000
            return self._server_us + (time.monotonic_ns() - self._midpoint_ns) // 1000

    @classmethod
    def _sample_rtt_us(cls, server_ms, started_ns, received_ns):
        rtt_us = (received_ns - started_ns) // 1000
        if not isinstance(server_ms, int) or isinstance(server_ms, bool) or not 1_000_000_000_000 <= server_ms <= 4_102_444_800_000:
            raise ValueError("服务器时间格式无效")
        if not 0 <= rtt_us <= cls.MAX_RTT_US:
            raise ValueError("校时往返超过1秒，未采用该样本")
        return rtt_us

    def accept_sample(self, server_ms, started_ns, received_ns):
        rtt_us = self._sample_rtt_us(server_ms, started_ns, received_ns)
        with self._lock:
            self._server_us = server_ms * 1000
            self._midpoint_ns = (started_ns + received_ns) // 2
            self._received_ns = received_ns
            self._rtt_us = rtt_us
            self.error = None
            self._consecutive_failures = 0

    def snapshot(self):
        with self._lock:
            now = time.monotonic_ns()
            age = None if self._received_ns is None else max(0, (now - self._received_ns) / 1e9)
            status = "pending" if age is None else "synced" if age <= self.MAX_AGE_SECONDS else "stale"
            return {"status": status, "source": "Binance /api/v3/time", "enabled": self.enabled,
                    "localAheadMs": None if age is None else (time.time_ns() // 1000 - self.now_us()) / 1000,
                    "roundTripMs": None if self._rtt_us is None else self._rtt_us / 1000,
                    "uncertaintyMs": None if self._rtt_us is None else self._rtt_us / 2000 + 1,
                    "sampleAgeSeconds": age, "error": self.error,
                    "endpoint": self._endpoint,
                    "lastAttemptAgeSeconds": None if self._attempted_ns is None else max(0, (now - self._attempted_ns) / 1e9),
                    "nextAttemptInSeconds": None if self._next_attempt_ns is None else max(0, (self._next_attempt_ns - now) / 1e9),
                    "consecutiveFailures": self._consecutive_failures,
                    "poolRebuilds": self._pool_rebuilds,
                    "lastPoolRebuildAgeSeconds": None if self._pool_rebuilt_ns is None else max(0, (now - self._pool_rebuilt_ns) / 1e9),
                    "poolCleanupError": self._pool_cleanup_error}

    def client(self, proxy=None):
        """Owned by the service's async context, including startup and shutdown."""
        import httpx

        return httpx.AsyncClient(proxy=proxy, timeout=3, limits=httpx.Limits(
            max_connections=2, max_keepalive_connections=2, keepalive_expiry=120))

    async def synchronize(self, proxy=None, *, client=None):
        import httpx

        if client is None:
            async with self.client(proxy) as scoped_client:
                return await self.synchronize(client=scoped_client)
        with self._lock:
            self.enabled = True
            self._attempted_ns = time.monotonic_ns()
            self._next_attempt_ns = None
        best = None
        last_error = None
        for url in self.URLS:
            # A slow initial handshake does not prevent a warm-connection sample.
            # Keep the fastest valid sample, never the fastest invalid response.
            for _ in range(self.SAMPLES_PER_ENDPOINT):
                try:
                    started = time.monotonic_ns()
                    response = await client.get(url)
                    received = time.monotonic_ns()
                    response.raise_for_status()
                    server_ms = response.json()["serverTime"]
                    rtt = self._sample_rtt_us(server_ms, started, received)
                    if best is None or rtt < best[0]:
                        best = (rtt, server_ms, started, received, url)
                except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError) as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
            if best is not None:
                with self._lock:
                    self.accept_sample(best[1], best[2], best[3])
                    self._endpoint = best[4]
                return True
        with self._lock:
            self.error = last_error or "未获得可用校时样本"
            self._consecutive_failures += 1
        return False

    async def _close_client(self, client):
        """A broken transport must not hang pool recovery or swallow cancellation."""
        import httpx

        try:
            await asyncio.wait_for(client.aclose(), timeout=self.CLIENT_CLOSE_SECONDS)
        except (httpx.HTTPError, OSError, TimeoutError) as exc:
            return f"{type(exc).__name__}: {exc}"
        return None

    async def maintain(self, proxy=None, *, client=None):
        """Maintain calibration, replacing only the dedicated clock connection pool.

        The caller owns the initial client's context; this loop closes it when
        unhealthy and owns every replacement until shutdown. Pass the same proxy
        used to create the initial client so recovery preserves routing.
        """
        if client is None:
            async with self.client(proxy) as scoped_client:
                await self.maintain(proxy, client=scoped_client)
            return
        current = client
        owns_replacement = False
        with self._lock:
            failed_rounds = self._consecutive_failures
        try:
            while True:
                if failed_rounds >= self.REBUILD_AFTER_FAILURES:
                    cleanup_error = await self._close_client(current)
                    current = self.client(proxy)
                    owns_replacement = True
                    failed_rounds = 0
                    with self._lock:
                        self._pool_rebuilds += 1
                        self._pool_rebuilt_ns = time.monotonic_ns()
                        self._pool_cleanup_error = cleanup_error
                    # Rebuilding is not calibration. Keep the old sample age,
                    # last network error and total consecutive failures intact.
                with self._lock:
                    delay = self.REFRESH_SECONDS if self.snapshot()["status"] == "synced" and not self.error else self.RETRY_SECONDS
                    self._next_attempt_ns = time.monotonic_ns() + int(delay * 1e9)
                await asyncio.sleep(delay)
                success = await self.synchronize(client=current)
                failed_rounds = 0 if success else failed_rounds + 1
        finally:
            with self._lock:
                self._next_attempt_ns = None
            if owns_replacement:
                cleanup_error = await self._close_client(current)
                if cleanup_error:
                    with self._lock:
                        self._pool_cleanup_error = cleanup_error


market_clock = MarketClock()
