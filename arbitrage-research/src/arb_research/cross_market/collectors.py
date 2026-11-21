from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Awaitable, Callable

try:
    import websockets
except ImportError:  # Parser and feature tests don't require the network dependency.
    websockets = None  # type: ignore[assignment]


UpdateCallback = Callable[[str, dict[str, Any], Any, int], Awaitable[None]]
StatusCallback = Callable[[str, str, str | None], Awaitable[None]]


def _now_us() -> int:
    return time.time_ns() // 1_000


class ReconnectingCollector:
    venue = "UNKNOWN"

    def __init__(
        self,
        on_update: UpdateCallback,
        on_status: StatusCallback,
        *,
        proxy: str | None = None,
    ) -> None:
        self.on_update = on_update
        self.on_status = on_status
        self.proxy = proxy

    async def run(self, stop_event: asyncio.Event) -> None:
        if websockets is None:
            await self.on_status(self.venue, "dependency_error", "websockets is not installed")
            return
        delay = 1.0
        while not stop_event.is_set():
            try:
                await self.on_status(self.venue, "connecting", None)
                await self.connect(stop_event)
                delay = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - collectors must reconnect.
                await self.on_status(self.venue, "reconnecting", f"{type(exc).__name__}: {exc}")
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
                delay = min(delay * 2, 30.0)

    async def connect(self, stop_event: asyncio.Event) -> None:
        raise NotImplementedError


