from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


class LiveOrderDisabled(RuntimeError):
    """The safety skeleton deliberately has no order-sending implementation."""


@dataclass(frozen=True)
class OrderIntent:
    market_id: str
    token_id: str
    side: str
    price: Decimal
    quantity: Decimal
    order_type: str = "FAK"


class SafetyExecution:
    """Intent recorder that refuses to call any venue API in every mode."""

    def __init__(self, mode: str) -> None:
        if mode not in {"observe", "canary", "live"}:
            raise ValueError("mode must be observe, canary, or live")
        self.mode = mode
        self.intents: list[OrderIntent] = []
        self.max_session_intents = 1 if mode == "canary" else 0

    def submit(self, intent: OrderIntent) -> None:
        if self.mode == "canary" and len(self.intents) >= self.max_session_intents:
            raise LiveOrderDisabled("canary session limit reached")
        self.intents.append(intent)
        raise LiveOrderDisabled(
            f"real order transport is disabled in safety skeleton (mode={self.mode})"
        )
