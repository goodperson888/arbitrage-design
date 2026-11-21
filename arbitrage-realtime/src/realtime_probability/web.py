from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .calculator import RealtimeObservation, RealtimeProbabilityEngine
from .history import LightweightFactRecorder


ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = ROOT / "web"


class RuntimeState:
    def __init__(self, symbol: str, interval_seconds: int, history_path: str | Path | None = None, history_interval_seconds: float = 10.0) -> None:
        self.symbol = symbol.upper()
        self.interval_seconds = interval_seconds
        self.engine = RealtimeProbabilityEngine()
        default_history = ROOT / "data" / "realtime_facts.ndjson"
        self.history = LightweightFactRecorder(history_path or os.environ.get("REALTIME_HISTORY_PATH", str(default_history)), history_interval_seconds)
        self._lock = threading.Lock()
        self.latest: dict[str, Any] | None = None
        self.status = "starting"
        self.last_error: str | None = None
        self.connected_at: float | None = None
        self.last_message_at: float | None = None
        self.message_count = 0
        self.reconnect_requested = threading.Event()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            now = time.time()
            age = None if self.last_message_at is None else max(0.0, now - self.last_message_at)
            return {
                "service": "running",
                "status": self.status,
                "symbol": self.symbol,
                "interval_seconds": self.interval_seconds,
                "source": "Binance Spot WebSocket @aggTrade + @bookTicker + @depth5@100ms",
                "latest": self.latest,
                "message_count": self.message_count,
                "last_message_age_seconds": round(age, 3) if age is not None else None,
                "connected_at": self.connected_at,
                "last_error": self.last_error,
                "history": self.history.snapshot(),
            }

    def request_reconnect(self) -> None:
        self.reconnect_requested.set()


class BinanceWorker(threading.Thread):
    def __init__(self, state: RuntimeState, proxy: str | None = None) -> None:
        super().__init__(name="binance-realtime-worker", daemon=True)
        self.state = state
        self.proxy = proxy
        self.stop_event = threading.Event()

    def stop(self) -> None:
        self.stop_event.set()
        self.state.request_reconnect()

    def run(self) -> None:
        try:
            asyncio.run(self._run())
        except Exception as exc:  # keep the web page available even if network setup fails
            with self.state._lock:
                self.state.status = "error"
                self.state.last_error = f"{type(exc).__name__}: {exc}"

    async def _run(self) -> None:
        try:
            import websockets  # type: ignore
        except ImportError:
            with self.state._lock:
                self.state.status = "dependency_error"
                self.state.last_error = "未安装 websockets，请执行 python3 -m pip install -e '.[live]'"
            return
        symbol = self.state.symbol.lower()
        urls = (
            f"wss://stream.binance.com:9443/stream?streams={symbol}@aggTrade/{symbol}@bookTicker/{symbol}@depth5@100ms",
            f"wss://stream.binance.com:443/stream?streams={symbol}@aggTrade/{symbol}@bookTicker/{symbol}@depth5@100ms",
            f"wss://data-stream.binance.vision/stream?streams={symbol}@aggTrade/{symbol}@bookTicker/{symbol}@depth5@100ms",
        )
        delay = 1.0
        while not self.stop_event.is_set():
            connected = False
            for url in urls:
                if self.stop_event.is_set():
                    break
                try:
                    with self.state._lock:
                        self.state.status = "connecting"
                        self.state.last_error = None
                    # Explicitly pass None so websockets does not silently
                    # inherit an unrelated ALL_PROXY/HTTPS_PROXY setting.
                    kwargs: dict[str, Any] = {
                        "ping_interval": 20,
                        "close_timeout": 5,
                        "max_queue": 1024,
                    }
                    if "proxy" in inspect.signature(websockets.connect).parameters:
                        kwargs["proxy"] = self.proxy
                    elif self.proxy:
                        raise RuntimeError("当前 websockets 版本不支持代理参数，请升级 websockets")
                    async with websockets.connect(url, **kwargs) as socket:
                        connected = True
                        delay = 1.0
                        last_trade_price: float | None = None
                        last_observed_at: float | None = None
                        with self.state._lock:
                            self.state.status = "live"
                            self.state.connected_at = time.time()
                        while not self.stop_event.is_set() and not self.state.reconnect_requested.is_set():
                            try:
                                raw = await asyncio.wait_for(socket.recv(), timeout=1.0)
                            except asyncio.TimeoutError:
                                continue
                            message = json.loads(raw)
                            data = message.get("data") if isinstance(message, dict) and message.get("data") else message
                            event_type = str(data.get("e") or "")
                            event_ms = float(data.get("E") or data.get("T") or time.time() * 1000)
                            event_time = event_ms / 1000.0
                            # Combined streams can deliver book and trade events with
                            # slightly different exchange timestamps. Keep the
                            # calculator's sample clock monotonic without changing
                            # the UTC period used to identify the market.
                            observed_at = max(event_time, last_observed_at or event_time)
                            last_observed_at = observed_at
                            start = event_time - (event_time % self.state.interval_seconds)
                            best_bid = best_ask = best_bid_size = best_ask_size = None
                            trade_side = trade_quantity = None
                            is_trade = event_type in {"aggTrade", "trade"} or (data.get("p") is not None and data.get("q") is not None)
                            is_book_ticker = event_type == "bookTicker" or all(key in data for key in ("b", "a", "B", "A"))
                            is_depth_snapshot = event_type == "depthUpdate" or ("bids" in data and "asks" in data)
                            if is_trade and data.get("p") is not None:
                                last_trade_price = float(data["p"])
                                trade_quantity = float(data.get("q") or 0.0)
                                # Binance's `m` means the buyer was the market
                                # maker; therefore the taker side was SELL.
                                trade_side = "SELL" if bool(data.get("m")) else "BUY"
                            if is_book_ticker:
                                best_bid = float(data["b"])
                                best_ask = float(data["a"])
                                best_bid_size = float(data["B"])
                                best_ask_size = float(data["A"])
                            elif is_depth_snapshot:
                                bids = data.get("b") or data.get("bids") or []
                                asks = data.get("a") or data.get("asks") or []
                                if bids:
                                    best_bid, best_bid_size = float(bids[0][0]), float(bids[0][1])
                                if asks:
                                    best_ask, best_ask_size = float(asks[0][0]), float(asks[0][1])
                            current_price = last_trade_price
                            if current_price is None and best_bid is not None and best_ask is not None:
                                current_price = (best_bid + best_ask) / 2
                            if current_price is None:
                                continue
                            payload = {
                                "market_id": f"{self.state.symbol}-{int(start)}",
                                "market_start": start,
                                "market_end": start + self.state.interval_seconds,
                                "price": current_price,
                                "observed_at": observed_at,
                                "source_age_ms": max(0.0, time.time() * 1000 - event_ms),
                                "best_bid": best_bid,
                                "best_ask": best_ask,
                                "best_bid_size": best_bid_size,
                                "best_ask_size": best_ask_size,
                                "trade_side": trade_side,
                                "trade_quantity": trade_quantity,
                            }
                            result = self.state.engine.update(RealtimeObservation(
                                market_id=payload["market_id"],
                                market_start=payload["market_start"],
                                market_end=payload["market_end"],
                                price=payload["price"],
                                observed_at=payload["observed_at"],
                                source_age_ms=payload["source_age_ms"],
                                best_bid=payload["best_bid"],
                                best_ask=payload["best_ask"],
                                best_bid_size=payload["best_bid_size"],
                                best_ask_size=payload["best_ask_size"],
                                trade_side=payload["trade_side"],
                                trade_quantity=payload["trade_quantity"],
                            ))
                            with self.state._lock:
                                self.state.latest = result
                                self.state.message_count += 1
                                self.state.last_message_at = time.time()
                            self.state.history.record(result)
                        self.state.reconnect_requested.clear()
                    if connected:
                        break
                except Exception as exc:  # reconnect across official public endpoints
                    with self.state._lock:
                        self.state.status = "reconnecting"
                        self.state.last_error = f"{type(exc).__name__}: {exc}"
            if not self.stop_event.is_set():
                self.state.reconnect_requested.clear()
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)
        with self.state._lock:
            self.state.status = "stopped"


