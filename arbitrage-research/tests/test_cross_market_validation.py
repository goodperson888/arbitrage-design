from __future__ import annotations

import unittest

from arb_research.live.cross_market_validation import market_slug_for_start


class CrossMarketDurationTest(unittest.TestCase):
    def test_market_slug_uses_configured_duration(self) -> None:
        self.assertEqual(
            market_slug_for_start(1_800_000_000, 15),
            "btc-updown-15m-1800000000",
        )


if __name__ == "__main__":
    unittest.main()
