from __future__ import annotations

from decimal import Decimal
import unittest

from arb_research.experiments.candidate_stress import (
    MarketTradeSeries,
    StressCase,
    TradeTick,
    run_stress_case,
)


class CandidateStressTest(unittest.TestCase):
    def test_cross_confirmation_and_delay_use_later_trade(self) -> None:
        market = MarketTradeSeries(
            market_id="m1",
            end_us=100_000_000,
            winner="DOWN",
            trades={
                "DOWN": [
                    TradeTick(20_000_000, Decimal("0.70"), Decimal("10")),
                    TradeTick(30_000_000, Decimal("0.76"), Decimal("10")),
                    TradeTick(31_000_000, Decimal("0.77"), Decimal("10")),
                    TradeTick(33_000_000, Decimal("0.78"), Decimal("10")),
                ]
            },
        )
        case = StressCase(
            case_id="test",
            price_min=Decimal("0.75"),
            price_max=Decimal("0.80"),
            outcome="DOWN",
            path="up_cross",
            consecutive_trades=2,
            delay_seconds=2,
        )

        row = run_stress_case({"m1": market}, case, fee_model="none", fee_rate=Decimal("0"))

        self.assertEqual(row["fillCount"], 1)
        self.assertEqual(row["winRate"], "1")
        self.assertEqual(row["averageEntryPrice"], "0.78")
        self.assertEqual(row["averageRemainingSeconds"], "67")

    def test_down_cross_rejects_upward_entry(self) -> None:
        market = MarketTradeSeries(
            market_id="m1",
            end_us=100_000_000,
            winner="DOWN",
            trades={
                "DOWN": [
                    TradeTick(20_000_000, Decimal("0.70"), Decimal("10")),
                    TradeTick(30_000_000, Decimal("0.76"), Decimal("10")),
                ]
            },
        )
        case = StressCase(
            case_id="test",
            price_min=Decimal("0.75"),
            price_max=Decimal("0.80"),
            outcome="DOWN",
            path="down_cross",
        )

        row = run_stress_case({"m1": market}, case, fee_model="none", fee_rate=Decimal("0"))

        self.assertEqual(row["fillCount"], 0)


if __name__ == "__main__":
    unittest.main()