class _Handler(BaseHTTPRequestHandler):
    server_version = "RealtimeProbability/0.1"

    def _json(self, value: dict[str, Any], status: int = HTTPStatus.OK) -> None:
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        state: RuntimeState = self.server.runtime_state  # type: ignore[attr-defined]
        if self.path == "/api/state":
            self._json(state.snapshot())
            return
        if self.path in {"/", "/index.html"}:
            try:
                body = (WEB_DIR / "index.html").read_bytes()
            except FileNotFoundError:
                self._json({"error": "web/index.html not found"}, HTTPStatus.NOT_FOUND)
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        state: RuntimeState = self.server.runtime_state  # type: ignore[attr-defined]
        if self.path == "/api/reconnect":
            state.request_reconnect()
            self._json({"ok": True})
            return
        self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def log_message(self, *_: Any) -> None:
        return


def run_server(host: str = "127.0.0.1", port: int = 8787, symbol: str = "BTCUSDT", interval_seconds: int = 300, proxy: str | None = None, history_path: str | None = None, history_interval_seconds: float = 10.0, stop_file: Path | None = None) -> None:
    state = RuntimeState(symbol, interval_seconds, history_path, history_interval_seconds)
    worker = BinanceWorker(state, proxy=proxy)
    worker.start()
    server = ThreadingHTTPServer((host, port), _Handler)
    server.runtime_state = state  # type: ignore[attr-defined]
    closing = threading.Event()

    def watch_stop():
        while not closing.wait(0.2):
            if stop_file is not None and stop_file.exists():
                server.shutdown()
                return

    watcher = threading.Thread(target=watch_stop, daemon=True)
    if stop_file is not None:
        watcher.start()
    print(f"实时概率页面：http://{host}:{port}")
    print(f"行情：Binance Spot @aggTrade + @bookTicker + @depth5@100ms | symbol={state.symbol} | interval={interval_seconds}s")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        closing.set()
        worker.stop()
        worker.join(timeout=5)
        server.server_close()


def main() -> int:
    parser = argparse.ArgumentParser(description="本地通用实时概率网页")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("REALTIME_WEB_PORT", "8787")))
    parser.add_argument("--symbol", default=os.environ.get("REALTIME_SYMBOL", "BTCUSDT"))
    parser.add_argument("--interval-seconds", type=int, default=int(os.environ.get("REALTIME_INTERVAL_SECONDS", "300")))
    parser.add_argument("--proxy", default=os.environ.get("REALTIME_BINANCE_PROXY"))
    parser.add_argument("--history-path", default=os.environ.get("REALTIME_HISTORY_PATH"))
    parser.add_argument("--stop-file", type=Path)
    parser.add_argument("--history-interval-seconds", type=float, default=float(os.environ.get("REALTIME_HISTORY_INTERVAL_SECONDS", "10")))
    args = parser.parse_args()
    if args.interval_seconds <= 0:
        parser.error("--interval-seconds must be positive")
    run_server(args.host, args.port, args.symbol, args.interval_seconds, args.proxy, args.history_path, args.history_interval_seconds, args.stop_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
