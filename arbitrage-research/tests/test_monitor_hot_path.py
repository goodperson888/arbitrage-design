import asyncio
from decimal import Decimal as D
import json
from random import Random
from types import SimpleNamespace

import pytest

from arb_research.live.strategy_monitor import LiveMarket, LiveMonitorConfig, ResearchPaperMonitor
from arb_research.live.candidates import feed as feed_module


@pytest.fixture
def monitor(tmp_path):
    result = ResearchPaperMonitor(LiveMonitorConfig(data_dir=tmp_path))
    yield result
    result.close()


def test_incremental_history_matches_old_momentum_with_late_and_equal_timestamps(monitor):
    rng = Random(71)
    reference = []
    at = 1_800_000_000_000_000
    for i in range(1200):
        at += 100_000
        source = at - rng.choice([0, 0, 0, 0, 0, 100_000, 4_000_000])
        price = D(rng.randrange(30, 90)) / 100
        monitor.books["u"] = {f"ASK:{price}": D(100)}
        monitor._record_price_observation("u", source)
        reference.append((source, price))
        reference = [row for row in reference if row[0] >= source - 30_000_000]
        assert list(monitor.price_history["u"]) == reference
        for query in (source, source - 2_000_000):
            current = next((p for t, p in reversed(reference) if t <= query), None)
            previous = next((p for t, p in reversed(reference) if t <= query - 5_000_000), None)
            expected = None if current is None or previous is None else current - previous
            assert monitor._price_momentum("u", query) == expected


def test_ordered_history_expires_in_place(monitor):
    monitor.books["u"] = {"ASK:.4": D(100)}
    for i in range(1200):
        monitor._record_price_observation("u", i * 100_000)
        if i == 1:
            history = monitor.price_history["u"]
    assert monitor.price_history["u"] is history
    assert len(history) == 301
    assert monitor._price_momentum("u", 119_900_000) == 0


def test_book_cache_invalidated_by_snapshot_changes_and_deletions(monitor):
    at = 1_800_000_000_000_000
    market = LiveMarket("btc-updown-5m-1800000000", "test", at, at + 300_000_000,
                        {"UP": "u", "DOWN": "d"}, "test")

    def send(message):
        monitor._process_ws_event(market, {"u": "UP", "d": "DOWN"}, message, at)

    def snapshot(price):
        send({"event_type": "book", "asset_id": "u", "timestamp": str(at // 1000),
              "bids": [{"price": ".39", "size": "100"}],
              "asks": [{"price": price, "size": "20"}, {"price": ".49", "size": "30"}]})

    def change(side, price, size):
        send({"event_type": "price_change", "timestamp": str(at // 1000),
              "price_changes": [{"asset_id": "u", "side": side, "price": price, "size": size}]})

    snapshot(".40")
    original = monitor._levels("u", "ASK")
    change("BUY", ".38", "40")
    assert monitor._levels("u", "ASK") is original
    change("SELL", ".40", "2")
    assert monitor._levels("u", "ASK")[0].size == 2
    assert monitor._execution_preview("u")[0] == D(".472")
    change("SELL", ".40", "0")
    assert monitor._levels("u", "ASK")[0].price == D(".49")
    assert monitor.price_history["u"][-1][1] == D(".49")
    snapshot(".35")
    assert monitor._levels("u", "ASK")[0].price == D(".35")
    assert monitor._execution_preview("u")[0] == D(".35")


def test_buffered_binance_trade_burst_yields_to_other_tasks(tmp_path, monkeypatch):
    progress = {"messages": 0, "otherTaskTurns": 0, "clock": 0.0}

    def monotonic():
        progress["clock"] += .001
        return progress["clock"]

    class BufferedSocket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if progress["messages"] == 400:
                raise asyncio.CancelledError()
            progress["messages"] += 1
            return json.dumps({"data": {"e": "trade", "T": progress["messages"], "p": "100"}})

    async def bootstrap():
        pass

    async def run():
        f = feed_module.BinanceFeed(tmp_path, None)
        monkeypatch.setattr(f, "bootstrap", bootstrap)
        monkeypatch.setattr(feed_module, "time", SimpleNamespace(monotonic=monotonic, time_ns=lambda: 1_800_000_000_000_000_000))
        monkeypatch.setattr(feed_module, "clock_us", lambda: 1_800_000_000_000_000)
        monkeypatch.setattr(feed_module.websockets, "connect", lambda *args, **kwargs: BufferedSocket())

        async def other_task():
            while progress["messages"] < 400:
                progress["otherTaskTurns"] += 1
                await asyncio.sleep(0)

        task = asyncio.create_task(other_task())
        with pytest.raises(asyncio.CancelledError):
            await f.run()
        await task

    asyncio.run(run())
    assert progress["messages"] == 400
    assert progress["otherTaskTurns"] > 1
