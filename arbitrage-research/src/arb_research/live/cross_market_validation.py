from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from decimal import Decimal
import json
from pathlib import Path
import threading
import time
from typing import Any

import requests
import websockets

from ..cross_market.collectors import (
    BinanceCollector,
    BinanceFuturesCollector,
    DeribitCollector,
    HyperliquidCollector,
    PolymarketReferenceCollector,
)
from ..cross_market.state import CrossMarketState
from ..market_clock import market_clock
from ..experiments.orderbook_depth import BookLevel, consume_asks, consume_bids
from .strategy_monitor import (
    GAMMA_URL,
    WSS_URL,
    LiveMarket,
    PartitionedNDJSONStore,
    decimal_string,
    now_us,
    parse_gamma_market,
    settlement_retry_delay_seconds,
    winner_from_gamma,
)


@dataclass(frozen=True)
class CrossMarketValidationConfig:
    data_dir: Path
    market_duration_minutes: int = 5
    observation_remaining_seconds: tuple[int, ...] = (240, 180, 120, 100, 90, 60, 30)
    observation_tolerance_seconds: float = 2.5
    venue_snapshot_interval_ms: int = 1_000
    memory_window_seconds: int = 600
    discovery_interval_seconds: float = 2.0
    settlement_interval_seconds: float = 5.0
    store_raw_messages: bool = False
    raw_sample_interval_ms: int = 1_000
    lead_lag_interval_ms: int = 100
    option_strikes_each_side: int = 12
    polymarket_book_enabled: bool = False
    polymarket_snapshot_interval_ms: int = 1_000
    polymarket_quantity: float = 10.0
    polymarket_depth_caps: tuple[float, ...] = (0.70, 0.75, 0.80, 0.85, 0.90)
    proxy: str | None = None

    def to_public_dict(self) -> dict[str, Any]:
        raw = asdict(self)
        raw["data_dir"] = str(self.data_dir)
        return raw


def market_slug_for_start(start_seconds: int, duration_minutes: int) -> str:
    """Build the public BTC UP/DOWN slug for a configured market duration."""
    return f"btc-updown-{int(duration_minutes)}m-{int(start_seconds)}"


