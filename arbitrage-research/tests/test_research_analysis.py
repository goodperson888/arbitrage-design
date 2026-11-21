from __future__ import annotations

from decimal import Decimal
import unittest

from scripts.analysis.analyze_intramarket_exits import (
    Policy,
    complement_bids,
    consume,
    execute_policy,
)


class IntramarketExitAnalysisTests(unittest.TestCase):
    def test_complement_book_produces_executable_bid(self) -> None:
        bids = complement_bids([["0.30", "10"]])
        result = consume(bids, Decimal("10"), descending=True)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result[0], Decimal("0.70"))

    def test_policy_uses_first_take_profit_before_close(self) -> None:
        entry = {"sourceUs": 1_000_000, "vwap": Decimal("0.60"), "fee": Decimal("0")}
        path = [
            {"sourceUs": 2_000_000, "remaining": Decimal("50"), "vwap": Decimal("0.62"), "fee": Decimal("0")},
            {"sourceUs": 3_000_000, "remaining": Decimal("49"), "vwap": Decimal("0.66"), "fee": Decimal("0")},
            {"sourceUs": 4_000_000, "remaining": Decimal("4"), "vwap": Decimal("0.50"), "fee": Decimal("0")},
        ]
        result = execute_policy(path, entry, Policy("test", Decimal("0.05"), Decimal("0.05")))
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["trigger"], "TP")
        self.assertEqual(result["exitVwap"], Decimal("0.66"))
        self.assertEqual(result["pnlPerShare"], Decimal("0.06"))

    def test_policy_uses_first_stop_loss(self) -> None:
        entry = {"sourceUs": 1_000_000, "vwap": Decimal("0.80"), "fee": Decimal("0")}
        path = [
            {"sourceUs": 2_000_000, "remaining": Decimal("50"), "vwap": Decimal("0.74"), "fee": Decimal("0")},
            {"sourceUs": 3_000_000, "remaining": Decimal("49"), "vwap": Decimal("0.90"), "fee": Decimal("0")},
        ]
        result = execute_policy(path, entry, Policy("test", Decimal("0.08"), Decimal("0.05")))
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["trigger"], "SL")
        self.assertEqual(result["pnlPerShare"], Decimal("-0.06"))


if __name__ == "__main__":
    unittest.main()
