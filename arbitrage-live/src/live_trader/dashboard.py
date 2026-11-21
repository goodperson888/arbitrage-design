from __future__ import annotations

import json
import importlib.util
import os
import signal
import subprocess
import sys
import threading
from collections import Counter
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import monotonic, sleep, time
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, urlparse

from .monitoring import STREAMS, MonitoringRecorder, data_usage, iter_partitioned_events, load_runtime_state, read_partitioned_events_tail
from .identity import CLOBIdentityManager, account_type_for_signature
from .order_state import OrderState, OrderStateMachine, SQLiteOrderStore
from .transport import DisabledOrderTransport
from .settings import ADDRESS_RE, KeychainSecretStore, PolygonAccountDetector, SettingsStore, SettingsValidationError, effective_daily_loss_count, effective_daily_loss_total, effective_order_budget


STATIC_DIR = Path(__file__).with_name("static")
COUNT_STREAMS = ("events", "signals", "orders", "fills", "settlements", "health", "sessions")


def merge_runtime_control_state(
    state: dict[str, Any],
    runner: Mapping[str, Any],
    control: Mapping[str, Any],
) -> dict[str, Any]:
    """Overlay the live process/control truth onto persisted monitoring state.

    Monitoring events intentionally survive restarts, so ``build_state``
    cannot by itself decide whether the current process may submit.  Keeping
    this overlay explicit prevents a stale session from making the dashboard
    show either a false READY state or a permanently hard-coded DISABLED one.
    """
    state["runner"] = dict(runner)
    state["control"] = dict(control)
    running = runner.get("status") == "running"
    mode = str(runner.get("mode") or control.get("desiredMode") or "observe")
    freshness_verified = bool(control.get("freshnessVerified"))
    submission_requested = bool(control.get("orderSubmissionEnabled"))
    orders_enabled = bool(control.get("effectiveOrderSubmissionEnabled"))
    transport_ready = running and mode in {"canary", "live"} and freshness_verified

    safety = state.setdefault("safety", {})
    safety["ordersDisabled"] = not orders_enabled
    safety["transport"] = "READY" if transport_ready else "DISABLED"
    if not running:
        block_reason = "runner_stopped"
    elif mode == "observe":
        block_reason = "observe_mode"
    elif not freshness_verified:
        block_reason = "waiting_for_freshness"
    elif not submission_requested:
        block_reason = "submission_paused"
    elif not orders_enabled:
        block_reason = "safety_gate_blocked"
    else:
        block_reason = ""
    safety["orderBlockReason"] = block_reason

    diagnostics = state.setdefault("executionDiagnostics", {})
    diagnostics_runner = diagnostics.setdefault("runner", {})
    diagnostics_runner["status"] = "running" if running else str(runner.get("status") or "stopped")
    if running:
        # A running process has no stop reason.  Preserve the heartbeat and
        # uptime values computed from the sidecar, but do not show an old
        # HEALTH_STOPPED event as the current failure.
        diagnostics_runner["stopReason"] = ""
        diagnostics_runner["stopDetail"] = "runner 正在运行；若订单数为 0，请查看下方拒绝原因统计"
    elif not diagnostics_runner.get("stopReason"):
        diagnostics_runner["stopReason"] = "runner_stopped"
        diagnostics_runner["stopDetail"] = "runner 未运行；不会接收新行情或提交订单"

    service = state.setdefault("service", {})
    service["mode"] = mode
    service["ordersDisabled"] = not orders_enabled
    overview = state.setdefault("overview", {})
    if not running:
        service["status"] = "stopped"
        service["health"] = "stopped"
        overview["serviceLabel"] = "已停止 · 行情不再更新"
        overview["orderGate"] = "已关闭（runner 未运行）"
        overview["marketFeed"] = "历史快照 · 非实时"
    elif mode == "observe":
        service["status"] = "observe_only"
        overview["orderGate"] = "已关闭（observe）"
    elif not freshness_verified:
        service["status"] = "order_gate_waiting"
        overview["serviceLabel"] = "行情启动中 · 订单关闭"
        overview["orderGate"] = "等待行情新鲜度验证"
    elif not submission_requested:
        service["status"] = "submission_paused"
        overview["serviceLabel"] = "行情正常 · 新订单已暂停"
        overview["orderGate"] = "预检已通过 · 等待允许新订单"
    elif orders_enabled:
        service["status"] = "order_submission_enabled"
        overview["serviceLabel"] = "行情正常 · 真实订单已允许"
        overview["orderGate"] = "已开启"
    else:
        service["status"] = "order_gate_blocked"
        overview["orderGate"] = "安全闸门阻止提交"
    return state


def _safe_identity_runtime_error(exc: RuntimeError) -> str:
    """Translate known SDK/runtime failures without exposing provider details."""
    message = str(exc)
    if "安装 py-clob-client-v2" in message:
        return "身份验证失败：当前 Python 环境未安装 py-clob-client-v2，请安装项目 live 依赖后重试"
    if "缺少 eth-account" in message:
        return "身份验证失败：当前 Python 环境缺少 eth-account，请安装项目 live 依赖后重试"
    if "SDK 不支持刷新 CLOB V2 余额缓存" in message:
        return "身份验证失败：当前 CLOB SDK 不支持余额/授权刷新，请升级 py-clob-client-v2"
    if "SDK 不支持未结订单查询" in message:
        return "身份验证失败：当前 CLOB SDK 不支持未结订单查询，请升级 py-clob-client-v2"
    if "身份派生" in message or "CLOB API 凭据" in message:
        return "身份验证失败：CLOB 凭据派生接口不可用，请确认 SDK 版本和网络连接"
    return "身份验证失败：CLOB SDK 运行时异常，请先运行启动前检查并确认 live 依赖已安装"


def _read_events(root: Path, stream: str, limit: int = 60) -> list[dict[str, Any]]:
    if not (root / stream).exists():
        return []
    return read_partitioned_events_tail(root, stream, limit)


_COUNT_CACHE: dict[tuple[str, str], tuple[tuple[tuple[str, int, int], ...], int]] = {}


def _stream_count(root: Path, stream: str) -> int:
    """Return a cached line count without reparsing JSON on every refresh."""
    paths = sorted((root / stream).glob("date=*/hour=*/part-*.ndjson"))
    paths.extend(sorted((root / stream).glob("date=*/hour=*/part-*.ndjson.gz")))
    signature = tuple((str(path), path.stat().st_size, path.stat().st_mtime_ns) for path in paths)
    key = (str(root), stream)
    cached = _COUNT_CACHE.get(key)
    if cached and cached[0] == signature:
        return cached[1]
    # wc is implemented in native code and avoids decoding every JSON event;
    # importantly, it runs only once per changed partition and not per poll.
    total = 0
    for path in paths:
        if path.suffix == ".gz":
            continue
        try:
            result = subprocess.run(
                ["wc", "-l", str(path)], capture_output=True, text=True,
                check=True, timeout=5,
            )
            total += int(result.stdout.strip().split()[0])
        except (OSError, ValueError, IndexError, subprocess.SubprocessError):
            # A transient writer/permission issue should not take the state
            # endpoint down; the recent tail remains available.
            total = 0
            break
    _COUNT_CACHE[key] = (signature, total)
    return total


def _latest_event(events: list[dict[str, Any]], event_types: set[str], *, venue: str | None = None) -> dict[str, Any] | None:
    candidates = [
        event
        for event in events
        if str(event.get("eventType", "")) in event_types
        and (venue is None or str(event.get("venue", "")).upper() == venue.upper())
    ]
    return max(candidates, key=lambda event: int(event.get("receivedTimeUs", 0)), default=None)


