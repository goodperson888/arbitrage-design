from __future__ import annotations

from typing import Any, Mapping, Protocol

from .execution import LiveOrderDisabled


class OrderTransport(Protocol):
    def submit(self, request: Any) -> Mapping[str, Any]: ...
    def cancel(self, remote_order_id: str) -> Mapping[str, Any]: ...
    def fetch(self, remote_order_id: str) -> Mapping[str, Any]: ...
    def fills(self, remote_order_id: str) -> list[Mapping[str, Any]]: ...
    def market_fee_details(self, condition_id: str) -> Mapping[str, str]: ...


class DisabledOrderTransport:
    """Default transport. It contains no credentials and cannot make network calls."""

    def submit(self, request: Any) -> Mapping[str, Any]:
        raise LiveOrderDisabled("order transport is disabled; no real request was sent")

    def cancel(self, remote_order_id: str) -> Mapping[str, Any]:
        raise LiveOrderDisabled("order transport is disabled; cancel was not sent")

    def fetch(self, remote_order_id: str) -> Mapping[str, Any]:
        raise LiveOrderDisabled("order transport is disabled; reconciliation was not sent")

    def fills(self, remote_order_id: str) -> list[Mapping[str, Any]]:
        raise LiveOrderDisabled("order transport is disabled; fill reconciliation was not sent")

    def market_fee_details(self, condition_id: str) -> Mapping[str, str]:
        raise LiveOrderDisabled("order transport is disabled; market fee lookup was not sent")
