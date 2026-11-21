from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import errno
import os
import socket
from pathlib import Path
import threading
from typing import Any, Callable


STATIC_DIR = Path(__file__).with_name("static")


class LocalHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def load_gate_dashboard_state(state_path: Path | None) -> dict[str, Any] | None:
    if state_path is None or not state_path.exists():
        return None
    try:
        state = json.loads(state_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    runtime_path = state_path.with_name("runtime.json")
    try:
        runtime = json.loads(runtime_path.read_text()) if runtime_path.exists() else {}
    except (OSError, json.JSONDecodeError):
        runtime = {}
    signals = runtime.get("signals", {}) if isinstance(runtime.get("signals", {}), dict) else {}
    settled = runtime.get("settled", {}) if isinstance(runtime.get("settled", {}), dict) else {}
    events = [*signals.values(), *settled.values()]
    events.sort(key=lambda item: int(item.get("receivedTimeUs", 0)))
    state["recentPaperEvents"] = events[-50:]
    stats = dict(state.get("paperStats") or {})
    stats["openPositions"] = sum(1 for key in signals if key not in settled)
    state["paperStats"] = stats
    stats_by_outcome: dict[str, dict[str, Any]] = {}
    for outcome in ("UP", "DOWN"):
        outcome_signals = {
            key: value for key, value in signals.items()
            if value.get("outcome") == outcome
        }
        outcome_settlements = {
            key: value for key, value in settled.items()
            if value.get("outcome") == outcome
        }
        wins = sum(1 for value in outcome_settlements.values() if value.get("won") is True)
        closed = len(outcome_settlements)
        stats_by_outcome[outcome] = {
            "signals": len(outcome_signals),
            "openPositions": sum(1 for key in outcome_signals if key not in outcome_settlements),
            "closedTrades": closed,
            "wins": wins,
            "losses": closed - wins,
            "winRate": wins / closed if closed else None,
            "grossPnlPerShareTotal": sum(
                float(value.get("grossPnlPerShare", 0))
                for value in outcome_settlements.values()
            ),
        }
    state["paperStatsByOutcome"] = stats_by_outcome
    return state


class DashboardServer:
    def __init__(
        self,
        state_provider: Callable[[], dict[str, Any]],
        host: str = "127.0.0.1",
        port: int = 8765,
        gate_state_path: Path | None = None,
    ) -> None:
        self.state_provider = state_provider
        self.host = host
        self.port = port
        self.gate_state_path = gate_state_path
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        provider = self.state_provider
        gate_state_path = self.gate_state_path

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract.
                if self.path == "/favicon.ico":
                    self.send_response(204)
                    self.end_headers()
                    return
                if self.path in {"/api/state", "/api/health"}:
                    state = provider()
                    gate_state = load_gate_dashboard_state(gate_state_path)
                    if gate_state is not None:
                        state = {**state, "gateMonitor": gate_state}
                    payload = state if self.path == "/api/state" else {
                        "status": state["service"]["status"],
                        "lastMessageAtUs": state["service"]["lastMessageAtUs"],
                        "lastError": state["service"]["lastError"],
                    }
                    self._send(json.dumps(payload, ensure_ascii=False).encode(), "application/json")
                    return
                name = "index.html" if self.path in {"/", "/index.html"} else self.path.lstrip("/")
                if name not in {"index.html", "monitor.css", "monitor.js", "candidates.js"}:
                    self.send_error(404)
                    return
                path = STATIC_DIR / name
                content_type = {
                    ".html": "text/html; charset=utf-8",
                    ".css": "text/css; charset=utf-8",
                    ".js": "text/javascript; charset=utf-8",
                }[path.suffix]
                self._send(path.read_bytes(), content_type)

            def _send(self, body: bytes, content_type: str) -> None:
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:
                return

        try:
            self._server = LocalHTTPServer((self.host, self.port), Handler)
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE and getattr(exc, "winerror", None) not in {10048, 10013}:
                raise
            self._server = LocalHTTPServer((self.host, 0), Handler)
        self.port = self._server.server_address[1]
        if os.environ.get("ARB_WEB_READY_FILE"):
            Path(os.environ["ARB_WEB_READY_FILE"]).write_text(str(self.port), encoding="ascii")
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=3)
