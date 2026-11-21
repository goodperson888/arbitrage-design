from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

import unittest

from live_trader.execution import LiveOrderDisabled, OrderIntent, SafetyExecution
from live_trader.ledger import CriticalLedger
from live_trader.models import ActiveTrade, MarketSnapshot, StrategyConfig
from live_trader.risk import DailyRiskGate, RiskHalt
from live_trader.strategy import evaluate_signal, fee_per_share, max_quantity_for_budget


def config() -> StrategyConfig:
    return StrategyConfig(
        side="DOWN",
        min_remaining_seconds=Decimal("30"),
        max_remaining_seconds=Decimal("90"),
        max_budget=Decimal("5"),
        min_valid_cost=Decimal("2.5"),
        max_entry_price=Decimal("0.85"),
        reversal_window_seconds=Decimal("5"),
        reversal_tolerance=Decimal("0.01"),
        binance_lookback_seconds=Decimal("300"),
        fee_rate=Decimal("0.07"),
        quantity_step=Decimal("0.01"),
        active_trade_min_quantity=Decimal("10"),
        market_data_max_age_seconds=Decimal("2"),
    )


def snapshot(**overrides: object) -> MarketSnapshot:
    values: dict[str, object] = {
        "market_id": "btc-5m-1",
        "condition_id": "condition-1",
        "down_token_id": "down-token-1",
        "remaining_seconds": Decimal("60"),
        "down_price_now": Decimal("0.82"),
        "down_price_five_seconds_ago": Decimal("0.82"),
        "binance_return_5m": Decimal("-0.002"),
        "active_trade": ActiveTrade(
            side="DOWN",
            aggressor="BUY",
            quantity=Decimal("12"),
            event_time=datetime.now(timezone.utc),
        ),
        "received_at": datetime.now(timezone.utc),
    }
    values.update(overrides)
    return MarketSnapshot(**values)


