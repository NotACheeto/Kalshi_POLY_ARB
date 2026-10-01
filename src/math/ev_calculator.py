"""
Conservative Arbitrage and Expected Value (EV) Engine.
Enforces hard safety gates: NEVER executes unless conservative EV is strictly positive
after all fees, spreads, slippage buffers, capital costs, and adverse execution buffers.
"""

import math
import uuid
from datetime import datetime, timezone
from typing import Optional, Tuple, List

from src.models import (
    Platform,
    TokenType,
    OrderSide,
    NormalizedOrderBook,
    MatchedMarketPair,
    ArbitrageOpportunity,
    ArbitrageLegSpec,
)
from src.config import ArbitrageGateConfig
from src.math.fee_calculator import FeeCalculator


class EVCalculator:
    """
    Evaluates cross-exchange order books for genuine arbitrage opportunities.
    Computes executable prices, sizing, fees, slippage, and net expected value.
    """

    def __init__(self, config: ArbitrageGateConfig, fee_calculator: FeeCalculator):
        self.config = config
        self.fees = fee_calculator

    def evaluate_pair(
        self,
        pair: MatchedMarketPair,
        poly_ob: NormalizedOrderBook,
        kalshi_ob: NormalizedOrderBook,
        now: Optional[datetime] = None,
        latency_ms: float = 0.0,
    ) -> Optional[ArbitrageOpportunity]:
        """
        Evaluate a matched market pair for arbitrage opportunities in both directions.
        Returns the most profitable conservative positive-EV opportunity, or None.
        """
        if now is None:
            now = datetime.now(timezone.utc)

        # 1. Stale data check
        poly_age = poly_ob.age_seconds(now)
        kalshi_age = kalshi_ob.age_seconds(now)
        if poly_age > self.config.max_quote_age_seconds or kalshi_age > self.config.max_quote_age_seconds:
            return None

        # 2. Expiration window check
        res_time = pair.resolution_time
        if res_time.tzinfo is None:
            res_time = res_time.replace(tzinfo=timezone.utc)
            
        time_to_res = res_time - now
        hours_to_res = time_to_res.total_seconds() / 3600.0

        if hours_to_res > self.config.max_hours_to_resolution:
            # Beyond our short-duration horizon (> 24h)
            return None
        if hours_to_res < (self.config.min_minutes_to_resolution / 60.0):
            # Too close to expiration (< 5 min), high settlement chaos risk
            return None

        opps: List[ArbitrageOpportunity] = []

        # Direction 1: Buy YES on Poly + Buy NO on Kalshi
        opp1 = self._evaluate_direction(
            pair=pair,
            leg1_platform=Platform.POLYMARKET,
            leg1_token=TokenType.YES,
            leg1_price=poly_ob.best_yes_ask,
            leg1_size=poly_ob.best_yes_ask_size,
            leg1_token_id=pair.poly_market.yes_token_id,
            leg2_platform=Platform.KALSHI,
            leg2_token=TokenType.NO,
            leg2_price=kalshi_ob.best_no_ask,
            leg2_size=kalshi_ob.best_no_ask_size,
            leg2_token_id=pair.kalshi_market.market_id,
            hours_to_res=hours_to_res,
            now=now,
            latency_ms=latency_ms,
        )
        if opp1 is not None and opp1.is_positive_ev:
            opps.append(opp1)

        # Direction 2: Buy NO on Poly + Buy YES on Kalshi
        opp2 = self._evaluate_direction(
            pair=pair,
            leg1_platform=Platform.POLYMARKET,
            leg1_token=TokenType.NO,
            leg1_price=poly_ob.best_no_ask,
            leg1_size=poly_ob.best_no_ask_size,
            leg1_token_id=pair.poly_market.no_token_id,
            leg2_platform=Platform.KALSHI,
            leg2_token=TokenType.YES,
            leg2_price=kalshi_ob.best_yes_ask,
            leg2_size=kalshi_ob.best_yes_ask_size,
            leg2_token_id=pair.kalshi_market.market_id,
            hours_to_res=hours_to_res,
            now=now,
            latency_ms=latency_ms,
        )
        if opp2 is not None and opp2.is_positive_ev:
            opps.append(opp2)

        if not opps:
            return None

        # Return opportunity with highest net profit
        return max(opps, key=lambda o: o.net_profit)

    def _evaluate_direction(
        self,
        pair: MatchedMarketPair,
        leg1_platform: Platform,
        leg1_token: TokenType,
        leg1_price: Optional[float],
        leg1_size: Optional[float],
        leg1_token_id: Optional[str],
        leg2_platform: Platform,
        leg2_token: TokenType,
        leg2_price: Optional[float],
        leg2_size: Optional[float],
        leg2_token_id: Optional[str],
        hours_to_res: float,
        now: datetime,
        latency_ms: float = 0.0,
    ) -> Optional[ArbitrageOpportunity]:
        """Calculates exact EV for a specific synthetic bundle direction."""
        if leg1_price is None or leg2_price is None or leg1_size is None or leg2_size is None:
            return None

        # Prices must be strictly between 0 and 1
        if not (0.001 <= leg1_price <= 0.999) or not (0.001 <= leg2_price <= 0.999):
            return None

        # Available executable quantity is limited by the thinnest book
        executable_qty = min(leg1_size, leg2_size)
        if executable_qty <= 0.0:
            return None

        # Base outlay per contract
        gross_cost_per_unit = leg1_price + leg2_price
        gross_payout_per_unit = 1.00  # Exactly one of YES or NO pays $1.00
        gross_edge_per_unit = gross_payout_per_unit - gross_cost_per_unit

        # Preliminary gross edge check: if gross cost >= 1.00, it cannot be profitable
        if gross_edge_per_unit <= 0.0:
            return None

        # Total gross capital required
        gross_capital_outlay = gross_cost_per_unit * executable_qty

        # Exact exchange fees
        leg1_fee = self.fees.calculate_fee(leg1_platform, leg1_price, executable_qty, OrderSide.BUY, is_taker=True)
        leg2_fee = self.fees.calculate_fee(leg2_platform, leg2_price, executable_qty, OrderSide.BUY, is_taker=True)

        poly_fee_total = leg1_fee if leg1_platform == Platform.POLYMARKET else leg2_fee
        kalshi_fee_total = leg1_fee if leg1_platform == Platform.KALSHI else leg2_fee

        # Friction buffers
        # 1. Slippage buffer
        slippage_buffer_total = (
            executable_qty * (self.config.slippage_buffer_per_leg * leg1_price + self.config.slippage_buffer_per_leg * leg2_price)
        )
        
        # 2. Adverse execution / unhedged fill safety buffer
        adverse_buffer_total = executable_qty * self.config.adverse_leg_buffer_cents

        # 3. Capital opportunity cost: (gross_outlay * r_annual * hours / 8760)
        capital_cost_total = gross_capital_outlay * self.config.annual_capital_cost_rate * (hours_to_res / 8760.0)

        # 4. Dynamic latency friction buffer:
        # Accounts for execution lag / quote decay when round-trip latency > 100ms
        latency_buffer_total = 0.0
        if latency_ms > 100.0:
            # $0.002 per contract per 100ms of excess network latency
            latency_rate = ((latency_ms - 100.0) / 100.0) * 0.002
            latency_buffer_total = executable_qty * latency_rate

        total_costs = (
            poly_fee_total
            + kalshi_fee_total
            + slippage_buffer_total
            + adverse_buffer_total
            + capital_cost_total
            + latency_buffer_total
        )

        gross_profit_total = gross_edge_per_unit * executable_qty
        conservative_net_profit = gross_profit_total - total_costs
        conservative_net_edge_pct = conservative_net_profit / gross_capital_outlay if gross_capital_outlay > 0 else 0.0

        # Hard Gate Enforcements:
        # 1. Must satisfy minimum required net profit in dollars
        if conservative_net_profit < self.config.min_net_profit_dollars:
            return None

        # 2. Must satisfy minimum net percentage edge
        if conservative_net_edge_pct < self.config.min_net_edge_pct:
            return None

        # Annualized ROI
        annualized_return = (
            (conservative_net_edge_pct * (8760.0 / hours_to_res))
            if hours_to_res > 0
            else 0.0
        )

        opp_id = f"arb_{uuid.uuid4().hex[:12]}"
        leg1_spec = ArbitrageLegSpec(
            platform=leg1_platform,
            market_id=pair.poly_market.market_id if leg1_platform == Platform.POLYMARKET else pair.kalshi_market.market_id,
            token_type=leg1_token,
            side=OrderSide.BUY,
            executable_price=leg1_price,
            available_size=leg1_size,
            expected_fee=leg1_fee,
            token_id=leg1_token_id,
        )
        leg2_spec = ArbitrageLegSpec(
            platform=leg2_platform,
            market_id=pair.poly_market.market_id if leg2_platform == Platform.POLYMARKET else pair.kalshi_market.market_id,
            token_type=leg2_token,
            side=OrderSide.BUY,
            executable_price=leg2_price,
            available_size=leg2_size,
            expected_fee=leg2_fee,
            token_id=leg2_token_id,
        )

        return ArbitrageOpportunity(
            opportunity_id=opp_id,
            market_pair=pair,
            leg1=leg1_spec,
            leg2=leg2_spec,
            executable_quantity=executable_qty,
            gross_cost_per_unit=gross_cost_per_unit,
            gross_payout_per_unit=gross_payout_per_unit,
            gross_edge_per_unit=gross_edge_per_unit,
            poly_fee_total=poly_fee_total,
            kalshi_fee_total=kalshi_fee_total,
            slippage_buffer_total=slippage_buffer_total,
            capital_cost_total=capital_cost_total,
            adverse_buffer_total=adverse_buffer_total,
            latency_buffer_total=latency_buffer_total,
            total_costs=total_costs,
            net_profit=conservative_net_profit,
            net_edge_pct=conservative_net_edge_pct,
            annualized_return=annualized_return,
            hours_to_resolution=hours_to_res,
            detected_at=now,
        )
