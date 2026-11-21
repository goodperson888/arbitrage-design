from __future__ import annotations

import asyncio
import os
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, ROUND_UP
from pathlib import Path
from typing import Any, AsyncIterable, Iterable, Mapping, Optional, Tuple

from .adapters import NormalizedBinanceCandle, normalize_binance_kline, normalize_polymarket_message
from .discovery import MarketDefinition, normalize_market_payload
from .fak import BookLevel, FAKRejected, build_fak_request
from .clob_transport import safe_provider_message
from .monitoring import MonitoringRecorder
from .models import ActiveTrade, MarketSnapshot, StrategyConfig
from .order_state import OrderRecord, OrderState, OrderStateMachine, OrderUnknown, SQLiteOrderStore
from .preflight import PreflightReport, run_preflight
from .risk import DailyRiskGate, RiskHalt, risk_day_now
from .settings import SettingsStore
from .strategy import evaluate_signal, fee_per_share
from .transport import DisabledOrderTransport, OrderTransport


@dataclass(frozen=True)
class RunnerConfig:
    mode: str = "observe"
    data_dir: Path = Path("data/live_trading")
    strategy: Optional[StrategyConfig] = None
    transport_enabled: bool = False
    live_enabled: bool = False
    preflight_passed: bool = False
    credentials_present: bool = False
    idle_seconds: float = 1.0
    proxy: Optional[str] = None
    gamma_url: str = "https://gamma-api.polymarket.com"
    raw_sample_seconds: float = 5.0
    orderbook_sample_seconds: float = 5.0
    signal_reject_cooldown_seconds: float = 1.0
    orderbook_levels: int = 10
    canary_max_session_orders: int = 1
    max_daily_loss_count: int = 2
    max_daily_loss_total: Decimal = Decimal("10")
    max_unsettled_markets: int = 1
    preflight_environment: Optional[Mapping[str, str]] = None


