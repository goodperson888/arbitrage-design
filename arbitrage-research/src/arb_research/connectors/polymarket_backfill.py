from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore

from ..models import MarketEvent


@dataclass
class PolymarketMarket:
    market_id: str
    question: str
    description: str
    end_date: str
    tokens: list[dict[str, Any]]
    outcomes: list[str]


@dataclass
class PolymarketTrade:
    market_id: str
    token_id: str
    outcome: str
    price: str
    size: str
    timestamp: str


class PolymarketBackfill:
    """Polymarket historical data backfill connector."""
    
    BASE_URL = "https://api.polymarket.com"
    
    def __init__(self, timeout: int = 30, proxy: str | None = None):
        if httpx is None:
            raise ImportError("httpx is required for connectors. Install with: pip install httpx")
        
        # Setup proxy if provided
        self.client = httpx.Client(timeout=timeout, proxy=proxy)
        self._rate_limit_delay = 0.1  # 100ms between requests
    
    def _get(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Make a GET request with rate limiting."""
        response = self.client.get(f"{self.BASE_URL}{endpoint}", params=params)
        response.raise_for_status()
        time.sleep(self._rate_limit_delay)
        return response.json()
    
    def get_markets(
        self,
        limit: int = 100,
        offset: int = 0,
        closed: bool = True,
    ) -> list[PolymarketMarket]:
        """Get list of markets from Polymarket."""
        params = {
            "limit": limit,
            "offset": offset,
            "closed": str(closed).lower(),
        }
        data = self._get("/markets", params)
        
        markets = []
        for item in data.get("data", []):
            tokens = item.get("tokens", [])
            outcomes = [token.get("outcome") for token in tokens if token.get("outcome")]
            
            market = PolymarketMarket(
                market_id=item.get("condition_id", ""),
                question=item.get("question", ""),
                description=item.get("description", ""),
                end_date=item.get("end_date", ""),
                tokens=tokens,
                outcomes=outcomes,
            )
            markets.append(market)
        
        return markets
    
    def get_market_trades(self, market_id: str, limit: int = 100) -> list[PolymarketTrade]:
        """Get trades for a specific market."""
        params = {"limit": limit}
        data = self._get(f"/markets/{market_id}/trades", params)
        
        trades = []
        for item in data.get("data", []):
            trade = PolymarketTrade(
                market_id=market_id,
                token_id=item.get("token_id", ""),
                outcome=item.get("outcome", ""),
                price=str(item.get("price", 0)),
                size=str(item.get("size", 0)),
                timestamp=item.get("timestamp", ""),
            )
            trades.append(trade)
        
        return trades
    
    def market_to_events(self, market: PolymarketMarket) -> list[MarketEvent]:
        """Convert a Polymarket market to MarketEvent objects."""
        events = []
        
        # Parse end date to microseconds
        try:
            end_datetime = datetime.fromisoformat(market.end_date.replace("Z", "+00:00"))
            end_timestamp = int(end_datetime.timestamp() * 1_000_000)
            start_timestamp = end_timestamp - (5 * 60 * 1_000_000)  # Assume 5 min duration
        except (ValueError, AttributeError):
            return events
        
        # MARKET_DEFINITION event
        definition_event = MarketEvent(
            schema_version=1,
            event_type="MARKET_DEFINITION",
            venue="POLYMARKET",
            market_id=market.market_id,
            source_time_us=start_timestamp,
            received_time_us=int(time.time() * 1_000_000),
            payload={
                "underlying": market.question,
                "eventStartUs": str(start_timestamp),
                "eventEndUs": str(end_timestamp),
                "outcomes": market.outcomes,
            },
            source={
                "type": "PLATFORM_API",
                "collector": "arbitrage-research",
                "connectorVersion": "1.0.0",
            },
        )
        events.append(definition_event)
        
        # TOKEN_DEFINITION events for each token
        for token in market.tokens:
            token_event = MarketEvent(
                schema_version=1,
                event_type="TOKEN_DEFINITION",
                venue="POLYMARKET",
                market_id=market.market_id,
                instrument_id=token.get("token_id"),
                source_time_us=start_timestamp,
                received_time_us=int(time.time() * 1_000_000),
                payload={
                    "outcome": token.get("outcome"),
                    "tokenId": token.get("token_id"),
                },
                source={
                    "type": "PLATFORM_API",
                    "collector": "arbitrage-research",
                },
            )
            events.append(token_event)
        
        return events
    
    def trades_to_events(self, trades: list[PolymarketTrade]) -> list[MarketEvent]:
        """Convert Polymarket trades to MarketEvent objects."""
        events = []
        
        for trade in trades:
            try:
                trade_datetime = datetime.fromisoformat(trade.timestamp.replace("Z", "+00:00"))
                trade_timestamp = int(trade_datetime.timestamp() * 1_000_000)
            except (ValueError, AttributeError):
                continue
            
            trade_event = MarketEvent(
                schema_version=1,
                event_type="TRADE",
                venue="POLYMARKET",
                market_id=trade.market_id,
                instrument_id=trade.token_id,
                outcome=trade.outcome,
                source_time_us=trade_timestamp,
                received_time_us=int(time.time() * 1_000_000),
                payload={
                    "price": trade.price,
                    "size": trade.size,
                    "side": "UNKNOWN",  # API doesn't provide side
                },
                source={
                    "type": "PLATFORM_API",
                    "collector": "arbitrage-research",
                },
            )
            events.append(trade_event)
        
        return events
    
    def close(self) -> None:
        """Close the HTTP client."""
        self.client.close()
    
    def __enter__(self) -> "PolymarketBackfill":
        return self
    
    def __exit__(self, *args: Any) -> None:
        self.close()