class BinanceCollector(ReconnectingCollector):
    venue = "BINANCE"
    STREAMS = (
        "btcusdt@aggTrade/btcusdt@bookTicker/"
        "btcusdt@kline_1m/btcusdt@kline_5m"
    )
    URLS = (
        f"wss://stream.binance.com:9443/stream?streams={STREAMS}",
        f"wss://stream.binance.com:443/stream?streams={STREAMS}",
        f"wss://data-stream.binance.vision/stream?streams={STREAMS}",
    )

    @staticmethod
    def parse_message(raw: Any, received_us: int) -> dict[str, Any] | None:
        wrapper = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        data = wrapper.get("data", wrapper) if isinstance(wrapper, dict) else {}
        event = data.get("e")
        if event in {"aggTrade", "trade"}:
            return {
                "kind": "trade",
                "sourceTimeUs": str(int(data.get("T", data.get("E", 0))) * 1_000),
                "receivedTimeUs": str(received_us),
                "price": data.get("p"),
                "quantity": data.get("q"),
                "isBuyerMaker": bool(data.get("m")),
                "aggregateTradeId": str(data.get("a", data.get("t", ""))),
            }
        if event == "kline":
            item = data.get("k", {})
            return {
                "kind": "kline",
                "sourceTimeUs": str(int(data.get("E", received_us // 1_000)) * 1_000),
                "receivedTimeUs": str(received_us),
                "interval": item.get("i"),
                "openTimeUs": str(int(item.get("t", 0)) * 1_000),
                "closeTimeUs": str(int(item.get("T", 0)) * 1_000),
                "open": item.get("o"),
                "high": item.get("h"),
                "low": item.get("l"),
                "close": item.get("c"),
                "volume": item.get("v"),
                "quoteVolume": item.get("q"),
                "takerBuyBaseVolume": item.get("V"),
                "tradesCount": item.get("n"),
                "closed": bool(item.get("x")),
            }
        if "u" in data and "b" in data and "a" in data:
            return {
                "kind": "book",
                "sourceTimeUs": str(int(data.get("E", received_us // 1_000)) * 1_000),
                "receivedTimeUs": str(received_us),
                "bid": data.get("b"),
                "bidSize": data.get("B"),
                "ask": data.get("a"),
                "askSize": data.get("A"),
                "updateId": str(data.get("u", "")),
            }
        return None

    async def connect(self, stop_event: asyncio.Event) -> None:
        last_error: Exception | None = None
        for url in self.URLS:
            try:
                await self._connect_url(url, stop_event)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # try Binance's other official public-data hosts.
                last_error = exc
        if last_error is not None:
            raise last_error

    async def _connect_url(self, url: str, stop_event: asyncio.Event) -> None:
        proxy: str | bool | None = self.proxy if self.proxy else True
        async with websockets.connect(
            url, proxy=proxy, ping_interval=20, ping_timeout=20,
            max_queue=1024, max_size=2 * 1024 * 1024
        ) as websocket:
            await self.on_status(self.venue, "live", None)
            while not stop_event.is_set():
                try:
                    raw = await asyncio.wait_for(websocket.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                received_us = _now_us()
                update = self.parse_message(raw, received_us)
                if update:
                    await self.on_update(self.venue, update, json.loads(raw), received_us)


class BinanceFuturesCollector(BinanceCollector):
    """Binance USDⓈ-M BTCUSDT perpetual market-data stream.

    The payloads for aggTrade/bookTicker/kline are compatible with the spot
    parser above; keeping a separate collector and venue label lets the lead-
    lag study compare spot and futures without mixing their histories.
    """

    venue = "BINANCE_FUTURES"
    STREAMS = (
        "btcusdt@trade/btcusdt@bookTicker/"
        "btcusdt@kline_1m/btcusdt@kline_5m"
    )
    URLS = (
        f"wss://fstream.binance.com/stream?streams={STREAMS}",
        f"wss://fstream.binance.com:443/stream?streams={STREAMS}",
    )


class HyperliquidCollector(ReconnectingCollector):
    venue = "HYPERLIQUID"
    URL = "wss://api.hyperliquid.xyz/ws"
    SUBSCRIPTIONS = (
        {"type": "allMids"},
        {"type": "trades", "coin": "BTC"},
        {"type": "bbo", "coin": "BTC"},
        {"type": "candle", "coin": "BTC", "interval": "1m"},
        {"type": "activeAssetCtx", "coin": "BTC"},
    )

    @staticmethod
    def parse_message(raw: Any, received_us: int) -> list[dict[str, Any]]:
        message = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        if not isinstance(message, dict):
            return []
        channel = message.get("channel")
        data = message.get("data")
        updates: list[dict[str, Any]] = []
        if channel == "allMids" and isinstance(data, dict):
            price = (data.get("mids") or {}).get("BTC")
            if price is not None:
                updates.append({"kind": "mid", "sourceTimeUs": str(received_us), "price": price})
        elif channel == "trades":
            for item in data if isinstance(data, list) else []:
                if item.get("coin") == "BTC":
                    updates.append({
                        "kind": "trade",
                        "sourceTimeUs": str(int(item.get("time", received_us // 1_000)) * 1_000),
                        "price": item.get("px"),
                        "quantity": item.get("sz"),
                        "side": item.get("side"),
                        "tradeId": str(item.get("tid", "")),
                    })
        elif channel == "bbo" and isinstance(data, dict) and data.get("coin") == "BTC":
            levels = data.get("bbo") or [None, None]
            bid = levels[0] or {}
            ask = levels[1] or {}
            updates.append({
                "kind": "book",
                "sourceTimeUs": str(int(data.get("time", received_us // 1_000)) * 1_000),
                "bid": bid.get("px"), "bidSize": bid.get("sz"),
                "ask": ask.get("px"), "askSize": ask.get("sz"),
            })
        elif channel == "candle" and isinstance(data, dict) and data.get("s") == "BTC":
            updates.append({
                "kind": "kline",
                "sourceTimeUs": str(int(data.get("T", data.get("t", received_us // 1_000))) * 1_000),
                "openTimeUs": str(int(data.get("t", 0)) * 1_000),
                "closeTimeUs": str(int(data.get("T", 0)) * 1_000),
                "open": data.get("o"), "high": data.get("h"), "low": data.get("l"),
                "close": data.get("c"), "volume": data.get("v"), "tradesCount": data.get("n"),
            })
        elif channel == "activeAssetCtx" and isinstance(data, dict):
            context = data.get("ctx", data)
            if data.get("coin", "BTC") == "BTC":
                updates.append({
                    "kind": "context", "sourceTimeUs": str(received_us),
                    "markPrice": context.get("markPx"), "oraclePrice": context.get("oraclePx"),
                    "midPrice": context.get("midPx"), "openInterest": context.get("openInterest"),
                    "funding": context.get("funding"),
                })
        for update in updates:
            update["receivedTimeUs"] = str(received_us)
        return updates

    async def connect(self, stop_event: asyncio.Event) -> None:
        proxy: str | bool | None = self.proxy if self.proxy else True
        async with websockets.connect(
            self.URL, proxy=proxy, ping_interval=20, ping_timeout=20,
            max_queue=1024, max_size=4 * 1024 * 1024
        ) as websocket:
            for subscription in self.SUBSCRIPTIONS:
                await websocket.send(json.dumps({"method": "subscribe", "subscription": subscription}))
            await self.on_status(self.venue, "live", None)
            last_app_ping = time.monotonic()
            while not stop_event.is_set():
                if time.monotonic() - last_app_ping >= 30:
                    await websocket.send(json.dumps({"method": "ping"}))
                    last_app_ping = time.monotonic()
                try:
                    raw = await asyncio.wait_for(websocket.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                received_us = _now_us()
                parsed = json.loads(raw)
                for update in self.parse_message(parsed, received_us):
                    await self.on_update(self.venue, update, parsed, received_us)


class DeribitCollector(ReconnectingCollector):
    venue = "DERIBIT"
    URL = "wss://www.deribit.com/ws/api/v2"

    def __init__(self, *args: Any, option_strikes_each_side: int = 12, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.option_strikes_each_side = option_strikes_each_side
        self._request_id = 0

    async def _rpc(self, websocket: Any, method: str, params: dict[str, Any]) -> Any:
        self._request_id += 1
        request_id = self._request_id
        await websocket.send(json.dumps({
            "jsonrpc": "2.0", "id": request_id, "method": method, "params": params
        }))
        while True:
            raw = await asyncio.wait_for(websocket.recv(), timeout=20)
            message = json.loads(raw)
            if message.get("id") == request_id:
                if message.get("error"):
                    raise RuntimeError(message["error"])
                return message.get("result")

    @staticmethod
    def select_option_instruments(
        instruments: list[dict[str, Any]], index_price: float, now_ms: int, strikes_each_side: int
    ) -> list[dict[str, Any]]:
        valid = [item for item in instruments if int(item.get("expiration_timestamp", 0)) > now_ms + 12 * 3_600_000]
        if not valid:
            return []
        expiry = min(int(item["expiration_timestamp"]) for item in valid)
        expiry_items = [item for item in valid if int(item["expiration_timestamp"]) == expiry]
        selected: list[dict[str, Any]] = []
        for option_type in ("call", "put"):
            typed = [item for item in expiry_items if item.get("option_type") == option_type]
            typed.sort(key=lambda item: abs(float(item.get("strike", 0)) - index_price))
            selected.extend(typed[: max(1, strikes_each_side)])
        return selected

    @staticmethod
    def parse_subscription(message: dict[str, Any], received_us: int) -> dict[str, Any] | None:
        params = message.get("params") or {}
        channel = str(params.get("channel", ""))
        data = params.get("data") or {}
        if channel == "trades.option.BTC.100ms":
            trades = data if isinstance(data, list) else []
            source_ms = max(
                (int(item.get("timestamp", 0)) for item in trades if isinstance(item, dict)),
                default=received_us // 1_000,
            )
            return {
                "kind": "option_trades",
                "sourceTimeUs": str(source_ms * 1_000),
                "receivedTimeUs": str(received_us),
                "trades": [
                    {
                        "tradeId": str(item.get("trade_id", "")),
                        "tradeSequence": item.get("trade_seq"),
                        "timestampUs": str(
                            int(item.get("timestamp", received_us // 1_000)) * 1_000
                        ),
                        "instrument": item.get("instrument_name"),
                        "direction": item.get("direction"),
                        "amount": item.get("amount"),
                        "contracts": item.get("contracts"),
                        "price": item.get("price"),
                        "iv": item.get("iv"),
                        "indexPrice": item.get("index_price"),
                        "markPrice": item.get("mark_price"),
                        "tickDirection": item.get("tick_direction"),
                    }
                    for item in trades
                    if isinstance(item, dict)
                ],
            }
        if not isinstance(data, dict):
            return None
        source_us = int(data.get("timestamp", received_us // 1_000)) * 1_000
        base = {"sourceTimeUs": str(source_us), "receivedTimeUs": str(received_us)}
        if channel == "deribit_price_index.btc_usd":
            return {**base, "kind": "index", "price": data.get("price")}
        if channel == "ticker.BTC-PERPETUAL.100ms":
            return {
                **base, "kind": "perpetual", "lastPrice": data.get("last_price"),
                "markPrice": data.get("mark_price"), "indexPrice": data.get("index_price"),
                "funding8h": data.get("funding_8h"), "openInterest": data.get("open_interest"),
            }
        if channel.startswith("ticker.BTC-"):
            instrument = str(data.get("instrument_name") or channel.split(".", 2)[1])
            return {
                **base, "kind": "option", "instrument": instrument,
                "markIv": data.get("mark_iv"), "bidIv": data.get("bid_iv"),
                "askIv": data.get("ask_iv"), "markPrice": data.get("mark_price"),
                "underlyingPrice": data.get("underlying_price"), "openInterest": data.get("open_interest"),
                "delta": (data.get("greeks") or {}).get("delta"),
            }
        if channel == "deribit_volatility_index.btc_usd":
            value = (
                data.get("volatility") or data.get("close")
                or data.get("last") or data.get("value")
            )
            return {**base, "kind": "volatility_index", "value": value}
        return None

    async def connect(self, stop_event: asyncio.Event) -> None:
        proxy: str | bool | None = self.proxy if self.proxy else True
        async with websockets.connect(
            self.URL, proxy=proxy, ping_interval=20, ping_timeout=20,
            max_queue=1024, max_size=8 * 1024 * 1024
        ) as websocket:
            index = await self._rpc(websocket, "public/get_index_price", {"index_name": "btc_usd"})
            instruments = await self._rpc(websocket, "public/get_instruments", {
                "currency": "BTC", "kind": "option", "expired": False
            })
            selected = self.select_option_instruments(
                instruments or [], float(index["index_price"]), int(time.time() * 1_000),
                self.option_strikes_each_side,
            )
            await self.on_update(
                self.venue,
                {"kind": "instruments", "sourceTimeUs": str(_now_us()), "instruments": selected},
                {"selectedOptionInstruments": selected},
                _now_us(),
            )
            channels = [
                "deribit_price_index.btc_usd",
                "ticker.BTC-PERPETUAL.100ms",
                "deribit_volatility_index.btc_usd",
                "trades.option.BTC.100ms",
                *[f"ticker.{item['instrument_name']}.100ms" for item in selected],
            ]
            await self._rpc(websocket, "public/subscribe", {"channels": channels})
            await self.on_status(self.venue, "live", None)
            while not stop_event.is_set():
                try:
                    raw = await asyncio.wait_for(websocket.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                received_us = _now_us()
                parsed = json.loads(raw)
                update = self.parse_subscription(parsed, received_us)
                if update:
                    await self.on_update(self.venue, update, parsed, received_us)


class PolymarketReferenceCollector(ReconnectingCollector):
    """Polymarket RTDS Binance and Chainlink reference-price streams."""

    venue = "POLYMARKET_RTDS"
    URL = "wss://ws-live-data.polymarket.com"
    SUBSCRIPTION = {
        "action": "subscribe",
        "subscriptions": [
            {
                "topic": "crypto_prices",
                "type": "update",
                "filters": '{"symbol":"BTCUSDT"}',
            },
            {
                "topic": "crypto_prices_chainlink",
                "type": "update",
                "filters": '{"symbol":"BTC/USD"}',
            },
        ],
    }

    @staticmethod
    def parse_message(raw: Any, received_us: int) -> dict[str, Any] | None:
        message = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        if not isinstance(message, dict):
            return None
        topic = str(message.get("topic", ""))
        payload = message.get("payload") or {}
        if topic not in {"crypto_prices", "crypto_prices_chainlink"}:
            return None
        # Filtered subscriptions may begin with a small historical snapshot.
        # Use its newest observation so the live state is initialized without
        # retaining or replaying the entire snapshot.
        data = payload.get("data")
        observation = payload
        if isinstance(data, list):
            candidates = [item for item in data if isinstance(item, dict)]
            if not candidates:
                return None
            observation = max(
                candidates,
                key=lambda item: int(item.get("timestamp") or 0),
            )
        price = observation.get("value")
        timestamp_ms = observation.get("timestamp") or message.get("timestamp")
        if price is None or timestamp_ms is None:
            return None
        symbol = str(observation.get("symbol") or payload.get("symbol") or "")
        is_chainlink = topic == "crypto_prices_chainlink" or "/" in symbol
        return {
            "kind": "reference",
            "source": "CHAINLINK" if is_chainlink else "POLYMARKET_BINANCE",
            "symbol": symbol,
            "price": price,
            "sourceTimeUs": str(int(timestamp_ms) * 1_000),
            "receivedTimeUs": str(received_us),
        }

    async def connect(self, stop_event: asyncio.Event) -> None:
        proxy: str | bool | None = self.proxy if self.proxy else True
        async with websockets.connect(
            self.URL, proxy=proxy, ping_interval=None,
            max_queue=1024, max_size=2 * 1024 * 1024
        ) as websocket:
            await websocket.send(json.dumps(self.SUBSCRIPTION))
            await self.on_status(self.venue, "live", None)
            last_ping = time.monotonic()
            while not stop_event.is_set():
                if time.monotonic() - last_ping >= 5:
                    await websocket.send("PING")
                    last_ping = time.monotonic()
                try:
                    raw = await asyncio.wait_for(websocket.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                if not raw or raw == "PONG":
                    continue
                received_us = _now_us()
                parsed = json.loads(raw)
                update = self.parse_message(parsed, received_us)
                if update:
                    await self.on_update(self.venue, update, parsed, received_us)
