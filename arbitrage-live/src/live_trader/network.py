from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Mapping, Optional
from urllib.parse import quote
from urllib.request import Request, ProxyHandler, build_opener, urlopen

from .readonly import ReadOnlyNetworkDisabled


GAMMA_URL = "https://gamma-api.polymarket.com"
POLYMARKET_WSS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
BINANCE_WSS_URL = "wss://stream.binance.com:9443/ws/btcusdt@kline_1m"


def market_slugs_for_now(now: datetime) -> list[str]:
    now_utc = now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
    epoch = int(now_utc.timestamp())
    base = epoch - epoch % 300
    return [f"btc-updown-5m-{value}" for value in (base, base - 300, base + 300)]


def gamma_to_market_message(raw: Mapping[str, Any]) -> dict[str, Any]:
    outcomes = raw.get("outcomes", [])
    tokens = raw.get("clobTokenIds", [])
    if isinstance(outcomes, str):
        outcomes = json.loads(outcomes)
    if isinstance(tokens, str):
        tokens = json.loads(tokens)
    if not isinstance(outcomes, list) or not isinstance(tokens, list) or len(outcomes) != len(tokens):
        raise ValueError("Gamma market does not contain paired outcomes and CLOB tokens")
    return {
        "type": "market_definition",
        "id": str(raw.get("id", raw.get("slug", ""))),
        "slug": str(raw.get("slug", raw.get("id", ""))),
        "conditionId": str(raw.get("conditionId", raw.get("condition_id", ""))),
        "question": str(raw.get("question", raw.get("title", ""))),
        "active": bool(raw.get("active", True)),
        "closed": bool(raw.get("closed", False)),
        "startDate": raw.get("startDate", raw.get("start_date")),
        "endDate": raw.get("endDate", raw.get("end_date")),
        "tokens": [
            {"outcome": str(outcome), "token_id": str(token)}
            for outcome, token in zip(outcomes, tokens)
        ],
    }


def gamma_winner_outcome(raw: Mapping[str, Any]) -> Optional[str]:
    """Return a winner only when Gamma exposes an explicit resolved result.

    A market price merely approaching 1.0 is not a settlement. We therefore
    require a closed/resolved marker and a one-hot outcome price (or an
    explicit winner field) before sending a settlement event to the runner.
    """
    for key in ("winnerOutcome", "winningOutcome", "winner", "resolvedOutcome"):
        value = str(raw.get(key, "")).strip().upper()
        if value in {"UP", "DOWN"}:
            return value
    resolved = bool(raw.get("closed") or raw.get("resolved")) or str(raw.get("umaResolutionStatus", "")).lower() in {"resolved", "confirmed"}
    if not resolved:
        return None
    outcomes = raw.get("outcomes", [])
    prices = raw.get("outcomePrices", raw.get("outcome_prices", []))
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except json.JSONDecodeError:
            return None
    if isinstance(prices, str):
        try:
            prices = json.loads(prices)
        except json.JSONDecodeError:
            return None
    if not isinstance(outcomes, list) or not isinstance(prices, list) or len(outcomes) != len(prices):
        return None
    try:
        values = [float(value) for value in prices]
    except (TypeError, ValueError):
        return None
    winners = [str(outcome).strip().upper() for outcome, price in zip(outcomes, values) if price >= 0.99]
    if len(winners) != 1 or winners[0] not in {"UP", "DOWN"}:
        return None
    return winners[0]


def _fetch_json(url: str, timeout: float = 10.0, proxy: Optional[str] = None) -> Any:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "arbitrage-live/0.1"})
    opener = build_opener(ProxyHandler({"http": proxy, "https": proxy})) if proxy else None
    response_context = opener.open(request, timeout=timeout) if opener else urlopen(request, timeout=timeout)  # noqa: S310 - URL is fixed/configured by operator.
    with response_context as response:
        return json.loads(response.read().decode("utf-8"))