class CrossMarketValidationService:
    """Collect external predictors and label them with official Polymarket outcomes."""

    def __init__(self, config: CrossMarketValidationConfig) -> None:
        self.config = config
        self.store = PartitionedNDJSONStore(
            config.data_dir,
            compressed_streams=(
                "raw", "venue_snapshots", "lead_lag",
                "polymarket_snapshots", "polymarket_trades",
            ),
        )
        self.state = CrossMarketState(config.memory_window_seconds)
        self.session = requests.Session()
        self.proxies = {"http": config.proxy, "https": config.proxy} if config.proxy else None
        self.stop_event = asyncio.Event()
        self.started_us = now_us()
        self.active_market: LiveMarket | None = None
        self.known_markets: dict[str, LiveMarket] = {}
        self.recorded_market_definitions: set[str] = set()
        self.recorded_observations: set[tuple[str, int]] = set()
        self.missed_observations: set[tuple[str, int]] = set()
        self.settled_markets: set[str] = set()
        self._settlement_retry_at_us: dict[str, int] = {}
        self._settlement_attempts: dict[str, int] = {}
        self.statuses: dict[str, dict[str, Any]] = {
            venue: {"status": "starting", "lastError": None, "updatedAtUs": None}
            for venue in (
                "BINANCE", "BINANCE_FUTURES", "HYPERLIQUID", "DERIBIT",
                "POLYMARKET_RTDS", "POLYMARKET", "POLYMARKET_CLOB"
            )
        }
        self.last_feature: dict[str, Any] | None = None
        self.last_venue_snapshot_us = 0
        self.last_polymarket_snapshot_us = 0
        self.last_polymarket_snapshot: dict[str, Any] | None = None
        self.polymarket_books: dict[str, dict[str, Decimal]] = {}
        self.polymarket_book_updated_us: dict[str, int] = {}
        self.polymarket_last_trade: dict[str, dict[str, Any]] = {}
        self.polymarket_session_extrema: dict[str, dict[str, Decimal]] = {}
        self.polymarket_ws_market_id: str | None = None
        self._last_raw_us: dict[tuple[str, str], int] = {}
        self._last_lead_lag_us: dict[tuple[str, str], int] = {}
        self.counters = {
            "updates": {
                "BINANCE": 0, "BINANCE_FUTURES": 0, "HYPERLIQUID": 0, "DERIBIT": 0,
                "POLYMARKET_RTDS": 0,
            },
            "rawMessagesWritten": 0,
            "leadLagEventsWritten": 0,
            "venueSnapshotsWritten": 0,
            "featureSnapshotsWritten": 0,
            "labelsWritten": 0,
            "missedObservations": 0,
            "polymarketSnapshotsWritten": 0,
            "polymarketTradesWritten": 0,
            "polymarketMessagesReceived": 0,
        }
        self._lock = threading.RLock()
        self._restore()
        self.store.append("sessions", {
            "schemaVersion": 1,
            "eventType": "CROSS_MARKET_VALIDATION_SESSION_STARTED",
            "receivedTimeUs": str(self.started_us),
            "payload": {
                "config": config.to_public_dict(),
                "paperOnly": True,
                "placesOrders": False,
                "boundedMemory": True,
                "defaultStoragePolicy": "one_aggregate_snapshot_per_second",
            },
        }, self.started_us)

    def _restore(self) -> None:
        for path in sorted((self.config.data_dir / "markets").glob("date=*/hour=*/part-*.ndjson")):
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    raw = json.loads(line)
                    payload = raw.get("payload") or {}
                    try:
                        market = LiveMarket(
                            slug=str(raw["marketId"]),
                            condition_id=str(payload.get("conditionId", "")),
                            start_us=int(payload["eventStartUs"]),
                            end_us=int(payload["eventEndUs"]),
                            token_by_outcome=dict(payload.get("tokenByOutcome") or {}),
                            title=str(payload.get("title", raw["marketId"])),
                        )
                    except (KeyError, TypeError, ValueError):
                        continue
                    self.known_markets[market.market_id] = market
                    self.recorded_market_definitions.add(market.market_id)
        for path in sorted((self.config.data_dir / "features").glob("date=*/hour=*/part-*.ndjson")):
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    if line.strip():
                        raw = json.loads(line)
                        self.recorded_observations.add(
                            (str(raw.get("marketId")), int(raw.get("observationTargetSeconds", -1)))
                        )
        for path in sorted((self.config.data_dir / "labels").glob("date=*/hour=*/part-*.ndjson")):
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    if line.strip():
                        self.settled_markets.add(str(json.loads(line).get("marketId")))

    def request_stop(self) -> None:
        self.stop_event.set()

    def close(self) -> None:
        self.session.close()
        self.store.close()

    async def _on_status(self, venue: str, status: str, error: str | None) -> None:
        with self._lock:
            self.statuses[venue] = {
                "status": status, "lastError": error, "updatedAtUs": str(now_us())
            }
        if error:
            self.store.append("health", {
                "schemaVersion": 1, "eventType": "SOURCE_HEALTH",
                "receivedTimeUs": str(now_us()),
                "payload": {"venue": venue, "status": status, "error": error},
            })

    async def _on_update(
        self, venue: str, update: dict[str, Any], raw: Any, received_us: int
    ) -> None:
        if venue == "BINANCE":
            self.state.update_binance(update)
        elif venue == "BINANCE_FUTURES":
            self.state.update_binance_futures(update)
        elif venue == "HYPERLIQUID":
            self.state.update_hyperliquid(update)
        elif venue == "DERIBIT":
            if update.get("kind") == "instruments":
                self.state.set_deribit_instruments(update.get("instruments") or [])
            else:
                self.state.update_deribit(update)
        elif venue == "POLYMARKET_RTDS":
            self.state.update_reference(update)
        with self._lock:
            self.counters["updates"][venue] += 1
        self._record_lead_lag(venue, update, received_us)
        if not self.config.store_raw_messages:
            return
        sample_key = (venue, str(update.get("kind", "unknown")))
        interval_us = self.config.raw_sample_interval_ms * 1_000
        if received_us - self._last_raw_us.get(sample_key, 0) < interval_us:
            return
        self._last_raw_us[sample_key] = received_us
        self.store.append("raw", {
            "schemaVersion": 1, "venue": venue, "receivedTimeUs": str(received_us),
            "normalized": update, "message": raw,
        }, received_us)
        with self._lock:
            self.counters["rawMessagesWritten"] += 1

    def _record_lead_lag(
        self, venue: str, update: dict[str, Any], received_us: int
    ) -> None:
        kind = str(update.get("kind", "unknown"))
        source = str(update.get("source") or venue)
        instrument = str(update.get("instrument") or "")
        key = (source, f"{kind}:{instrument}" if instrument else kind)
        interval_us = self.config.lead_lag_interval_ms * 1_000
        if kind == "option":
            # Per-strike IV/OI/Delta only need one-second resolution for this study.
            interval_us = max(interval_us, 1_000_000)
        if received_us - self._last_lead_lag_us.get(key, 0) < interval_us:
            return
        self._last_lead_lag_us[key] = received_us
        useful = {
            name: update.get(name)
            for name in (
                "price", "bid", "ask", "bidSize", "askSize", "markPrice",
                "oraclePrice", "midPrice", "lastPrice", "indexPrice",
                "quantity", "side", "isBuyerMaker", "symbol",
                "instrument", "markIv", "bidIv", "askIv", "openInterest",
                "delta", "value",
            )
            if update.get(name) is not None
        }
        if kind == "option_trades":
            # This is already Deribit's compact 100ms aggregate, not a raw WS frame.
            useful["trades"] = update.get("trades") or []
        market = self.active_market
        self.store.append("lead_lag", {
            "schemaVersion": 1,
            "eventType": "LEAD_LAG_SOURCE_UPDATE",
            "venue": venue,
            "source": source,
            "kind": kind,
            "marketId": market.market_id if market else None,
            "sourceTimeUs": str(update.get("sourceTimeUs") or received_us),
            "receivedTimeUs": str(received_us),
            "payload": useful,
        }, received_us)
        self.counters["leadLagEventsWritten"] += 1

    def _get_json(self, url: str) -> Any:
        response = self.session.get(url, proxies=self.proxies, timeout=15)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    async def _fetch_market(self, slug: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get_json, f"{GAMMA_URL}/markets/slug/{slug}")

    async def _discover_current_market(self) -> LiveMarket | None:
        current_seconds = now_us() // 1_000_000
        duration_seconds = self.config.market_duration_minutes * 60
        base = current_seconds - current_seconds % duration_seconds
        for start_seconds in (base, base - duration_seconds, base + duration_seconds):
            raw = await self._fetch_market(
                market_slug_for_start(start_seconds, self.config.market_duration_minutes)
            )
            if not raw:
                continue
            market = parse_gamma_market(raw)
            if market.start_us <= current_seconds * 1_000_000 < market.end_us:
                return market
        return None

    def _record_market(self, market: LiveMarket) -> None:
        self.known_markets[market.market_id] = market
        if market.market_id in self.recorded_market_definitions:
            return
        received_us = now_us()
        self.store.append("markets", {
            "schemaVersion": 1, "eventType": "MARKET_DEFINITION", "venue": "POLYMARKET",
            "marketId": market.market_id, "sourceTimeUs": str(market.start_us),
            "receivedTimeUs": str(received_us), "payload": {
                "eventStartUs": str(market.start_us), "eventEndUs": str(market.end_us),
                "conditionId": market.condition_id, "tokenByOutcome": market.token_by_outcome,
                "title": market.title, "labelSource": "official_gamma_settlement",
            },
        }, received_us)
        self.recorded_market_definitions.add(market.market_id)

    async def _market_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                market = await self._discover_current_market()
                if market:
                    self.active_market = market
                    self._record_market(market)
                    await self._on_status("POLYMARKET", "live", None)
                else:
                    await self._on_status("POLYMARKET", "waiting_for_market", None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                await self._on_status("POLYMARKET", "error", f"{type(exc).__name__}: {exc}")
            await asyncio.sleep(self.config.discovery_interval_seconds)

    def _record_observations(self, current_us: int) -> None:
        market = self.active_market
        if market is None or not (market.start_us <= current_us <= market.end_us):
            return
        remaining = (market.end_us - current_us) / 1_000_000
        for target in sorted(self.config.observation_remaining_seconds, reverse=True):
            key = (market.market_id, target)
            if key in self.recorded_observations or key in self.missed_observations:
                continue
            difference = target - remaining
            if abs(difference) <= self.config.observation_tolerance_seconds:
                row = self.state.snapshot(
                    market_id=market.market_id, start_us=market.start_us,
                    end_us=market.end_us, as_of_us=current_us,
                )
                row["observationTargetSeconds"] = target
                row["observationTimingErrorMs"] = difference * 1_000
                row["receivedTimeUs"] = str(now_us())
                self.store.append("features", row, current_us)
                self.recorded_observations.add(key)
                self.last_feature = row
                self.counters["featureSnapshotsWritten"] += 1
            elif remaining < target - self.config.observation_tolerance_seconds:
                self.missed_observations.add(key)
                self.counters["missedObservations"] += 1

    def _record_venue_snapshot(self, current_us: int) -> None:
        if current_us - self.last_venue_snapshot_us < self.config.venue_snapshot_interval_ms * 1_000:
            return
        market = self.active_market
        duration_seconds = self.config.market_duration_minutes * 60
        start_seconds = current_us // 1_000_000
        start_seconds -= start_seconds % duration_seconds
        market_id = market.market_id if market else market_slug_for_start(
            start_seconds, self.config.market_duration_minutes
        )
        start_us = market.start_us if market else start_seconds * 1_000_000
        end_us = market.end_us if market else start_us + duration_seconds * 1_000_000
        row = self.state.snapshot(
            market_id=market_id, start_us=start_us, end_us=end_us, as_of_us=current_us
        )
        row["eventType"] = "CROSS_MARKET_VENUE_SNAPSHOT"
        row["receivedTimeUs"] = str(now_us())
        self.store.append("venue_snapshots", row, current_us)
        self.last_venue_snapshot_us = current_us
        self.counters["venueSnapshotsWritten"] += 1

    @staticmethod
    def _message_time_us(message: dict[str, Any], fallback_us: int) -> int:
        try:
            value = int(message.get("timestamp", fallback_us // 1_000))
        except (TypeError, ValueError):
            return fallback_us
        # Official market WebSocket timestamps are milliseconds. Be tolerant
        # of a future microsecond payload without multiplying it again.
        return value if value >= 10**15 else value * 1_000

    def _polymarket_levels(self, token: str, side: str) -> list[BookLevel]:
        prefix = f"{side}:"
        return sorted(
            (
                BookLevel(
                    price=Decimal(key.split(":", 1)[1]),
                    size=size,
                )
                for key, size in self.polymarket_books.get(token, {}).items()
                if key.startswith(prefix) and size > 0
            ),
            key=lambda level: level.price,
            reverse=side == "BID",
        )

    @staticmethod
    def _execution_payload(
        result: tuple[Decimal, Decimal, Decimal] | None,
    ) -> dict[str, str] | None:
        if result is None:
            return None
        vwap, _fee_per_share, worst_price = result
        return {
            "vwap": decimal_string(vwap),
            "worstPrice": decimal_string(worst_price),
        }

    def _record_polymarket_trade(
        self,
        market: LiveMarket,
        token_to_outcome: dict[str, str],
        message: dict[str, Any],
        received_us: int,
    ) -> None:
        token = str(message.get("asset_id", ""))
        outcome = token_to_outcome.get(token)
        if outcome not in {"UP", "DOWN"}:
            return
        source_us = self._message_time_us(message, received_us)
        payload = {
            "outcome": outcome,
            "side": str(message.get("side", "")).upper(),
            "price": str(message.get("price", "")),
            "size": str(message.get("size", "")),
            "feeRateBps": str(message.get("fee_rate_bps", "")),
            "transactionHash": str(message.get("transaction_hash", "")),
            "messageType": "last_trade_price",
        }
        row = {
            "schemaVersion": 1,
            "eventType": "POLYMARKET_TRADE",
            "venue": "POLYMARKET",
            "marketId": market.market_id,
            "instrumentId": token,
            "outcome": outcome,
            "sourceTimeUs": str(source_us),
            "receivedTimeUs": str(received_us),
            "payload": payload,
        }
        self.store.append("polymarket_trades", row, received_us)
        self.polymarket_last_trade[outcome] = {
            "sourceTimeUs": str(source_us), **payload,
        }
        self.counters["polymarketTradesWritten"] += 1

    def _process_polymarket_ws_event(
        self,
        market: LiveMarket,
        token_to_outcome: dict[str, str],
        message: dict[str, Any],
        received_us: int,
    ) -> None:
        with self._lock:
            event_type = str(message.get("event_type", ""))
            if event_type == "book":
                token = str(message.get("asset_id", ""))
                if token not in token_to_outcome:
                    return
                book = {
                    f"BID:{level['price']}": Decimal(str(level["size"]))
                    for level in message.get("bids", [])
                    if Decimal(str(level["size"])) > 0
                }
                book.update({
                    f"ASK:{level['price']}": Decimal(str(level["size"]))
                    for level in message.get("asks", [])
                    if Decimal(str(level["size"])) > 0
                })
                self.polymarket_books[token] = book
                self.polymarket_book_updated_us[token] = received_us
            elif event_type == "price_change":
                for change in message.get("price_changes", []):
                    token = str(change.get("asset_id", ""))
                    if token not in token_to_outcome:
                        continue
                    side = "BID" if str(change.get("side", "")).upper() == "BUY" else "ASK"
                    try:
                        size = Decimal(str(change["size"]))
                        key = f"{side}:{change['price']}"
                    except (KeyError, ValueError):
                        continue
                    book = self.polymarket_books.setdefault(token, {})
                    if size > 0:
                        book[key] = size
                    else:
                        book.pop(key, None)
                    self.polymarket_book_updated_us[token] = received_us
            elif event_type == "last_trade_price":
                self._record_polymarket_trade(
                    market, token_to_outcome, message, received_us
                )

    def _build_polymarket_snapshot(
        self, market: LiveMarket, current_us: int
    ) -> dict[str, Any] | None:
        quantity = Decimal(str(self.config.polymarket_quantity))
        caps = tuple(Decimal(str(value)) for value in self.config.polymarket_depth_caps)
        outcomes: dict[str, Any] = {}
        with self._lock:
            for outcome, token in market.token_by_outcome.items():
                bids = self._polymarket_levels(token, "BID")
                asks = self._polymarket_levels(token, "ASK")
                if not bids and not asks:
                    continue
                best_bid = bids[0] if bids else None
                best_ask = asks[0] if asks else None
                extrema = self.polymarket_session_extrema.setdefault(outcome, {})
                if best_ask is not None:
                    extrema["bestAskLow"] = min(
                        extrema.get("bestAskLow", best_ask.price), best_ask.price
                    )
                    extrema["bestAskHigh"] = max(
                        extrema.get("bestAskHigh", best_ask.price), best_ask.price
                    )
                cap_summaries: dict[str, Any] = {}
                for cap in caps:
                    cap_key = format(cap, ".2f")
                    eligible = [level for level in asks if level.price <= cap]
                    depth = sum((level.size for level in eligible), Decimal("0"))
                    notional = sum(
                        (level.price * level.size for level in eligible), Decimal("0")
                    )
                    fill = consume_asks(
                        asks, quantity, cap, fee_model="none", fee_rate=Decimal("0")
                    )
                    cap_summaries[cap_key] = {
                        "askDepth": decimal_string(depth),
                        "askNotional": decimal_string(notional),
                        "buy": self._execution_payload(fill),
                    }
                buy = consume_asks(
                    asks, quantity, Decimal("1"),
                    fee_model="none", fee_rate=Decimal("0"),
                )
                sell = consume_bids(
                    bids, quantity, Decimal("0"),
                    fee_model="none", fee_rate=Decimal("0"),
                )
                updated_us = self.polymarket_book_updated_us.get(token)
                outcomes[outcome] = {
                    "tokenId": token,
                    "bestBid": decimal_string(best_bid.price) if best_bid else None,
                    "bestBidSize": decimal_string(best_bid.size) if best_bid else None,
                    "bestAsk": decimal_string(best_ask.price) if best_ask else None,
                    "bestAskSize": decimal_string(best_ask.size) if best_ask else None,
                    "bookAgeMs": (
                        (current_us - updated_us) / 1_000 if updated_us else None
                    ),
                    "buyQuantity": decimal_string(quantity),
                    "buy": self._execution_payload(buy),
                    "sell": self._execution_payload(sell),
                    "depthCaps": cap_summaries,
                    "sessionBestAskLow": (
                        decimal_string(extrema["bestAskLow"])
                        if "bestAskLow" in extrema else None
                    ),
                    "sessionBestAskHigh": (
                        decimal_string(extrema["bestAskHigh"])
                        if "bestAskHigh" in extrema else None
                    ),
                    "lastTrade": self.polymarket_last_trade.get(outcome),
                }
        if not outcomes:
            return None
        return {
            "schemaVersion": 1,
            "eventType": "POLYMARKET_COMPACT_BOOK_SNAPSHOT",
            "venue": "POLYMARKET",
            "marketId": market.market_id,
            "marketStartUs": str(market.start_us),
            "marketEndUs": str(market.end_us),
            "sourceTimeUs": str(current_us),
            "receivedTimeUs": str(now_us()),
            "remainingSeconds": (market.end_us - current_us) / 1_000_000,
            "completeOutcomeBooks": set(outcomes) == {"UP", "DOWN"},
            "outcomes": outcomes,
        }

    def _record_polymarket_snapshot(self, current_us: int) -> None:
        if not self.config.polymarket_book_enabled:
            return
        interval_us = self.config.polymarket_snapshot_interval_ms * 1_000
        if current_us - self.last_polymarket_snapshot_us < interval_us:
            return
        market = self.active_market
        if (
            market is None
            or self.polymarket_ws_market_id != market.market_id
            or not (market.start_us <= current_us <= market.end_us)
        ):
            return
        row = self._build_polymarket_snapshot(market, current_us)
        if row is None:
            return
        self.store.append("polymarket_snapshots", row, current_us)
        self.last_polymarket_snapshot_us = current_us
        self.last_polymarket_snapshot = row
        self.counters["polymarketSnapshotsWritten"] += 1

    async def _monitor_polymarket_market_ws(self, market: LiveMarket) -> None:
        tokens = list(market.token_by_outcome.values())
        token_to_outcome = {
            token: outcome for outcome, token in market.token_by_outcome.items()
        }
        with self._lock:
            self.polymarket_books = {}
            self.polymarket_book_updated_us = {}
            self.polymarket_last_trade = {}
            self.polymarket_session_extrema = {}
            self.polymarket_ws_market_id = market.market_id
            self.last_polymarket_snapshot_us = 0
        proxy: str | bool | None = self.config.proxy if self.config.proxy else True
        async with websockets.connect(
            WSS_URL,
            proxy=proxy,
            open_timeout=15,
            ping_interval=20,
            ping_timeout=20,
            max_queue=1024,
            max_size=4 * 1024 * 1024,
        ) as websocket:
            await websocket.send(json.dumps({"assets_ids": tokens, "type": "market"}))
            await self._on_status("POLYMARKET_CLOB", "live", None)
            last_ping = time.monotonic()
            while (
                not self.stop_event.is_set()
                and now_us() < market.end_us + 2_000_000
                and self.active_market is not None
                and self.active_market.market_id == market.market_id
            ):
                if time.monotonic() - last_ping >= 10:
                    await websocket.send("PING")
                    last_ping = time.monotonic()
                try:
                    raw = await asyncio.wait_for(websocket.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                received_us = now_us()
                if raw == "PONG":
                    continue
                parsed = json.loads(raw)
                self.counters["polymarketMessagesReceived"] += 1
                values = parsed if isinstance(parsed, list) else [parsed]
                for value in values:
                    if isinstance(value, dict):
                        self._process_polymarket_ws_event(
                            market, token_to_outcome, value, received_us
                        )

    async def _polymarket_book_loop(self) -> None:
        retry_seconds = 1.0
        while not self.stop_event.is_set():
            market = self.active_market
            if market is None or now_us() >= market.end_us + 2_000_000:
                await asyncio.sleep(0.25)
                continue
            try:
                await self._monitor_polymarket_market_ws(market)
                retry_seconds = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                await self._on_status(
                    "POLYMARKET_CLOB", "reconnecting",
                    f"{type(exc).__name__}: {exc}",
                )
                try:
                    await asyncio.wait_for(
                        self.stop_event.wait(), timeout=retry_seconds
                    )
                except asyncio.TimeoutError:
                    pass
                retry_seconds = min(retry_seconds * 2, 30.0)

    async def _snapshot_loop(self) -> None:
        while not self.stop_event.is_set():
            current_us = now_us()
            self._record_venue_snapshot(current_us)
            self._record_polymarket_snapshot(current_us)
            self._record_observations(current_us)
            await asyncio.sleep(0.1)

    async def _settlement_loop(self) -> None:
        while not self.stop_event.is_set():
            current_us = now_us()
            for market in list(self.known_markets.values()):
                if market.market_id in self.settled_markets or current_us < market.end_us:
                    continue
                if current_us < self._settlement_retry_at_us.get(market.market_id, 0):
                    continue
                try:
                    raw = await self._fetch_market(market.slug)
                    winner = winner_from_gamma(raw) if raw else None
                    if winner:
                        self.store.append("labels", {
                            "schemaVersion": 1, "eventType": "MARKET_LABEL",
                            "venue": "POLYMARKET", "marketId": market.market_id,
                            "sourceTimeUs": str(market.end_us), "receivedTimeUs": str(now_us()),
                            "payload": {"winnerOutcome": winner, "labelUp": 1 if winner == "UP" else 0,
                                        "source": "official_gamma_settlement"},
                        }, now_us())
                        self.settled_markets.add(market.market_id)
                        self.counters["labelsWritten"] += 1
                        self._settlement_retry_at_us.pop(market.market_id, None)
                        self._settlement_attempts.pop(market.market_id, None)
                    else:
                        self._schedule_settlement_retry(market.market_id, current_us)
                except Exception as exc:  # noqa: BLE001
                    await self._on_status(
                        "POLYMARKET", "settlement_error", f"{market.slug}: {type(exc).__name__}: {exc}"
                    )
                    self._schedule_settlement_retry(market.market_id, current_us)
            await asyncio.sleep(self.config.settlement_interval_seconds)

    def _schedule_settlement_retry(self, market_id: str, current_us: int) -> None:
        attempts = self._settlement_attempts.get(market_id, 0)
        self._settlement_attempts[market_id] = attempts + 1
        delay = settlement_retry_delay_seconds(self.config.settlement_interval_seconds, attempts)
        self._settlement_retry_at_us[market_id] = current_us + int(delay * 1_000_000)

    async def _state_writer_loop(self) -> None:
        while not self.stop_event.is_set():
            self._write_state()
            await asyncio.sleep(1)

    def _write_state(self) -> None:
        state_dir = self.config.data_dir / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        target = state_dir / "latest.json"
        temporary = state_dir / "latest.json.tmp"
        temporary.write_text(json.dumps(self.get_state(), ensure_ascii=False, indent=2) + "\n")
        temporary.replace(target)

    async def _duration_timer(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
        self.request_stop()

    async def run(self, duration_seconds: float | None = None) -> None:
        async with market_clock.client(self.config.proxy) as clock_client:
            await self._run_with_clock(clock_client, duration_seconds)

    async def _run_with_clock(self, clock_client, duration_seconds: float | None) -> None:
        await market_clock.synchronize(client=clock_client)
        collectors = (
            BinanceCollector(self._on_update, self._on_status, proxy=self.config.proxy),
            BinanceFuturesCollector(self._on_update, self._on_status, proxy=self.config.proxy),
            HyperliquidCollector(self._on_update, self._on_status, proxy=self.config.proxy),
            DeribitCollector(
                self._on_update, self._on_status, proxy=self.config.proxy,
                option_strikes_each_side=self.config.option_strikes_each_side,
            ),
            PolymarketReferenceCollector(
                self._on_update, self._on_status, proxy=self.config.proxy
            ),
        )
        tasks = [asyncio.create_task(item.run(self.stop_event)) for item in collectors]
        tasks.extend([
            asyncio.create_task(market_clock.maintain(client=clock_client)),
            asyncio.create_task(self._market_loop()), asyncio.create_task(self._snapshot_loop()),
            asyncio.create_task(self._settlement_loop()), asyncio.create_task(self._state_writer_loop()),
        ])
        if self.config.polymarket_book_enabled:
            tasks.append(asyncio.create_task(self._polymarket_book_loop()))
        if duration_seconds:
            tasks.append(asyncio.create_task(self._duration_timer(duration_seconds)))
        try:
            await self.stop_event.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._write_state()
            self.close()

    def get_state(self) -> dict[str, Any]:
        current_us = now_us()
        market = self.active_market
        current_feature = None
        if market is not None:
            current_feature = self.state.snapshot(
                market_id=market.market_id,
                start_us=market.start_us,
                end_us=market.end_us,
                as_of_us=current_us,
            )
            current_feature["eventType"] = "CROSS_MARKET_CURRENT_FEATURE"
        return {
            "schemaVersion": 1, "generatedAtUs": str(current_us),
            "service": {
                "status": "running" if not self.stop_event.is_set() else "stopped",
                "startedAtUs": str(self.started_us), "dataDir": str(self.config.data_dir.resolve()),
                "paperOnly": True, "placesOrders": False,
                "clock": market_clock.snapshot(),
            },
            "sources": dict(self.statuses), "config": self.config.to_public_dict(),
            "market": {
                "marketId": market.market_id, "startUs": str(market.start_us),
                "endUs": str(market.end_us),
                "remainingSeconds": (market.end_us - current_us) / 1_000_000,
            } if market else None,
            "counters": json.loads(json.dumps(self.counters)),
            "memory": self.state.health(current_us),
            "currentFeature": current_feature,
            "lastFeature": self.last_feature,
            "latestPolymarketSnapshot": self.last_polymarket_snapshot,
        }
