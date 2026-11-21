from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import sys
import time
from pathlib import Path
from typing import Any

from .calculator import RealtimeObservation, RealtimeProbabilityEngine
from .history import LightweightFactRecorder


def _observation(payload: dict[str, Any]) -> RealtimeObservation:
    return RealtimeObservation(
        market_id=str(payload["market_id"]),
        market_start=float(payload["market_start"]),
        market_end=float(payload["market_end"]),
        price=float(payload["price"]),
        observed_at=float(payload.get("observed_at", time.time())),
        open_price=float(payload["open_price"]) if payload.get("open_price") is not None else None,
        source_age_ms=float(payload["source_age_ms"]) if payload.get("source_age_ms") is not None else None,
        best_bid=float(payload["best_bid"]) if payload.get("best_bid") is not None else None,
        best_ask=float(payload["best_ask"]) if payload.get("best_ask") is not None else None,
        best_bid_size=float(payload["best_bid_size"]) if payload.get("best_bid_size") is not None else None,
        best_ask_size=float(payload["best_ask_size"]) if payload.get("best_ask_size") is not None else None,
        book_imbalance=float(payload["book_imbalance"]) if payload.get("book_imbalance") is not None else None,
        trade_side=str(payload["trade_side"]).upper() if payload.get("trade_side") is not None else None,
        trade_quantity=float(payload["trade_quantity"]) if payload.get("trade_quantity") is not None else None,
    )


def run_stdin(history_path: str | Path | None = None, history_interval_seconds: float = 10.0) -> int:
    engine = RealtimeProbabilityEngine()
    recorder = LightweightFactRecorder(history_path, history_interval_seconds) if history_path else None
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            result = engine.update(_observation(json.loads(line)))
            if recorder:
                recorder.record(result)
            print(json.dumps(result, ensure_ascii=False, separators=(",", ":")), flush=True)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False), flush=True)
    return 0


async def run_binance(symbol: str, interval_seconds: int, history_path: str | Path | None = None, history_interval_seconds: float = 10.0) -> int:
    try:
        import websockets  # type: ignore
    except ImportError as exc:
        raise SystemExit("Binance 模式需要安装 websockets：python3 -m pip install -e '.[live]'") from exc
    lowered = symbol.lower()
    url = f"wss://stream.binance.com:9443/stream?streams={lowered}@aggTrade/{lowered}@bookTicker/{lowered}@depth5@100ms"
    engine = RealtimeProbabilityEngine()
    recorder = LightweightFactRecorder(history_path, history_interval_seconds) if history_path else None
    last_trade_price: float | None = None
    last_observed_at: float | None = None
    while True:
        try:
            kwargs = {"ping_interval": 20, "close_timeout": 5}
            if "proxy" in inspect.signature(websockets.connect).parameters:
                kwargs["proxy"] = None  # do not inherit a machine-wide proxy in direct mode
            async with websockets.connect(url, **kwargs) as socket:
                async for raw in socket:
                    message = json.loads(raw)
                    data = message.get("data") if isinstance(message, dict) and message.get("data") else message
                    event_type = str(data.get("e") or "")
                    event_ms = float(data.get("E") or data.get("T") or time.time() * 1000)
                    event_time = event_ms / 1000.0
                    observed_at = max(event_time, last_observed_at or event_time)
                    last_observed_at = observed_at
                    start = event_time - (event_time % interval_seconds)
                    best_bid = best_ask = best_bid_size = best_ask_size = None
                    trade_side = trade_quantity = None
                    is_trade = event_type in {"aggTrade", "trade"} or (data.get("p") is not None and data.get("q") is not None)
                    is_book_ticker = event_type == "bookTicker" or all(key in data for key in ("b", "a", "B", "A"))
                    is_depth_snapshot = event_type == "depthUpdate" or ("bids" in data and "asks" in data)
                    if is_trade and data.get("p") is not None:
                        last_trade_price = float(data["p"])
                        trade_quantity = float(data.get("q") or 0.0)
                        trade_side = "SELL" if bool(data.get("m")) else "BUY"
                    if is_book_ticker:
                        best_bid, best_ask = float(data["b"]), float(data["a"])
                        best_bid_size, best_ask_size = float(data["B"]), float(data["A"])
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
                        "market_id": f"{symbol.upper()}-{int(start)}",
                        "market_start": start,
                        "market_end": start + interval_seconds,
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
                    result = engine.update(_observation(payload))
                    if recorder:
                        recorder.record(result)
                    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")), flush=True)
        except Exception as exc:  # reconnect with bounded delay; no order path exists here.
            print(json.dumps({"error": f"binance reconnect: {type(exc).__name__}: {exc}"}, ensure_ascii=False), file=sys.stderr, flush=True)
            await asyncio.sleep(3)


def main() -> int:
    parser = argparse.ArgumentParser(description="History-free real-time probability calculator")
    parser.add_argument("--mode", choices=("stdin", "binance"), default="stdin")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--interval-seconds", type=int, default=300)
    parser.add_argument("--history-path", default=None)
    parser.add_argument("--history-interval-seconds", type=float, default=10.0)
    args = parser.parse_args()
    if args.mode == "stdin":
        return run_stdin(args.history_path, args.history_interval_seconds)
    return asyncio.run(run_binance(args.symbol, args.interval_seconds, args.history_path, args.history_interval_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
