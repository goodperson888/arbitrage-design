from decimal import Decimal

from scripts.analysis.analyze_positive_upcross_time_grid import (
    remaining_bucket,
    strict_upcrosses,
)


def point(source_us: int, remaining: str, vwap: str) -> dict:
    return {
        "sourceUs": source_us,
        "remaining": Decimal(remaining),
        "vwap": Decimal(vwap),
        "fee": Decimal("0"),
    }


def test_remaining_bucket_uses_countdown_intervals() -> None:
    assert remaining_bucket(Decimal("299.9")) == (300, 280)
    assert remaining_bucket(Decimal("280")) == (280, 260)
    assert remaining_bucket(Decimal("279.9")) == (280, 260)
    assert remaining_bucket(Decimal("0")) is None


def test_strict_upcross_rejects_downward_touch() -> None:
    rows = [
        point(1_000_000, "290", "0.79"),
        point(2_000_000, "289", "0.81"),
        point(3_000_000, "288", "0.80"),
    ]
    selected = [row for row in strict_upcrosses(rows) if row["threshold"] == Decimal("0.80")]
    assert len(selected) == 1
    assert selected[0]["sourceUs"] == 2_000_000


def test_strict_upcross_keeps_same_threshold_in_different_time_buckets() -> None:
    rows = [
        point(1_000_000, "290", "0.79"),
        point(2_000_000, "289", "0.80"),
        point(3_000_000, "270", "0.79"),
        point(4_000_000, "269", "0.81"),
    ]
    selected = [row for row in strict_upcrosses(rows) if row["threshold"] == Decimal("0.80")]
    assert [(row["bucketUpper"], row["bucketLower"]) for row in selected] == [
        (300, 280),
        (280, 260),
    ]


def test_strict_upcross_rejects_stale_gap_and_large_overshoot() -> None:
    stale = [point(1_000_000, "290", "0.79"), point(8_000_000, "289", "0.81")]
    overshoot = [point(1_000_000, "290", "0.79"), point(2_000_000, "289", "0.86")]
    assert not [row for row in strict_upcrosses(stale) if row["threshold"] == Decimal("0.80")]
    assert not [row for row in strict_upcrosses(overshoot) if row["threshold"] == Decimal("0.80")]
