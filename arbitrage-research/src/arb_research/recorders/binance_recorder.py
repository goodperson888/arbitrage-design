from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

try:
    import websockets
except ImportError:
    websockets = None  # type: ignore

from .base_recorder import BaseRecorder, RecorderConfig
from ..models import MarketEvent


class BinanceRecorder(BaseRecorder):
    """Binance WebSocket recorder for real-time market data."""
    
    BASE_WS_URL = "wss://stream.binance.com:9443/ws"
    
    def __init__(self, config: RecorderConfig, symbols: list[str], proxy: str | None = None):
        super().__init__(config)
        
        if websockets is None:
            raise ImportError("websockets is required for recorders. Install with: pip install websockets")
        
        self.symbols = symbols
        self.proxy = proxy
        self._running = False
        self._websocket_task: asyncio.Task | None = None
    
    def _build_stream_url(self) -> str:
        """Build WebSocket URL for the given symbols."""
        # For trade streams: <symbol>@trade
        # For kline streams: <symbol>@kline_<interval>
        streams = []
        for symbol in self.symbols:
            symbol_lower = symbol.lower()
            streams.append(f"{symbol_lower}@trade")
            streams.append(f"{symbol_lower}@kline_1m")
        
        return f"{self.BASE_WS_URL}/{'/'.join(streams)}"
    
    async def _ws_handler(self) -> None:
        """WebSocket message handler."""
        uri = self._build_stream_url()
        
        # Setup proxy if provided
        proxy_config = None
        if self.proxy:
            proxy_config = {
                "proxy": self.proxy,
            }
        
        try:
            async with websockets.connect(uri, **(proxy_config or {})) as websocket:
                print(f"Connected to Binance WebSocket for {self.symbols}")
                
                while self._running:
                    try:
                        message = await asyncio.wait_for(websocket.recv(), timeout=1.0)
                        await self._process_message(message)
                    except asyncio.TimeoutError:
                        continue
                    except Exception as e:
                        print(f"WebSocket error: {e}")
                        break
        
        except Exception as e:
            print(f"WebSocket connection error: {e}")
    
    async def _process_message(self, message: str) -> None:
        """Process a WebSocket message and convert to MarketEvent."""
        try:
            data = json.loads(message)
            event_type = data.get("e")
            
            if event_type == "trade":
                await self._process_trade(data)
            elif event_type == "kline":
                await self._process_kline(data)
        
        except Exception as e:
            print(f"Error processing message: {e}")
    
    async def _process_trade(self, data: dict[str, Any]) -> None:
        """Process a trade event."""
        symbol = data.get("s", "")
        trade_time_us = int(data.get("T", 0)) * 1000  # Convert ms to microseconds
        received_time_us = int(time.time() * 1_000_000)
        
        event = MarketEvent(
            schema_version=1,
            event_type="REFERENCE_PRICE",
            venue="BINANCE",
            market_id=f"{symbol.lower()}_spot",
            source_time_us=trade_time_us,
            received_time_us=received_time_us,
            payload={
                "symbol": symbol,
                "price": str(data.get("p", "0")),
                "quantity": str(data.get("q", "0")),
                "isBuyerMaker": data.get("m", False),
                "tradeId": str(data.get("t", "")),
            },
            source={
                "type": "PLATFORM_WEBSOCKET",
                "collector": "arbitrage-research",
                "connectorVersion": "1.0.0",
            },
        )
        
        self.record_event(event)
    
    async def _process_kline(self, data: dict[str, Any]) -> None:
        """Process a kline event."""
        kline_data = data.get("k", {})
        symbol = data.get("s", "")
        
        # Only process closed klines
        if not kline_data.get("x", False):
            return
        
        close_time_us = int(kline_data.get("t", 0)) * 1000  # Convert ms to microseconds
        received_time_us = int(time.time() * 1_000_000)
        
        event = MarketEvent(
            schema_version=1,
            event_type="REFERENCE_PRICE",
            venue="BINANCE",
            market_id=f"{symbol.lower()}_spot",
            source_time_us=close_time_us,
            received_time_us=received_time_us,
            payload={
                "symbol": symbol,
                "price": str(kline_data.get("c", "0")),
                "open": str(kline_data.get("o", "0")),
                "high": str(kline_data.get("h", "0")),
                "low": str(kline_data.get("l", "0")),
                "volume": str(kline_data.get("v", "0")),
                "tradesCount": int(kline_data.get("n", 0)),
                "interval": kline_data.get("i", ""),
            },
            source={
                "type": "PLATFORM_WEBSOCKET",
                "collector": "arbitrage-research",
                "connectorVersion": "1.0.0",
            },
        )
        
        self.record_event(event)
    
    def collect(self) -> None:
        """Start collecting data from Binance WebSocket."""
        if self._running:
            return
        
        self._running = True
        self.start()  # Start the base recorder
        
        # Run WebSocket handler in async context
        try:
            asyncio.run(self._ws_handler())
        except KeyboardInterrupt:
            print("Stopping recorder...")
        finally:
            self.stop()
    
    def stop(self) -> None:
        """Stop the recorder."""
        self._running = False
        super().stop()