def _execution_diagnostics(
    root: Path,
    stream_events: Mapping[str, list[dict[str, Any]]],
    runtime_state: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a small, operator-facing explanation for every non-fill.

    This intentionally uses the recent tails already loaded for the dashboard
    rather than scanning raw/high-frequency partitions.  It remains useful
    when recording is paused because the runner keeps the same bounded tail in
    ``runtime_state.json``.  The UI labels the values as a recent window so a
    zero does not get mistaken for an all-time statistic.
    """
    execution_types = {
        "SIGNAL_ACCEPTED", "SIGNAL_REJECTED", "SIGNAL_NOT_SUBMITTED",
        "SIGNAL_PAPER_EXECUTED", "ORDER_REQUEST", "ORDER_RESPONSE",
        "ORDER_REJECTED", "ORDER_UNKNOWN", "ORDER_RECONCILED_NO_REMOTE", "FILL", "PAPER_FILL",
    }
    execution_events = [
        event
        for stream in ("signals", "orders", "fills")
        for event in stream_events.get(stream, [])
        if str(event.get("eventType", "")).upper() in execution_types
    ]
    execution_events.sort(key=lambda event: int(event.get("receivedTimeUs", 0) or 0))
    counts = Counter(str(event.get("eventType", "")).upper() for event in execution_events)
    reject_reasons: Counter[str] = Counter()
    for event in execution_events:
        if str(event.get("eventType", "")).upper() not in {"SIGNAL_REJECTED", "SIGNAL_NOT_SUBMITTED", "ORDER_REJECTED", "ORDER_UNKNOWN"}:
            continue
        payload = event.get("payload") or {}
        reason = str(payload.get("reason") or payload.get("detail") or "未提供原因")
        reject_reasons[reason] += 1

    runtime_updated_us = int(runtime_state.get("updatedTimeUs", 0) or 0)
    latest_events = [event for events in stream_events.values() for event in events]
    latest_event = max(latest_events, key=lambda event: int(event.get("receivedTimeUs", 0) or 0), default=None)
    latest_health = _latest_event(stream_events.get("health", []), {
        "HEALTH_STOPPED", "RUNNER_STOPPED", "RUNNER_ERROR", "PROCESS_EXITED",
    })
    latest_started = _latest_event(stream_events.get("sessions", []), {"SESSION_STARTED"})
    now_us = int(time() * 1_000_000)
    heartbeat_us = max(runtime_updated_us, int((latest_event or {}).get("receivedTimeUs", 0) or 0))
    heartbeat_age = max(0.0, (now_us - heartbeat_us) / 1_000_000) if heartbeat_us else None
    started_us = int((latest_started or {}).get("receivedTimeUs", 0) or 0)
    uptime = max(0.0, (now_us - started_us) / 1_000_000) if started_us else None

    stop_reason = ""
    stop_detail = ""
    if latest_health is not None and int(latest_health.get("receivedTimeUs", 0) or 0) >= heartbeat_us:
        payload = latest_health.get("payload") or {}
        stop_reason = str(payload.get("reason") or latest_health.get("eventType") or "runner_stopped")
        stop_detail = str(payload.get("detail") or payload.get("status") or "记录到服务停止事件")
    elif heartbeat_age is None:
        stop_reason = "no_heartbeat"
        stop_detail = "尚未收到 runner 心跳；程序可能从未启动或运行记录不存在"
    elif heartbeat_age > 10:
        stop_reason = "heartbeat_stale"
        stop_detail = f"最后心跳已超过 {heartbeat_age:.1f} 秒；进程可能异常退出、被终止或网络任务未继续运行"

    latest_execution = execution_events[-1] if execution_events else None
    latest_execution_payload = dict((latest_execution or {}).get("payload") or {})
    live_fills = sum(1 for event in execution_events if str(event.get("eventType", "")).upper() == "FILL" and not bool(event.get("paperOnly")))
    paper_fills = sum(1 for event in execution_events if str(event.get("eventType", "")).upper() == "PAPER_FILL" or (bool(event.get("paperOnly")) and str(event.get("eventType", "")).upper() == "FILL"))
    return {
        "window": "最近记录窗口（各流最多 100 条；高频记录暂停时来自 runtime sidecar）",
        "signalEvaluations": counts["SIGNAL_ACCEPTED"] + counts["SIGNAL_REJECTED"],
        "acceptedSignals": counts["SIGNAL_ACCEPTED"],
        "rejectedSignals": counts["SIGNAL_REJECTED"],
        "notSubmittedSignals": counts["SIGNAL_NOT_SUBMITTED"],
        "orderRequests": counts["ORDER_REQUEST"],
        "orderResponses": counts["ORDER_RESPONSE"],
        "orderRejected": counts["ORDER_REJECTED"],
        "fills": counts["FILL"] + counts["PAPER_FILL"],
        "liveFills": live_fills,
        "paperFills": paper_fills,
        "rejectReasons": [
            {"reason": reason, "count": count}
            for reason, count in reject_reasons.most_common(6)
        ],
        "latestExecution": {
            "eventType": str((latest_execution or {}).get("eventType", "")),
            "reason": str(latest_execution_payload.get("reason", "")),
            "marketId": str((latest_execution or {}).get("marketId", "")),
            "receivedTimeUs": int((latest_execution or {}).get("receivedTimeUs", 0) or 0),
        },
        "runner": {
            "lastHeartbeatUs": heartbeat_us,
            "heartbeatAgeSeconds": heartbeat_age,
            "startedAtUs": started_us,
            "uptimeSeconds": uptime,
            "stopReason": stop_reason,
            "stopDetail": stop_detail,
        },
    }


def _payload_value(payload: Mapping[str, Any], *names: str, default: str = "") -> str:
    for name in names:
        value = payload.get(name)
        if value is not None and value != "":
            return str(value)
    return default


def _definition_token_ids(payload: Mapping[str, Any]) -> dict[str, str]:
    """Extract the paired CLOB token ids for UI identity checks.

    Gamma has returned both ``tokens`` (objects) and ``clobTokenIds`` (a JSON
    list) over time.  This is display-only; strategy execution keeps using the
    normalized MarketDefinition from the runner.
    """
    raw = payload.get("tokens") or payload.get("clobTokenIds") or []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return {}
    result: dict[str, str] = {}
    outcomes = payload.get("outcomes") or []
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except (TypeError, ValueError):
            outcomes = []
    if isinstance(raw, list):
        for index, item in enumerate(raw):
            if isinstance(item, Mapping):
                side = str(item.get("outcome", "")).upper()
                token = item.get("token_id", item.get("tokenId", ""))
            else:
                side = str(outcomes[index]).upper() if isinstance(outcomes, list) and index < len(outcomes) else ""
                token = item
            if side in {"UP", "DOWN"} and token not in (None, ""):
                result[side] = str(token)
    return result


def _event_kind(event: Mapping[str, Any]) -> str:
    """Classify execution records without treating market/health events as paper trades."""
    event_type = str(event.get("eventType", "")).upper()
    if "paperOnly" in event:
        return "paper" if bool(event.get("paperOnly")) else "live"
    payload = event.get("payload") or {}
    if event_type.startswith("PAPER_") or bool(payload.get("paper")):
        return "paper"
    return "live"


def _book_vwap(payload: Mapping[str, Any], quantity: Decimal = Decimal("10")) -> str:
    """VWAP for consuming the requested quantity from visible asks."""
    asks = payload.get("asks") or []
    remaining = quantity
    notional = Decimal("0")
    for level in asks:
        try:
            if isinstance(level, Mapping):
                price = Decimal(str(level.get("price", "0")))
                available = Decimal(str(level.get("size", "0")))
            else:
                price = Decimal(str(level[0]))
                available = Decimal(str(level[1]))
        except (InvalidOperation, TypeError, ValueError, IndexError):
            continue
        if price <= 0 or price > Decimal("0.85") or available <= 0:
            continue
        take = min(remaining, available)
        notional += take * price
        remaining -= take
        if remaining <= 0:
            return format((notional / quantity).quantize(Decimal("0.0001")), "f")
    return ""


def _best_ask_size(payload: Mapping[str, Any]) -> str:
    """Available quantity at the current best ask, for the live quote card."""
    asks = payload.get("asks") or []
    if not asks:
        return ""
    level = asks[0]
    try:
        value = level.get("size", "") if isinstance(level, Mapping) else level[1]
    except (IndexError, TypeError, KeyError):
        return ""
    return str(value) if value not in (None, "") else ""


def _performance_summary(root: Path, *, market_id: str, best_bid: str) -> dict[str, dict[str, Any]]:
    summaries: dict[str, dict[str, Any]] = {
        kind: {
            "realizedPnl": Decimal("0"),
            "wins": 0,
            "losses": 0,
            "flat": 0,
            "settledCount": 0,
            "settledCost": Decimal("0"),
            "returnRate": None,
            "bySide": {
                side: {
                    "realizedPnl": Decimal("0"),
                    "wins": 0,
                    "losses": 0,
                    "flat": 0,
                    "settledCount": 0,
                    "settledCost": Decimal("0"),
                    "winRate": None,
                    "returnRate": None,
                }
                for side in ("UP", "DOWN")
            },
            "openPositions": 0,
            "unrealizedPnl": None,
            "valuationStatus": "none",
        }
        for kind in ("paper", "live")
    }
    settlements = list(iter_partitioned_events(root, "settlements")) if (root / "settlements").exists() else []
    fill_events = list(iter_partitioned_events(root, "fills")) if (root / "fills").exists() else []
    cost_by_market_kind: dict[tuple[str, str], Decimal] = {}
    for fill in fill_events:
        market = str(fill.get("marketId", ""))
        if not market:
            continue
        kind = _event_kind(fill)
        payload = fill.get("payload") or {}
        try:
            if kind == "paper":
                cost = Decimal(str(payload.get("expectedCost", "0")))
            else:
                quantity = Decimal(str(payload.get("size", payload.get("quantity", payload.get("matched_size", "0"))) or "0"))
                price = Decimal(str(payload.get("price", "0") or "0"))
                fee = Decimal(str(payload.get("fee", payload.get("fee_amount", payload.get("feeAmount", "0"))) or "0"))
                cost = quantity * price + fee
        except (InvalidOperation, TypeError, ValueError):
            continue
        if cost > 0:
            cost_by_market_kind[(market, kind)] = cost_by_market_kind.get((market, kind), Decimal("0")) + cost
    settled_markets = {
        (str(event.get("marketId", "")), _event_kind(event))
        for event in settlements
        if event.get("marketId")
    }
    for settlement in settlements:
        payload = settlement.get("payload") or {}
        if payload.get("formal", True) is False:
            continue
        try:
            pnl = Decimal(str(payload.get("netPnl", payload.get("pnl", "0"))))
        except (InvalidOperation, TypeError, ValueError):
            continue
        kind = _event_kind(settlement)
        summary = summaries[kind]
        side = str(payload.get("side", settlement.get("outcome", ""))).upper()
        side_summary = summary["bySide"].get(side)
        summary["realizedPnl"] += pnl
        try:
            settlement_cost = Decimal(str(payload.get("actualCost", payload.get("cost", "0")) or "0"))
        except (InvalidOperation, TypeError, ValueError):
            settlement_cost = Decimal("0")
        if settlement_cost <= 0:
            settlement_cost = cost_by_market_kind.get((str(settlement.get("marketId", "")), kind), Decimal("0"))
        summary["settledCost"] += settlement_cost
        summary["settledCount"] += 1
        if side_summary is not None:
            side_summary["realizedPnl"] += pnl
            side_summary["settledCost"] += settlement_cost
            side_summary["settledCount"] += 1
        if pnl > 0:
            summary["wins"] += 1
            if side_summary is not None:
                side_summary["wins"] += 1
        elif pnl < 0:
            summary["losses"] += 1
            if side_summary is not None:
                side_summary["losses"] += 1
        else:
            summary["flat"] += 1
            if side_summary is not None:
                side_summary["flat"] += 1

    positions: dict[str, dict[str, dict[str, Decimal]]] = {"paper": {}, "live": {}}
    seen_live_fills: set[tuple[str, str]] = set()
    fills = fill_events
    for fill in fills:
        fill_market = str(fill.get("marketId", ""))
        paper = _event_kind(fill) == "paper"
        kind = "paper" if paper else "live"
        if not fill_market or (fill_market, kind) in settled_markets:
            continue
        payload = fill.get("payload") or {}
        try:
            if paper:
                quantity = Decimal(str(payload.get("quantity", "0")))
                cost = Decimal(str(payload.get("expectedCost", "0")))
            else:
                trade_id = str(payload.get("trade_id", payload.get("tradeId", "")) or fill.get("eventId", ""))
                dedupe_key = (fill_market, trade_id)
                if dedupe_key in seen_live_fills:
                    continue
                seen_live_fills.add(dedupe_key)
                quantity = Decimal(str(payload.get("size", payload.get("quantity", payload.get("matched_size", "0"))) or "0"))
                price = Decimal(str(payload.get("price", "0") or "0"))
                fee = Decimal(str(payload.get("fee", payload.get("fee_amount", payload.get("feeAmount", "0"))) or "0"))
                cost = quantity * price + fee
        except (InvalidOperation, TypeError, ValueError):
            continue
        if quantity <= 0 or cost <= 0:
            continue
        position = positions[kind].setdefault(fill_market, {"quantity": Decimal("0"), "cost": Decimal("0")})
        position["quantity"] += quantity
        position["cost"] += cost

    try:
        bid = Decimal(str(best_bid)) if best_bid not in (None, "") else None
    except (InvalidOperation, TypeError, ValueError):
        bid = None
    for kind, open_positions in positions.items():
        summary = summaries[kind]
        summary["openPositions"] = len(open_positions)
        if not open_positions:
            summary["valuationStatus"] = "none"
        elif market_id in open_positions and bid is not None and bid > 0:
            position = open_positions[market_id]
            summary["unrealizedPnl"] = position["quantity"] * bid - position["cost"]
            summary["valuationStatus"] = "priced"
        else:
            summary["valuationStatus"] = "awaiting_market"
        settled_count = int(summary["settledCount"])
        summary["winRate"] = (
            format((Decimal(int(summary["wins"])) * Decimal("100") / Decimal(settled_count)).quantize(Decimal("0.1")), "f")
            if settled_count
            else None
        )
        for side_summary in summary["bySide"].values():
            side_count = int(side_summary["settledCount"])
            side_summary["winRate"] = (
                format((Decimal(int(side_summary["wins"])) * Decimal("100") / Decimal(side_count)).quantize(Decimal("0.1")), "f")
                if side_count
                else None
            )
            side_pnl = side_summary["realizedPnl"]
            side_summary["realizedPnl"] = format(side_pnl, "f")
            side_summary["returnRate"] = (
                format((side_pnl / side_summary["settledCost"] * Decimal("100")).quantize(Decimal("0.1")), "f")
                if side_summary["settledCost"] > 0
                else None
            )
            side_summary["settledCost"] = format(side_summary["settledCost"], "f")
        realized_pnl = summary["realizedPnl"]
        summary["realizedPnl"] = format(realized_pnl, "f")
        if summary["settledCost"] > 0:
            summary["returnRate"] = format((realized_pnl / summary["settledCost"] * Decimal("100")).quantize(Decimal("0.1")), "f")
        else:
            summary["returnRate"] = None
        # Keep the API JSON-serializable while exposing the denominator used
        # by returnRate for an auditable dashboard statistic.
        summary["settledCost"] = format(summary["settledCost"], "f")
        if summary["unrealizedPnl"] is not None:
            summary["unrealizedPnl"] = format(summary["unrealizedPnl"], "f")
    return summaries


def build_state(root: Path) -> dict[str, Any]:
    # Raw is intentionally not parsed on every dashboard refresh: it can be
    # the largest stream and is for replay, not UI counters.
    stream_events: dict[str, list[dict[str, Any]]] = {
        stream: _read_events(root, stream, 100) for stream in COUNT_STREAMS
    }
    # The runner maintains a tiny latest-state sidecar even when historical
    # recording is paused.  Merge it only for the dashboard view; it is not a
    # replay stream and does not affect persisted counters.
    runtime_state = load_runtime_state(root)
    for runtime_event in runtime_state.get("events", []):
        stream = str(runtime_event.get("_stream", "events"))
        if stream not in stream_events:
            continue
        event = dict(runtime_event)
        event.pop("_stream", None)
        existing_ids = {str(item.get("eventId", "")) for item in stream_events[stream]}
        if str(event.get("eventId", "")) not in existing_ids:
            stream_events[stream].append(event)
            stream_events[stream] = sorted(stream_events[stream], key=lambda item: int(item.get("receivedTimeUs", 0) or 0))[-100:]
    stream_events["raw"] = []
    all_events = [event for events in stream_events.values() for event in events]
    all_events.sort(key=lambda event: int(event.get("receivedTimeUs", 0)))
    execution_diagnostics = _execution_diagnostics(root, stream_events, runtime_state)
    latest = all_events[-1] if all_events else None
    session = stream_events["sessions"][-1] if stream_events["sessions"] else None
    mode = str((session or {}).get("liveMode", "observe"))
    health_events = stream_events["health"]
    latest_health = health_events[-1] if health_events else None
    counts = {stream: _stream_count(root, stream) if (root / stream).exists() else 0 for stream in COUNT_STREAMS}
    counts_by_kind = {
        kind: {"fills": 0, "settlements": 0}
        for kind in ("paper", "live")
    }
    # Critical streams are durable and normally small. Count their execution
    # kind from the envelope so the UI never presents a blended total as real.
    for stream in ("fills", "settlements"):
        if not (root / stream).exists():
            continue
        for event in iter_partitioned_events(root, stream):
            counts_by_kind[_event_kind(event)][stream] += 1
    counts["raw"] = 0
    usage = data_usage(root)
    ui_settings = SettingsStore(root).load()
    budget = str(effective_order_budget(ui_settings))
    min_quantity = str(ui_settings.get("activeTradeMinQuantity", "10"))
    active_multiplier = str(ui_settings.get("activeTradeMultiplier", "1.5"))
    daily_loss_count = effective_daily_loss_count(ui_settings)
    daily_loss_total = effective_daily_loss_total(ui_settings)
    strategy_side = str(ui_settings.get("strategySide", "DOWN")).upper()
    market_snapshot = _latest_event(stream_events["events"], {"MARKET_SNAPSHOT_1S", "ORDERBOOK_SNAPSHOT"}, venue="POLYMARKET")
    market_id = str((market_snapshot or {}).get("marketId", ""))
    trade_events = [
        event for event in stream_events["events"]
        if str(event.get("eventType", "")) == "TRADE"
        and str(event.get("venue", "")).upper() == "POLYMARKET"
        and (not market_id or str(event.get("marketId", "")) == market_id)
    ]
    market_trade = max(trade_events, key=lambda event: int(event.get("receivedTimeUs", 0) or 0), default=None)
    if not market_id:
        market_id = str((market_trade or {}).get("marketId", ""))
    market_definition = _latest_event(stream_events["events"], {"MARKET_DEFINITION"}, venue="POLYMARKET")
    # Do not combine a current orderbook with a definition from a previous
    # 5-minute market.  A missing matching definition is safer than showing a
    # plausible but incorrect title/slug in the live card.
    if market_definition and market_id and str(market_definition.get("marketId", "")) != market_id:
        market_definition = None
    side_snapshots = {
        side: _latest_event(
            [
                event for event in stream_events["events"]
                if str(event.get("outcome", "")).upper() == side
                and (not market_id or str(event.get("marketId", "")) == market_id)
            ],
            {"MARKET_SNAPSHOT_1S", "ORDERBOOK_SNAPSHOT"},
            venue="POLYMARKET",
        )
        for side in ("UP", "DOWN")
    }
    side_trades = {
        side: _latest_event(
            [
                event for event in stream_events["events"]
                if str(event.get("outcome", "")).upper() == side
                and (not market_id or str(event.get("marketId", "")) == market_id)
            ],
            {"TRADE"},
            venue="POLYMARKET",
        )
        for side in ("UP", "DOWN")
    }
    snapshot_payload = dict((market_snapshot or {}).get("payload") or {})
    trade_payload = dict((market_trade or {}).get("payload") or {})
    definition_payload = dict((market_definition or {}).get("payload") or {})
    if not market_id:
        market_id = str((market_definition or {}).get("marketId", ""))
    market = {
        "connected": market_snapshot is not None or market_trade is not None,
        "feed": "公开 Market WebSocket · 被动推送（只读）",
        "pageRefreshSeconds": 2.5,
        "clobTransport": "disabled",
        "marketId": market_id,
        "conditionId": _payload_value(definition_payload, "conditionId", "condition_id"),
        "tokenIds": _definition_token_ids(definition_payload),
        "question": _payload_value(definition_payload, "question", "title"),
        "slug": _payload_value(definition_payload, "slug"),
        "outcome": str((market_snapshot or market_trade or {}).get("outcome", "DOWN")),
        "downPrice": _payload_value(snapshot_payload, "downPrice", "down_price_now"),
        "bestAsk": _payload_value(snapshot_payload, "bestAsk", "down_price_now"),
        "bestBid": _payload_value(snapshot_payload, "bestBid"),
        "lastTradePrice": _payload_value(trade_payload, "price"),
        "lastTradeQuantity": _payload_value(trade_payload, "quantity"),
        "lastTradeSide": _payload_value(trade_payload, "aggressor", "side"),
        "lastTradeOutcome": str((market_trade or {}).get("outcome", "")),
        "remainingSeconds": _payload_value(snapshot_payload, "remainingSeconds", "remaining_seconds"),
        "binanceSpotReturn5m": _payload_value(snapshot_payload, "binanceSpotReturn5m"),
        "updatedAtUs": int((market_snapshot or market_trade or {}).get("receivedTimeUs", 0) or 0),
    }
    market["quotes"] = {}
    for side in ("UP", "DOWN"):
        snapshot = side_snapshots[side]
        snapshot_payload_side = dict((snapshot or {}).get("payload") or {})
        trade = side_trades[side]
        trade_payload_side = dict((trade or {}).get("payload") or {})
        market["quotes"][side] = {
            "bestBid": _payload_value(snapshot_payload_side, "bestBid"),
            "bestAsk": _payload_value(snapshot_payload_side, "bestAsk", "down_price_now"),
            "lastTradePrice": _payload_value(trade_payload_side, "price"),
            "lastTradeQuantity": _payload_value(trade_payload_side, "quantity"),
            "lastTradeAggressor": _payload_value(trade_payload_side, "aggressor", "side"),
            "vwap10": _book_vwap(snapshot_payload_side),
            "bestAskSize": _best_ask_size(snapshot_payload_side),
            "updatedAtUs": int((snapshot or trade or {}).get("receivedTimeUs", 0) or 0),
        }
    latest_signal = _latest_event(
        stream_events["signals"],
        {"SIGNAL_ACCEPTED", "SIGNAL_REJECTED", "SIGNAL_NOT_SUBMITTED", "SIGNAL_PAPER_EXECUTED"},
    )
    latest_signal_payload = dict((latest_signal or {}).get("payload") or {})
    strategy_decision = {
        "eventType": str((latest_signal or {}).get("eventType", "")),
        "marketId": str((latest_signal or {}).get("marketId", "")),
        "outcome": str((latest_signal or {}).get("outcome", "")),
        "reason": str(latest_signal_payload.get("reason", "")),
        "receivedTimeUs": int((latest_signal or {}).get("receivedTimeUs", 0) or 0),
        "paperOnly": bool((latest_signal or {}).get("paperOnly")),
    }
    performance = _performance_summary(root, market_id=market_id, best_bid=market["bestBid"])
    settlement_stats = {
        "wins": performance["live"]["wins"],
        "losses": performance["live"]["losses"],
        "paperWins": performance["paper"]["wins"],
        "paperLosses": performance["paper"]["losses"],
    }
    reference_prices: dict[str, dict[str, Any]] = {}
    for event in stream_events["events"]:
        if str(event.get("eventType", "")) not in {"REFERENCE_PRICE", "RTDS_REFERENCE_PRICE"}:
            continue
        payload = dict(event.get("payload") or {})
        source = str(payload.get("source") or "").strip()
        if not source or payload.get("value") in (None, ""):
            continue
        value = dict(payload)
        value["value"] = str(payload["value"])
        value["updatedAtUs"] = int(event.get("receivedTimeUs", 0) or 0)
        reference_prices[source] = value
    rtds_health = _latest_event(stream_events["health"], {"CONNECTION_HEALTH", "RTDS_HEALTH"}, venue="POLYMARKET_RTDS")
    rtds_payload = dict((rtds_health or {}).get("payload") or {})
    now_us = int(time() * 1_000_000)
    market_age_seconds = (
        max(0.0, (now_us - int(market["updatedAtUs"])) / 1_000_000)
        if int(market["updatedAtUs"] or 0) > 0
        else None
    )
    latest_binance = _latest_event(stream_events["events"], {"REFERENCE_PRICE"}, venue="BINANCE")
    binance_received_us = int((latest_binance or {}).get("receivedTimeUs", 0) or 0)
    binance_age_seconds = max(0.0, (now_us - binance_received_us) / 1_000_000) if binance_received_us else None
    # The strategy's hard data source is Polymarket Market WS.  Binance is
    # optional reference telemetry and must not make an otherwise healthy
    # market feed appear disconnected.
    feeds_fresh = market_age_seconds is not None and market_age_seconds <= 3.0
    market["fresh"] = market_age_seconds is not None and market_age_seconds <= 3.0
    market["ageSeconds"] = market_age_seconds
    market["realtime"] = market["fresh"]
    market["historical"] = bool(market["updatedAtUs"]) and not market["fresh"]
    overview = {
        "modeLabel": "只读观察" if mode == "observe" else f"{mode.upper()} 行情 · 订单关闭",
        "serviceLabel": ("行情正常 · 订单关闭" if feeds_fresh else ("运行中 · 订单关闭" if latest_health else "等待启动")),
        "orderGate": "已关闭（observe）" if mode == "observe" else "已关闭（CLOB transport disabled）",
        "marketFeed": "已接收" if market["connected"] else "等待 Market WS",
        "referenceFeed": str(rtds_payload.get("state") or "RTDS 默认关闭"),
    }
    return {
        "service": {
            "status": "observe_only" if mode == "observe" else "transport_disabled",
            "mode": mode,
            "ordersDisabled": True,
            "lastEventType": latest.get("eventType") if latest else None,
            "lastEventAtUs": latest.get("receivedTimeUs") if latest else None,
            "health": "connected" if feeds_fresh else (latest_health.get("payload", {}).get("status") if latest_health else "starting"),
        },
        "mode": mode,
        "strategy": {
            "name": f"BTC 5m {strategy_side}",
            "window": "剩余 90–30 秒",
            "vwapBand": "0.80–0.85",
            "priceCap": "≤ 0.85",
            "budget": f"≤ {budget}U（含手续费）",
            "minFill": "≥ 2.5U",
            "trigger": f"主动 {strategy_side} BUY ≥ max({min_quantity}份, 计划买入×{active_multiplier})",
            "momentum": "入场前5秒同方向上涨≥0.01",
        },
        "counts": counts,
        "countsByKind": counts_by_kind,
        "settlementStats": settlement_stats,
        "performance": performance,
        "market": market,
        "strategyDecision": strategy_decision,
        "executionDiagnostics": execution_diagnostics,
        "referencePrices": reference_prices,
        "overview": overview,
        "connections": {
            "polymarketMarket": "public Gamma + Market WebSocket (read-only)",
            "binanceSpot": "public BTCUSDT 1m WebSocket (read-only)",
            "polymarketRtds": "Polymarket RTDS · Chainlink/Binance reference · read-only / disabled by default",
            "polymarketClob": "optional order/identity API · disabled by default",
        },
        "safety": {
            "ordersDisabled": True,
            "transport": "DISABLED",
            "dailyLossRule": f"{daily_loss_count} 笔亏损或累计 {daily_loss_total}U 即停",
            "rawDropped": usage.get("rawDropped", 0),
        },
        "usage": usage,
        "recentEvents": all_events[-40:],
    }


class DashboardServer:
    def __init__(
        self,
        data_dir: Path,
        host: str = "127.0.0.1",
        port: int = 8766,
        *,
        settings_store: SettingsStore | None = None,
        keychain_store: KeychainSecretStore | None = None,
        account_detector: PolygonAccountDetector | None = None,
        identity_manager: CLOBIdentityManager | None = None,
        runner_popen: Any | None = None,
        sdk_available: Callable[[], bool] | None = None,
        feed_health_provider: Callable[[], Mapping[str, Any]] | None = None,
        bootstrap_timeout_seconds: float = 20.0,
        bootstrap_poll_seconds: float = 0.25,
    ) -> None:
        self.data_dir = data_dir
        self.host = host
        self.port = port
        self.settings_store = settings_store or SettingsStore(data_dir)
        self.keychain_store = keychain_store or KeychainSecretStore()
        self.account_detector = account_detector or PolygonAccountDetector()
        self.identity_manager = identity_manager or CLOBIdentityManager(
            private_key_store=self.keychain_store,
            credential_store=KeychainSecretStore(account="polymarket-clob-credentials"),
        )
        self.last_account_detection: dict[str, Any] | None = None
        self.runner_popen = runner_popen or subprocess.Popen
        self.sdk_available = sdk_available or (
            lambda: importlib.util.find_spec("py_clob_client_v2") is not None
        )
        self.feed_health_provider = feed_health_provider or self._feed_health
        self.bootstrap_timeout_seconds = max(float(bootstrap_timeout_seconds), 0.0)
        self.bootstrap_poll_seconds = max(float(bootstrap_poll_seconds), 0.0)
        self.runner_process: Any | None = None
        self.runner_mode: str | None = None
        self.runner_submission_ready = False
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def _feed_health(self) -> dict[str, Any]:
        """Read the runner's latest-state sidecar without scanning partitions."""
        events = list(load_runtime_state(self.data_dir).get("events", []))
        now_us = int(time() * 1_000_000)

        def latest_age(venue: str, event_types: set[str]) -> float | None:
            timestamps = [
                int(event.get("receivedTimeUs", 0) or 0)
                for event in events
                if str(event.get("venue", "")).upper() == venue
                and str(event.get("eventType", "")) in event_types
            ]
            if not timestamps:
                return None
            return max(0.0, (now_us - max(timestamps)) / 1_000_000)

        market_age = latest_age(
            "POLYMARKET",
            {"ORDERBOOK_SNAPSHOT", "MARKET_SNAPSHOT_1S", "TRADE", "BEST_BID_ASK"},
        )
        binance_age = latest_age("BINANCE", {"REFERENCE_PRICE"})
        # Strategy book freshness is two seconds.  Preflight allows a small
        # scheduling margin, while the runner rechecks the strict threshold
        # immediately before every order.
        return {
            "marketFresh": market_age is not None and market_age <= 3.0,
            "binanceFresh": binance_age is not None and binance_age <= 75.0,
            "marketAgeSeconds": market_age,
            "binanceAgeSeconds": binance_age,
        }

    def start(self) -> None:
        data_dir = self.data_dir
        static_dir = STATIC_DIR
        dashboard = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path == "/api/state":
                    state = build_state(data_dir)
                    runner = dashboard.runner_state()
                    control = dashboard.control_state()
                    merge_runtime_control_state(state, runner, control)
                    state.setdefault("market", {})["realtime"] = (
                        runner.get("status") == "running"
                        and bool(state.get("market", {}).get("fresh"))
                    )
                    # Persisted health/events remain useful history, but must
                    # not make a stopped process look like a live feed.
                    if runner.get("status") != "running":
                        state["market"]["historical"] = bool(state["market"].get("updatedAtUs"))
                    self._send_json(state)
                    return
                if parsed.path == "/api/health":
                    state = build_state(data_dir)
                    self._send_json(state["service"])
                    return
                if parsed.path == "/api/settings":
                    self._send_json(dashboard.settings_payload())
                    return
                if parsed.path == "/api/preflight":
                    self._send_json(dashboard.preflight_state())
                    return
                if parsed.path == "/api/runner":
                    self._send_json(dashboard.runner_state())
                    return
                if parsed.path == "/api/control":
                    self._send_json(dashboard.control_state())
                    return
                if parsed.path == "/api/orders/recovery":
                    self._send_json(dashboard.recovery_orders())
                    return
                if parsed.path in {"/", "/index.html"}:
                    self._send_file(static_dir / "index.html", "text/html; charset=utf-8")
                    return
                if parsed.path == "/styles.css":
                    self._send_file(static_dir / "styles.css", "text/css; charset=utf-8")
                    return
                if parsed.path == "/app.js":
                    self._send_file(static_dir / "app.js", "text/javascript; charset=utf-8")
                    return
                self.send_error(404)

            def do_POST(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path not in {"/api/settings", "/api/account/detect", "/api/account/configure", "/api/account/validate", "/api/control", "/api/runner/start", "/api/runner/stop", "/api/orders/reconcile-no-remote"}:
                    self.send_error(404)
                    return
                if parsed.path == "/api/runner/start":
                    try:
                        self._send_json(dashboard.start_runner())
                    except RuntimeError as exc:
                        self._send_json({"error": str(exc)}, status=409)
                    return
                if parsed.path == "/api/runner/stop":
                    self._send_json(dashboard.stop_runner())
                    return
                if parsed.path not in {"/api/settings", "/api/account/detect", "/api/account/configure", "/api/account/validate", "/api/control", "/api/orders/reconcile-no-remote"}:
                    self.send_error(404)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > 64 * 1024:
                        raise ValueError("请求体大小无效")
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    if not isinstance(payload, dict):
                        raise ValueError("请求体必须是 JSON 对象")
                    if parsed.path == "/api/settings":
                        self._send_json(dashboard.save_settings(payload))
                        return
                    if parsed.path == "/api/control":
                        self._send_json(dashboard.set_control(payload))
                        return
                    if parsed.path == "/api/orders/reconcile-no-remote":
                        self._send_json(dashboard.reconcile_no_remote_order(payload))
                        return
                    if parsed.path == "/api/account/configure":
                        self._send_json(dashboard.configure_account(payload))
                        return
                    if parsed.path == "/api/account/validate":
                        self._send_json(dashboard.validate_account(payload))
                        return
                    self._send_json(dashboard.detect_account(payload))
                except (SettingsValidationError, ValueError, json.JSONDecodeError) as exc:
                    self._send_json({"error": str(exc)}, status=400)
                except RuntimeError as exc:
                    if parsed.path in {"/api/account/configure", "/api/account/validate"}:
                        self._send_json({"error": _safe_identity_runtime_error(exc)}, status=409)
                    elif parsed.path == "/api/orders/reconcile-no-remote":
                        self._send_json({"error": str(exc)}, status=409)
                    else:
                        self._send_json({"error": f"设置操作失败：{type(exc).__name__}"}, status=500)
                except Exception as exc:  # noqa: BLE001 - never expose secrets/provider details.
                    self._send_json({"error": f"设置操作失败：{type(exc).__name__}"}, status=500)

            def _send_json(self, payload: Any, status: int = 200) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _send_file(self, path: Path, content_type: str) -> None:
                if not path.exists():
                    self.send_error(404)
                    return
                body = path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: Any) -> None:
                return

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, name="live-dashboard", daemon=True)
        self._thread.start()

    def settings_payload(self) -> dict[str, Any]:
        result = self.settings_store.public(private_key_configured=bool(self.keychain_store.get()))
        result["clobIdentityConfigured"] = self.identity_manager.is_configured()
        result["clobIdentitySummary"] = self.identity_manager.summary()
        return result

    def recovery_orders(self) -> dict[str, Any]:
        """Return sanitized local order state for operator recovery."""
        records = SQLiteOrderStore(self.data_dir / "orders.sqlite3").list_all()
        timestamps: dict[str, int] = {}
        for event in iter_partitioned_events(self.data_dir, "orders"):
            client_id = str((event.get("payload") or {}).get("clientOrderId", ""))
            if client_id:
                timestamps[client_id] = max(timestamps.get(client_id, 0), int(event.get("receivedTimeUs", 0) or 0))
        pending_states = {OrderState.INTENT, OrderState.SUBMITTING, OrderState.UNKNOWN}
        unresolved_states = pending_states | {OrderState.ACKNOWLEDGED, OrderState.PARTIAL, OrderState.FILLED}
        orders = []
        for record in reversed(records[-50:]):
            payload = dict(record.payload or {})
            orders.append({
                "clientOrderId": record.client_order_id,
                "remoteOrderId": record.remote_order_id or "",
                "marketId": record.market_id,
                "side": str(payload.get("side", "")),
                "quantity": str(payload.get("quantity", "")),
                "expectedCost": str(payload.get("expected_cost", payload.get("expectedCost", ""))),
                "state": record.state.value,
                "updatedAtUs": timestamps.get(record.client_order_id, 0),
                "needsAttention": record.state in unresolved_states,
                "canConfirmNoRemote": record.state in pending_states and not record.remote_order_id,
                "reconciliation": payload.get("reconciliation") or {},
            })
        return {"unresolvedCount": sum(1 for item in orders if item["needsAttention"]), "orders": orders}

    def reconcile_no_remote_order(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Close a local pending request only after explicit operator confirmation."""
        settings = self.settings_store.load()
        # A stopped runner cannot submit.  Allow recovery in that state even
        # if an old UI setting still says enabled; this removes the confusing
        # dead-end where the operator cannot clear a stale UNKNOWN order after
        # a restart.  A running process still requires pausing new orders.
        if bool(settings.get("orderSubmissionEnabled", True)) and self.runner_state().get("status") == "running":
            raise RuntimeError("请先暂停新订单，再执行手动对账")
        client_id = str(payload.get("clientOrderId", "")).strip()
        confirmation = str(payload.get("confirmation", "")).strip()
        if not client_id or confirmation != client_id or payload.get("officialNoRecordConfirmed") is not True:
            raise ValueError("必须确认官方无订单，并完整输入本地 clientOrderId")
        store = SQLiteOrderStore(self.data_dir / "orders.sqlite3")
        try:
            record = store.get(client_id)
        except KeyError:
            raise ValueError("找不到该本地订单") from None
        if record.remote_order_id:
            raise RuntimeError("该记录已有官方订单号，不能按无远端订单关闭")
        machine = OrderStateMachine(store, DisabledOrderTransport())
        try:
            closed = machine.reconcile_no_remote_record(
                client_id,
                note="operator confirmed official CLOB has no matching order via dashboard",
            )
        except Exception as exc:
            raise RuntimeError(f"当前状态不能按无远端订单关闭：{type(exc).__name__}") from None
        recorder = MonitoringRecorder(self.data_dir, live_mode=str(settings.get("liveMode", "canary")))
        recorder.enabled = bool(settings.get("dataRecordingEnabled", True))
        try:
            recorder.record(
                "orders",
                "ORDER_RECONCILED_NO_REMOTE",
                venue="POLYMARKET",
                market_id=closed.market_id,
                outcome=str(closed.payload.get("side", "")) or None,
                payload={"clientOrderId": client_id, "reason": "official_no_record", "state": "canceled", "operatorConfirmed": True},
            )
        finally:
            recorder.close()
        return {"status": "canceled", "clientOrderId": client_id, "message": "本地待定状态已关闭；审计记录已保存"}

    def control_state(self) -> dict[str, Any]:
        settings = self.settings_store.load()
        desired_mode = str(settings.get("liveMode", "observe"))
        runner = self.runner_state()
        mode = str(runner.get("mode") or desired_mode)
        recording = bool(settings.get("dataRecordingEnabled", True))
        submission = bool(settings.get("orderSubmissionEnabled", True))
        freshness_verified = bool(self.runner_submission_ready or runner.get("freshnessVerified"))
        return {
            "mode": mode,
            "desiredMode": desired_mode,
            "dataRecordingEnabled": recording,
            "orderSubmissionEnabled": submission,
            "effectiveOrderSubmissionEnabled": runner["status"] == "running" and mode != "observe" and freshness_verified and submission,
            "freshnessVerified": freshness_verified,
            "runner": runner,
        }

    def set_control(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for key in ("dataRecordingEnabled", "orderSubmissionEnabled"):
            if key in payload:
                value = payload[key]
                if not isinstance(value, bool):
                    raise ValueError(f"{key} must be boolean")
                if key == "orderSubmissionEnabled" and value:
                    runner = self.runner_state()
                    if runner.get("status") != "running":
                        raise ValueError("runner 未运行，请先启动当前模式")
                    if runner.get("mode") == "observe":
                        raise ValueError("observe 模式不会提交真实订单")
                    if not bool(self.control_state().get("freshnessVerified")):
                        raise ValueError("行情启动预检尚未完成，不能开放新订单")
                values[key] = value
        if not values:
            raise ValueError("至少提供一个运行控制项")
        self.settings_store.save(values)
        return self.control_state()

    def save_settings(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        values = dict(payload)
        secret = values.pop("privateKey", None)
        previous = self.settings_store.load()
        if "walletAddress" in values and str(values.get("walletAddress", "")) != str(previous.get("walletAddress", "")):
            # A detected account belongs to a specific address; never carry it
            # over when the operator edits the wallet field.
            values["accountType"] = ""
            values["accountDetectionStatus"] = ""
        saved = self.settings_store.save(values)
        if secret is not None:
            self.keychain_store.set(str(secret))
        saved["privateKeyConfigured"] = bool(self.keychain_store.get())
        return saved

    def detect_account(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Validate the manually selected identity locally; no Polygon RPC."""
        current = self.settings_store.load()
        address = str(payload.get("walletAddress", current.get("walletAddress", "")))
        try:
            signature_type = int(payload.get("signatureType", current.get("signatureType", 0)))
        except (TypeError, ValueError):
            raise ValueError("signatureType 必须是 0、1、2 或 3") from None
        funder = str(payload.get("funder", current.get("funder", ""))).strip() or (address if signature_type == 0 else "")
        if not ADDRESS_RE.fullmatch(address):
            return {"status": "INVALID", "address": address, "accountType": "INVALID", "codePresent": False, "signatureTypeSuggestion": None, "funderSuggestion": "", "error": "请填写有效的 Polymarket Funder/钱包公开地址"}
        if signature_type not in {0, 1, 2, 3}:
            return {"status": "INVALID", "address": address, "accountType": "INVALID", "codePresent": False, "signatureTypeSuggestion": None, "funderSuggestion": "", "error": "signature_type 必须是 0、1、2 或 3"}
        if not ADDRESS_RE.fullmatch(funder):
            return {"status": "INVALID", "address": address, "accountType": "INVALID", "codePresent": False, "signatureTypeSuggestion": signature_type, "funderSuggestion": "", "error": "当前签名类型需要填写有效的 funder 地址"}
        if signature_type == 0 and funder.lower() != address.lower():
            return {"status": "INVALID", "address": address, "accountType": "EOA", "codePresent": False, "signatureTypeSuggestion": 0, "funderSuggestion": funder, "error": "钱包登录的 EOA 模式要求钱包地址与 funder 完全一致"}
        result = {"status": "OK", "address": address, "accountType": account_type_for_signature(signature_type), "codePresent": False, "signatureTypeSuggestion": signature_type, "funderSuggestion": funder, "error": "本地身份配置格式通过；点击配置并派生身份以访问 CLOB"}
        self.last_account_detection = result
        self.settings_store.save({"walletAddress": address, "accountType": result["accountType"], "accountDetectionStatus": result["status"], "signatureType": signature_type, "funder": funder})
        return result

    def configure_account(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        current = self.settings_store.load()
        address = str(payload.get("walletAddress", current.get("walletAddress", ""))).strip()
        funder = str(payload.get("funder", current.get("funder", ""))).strip() or address
        signature_type = int(payload.get("signatureType", current.get("signatureType", 0)))
        result = self.identity_manager.configure(
            signature_type=signature_type,
            funder_address=funder,
            signer_private_key=str(payload.get("privateKey", "")),
            proxy=str(payload.get("proxy", current.get("proxy", ""))),
        )
        self.last_account_detection = {
            "status": result.get("status", "OK"),
            "address": address or funder,
            "accountType": result.get("accountType", account_type_for_signature(signature_type)),
            "signatureTypeSuggestion": signature_type,
            "funderSuggestion": funder,
            "error": "",
        }
        self.settings_store.save({
            "walletAddress": address or funder,
            "funder": funder,
            "signatureType": signature_type,
            "accountType": result.get("accountType", account_type_for_signature(signature_type)),
            "accountDetectionStatus": "OK",
        })
        result["privateKeyConfigured"] = bool(self.keychain_store.get())
        return result

    def validate_account(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        current = self.settings_store.load()
        return self.identity_manager.validate(proxy=str(payload.get("proxy", current.get("proxy", ""))))

    def runner_state(self) -> dict[str, Any]:
        if self.runner_process is None:
            # A runner may be launched by ops/systemd instead of this UI
            # process.  Use the runner's heartbeat sidecar to avoid showing a
            # live feed as STOPPED (and to prevent a second instance from
            # being started).  The short TTL deliberately fails closed when
            # the sidecar stops changing.
            runtime_path = self.data_dir / "runtime_state.json"
            try:
                age = max(0.0, time() - runtime_path.stat().st_mtime)
                runtime = load_runtime_state(self.data_dir)
                events = runtime.get("events", [])
                latest = events[-1] if events else {}
                mode = str(latest.get("liveMode") or self.settings_store.load().get("liveMode", "observe"))
                if age <= 10.0 and latest:
                    return {"status": "running", "pid": None, "mode": mode, "freshnessVerified": age <= 3.0, "external": True}
            except (OSError, TypeError, ValueError):
                pass
            return {"status": "stopped", "pid": None, "mode": None}
        returncode = self.runner_process.poll()
        if returncode is None:
            return {"status": "running", "pid": getattr(self.runner_process, "pid", None), "mode": self.runner_mode, "freshnessVerified": self.runner_submission_ready}
        return {"status": "exited", "pid": getattr(self.runner_process, "pid", None), "mode": self.runner_mode, "returncode": returncode}

    def start_runner(self) -> dict[str, Any]:
        settings = self.settings_store.load()
        mode = str(settings.get("liveMode", "observe"))
        current = self.runner_state()
        if current["status"] == "running" and current.get("mode") == mode:
            return current
        preflight = self.preflight_state(allow_pending_feeds=mode != "observe")
        if not preflight["safe"]:
            failed = ", ".join(item["label"] for item in preflight["checks"].values() if not item["passed"])
            raise RuntimeError(f"预检未通过：{failed}")
        if current["status"] == "running":
            self.stop_runner()
        restore_submission = bool(settings.get("orderSubmissionEnabled", True))
        bootstrap_feeds = mode in {"canary", "live"}
        if bootstrap_feeds:
            self.settings_store.save({"orderSubmissionEnabled": False})
        # Propagate the UI's runtime gates to the child process.  Without the
        # read-only feed flags the runner's own network gate correctly fails
        # closed, which previously looked like a runner that started and then
        # silently stopped with no Market WS data.
        env = dict(os.environ)
        enabled = mode != "observe"
        env.update({
            "LIVE_MODE": mode,
            "LIVE_TRANSPORT_ENABLED": "1" if enabled else "0",
            "LIVE_TRADING_ENABLE": "1" if enabled else "0",
            "POLYMARKET_ORDER_ENABLED": "1" if enabled else "0",
            "POLYMARKET_PREFLIGHT_APPROVED": "1" if enabled else "0",
            "POLYMARKET_READONLY_NETWORK_ENABLED": "1",
            "BINANCE_READONLY_NETWORK_ENABLED": "1",
            "LIVE_DATA_RECORDING_ENABLED": "1" if bool(settings.get("dataRecordingEnabled", True)) else "0",
            "LIVE_ORDER_SUBMISSION_ENABLED": "0" if bootstrap_feeds else ("1" if restore_submission else "0"),
        })
        if settings.get("proxy"):
            env["LIVE_PROXY"] = str(settings["proxy"])
        if settings.get("funder"):
            env["POLYMARKET_FUNDER"] = str(settings["funder"])
        if settings.get("signatureType") is not None:
            env["POLYMARKET_SIGNATURE_TYPE"] = str(settings["signatureType"])
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1]) + os.pathsep + env.get("PYTHONPATH", "")
        env["ARBITRAGE_LIVE_DASHBOARD_STARTED"] = "1"
        runner_python = os.environ.get("ARBITRAGE_PYTHON_BIN", sys.executable)
        command = [runner_python, "-m", "live_trader.cli", "run", "--data-dir", str(self.data_dir)]
        try:
            self.runner_process = self.runner_popen(
                command,
                cwd=str(Path(__file__).resolve().parents[2]),
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
            )
            self.runner_mode = mode
            self.runner_submission_ready = False
        except Exception:
            self.runner_process = None
            self.runner_mode = None
            self.runner_submission_ready = False
            if bootstrap_feeds:
                self.settings_store.save({"orderSubmissionEnabled": restore_submission})
            raise
        if not bootstrap_feeds:
            return {"status": "running", "pid": getattr(self.runner_process, "pid", None), "mode": mode, "freshnessVerified": False}
        try:
            deadline = monotonic() + self.bootstrap_timeout_seconds
            while True:
                health = dict(self.feed_health_provider())
                if bool(health.get("marketFresh")):
                    break
                if monotonic() >= deadline:
                    raise RuntimeError("行情连接超时，订单保持关闭")
                sleep(self.bootstrap_poll_seconds)
            full_preflight = self.preflight_state(allow_pending_feeds=False)
            if not full_preflight["safe"]:
                failed = ", ".join(item["label"] for item in full_preflight["checks"].values() if not item["passed"])
                raise RuntimeError(f"行情恢复后的完整预检未通过：{failed}")
            self.runner_submission_ready = True
            self.settings_store.save({"orderSubmissionEnabled": restore_submission})
        except Exception:
            self.stop_runner()
            self.settings_store.save({"orderSubmissionEnabled": restore_submission})
            raise
        return {"status": "running", "pid": getattr(self.runner_process, "pid", None), "mode": mode, "freshnessVerified": True}

    def stop_runner(self) -> dict[str, Any]:
        process = self.runner_process
        # Close the order gate before signalling the child.  SIGINT lets the
        # asyncio runner execute its finally/close path, draining fill and fee
        # reconciliation threads and writing HEALTH_STOPPED.  Test doubles or
        # platforms without send_signal fall back to terminate().
        self.settings_store.save({"orderSubmissionEnabled": False})
        if process is not None and process.poll() is None:
            send_signal = getattr(process, "send_signal", None)
            if callable(send_signal):
                try:
                    send_signal(signal.SIGINT)
                except OSError:
                    process.terminate()
            else:
                process.terminate()
            try:
                process.wait(timeout=8)
            except (AttributeError, subprocess.TimeoutExpired):
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except (AttributeError, subprocess.TimeoutExpired):
                        pass
        self.runner_process = None
        self.runner_mode = None
        self.runner_submission_ready = False
        return {"status": "stopped", "pid": None, "mode": None}

    def preflight_state(self, *, allow_pending_feeds: bool | None = None) -> dict[str, Any]:
        settings = self.settings_store.load()
        private_key = bool(self.keychain_store.get())
        account_type = str(settings.get("accountType", ""))
        mode = str(settings.get("liveMode", "observe"))
        observe_only = mode == "observe"
        if allow_pending_feeds is None:
            allow_pending_feeds = self.runner_state()["status"] != "running"
        checks = {
            "privateKey": {"passed": private_key or observe_only, "label": "Keychain 私钥", "detail": "已配置" if private_key else ("observe 不需要私钥" if observe_only else "未配置")},
            "walletAddress": {"passed": bool(settings.get("walletAddress")) or observe_only, "label": "Polygon 钱包地址", "detail": settings.get("walletAddress") or ("observe 不需要钱包地址" if observe_only else "未填写")},
            "accountDetected": {"passed": account_type in {"EOA", "POLY_PROXY", "GNOSIS_SAFE", "POLY_1271"} or observe_only or (not observe_only and self.identity_manager.is_configured()), "label": "Polymarket 身份类型", "detail": account_type or ("observe 不需要身份配置" if observe_only else ("已配置 CLOB 身份" if self.identity_manager.is_configured() else "请选择并保存身份类型"))},
            "rpcUrl": {"passed": True, "label": "Polygon RPC（可选）", "detail": "身份配置不依赖 Polygon RPC"},
            "riskLimits": {"passed": True, "label": "风险限额", "detail": f"有效单笔预算 {effective_order_budget(settings)}U，{effective_daily_loss_count(settings)} 笔亏损或 {effective_daily_loss_total(settings)}U 停止"},
        }
        if account_type in {"POLY_PROXY", "GNOSIS_SAFE", "POLY_1271", "CONTRACT_PROXY"} and not observe_only:
            funder = str(settings.get("funder", ""))
            checks["funder"] = {"passed": bool(ADDRESS_RE.fullmatch(funder)), "label": "代理账户 funder", "detail": funder or "合约代理必须填写资金账户"}
        if mode == "observe":
            checks["orderSwitches"] = {"passed": True, "label": "订单开关", "detail": "observe 只读"}
            checks["readonlyNetwork"] = {"passed": True, "label": "只读行情", "detail": "Polymarket + Binance 自动开启"}
        else:
            checks["orderSwitches"] = {"passed": True, "label": "订单通道", "detail": f"{mode} 模式启动时自动开启（启动按钮即为确认）"}
            checks["readonlyNetwork"] = {"passed": True, "label": "只读行情", "detail": "Polymarket + Binance 自动开启"}
            sdk_ok = bool(self.sdk_available())
            checks["clobSdk"] = {
                "passed": sdk_ok,
                "label": "Polymarket CLOB V2 SDK",
                "detail": "已安装" if sdk_ok else "未安装 py-clob-client-v2",
            }

            identity_configured = bool(self.identity_manager.is_configured())
            identity_report: Mapping[str, Any] = {}
            identity_error = ""
            if identity_configured and sdk_ok:
                try:
                    identity_report = self.identity_manager.validate(proxy=str(settings.get("proxy", "")))
                except Exception as exc:  # noqa: BLE001 - fail closed and redact provider details.
                    identity_error = type(exc).__name__
            identity_ok = bool(identity_report.get("ok")) and str(identity_report.get("status", "")).upper() == "OK"
            checks["clobIdentity"] = {
                "passed": identity_ok,
                "label": "CLOB 身份认证",
                "detail": "L1/L2 身份验证通过" if identity_ok else (f"验证失败（{identity_error}）" if identity_error else "尚未通过真实 CLOB 身份验证"),
            }

            try:
                balance = Decimal(str(identity_report.get("collateralBalance", "0")))
                required_balance = effective_order_budget(settings)
                balance_ok = balance >= required_balance
            except (InvalidOperation, TypeError, ValueError):
                balance = Decimal("0")
                required_balance = Decimal("5")
                balance_ok = False
            checks["collateralBalance"] = {
                "passed": balance_ok,
                "label": "可用 pUSD 抵押品",
                "detail": f"{balance}U（至少需要 {required_balance}U）",
            }

            allowance_ok = bool(identity_report.get("allowanceConfigured"))
            checks["allowance"] = {
                "passed": allowance_ok,
                "label": "CLOB 交易授权",
                "detail": "已配置" if allowance_ok else "余额授权不足或未确认",
            }
            try:
                open_order_count = int(identity_report.get("openOrderCount", -1))
            except (TypeError, ValueError):
                open_order_count = -1
            checks["openOrders"] = {
                "passed": open_order_count == 0,
                "label": "未结订单检查",
                "detail": "无未结订单" if open_order_count == 0 else (f"存在 {open_order_count} 笔未结订单" if open_order_count > 0 else "无法确认未结订单"),
            }
            try:
                settled_markets = {
                    str(event.get("marketId", ""))
                    for event in iter_partitioned_events(self.data_dir, "settlements")
                    if event.get("marketId")
                }
                unresolved_local = [
                    record
                    for record in SQLiteOrderStore(self.data_dir / "orders.sqlite3").list_all()
                    if record.market_id not in settled_markets
                    and record.state not in {OrderState.CANCELED, OrderState.REJECTED, OrderState.DISABLED}
                ]
                local_orders_ok = not unresolved_local
            except Exception:  # noqa: BLE001 - unreadable local state is unsafe.
                unresolved_local = []
                local_orders_ok = False
            checks["localOrders"] = {
                "passed": local_orders_ok,
                "label": "本地订单恢复状态",
                "detail": "无未决本地订单" if local_orders_ok else (f"存在 {len(unresolved_local)} 笔未对账/未结算订单" if unresolved_local else "本地订单库无法读取"),
            }
            closed_only = bool(identity_report.get("closedOnly", True))
            checks["venueStatus"] = {
                "passed": not closed_only,
                "label": "CLOB 市场状态",
                "detail": "可交易" if not closed_only else "当前仅允许撤单或状态未知",
            }

            if allow_pending_feeds:
                checks["marketFreshness"] = {
                    "passed": True,
                    "pending": True,
                    "label": "Polymarket 行情新鲜度",
                    "detail": "待启动后连接并再次验证；验证完成前禁止下单",
                }
                checks["binanceFreshness"] = {
                    "passed": True,
                    "pending": False,
                    "optional": True,
                    "label": "Binance Spot 参考行情（可选）",
                    "detail": "不参与当前策略门禁；缺失不会阻断信号或订单",
                }
            else:
                try:
                    feed_health = dict(self.feed_health_provider())
                except Exception:  # noqa: BLE001 - a missing health source must block trading.
                    feed_health = {}
                market_fresh = bool(feed_health.get("marketFresh"))
                binance_fresh = bool(feed_health.get("binanceFresh"))
                market_age = feed_health.get("marketAgeSeconds")
                binance_age = feed_health.get("binanceAgeSeconds")
                checks["marketFreshness"] = {
                    "passed": market_fresh,
                    "pending": False,
                    "label": "Polymarket 行情新鲜度",
                    "detail": "实时行情正常" if market_fresh else (f"行情已陈旧（{market_age:.1f}秒）" if isinstance(market_age, (int, float)) else "尚未收到实时行情"),
                }
                checks["binanceFreshness"] = {
                    "passed": True,
                    "pending": False,
                    "optional": True,
                    "label": "Binance Spot 参考行情（可选）",
                    "detail": "最近 1m K线正常" if binance_fresh else (f"未更新（{binance_age:.1f}秒），不阻断当前策略" if isinstance(binance_age, (int, float)) else "尚未收到，不阻断当前策略"),
                }
        safe = all(item["passed"] for item in checks.values())
        return {"safe": safe, "mode": mode, "checks": checks, "account": self.last_account_detection or {"accountType": account_type}}

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
