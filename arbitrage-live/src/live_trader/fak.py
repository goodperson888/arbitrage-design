from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Iterable, Tuple

from .models import StrategyConfig
from .strategy import fee_per_share


@dataclass(frozen=True)
class BookLevel:
    price: Decimal
    quantity: Decimal


@dataclass(frozen=True)
class FAKRequest:
    market_id: str
    token_id: str
    side: str
    order_type: str
    limit_price: Decimal
    quantity: Decimal
    expected_vwap: Decimal
    expected_cost: Decimal
    client_order_id: str
    fee_rate: Decimal
    fee_exponent: Decimal


class FAKRejected(ValueError):
    """FAK cannot satisfy the configured price/budget/depth constraints."""

    def __init__(self, message: str, *, details: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


def _round_quantity(quantity: Decimal, step: Decimal) -> Decimal:
    return (quantity / step).to_integral_value(rounding=ROUND_DOWN) * step


def _idempotency_key(market_id: str, token_id: str, side: str, quantity: Decimal, price: Decimal) -> str:
    raw = f"{market_id}|{token_id}|{side}|FAK|{quantity}|{price}"
    return "pm-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def build_fak_request(
    market_id: str,
    token_id: str,
    asks: Iterable[BookLevel],
    config: StrategyConfig,
    side: str = "DOWN",
) -> FAKRequest:
    side = str(side).upper()
    if side not in {"UP", "DOWN"}:
        raise FAKRejected("unsupported FAK side")
    total_quantity = Decimal("0")
    total_cost = Decimal("0")
    price_quantity = Decimal("0")
    eligible_quantity = Decimal("0")
    eligible_notional = Decimal("0")
    eligible_cost = Decimal("0")
    for level in sorted(asks, key=lambda item: item.price):
        if level.price <= 0 or level.price > config.max_entry_price or level.quantity <= 0:
            continue
        eligible_quantity += level.quantity
        eligible_notional += level.quantity * level.price
        eligible_cost += level.quantity * (level.price + fee_per_share(level.price, config.fee_rate, config.fee_exponent))
        per_share = level.price + fee_per_share(level.price, config.fee_rate, config.fee_exponent)
        affordable = (config.max_budget - total_cost) / per_share
        quantity = _round_quantity(min(level.quantity, affordable), config.quantity_step)
        if quantity <= 0:
            continue
        total_quantity += quantity
        total_cost += quantity * per_share
        price_quantity += quantity * level.price
        if total_cost >= config.max_budget:
            break
    if total_quantity <= 0 or total_cost < config.min_valid_cost:
        raise FAKRejected(
            "insufficient depth for minimum valid FAK cost",
            details={
                "eligibleQuantity": str(eligible_quantity),
                "eligibleNotional": str(eligible_notional),
                "eligibleCostWithFees": str(eligible_cost),
                "minimumValidCost": str(config.min_valid_cost),
                "maxEntryPrice": str(config.max_entry_price),
            },
        )
    vwap = price_quantity / total_quantity
    return FAKRequest(
        market_id=market_id,
        token_id=token_id,
        side=side,
        order_type="FAK",
        limit_price=config.max_entry_price,
        quantity=total_quantity,
        expected_vwap=vwap,
        expected_cost=total_cost,
        client_order_id=_idempotency_key(market_id, token_id, side, total_quantity, config.max_entry_price),
        fee_rate=config.fee_rate,
        fee_exponent=config.fee_exponent,
    )
