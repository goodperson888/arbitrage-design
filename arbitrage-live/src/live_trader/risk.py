from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Set
from datetime import datetime
from zoneinfo import ZoneInfo


class RiskHalt(RuntimeError):
    """Raised when the safety gate forbids a new order."""


def risk_day_now(timezone_name: str = "Asia/Shanghai") -> date:
    """Return the configured business-risk date without using local machine time."""
    return datetime.now(ZoneInfo(timezone_name)).date()


@dataclass
class DailyRiskGate:
    day: date
    max_budget: Decimal = Decimal("5")
    max_loss_count: int = 2
    max_loss_total: Decimal = Decimal("10")
    max_unsettled_markets: int = 1
    loss_count: int = 0
    loss_total: Decimal = Decimal("0")
    daily_cost: Decimal = Decimal("0")
    halted: bool = False
    manual_required: bool = False
    reserved_markets: Set[str] = field(default_factory=set)

    def admit(self, market_id: str, estimated_cost: Decimal) -> None:
        if self.manual_required:
            raise RiskHalt("risk gate requires manual resume")
        if self.halted:
            raise RiskHalt(
                f"risk gate halted: loss_count={self.loss_count} loss_total={self.loss_total}"
            )
        if estimated_cost <= 0 or estimated_cost > self.max_budget:
            raise RiskHalt("estimated cost exceeds per-order budget")
        if market_id in self.reserved_markets:
            raise RiskHalt("market already has an order")
        if len(self.reserved_markets) >= self.max_unsettled_markets:
            raise RiskHalt("maximum unsettled market exposure reached")
        self.reserved_markets.add(market_id)
        self.daily_cost += estimated_cost

    def record_settlement(self, pnl: Decimal) -> None:
        if pnl < 0:
            self.loss_count += 1
            self.loss_total += -pnl
        if self.loss_count >= self.max_loss_count or self.loss_total >= self.max_loss_total:
            self.halted = True

    def settle_market(self, market_id: str, pnl: Decimal) -> None:
        self.reserved_markets.discard(market_id)
        self.record_settlement(pnl)

    def advance_day(self, new_day: date) -> None:
        if new_day == self.day:
            return
        self.day = new_day
        self.loss_count = 0
        self.loss_total = Decimal("0")
        self.daily_cost = Decimal("0")
        self.halted = True
        self.manual_required = True

    def manual_resume(self, expected_day: date) -> None:
        if expected_day != self.day:
            raise RiskHalt("manual resume day mismatch")
        self.halted = False
        self.manual_required = False
