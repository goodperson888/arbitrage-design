from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from arb_research.live.dashboard import load_gate_dashboard_state


class GateDashboardStateTest(unittest.TestCase):
    def test_merges_recent_gate_paper_events_and_open_positions(self) -> None:
        with TemporaryDirectory() as raw_dir:
            state_dir = Path(raw_dir)
            latest = state_dir / "latest.json"
            latest.write_text(json.dumps({"paperStats": {"closedTrades": 1}}))
            (state_dir / "runtime.json").write_text(json.dumps({
                "signals": {
                    "one": {"eventType": "GATE_PAPER_SIGNAL", "receivedTimeUs": "100", "outcome": "UP"},
                    "two": {"eventType": "GATE_PAPER_SIGNAL", "receivedTimeUs": "300", "outcome": "DOWN"},
                },
                "settled": {
                    "one": {
                        "eventType": "GATE_PAPER_SETTLEMENT", "receivedTimeUs": "200",
                        "outcome": "UP", "won": True, "grossPnlPerShare": "0.17",
                    },
                },
            }))

            state = load_gate_dashboard_state(latest)

            self.assertIsNotNone(state)
            assert state is not None
            self.assertEqual(state["paperStats"]["openPositions"], 1)
            self.assertEqual(
                [item["receivedTimeUs"] for item in state["recentPaperEvents"]],
                ["100", "200", "300"],
            )
            self.assertEqual(state["paperStatsByOutcome"]["DOWN"]["signals"], 1)
            self.assertEqual(state["paperStatsByOutcome"]["DOWN"]["openPositions"], 1)
            self.assertEqual(state["paperStatsByOutcome"]["UP"]["wins"], 1)
            self.assertEqual(state["paperStatsByOutcome"]["UP"]["grossPnlPerShareTotal"], 0.17)


if __name__ == "__main__":
    unittest.main()