class LiveNetworkFeed:
    """Read-only market feed feeding normalized events into ``LiveRunner``.

    It has no order methods. The order transport lives behind the runner's
    separate rollout gates, so enabling market WebSockets never enables orders.
    """

    def __init__(
        self,
        runner: Any,
        *,
        gamma_url: str = GAMMA_URL,
        proxy: Optional[str] = None,
        settlement_poll_seconds: float = 5.0,
    ) -> None:
        self.runner = runner
        self.gamma_url = gamma_url.rstrip("/")
        self.proxy = proxy
        self.settlement_poll_seconds = settlement_poll_seconds
        self.stop_event = asyncio.Event()
        self._settlement_tasks: dict[str, asyncio.Task[Any]] = {}

    def request_stop(self) -> None:
        self.stop_event.set()

    def _record_connection_health(self, event_type: str, venue: str) -> None:
        recorder = getattr(self.runner, "recorder", None)
        record = getattr(recorder, "record", None)
        if callable(record):
            record("health", event_type, venue=venue, payload={"status": "connected"})

    async def run(self) -> None:
        self._require_network("POLYMARKET_READONLY_NETWORK_ENABLED")
        self._require_network("BINANCE_READONLY_NETWORK_ENABLED")
        tasks = [
            asyncio.create_task(self._market_loop(), name="polymarket-market-feed"),
            asyncio.create_task(self._binance_loop(), name="binance-spot-feed"),
        ]
        pending_markets = getattr(self.runner, "pending_settlement_market_ids", None)
        if callable(pending_markets):
            for market_id in pending_markets():
                self._schedule_settlement(str(market_id), market_id=str(market_id), by_id=True)
        # RTDS is an optional reference stream. It is independently gated and
        # never replaces the Market WS or enables order submission.
        if os.environ.get("POLYMARKET_RTDS_NETWORK_ENABLED") == "1":
            tasks.append(asyncio.create_task(self._rtds_loop(), name="polymarket-rtds-reference-feed"))
        try:
            await self.stop_event.wait()
        finally:
            for task in tasks:
                task.cancel()
            settlement_tasks = list(self._settlement_tasks.values())
            for task in settlement_tasks:
                task.cancel()
            await asyncio.gather(*tasks, *settlement_tasks, return_exceptions=True)

    @staticmethod
    def _require_network(env_name: str) -> None:
        if os.environ.get(env_name) != "1":
            raise ReadOnlyNetworkDisabled(f"set {env_name}=1 to enable read-only network")

    async def _discover(self) -> Optional[Mapping[str, Any]]:
        for slug in market_slugs_for_now(datetime.now(timezone.utc)):
            url = f"{self.gamma_url}/markets/slug/{quote(slug, safe='')}"
            try:
                raw = await asyncio.to_thread(_fetch_json, url, proxy=self.proxy)
            except Exception:
                continue
            if isinstance(raw, Mapping) and raw.get("active") and not raw.get("closed"):
                return raw
        return None

    async def _market_loop(self) -> None:
        delay = 1.0
        while not self.stop_event.is_set():
            try:
                raw = await self._discover()
                if raw is None:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=2.0)
                    continue
                definition = gamma_to_market_message(raw)
                self.runner.process_event("polymarket", definition)
                await self._market_socket(definition)
                # ``gamma_to_market_message`` returns the versioned wire
                # envelope as a mapping; do not treat it like the discovery
                # dataclass used elsewhere in the codebase.
                lookup_slug = str(definition.get("slug") or definition.get("id") or "")
                market_id = str(definition.get("id") or lookup_slug)
                self._schedule_settlement(lookup_slug, market_id=market_id, by_id=False)
                delay = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect loop must survive provider disconnects.
                self.runner.record_health("POLYMARKET_FEED_ERROR", f"{type(exc).__name__}: {exc}")
                try:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
                delay = min(delay * 2, 30.0)

    async def _rtds_loop(self) -> None:
        from .rtds import RTDSReadOnlyAdapter

        delay = 1.0
        while not self.stop_event.is_set():
            adapter = RTDSReadOnlyAdapter()
            heartbeat_task: asyncio.Task[Any] | None = None
            try:
                await adapter.connect(
                    sources=("binance", "chainlink", "chainlink_twap"),
                    symbols=("btcusdt", "btc/usd"),
                    twap_windows=(30, 60),
                )
                self.runner.recorder.record("health", "CONNECTION_HEALTH", venue="POLYMARKET_RTDS", payload={"state": "connected", "feed": "chainlink/binance"})

                async def heartbeat() -> None:
                    while not self.stop_event.is_set():
                        await asyncio.sleep(5.0)
                        await adapter.heartbeat()

                heartbeat_task = asyncio.create_task(heartbeat(), name="polymarket-rtds-heartbeat")
                async for message in adapter.messages():
                    self.runner.process_event("rtds", message)
                    if self.stop_event.is_set():
                        break
                delay = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - optional feed reconnect loop.
                self.runner.record_health("RTDS_FEED_ERROR", f"{type(exc).__name__}: {exc}")
                try:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
                delay = min(delay * 2, 30.0)
            finally:
                if heartbeat_task is not None:
                    heartbeat_task.cancel()
                    await asyncio.gather(heartbeat_task, return_exceptions=True)
                await adapter.close()

    def _schedule_settlement(self, lookup: str, *, market_id: str, by_id: bool = False) -> None:
        if not lookup or not market_id or market_id in self._settlement_tasks:
            return
        task = asyncio.create_task(
            self._settlement_loop(lookup, market_id=market_id, by_id=by_id),
            name=f"settlement-{market_id}",
        )
        self._settlement_tasks[market_id] = task
        task.add_done_callback(lambda _task, key=market_id: self._settlement_tasks.pop(key, None))

    async def _settlement_loop(self, lookup: str, *, market_id: str, by_id: bool = False) -> None:
        """Poll until Gamma exposes an explicit result; never guess from price."""
        attempts = 0
        while not self.stop_event.is_set():
            attempts += 1
            suffix = quote(lookup, safe="") if by_id else f"slug/{quote(lookup, safe='')}"
            try:
                raw = await asyncio.to_thread(
                    _fetch_json,
                    f"{self.gamma_url}/markets/{suffix}",
                    proxy=self.proxy,
                )
            except Exception as exc:
                if attempts == 1 or attempts % 12 == 0:
                    self.runner.record_health("SETTLEMENT_VERIFY_ERROR", type(exc).__name__)
            else:
                if isinstance(raw, Mapping):
                    winner = gamma_winner_outcome(raw)
                    if winner is not None:
                        self.runner.process_event(
                            "polymarket",
                            {"type": "market_resolved", "market_id": market_id, "winner": winner, "settlement_source": "gamma"},
                        )
                        return
                if attempts == 1 or attempts % 12 == 0:
                    self.runner.record_health("SETTLEMENT_PENDING", f"Gamma 未返回明确结算：{lookup}")
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=max(self.settlement_poll_seconds, 0.001))
            except asyncio.TimeoutError:
                continue

    async def _market_socket(self, definition: Mapping[str, Any]) -> None:
        import websockets  # type: ignore

        tokens = [str(item["token_id"]) for item in definition["tokens"]]
        end_value = definition.get("endDate")
        end_time = datetime.fromisoformat(str(end_value).replace("Z", "+00:00")) if end_value else None
        if end_time is not None and end_time.tzinfo is None:
            end_time = end_time.replace(tzinfo=timezone.utc)
        subscription = {"assets_ids": tokens, "type": "market"}
        proxy: str | bool | None = self.proxy if self.proxy else True
        async with websockets.connect(
            POLYMARKET_WSS_URL,
            proxy=proxy,
            open_timeout=15,
            # The Market channel uses its documented text PING/PONG
            # heartbeat. Running a second protocol-level heartbeat caused
            # avoidable disconnects through some proxies.
            ping_interval=None,
            max_size=4 * 1024 * 1024,
        ) as websocket:
            await websocket.send(json.dumps(subscription))
            self._record_connection_health("POLYMARKET_FEED_CONNECTED", "POLYMARKET")
            last_ping = time.monotonic()
            last_activity = time.monotonic()
            while not self.stop_event.is_set():
                if end_time is not None and datetime.now(timezone.utc) >= end_time:
                    return
                if time.monotonic() - last_ping >= 10:
                    await websocket.send("PING")
                    last_ping = time.monotonic()
                try:
                    raw = await asyncio.wait_for(websocket.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    if time.monotonic() - last_activity > 30:
                        raise TimeoutError("Polymarket Market WS heartbeat stale")
                    continue
                last_activity = time.monotonic()
                if raw in {"PONG", "pong"}:
                    continue
                message = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
                values = message if isinstance(message, list) else [message]
                for value in values:
                    if isinstance(value, Mapping):
                        received = dict(value)
                        received.setdefault("received_time_us", int(time.time() * 1_000_000))
                        self.runner.process_event("polymarket", received)

    async def _binance_loop(self) -> None:
        import websockets  # type: ignore

        delay = 1.0
        while not self.stop_event.is_set():
            try:
                proxy: str | bool | None = self.proxy if self.proxy else True
                async with websockets.connect(BINANCE_WSS_URL, proxy=proxy, ping_interval=20, ping_timeout=20) as websocket:
                    self._record_connection_health("BINANCE_FEED_CONNECTED", "BINANCE")
                    delay = 1.0
                    while not self.stop_event.is_set():
                        try:
                            raw = await asyncio.wait_for(websocket.recv(), timeout=1.0)
                        except asyncio.TimeoutError:
                            continue
                        message = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
                        if isinstance(message, Mapping):
                            self.runner.process_event("binance", message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect loop must survive provider disconnects.
                self.runner.record_health("BINANCE_FEED_ERROR", f"{type(exc).__name__}: {exc}")
                try:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
                delay = min(delay * 2, 30.0)
