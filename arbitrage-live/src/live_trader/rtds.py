"""Read-only adapter for Polymarket's Real-Time Data Service (RTDS).

RTDS is a reference feed only.  It never places orders and is disabled unless
the caller explicitly enables the public network connection.  Market/CLOB
execution remains a separate, independently gated path.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, AsyncIterator, Mapping, Sequence

from .readonly import PublicWebSocketConnector, ReadOnlyNetworkDisabled

RTDS_URL = "wss://ws-live-data.polymarket.com"
RTDS_NETWORK_ENV = "POLYMARKET_RTDS_NETWORK_ENABLED"


def _source_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and not value.strip().replace(".", "", 1).isdigit():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("RTDS message has invalid timestamp") from exc
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("RTDS message has invalid timestamp") from exc
    # RTDS examples use milliseconds while replay fixtures often use seconds.
    if numeric > 100_000_000_000:
        numeric /= 1000.0
    return datetime.fromtimestamp(numeric, tz=timezone.utc)


def _json_filters(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), separators=(",", ":"), ensure_ascii=True)


def normalize_rtds_message(message: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize both RTDS raw topics and SDK-style topic names.

    The returned value deliberately uses ``Decimal`` and timezone-aware UTC
    timestamps so reference values cannot introduce binary-float rounding into
    strategy diagnostics.
    """

    if not isinstance(message, Mapping):
        raise ValueError("RTDS message must be an object")
    topic = str(message.get("topic") or "")
    payload = message.get("payload")
    if not isinstance(payload, Mapping):
        raise ValueError("RTDS message payload must be an object")
    source_by_topic = {
        "crypto_prices": "binance",
        "prices.crypto.binance": "binance",
        "crypto_prices_chainlink": "chainlink",
        "prices.crypto.chainlink": "chainlink",
        "prices.crypto.chainlink.twap": "chainlink_twap",
        "crypto_prices_chainlink_twap": "chainlink_twap",
    }
    source = source_by_topic.get(topic)
    if source is None:
        raise ValueError(f"unsupported RTDS topic: {topic or '<missing>'}")
    symbol = payload.get("symbol")
    value = payload.get("value")
    if symbol in (None, "") or value in (None, ""):
        raise ValueError("RTDS message requires symbol and value")
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("RTDS message has invalid value") from exc
    timestamp = payload.get("timestamp", message.get("timestamp"))
    result: dict[str, Any] = {
        "source": source,
        "topic": topic,
        "symbol": str(symbol),
        "value": decimal_value,
        "source_time": _source_time(timestamp),
    }
    window = payload.get("windowSeconds", payload.get("window_seconds"))
    if source == "chainlink_twap" and window is not None:
        try:
            result["window_seconds"] = int(window)
        except (TypeError, ValueError) as exc:
            raise ValueError("RTDS TWAP window must be an integer") from exc
    return result


class RTDSReadOnlyAdapter:
    """Public RTDS connection with explicit network opt-in and no order API."""

    def __init__(
        self,
        *,
        url: str = RTDS_URL,
        connector: Any | None = None,
        enabled: bool | None = None,
        enabled_env: str = RTDS_NETWORK_ENV,
    ) -> None:
        self.url = url
        self.enabled = enabled
        self.enabled_env = enabled_env
        self.connector = connector or PublicWebSocketConnector(url, enabled_env=enabled_env)
        self.connected = False

    def _network_enabled(self) -> bool:
        if self.enabled is not None:
            return bool(self.enabled)
        return os.environ.get(self.enabled_env, "").strip() == "1"

    async def connect(
        self,
        *,
        sources: Sequence[str] = ("binance", "chainlink"),
        symbols: Sequence[str] = ("btcusdt", "btc/usd"),
        twap_windows: Sequence[int] = (30, 60),
    ) -> None:
        if not self._network_enabled():
            raise ReadOnlyNetworkDisabled(
                f"RTDS network disabled; set {self.enabled_env}=1 for read-only diagnostics"
            )
        await self.connector.connect()
        subscriptions: list[dict[str, Any]] = []
        for source in sources:
            if source == "binance":
                binance_symbols = tuple(symbol for symbol in symbols if "/" not in symbol)
                if binance_symbols:
                    subscriptions.append({
                        "topic": "crypto_prices",
                        "type": "update",
                        "filters": ",".join(binance_symbols),
                    })
            elif source == "chainlink":
                chainlink_symbols = tuple(symbol for symbol in symbols if "/" in symbol)
                for symbol in chainlink_symbols:
                    subscriptions.append({
                        "topic": "crypto_prices_chainlink",
                        "type": "*",
                        "filters": _json_filters({"symbol": symbol}),
                    })
            elif source == "chainlink_twap":
                chainlink_symbols = tuple(symbol for symbol in symbols if "/" in symbol)
                for symbol in chainlink_symbols:
                    for window in twap_windows:
                        subscriptions.append({
                            "topic": "prices.crypto.chainlink.twap",
                            "type": "update",
                            "filters": _json_filters({"symbol": symbol, "windowSeconds": int(window)}),
                        })
            else:
                raise ValueError(f"unsupported RTDS source: {source}")
        if subscriptions:
            await self.connector.send({"action": "subscribe", "subscriptions": subscriptions})
        self.connected = True

    async def messages(self) -> AsyncIterator[dict[str, Any]]:
        if not self.connected:
            raise RuntimeError("RTDS adapter is not connected")
        while self.connected:
            message = await self.connector.recv()
            if isinstance(message, str):
                if message.strip().upper() in {"PING", "PONG", ""}:
                    continue
                try:
                    message = json.loads(message)
                except json.JSONDecodeError:
                    continue
            if not isinstance(message, Mapping):
                continue
            yield normalize_rtds_message(message)

    async def heartbeat(self) -> None:
        """Send the RTDS application heartbeat when the connector supports it."""

        send_text = getattr(self.connector, "send_text", None)
        if send_text is not None:
            await send_text("PING")
        else:
            # Test doubles and older connectors expose only JSON send.  The
            # transport remains read-only; production connector can override
            # send_text to emit the documented raw PING frame.
            await self.connector.send("PING")

    async def close(self) -> None:
        self.connected = False
        await self.connector.close()
