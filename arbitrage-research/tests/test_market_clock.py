import asyncio
from types import SimpleNamespace

import httpx
import pytest

from arb_research import market_clock as clock_module
from arb_research.market_clock import MarketClock
from arb_research.live.candidates.engine import fresh

BASE = 1_800_000_000_000_000


def calibrated(monkeypatch, bias=5_000_000):
    values = {"mono": 10_000_000_000, "local": BASE + bias}
    monkeypatch.setattr(clock_module, "time", SimpleNamespace(
        monotonic_ns=lambda: values["mono"], time_ns=lambda: values["local"] * 1000))
    clock = MarketClock()
    clock.accept_sample((BASE - 50_000) // 1000, 9_900_000_000, 10_000_000_000)
    return clock, values


@pytest.mark.parametrize("bias", [5_000_000, -5_000_000, 3_600_000_000])
def test_clock_bias_does_not_make_new_trades_stale_or_old_trades_fresh(monkeypatch, bias):
    clock, _ = calibrated(monkeypatch, bias)
    assert clock.now_us() == BASE
    assert clock.snapshot()["localAheadMs"] == bias / 1000
    assert fresh({"sourceUs": BASE - 490_000, "receivedUs": clock.now_us()}, clock.now_us())
    # A newly received buffered trade remains stale after clock correction.
    assert not fresh({"sourceUs": BASE - 5_490_000, "receivedUs": clock.now_us()}, clock.now_us())


def test_system_time_jump_cannot_change_calibrated_clock_or_sample_expiry(monkeypatch):
    clock, values = calibrated(monkeypatch)
    values["local"] += 3_600_000_000
    values["mono"] += 200_000_000
    assert clock.now_us() == BASE + 200_000
    assert clock.snapshot()["status"] == "synced"
    values["mono"] += 181_000_000_000
    assert clock.snapshot()["status"] == "stale"


def test_slow_or_invalid_server_sample_does_not_replace_good_calibration(monkeypatch):
    clock, _ = calibrated(monkeypatch)
    with pytest.raises(ValueError, match="往返"):
        clock.accept_sample(BASE // 1000 + 5_000, 8_000_000_000, 10_000_000_000)
    with pytest.raises(ValueError, match="格式"):
        clock.accept_sample(1, 9_900_000_000, 10_000_000_000)
    assert clock.now_us() == BASE


def test_known_clock_uncertainty_only_tolerates_small_future_source_time():
    value = {"sourceUs": BASE + 30_000, "receivedUs": BASE, "clockUncertaintyUs": 51_000}
    assert fresh(value, BASE)
    assert not fresh(dict(value, sourceUs=BASE - 2_001_000), BASE)
    assert not fresh(dict(value, sourceUs=BASE + 2_000_000), BASE)
    assert not fresh(dict(value, receivedUs=BASE + 1), BASE)


def scripted_client(monkeypatch, samples):
    values = {"mono": 10_000_000_000}
    monkeypatch.setattr(clock_module, "time", SimpleNamespace(
        monotonic_ns=lambda: values["mono"], time_ns=lambda: BASE * 1000))
    pending = iter(samples)
    requested = []

    async def handle(request):
        requested.append(str(request.url))
        elapsed_ns, result = next(pending)
        midpoint_ns = values["mono"] + elapsed_ns // 2
        values["mono"] += elapsed_ns
        if isinstance(result, Exception):
            raise result
        return httpx.Response(result, json={"serverTime": (BASE * 1000 + midpoint_ns) // 1_000_000})

    return httpx.AsyncClient(transport=httpx.MockTransport(handle)), values, requested


def test_warm_samples_recover_from_slow_handshake_and_choose_fastest(monkeypatch):
    async def run():
        client, values, requested = scripted_client(monkeypatch, [
            (1_500_000_000, 200), (80_000_000, 200), (300_000_000, 200),
        ])
        clock = MarketClock()
        async with client:
            assert await clock.synchronize(client=client)
        state = clock.snapshot()
        assert state["status"] == "synced"
        assert state["roundTripMs"] == 80
        # The selected sample's age includes the later, slower probe.
        assert state["sampleAgeSeconds"] == pytest.approx(.3)
        assert state["uncertaintyMs"] == 41
        assert state["error"] is None
        assert requested == [clock.URLS[0]] * 3
        assert abs(clock.now_us() - (BASE + values["mono"] // 1000)) < 1000

    asyncio.run(run())


def test_backup_endpoint_recovers_after_all_primary_samples_fail(monkeypatch):
    async def run():
        client, _, requested = scripted_client(monkeypatch, [
            (10_000_000, 503), (10_000_000, 503), (10_000_000, OSError("connection reset")),
            (90_000_000, 200), (40_000_000, 200), (10_000_000, 403),
        ])
        clock = MarketClock()
        async with client:
            assert await clock.synchronize(client=client)
        assert requested == [clock.URLS[0]] * 3 + [clock.URLS[1]] * 3
        assert clock.snapshot()["endpoint"] == clock.URLS[1]
        assert clock.snapshot()["roundTripMs"] == 40
        # A subsequent failed probe does not discard a valid sample.
        assert clock.snapshot()["error"] is None

    asyncio.run(run())


def test_failed_rounds_never_refresh_old_sample_age_and_recovery_clears_error(monkeypatch):
    async def run():
        client, values, _ = scripted_client(monkeypatch,
            [(20_000_000, 200)] * 3 + [(1_100_000_000, 200)] * 12 + [(30_000_000, 200)] * 3)
        clock = MarketClock()
        async with client:
            assert await clock.synchronize(client=client)
            original_received = clock._received_ns
            values["mono"] += 100_000_000_000
            assert not await clock.synchronize(client=client)
            assert clock._received_ns == original_received
            assert clock.snapshot()["status"] == "synced"
            assert clock.snapshot()["consecutiveFailures"] == 1
            values["mono"] += 90_000_000_000
            assert not await clock.synchronize(client=client)
            assert clock._received_ns == original_received
            assert clock.snapshot()["status"] == "stale"
            assert clock.snapshot()["consecutiveFailures"] == 2
            assert "往返超过1秒" in clock.snapshot()["error"]
            assert await clock.synchronize(client=client)
            assert clock.snapshot()["status"] == "synced"
            assert clock.snapshot()["consecutiveFailures"] == 0
            assert clock.snapshot()["error"] is None

    asyncio.run(run())


def test_maintenance_retries_quickly_then_returns_to_normal_interval_and_closes(monkeypatch):
    async def run():
        client, values, _ = scripted_client(monkeypatch, [])
        clock = MarketClock()
        created = []
        delays = []
        attempts = []

        def create(proxy=None):
            created.append(proxy)
            return client

        async def sleep(seconds):
            delays.append(seconds)
            assert clock.snapshot()["nextAttemptInSeconds"] == seconds
            values["mono"] += seconds * 1_000_000_000
            if len(delays) == 3:
                raise asyncio.CancelledError()

        async def synchronize(*, client):
            attempts.append(client)
            if len(attempts) == 1:
                clock.error = "temporary network failure"
                return False
            clock.accept_sample(BASE // 1000, values["mono"] - 10_000_000, values["mono"])
            return True

        monkeypatch.setattr(clock, "client", create)
        monkeypatch.setattr(clock, "synchronize", synchronize)
        monkeypatch.setattr(clock_module.asyncio, "sleep", sleep)
        with pytest.raises(asyncio.CancelledError):
            await clock.maintain("http://127.0.0.1:7890")
        assert created == ["http://127.0.0.1:7890"]
        assert delays == [5, 5, 60]
        assert attempts == [client, client]
        assert client.is_closed
        assert clock.snapshot()["nextAttemptInSeconds"] is None

    asyncio.run(run())


def test_pending_or_stale_clock_stays_blocked_when_every_probe_fails(monkeypatch):
    async def run():
        client, _, _ = scripted_client(monkeypatch, [(10_000_000, 503)] * 6)
        clock = MarketClock()
        async with client:
            assert not await clock.synchronize(client=client)
        assert clock.snapshot()["status"] == "pending"
        assert clock.snapshot()["sampleAgeSeconds"] is None
        assert clock.snapshot()["consecutiveFailures"] == 1

    asyncio.run(run())


@pytest.mark.parametrize("service_kind", ["paper", "cross"])
def test_service_closes_clock_client_when_startup_is_cancelled(monkeypatch, service_kind):
    from arb_research.live.strategy_monitor import ResearchPaperMonitor
    from arb_research.live.cross_market_validation import CrossMarketValidationService

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(503)))
        service_type = ResearchPaperMonitor if service_kind == "paper" else CrossMarketValidationService
        service = service_type.__new__(service_type)
        service.config = SimpleNamespace(proxy="http://127.0.0.1:7890")
        created = []

        def create(proxy):
            created.append(proxy)
            return client

        async def cancelled(clock_client, duration):
            assert clock_client is client
            assert not client.is_closed
            raise asyncio.CancelledError()

        monkeypatch.setattr(clock_module.market_clock, "client", create)
        monkeypatch.setattr(service, "_run_with_clock", cancelled)
        with pytest.raises(asyncio.CancelledError):
            await service.run()
        assert client.is_closed
        assert created == [service.config.proxy]

    asyncio.run(run())


def test_repeated_rounds_reuse_actual_http_connection_and_close_cleanly(monkeypatch):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    import time

    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    peers = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            peers.append(self.client_address)
            body = ('{"serverTime":%d}' % (time.time_ns() // 1_000_000)).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    async def run():
        clock = MarketClock()
        clock.URLS = (f"http://127.0.0.1:{server.server_port}/time",)
        async with clock.client() as client:
            assert await clock.synchronize(client=client), clock.snapshot()
            assert await clock.synchronize(client=client)
        assert client.is_closed
        assert len(peers) == 6
        assert len(set(peers)) == 1

    try:
        asyncio.run(run())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
