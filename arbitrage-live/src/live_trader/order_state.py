from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set

from .execution import LiveOrderDisabled
from .fak import FAKRequest
from .transport import OrderTransport
from .clob_transport import TransportNotSubmitted, TransportRejected, safe_provider_message


class OrderState(str, Enum):
    INTENT = "intent"
    SUBMITTING = "submitting"
    ACKNOWLEDGED = "acknowledged"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    UNKNOWN = "unknown"
    DISABLED = "disabled"


TERMINAL = {OrderState.FILLED, OrderState.CANCELED, OrderState.REJECTED, OrderState.DISABLED}


class OrderUnknown(RuntimeError):
    pass


class InvalidOrderTransition(RuntimeError):
    pass


ALLOWED_TRANSITIONS = {
    OrderState.INTENT: {OrderState.SUBMITTING, OrderState.DISABLED},
    OrderState.SUBMITTING: {
        OrderState.ACKNOWLEDGED,
        OrderState.PARTIAL,
        OrderState.FILLED,
        OrderState.CANCELED,
        OrderState.REJECTED,
        OrderState.UNKNOWN,
        OrderState.DISABLED,
    },
    OrderState.ACKNOWLEDGED: {
        OrderState.ACKNOWLEDGED,
        OrderState.PARTIAL,
        OrderState.FILLED,
        OrderState.CANCELED,
        OrderState.UNKNOWN,
    },
    OrderState.PARTIAL: {
        OrderState.PARTIAL,
        OrderState.FILLED,
        OrderState.CANCELED,
        OrderState.UNKNOWN,
    },
    OrderState.UNKNOWN: {
        OrderState.UNKNOWN,
        OrderState.ACKNOWLEDGED,
        OrderState.PARTIAL,
        OrderState.FILLED,
        OrderState.CANCELED,
        OrderState.REJECTED,
    },
}


@dataclass(frozen=True)
class OrderRecord:
    client_order_id: str
    market_id: str
    token_id: str
    state: OrderState
    payload: Dict[str, Any]
    remote_order_id: Optional[str] = None


class SQLiteOrderStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS orders ("
                "client_order_id TEXT PRIMARY KEY, market_id TEXT NOT NULL, token_id TEXT NOT NULL,"
                "state TEXT NOT NULL, payload_json TEXT NOT NULL, remote_order_id TEXT, updated_at TEXT NOT NULL)"
            )
            connection.commit()

    def save(self, record: OrderRecord) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO orders(client_order_id, market_id, token_id, state, payload_json, remote_order_id, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (record.client_order_id, record.market_id, record.token_id, record.state.value, json.dumps(record.payload, sort_keys=True, default=str), record.remote_order_id, datetime.now(timezone.utc).isoformat()),
            )
            connection.commit()

    def get(self, client_order_id: str) -> OrderRecord:
        with sqlite3.connect(self.path) as connection:
            row = connection.execute("SELECT client_order_id, market_id, token_id, state, payload_json, remote_order_id FROM orders WHERE client_order_id = ?", (client_order_id,)).fetchone()
        if row is None:
            raise KeyError(client_order_id)
        return OrderRecord(row[0], row[1], row[2], OrderState(row[3]), json.loads(row[4]), row[5])

    def list_all(self) -> List[OrderRecord]:
        with sqlite3.connect(self.path) as connection:
            rows = connection.execute("SELECT client_order_id, market_id, token_id, state, payload_json, remote_order_id FROM orders ORDER BY updated_at").fetchall()
        return [OrderRecord(row[0], row[1], row[2], OrderState(row[3]), json.loads(row[4]), row[5]) for row in rows]

    def claim_submission(self, client_order_id: str) -> bool:
        """Atomically claim an unsent intent, including across runner instances."""
        with sqlite3.connect(self.path) as connection:
            changed = connection.execute(
                "UPDATE orders SET state = ?, updated_at = ? WHERE client_order_id = ? AND state = ?",
                (OrderState.SUBMITTING.value, datetime.now(timezone.utc).isoformat(), client_order_id, OrderState.INTENT.value),
            ).rowcount
            connection.commit()
        return changed == 1


