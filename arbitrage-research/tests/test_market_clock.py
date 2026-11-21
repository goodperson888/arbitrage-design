from types import SimpleNamespace

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
