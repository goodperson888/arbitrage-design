from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore

from ..models import MarketEvent


@dataclass
class BinanceKline:
    symbol: str
    open_time: int
    close_time: int
    open_price: str
    high_price: str
    low_price: str
    close_price: str
    volume: str
    trades_count: int


@dataclass
class BinanceTrade:
    symbol: str
    trade_id: int
    price: str
    quantity: str
    time: int
    is_buyer_maker: bool


class BinanceBackfill:
    """Binance historical data backfill connector for reference prices."""
    
    BASE_URL = "https://api.binance.com"
    
    def __init__(self, timeout: int = 30, proxy: str | None = None):
        if httpx is None:
            raise ImportError("httpx is required for connectors. Install with: pip install httpx")
        
        # Setup proxy if provided
        self.client = httpx.Client(timeout=timeout, proxy=proxy)
        self._rate_limit_delay = 0.05  # 50ms between requests
    
    def _get(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any] | list[Any]:
        """Make a GET request with rate limiting."""
        response = self.client.get(f"{self.BASE_URL}{endpoint}", params=params)
        response.raise_for_status()
        time.sleep(self._rate_limit_delay)
        return response.json()
    
    def get_klines(
        self,
        symbol: str,
        interval: str = "1m",
        limit: int = 1000,
        start_time: int | None = None,
        end_time: int | None = None,
    ) -> list[BinanceKline]:
        """Get kline/candlestick data from Binance."""
        params = {
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
        }
        if start_time:
            params["startTime"] = start_time
        if end_time:
            params["endTime"] = end_time
        
        data = self._get("/api/v3/klines", params)
        
        klines = []
        for item in data:
            kline = BinanceKline(
                symbol=symbol,
                open_time=int(item[0]),
                close_time=int(item[6]),
                open_price=str(item[1]),
                high_price=str(item[2]),
                low_price=str(item[3]),
                close_price=str(item[4]),
                volume=str(item[5]),
                trades_count=int(item[8]),
            )
            klines.append(kline)
        
        return klines
    
    def get_trades(
        self,
        symbol: str,
        limit: int = 1000,
        from_id: int | None = None,
    ) -> list[BinanceTrade]:
        """Get recent trades from Binance."""
        params = {
            "symbol": symbol,
            "limit": limit,
        }
        if from_id:
            params["fromId"] = from_id
        
        data = self._get("/api/v3/trades", params)
        
        trades = []
        for item in data:
            trade = BinanceTrade(
                symbol=symbol,
                trade_id=int(item["id"]),
                price=str(item["price"]),
                quantity=str(item["qty"]),
                time=int(item["time"]),
                is_buyer_maker=bool(item["isBuyerMaker"]),
            )
            trades.append(trade)
        
        return trades
    
    def klines_to_events(self, klines: list[BinanceKline]) -> list[MarketEvent]:
        """Convert Binance klines to REFERENCE_PRICE events."""
        events = []
        
        for kline in klines:
            # Use close time as the reference time
            ref_time_us = kline.close_time * 1000  # Convert ms to microseconds
            
            event = MarketEvent(
                schema_version=1,
                event_type="REFERENCE_PRICE",
                venue="BINANCE",
                market_id=f"{kline.symbol.lower()}_spot",
                source_time_us=ref_time_us,
                received_time_us=int(time.time() * 1_000_000),
                payload={
                    "symbol": kline.symbol,
                    "price": kline.close_price,
                    "volume": kline.volume,
                    "high": kline.high_price,
                    "low": kline.low_price,
                    "open": kline.open_price,
                    "tradesCount": kline.trades_count,
                },
                source={
                    "type": "PLATFORM_API",
                    "collector": "arbitrage-research",
                    "connectorVersion": "1.0.0",
                },
            )
            events.append(event)
        
        return events
    
    def trades_to_events(self, trades: list[BinanceTrade]) -> list[MarketEvent]:
        """Convert Binance trades to REFERENCE_PRICE events."""
        events = []
        
        for trade in trades:
            trade_time_us = trade.time * 1000  # Convert ms to microseconds
            
            event = MarketEvent(
                schema_version=1,
                event_type="REFERENCE_PRICE",
                venue="BINANCE",
                market_id=f"{trade.symbol.lower()}_spot",
                source_time_us=trade_time_us,
                received_time_us=int(time.time() * 1_000_000),
                payload={
                    "symbol": trade.symbol,
                    "price": trade.price,
                    "quantity": trade.quantity,
                    "isBuyerMaker": trade.is_buyer_maker,
                },
                source={
                    "type": "PLATFORM_API",
                    "collector": "arbitrage-research",
                },
            )
            events.append(event)
        
        return events
    
    def close(self) -> None:
        """Close the HTTP client."""
        self.client.close()
    
    def __enter__(self) -> "BinanceBackfill":
        return self
    
    def __exit__(self, *args: Any) -> None:
        self.close()