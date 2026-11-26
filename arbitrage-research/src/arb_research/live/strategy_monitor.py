from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
import gzip
import json
from pathlib import Path
import threading
import time
from typing import Any
from uuid import uuid4

import requests
import websockets

from ..experiments.orderbook_depth import BookLevel, consume_asks, consume_bids
from ..market_clock import market_clock
from .log_storage import COMPRESSED_PAPER_STREAMS, disk_space_state, partition_lock
from .price_history import PriceHistory


GAMMA_URL = "https://gamma-api.polymarket.com"
WSS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


def settlement_retry_delay_seconds(base_interval: float, attempts: int, max_delay: float = 3600.0) -> float:
    """Return a bounded exponential delay after a failed settlement lookup."""
    return min(max_delay, max(0.1, float(base_interval)) * (2 ** max(0, int(attempts))))


def now_us() -> int:
    return market_clock.now_us()


def iso_to_us(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1_000_000)


def decimal_string(value: Decimal) -> str:
    return format(value, "f")


@dataclass(frozen=True)
class LiveMonitorConfig:
    data_dir: Path
    strategy_id: str = "down_active_vwap80_85_remaining90_30_momentum5_01_v1"
    outcomes: tuple[str, ...] = ("DOWN",)
    price_min: Decimal = Decimal("0.80")
    price_max: Decimal = Decimal("0.85")
    entry_price_min: Decimal = Decimal("0.80")
    max_remaining_seconds: int = 90
    min_remaining_seconds: int = 30
    min_observed_trade_size: Decimal = Decimal("10")
    momentum_window_seconds: int = 5
    min_entry_momentum: Decimal = Decimal("0")
    paper_quantity: Decimal = Decimal("10")
    fee_model: str = "polymarket_crypto_taker"
    fee_rate: Decimal = Decimal("0.07")
    max_book_age_ms: int = 2_000
    derived_snapshot_interval_ms: int = 250
    raw_price_change_interval_ms: int = 100
    raw_book_interval_ms: int = 1_000
    stable_book_ms: int = 0
    cross_market_state_path: Path | None = None
    cross_market_max_age_ms: int = 2_500
    require_cross_market_resonance: bool = False
    early_exit_price: Decimal = Decimal("0.99")
    enable_early_exit: bool = False
    discovery_interval_seconds: float = 2.0
    settlement_interval_seconds: float = 5.0
    proxy: str | None = None
    candidates_enabled: bool = False

    def to_public_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["data_dir"] = str(self.data_dir)
        for key, value in list(result.items()):
            if isinstance(value, Decimal):
                result[key] = decimal_string(value)
            elif isinstance(value, Path):
                result[key] = str(value)
        return result


@dataclass(frozen=True)
class LiveMarket:
    slug: str
    condition_id: str
    start_us: int
    end_us: int
    token_by_outcome: dict[str, str]
    title: str

    @property
    def market_id(self) -> str:
        return self.slug


class PartitionedNDJSONStore:
    def __init__(
        self, base_dir: Path, *, compressed_streams: tuple[str, ...] = ("raw",)
    ) -> None:
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.compressed_streams = frozenset(compressed_streams)
        self._handles: dict[tuple[str, str, str], Any] = {}
        self._partition_locks: dict[tuple[str, str, str], Any] = {}
        self._lock = threading.Lock()

    def append(self, stream: str, row: dict[str, Any], timestamp_us: int | None = None) -> None:
        timestamp_us = timestamp_us or now_us()
        current = datetime.fromtimestamp(timestamp_us / 1_000_000, timezone.utc)
        day = current.strftime("%Y-%m-%d")
        hour = current.strftime("%H")
        key = (stream, day, hour)
        line = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        with self._lock:
            handle = self._handles.get(key)
            if handle is None:
                for old_key in [item for item in self._handles if item[0] == stream and item != key]:
                    self._handles.pop(old_key).close()
                    self._partition_locks.pop(old_key).close()
                compressed = stream in self.compressed_streams
                suffix = "part-001.ndjson.gz" if compressed else "part-001.ndjson"
                path = self.base_dir / stream / f"date={day}" / f"hour={hour}" / suffix
                path.parent.mkdir(parents=True, exist_ok=True)
                if compressed:
                    # Keep pre-upgrade plain files intact for verified archival.
                    parts = [int(item.name.split(".", 1)[0].split("-")[1])
                             for item in path.parent.glob("part-*.ndjson*")
                             if item.name.removesuffix(".gz").endswith(".ndjson")]
                    # Start a fresh gzip member file after restart, even if the
                    # previous process left a truncated stream during a crash.
                    number = max(parts, default=0) + 1
                    path = path.with_name(f"part-{number:03d}.ndjson.gz")
                lock = partition_lock(path)
                try:
                    handle = (
                        gzip.open(path, "at", encoding="utf-8", compresslevel=3)
                        if compressed
                        else path.open("a", encoding="utf-8")
                    )
                except BaseException:
                    lock.close()
                    raise
                self._handles[key] = handle
                self._partition_locks[key] = lock
            handle.write(line)
            handle.flush()

    def close(self) -> None:
        with self._lock:
            for handle in self._handles.values():
                handle.close()
            self._handles.clear()
            for lock in self._partition_locks.values():
                lock.close()
            self._partition_locks.clear()


