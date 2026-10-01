"""
Explicit fee calculation engine for Polymarket and Kalshi.
Applies conservative rounding (ceil) to ensure fees are never underestimated.
"""

import math
from typing import Optional
from src.models import Platform, OrderSide


class FeeCalculator:
    """
    Centralized, deterministic fee calculator.
    Uses exchange-specific official fee formulas and conservative rounding.
    """

    def __init__(
        self,
        poly_taker_fee_pct: float = 0.0,
        kalshi_taker_multiplier: float = 0.07,
        kalshi_fee_cap: float = 0.02,
        fixed_gas_per_poly_order: float = 0.00,
    ):
        self.poly_taker_fee_pct = poly_taker_fee_pct
        self.kalshi_taker_multiplier = kalshi_taker_multiplier
        self.kalshi_fee_cap = kalshi_fee_cap
        self.fixed_gas_per_poly_order = fixed_gas_per_poly_order

    def calculate_kalshi_fee_per_contract(self, price: float, is_taker: bool = True) -> float:
        """
        Calculates Kalshi fee per contract in dollars.
        For taker orders, Kalshi uses a quadratic fee formula:
        fee = ceil(multiplier * price * (1 - price) * 100) / 100.
        Capped at kalshi_fee_cap (default $0.02).
        For maker orders, fee is $0.00.
        """
        if not is_taker:
            return 0.0

        p = max(0.0, min(1.0, price))
        raw_cents = self.kalshi_taker_multiplier * p * (1.0 - p) * 100.0
        # Conservative rounding: always round up to next cent
        fee_cents = math.ceil(raw_cents)
        fee_dollars = min(fee_cents / 100.0, self.kalshi_fee_cap)
        return fee_dollars

    def calculate_kalshi_total_fee(
        self,
        price: float,
        quantity: float,
        is_taker: bool = True
    ) -> float:
        """Calculate total Kalshi fee for a given trade quantity."""
        fee_per_contract = self.calculate_kalshi_fee_per_contract(price, is_taker)
        # Conservative rounding to 4 decimal places, ceiling to nearest cent total
        raw_total = fee_per_contract * quantity
        return math.ceil(raw_total * 100.0) / 100.0

    def calculate_polymarket_fee(
        self,
        price: float,
        quantity: float,
        is_taker: bool = True,
        override_taker_pct: Optional[float] = None
    ) -> float:
        """
        Calculate total Polymarket fee for a given trade.
        Polymarket charges a percentage of notional on taker trades in fee-bearing markets.
        Maker trades have 0% fee.
        """
        if not is_taker:
            return 0.0

        pct = override_taker_pct if override_taker_pct is not None else self.poly_taker_fee_pct
        notional = price * quantity
        trade_fee = notional * pct
        total_fee = trade_fee + self.fixed_gas_per_poly_order
        # Conservative rounding up
        return math.ceil(total_fee * 100.0) / 100.0

    def calculate_fee(
        self,
        platform: Platform,
        price: float,
        quantity: float,
        side: OrderSide,
        is_taker: bool = True,
    ) -> float:
        """Generic dispatch for exchange fee calculation."""
        if platform == Platform.KALSHI:
            return self.calculate_kalshi_total_fee(price, quantity, is_taker)
        elif platform == Platform.POLYMARKET:
            return self.calculate_polymarket_fee(price, quantity, is_taker)
        else:
            raise ValueError(f"Unknown platform: {platform}")
