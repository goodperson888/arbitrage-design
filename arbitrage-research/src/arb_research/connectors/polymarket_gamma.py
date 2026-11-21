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
class PolymarketGammaEvent:
    """Polymarket Gamma API事件数据."""
    event_id: str
    question: str
    description: str
    end_date: str
    active: bool
    tags: list[dict[str, Any]]
    markets: list[dict[str, Any]]


@dataclass
class PolymarketClobPrice:
    """Polymarket CLOB API价格数据."""
    token_id: str
    price: str
    timestamp: int


class PolymarketGammaClient:
    """Polymarket Gamma API客户端 - 无需认证的公开数据."""
    
    BASE_URL = "https://gamma-api.polymarket.com"
    
    def __init__(self, timeout: int = 30, proxy: str | None = None):
        if httpx is None:
            raise ImportError("httpx is required for connectors. Install with: pip install httpx")
        
        self.client = httpx.Client(timeout=timeout, proxy=proxy)
        self._rate_limit_delay = 0.1  # 100ms between requests
    
    def _get(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Make a GET request with rate limiting."""
        response = self.client.get(f"{self.BASE_URL}{endpoint}", params=params)
        response.raise_for_status()
        time.sleep(self._rate_limit_delay)
        return response.json()
    
    def get_events(
        self,
        active: bool = True,
        closed: bool = False,
        limit: int = 100,
        tag_id: str | None = None,
    ) -> list[PolymarketGammaEvent]:
        """获取事件列表."""
        params = {
            "active": str(active).lower(),
            "closed": str(closed).lower(),
            "limit": limit,
        }
        if tag_id:
            params["tag_id"] = tag_id
        
        data = self._get("/events", params)
        
        events = []
        for item in data.get("data", []):
            event = PolymarketGammaEvent(
                event_id=item.get("id", ""),
                question=item.get("question", ""),
                description=item.get("description", ""),
                end_date=item.get("end_date", ""),
                active=item.get("active", False),
                tags=item.get("tags", []),
                markets=item.get("markets", []),
            )
            events.append(event)
        
        return events
    
    def get_markets(
        self,
        slug: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """获取市场列表."""
        params = {"limit": limit}
        if slug:
            params["slug"] = slug
        
        data = self._get("/markets", params)
        return data.get("data", [])
    
    def get_tags(self) -> list[dict[str, Any]]:
        """获取所有标签."""
        data = self._get("/tags")
        return data.get("data", [])
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.client.close()


class PolymarketClobClient:
    """Polymarket CLOB API客户端 - 价格和订单簿数据."""
    
    BASE_URL = "https://clob.polymarket.com"
    
    def __init__(self, timeout: int = 30, proxy: str | None = None):
        if httpx is None:
            raise ImportError("httpx is required for connectors. Install with: pip install httpx")
        
        self.client = httpx.Client(timeout=timeout, proxy=proxy)
        self._rate_limit_delay = 0.1
    
    def _get(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Make a GET request with rate limiting."""
        response = self.client.get(f"{self.BASE_URL}{endpoint}", params=params)
        response.raise_for_status()
        time.sleep(self._rate_limit_delay)
        return response.json()
    
    def get_price(self, token_id: str, side: str = "buy") -> PolymarketClobPrice:
        """获取特定token的当前价格."""
        params = {"token_id": token_id, "side": side}
        data = self._get("/price", params)
        
        return PolymarketClobPrice(
            token_id=token_id,
            price=str(data.get("price", 0)),
            timestamp=int(data.get("timestamp", 0)),
        )
    
    def get_order_book(self, token_id: str) -> dict[str, Any]:
        """获取完整订单簿深度."""
        params = {"token_id": token_id}
        return self._get("/book", params)
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.client.close()