from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional, Protocol


class ReadOnlyNetworkDisabled(RuntimeError):
    pass


class ConnectionState(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    STALE = "stale"
    STOPPED = "stopped"


@dataclass
class ConnectionHealth:
    state: ConnectionState = ConnectionState.DISCONNECTED
    last_message_at: Optional[datetime] = None
    last_heartbeat_at: Optional[datetime] = None
    reconnects: int = 0
    disconnect_reason: str = ""


class WebSocketConnector(Protocol):
    async def connect(self) -> None: ...
    async def send(self, payload: Any) -> None: ...
    async def send_text(self, payload: str) -> None: ...
    async def recv(self) -> Any: ...
    async def ping(self) -> None: ...
    async def close(self) -> None: ...


class PublicWebSocketConnector:
    """Lazy websocket connector. It is read-only and disabled unless explicitly enabled."""

    def __init__(self, url: str, enabled_env: str = "POLYMARKET_READONLY_NETWORK_ENABLED") -> None:
        self.url = url
        self.enabled_env = enabled_env
        self._socket: Any = None

    async def connect(self) -> None:
        if os.environ.get(self.enabled_env) != "1":
            raise ReadOnlyNetworkDisabled(f"set {self.enabled_env}=1 to enable read-only network")
        try:
            import websockets  # type: ignore
        except ImportError as exc:
            raise RuntimeError("websockets package is required for read-only network mode") from exc
        self._socket = await websockets.connect(self.url, ping_interval=None)

    async def send(self, payload: Any) -> None:
        if self._socket is None:
            raise RuntimeError("websocket is not connected")
        await self._socket.send(json.dumps(payload, sort_keys=True))

    async def send_text(self, payload: str) -> None:
        if self._socket is None:
            raise RuntimeError("websocket is not connected")
        await self._socket.send(str(payload))

    async def recv(self) -> Any:
        if self._socket is None:
            raise RuntimeError("websocket is not connected")
        return json.loads(await self._socket.recv())

    async def ping(self) -> None:
        if self._socket is None:
            raise RuntimeError("websocket is not connected")
        pong = await self._socket.ping()
        await pong

    async def close(self) -> None:
        if self._socket is not None:
            await self._socket.close()
            self._socket = None


class ReadOnlyConnectionSession:
    def __init__(self, connector: WebSocketConnector, heartbeat_interval: float = 10, max_silence: float = 2, max_clock_skew: float = 2) -> None:
        self.connector = connector
        self.heartbeat_interval = heartbeat_interval
        self.max_silence = max_silence
        self.max_clock_skew = max_clock_skew
        self.health = ConnectionHealth()

    @classmethod
    def public(cls, url: str, **kwargs: Any) -> "ReadOnlyConnectionSession":
        return cls(PublicWebSocketConnector(url), **kwargs)

    @property
    def can_process_new_signal(self) -> bool:
        return self.health.state == ConnectionState.CONNECTED and self.health.last_message_at is not None

    async def connect(self) -> None:
        self.health.state = ConnectionState.CONNECTING
        try:
            await self.connector.connect()
        except Exception as exc:
            self.health.state = ConnectionState.DISCONNECTED
            self.health.disconnect_reason = str(exc)
            raise
        now = datetime.now(timezone.utc)
        self.health.state = ConnectionState.CONNECTED
        self.health.last_heartbeat_at = now

    async def reconnect(self) -> None:
        await self.close()
        self.health.reconnects += 1
        await self.connect()

    async def heartbeat(self) -> None:
        await self.connector.ping()
        self.health.last_heartbeat_at = datetime.now(timezone.utc)

    def note_message(self, received_at: datetime, event_time: Optional[datetime] = None) -> None:
        now = datetime.now(timezone.utc)
        if received_at.tzinfo is None:
            received_at = received_at.replace(tzinfo=timezone.utc)
        age = (now - received_at.astimezone(timezone.utc)).total_seconds()
        if event_time is not None:
            if event_time.tzinfo is None:
                event_time = event_time.replace(tzinfo=timezone.utc)
            clock_skew = abs((now - event_time.astimezone(timezone.utc)).total_seconds())
            if clock_skew > self.max_clock_skew:
                self.health.last_message_at = received_at.astimezone(timezone.utc)
                self.health.state = ConnectionState.STALE
                self.health.disconnect_reason = "event clock skew exceeds gate"
                return
        self.health.last_message_at = received_at.astimezone(timezone.utc)
        self.health.state = ConnectionState.STALE if age > self.max_silence else ConnectionState.CONNECTED

    def check_freshness(self, now: Optional[datetime] = None) -> bool:
        if self.health.last_message_at is None:
            self.health.state = ConnectionState.STALE
            return False
        current = now or datetime.now(timezone.utc)
        age = (current - self.health.last_message_at).total_seconds()
        if age > self.max_silence:
            self.health.state = ConnectionState.STALE
            return False
        return self.health.state == ConnectionState.CONNECTED

    async def close(self) -> None:
        await self.connector.close()
        self.health.state = ConnectionState.STOPPED