class PaperLedger:
    def __init__(self, store: PartitionedNDJSONStore, data_dir: Path) -> None:
        self.store = store
        self.data_dir = data_dir
        self.positions: dict[str, dict[str, Any]] = {}
        self.closed: list[dict[str, Any]] = []
        self.recent_events: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        paper_dir = self.data_dir / "paper"
        if not paper_dir.exists():
            return
        paths = sorted(paper_dir.glob("date=*/hour=*/part-*.ndjson"))
        for path in paths:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    self._apply(json.loads(line), loading=True)

    def _apply(self, event: dict[str, Any], *, loading: bool = False) -> None:
        event_type = event.get("eventType")
        market_id = str(event.get("marketId", ""))
        outcome = str(event.get("outcome", "DOWN"))
        position_key = self.position_key(market_id, outcome)
        if event_type == "PAPER_FILL":
            self.positions[position_key] = event
        elif event_type in {"PAPER_SETTLEMENT", "PAPER_EXIT"}:
            position = self.positions.pop(position_key, None)
            if position is not None:
                closed = {**position, **event, "entry": position}
                self.closed.append(closed)
        self.recent_events.append(event)
        self.recent_events = self.recent_events[-100:]
        if not loading:
            self.store.append("paper", event, int(event["receivedTimeUs"]))

    @staticmethod
    def position_key(market_id: str, outcome: str) -> str:
        return f"{market_id}|{outcome}"

    def has_market_position(self, market_id: str) -> bool:
        return any(
            position.get("marketId") == market_id
            for position in self.positions.values()
        )

    def fill(
        self,
        market: LiveMarket,
        *,
        outcome: str,
        signal: dict[str, Any],
        quantity: Decimal,
        vwap: Decimal,
        worst_price: Decimal,
        fee_per_share: Decimal,
        received_us: int,
        depth_at_cap: Decimal,
    ) -> dict[str, Any]:
        event = {
            "schemaVersion": 1,
            "eventId": f"paper-fill-{market.market_id}-{uuid4().hex}",
            "eventType": "PAPER_FILL",
            "venue": "POLYMARKET_PAPER",
            "marketId": market.market_id,
            "outcome": outcome,
            "sourceTimeUs": str(signal["sourceTimeUs"]),
            "receivedTimeUs": str(received_us),
            "payload": {
                "strategyId": signal["payload"]["strategyId"],
                "quantity": decimal_string(quantity),
                "entryVwap": decimal_string(vwap),
                "worstPrice": decimal_string(worst_price),
                "feePerShare": decimal_string(fee_per_share),
                "grossCost": decimal_string(quantity * (vwap + fee_per_share)),
                "depthAtPriceCap": decimal_string(depth_at_cap),
                "signalTradePrice": signal["payload"]["price"],
                "signalTradeSize": signal["payload"]["size"],
                "remainingSeconds": signal["payload"]["remainingSeconds"],
                "timeCohorts": signal["payload"].get("timeCohorts", []),
                "bookStableMs": signal["payload"].get("bookStableMs"),
                "crossMarket": signal["payload"].get("crossMarket"),
                "paperOnly": True,
            },
            "market": {
                "conditionId": market.condition_id,
                "endUs": str(market.end_us),
                "tokenByOutcome": market.token_by_outcome,
            },
        }
        self._apply(event)
        return event

    def exit(
        self,
        position: dict[str, Any],
        *,
        vwap: Decimal,
        worst_price: Decimal,
        fee_per_share: Decimal,
        received_us: int,
    ) -> dict[str, Any]:
        entry = position["payload"]
        quantity = Decimal(str(entry["quantity"]))
        gross_cost = Decimal(str(entry["grossCost"]))
        net_proceeds = quantity * (vwap - fee_per_share)
        net_pnl = net_proceeds - gross_cost
        event = {
            "schemaVersion": 1,
            "eventId": f"paper-exit-{position['marketId']}-{position['outcome']}-{uuid4().hex}",
            "eventType": "PAPER_EXIT",
            "venue": "POLYMARKET_PAPER",
            "marketId": position["marketId"],
            "outcome": position["outcome"],
            "sourceTimeUs": str(received_us),
            "receivedTimeUs": str(received_us),
            "payload": {
                "strategyId": entry.get("strategyId"),
                "exitReason": "executable_bid_at_0.99",
                "quantity": decimal_string(quantity),
                "entryVwap": entry["entryVwap"],
                "entryGrossCost": decimal_string(gross_cost),
                "exitVwap": decimal_string(vwap),
                "exitWorstPrice": decimal_string(worst_price),
                "exitFeePerShare": decimal_string(fee_per_share),
                "netProceeds": decimal_string(net_proceeds),
                "netPnl": decimal_string(net_pnl),
                "won": net_pnl > 0,
                "paperOnly": True,
            },
        }
        self._apply(event)
        return event

    def settle(self, market: LiveMarket, winner: str, received_us: int) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        positions = [
            position
            for position in self.positions.values()
            if position.get("marketId") == market.market_id
        ]
        for position in positions:
            payload = position["payload"]
            quantity = Decimal(str(payload["quantity"]))
            gross_cost = Decimal(str(payload["grossCost"]))
            payout = quantity if winner == position["outcome"] else Decimal("0")
            event = {
                "schemaVersion": 1,
                "eventId": f"paper-settlement-{market.market_id}-{position['outcome']}-{uuid4().hex}",
                "eventType": "PAPER_SETTLEMENT",
                "venue": "POLYMARKET_PAPER",
                "marketId": market.market_id,
                "outcome": position["outcome"],
                "sourceTimeUs": str(market.end_us),
                "receivedTimeUs": str(received_us),
                "payload": {
                    "winnerOutcome": winner,
                    "won": winner == position["outcome"],
                    "quantity": decimal_string(quantity),
                    "payout": decimal_string(payout),
                    "netPnl": decimal_string(payout - gross_cost),
                    "paperOnly": True,
                },
            }
            self._apply(event)
            events.append(event)
        return events

    def stats(
        self,
        outcome: str | None = None,
        strategy_id: str | None = None,
        time_cohort: str | None = None,
    ) -> dict[str, Any]:
        def matches_strategy(item: dict[str, Any]) -> bool:
            if strategy_id is None:
                return True
            entry = item.get("entry") or item
            entry_payload = entry.get("payload") or {}
            if entry_payload.get("strategyId") != strategy_id:
                return False
            return time_cohort is None or time_cohort in entry_payload.get("timeCohorts", [])

        closed = [
            item for item in self.closed
            if (outcome is None or item.get("outcome") == outcome)
            and matches_strategy(item)
        ]
        positions = [
            item for item in self.positions.values()
            if (outcome is None or item.get("outcome") == outcome)
            and matches_strategy(item)
        ]
        wins = sum(bool(item["payload"].get("won")) for item in closed)
        total = len(closed)
        total_pnl = sum(
            (Decimal(str(item["payload"]["netPnl"])) for item in closed),
            Decimal("0"),
        )
        return {
            "closedTrades": total,
            "wins": wins,
            "losses": total - wins,
            "winRate": decimal_string(Decimal(wins) / Decimal(total)) if total else "0",
            "totalPnl": decimal_string(total_pnl),
            "openPositions": len(positions),
        }


