from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, AsyncIterator, Iterable, Mapping, Optional, Protocol

from .readonly import PublicWebSocketConnector, WebSocketConnector


def _parse_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, (int, float)):
        result = datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc)
    else:
        text = str(value).replace("Z", "+00:00")
        numeric = text.strip()
        if numeric.replace(".", "", 1).isdigit():
            result = datetime.fromtimestamp(float(numeric) / 1000, tz=timezone.utc)
        else:
            result = datetime.fromisoformat(text)
    if result.tzinfo is None:
        return result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


@dataclass(frozen=True)
class NormalizedPolymarketTrade:
    market_id: str
    token_id: str
    side: str
    aggressor: str
    price: Decimal
    quantity: Decimal
    event_time: datetime


@dataclass(frozen=True)
class NormalizedBinanceCandle:
    symbol: str
    open_time: datetime
    close_time: datetime
    open: Decimal
    close: Decimal

    @property
    def return_value(self) -> Decimal:
        if self.open == 0:
            return Decimal("0")
        return (self.close - self.open) / self.open


def rolling_binance_return(candles: Iterable[NormalizedBinanceCandle]) -> Decimal:
    """Calculate the latest rolling Spot return from one-minute normalized candles."""
    ordered = sorted(candles, key=lambda candle: candle.open_time)
    if len(ordered) < 5:
        raise ValueError("at least five one-minute candles are required")
    window = ordered[-5:]
    if window[0].open == 0:
        return Decimal("0")
    return (window[-1].close - window[0].open) / window[0].open


def normalize_polymarket_message(message: Mapping[str, Any]) -> NormalizedPolymarketTrade:
    event_type = str(message.get("type", message.get("event_type", ""))).lower()
    if event_type not in {"trade", "last_trade", "last_trade_price", "fill"}:
        raise ValueError("unsupported Polymarket message type")
    side = str(message.get("side", "")).upper()
    if side not in {"BUY", "SELL"}:
        raise ValueError("Polymarket trade side must be BUY or SELL")
    return NormalizedPolymarketTrade(
        market_id=str(message.get("market_id", message.get("marketId", ""))),
        token_id=str(message.get("token_id", message.get("tokenId", message.get("asset_id", message.get("assetId", ""))))),
        side=side,
        aggressor=side,
        price=Decimal(str(message["price"])),
        quantity=Decimal(str(message.get("size", message.get("quantity", "0")))),
        event_time=_parse_time(message.get("timestamp", message.get("timestamp_ms", message.get("sourceTimeUs")))),
    )


def normalize_binance_kline(message: Mapping[str, Any]) -> NormalizedBinanceCandle:
    kline = message.get("k", message)
    return NormalizedBinanceCandle(
        symbol=str(message.get("s", kline.get("s", "BTCUSDT"))),
        open_time=_parse_time(kline["t"]),
        close_time=_parse_time(kline["T"]),
        open=Decimal(str(kline["o"])),
        close=Decimal(str(kline["c"])),
    )


class PolymarketWebSocketAdapter(Protocol):
    async def connect(self, subscriptions: list[str]) -> None: ...
    async def messages(self) -> AsyncIterator[Mapping[str, Any]]: ...
    async def close(self) -> None: ...


class BinanceSpotWebSocketAdapter(Protocol):
    async def connect(self, symbol: str) -> None: ...
    async def messages(self) -> AsyncIterator[Mapping[str, Any]]: ...
    async def close(self) -> None: ...


class DisabledWebSocketAdapter:
    """Placeholder adapter: no network connection is possible in this phase."""

    async def connect(self, *_: Any, **__: Any) -> None:
        raise RuntimeError("websocket transport is disabled in safety skeleton")

    async def messages(self) -> AsyncIterator[Mapping[str, Any]]:
        if False:
            yield {}
        raise RuntimeError("websocket transport is disabled in safety skeleton")

    async def close(self) -> None:
        return None


class PolymarketReadOnlyAdapter:
    """Public market/trade subscription adapter; it has no order methods."""

    def __init__(self, url: str, connector: Optional[WebSocketConnector] = None) -> None:
        self.connector = connector or PublicWebSocketConnector(url)

    async def connect(self, token_ids: list[str]) -> None:
        await self.connector.connect()
        await self.connector.send({"assets_ids": token_ids, "type": "market"})

    async def messages(self) -> AsyncIterator[Mapping[str, Any]]:
        while True:
            value = await self.connector.recv()
            if isinstance(value, Mapping):
                yield value

    async def close(self) -> None:
        await self.connector.close()


class BinanceSpotReadOnlyAdapter:
    """Public Binance Spot kline adapter; stream is read-only by construction."""

    def __init__(self, symbol: str = "BTCUSDT", connector: Optional[WebSocketConnector] = None) -> None:
        self.symbol = symbol.upper()
        url = f"wss://stream.binance.com:9443/ws/{self.symbol.lower()}@kline_1m"
        self.connector = connector or PublicWebSocketConnector(url, enabled_env="BINANCE_READONLY_NETWORK_ENABLED")

    async def connect(self) -> None:
        await self.connector.connect()

    async def messages(self) -> AsyncIterator[Mapping[str, Any]]:
        while True:
            value = await self.connector.recv()
            if isinstance(value, Mapping):
                yield value

    async def close(self) -> None:
        await self.connector.close()
