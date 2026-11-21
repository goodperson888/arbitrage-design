from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Optional, Tuple

from .models import MarketSnapshot, SignalDecision, StrategyConfig


def evaluate_signal(
    snapshot: MarketSnapshot,
    config: StrategyConfig,
    now: Optional[datetime] = None,
) -> SignalDecision:
    """Evaluate hard entry gates; this function never submits an order."""
    if snapshot.remaining_seconds < config.min_remaining_seconds:
        return SignalDecision(False, "remaining_below_window", snapshot.market_id)
    if snapshot.remaining_seconds > config.max_remaining_seconds:
        return SignalDecision(False, "remaining_above_window", snapshot.market_id)
    target_side = str(snapshot.side or config.side).upper()
    if target_side not in {"UP", "DOWN"}:
        return SignalDecision(False, "unsupported_strategy_side", snapshot.market_id)
    if config.side.upper() not in {"BOTH", target_side}:
        return SignalDecision(False, "strategy_side_mismatch", snapshot.market_id)
    if snapshot.down_price_now > config.max_entry_price:
        return SignalDecision(False, "price_above_max", snapshot.market_id)
    if now is not None:
        age = Decimal(str((now - snapshot.received_at).total_seconds()))
        if age > config.market_data_max_age_seconds:
            return SignalDecision(False, "stale_market_data", snapshot.market_id)
    trade = snapshot.active_trade
    if trade is None or trade.side != target_side or trade.aggressor != "BUY":
        return SignalDecision(False, "active_trade_not_down_buy" if target_side == "DOWN" else "active_trade_not_up_buy", snapshot.market_id)
    if trade.quantity < config.active_trade_min_quantity:
        return SignalDecision(False, "active_trade_below_threshold", snapshot.market_id)
    if snapshot.down_price_now <= snapshot.down_price_five_seconds_ago - config.reversal_tolerance:
        return SignalDecision(False, "down_price_reversed" if target_side == "DOWN" else "up_price_reversed", snapshot.market_id)
    if (
        config.min_entry_momentum > 0
        and snapshot.down_price_now - snapshot.down_price_five_seconds_ago < config.min_entry_momentum
    ):
        return SignalDecision(False, "same_direction_momentum_below_min" if target_side == "DOWN" else "same_direction_up_momentum_below_min", snapshot.market_id)
    if now is not None and snapshot.received_at > now:
        return SignalDecision(False, "snapshot_from_future", snapshot.market_id)
    return SignalDecision(True, "eligible", snapshot.market_id, snapshot.down_price_now)


def fee_per_share(
    price: Decimal,
    fee_rate: Decimal,
    fee_exponent: Decimal = Decimal("1"),
) -> Decimal:
    if fee_rate <= 0:
        return Decimal("0")
    integral_exponent = fee_exponent.to_integral_value()
    if fee_exponent < 0 or integral_exponent != fee_exponent:
        raise ValueError("fee exponent must be a non-negative integer")
    return fee_rate * (price * (Decimal("1") - price)) ** int(integral_exponent)


def max_quantity_for_budget(price: Decimal, config: StrategyConfig) -> Tuple[Decimal, Decimal]:
    if price <= 0 or price > config.max_entry_price:
        return Decimal("0"), Decimal("0")
    per_share = price + fee_per_share(price, config.fee_rate, config.fee_exponent)
    raw_quantity = config.max_budget / per_share
    units = (raw_quantity / config.quantity_step).to_integral_value(rounding=ROUND_DOWN)
    quantity = units * config.quantity_step
    total_cost = quantity * per_share
    return quantity, total_cost


def is_valid_fill_cost(cost: Decimal, config: StrategyConfig) -> bool:
    return Decimal("0") < cost <= config.max_budget and cost >= config.min_valid_cost
