from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from arb_research.live.dashboard import load_gate_dashboard_state


def test_dashboard_binds_another_port_and_publishes_actual_address(tmp_path, monkeypatch):
    import socket
    import urllib.request
    from arb_research.live.dashboard import DashboardServer
    ready = tmp_path / "paper.port"
    monkeypatch.setenv("ARB_WEB_READY_FILE", str(ready))
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        original = occupied.getsockname()[1]
        server = DashboardServer(lambda: {"service": {"status": "live"}}, port=original)
        try:
            server.start()
            assert server.port != original
            assert int(ready.read_text()) == server.port
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(f"http://127.0.0.1:{server.port}/api/state", timeout=2) as response:
                assert json.load(response)["service"]["status"] == "live"
        finally:
            server.stop()


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