class SafetyTests(unittest.TestCase):
  def test_v2_dynamic_fee_exponent_is_used_for_fee_inclusive_budget(self) -> None:
    price = Decimal("0.82")
    self.assertEqual(
        fee_per_share(price, Decimal("0.12"), Decimal("2")),
        Decimal("0.12") * (price * (Decimal("1") - price)) ** 2,
    )

  def test_signal_accepts_only_confirmed_down_conditions(self) -> None:
    decision = evaluate_signal(snapshot(), config(), now=datetime.now(timezone.utc))

    self.assertTrue(decision.accepted)
    self.assertEqual(decision.reason, "eligible")

  def test_signal_does_not_require_binance_spot_to_be_negative(self) -> None:
    decision = evaluate_signal(snapshot(binance_return_5m=Decimal("0.002")), config(), now=datetime.now(timezone.utc))

    self.assertTrue(decision.accepted)
    self.assertEqual(decision.reason, "eligible")


  def test_signal_rejects_reverse_move_before_entry(self) -> None:
    decision = evaluate_signal(
        snapshot(down_price_now=Decimal("0.80")),
        config(),
        now=datetime.now(timezone.utc),
    )

    self.assertFalse(decision.accepted)
    self.assertEqual(decision.reason, "down_price_reversed")


  def test_signal_requires_same_direction_momentum_when_configured(self) -> None:
    momentum_config = config().__class__(**{
        **config().__dict__,
        "min_entry_momentum": Decimal("0.01"),
    })
    flat = evaluate_signal(snapshot(), momentum_config, now=datetime.now(timezone.utc))
    self.assertFalse(flat.accepted)
    self.assertEqual(flat.reason, "same_direction_momentum_below_min")

    rising = evaluate_signal(
        snapshot(down_price_now=Decimal("0.83")),
        momentum_config,
        now=datetime.now(timezone.utc),
    )
    self.assertTrue(rising.accepted)


  def test_signal_rejects_non_down_aggressor(self) -> None:
    trade = ActiveTrade(
        side="DOWN",
        aggressor="SELL",
        quantity=Decimal("12"),
        event_time=datetime.now(timezone.utc),
    )
    decision = evaluate_signal(snapshot(active_trade=trade), config(), now=datetime.now(timezone.utc))

    self.assertFalse(decision.accepted)
    self.assertEqual(decision.reason, "active_trade_not_down_buy")

  def test_signal_rejects_active_trade_below_ten_shares(self) -> None:
    trade = ActiveTrade(
        side="DOWN",
        aggressor="BUY",
        quantity=Decimal("9.99"),
        event_time=datetime.now(timezone.utc),
    )
    decision = evaluate_signal(snapshot(active_trade=trade), config(), now=datetime.now(timezone.utc))

    self.assertFalse(decision.accepted)
    self.assertEqual(decision.reason, "active_trade_below_threshold")

  def test_signal_rejects_stale_book(self) -> None:
    decision = evaluate_signal(
        snapshot(received_at=datetime.now(timezone.utc).replace(microsecond=0)),
        config(),
        now=datetime.now(timezone.utc),
    )

    self.assertTrue(decision.accepted)

    stale = snapshot(received_at=datetime.now(timezone.utc).replace(year=2020))
    decision = evaluate_signal(stale, config(), now=datetime.now(timezone.utc))
    self.assertFalse(decision.accepted)
    self.assertEqual(decision.reason, "stale_market_data")


  def test_quantity_is_rounded_down_and_stays_within_fee_inclusive_budget(self) -> None:
    quantity, total_cost = max_quantity_for_budget(Decimal("0.85"), config())

    self.assertEqual(quantity, Decimal("5.82"))
    self.assertLessEqual(total_cost, Decimal("5"))


  def test_safety_execution_never_sends_real_orders(self) -> None:
    execution = SafetyExecution(mode="canary")
    intent = OrderIntent(
        market_id="btc-5m-1",
        token_id="down-token-1",
        side="DOWN",
        price=Decimal("0.85"),
        quantity=Decimal("5"),
        order_type="FAK",
    )

    with self.assertRaises(LiveOrderDisabled):
        execution.submit(intent)

    with self.assertRaisesRegex(LiveOrderDisabled, "session limit"):
        execution.submit(intent)


  def test_daily_gate_halts_on_two_losses_or_ten_units(self) -> None:
    gate = DailyRiskGate(day=date(2026, 8, 24))
    gate.record_settlement(Decimal("-1"))
    gate.record_settlement(Decimal("-1"))

    with self.assertRaisesRegex(RiskHalt, "loss_count"):
        gate.admit("btc-5m-3", Decimal("3"))


  def test_new_day_stays_halted_until_manual_resume(self) -> None:
    gate = DailyRiskGate(day=date(2026, 8, 24))
    gate.record_settlement(Decimal("-10"))
    gate.advance_day(date(2026, 8, 25))

    with self.assertRaisesRegex(RiskHalt, "manual"):
        gate.admit("btc-5m-4", Decimal("3"))

    gate.manual_resume(date(2026, 8, 25))
    gate.admit("btc-5m-4", Decimal("3"))

  def test_one_unsettled_market_is_the_default_limit(self) -> None:
    gate = DailyRiskGate(day=date(2026, 8, 24), max_unsettled_markets=1)
    gate.admit("btc-5m-5", Decimal("3"))
    with self.assertRaisesRegex(RiskHalt, "unsettled"):
        gate.admit("btc-5m-6", Decimal("3"))

  def test_ledger_writes_sqlite_and_ndjson(self) -> None:
    with TemporaryDirectory() as temp:
      root = Path(temp)
      ledger = CriticalLedger(root / "ledger.sqlite3", root / "ledger.ndjson")
      ledger.record("order_rejected", {"reason": "test"})
      self.assertTrue((root / "ledger.sqlite3").exists())
      self.assertEqual(len((root / "ledger.ndjson").read_text().splitlines()), 1)
