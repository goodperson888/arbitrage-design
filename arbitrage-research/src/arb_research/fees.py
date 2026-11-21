from __future__ import annotations

from decimal import Decimal


ZERO = Decimal("0")


def taker_fee_per_share(model: str, price: Decimal, fee_rate: Decimal) -> Decimal:
    if model == "none":
        return ZERO
    if model == "polymarket_crypto_taker":
        return fee_rate * price * (Decimal("1") - price)
    raise ValueError(f"unsupported fee_model: {model}")


def taker_fee(model: str, price: Decimal, quantity: Decimal, fee_rate: Decimal) -> Decimal:
    return taker_fee_per_share(model, price, fee_rate) * quantity