class LiveRunner:
    def __init__(self, config: RunnerConfig, transport: Optional[OrderTransport] = None) -> None:
        if config.mode not in {"observe", "canary", "live"}:
            raise ValueError("runner mode must be observe, canary, or live")
        if config.strategy is None:
            raise ValueError("strategy configuration is required")
        self.config = config
        self.strategy = config.strategy
        self.recorder = MonitoringRecorder(config.data_dir, live_mode=config.mode)
        env = dict(config.preflight_environment or os.environ)
        env["LIVE_MODE"] = config.mode
        if config.live_enabled:
            env["LIVE_TRADING_ENABLE"] = "1"
        if config.transport_enabled:
            env["POLYMARKET_ORDER_ENABLED"] = "1"
        if config.preflight_passed:
            env["POLYMARKET_PREFLIGHT_APPROVED"] = "1"
        if not config.credentials_present:
            for name in ("POLYMARKET_PRIVATE_KEY", "POLYMARKET_API_KEY", "POLYMARKET_API_SECRET", "POLYMARKET_API_PASSPHRASE", "CLOB_API_KEY", "CLOB_SECRET", "CLOB_PASS_PHRASE"):
                env.pop(name, None)
        elif config.mode != "observe" and not any(env.get(name, "") for name in ("POLYMARKET_PRIVATE_KEY",)):
            # Unit/integration callers may provide an injected transport and
            # only assert the rollout gates.  The real CLI always supplies the
            # actual credentials from the environment and never uses this
            # placeholder path.
            env.update({
                "POLYMARKET_PRIVATE_KEY": "injected-transport-credential",
                "POLYMARKET_API_KEY": "injected-transport-api-key",
                "POLYMARKET_API_SECRET": "injected-transport-api-secret",
                "POLYMARKET_API_PASSPHRASE": "injected-transport-api-passphrase",
            })
        self.preflight: PreflightReport = run_preflight(
            env,
            transport_enabled=config.transport_enabled,
            require_credentials=config.mode != "observe",
            rollout_approved=config.preflight_passed,
        )
        self.submit_allowed = config.mode != "observe" and self.preflight.safe
        self.transport = transport or DisabledOrderTransport()
        self.order_store = SQLiteOrderStore(config.data_dir / "orders.sqlite3")
        self.order_machine = OrderStateMachine(self.order_store, self.transport)
        self.risk = DailyRiskGate(
            day=risk_day_now(),
            max_budget=config.strategy.max_budget,
            max_loss_count=config.max_daily_loss_count,
            max_loss_total=config.max_daily_loss_total,
            max_unsettled_markets=config.max_unsettled_markets,
        )
        self.market: Optional[MarketDefinition] = None
        self.active_trade: Optional[ActiveTrade] = None
        self._active_trades: dict[str, ActiveTrade] = {}
        self.binance_candles: list[NormalizedBinanceCandle] = []
        self._binance_last_received_at: Optional[datetime] = None
        self.book: Optional[Mapping[str, Any]] = None
        self._books: dict[str, Mapping[str, Any]] = {}
        self._last_down_price: Optional[Decimal] = None
        self._last_down_price_at: Optional[datetime] = None
        self._down_price_history: list[tuple[datetime, Decimal]] = []
        self._price_histories: dict[str, list[tuple[datetime, Decimal]]] = {}
        self.order_attempts = 0
        self.accepted_signals = 0
        self._order_markets: set[str] = set()
        self._paper_markets: set[str] = set()
        self._paper_positions: dict[str, dict[str, Any]] = {}
        self._settled_markets: set[str] = set()
        self._settled_order_markets: set[str] = set()
        self._settled_paper_markets: set[str] = set()
        self._execution_strategy_cache: dict[str, StrategyConfig] = {}
        self._execution_strategy_lock = threading.Lock()
        self._fee_warm_threads: list[threading.Thread] = []
        self._snapshot_buckets: dict[str, int] = {}
        self._raw_sample_buckets: dict[str, int] = {}
        self._orderbook_sample_buckets: dict[str, int] = {}
        self._signal_reject_buckets: dict[tuple[str, str], int] = {}
        self._fill_threads: list[threading.Thread] = []
        self._fill_threads_lock = threading.Lock()
        self._control_path = config.data_dir / "ui_settings.json"
        self._control_mtime_ns: Optional[int] = None
        self._order_submission_enabled = config.mode != "observe"
        self._refresh_runtime_control(force=True)
        self._restore_risk_state()
        self.recorder.record(
            "sessions",
            "SESSION_STARTED",
            venue="SYSTEM",
            payload={
                "mode": config.mode,
                "preflightSafe": self.preflight.safe,
                "strategyParameters": {
                    "maxBudgetUsd": str(self.strategy.max_budget),
                    "activeTradeMinQuantity": str(self.strategy.active_trade_min_quantity),
                    "activeTradeMultiplier": str(self.strategy.active_trade_multiplier),
                    "maxDailyLossCount": self.config.max_daily_loss_count,
                    "maxDailyLossTotalUsd": str(self.config.max_daily_loss_total),
                },
                "recordingPolicy": {
                    "rawSampleSeconds": config.raw_sample_seconds,
                    "orderbookSampleSeconds": config.orderbook_sample_seconds,
                    "signalRejectCooldownSeconds": config.signal_reject_cooldown_seconds,
                    "orderbookLevels": config.orderbook_levels,
                },
            },
        )
        self.recorder.record("health", "HEALTH_STATUS", venue="SYSTEM", payload={"status": "runner_started", "submitAllowed": self.submit_allowed})
        self._recover_orders_before_trading()
        self._prepare_transport_before_signals()

    def run_events(self, events: Iterable[Tuple[str, Mapping[str, Any]]]) -> None:
        for venue, message in events:
            self.process_event(venue, message)

    def pending_settlement_market_ids(self) -> set[str]:
        active_markets = self._paper_markets if self.config.mode == "observe" else self._order_markets
        settled_markets = self._settled_paper_markets if self.config.mode == "observe" else self._settled_order_markets
        return set(active_markets) - set(settled_markets)

    def _recover_orders_before_trading(self) -> None:
        if not self.submit_allowed:
            return
        open_orders = self.order_machine.recover_open_orders()
        for record in open_orders:
            if not record.remote_order_id:
                self.submit_allowed = False
                self.recorder.record(
                    "health",
                    "ORDER_RECOVERY_BLOCKED",
                    venue="POLYMARKET",
                    market_id=record.market_id,
                    payload={"status": "halted", "clientOrderId": record.client_order_id, "state": record.state.value, "reason": "missing_remote_order_id"},
                )

                continue
            try:
                response = self.transport.fetch(record.remote_order_id)
                recovered = self.order_machine.reconcile(record.client_order_id, response)
                self.recorder.record(
                    "orders",
                    "ORDER_RECOVERED",
                    venue="POLYMARKET",
                    market_id=record.market_id,
                    outcome="DOWN",
                    payload={"clientOrderId": record.client_order_id, "orderId": record.remote_order_id, "state": recovered.state.value},
                )
                if recovered.state == OrderState.UNKNOWN:
                    self.submit_allowed = False
                    self.recorder.record("health", "ORDER_RECOVERY_BLOCKED", venue="POLYMARKET", market_id=record.market_id, payload={"status": "halted", "clientOrderId": record.client_order_id, "reason": "remote_state_unknown"})
                else:
                    self._schedule_fill_reconciliation(recovered)
            except Exception as exc:  # noqa: BLE001 - any recovery ambiguity blocks all new orders.
                self.submit_allowed = False
                self.recorder.record(
                    "health",
                    "ORDER_RECOVERY_BLOCKED",
                    venue="POLYMARKET",
                    market_id=record.market_id,
                    payload={"status": "halted", "clientOrderId": record.client_order_id, "reason": type(exc).__name__},
                )

    def _prepare_transport_before_signals(self) -> None:
        """Warm an injected/real transport off the signal hot path."""
        if not self.submit_allowed or self.config.mode == "observe":
            return
        prepare = getattr(self.transport, "prepare", None)
        if not callable(prepare):
            return
        try:
            prepare()
        except Exception as exc:  # noqa: BLE001 - fail closed before signals.
            self.submit_allowed = False
            self.recorder.record(
                "health",
                "TRANSPORT_PREPARE_FAILED",
                venue="POLYMARKET",
                payload={"status": "halted", "reason": safe_provider_message(exc) or type(exc).__name__},
            )

    async def run_async(self, events: AsyncIterable[Tuple[str, Mapping[str, Any]]]) -> None:
        async for venue, message in events:
            self.process_event(venue, message)

    async def run_forever(self) -> None:
        from .network import LiveNetworkFeed

        feed = LiveNetworkFeed(self, gamma_url=self.config.gamma_url, proxy=self.config.proxy)
        try:
            await feed.run()
        except asyncio.CancelledError:
            feed.request_stop()
            raise

    def record_health(self, event_type: str, detail: str) -> None:
        self.recorder.record("health", event_type, venue="SYSTEM", payload={"status": "feed_error", "detail": detail})

    def process_event(self, venue: str, message: Mapping[str, Any]) -> None:
        self._refresh_runtime_control()
        if self._should_record_raw(venue, message):
            self.recorder.record("raw", "RAW_MESSAGE", venue=venue.upper(), market_id=str(message.get("market_id", message.get("marketId", ""))), payload=dict(message))
        if venue == "rtds":
            source = str(message.get("source", ""))
            source_time = message.get("source_time")
            if isinstance(source_time, datetime):
                source_time_us = int(source_time.astimezone(timezone.utc).timestamp() * 1_000_000)
            else:
                source_time_us = int(message.get("source_time_us", int(time.time() * 1_000_000)))
            payload = {
                "source": source,
                "topic": str(message.get("topic", "")),
                "symbol": str(message.get("symbol", "")),
                "value": str(message.get("value", "")),
            }
            if message.get("window_seconds") is not None:
                payload["windowSeconds"] = int(message["window_seconds"])
            self.recorder.record(
                "events",
                "REFERENCE_PRICE",
                venue="POLYMARKET_RTDS",
                market_id=str(message.get("symbol", "")),
                source_time_us=source_time_us,
                payload=payload,
            )
            return
        if venue == "polymarket" and message.get("type") == "market_definition":
            self.market = normalize_market_payload(message)
            self.active_trade = None
            self._active_trades.clear()
            self.book = None
            self._books.clear()
            self._down_price_history.clear()
            self._price_histories.clear()
            self._last_down_price = None
            self._last_down_price_at = None
            self.recorder.record("events", "MARKET_DEFINITION", venue="POLYMARKET", market_id=self.market.market_id, payload=dict(message))
            self._warm_market_execution_async()
            return
        if venue == "polymarket":
            event_type = str(message.get("type", message.get("event_type", ""))).lower()
            if event_type == "market_resolved":
                self._settle_market(str(message.get("market_id", self.market.market_id if self.market else "")), str(message.get("winning_outcome", message.get("winner", ""))).upper())
                return
            if event_type in {"book", "price_change"}:
                self._process_market_book(message)
                return
            if event_type == "tick_size_change":
                self.recorder.record(
                    "events",
                    "MARKET_TICK_SIZE_CHANGED",
                    venue="POLYMARKET",
                    market_id=self.market.market_id if self.market else str(message.get("market", "")),
                    outcome="DOWN" if self.market and str(message.get("asset_id", "")) == self.market.down_token_id else None,
                    payload=dict(message),
                )
                return
            if event_type == "best_bid_ask":
                self.recorder.record(
                    "events",
                    "BEST_BID_ASK",
                    venue="POLYMARKET",
                    market_id=self.market.market_id if self.market else str(message.get("market", "")),
                    outcome="DOWN" if self.market and str(message.get("asset_id", "")) == self.market.down_token_id else None,
                    payload=dict(message),
                )
                return
            if event_type not in {"trade", "last_trade", "last_trade_price", "fill"}:
                self.recorder.record(
                    "health",
                    "POLYMARKET_EVENT_IGNORED",
                    venue="POLYMARKET",
                    market_id=self.market.market_id if self.market else str(message.get("market", "")),
                    payload={"status": "warning", "eventType": event_type or "missing"},
                )
                return
            normalized = dict(message)
            normalized.setdefault("market_id", self.market.market_id if self.market else "")
            trade = normalize_polymarket_message(normalized)
            side = "DOWN" if self.market and trade.token_id == self.market.down_token_id else "UP" if self.market and trade.token_id == self.market.up_token_id else ""
            if not side or self.strategy.side.upper() not in {"BOTH", side}:
                return
            self.active_trade = ActiveTrade(side, trade.aggressor, trade.quantity, trade.event_time)
            self._active_trades[side] = self.active_trade
            self.recorder.record("events", "TRADE", venue="POLYMARKET", market_id=trade.market_id, outcome=side, source_time_us=int(trade.event_time.timestamp() * 1_000_000), payload={"price": str(trade.price), "quantity": str(trade.quantity), "aggressor": trade.aggressor})
            self._try_signal(side)
            return
        if venue == "binance":
            candle = normalize_binance_kline(message)
            self._binance_last_received_at = self._event_received_at(message)
            replaced = False
            for index, existing in enumerate(self.binance_candles):
                if existing.open_time == candle.open_time:
                    self.binance_candles[index] = candle
                    replaced = True
                    break
            if not replaced:
                self.binance_candles.append(candle)
            self.binance_candles.sort(key=lambda item: item.open_time)
            self.binance_candles = self.binance_candles[-5:]
            self.recorder.record("events", "REFERENCE_PRICE", venue="BINANCE", market_id=f"{candle.symbol.lower()}_spot", source_time_us=int(candle.close_time.timestamp() * 1_000_000), payload={"open": str(candle.open), "close": str(candle.close), "interval": "1m"})
            self._try_signal()
            return
        if venue == "book":
            side = str(message.get("side", "DOWN")).upper()
            self.book = message
            self._books[side] = message
            self._update_side_price(side, Decimal(str(message.get("down_price_now", message.get("price_now", "0")))), self._event_received_at(message))
            self.recorder.record("events", "ORDERBOOK_SNAPSHOT", venue="POLYMARKET", market_id=str(message.get("market_id", "")), outcome=side, payload=dict(message))
            self._try_signal(side)

    def _message_time_us(self, message: Mapping[str, Any]) -> int:
        value = message.get("received_time_us", message.get("receivedTimeUs"))
        if value is None:
            value = message.get("timestamp", message.get("time"))
        if value is None:
            return int(time.time() * 1_000_000)
        numeric = float(value)
        if numeric < 10_000_000_000:
            numeric *= 1_000_000
        elif numeric < 10_000_000_000_000:
            numeric *= 1_000
        return int(numeric)

    def _should_record_raw(self, venue: str, message: Mapping[str, Any]) -> bool:
        event_type = str(message.get("event_type", message.get("type", ""))).lower()
        if event_type in {"market_definition", "last_trade_price", "trade", "market_resolved"}:
            return True
        interval = max(float(self.config.raw_sample_seconds), 0.0)
        if interval == 0:
            return True
        bucket = int(self._message_time_us(message) / (interval * 1_000_000))
        key = venue.upper()
        if self._raw_sample_buckets.get(key) == bucket:
            return False
        self._raw_sample_buckets[key] = bucket
        return True

    def _event_received_at(self, message: Mapping[str, Any]) -> datetime:
        value = message.get("received_time_us", message.get("receivedTimeUs"))
        if value is None:
            return datetime.now(timezone.utc)
        return datetime.fromtimestamp(float(value) / 1_000_000, tz=timezone.utc)

    def _update_down_price(self, price: Decimal, received_at: datetime) -> None:
        self._update_side_price("DOWN", price, received_at)

    def _update_side_price(self, side: str, price: Decimal, received_at: datetime) -> None:
        history = self._price_histories.setdefault(side, [])
        history.append((received_at, price))
        cutoff = received_at.timestamp() - 6.0
        history[:] = [item for item in history if item[0].timestamp() >= cutoff]
        if side == "DOWN":
            self._down_price_history = history
        self._last_down_price = price
        self._last_down_price_at = received_at

    def _down_price_five_seconds_ago(self, received_at: datetime, fallback: Decimal) -> Decimal:
        return self._side_price_five_seconds_ago("DOWN", received_at, fallback)

    def _side_price_five_seconds_ago(self, side: str, received_at: datetime, fallback: Decimal) -> Decimal:
        cutoff = received_at.timestamp() - 5.0
        eligible = [price for timestamp, price in self._price_histories.get(side, []) if timestamp.timestamp() <= cutoff]
        return eligible[-1] if eligible else fallback

    def _process_market_book(self, message: Mapping[str, Any]) -> None:
        if self.market is None:
            return
        asset_id = str(message.get("asset_id", message.get("assetId", "")))
        if not asset_id and message.get("price_changes"):
            first_change = message["price_changes"][0]
            asset_id = str(first_change.get("asset_id", first_change.get("assetId", ""))) if isinstance(first_change, Mapping) else ""
        if asset_id == self.market.down_token_id:
            side = "DOWN"
        elif asset_id == self.market.up_token_id:
            side = "UP"
        else:
            side = str(message.get("side", "DOWN")).upper()
        if side not in {"UP", "DOWN"} or self.strategy.side.upper() not in {"BOTH", side}:
            return
        received_at = self._event_received_at(message)
        asks = message.get("asks") or []
        bids = message.get("bids") or []
        if str(message.get("event_type", message.get("type", ""))).lower() == "price_change":
            # Apply provider deltas to the last full snapshot.  Polymarket
            # labels BUY as a bid update and SELL as an ask update.
            prior = dict(self._books.get(side) or {})
            asks = [[str(level[0]), str(level[1])] for level in prior.get("asks", [])]
            bids = [[str(level[0]), str(level[1])] for level in prior.get("bids", [])]
            for change in message.get("price_changes", []):
                if str(change.get("asset_id", change.get("assetId", ""))) != (self.market.down_token_id if side == "DOWN" else self.market.up_token_id):
                    continue
                target = bids if str(change.get("side", "")).upper() == "BUY" else asks
                price = str(change.get("price", "0"))
                size = Decimal(str(change.get("size", "0")))
                target[:] = [level for level in target if level[0] != price]
                if size > 0:
                    target.append([price, str(size)])
        def level_pair(level: Any) -> list[str]:
            if isinstance(level, Mapping):
                return [str(level.get("price", "0")), str(level.get("size", "0"))]
            return [str(level[0]), str(level[1])]

        asks = [level_pair(level) for level in asks]
        bids = [level_pair(level) for level in bids]
        asks.sort(key=lambda level: Decimal(level[0]))
        bids.sort(key=lambda level: Decimal(level[0]), reverse=True)
        if not asks:
            return
        down_price = Decimal(asks[0][0])
        remaining = Decimal(str(message.get("remaining_seconds", "0")))
        if not remaining and self.market.end_time:
            remaining = Decimal(str((self.market.end_time - received_at).total_seconds()))
        five_seconds_ago = self._side_price_five_seconds_ago(side, received_at, down_price)
        self._update_side_price(side, down_price, received_at)
        self.book = {
            "market_id": self.market.market_id,
            "side": side,
            "remaining_seconds": str(remaining),
            "down_price_now": str(down_price),
            "down_price_five_seconds_ago": str(five_seconds_ago),
            "asks": asks,
            "bids": bids,
            "received_time_us": int(received_at.timestamp() * 1_000_000),
        }
        self._books[side] = self.book
        sample_interval = max(float(self.config.orderbook_sample_seconds), 0.0)
        sample_bucket = int(received_at.timestamp() / sample_interval) if sample_interval else None
        should_record = sample_interval == 0 or self._orderbook_sample_buckets.get(self.market.market_id) != sample_bucket
        if should_record:
            self._orderbook_sample_buckets[self.market.market_id] = sample_bucket if sample_bucket is not None else 0
            sampled_book = dict(self.book)
            levels = max(int(self.config.orderbook_levels), 1)
            sampled_book["asks"] = asks[:levels]
            sampled_book["bids"] = bids[:levels]
            self.recorder.record("events", "ORDERBOOK_SNAPSHOT", venue="POLYMARKET", market_id=self.market.market_id, outcome=side, payload=sampled_book)
        self._record_second_snapshot(received_at, side)
        self._try_signal(side)

    def _record_second_snapshot(self, received_at: datetime, side: str = "DOWN") -> None:
        if self.market is None or self.book is None:
            return
        bucket = int(received_at.timestamp())
        market_id = self.market.market_id
        if self._snapshot_buckets.get(market_id) == bucket:
            return
        self._snapshot_buckets[market_id] = bucket
        asks = self.book.get("asks", [])
        bids = self.book.get("bids", [])
        best_ask = Decimal(str(asks[0][0])) if asks else Decimal("0")
        best_bid = Decimal(str(bids[0][0])) if bids else Decimal("0")
        ask_depth = sum((Decimal(str(level[1])) for level in asks[:10]), Decimal("0"))
        bid_depth = sum((Decimal(str(level[1])) for level in bids[:10]), Decimal("0"))
        binance_return = ""
        if len(self.binance_candles) >= 5 and self.binance_candles[0].open:
            binance_return = str((self.binance_candles[-1].close - self.binance_candles[0].open) / self.binance_candles[0].open)
        payload = {
            "remainingSeconds": self.book.get("remaining_seconds", ""),
            "downPrice": self.book.get("down_price_now", ""),
            "downPriceFiveSecondsAgo": self.book.get("down_price_five_seconds_ago", ""),
            "bestAsk": str(best_ask),
            "bestBid": str(best_bid),
            "spread": str(best_ask - best_bid) if best_bid else "",
            "askDepthTop10": str(ask_depth),
            "bidDepthTop10": str(bid_depth),
            "binanceSpotReturn5m": binance_return,
            "activeTradeSide": self.active_trade.side if self.active_trade else "",
            "activeTradeAggressor": self.active_trade.aggressor if self.active_trade else "",
            "activeTradeQuantity": str(self.active_trade.quantity) if self.active_trade else "",
            "sourceBucketSecond": bucket,
        }
        self.recorder.record("events", "MARKET_SNAPSHOT_1S", venue="POLYMARKET", market_id=market_id, outcome=side, source_time_us=int(received_at.timestamp() * 1_000_000), payload=payload)

    def _try_signal(self, side: str | None = None) -> None:
        self._refresh_runtime_control()
        if self.market is None:
            return
        side = str(side or (self.active_trade.side if self.active_trade else "DOWN")).upper()
        if side not in {"UP", "DOWN"} or self.strategy.side.upper() not in {"BOTH", side}:
            return
        book = self._books.get(side) or (self.book if str((self.book or {}).get("side", "DOWN")).upper() == side else None)
        active_trade = self._active_trades.get(side)
        if book is None or active_trade is None:
            return
        self.book = book
        self.active_trade = active_trade
        outcome = side
        now = datetime.now(timezone.utc)
        # Binance Spot is optional reference telemetry.  The strategy no
        # longer uses its 5-minute direction as an entry condition, so a
        # missing/stale Binance feed must not suppress a valid Polymarket
        # signal.  Its latest rolling return is still attached when available.
        binance_return = Decimal("0")
        if len(self.binance_candles) >= 5 and self.binance_candles[0].open:
            binance_return = (self.binance_candles[-1].close - self.binance_candles[0].open) / self.binance_candles[0].open
        snapshot = MarketSnapshot(
            market_id=self.market.market_id,
            condition_id=self.market.condition_id,
            down_token_id=self.market.down_token_id if side == "DOWN" else self.market.up_token_id,
            remaining_seconds=Decimal(str(book["remaining_seconds"])),
            down_price_now=Decimal(str(book["down_price_now"])),
            down_price_five_seconds_ago=Decimal(str(book["down_price_five_seconds_ago"])),
            binance_return_5m=binance_return,
            active_trade=active_trade,
            received_at=datetime.fromtimestamp(int(book.get("received_time_us", 0)) / 1_000_000, tz=timezone.utc),
            side=side,
        )
        backend_eval_started_us = int(time.time() * 1_000_000)
        trigger_time_us = max(
            int(book.get("received_time_us", 0) or 0),
            int(active_trade.event_time.timestamp() * 1_000_000),
        )
        decision = evaluate_signal(snapshot, self.strategy, now=datetime.now(timezone.utc))
        if not decision.accepted:
            interval = max(float(self.config.signal_reject_cooldown_seconds), 0.0)
            bucket = int(snapshot.received_at.timestamp() / interval) if interval else None
            key = (self.market.market_id, decision.reason)
            if interval and self._signal_reject_buckets.get(key) == bucket:
                return
            if bucket is not None:
                self._signal_reject_buckets[key] = bucket
            self.recorder.record("signals", "SIGNAL_REJECTED", venue="POLYMARKET", market_id=self.market.market_id, outcome=outcome, payload={"reason": decision.reason, "price": str(snapshot.down_price_now)})
            return
        try:
            execution_strategy = self._strategy_for_execution()
            asks = [BookLevel(Decimal(str(level[0])), Decimal(str(level[1]))) for level in book.get("asks", [])]
            token_id = self.market.down_token_id if side == "DOWN" else self.market.up_token_id
            request = build_fak_request(self.market.market_id, token_id, asks, execution_strategy, side=side)
            request_built_us = int(time.time() * 1_000_000)
            if request.expected_vwap < self.strategy.min_entry_vwap:
                self.recorder.record("signals", "SIGNAL_REJECTED", venue="POLYMARKET", market_id=self.market.market_id, outcome=outcome, payload={"reason": "entry_vwap_below_min", "expectedVwap": str(request.expected_vwap), "minEntryVwap": str(self.strategy.min_entry_vwap)})
                return
            required_active_quantity = max(
                self.strategy.active_trade_min_quantity,
                (
                    request.quantity * self.strategy.active_trade_multiplier / self.strategy.quantity_step
                ).to_integral_value(rounding=ROUND_UP) * self.strategy.quantity_step,
            )
            signal_payload = {
                "reason": decision.reason,
                "price": str(snapshot.down_price_now),
                "effectiveOrderBudgetUsd": str(self.strategy.max_budget),
                "plannedOrderQuantity": str(request.quantity),
                "activeTradeQuantity": str(active_trade.quantity),
                "baseActiveTradeMinQuantity": str(self.strategy.active_trade_min_quantity),
                "activeTradeMultiplier": str(self.strategy.active_trade_multiplier),
                "requiredActiveTradeQuantity": str(required_active_quantity),
                "backendEvaluationStartedUs": backend_eval_started_us,
                "backendRequestBuiltUs": request_built_us,
                "triggerToRequestMs": round(max(0, request_built_us - trigger_time_us) / 1000, 3) if trigger_time_us else None,
            }
            if active_trade.quantity < required_active_quantity:
                self.recorder.record(
                    "signals",
                    "SIGNAL_REJECTED",
                    venue="POLYMARKET",
                    market_id=self.market.market_id,
                    outcome=outcome,
                    payload={**signal_payload, "reason": "active_trade_below_dynamic_threshold"},
                )
                return
            self.recorder.record("signals", "SIGNAL_ACCEPTED", venue="POLYMARKET", market_id=self.market.market_id, outcome=outcome, payload=signal_payload)
            self.accepted_signals += 1
            if self.config.mode == "canary" and self.order_attempts >= max(int(self.config.canary_max_session_orders), 0):
                self.recorder.record("signals", "SIGNAL_NOT_SUBMITTED", venue="POLYMARKET", market_id=self.market.market_id, outcome=outcome, payload={"reason": "canary_session_limit", "limit": int(self.config.canary_max_session_orders)})
                return
            # A non-observe runner must not reserve an unsettled-market slot
            # when its preflight/transport gate is closed.  Reserving first
            # would leave the in-memory risk gate occupied forever because no
            # order record exists to release it (the old behavior caused all
            # later opportunities to be reported as maximum exposure).
            if self.config.mode != "observe" and not self.submit_allowed:
                self.recorder.record("signals", "SIGNAL_NOT_SUBMITTED", venue="POLYMARKET", market_id=self.market.market_id, outcome=outcome, payload={"reason": "preflight_blocked"})
                return
            if self.config.mode != "observe":
                # Dashboard/operator reconciliation may close an interrupted
                # local request while this process remains online. Release
                # those terminal markets just before the next eligible signal
                # so a restart is not required and the one-market risk slot
                # cannot remain stale.
                self._release_closed_market_reservations()
            occupied_markets = self._paper_markets if self.config.mode == "observe" else self._order_markets
            if self.market.market_id in occupied_markets:
                self.recorder.record("signals", "SIGNAL_NOT_SUBMITTED", venue="POLYMARKET", market_id=self.market.market_id, outcome=outcome, payload={"reason": "one_order_per_market"})
                return
            if self.config.mode != "observe" and not self._order_submission_enabled:
                self.recorder.record("signals", "SIGNAL_NOT_SUBMITTED", venue="POLYMARKET", market_id=self.market.market_id, outcome=outcome, payload={"reason": "trading_paused"})
                return
            try:
                self.risk.admit(self.market.market_id, request.expected_cost)
            except RiskHalt as exc:
                self.recorder.record("orders", "ORDER_REJECTED", venue="POLYMARKET", market_id=self.market.market_id, outcome=outcome, payload={"reason": str(exc), "riskGate": True})
                return
            if not self.submit_allowed:
                if self.config.mode == "observe":
                    self._paper_markets.add(self.market.market_id)
                    paper_payload = {
                        "side": outcome,
                        "quantity": str(request.quantity),
                        "entryPrice": str(request.expected_vwap),
                        "expectedCost": str(request.expected_cost),
                        "limitPrice": str(request.limit_price),
                        "orderType": request.order_type,
                    }
                    self._paper_positions[self.market.market_id] = paper_payload
                    self.recorder.record("signals", "SIGNAL_PAPER_EXECUTED", venue="POLYMARKET", market_id=self.market.market_id, outcome=outcome, payload=paper_payload)
                    self.recorder.record("fills", "PAPER_FILL", venue="POLYMARKET", market_id=self.market.market_id, outcome=outcome, payload=paper_payload)
                else:
                    # Defensive release for a concurrent gate change between
                    # the check above and risk admission.  No live order was
                    # registered, so retaining this reservation would be
                    # incorrect.
                    self.risk.reserved_markets.discard(self.market.market_id)
                    self.recorder.record("signals", "SIGNAL_NOT_SUBMITTED", venue="POLYMARKET", market_id=self.market.market_id, outcome=outcome, payload={"reason": "preflight_blocked"})
                return
            # A critical monitoring write failure is a hard stop.  Do not
            # create or submit another order until the ledger is repaired and
            # the runner is restarted/reconciled.
            if not self.recorder.accepting_orders:
                self.submit_allowed = False
                self.risk.reserved_markets.discard(self.market.market_id)
                self.recorder.record("health", "ORDER_SUBMISSION_BLOCKED", venue="SYSTEM", market_id=self.market.market_id, payload={"reason": "critical_ledger_unavailable"})
                return
            order = self.order_machine.register(request)
            if order.state != OrderState.INTENT:
                if not self._holds_market_exposure(order):
                    self._release_closed_market_reservations()
                else:
                    self._order_markets.add(self.market.market_id)
                self.recorder.record("signals", "SIGNAL_NOT_SUBMITTED", venue="POLYMARKET", market_id=self.market.market_id, outcome=outcome, payload={"reason": "order_already_processed", "clientOrderId": order.client_order_id, "state": order.state.value})
                return
            self._order_markets.add(self.market.market_id)
            self.recorder.record("orders", "ORDER_REQUEST", venue="POLYMARKET", market_id=self.market.market_id, outcome=outcome, payload={"clientOrderId": order.client_order_id, "quantity": str(request.quantity), "limitPrice": str(request.limit_price), "orderType": "FAK", "side": outcome})
            self.order_attempts += 1
            submit_started_us = int(time.time() * 1_000_000)
            response = self.order_machine.submit(order.client_order_id)
            submit_finished_us = int(time.time() * 1_000_000)
            response_payload = dict(response.payload)
            response_payload.update({
                "submitStartedUs": submit_started_us,
                "submitFinishedUs": submit_finished_us,
                "submitLatencyMs": round((submit_finished_us - submit_started_us) / 1000, 3),
            })
            self.recorder.record("orders", "ORDER_RESPONSE", venue="POLYMARKET", market_id=self.market.market_id, outcome=outcome, payload=response_payload)
            if response.state == OrderState.UNKNOWN:
                self.submit_allowed = False
                self.recorder.record("orders", "ORDER_UNKNOWN", venue="POLYMARKET", market_id=self.market.market_id, outcome=outcome, payload={"clientOrderId": order.client_order_id, "reason": "CLOB response has no recognized order status", "reconcileRequired": True, "submissionError": response.payload.get("submission_error", {})})
            if response.state in {OrderState.REJECTED, OrderState.DISABLED, OrderState.CANCELED}:
                self._release_closed_market_reservations()
            if response.state == OrderState.DISABLED:
                self.order_attempts -= 1
                self.submit_allowed = False
                self.recorder.record("health", "ORDER_SUBMISSION_BLOCKED", venue="SYSTEM", market_id=self.market.market_id, payload={"reason": "order_gate_closed_before_post", "clientOrderId": order.client_order_id, "requestSent": False})
            if response.state == OrderState.REJECTED:
                provider_error = response.payload.get("provider_error", {}) if isinstance(response.payload, Mapping) else {}
                submission_error = response.payload.get("submission_error", {})
                not_sent = submission_error.get("requestSent") is False
                if not_sent:
                    # This attempt never reached post_order. It consumes no
                    # canary order quota, but a configuration/signer defect
                    # must be repaired before accepting another signal.
                    self.order_attempts -= 1
                    self.submit_allowed = False
                    self.recorder.record("health", "ORDER_PREPARATION_FAILED", venue="POLYMARKET", market_id=self.market.market_id, payload={"status": "halted", "clientOrderId": order.client_order_id, **submission_error})
                self.recorder.record(
                    "orders",
                    "ORDER_REJECTED",
                    venue="POLYMARKET",
                    market_id=self.market.market_id,
                    outcome=outcome,
                    payload={
                        "reason": str(submission_error.get("message") or provider_error.get("message") or "CLOB provider rejected order"),
                        "clientOrderId": order.client_order_id,
                        "phase": "prepare" if not_sent else "submit",
                        "requestSent": False if not_sent else True,
                        "reconcileRequired": False,
                        "statusCode": provider_error.get("statusCode"),
                        "authoritative": bool(provider_error.get("authoritative")),
                    },
                )
            self._schedule_fill_reconciliation(response)
        except OrderUnknown as exc:
            # An SDK/network exception after submit is ambiguous: the venue
            # may have accepted the FAK even though no response arrived. Keep
            # it visibly distinct from an authoritative reject and require
            # official reconciliation before any retry.
            self.submit_allowed = False
            saved = self.order_store.get(request.client_order_id)
            self.recorder.record("orders", "ORDER_UNKNOWN", venue="POLYMARKET", market_id=self.market.market_id, outcome=outcome, payload={"clientOrderId": saved.client_order_id, "reason": str(exc), "reconcileRequired": True, "submissionError": saved.payload.get("submission_error", {})})
        except (FAKRejected, OSError, RuntimeError) as exc:
            details = dict(getattr(exc, "details", {}) or {})
            self.recorder.record(
                "orders",
                "ORDER_REJECTED",
                venue="POLYMARKET",
                market_id=self.market.market_id,
                outcome=outcome,
                payload={"reason": str(exc), "depth": details} if details else {"reason": str(exc)},
            )

    def _strategy_for_execution(self) -> StrategyConfig:
        if self.config.mode == "observe":
            return self.strategy
        if self.market is None or not self.market.condition_id:
            self.submit_allowed = False
            raise RuntimeError("missing condition_id for CLOB V2 fee lookup")
        with self._execution_strategy_lock:
            cached = self._execution_strategy_cache.get(self.market.condition_id)
            if cached is not None:
                return cached
            fee_lookup = getattr(self.transport, "market_fee_details", None)
            if not callable(fee_lookup):
                self.submit_allowed = False
                raise RuntimeError("CLOB V2 dynamic fee lookup is required")
            try:
                details = fee_lookup(self.market.condition_id)
                fee_rate = Decimal(str(details["rate"]))
                fee_exponent = Decimal(str(details["exponent"]))
                # Validate the exact fee curve before building any order.
                fee_per_share(Decimal("0.5"), fee_rate, fee_exponent)
            except Exception as exc:  # noqa: BLE001 - unknown fee must halt orders.
                self.submit_allowed = False
                self.recorder.record("health", "MARKET_FEE_LOOKUP_FAILED", venue="POLYMARKET", market_id=self.market.market_id, payload={"status": "halted", "reason": type(exc).__name__})
                raise RuntimeError("CLOB V2 market fee lookup failed") from exc
            execution_strategy = replace(self.strategy, fee_rate=fee_rate, fee_exponent=fee_exponent)
            self._execution_strategy_cache[self.market.condition_id] = execution_strategy
            self.recorder.record("events", "MARKET_FEE_CONFIG", venue="POLYMARKET", market_id=self.market.market_id, outcome="DOWN", payload={"rate": str(fee_rate), "exponent": str(fee_exponent), "source": "clob_v2"})
            return execution_strategy

    def _warm_market_execution_async(self) -> None:
        """Fetch the market fee curve before its 90-second entry window."""
        if self.config.mode == "observe" or not self.submit_allowed or self.market is None:
            return
        thread = threading.Thread(target=self._warm_market_execution, args=(self.market.condition_id,), name="live-market-fee-warm", daemon=True)
        self._fee_warm_threads.append(thread)
        thread.start()

    def _warm_market_execution(self, condition_id: str) -> None:
        if not condition_id:
            return
        if self.market is None or self.market.condition_id != condition_id:
            return
        try:
            self._strategy_for_execution()
        except Exception:
            # The synchronous signal path will fail closed as well; this
            # health event gives the operator an early diagnosis.
            return

    def _schedule_fill_reconciliation(self, order: Any) -> None:
        if getattr(order, "state", None) not in {OrderState.ACKNOWLEDGED, OrderState.FILLED, OrderState.PARTIAL}:
            return
        remote_id = str(getattr(order, "remote_order_id", "") or "")
        fills_fn = getattr(self.transport, "fills", None)
        if not remote_id or not callable(fills_fn):
            return
        thread = threading.Thread(target=self._fetch_and_record_fills, args=(fills_fn, remote_id, order), name="live-fill-reconcile", daemon=True)
        with self._fill_threads_lock:
            self._fill_threads.append(thread)
        thread.start()

    def _fetch_and_record_fills(self, fills_fn: Any, remote_id: str, order: Any) -> None:
        try:
            order_side = str(getattr(order, "payload", {}).get("side", "DOWN")).upper()
            items: Any = []
            for attempt in range(3):
                items = fills_fn(remote_id) or []
                if items:
                    break
                if attempt < 2:
                    time.sleep(0.2)
            if isinstance(items, Mapping):
                items = items.get("fills", items.get("trades", []))
            if not isinstance(items, list):
                items = list(items) if items else []
            unique_items: list[Mapping[str, Any]] = []
            seen_trade_ids: set[str] = set()
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                trade_id = str(item.get("trade_id", item.get("tradeId", item.get("id", ""))) or "")
                if trade_id and trade_id in seen_trade_ids:
                    continue
                if trade_id:
                    seen_trade_ids.add(trade_id)
                unique_items.append(item)
                payload = dict(item)
                payload.setdefault("orderId", remote_id)
                payload.setdefault("clientOrderId", getattr(order, "client_order_id", ""))
                self.recorder.record("fills", "FILL", venue="POLYMARKET", market_id=getattr(order, "market_id", ""), outcome=order_side, payload=payload)
            if unique_items:
                self._persist_fill_summary(order, unique_items)
            else:
                self.recorder.record("health", "FILL_RECONCILIATION_EMPTY", venue="POLYMARKET", market_id=getattr(order, "market_id", ""), payload={"orderId": remote_id})
        except Exception as exc:  # noqa: BLE001 - reconciliation must not affect the order loop.
            self.submit_allowed = False
            self.recorder.record("health", "FILL_RECONCILIATION_ERROR", venue="POLYMARKET", market_id=getattr(order, "market_id", ""), payload={"orderId": remote_id, "error": type(exc).__name__})

    def _persist_fill_summary(self, order: Any, items: list[Mapping[str, Any]]) -> None:
        current = self.order_store.get(str(getattr(order, "client_order_id", "")))
        order_side = str(current.payload.get("side", "DOWN")).upper()
        fee_rate = Decimal(str(current.payload.get("fee_rate", self.strategy.fee_rate)))
        fee_exponent = Decimal(str(current.payload.get("fee_exponent", self.strategy.fee_exponent)))
        quantity = Decimal("0")
        notional = Decimal("0")
        total_fee = Decimal("0")
        worst_price = Decimal("0")
        trade_ids: list[str] = []
        for item in items:
            price = Decimal(str(item.get("price", "0") or "0"))
            size = Decimal(str(item.get("size", item.get("quantity", item.get("matched_size", "0"))) or "0"))
            if price <= 0 or size <= 0:
                continue
            fee_value = item.get("fee", item.get("fee_amount", item.get("feeAmount")))
            fee = Decimal(str(fee_value)) if fee_value not in (None, "") else size * fee_per_share(price, fee_rate, fee_exponent)
            quantity += size
            notional += price * size
            total_fee += fee
            worst_price = max(worst_price, price)
            trade_id = str(item.get("trade_id", item.get("tradeId", item.get("id", ""))) or "")
            if trade_id:
                trade_ids.append(trade_id)
        if quantity <= 0:
            raise ValueError("fill reconciliation returned no positive fills")

        expected_quantity = Decimal(str(current.payload.get("quantity", "0")))
        actual_cost = notional + total_fee
        actual_vwap = notional / quantity
        state = OrderState.FILLED if quantity >= expected_quantity else OrderState.PARTIAL
        payload = dict(current.payload)
        payload.update({
            "actual_filled_quantity": str(quantity),
            "actual_filled_notional": str(notional),
            "actual_filled_cost": str(actual_cost),
            "actual_fee": str(total_fee),
            "actual_vwap": str(actual_vwap),
            "actual_worst_price": str(worst_price),
            "trade_ids": trade_ids,
        })
        reconciled = OrderRecord(
            current.client_order_id,
            current.market_id,
            current.token_id,
            state,
            payload,
            current.remote_order_id,
        )
        self.order_store.save(reconciled)
        self.recorder.record(
            "orders",
            "ORDER_FILL_RECONCILED",
            venue="POLYMARKET",
            market_id=current.market_id,
            outcome=order_side,
            payload={
                "clientOrderId": current.client_order_id,
                "orderId": current.remote_order_id or "",
                "state": state.value,
                "actualFilledQuantity": str(quantity),
                "actualCost": str(actual_cost),
                "actualFee": str(total_fee),
                "actualVwap": str(actual_vwap),
            },
        )
        if (
            actual_cost > self.strategy.max_budget
            or worst_price > self.strategy.max_entry_price
            or actual_vwap < self.strategy.min_entry_vwap
        ):
            self.submit_allowed = False
            self.risk.halted = True
            self.recorder.record(
                "health",
                "FILL_RISK_BREACH",
                venue="POLYMARKET",
                market_id=current.market_id,
                payload={
                    "status": "halted",
                    "actualCost": str(actual_cost),
                    "worstPrice": str(worst_price),
                    "actualVwap": str(actual_vwap),
                    "minEntryVwap": str(self.strategy.min_entry_vwap),
                },
            )

    @staticmethod
    def _holds_market_exposure(record: OrderRecord) -> bool:
        # A canceled remainder can coexist with actual fills. Never release
        # exposure merely because the order's status is terminal.
        try:
            if Decimal(str(record.payload.get("actual_filled_quantity", "0"))) > 0 or Decimal(str(record.payload.get("actual_filled_cost", "0"))) > 0:
                return True
        except (ArithmeticError, ValueError):
            return True
        if record.state in {OrderState.REJECTED, OrderState.DISABLED}:
            return False
        if record.state == OrderState.CANCELED:
            if not record.remote_order_id:
                return False
            remote = record.payload.get("remote_response") or {}
            trade_ids = remote.get("associate_trades") or remote.get("tradeIDs") or remote.get("trade_ids")
            if trade_ids:
                return True
            try:
                return Decimal(str(remote.get("size_matched", "NaN"))) != 0
            except (ArithmeticError, ValueError):
                return True
        return True

    def _release_closed_market_reservations(self) -> None:
        records = self.order_store.list_all()
        held = {record.market_id for record in records if self._holds_market_exposure(record)}
        released = {record.market_id for record in records} - held
        self._order_markets.difference_update(released)
        self.risk.reserved_markets.difference_update(released)

    def _restore_risk_state(self) -> None:
        try:
            from .monitoring import iter_partitioned_events

            if self.config.mode != "observe":
                # An audit request is not proof of an open venue position.
                # Restore only markets whose persisted order can still
                # represent exposure (or whose status is unknown and must be
                # held until reconciliation).  Loading every historical
                # ORDER_REQUEST here permanently consumed the one-market
                # risk slot after a rejected/no-remote attempt.
                for record in self.order_store.list_all():
                    market_id = str(record.market_id or "")
                    if market_id and self._holds_market_exposure(record):
                        self._order_markets.add(market_id)
            if self.config.mode == "observe":
                for event in iter_partitioned_events(self.config.data_dir, "fills"):
                    if event.get("eventType") != "PAPER_FILL" or not event.get("paperOnly", True):
                        continue
                    market_id = str(event.get("marketId", ""))
                    if market_id:
                        self._paper_markets.add(market_id)
                        self._paper_positions[market_id] = dict(event.get("payload") or {})
            for event in iter_partitioned_events(self.config.data_dir, "settlements"):
                is_paper = bool(event.get("paperOnly")) or bool((event.get("payload") or {}).get("paper"))
                market_id = str(event.get("marketId", ""))
                if market_id:
                    if is_paper:
                        self._settled_paper_markets.add(market_id)
                        self._paper_markets.discard(market_id)
                        self._paper_positions.pop(market_id, None)
                    else:
                        self._settled_order_markets.add(market_id)
                        self._order_markets.discard(market_id)
                    self._settled_markets.add(market_id)
                payload = event.get("payload") or {}
                if (self.config.mode == "observe") != is_paper:
                    continue
                if payload.get("formal", True) is False:
                    continue
                try:
                    self.risk.record_settlement(Decimal(str(payload.get("netPnl", payload.get("pnl", "0")))))
                except (ArithmeticError, ValueError):
                    continue
            if self.config.mode == "observe":
                reserved_markets = set(self._paper_markets)
            else:
                # ORDER_REQUEST is an audit/attempt record, not proof of an
                # open position.  Only orders that can still represent venue
                # exposure reserve the daily unsettled-market slot.  This
                # prevents a canceled/rejected/no-remote request from
                # permanently blocking the next session.
                reserved_markets = {
                    record.market_id
                    for record in self.order_store.list_all()
                    if self._holds_market_exposure(record)
                    and record.market_id not in self._settled_order_markets
                }
            for market_id in reserved_markets:
                self.risk.reserved_markets.add(market_id)
        except OSError:
            self.risk.halted = True

    def _settle_market(self, market_id: str, winner: str) -> None:
        if (
            not market_id
            or winner not in {"UP", "DOWN"}
            or (
                market_id in self._settled_order_markets
                and market_id in self._settled_paper_markets
            )
        ):
            return
        records = [
            record for record in self.order_store.list_all()
            if record.market_id == market_id
            and (record.state in {OrderState.FILLED, OrderState.PARTIAL} or (record.state == OrderState.CANCELED and "actual_filled_quantity" in record.payload))
            and market_id not in self._settled_order_markets
        ]
        settled_order = False
        for record in records:
            if "actual_filled_quantity" not in record.payload or "actual_filled_cost" not in record.payload:
                self.submit_allowed = False
                self.recorder.record("health", "SETTLEMENT_BLOCKED_MISSING_FILLS", venue="POLYMARKET", market_id=market_id, payload={"status": "halted", "clientOrderId": record.client_order_id})
                continue
            quantity = Decimal(str(record.payload["actual_filled_quantity"]))
            cost = Decimal(str(record.payload["actual_filled_cost"]))
            payout = quantity if winner == str(record.payload.get("side", "DOWN")).upper() else Decimal("0")
            pnl = payout - cost
            formal = cost >= self.strategy.min_valid_cost
            if formal:
                self.risk.settle_market(market_id, pnl)
            else:
                self.risk.reserved_markets.discard(market_id)
            self.recorder.record("settlements", "SETTLEMENT", venue="POLYMARKET", market_id=market_id, outcome=str(record.payload.get("side", "DOWN")).upper(), payload={"winner": winner, "quantity": str(quantity), "actualCost": str(cost), "actualFee": str(record.payload.get("actual_fee", "0")), "pnl": str(pnl), "netPnl": str(pnl), "formal": formal})
            settled_order = True
        paper = self._paper_positions.pop(market_id, None) if market_id not in self._settled_paper_markets else None
        self._paper_markets.discard(market_id)
        if not records and paper is not None:
            quantity = Decimal(str(paper.get("quantity", "0")))
            cost = Decimal(str(paper.get("expectedCost", "0")))
            payout = quantity if winner == str(paper.get("side", "DOWN")).upper() else Decimal("0")
            pnl = payout - cost
            self.risk.settle_market(market_id, pnl)
            self.recorder.record("settlements", "SETTLEMENT", venue="POLYMARKET", market_id=market_id, outcome=str(paper.get("side", "DOWN")).upper(), payload={"winner": winner, "quantity": str(quantity), "pnl": str(pnl), "netPnl": str(pnl), "paper": True, "side": str(paper.get("side", "DOWN")).upper()})
        self._order_markets.discard(market_id)
        if settled_order:
            self._settled_order_markets.add(market_id)
        if paper is not None:
            self._settled_paper_markets.add(market_id)
        if settled_order or paper is not None:
            self._settled_markets.add(market_id)

    def close(self) -> None:
        self.recorder.record("health", "HEALTH_STOPPED", venue="SYSTEM", payload={"status": "runner_stopped", "orderAttempts": self.order_attempts})
        with self._fill_threads_lock:
            threads = list(self._fill_threads)
        for thread in threads:
            thread.join(timeout=2)
        for thread in list(self._fee_warm_threads):
            thread.join(timeout=2)
        close_transport = getattr(self.transport, "close", None)
        if callable(close_transport):
            close_transport()
        self.recorder.close()

    def _refresh_runtime_control(self, *, force: bool = False) -> None:
        """Poll dashboard controls without adding a command channel to feeds."""
        try:
            stat = self._control_path.stat()
        except OSError:
            if force:
                self.recorder.enabled = True
                self._order_submission_enabled = self.config.mode != "observe"
            return
        if not force and self._control_mtime_ns == stat.st_mtime_ns:
            return
        self._control_mtime_ns = stat.st_mtime_ns
        settings = SettingsStore(self.config.data_dir).load()
        self.recorder.enabled = bool(settings.get("dataRecordingEnabled", True))
        self._order_submission_enabled = self.config.mode != "observe" and bool(settings.get("orderSubmissionEnabled", True))