class ResearchPaperMonitor:
    def __init__(self, config: LiveMonitorConfig) -> None:
        self.config = config
        self.store = PartitionedNDJSONStore(config.data_dir, compressed_streams=COMPRESSED_PAPER_STREAMS)
        self._disk_state = disk_space_state(config.data_dir)
        self._disk_checked_at = time.monotonic()
        self._disk_reported_level: str | None = None
        self.ledger = PaperLedger(self.store, config.data_dir)
        self.session = requests.Session()
        self.proxies = {"http": config.proxy, "https": config.proxy} if config.proxy else None
        self.stop_event = asyncio.Event()
        self.started_us = now_us()
        self.active_market: LiveMarket | None = None
        self.known_markets: dict[str, LiveMarket] = {}
        self._restore_open_markets()
        self.settled_markets: set[str] = set()
        self._settlement_retry_at_us: dict[str, int] = {}
        self._settlement_attempts: dict[str, int] = {}
        self.books: dict[str, dict[str, Decimal]] = {}
        self.book_updated_us: dict[str, int] = {}
        self.book_executable_since_us: dict[str, int] = {}
        self.price_history: dict[str, PriceHistory] = {}
        self._book_levels_cache: dict[tuple[str, str], list[BookLevel]] = {}
        self.last_snapshot_us = 0
        self.last_raw_price_change_us = 0
        self.last_raw_book_us = 0
        self.recent_signals: list[dict[str, Any]] = []
        self.counters = {
            "receivedMessages": 0,
            "rawMessages": 0,
            "normalizedEvents": 0,
            "signalEvaluations": 0,
            "qualifiedSignals": 0,
            "paperFills": 0,
            "paperNoFills": 0,
            "paperExits": 0,
            "connectionErrors": 0,
            "signalEvaluationsByOutcome": {outcome: 0 for outcome in config.outcomes},
            "qualifiedSignalsByOutcome": {outcome: 0 for outcome in config.outcomes},
            "paperFillsByOutcome": {outcome: 0 for outcome in config.outcomes},
            "paperNoFillsByOutcome": {outcome: 0 for outcome in config.outcomes},
        }
        self.connection_status = "starting"
        self.last_message_us: int | None = None
        self.last_error: str | None = None
        self._cross_market_cache: dict[str, Any] | None = None
        self._cross_market_cache_read_us = 0
        self._state_lock = threading.RLock()
        self.book_source_us: dict[str, int] = {}
        self.candidate_books_initialized: set[str] = set()
        self.candidate_settlements: dict[str, dict[str, Any]] = {}
        self.candidate_engine = None
        self.candidate_feed = None
        self.candidate_error: str | None = None
        if config.candidates_enabled:
            from .candidates.engine import CandidateEngine
            from .candidates.feed import BinanceFeed
            try:
                directory = config.data_dir / "candidates" / "v1"
                self.candidate_engine = CandidateEngine(directory, self.started_us)
                self.candidate_feed = BinanceFeed(directory, config.proxy)
                for saved in self.candidate_engine.open_markets():
                    self.known_markets[saved["marketId"]] = LiveMarket(
                        slug=saved["marketId"], condition_id=saved["conditionId"],
                        start_us=saved["startUs"], end_us=saved["endUs"],
                        token_by_outcome=saved["tokenByOutcome"], title=saved["marketId"],
                    )
            except Exception as exc:
                self.candidate_error = f"候选账户加载失败，已暂停：{type(exc).__name__}: {exc}"
        self.store.append(
            "sessions",
            {
                "schemaVersion": 1,
                "eventId": f"monitor-session-{self.started_us}-{uuid4().hex[:8]}",
                "eventType": "MONITOR_SESSION_STARTED",
                "receivedTimeUs": str(self.started_us),
                "payload": {
                    "config": self.config.to_public_dict(),
                    "gammaUrl": GAMMA_URL,
                    "websocketUrl": WSS_URL,
                    "rawCompression": "gzip",
                    "compressedStreams": list(COMPRESSED_PAPER_STREAMS),
                    "rawPolicy": {
                        "allTrades": True,
                        "priceChangeIntervalMs": self.config.raw_price_change_interval_ms,
                        "bookIntervalMs": self.config.raw_book_interval_ms,
                        "derivedTop10SnapshotIntervalMs": self.config.derived_snapshot_interval_ms,
                    },
                    "paperOnly": True,
                    "usesPrivateKey": False,
                },
            },
            self.started_us,
        )

    def _restore_open_markets(self) -> None:
        for position in self.ledger.positions.values():
            market_id = str(position.get("marketId", ""))
            market_data = position.get("market") or {}
            token_by_outcome = dict(market_data.get("tokenByOutcome") or {})
            if not market_id or set(token_by_outcome) != {"UP", "DOWN"}:
                continue
            try:
                start_us = int(market_id.rsplit("-", 1)[1]) * 1_000_000
                end_us = int(market_data["endUs"])
            except (KeyError, TypeError, ValueError):
                continue
            self.known_markets[market_id] = LiveMarket(
                slug=market_id,
                condition_id=str(market_data.get("conditionId", "")),
                start_us=start_us,
                end_us=end_us,
                token_by_outcome={str(key): str(value) for key, value in token_by_outcome.items()},
                title=market_id,
            )

    def request_stop(self) -> None:
        self.stop_event.set()

    def close(self) -> None:
        if self.candidate_engine is not None:
            self.candidate_engine.close()
        self.session.close()
        self.store.close()

    async def run(self, duration_seconds: float | None = None) -> None:
        async with market_clock.client(self.config.proxy) as clock_client:
            await self._run_with_clock(clock_client, duration_seconds)

    async def _run_with_clock(self, clock_client, duration_seconds: float | None) -> None:
        await market_clock.synchronize(client=clock_client)
        tasks = [
            asyncio.create_task(market_clock.maintain(self.config.proxy, client=clock_client)),
            asyncio.create_task(self._market_loop()),
            asyncio.create_task(self._settlement_loop()),
            asyncio.create_task(self._state_writer_loop()),
        ]
        if self.candidate_engine is not None and self.candidate_feed is not None and not self.candidate_error:
            tasks.extend([asyncio.create_task(self.candidate_feed.run()),
                          asyncio.create_task(self._candidate_loop())])
        if duration_seconds is not None and duration_seconds > 0:
            tasks.append(asyncio.create_task(self._duration_timer(duration_seconds)))
        try:
            await self.stop_event.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self.candidate_engine is not None:
                await asyncio.to_thread(self.candidate_engine.close)
            self.connection_status = "stopped"
            self._write_latest_state()
            self.close()

    async def _candidate_loop(self) -> None:
        """Snapshot quickly; all account calculations and durable writes run off-loop."""
        assert self.candidate_engine is not None and self.candidate_feed is not None
        while not self.stop_event.is_set():
            current_us = now_us()
            with self._state_lock:
                market = self.active_market
                definition = None if market is None else {
                    "marketId": market.market_id, "conditionId": market.condition_id,
                    "startUs": market.start_us, "endUs": market.end_us,
                    "tokenByOutcome": dict(market.token_by_outcome),
                }
                books = {}
                if market is not None and self.connection_status == "live":
                    for side, token in market.token_by_outcome.items():
                        if token not in self.candidate_books_initialized:
                            continue
                        asks = self._levels(token, "ASK")
                        relevant = [x for x in asks if x.price <= Decimal(".50")]
                        if not relevant:
                            relevant = asks[:1]
                        books[side] = {
                            "asks": [[str(x.price), str(x.size)] for x in relevant],
                            "sourceUs": self.book_source_us.get(token, 0),
                            "receivedUs": self.book_updated_us.get(token, 0),
                            "clockUncertaintyUs": int((market_clock.snapshot()["uncertaintyMs"] or 0) * 1000),
                        }
                settlements = dict(self.candidate_settlements)
                feed = self.candidate_feed.snapshot()
            try:
                await asyncio.to_thread(self.candidate_engine.tick, definition, books, feed, settlements, current_us)
                with self._state_lock:
                    for key, label in settlements.items():
                        if self.candidate_settlements.get(key) == label:
                            self.candidate_settlements.pop(key)
            except Exception as exc:
                self.candidate_error = f"候选账户暂停：{type(exc).__name__}: {exc}"
                self._record_health("ERROR", self.candidate_error)
                return
            await asyncio.sleep(0.05)

    async def _duration_timer(self, duration_seconds: float) -> None:
        await asyncio.sleep(duration_seconds)
        self.request_stop()

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
        base = current_seconds - current_seconds % 300
        candidates = (base, base - 300, base + 300)
        available: list[LiveMarket] = []
        for start_seconds in candidates:
            raw = await self._fetch_market(f"btc-updown-5m-{start_seconds}")
            if not raw:
                continue
            try:
                market = parse_gamma_market(raw)
            except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
                self.last_error = f"invalid Gamma market {start_seconds}: {exc}"
                continue
            if market.start_us <= current_seconds * 1_000_000 < market.end_us:
                available.append(market)
        return max(available, key=lambda item: item.start_us) if available else None

    async def _market_loop(self) -> None:
        retry_seconds = 1.0
        while not self.stop_event.is_set():
            try:
                market = await self._discover_current_market()
                if market is None:
                    self.connection_status = "waiting_for_market"
                    await asyncio.sleep(self.config.discovery_interval_seconds)
                    continue
                self.active_market = market
                self.known_markets[market.market_id] = market
                self._record_market_definition(market)
                await self._monitor_market_ws(market)
                retry_seconds = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep monitor alive and record diagnostics.
                self.connection_status = "reconnecting"
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.counters["connectionErrors"] += 1
                self._record_health("ERROR", self.last_error)
                await asyncio.sleep(retry_seconds)
                retry_seconds = min(retry_seconds * 2, 15.0)

    async def _monitor_market_ws(self, market: LiveMarket) -> None:
        assets = list(market.token_by_outcome.values())
        token_to_outcome = {token: outcome for outcome, token in market.token_by_outcome.items()}
        subscription = {
            "assets_ids": assets,
            "type": "market",
        }
        self.books = {}
        self.book_updated_us = {}
        self.book_source_us = {}
        self.candidate_books_initialized = set()
        self.book_executable_since_us = {}
        self.price_history = {}
        self._book_levels_cache.clear()
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
            await websocket.send(json.dumps(subscription))
            self.connection_status = "live"
            self.last_error = None
            last_app_ping = time.monotonic()
            while not self.stop_event.is_set() and now_us() < market.end_us + 2_000_000:
                if time.monotonic() - last_app_ping >= 10:
                    await websocket.send("PING")
                    last_app_ping = time.monotonic()
                try:
                    message = await asyncio.wait_for(websocket.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                received_us = now_us()
                self.last_message_us = received_us
                if message == "PONG":
                    continue
                parsed = json.loads(message)
                self.counters["receivedMessages"] += 1
                if self._should_record_raw(parsed, received_us):
                    self.store.append(
                        "raw",
                        {
                            "schemaVersion": 1,
                            "receivedTimeUs": str(received_us),
                            "marketId": market.market_id,
                            "message": parsed,
                        },
                        received_us,
                    )
                    self.counters["rawMessages"] += 1
                values = parsed if isinstance(parsed, list) else [parsed]
                for value in values:
                    if isinstance(value, dict):
                        self._process_ws_event(market, token_to_outcome, value, received_us)

    def _should_record_raw(self, message: Any, received_us: int) -> bool:
        if isinstance(message, list):
            self.last_raw_book_us = received_us
            return True
        if not isinstance(message, dict):
            return True
        event_type = str(message.get("event_type", ""))
        if event_type in {"last_trade_price", "market_resolved"}:
            return True
        if event_type == "price_change":
            interval_us = self.config.raw_price_change_interval_ms * 1_000
            if received_us - self.last_raw_price_change_us < interval_us:
                return False
            self.last_raw_price_change_us = received_us
            return True
        if event_type == "book":
            interval_us = self.config.raw_book_interval_ms * 1_000
            if received_us - self.last_raw_book_us < interval_us:
                return False
            self.last_raw_book_us = received_us
            return True
        return True

    def _process_ws_event(
        self,
        market: LiveMarket,
        token_to_outcome: dict[str, str],
        message: dict[str, Any],
        received_us: int,
    ) -> None:
        with self._state_lock:
            event_type = str(message.get("event_type", ""))
            if event_type == "book":
                asset_id = str(message.get("asset_id", ""))
                source_us = int(message.get("timestamp", received_us // 1_000)) * 1_000
                had_book = asset_id in self.books
                self.books[asset_id] = {
                    f"BID:{level['price']}": Decimal(str(level["size"]))
                    for level in message.get("bids", [])
                    if Decimal(str(level["size"])) > 0
                }
                self.books[asset_id].update({
                    f"ASK:{level['price']}": Decimal(str(level["size"]))
                    for level in message.get("asks", [])
                    if Decimal(str(level["size"])) > 0
                })
                self._invalidate_book_levels(asset_id)
                self.book_updated_us[asset_id] = received_us
                self.book_source_us[asset_id] = int(message.get("timestamp", 0)) * 1_000
                self.candidate_books_initialized.add(asset_id)
                self._record_price_observation(asset_id, source_us)
                self._update_executable_stability(asset_id, received_us)
                self._record_derived_snapshot(
                    market,
                    token_to_outcome,
                    message,
                    received_us,
                    force=not had_book,
                )
                self._try_paper_exits(market, received_us)
            elif event_type == "price_change":
                source_us = int(message.get("timestamp", received_us // 1_000)) * 1_000
                for change in message.get("price_changes", []):
                    asset_id = str(change.get("asset_id", ""))
                    side = "BID" if str(change.get("side", "")).upper() == "BUY" else "ASK"
                    key = f"{side}:{change['price']}"
                    size = Decimal(str(change["size"]))
                    book = self.books.setdefault(asset_id, {})
                    if size > 0:
                        book[key] = size
                    else:
                        book.pop(key, None)
                    self._invalidate_book_levels(asset_id, side)
                    self.book_updated_us[asset_id] = received_us
                    self.book_source_us[asset_id] = int(message.get("timestamp", 0)) * 1_000
                    self._record_price_observation(asset_id, source_us)
                    self._update_executable_stability(asset_id, received_us)
                self._record_derived_snapshot(market, token_to_outcome, message, received_us)
                self._try_paper_exits(market, received_us)
            elif event_type == "last_trade_price":
                asset_id = str(message.get("asset_id", ""))
                outcome = token_to_outcome.get(asset_id)
                if outcome:
                    trade_event = self._record_trade(market, outcome, message, received_us)
                    if outcome in self.config.outcomes:
                        self._evaluate_signal(market, trade_event, received_us)
            elif event_type == "market_resolved":
                winner = normalize_outcome(str(message.get("winning_outcome", "")))
                if winner in {"UP", "DOWN"}:
                    self._record_settlement(market, winner, received_us)

    def _record_market_definition(self, market: LiveMarket) -> None:
        event = {
            "schemaVersion": 1,
            "eventId": f"live-{market.market_id}-definition",
            "eventType": "MARKET_DEFINITION",
            "venue": "POLYMARKET",
            "marketId": market.market_id,
            "sourceTimeUs": str(market.start_us),
            "receivedTimeUs": str(now_us()),
            "source": {"type": "official_gamma", "collector": "research_paper_monitor"},
            "payload": {
                "underlying": "BTC/USD",
                "eventStartUs": str(market.start_us),
                "eventEndUs": str(market.end_us),
                "outcomes": ["UP", "DOWN"],
                "conditionId": market.condition_id,
                "tokenByOutcome": market.token_by_outcome,
                "title": market.title,
            },
        }
        self._record_event(event)

    def _record_trade(
        self,
        market: LiveMarket,
        outcome: str,
        message: dict[str, Any],
        received_us: int,
    ) -> dict[str, Any]:
        source_us = int(message.get("timestamp", received_us // 1_000)) * 1_000
        event = {
            "schemaVersion": 1,
            "eventId": f"live-trade-{message.get('transaction_hash') or uuid4().hex}-{source_us}",
            "eventType": "TRADE",
            "venue": "POLYMARKET",
            "marketId": market.market_id,
            "instrumentId": str(message.get("asset_id", "")),
            "outcome": outcome,
            "sourceTimeUs": str(source_us),
            "receivedTimeUs": str(received_us),
            "source": {"type": "official_market_wss", "collector": "research_paper_monitor"},
            "payload": {
                "outcome": outcome,
                "side": str(message.get("side", "")).upper(),
                "price": str(message.get("price", "0")),
                "size": str(message.get("size", "0")),
                "feeRateBps": str(message.get("fee_rate_bps", "")),
                "transactionHash": str(message.get("transaction_hash", "")),
            },
        }
        self._record_event(event)
        return event

    def _record_derived_snapshot(
        self,
        market: LiveMarket,
        token_to_outcome: dict[str, str],
        source_message: dict[str, Any],
        received_us: int,
        *,
        force: bool = False,
    ) -> None:
        if not force and received_us - self.last_snapshot_us < self.config.derived_snapshot_interval_ms * 1_000:
            return
        outcomes: list[dict[str, Any]] = []
        for token, outcome in token_to_outcome.items():
            book = self.books.get(token)
            if not book:
                continue
            bids = sorted(
                ((key.split(":", 1)[1], size) for key, size in book.items() if key.startswith("BID:")),
                key=lambda item: Decimal(item[0]),
                reverse=True,
            )[:10]
            asks = sorted(
                ((key.split(":", 1)[1], size) for key, size in book.items() if key.startswith("ASK:")),
                key=lambda item: Decimal(item[0]),
            )[:10]
            outcomes.append({
                "outcome": outcome,
                "tokenId": token,
                "bids": [[price, decimal_string(size)] for price, size in bids],
                "asks": [[price, decimal_string(size)] for price, size in asks],
                "bestBid": bids[0][0] if bids else None,
                "bestBidSize": decimal_string(bids[0][1]) if bids else None,
                "bestAsk": asks[0][0] if asks else None,
                "bestAskSize": decimal_string(asks[0][1]) if asks else None,
            })
        if not outcomes:
            return
        source_us = int(source_message.get("timestamp", received_us // 1_000)) * 1_000
        event = {
            "schemaVersion": 1,
            "eventId": f"live-book-{market.market_id}-{source_us}-{uuid4().hex[:8]}",
            "eventType": "ORDERBOOK_SNAPSHOT",
            "venue": "POLYMARKET",
            "marketId": market.market_id,
            "sourceTimeUs": str(source_us),
            "receivedTimeUs": str(received_us),
            "source": {"type": "official_market_wss_derived", "collector": "research_paper_monitor"},
            "payload": {"outcomes": outcomes, "depthLevels": 10},
        }
        self._record_event(event)
        self.last_snapshot_us = received_us

    def _evaluate_signal(
        self,
        market: LiveMarket,
        trade_event: dict[str, Any],
        received_us: int,
    ) -> None:
        payload = trade_event["payload"]
        outcome = str(trade_event.get("outcome") or payload.get("outcome") or "DOWN")
        price = Decimal(str(payload["price"]))
        size = Decimal(str(payload["size"]))
        remaining = Decimal(market.end_us - int(trade_event["sourceTimeUs"])) / Decimal("1000000")
        token = market.token_by_outcome.get(outcome)
        book_age_ms = (
            Decimal(received_us - self.book_updated_us.get(token, 0)) / Decimal("1000")
            if token else Decimal("999999999")
        )
        stable_since = self.book_executable_since_us.get(token or "", 0)
        book_stable_ms = (
            Decimal(received_us - stable_since) / Decimal("1000")
            if stable_since else Decimal("0")
        )
        preview = self._execution_preview(token)
        entry_vwap = preview[0] if preview else None
        entry_source_us = int(trade_event["sourceTimeUs"])
        momentum = self._price_momentum(token, entry_source_us)
        cross_market = self._cross_market_context(market, outcome, received_us)
        checks = {
            "clockSynchronized": not market_clock.enabled or market_clock.snapshot()["status"] == "synced",
            "buySide": payload["side"] == "BUY",
            "priceBand": self.config.price_min <= price <= self.config.price_max,
            "remainingBand": (
                Decimal(self.config.min_remaining_seconds)
                <= remaining
                <= Decimal(self.config.max_remaining_seconds)
            ),
            "tradeSize": size >= self.config.min_observed_trade_size,
            "freshOrderBook": book_age_ms <= self.config.max_book_age_ms,
            "stableOrderBook": book_stable_ms >= self.config.stable_book_ms,
            "fullDepthAtPriceCap": preview is not None,
            "entryVwapBand": (
                entry_vwap is not None
                and self.config.entry_price_min <= entry_vwap <= self.config.price_max
            ),
            "sameDirectionMomentum": (
                self.config.min_entry_momentum <= 0
                or (momentum is not None and momentum >= self.config.min_entry_momentum)
            ),
            "crossMarketAligned": (
                bool(cross_market.get("allAligned"))
                if self.config.require_cross_market_resonance else True
            ),
        }
        qualified = all(checks.values())
        reasons = [name for name, passed in checks.items() if not passed]
        signal = {
            "schemaVersion": 1,
            "eventId": f"signal-{market.market_id}-{trade_event['eventId']}",
            "eventType": "SIGNAL_EVALUATION",
            "venue": "POLYMARKET_PAPER",
            "marketId": market.market_id,
            "outcome": outcome,
            "sourceTimeUs": trade_event["sourceTimeUs"],
            "receivedTimeUs": str(received_us),
            "payload": {
                "strategyId": self.config.strategy_id,
                "outcome": outcome,
                "price": decimal_string(price),
                "size": decimal_string(size),
                "remainingSeconds": decimal_string(remaining),
                "timeCohorts": [
                    name for name, included in (
                        ("remaining_60_5", Decimal("5") <= remaining <= Decimal("60")),
                        ("remaining_60_45", Decimal("45") <= remaining <= Decimal("60")),
                        ("remaining_45_5", Decimal("5") <= remaining < Decimal("45")),
                    ) if included
                ],
                "bookAgeMs": decimal_string(book_age_ms),
                "bookStableMs": decimal_string(book_stable_ms),
                "entryVwapPreview": (
                    decimal_string(entry_vwap) if entry_vwap is not None else None
                ),
                "momentumWindowSeconds": self.config.momentum_window_seconds,
                "sameDirectionMomentum": (
                    decimal_string(momentum) if momentum is not None else None
                ),
                "crossMarket": cross_market,
                "checks": checks,
                "qualified": qualified,
                "status": "REJECTED" if not qualified else "QUALIFIED",
                "reasons": reasons,
            },
        }
        self.counters["signalEvaluations"] += 1
        self.counters["signalEvaluationsByOutcome"][outcome] += 1
        if qualified:
            self.counters["qualifiedSignals"] += 1
            self.counters["qualifiedSignalsByOutcome"][outcome] += 1
            self._try_paper_fill(market, signal, received_us)
        self.store.append("signals", signal, received_us)
        self.recent_signals.append(signal)
        self.recent_signals = self.recent_signals[-100:]

    def _record_price_observation(self, token: str, received_us: int) -> None:
        """Keep a short best-ask history for the pre-entry momentum gate."""
        asks = self._levels(token, "ASK")
        if not asks:
            return
        history = self.price_history.get(token)
        if history is None:
            history = self.price_history[token] = PriceHistory()
        history.append((received_us, asks[0].price))
        retention_us = max(self.config.momentum_window_seconds * 3, 30) * 1_000_000
        cutoff = received_us - retention_us
        history.expire_before(cutoff)

    def _price_momentum(self, token: str | None, received_us: int) -> Decimal | None:
        if not token:
            return None
        history = self.price_history.get(token, [])
        if not history:
            return None
        current = next((price for timestamp, price in reversed(history) if timestamp <= received_us), None)
        target_us = received_us - self.config.momentum_window_seconds * 1_000_000
        previous = next((price for timestamp, price in reversed(history) if timestamp <= target_us), None)
        if current is None or previous is None:
            return None
        return current - previous

    def _try_paper_fill(self, market: LiveMarket, signal: dict[str, Any], received_us: int) -> None:
        payload = signal["payload"]
        outcome = str(signal["outcome"])
        if self.ledger.has_market_position(market.market_id):
            payload["status"] = "ALREADY_FILLED"
            payload["reasons"] = ["oneTradePerMarket"]
            return
        token = market.token_by_outcome.get(outcome)
        if not token or token not in self.books:
            payload["status"] = "NO_FILL"
            payload["reasons"] = ["missingOrderBook"]
            self.counters["paperNoFills"] += 1
            self.counters["paperNoFillsByOutcome"][outcome] += 1
            return
        book_age_ms = Decimal(received_us - self.book_updated_us.get(token, 0)) / Decimal("1000")
        payload["bookAgeMs"] = decimal_string(book_age_ms)
        if book_age_ms > self.config.max_book_age_ms:
            payload["status"] = "NO_FILL"
            payload["reasons"] = ["staleOrderBook"]
            self.counters["paperNoFills"] += 1
            self.counters["paperNoFillsByOutcome"][outcome] += 1
            return
        asks = self._levels(token, "ASK")
        depth_at_cap = sum(
            (level.size for level in asks if level.price <= self.config.price_max),
            Decimal("0"),
        )
        payload["depthAtPriceCap"] = decimal_string(depth_at_cap)
        consumed = consume_asks(
            asks,
            self.config.paper_quantity,
            self.config.price_max,
            fee_model=self.config.fee_model,
            fee_rate=self.config.fee_rate,
        )
        if consumed is None:
            payload["status"] = "NO_FILL"
            payload["reasons"] = ["insufficientDepthAtPriceCap"]
            self.counters["paperNoFills"] += 1
            self.counters["paperNoFillsByOutcome"][outcome] += 1
            return
        vwap, fee_per_share, worst_price = consumed
        if not self.config.entry_price_min <= vwap <= self.config.price_max:
            payload["status"] = "NO_FILL"
            payload["reasons"] = ["entryVwapOutOfBand"]
            self.counters["paperNoFills"] += 1
            self.counters["paperNoFillsByOutcome"][outcome] += 1
            return
        fill = self.ledger.fill(
            market,
            outcome=outcome,
            signal=signal,
            quantity=self.config.paper_quantity,
            vwap=vwap,
            worst_price=worst_price,
            fee_per_share=fee_per_share,
            received_us=received_us,
            depth_at_cap=depth_at_cap,
        )
        payload["status"] = "PAPER_FILLED"
        payload["paperFillEventId"] = fill["eventId"]
        payload["entryVwap"] = decimal_string(vwap)
        payload["feePerShare"] = decimal_string(fee_per_share)
        self.counters["paperFills"] += 1
        self.counters["paperFillsByOutcome"][outcome] += 1

    def _execution_preview(
        self, token: str | None
    ) -> tuple[Decimal, Decimal, Decimal] | None:
        if not token or token not in self.books:
            return None
        return consume_asks(
            self._levels(token, "ASK"),
            self.config.paper_quantity,
            self.config.price_max,
            fee_model=self.config.fee_model,
            fee_rate=self.config.fee_rate,
        )

    def _update_executable_stability(self, token: str, received_us: int) -> None:
        if self._execution_preview(token) is None:
            self.book_executable_since_us.pop(token, None)
        else:
            self.book_executable_since_us.setdefault(token, received_us)

    def _try_paper_exits(self, market: LiveMarket, received_us: int) -> None:
        if not self.config.enable_early_exit:
            return
        positions = [
            position for position in self.ledger.positions.values()
            if position.get("marketId") == market.market_id
        ]
        for position in positions:
            token = market.token_by_outcome.get(str(position.get("outcome")))
            if not token:
                continue
            quantity = Decimal(str(position["payload"]["quantity"]))
            consumed = consume_bids(
                self._levels(token, "BID"),
                quantity,
                self.config.early_exit_price,
                fee_model=self.config.fee_model,
                fee_rate=self.config.fee_rate,
            )
            if consumed is None:
                continue
            vwap, fee_per_share, worst_price = consumed
            self.ledger.exit(
                position,
                vwap=vwap,
                worst_price=worst_price,
                fee_per_share=fee_per_share,
                received_us=received_us,
            )
            self.counters["paperExits"] += 1

    def _cross_market_context(
        self, market: LiveMarket, outcome: str, received_us: int
    ) -> dict[str, Any]:
        path = self.config.cross_market_state_path
        if path is None:
            return {"available": False, "fresh": False, "allAligned": False}
        if received_us - self._cross_market_cache_read_us >= 200_000:
            try:
                self._cross_market_cache = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                self._cross_market_cache = None
            self._cross_market_cache_read_us = received_us
        state = self._cross_market_cache or {}
        feature = state.get("currentFeature") or {}
        feature_time = int(feature.get("sourceTimeUs") or 0)
        age_ms = (received_us - feature_time) / 1_000 if feature_time else None
        fresh = (
            feature.get("marketId") == market.market_id
            and age_ms is not None
            and -1_000 <= age_ms <= self.config.cross_market_max_age_ms
        )
        signals = feature.get("signals") or {}
        sources = ("binance", "binanceFutures", "hyperliquid")
        directions = {source: signals.get(source, "MISSING") for source in sources}
        all_aligned = fresh and all(value == outcome for value in directions.values())
        features = feature.get("features") or {}
        short_returns = {
            source: {
                key: (features.get(source) or {}).get(key)
                for key in (
                    "return1s", "return3s", "return5s", "return10s", "returnFromStart"
                )
            }
            for source in sources
        }
        return {
            "available": bool(feature),
            "fresh": fresh,
            "featureAgeMs": age_ms,
            "featureSourceTimeUs": str(feature_time) if feature_time else None,
            "directions": directions,
            "allAligned": all_aligned,
            "shortReturns": short_returns,
        }

    def _levels(self, token: str, side: str) -> list[BookLevel]:
        cached = self._book_levels_cache.get((token, side))
        if cached is not None:
            return cached
        prefix = f"{side}:"
        levels = sorted(
            (
                BookLevel(price=Decimal(key.split(":", 1)[1]), size=size)
                for key, size in self.books.get(token, {}).items()
                if key.startswith(prefix) and size > 0
            ),
            key=lambda level: level.price,
            reverse=side == "BID",
        )
        # Only cache books maintained by the websocket handler. Synthetic books
        # supplied by diagnostics/tests may be mutated directly by their caller.
        if token in self.candidate_books_initialized:
            self._book_levels_cache[token, side] = levels
        return levels

    def _invalidate_book_levels(self, token: str, side: str | None = None) -> None:
        for value in ((side,) if side else ("BID", "ASK")):
            self._book_levels_cache.pop((token, value), None)

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
                        self._record_settlement(market, winner, now_us())
                        self._settlement_retry_at_us.pop(market.market_id, None)
                        self._settlement_attempts.pop(market.market_id, None)
                    else:
                        self._schedule_settlement_retry(market.market_id, current_us)
                except Exception as exc:  # noqa: BLE001
                    self.last_error = f"settlement {market.slug}: {exc}"
                    self._record_health("WARN", self.last_error)
                    self._schedule_settlement_retry(market.market_id, current_us)
            await asyncio.sleep(self.config.settlement_interval_seconds)

    def _schedule_settlement_retry(self, market_id: str, current_us: int) -> None:
        attempts = self._settlement_attempts.get(market_id, 0)
        self._settlement_attempts[market_id] = attempts + 1
        delay = settlement_retry_delay_seconds(self.config.settlement_interval_seconds, attempts)
        self._settlement_retry_at_us[market_id] = current_us + int(delay * 1_000_000)

    def _record_settlement(self, market: LiveMarket, winner: str, received_us: int) -> None:
        with self._state_lock:
            if market.market_id in self.settled_markets:
                return
            event = {
                "schemaVersion": 1,
                "eventId": f"live-{market.market_id}-settlement",
                "eventType": "SETTLEMENT",
                "venue": "POLYMARKET",
                "marketId": market.market_id,
                "sourceTimeUs": str(market.end_us),
                "receivedTimeUs": str(received_us),
                "source": {"type": "official_gamma", "collector": "research_paper_monitor"},
                "payload": {"winnerOutcome": winner, "settlementPrice": "1"},
            }
            self._record_event(event)
            self.ledger.settle(market, winner, received_us)
            if self.candidate_engine is not None:
                self.candidate_settlements[market.market_id] = {"winner": winner, "receivedUs": received_us}
            self.settled_markets.add(market.market_id)

    def _record_health(self, level: str, message: str) -> None:
        received_us = now_us()
        self.store.append(
            "health",
            {
                "schemaVersion": 1,
                "eventType": "MONITOR_HEALTH",
                "receivedTimeUs": str(received_us),
                "payload": {"level": level, "message": message},
            },
            received_us,
        )

    def _record_event(self, event: dict[str, Any]) -> None:
        self.store.append("events", event, int(event["receivedTimeUs"]))
        self.counters["normalizedEvents"] += 1

    async def _state_writer_loop(self) -> None:
        while not self.stop_event.is_set():
            if time.monotonic() - self._disk_checked_at >= 60:
                self._disk_state = await asyncio.to_thread(disk_space_state, self.config.data_dir)
                self._disk_checked_at = time.monotonic()
            self._report_disk_space()
            self._write_latest_state()
            await asyncio.sleep(1)

    def _report_disk_space(self) -> None:
        level = self._disk_state["level"]
        if level != self._disk_reported_level:
            if level != "ok" or self._disk_reported_level is not None:
                self._record_health("ERROR" if level == "critical" else "INFO" if level == "ok" else "WARN",
                                    self._disk_state["message"])
            self._disk_reported_level = level

    def _write_latest_state(self) -> None:
        state_dir = self.config.data_dir / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        target = state_dir / "latest.json"
        temporary = state_dir / "latest.json.tmp"
        temporary.write_text(json.dumps(self.get_state(), ensure_ascii=False, indent=2) + "\n")
        temporary.replace(target)

    def get_state(self) -> dict[str, Any]:
        with self._state_lock:
            market = self.active_market
            market_state: dict[str, Any] | None = None
            if market is not None:
                remaining = Decimal(market.end_us - now_us()) / Decimal("1000000")
                outcomes: dict[str, Any] = {}
                for outcome, token in market.token_by_outcome.items():
                    bids = self._levels(token, "BID")
                    asks = self._levels(token, "ASK")
                    outcomes[outcome] = {
                        "bestBid": decimal_string(bids[0].price) if bids else None,
                        "bestBidSize": decimal_string(bids[0].size) if bids else None,
                        "bestAsk": decimal_string(asks[0].price) if asks else None,
                        "bestAskSize": decimal_string(asks[0].size) if asks else None,
                        "bookAgeMs": decimal_string(
                            Decimal(now_us() - self.book_updated_us[token]) / Decimal("1000")
                        ) if token in self.book_updated_us else None,
                        "bids": [[decimal_string(x.price), decimal_string(x.size)] for x in bids[:5]],
                        "asks": [[decimal_string(x.price), decimal_string(x.size)] for x in asks[:5]],
                    }
                market_state = {
                    "slug": market.slug,
                    "title": market.title,
                    "conditionId": market.condition_id,
                    "tokenByOutcome": dict(market.token_by_outcome),
                    "startUs": str(market.start_us),
                    "endUs": str(market.end_us),
                    "remainingSeconds": decimal_string(remaining),
                    "outcomes": outcomes,
                }
            return {
                "schemaVersion": 1,
                "generatedAtUs": str(now_us()),
                "service": {
                    "status": self.connection_status,
                    "startedAtUs": str(self.started_us),
                    "lastMessageAtUs": str(self.last_message_us) if self.last_message_us else None,
                    "lastError": self.last_error,
                    "dataDir": str(self.config.data_dir.resolve()),
                    "clock": market_clock.snapshot(),
                    "dashboardUrl": getattr(self, "dashboard_url", None),
                },
                "strategy": self.config.to_public_dict(),
                "storage": dict(self._disk_state),
                "market": market_state,
                "counters": dict(self.counters),
                "paperStats": self.ledger.stats(strategy_id=self.config.strategy_id),
                "paperStatsAllHistory": self.ledger.stats(),
                "paperStatsByOutcome": {
                    outcome: self.ledger.stats(outcome, self.config.strategy_id)
                    for outcome in self.config.outcomes
                },
                "paperStatsByTimeCohort": {
                    cohort: self.ledger.stats(
                        strategy_id=self.config.strategy_id, time_cohort=cohort
                    )
                    for cohort in ("remaining_60_45", "remaining_45_5", "remaining_60_5")
                },
                "openPositions": list(self.ledger.positions.values()),
                "recentPaperEvents": self.ledger.recent_events[-25:],
                "recentSignals": self.recent_signals[-25:],
                "candidates": ({**self.candidate_engine.public_state, "runtimeError": self.candidate_error}
                               if self.candidate_engine is not None else {
                                   "enabled": self.config.candidates_enabled,
                                   "runtimeError": self.candidate_error,
                               }),
            }


def parse_gamma_market(raw: dict[str, Any]) -> LiveMarket:
    slug = str(raw["slug"])
    start_seconds = int(slug.rsplit("-", 1)[1])
    outcomes_raw = raw.get("outcomes") or []
    tokens_raw = raw.get("clobTokenIds") or []
    outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
    tokens = json.loads(tokens_raw) if isinstance(tokens_raw, str) else tokens_raw
    if len(outcomes) != len(tokens) or len(tokens) != 2:
        raise ValueError("expected two outcomes and two CLOB token IDs")
    token_by_outcome = {
        normalize_outcome(str(outcome)): str(token)
        for outcome, token in zip(outcomes, tokens)
    }
    if set(token_by_outcome) != {"UP", "DOWN"}:
        raise ValueError(f"unexpected outcomes: {list(token_by_outcome)}")
    return LiveMarket(
        slug=slug,
        condition_id=str(raw["conditionId"]),
        start_us=start_seconds * 1_000_000,
        end_us=iso_to_us(str(raw["endDate"])),
        token_by_outcome=token_by_outcome,
        title=str(raw.get("question") or slug),
    )


def normalize_outcome(value: str) -> str:
    normalized = value.strip().upper()
    if normalized in {"UP", "YES"}:
        return "UP"
    if normalized in {"DOWN", "NO"}:
        return "DOWN"
    return normalized


def winner_from_gamma(raw: dict[str, Any]) -> str | None:
    if not raw.get("closed"):
        return None
    outcomes_raw = raw.get("outcomes") or []
    prices_raw = raw.get("outcomePrices") or []
    outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
    prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
    for outcome, price in zip(outcomes, prices):
        if Decimal(str(price)) >= Decimal("0.999"):
            winner = normalize_outcome(str(outcome))
            return winner if winner in {"UP", "DOWN"} else None
    return None
