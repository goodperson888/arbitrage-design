from decimal import Decimal
import unittest

from arb_research.experiments.orderbook_depth import (
    BookLevel,
    BookMarket,
    BookSnapshot,
    consume_asks,
    has_book_coverage,
)


class OrderbookDepthTest(unittest.TestCase):
    def test_consumes_multiple_levels_and_reports_vwap(self) -> None:
        result = consume_asks(
            [
                BookLevel(price=Decimal("0.78"), size=Decimal("4")),
                BookLevel(price=Decimal("0.79"), size=Decimal("10")),
            ],
            Decimal("10"),
            Decimal("0.80"),
            fee_model="none",
            fee_rate=Decimal("0"),
        )
        self.assertEqual(result, (Decimal("0.786"), Decimal("0"), Decimal("0.79")))

    def test_rejects_partial_fill_beyond_price_cap(self) -> None:
        result = consume_asks(
            [
                BookLevel(price=Decimal("0.80"), size=Decimal("9")),
                BookLevel(price=Decimal("0.81"), size=Decimal("20")),
            ],
            Decimal("10"),
            Decimal("0.80"),
            fee_model="none",
            fee_rate=Decimal("0"),
        )
        self.assertIsNone(result)

    def test_coverage_rejects_missing_end_of_window(self) -> None:
        asks = {
            "UP": (BookLevel(Decimal("0.5"), Decimal("10")),),
            "DOWN": (BookLevel(Decimal("0.5"), Decimal("10")),),
        }
        market = BookMarket(
            market_id="m1",
            end_us=100_000_000,
            winner="UP",
            snapshots=[
                BookSnapshot(time_us=10_000_000, asks=asks),
                BookSnapshot(time_us=50_000_000, asks=asks),
            ],
        )
        self.assertFalse(has_book_coverage(market, max_remaining_seconds=90))


if __name__ == "__main__":
    unittest.main()
