from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class StrategyConfig:
    side: str
    min_remaining_seconds: Decimal
    max_remaining_seconds: Decimal
    max_budget: Decimal
    min_valid_cost: Decimal
    max_entry_price: Decimal
    reversal_window_seconds: Decimal
    reversal_tolerance: Decimal
    binance_lookback_seconds: Decimal
    fee_rate: Decimal
    quantity_step: Decimal
    fee_exponent: Decimal = Decimal("1")
    active_trade_min_quantity: Decimal = Decimal("10")
    market_data_max_age_seconds: Decimal = Decimal("2")
    min_entry_vwap: Decimal = Decimal("0")
    min_entry_momentum: Decimal = Decimal("0")
    active_trade_multiplier: Decimal = Decimal("1.5")

    @classmethod
    def from_mapping(cls, data: Dict[str, Any]) -> "StrategyConfig":
        return cls(
            side=str(data["side"]),
            min_remaining_seconds=Decimal(str(data["min_remaining_seconds"])),
            max_remaining_seconds=Decimal(str(data["max_remaining_seconds"])),
            max_budget=Decimal(str(data["max_budget"])),
            min_valid_cost=Decimal(str(data["min_valid_cost"])),
            max_entry_price=Decimal(str(data["max_entry_price"])),
            reversal_window_seconds=Decimal(str(data["reversal_window_seconds"])),
            reversal_tolerance=Decimal(str(data["reversal_tolerance"])),
            binance_lookback_seconds=Decimal(str(data["binance_lookback_seconds"])),
            fee_rate=Decimal(str(data["fee_rate"])),
            quantity_step=Decimal(str(data["quantity_step"])),
            fee_exponent=Decimal(str(data.get("fee_exponent", 1))),
            active_trade_min_quantity=Decimal(str(data.get("active_trade_min_quantity", 10))),
            market_data_max_age_seconds=Decimal(str(data.get("market_data_max_age_seconds", 2))),
            min_entry_vwap=Decimal(str(data.get("min_entry_vwap", 0))),
            min_entry_momentum=Decimal(str(data.get("min_entry_momentum", 0))),
            active_trade_multiplier=Decimal(str(data.get("active_trade_multiplier", 1.5))),
        )


@dataclass(frozen=True)
class ActiveTrade:
    side: str
    aggressor: str
    quantity: Decimal
    event_time: datetime


@dataclass(frozen=True)
class MarketSnapshot:
    market_id: str
    condition_id: str
    down_token_id: str
    remaining_seconds: Decimal
    down_price_now: Decimal
    down_price_five_seconds_ago: Decimal
    binance_return_5m: Decimal
    active_trade: Optional[ActiveTrade]
    received_at: datetime
    # ``side`` is the outcome represented by the price fields.  The original
    # fields retain their names for schema compatibility with DOWN history.
    side: str = "DOWN"


@dataclass(frozen=True)
class SignalDecision:
    accepted: bool
    reason: str
    market_id: str
    expected_price: Optional[Decimal] = None