class OrderStateMachine:
    def __init__(self, store: SQLiteOrderStore, transport: OrderTransport) -> None:
        self.store = store
        self.transport = transport
        self.paused_markets: Set[str] = set()
        for record in self.recover_open_orders():
            if record.state == OrderState.UNKNOWN:
                self.paused_markets.add(record.market_id)

    def register(self, request: FAKRequest) -> OrderRecord:
        try:
            return self.store.get(request.client_order_id)
        except KeyError:
            record = OrderRecord(
                request.client_order_id,
                request.market_id,
                request.token_id,
                OrderState.INTENT,
                {
                    "market_id": request.market_id,
                    "token_id": request.token_id,
                    "side": request.side,
                    "quantity": str(request.quantity),
                    "limit_price": str(request.limit_price),
                    "expected_vwap": str(request.expected_vwap),
                    "expected_cost": str(request.expected_cost),
                    "fee_rate": str(request.fee_rate),
                    "fee_exponent": str(request.fee_exponent),
                    "order_type": request.order_type,
                    "client_order_id": request.client_order_id,
                },
            )
            self.store.save(record)
            return record

    def submit(self, client_order_id: str) -> OrderRecord:
        record = self.store.get(client_order_id)
        if record.state in TERMINAL:
            return record
        if record.state != OrderState.INTENT:
            self.paused_markets.add(record.market_id)
            raise OrderUnknown("order was already submitted or is unknown; reconcile before retry")
        if not self.store.claim_submission(client_order_id):
            raise OrderUnknown("another submitter already claimed this order; reconcile before retry")
        submitting = self.store.get(client_order_id)
        request = submitting.payload
        try:
            response = self.transport.submit(request)
            return self._apply_remote(submitting, response)
        except LiveOrderDisabled:
            return self._transition(submitting, OrderState.DISABLED)
        except TransportNotSubmitted as exc:
            payload = dict(submitting.payload)
            payload["submission_error"] = {
                "phase": "prepare", "requestSent": False, "reconcileRequired": False,
                "exceptionType": exc.exception_type, "message": str(exc),
            }
            return self._transition(submitting, OrderState.REJECTED, payload=payload)
        except TransportRejected as exc:
            payload = dict(submitting.payload)
            payload["provider_error"] = {
                "statusCode": exc.status_code,
                "message": exc.provider_message or str(exc),
                "authoritative": True,
            }
            return self._transition(submitting, OrderState.REJECTED, payload=payload)
        except (TimeoutError, OSError, ConnectionError) as exc:
            self.paused_markets.add(record.market_id)
            self._mark_unknown(submitting, exc)
            raise OrderUnknown("submit timed out; order may have been accepted") from exc
        except Exception as exc:  # noqa: BLE001 - provider failures are ambiguous after submit.
            # SDK errors (for example an HTTP 4xx/5xx or TLS disconnect) can
            # happen after the venue accepted the request.  Never leave the
            # durable row in SUBMITTING: mark it UNKNOWN and require an
            # explicit reconciliation before any retry.
            self.paused_markets.add(record.market_id)
            self._mark_unknown(submitting, exc)
            raise OrderUnknown("CLOB submit failed after request; reconcile official status before retry") from exc

    def _mark_unknown(self, record: OrderRecord, exc: BaseException) -> None:
        diagnostic = getattr(self.transport, "error_message", safe_provider_message)
        payload = dict(record.payload)
        payload["submission_error"] = {
            "phase": "submit", "requestSent": None, "reconcileRequired": True,
            "exceptionType": type(exc).__name__, "message": diagnostic(exc),
        }
        self._transition(record, OrderState.UNKNOWN, payload=payload)

    def reconcile_no_remote_record(self, client_order_id: str, *, note: str = "official CLOB has no matching order") -> OrderRecord:
        """Close a locally pending request after an authoritative no-record check.

        This is deliberately explicit and never calls the transport.  It is
        for an operator who has independently confirmed that the venue has no
        order (for example, after a timeout with no remote order id).  The
        note is retained in the payload for audit and restart recovery.
        """
        record = self.store.get(client_order_id)
        if record.state in TERMINAL:
            return record
        if record.state not in {OrderState.INTENT, OrderState.SUBMITTING, OrderState.UNKNOWN}:
            raise InvalidOrderTransition(f"cannot reconcile {record.state.value} without a remote record")
        payload = dict(record.payload)
        payload["reconciliation"] = {
            "status": "no_remote_record",
            "note": str(note),
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
        self.paused_markets.discard(record.market_id)
        return self._transition(record, OrderState.CANCELED, payload=payload)

    def reconcile(self, client_order_id: str, response: Mapping[str, Any]) -> OrderRecord:
        record = self.store.get(client_order_id)
        if record.state == OrderState.INTENT:
            record = self._transition(record, OrderState.SUBMITTING)
        return self._apply_remote(record, response)

    def recover_open_orders(self) -> List[OrderRecord]:
        return [record for record in self.store.list_all() if record.state not in TERMINAL]

    def _transition(
        self,
        record: OrderRecord,
        state: OrderState,
        remote_order_id: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> OrderRecord:
        if state != record.state and state not in ALLOWED_TRANSITIONS.get(record.state, set()):
            raise InvalidOrderTransition(f"cannot transition {record.state.value} -> {state.value}")
        updated = OrderRecord(
            record.client_order_id,
            record.market_id,
            record.token_id,
            state,
            payload if payload is not None else record.payload,
            remote_order_id or record.remote_order_id,
        )
        self.store.save(updated)
        return updated

    def _apply_remote(self, record: OrderRecord, response: Mapping[str, Any]) -> OrderRecord:
        status = str(response.get("status", "")).lower()
        mapping = {
            "acknowledged": OrderState.ACKNOWLEDGED,
            "open": OrderState.ACKNOWLEDGED,
            "live": OrderState.ACKNOWLEDGED,
            "delayed": OrderState.ACKNOWLEDGED,
            "partial": OrderState.PARTIAL,
            # A FAK response marked "matched" can still represent a partial
            # execution. Exact fills decide PARTIAL versus FILLED.
            "matched": OrderState.ACKNOWLEDGED,
            "filled": OrderState.FILLED,
            "canceled": OrderState.CANCELED,
            "cancelled": OrderState.CANCELED,
            "rejected": OrderState.REJECTED,
        }
        remote_order_id = str(
            response.get("order_id", response.get("orderID", response.get("id", record.remote_order_id or "")))
        )
        if response.get("success") is False and status not in mapping:
            status = "rejected"
        state = mapping.get(status)
        payload = dict(record.payload)
        payload["remote_response"] = dict(response)
        if state is None:
            self.paused_markets.add(record.market_id)
            payload["submission_error"] = {
                "phase": "submit", "requestSent": None, "reconcileRequired": True,
                "exceptionType": "UnrecognizedOrderResponse", "message": "CLOB response has no recognized order status",
            }
            return self._transition(record, OrderState.UNKNOWN, remote_order_id, payload)
        if state == OrderState.FILLED:
            self.paused_markets.discard(record.market_id)
        return self._transition(record, state, remote_order_id, payload)
