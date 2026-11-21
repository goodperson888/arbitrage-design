from decimal import Decimal as D
import json
import math
from pathlib import Path
import sys

import pytest

from arb_research.live.candidates.engine import (
    CandidateEngine, buy_budget, price_cap, FEE, LEGACY_FINGERPRINT, FINGERPRINT,
)
from arb_research.live.candidates.feed import BinanceFeed
from arb_research.live.candidates.indicators import Supertrend, MINUTE
from arb_research.live.strategy_monitor import LiveMarket, LiveMonitorConfig, ResearchPaperMonitor

SECOND = 1_000_000
START = (1_800_000_000 // 300) * 300 * SECOND
NOW = START + 120 * SECOND


def market(start=START):
    return {"marketId": f"btc-updown-5m-{start // SECOND}", "conditionId": "test",
            "startUs": start, "endUs": start + 300 * SECOND,
            "tokenByOutcome": {"UP": "u", "DOWN": "d"}}


def book(now, price=".40", size="10000"):
    return {"sourceUs": now, "receivedUs": now, "asks": [[price, size]]}


def feed(now, start=START, side="UP"):
    end = (now // MINUTE) * MINUTE
    return {"status": "live", "spot": {"sourceUs": now, "receivedUs": now,
                                         "price": 101.0 if side == "UP" else 99.0},
            "opens": {str(start): {"price": 100.0, "receivedUs": start}},
            "indicators": {key: {"barEndUs": end, "availableUs": end + SECOND,
                                  "atr": .1, "direction": side, "flip": side}
                           for key in ("10", "14")}}


def tick(engine, now=NOW + 2 * SECOND, *, m=None, f=None, books=None, labels=None):
    engine.tick(m or market(), books or {s: book(now) for s in ("UP", "DOWN")},
                f or feed(now), labels or {}, now)


@pytest.fixture
def engine(tmp_path):
    result = CandidateEngine(tmp_path, NOW)
    yield result
    result.close()


def test_all_twelve_accounts_independent_and_direction_shared(engine):
    at = NOW + 2 * SECOND
    tick(engine, at)
    assert sum(bool(a["pending"]) for a in engine.accounts.values()) == 12
    tick(engine, at + 300_000)
    assert sum(bool(a["position"]) for a in engine.accounts.values()) == 12
    tick(engine, at + SECOND, f=feed(at + SECOND, side="DOWN"))
    assert all(a["fills"] == 1 and a["position"]["side"] == "UP" for a in engine.accounts.values())


@pytest.mark.parametrize("status", ["pending", "stale"])
def test_untrusted_clock_blocks_all_candidate_entries(engine, status):
    at = NOW + 2 * SECOND
    f = dict(feed(at), timeSync={"status": status})
    tick(engine, at, f=f)
    assert all(a["pending"] is None for a in engine.accounts.values())
    assert all("校时" in reason for reason in engine.reasons.values())


def test_clock_expiry_during_execution_never_fills_pending_orders(engine):
    at = NOW + 2 * SECOND
    tick(engine, at, f=dict(feed(at), timeSync={"status": "synced"}))
    tick(engine, at + 300_000, f=dict(feed(at + 300_000), timeSync={"status": "stale"}))
    assert all(a["fills"] == 0 and a["pending"] is None for a in engine.accounts.values())


def test_pnl_fees_and_martin_only_update_from_official_label(engine):
    at = NOW + 2 * SECOND
    tick(engine, at)
    tick(engine, at + 300_000)
    assert all(a["step"] == 0 and a["wins"] == a["losses"] == 0 for a in engine.accounts.values())
    received = START + 310 * SECOND
    tick(engine, received, labels={market()["marketId"]: {"winner": "DOWN", "receivedUs": received}})
    for a in engine.accounts.values():
        assert a["losses"] == 1 and a["wins"] == 0
        assert D(a["pnl"]) == -D(a["spent"])
        assert D(a["fees"]) > 0
        assert a["step"] == (1 if a["sizing"] == "martin11" else 0)
        assert a["availableUs"] == START + 360 * SECOND
    # Duplicate official event must not pay twice or advance the stake twice.
    tick(engine, received + SECOND, labels={market()["marketId"]: {"winner": "DOWN", "receivedUs": received}})
    assert all(a["losses"] == 1 for a in engine.accounts.values())


def test_winner_pays_shares_less_principal_and_fee(engine):
    at = NOW + 2 * SECOND
    tick(engine, at)
    tick(engine, at + 300_000)
    quantities = {a["id"]: D(a["position"]["quantity"]) for a in engine.accounts.values()}
    engine.settle(market()["marketId"], "UP", START + 400 * SECOND)
    for a in engine.accounts.values():
        assert D(a["pnl"]) == quantities[a["id"]] - D(a["spent"])
        assert D(a["cash"]) == D(a["initialCapital"]) + D(a["pnl"])


def test_net_cap_is_per_share_fee_adjusted_not_just_average():
    cap = price_cap(True)
    assert cap < D(".50")
    assert cap + FEE * cap * (1 - cap) <= D(".50")
    assert buy_budget([[".49", "100"]], D(1), cap) is None
    assert buy_budget([[".49", "100"]], D(1), price_cap(False)) is not None
    fill = buy_budget([[".30", "1"], [".45", "20"]], D(1), cap)
    assert D(fill["cost"]) <= 1 and 1 - D(fill["cost"]) <= D(".01")
    assert D(fill["quantity"]) % D(".01") == 0


def test_no_fill_cannot_be_replaced_with_later_favourable_book(engine):
    at = NOW + 2 * SECOND
    tick(engine, at)
    tick(engine, at + 300_000, books={s: book(at + 300_000, ".60") for s in ("UP", "DOWN")})
    tick(engine, at + SECOND)
    assert all(a["fills"] == 0 and a["noFills"] == 1 and not a["pending"] for a in engine.accounts.values())


def test_restart_does_not_reenter_and_recovers_pending_settlement(tmp_path):
    first = CandidateEngine(tmp_path, NOW)
    at = NOW + 2 * SECOND
    tick(first, at)
    tick(first, at + 300_000)
    first.close()
    restored = CandidateEngine(tmp_path, at + SECOND)
    try:
        assert len(restored.open_markets()) == 12
        tick(restored, at + SECOND)
        assert all(a["fills"] == 1 for a in restored.accounts.values())
        restored.settle(market()["marketId"], "UP", START + 400 * SECOND)
        assert all(a["wins"] == 1 for a in restored.accounts.values())
    finally:
        restored.close()


def test_pending_after_restart_never_fills_late(tmp_path):
    first = CandidateEngine(tmp_path, NOW)
    at = NOW + 2 * SECOND
    tick(first, at)
    first.close()
    restored = CandidateEngine(tmp_path, at + 10 * SECOND)
    try:
        tick(restored, at + 10 * SECOND)
        assert all(a["noFills"] == 1 and a["fills"] == 0 for a in restored.accounts.values())
    finally:
        restored.close()


@pytest.mark.parametrize("bad", ["source", "received", "missing", "future"])
def test_stale_or_unverifiable_books_are_rejected(engine, bad):
    at = NOW + 2 * SECOND
    b = book(at)
    if bad == "source": b["sourceUs"] -= 3 * SECOND
    if bad == "received": b["receivedUs"] -= 3 * SECOND
    if bad == "missing": b.pop("sourceUs")
    if bad == "future": b["sourceUs"] += SECOND
    tick(engine, at, books={s: b for s in ("UP", "DOWN")})
    assert not any(a["pending"] for a in engine.accounts.values())


def test_actual_doubled_budget_rechecks_depth(engine):
    at = NOW + 2 * SECOND
    for a in engine.accounts.values():
        if a["sizing"] == "martin11":
            a["step"] = 1
    tick(engine, at, books={s: book(at, size="3") for s in ("UP", "DOWN")})
    assert all(bool(a["pending"]) == (a["sizing"] == "fixed") for a in engine.accounts.values())


def test_bar_publication_lag_expiry_and_missing_exact_anchor(engine):
    # At the minute boundary no latest-bar signal can already be public.
    tick(engine, NOW)
    assert not any(a["pending"] for a in engine.accounts.values())
    at = NOW + 2 * SECOND
    f = feed(at)
    f["opens"] = {}
    tick(engine, at, f=f)
    assert all(bool(a["pending"]) == (a["family"] == "S1") for a in engine.accounts.values())


def test_direction_distance_and_s3_trend_filter(engine):
    at = NOW + 2 * SECOND
    f = feed(at, side="DOWN")
    f["indicators"]["14"]["direction"] = "UP"
    tick(engine, at, f=f)
    for a in engine.accounts.values():
        assert bool(a["pending"]) == (a["rule"] != "S3")
        if a["pending"]:
            assert a["pending"]["side"] == "DOWN"


def test_eleventh_loss_resets_and_records_failure(engine):
    at = NOW + 2 * SECOND
    for a in engine.accounts.values():
        if a["sizing"] == "martin11": a["step"] = 10
    tick(engine, at)
    tick(engine, at + 300_000)
    engine.settle(market()["marketId"], "DOWN", START + 400 * SECOND)
    assert all(a["step"] == 0 and a["capLossCycles"] == 1 for a in engine.accounts.values() if a["sizing"] == "martin11")


def test_journal_corruption_and_changed_model_fail_closed(tmp_path):
    e = CandidateEngine(tmp_path, NOW)
    tick(e)
    e.close()
    path = tmp_path / "accounts.ndjson"
    with path.open("a") as target: target.write('{"partial":')
    with pytest.raises(json.JSONDecodeError): CandidateEngine(tmp_path, NOW)


def test_same_directory_has_only_one_writer(engine, tmp_path):
    with pytest.raises(RuntimeError, match="禁止重复启动"):
        CandidateEngine(tmp_path, NOW)


def test_observations_separate_compressed_and_coverage_restored(tmp_path):
    e = CandidateEngine(tmp_path, NOW)
    tick(e)
    e.close()
    rows = [json.loads(line) for line in (tmp_path / "accounts.ndjson").read_text().splitlines()]
    assert not any(r["type"] == "OBSERVATION" for r in rows)
    assert list((tmp_path / "observations").glob("*/*.ndjson.gz"))
    recovered = CandidateEngine(tmp_path, NOW)
    assert recovered.coverage["eligibleSeconds"] == 1
    recovered.close()


def test_graceful_close_preserves_coverage_after_last_minute_checkpoint(tmp_path):
    e = CandidateEngine(tmp_path, NOW)
    tick(e, NOW + 2 * SECOND)
    tick(e, NOW + 5 * SECOND)
    expected = dict(e.coverage)
    assert expected["missedDecisionSeconds"] == 2
    e.close()
    recovered = CandidateEngine(tmp_path, NOW + 6 * SECOND)
    try:
        assert recovered.coverage == expected
    finally:
        recovered.close()


def test_streaming_indicators_match_frozen_research_and_gap_reset():
    analysis = Path(__file__).parents[1] / "scripts" / "analysis"
    sys.path.insert(0, str(analysis))
    try:
        import analyze_supertrend_direction as frozen
        bars = []
        for i in range(240):
            close = 100 + 12 * math.sin(i / 8)
            bars.append({"open_us": i * MINUTE, "end_us": (i + 1) * MINUTE,
                         "open": close - .1, "high": close + .5,
                         "low": close - .5, "close": close})
        for period in (10, 14):
            expected = frozen.build_supertrend(bars, period, 3)[1]
            calc = Supertrend(period)
            for bar, match in zip(bars, expected):
                row = calc.push({"openUs": bar["open_us"], "endUs": bar["end_us"],
                                 **{k: bar[k] for k in ("open", "high", "low", "close")}})
                assert row["direction"] == match["direction"]
                assert row["flip"] == match["flip"]
                assert row["atr"] == match["atr"]
            row = calc.push({"openUs": 250 * MINUTE, "endUs": 251 * MINUTE,
                             "open": 100., "high": 101., "low": 99., "close": 100.})
            assert row["atr"] is None and row["direction"] is None
    finally:
        sys.path.remove(str(analysis))


def test_feed_only_exact_open_and_closed_bars_with_publication_lag(tmp_path):
    f = BinanceFeed(tmp_path, None)
    bar = {"openUs": START, "endUs": START + MINUTE, "open": 100.,
           "high": 101., "low": 99., "close": 100.}
    f.accept_bar(bar, START + SECOND, False)
    assert f.opens[str(START)]["price"] == 100.
    assert not f.indicators
    f.accept_bar(bar, bar["endUs"] + 50_000, True)
    assert f.indicators["10"]["availableUs"] == bar["endUs"] + SECOND


def test_monitor_recovers_candidate_only_positions_for_official_settlement(tmp_path):
    e = CandidateEngine(tmp_path / "candidates" / "v1", NOW)
    tick(e)
    tick(e, NOW + 2 * SECOND + 300_000)
    e.close()
    monitor = ResearchPaperMonitor(LiveMonitorConfig(data_dir=tmp_path, candidates_enabled=True))
    try:
        assert market()["marketId"] in monitor.known_markets
        assert len(monitor.get_state()["candidates"]["accounts"]) == 12
        m = monitor.known_markets[market()["marketId"]]
        monitor._record_settlement(m, "UP", START + 400 * SECOND)
        assert monitor.candidate_settlements[m.market_id]["winner"] == "UP"
    finally:
        monitor.close()


@pytest.mark.parametrize("side", ["UP", "DOWN"])
@pytest.mark.parametrize("magnitude,a5,a6", [(0.8, False, False), (1.25, True, False), (1.75, True, True)])
def test_atr_distance_thresholds_without_time_scaling_or_trend_confirmation(engine, side, magnitude, a5, a6):
    at = NOW + 2 * SECOND
    f = feed(at, side=side)
    spot = f["spot"]["price"]
    f["indicators"]["14"].update(
        atr=abs(math.log(spot / 100)) * spot / magnitude, direction=None, flip=None,
    )
    for rule, should_enter in (("A5", a5), ("A6", a6)):
        account = next(a for a in engine.accounts.values() if a["rule"] == rule)
        signal, _, features = engine._signal(account, market(), f, at)
        assert signal == (side if should_enter else None)
        assert abs(features["atrDistanceScore"]) == pytest.approx(magnitude)
        # Remaining time stays an eligibility window, not a score denominator.
        signal_late, _, late = engine._signal(account, market(), f, START + 260 * SECOND)
        assert signal_late is None  # Old Spot/indicator cannot be reused later.
        later = START + 260 * SECOND
        late_feed = feed(later, side=side)
        late_feed["indicators"]["14"].update(f["indicators"]["14"])
        late_feed["indicators"]["14"].update(barEndUs=START + 240 * SECOND,
                                                 availableUs=START + 241 * SECOND)
        signal_late, _, late = engine._signal(account, market(), late_feed, later)
        assert signal_late == signal
        assert late["atrDistanceScore"] == features["atrDistanceScore"]


def test_atr_candidates_reject_missing_anchor_and_stale_spot(engine):
    at = NOW + 2 * SECOND
    a = next(a for a in engine.accounts.values() if a["rule"] == "A5")
    f = feed(at)
    f["opens"] = {}
    assert engine._signal(a, market(), f, at)[0] is None
    f = feed(at)
    f["spot"]["sourceUs"] -= 3 * SECOND
    assert engine._signal(a, market(), f, at)[0] is None
    f = feed(at)
    f["spot"]["receivedUs"] += SECOND
    assert engine._signal(a, market(), f, at)[0] is None


def test_atr14_matches_t068_frozen_formula_and_streaming_atr(engine):
    analysis = Path(__file__).parents[1] / "scripts" / "analysis"
    sys.path.insert(0, str(analysis))
    try:
        import analyze_atr_candidates as frozen
        bars = []
        for i in range(40):
            close = 100 + math.sin(i / 4)
            bars.append({"open_us": i * MINUTE, "end_us": (i + 1) * MINUTE,
                         "open": close, "high": close + .5, "low": close - .5, "close": close})
        expected = frozen.build_atr(bars)[1]
        calc = Supertrend(14)
        for bar, match in zip(bars, expected):
            row = calc.push({"openUs": bar["open_us"], "endUs": bar["end_us"],
                             **{k: bar[k] for k in ("open", "high", "low", "close")}})
            if match["atr"] is None:
                assert row["atr"] is None
            else:
                assert row["atr"] == pytest.approx(match["atr"])
        for spot in (98.0, 99.0, 100.0, 101.0, 102.0):
            signals = frozen.dynamic_signals(NOW, {"atr": 1.0}, (NOW, NOW, spot, 100.0), 178)
            at = NOW + 2 * SECOND
            f = feed(at)
            f["spot"]["price"] = spot
            f["indicators"]["14"].update(atr=1.0, direction=None, flip=None)
            for rule in ("A5", "A6"):
                a = next(a for a in engine.accounts.values() if a["rule"] == rule)
                actual, _, _ = engine._signal(a, market(), f, at)
                assert actual == signals[rule]
    finally:
        sys.path.remove(str(analysis))


def test_additive_upgrade_preserves_legacy_positions_and_starts_new_accounts_once(tmp_path):
    assert LEGACY_FINGERPRINT == "80df1440807513341a35c1d4dc1da707c232f4ba24304efebd3665997916edaa"
    first = CandidateEngine(tmp_path, NOW)
    tick(first)
    tick(first, NOW + 2 * SECOND + 300_000)
    old = {k: dict(a) for k, a in first.accounts.items() if a["rule"] not in ("A5", "A6")}
    first.close()
    path = tmp_path / "accounts.ndjson"
    legacy = []
    for row in map(json.loads, path.read_text().splitlines()):
        if row.get("account", {}).get("rule") in ("A5", "A6"):
            continue
        row["fingerprint"] = LEGACY_FINGERPRINT
        if "account" in row:
            row["account"].pop("startedUs", None)
        legacy.append(json.dumps(row))
    path.write_text("\n".join(legacy) + "\n")
    upgraded_at = NOW + 10 * SECOND
    restored = CandidateEngine(tmp_path, upgraded_at)
    try:
        assert len(restored.accounts) == 12
        for key, expected in old.items():
            assert restored.accounts[key] == expected
        for a in restored.accounts.values():
            if a["rule"] in ("A5", "A6"):
                assert a["startedUs"] == upgraded_at
                assert a["fills"] == a["wins"] == a["losses"] == a["step"] == 0
                assert a["position"] is a["pending"] is None
                assert a["cash"] == a["initialCapital"]
    finally:
        restored.close()
    length = len(path.read_text().splitlines())
    again = CandidateEngine(tmp_path, upgraded_at + SECOND)
    try:
        assert len(path.read_text().splitlines()) == length
        assert all(a["startedUs"] == upgraded_at for a in again.accounts.values() if a["rule"] in ("A5", "A6"))
    finally:
        again.close()


def test_unknown_fingerprint_and_legacy_atr_records_fail_closed(tmp_path):
    path = tmp_path / "accounts.ndjson"
    path.write_text(json.dumps({"type": "COVERAGE", "atUs": NOW, "fingerprint": "unknown"}) + "\n")
    with pytest.raises(ValueError, match="版本不匹配"):
        CandidateEngine(tmp_path, NOW)
    path.unlink()
    valid = CandidateEngine(tmp_path, NOW)
    valid.close()
    rows = list(map(json.loads, path.read_text().splitlines()))
    for row in rows:
        if row.get("rule") == "A5": row["fingerprint"] = LEGACY_FINGERPRINT
    path.write_text("\n".join(map(json.dumps, rows)) + "\n")
    with pytest.raises(ValueError, match="已知冻结规则"):
        CandidateEngine(tmp_path, NOW)
