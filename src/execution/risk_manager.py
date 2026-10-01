"""
Risk Manager with strict exposure checks, daily loss limits,
concurrency locking, and automatic kill switch.
"""

import asyncio
import logging
import math
from datetime import datetime, timezone
from typing import Set, Dict, Optional

from src.models import ArbitrageOpportunity, LiveOrder, OrderStatus
from src.config import RiskConfig

logger = logging.getLogger(__name__)


class RiskManager:
    """
    Centralized risk controller.
    Guarantees concurrency safety by locking market pairs during execution.
    Tracks global and per-market exposure and halts trading on limit breaches.
    """

    def __init__(self, config: RiskConfig):
        self.config = config
        self._locked_pairs: Set[str] = set()
        self._lock = asyncio.Lock()
        
        # Risk tracking state
        self.daily_realized_loss: float = 0.0
        self.current_global_exposure: float = 0.0
        self.market_exposure: Dict[str, float] = {}
        self.consecutive_failures: int = 0
        self.kill_switch_active: bool = False
        self.kill_switch_reason: str = ""

    async def acquire_pair_lock(self, pair_id: str) -> bool:
        """
        Atomically acquire execution lock for a market pair.
        Prevents racing updates from placing duplicate orders on the same opportunity.
        """
        async with self._lock:
            if self.kill_switch_active:
                logger.warning(f"Trade rejected: Kill switch active ({self.kill_switch_reason})")
                return False
            if pair_id in self._locked_pairs:
                logger.debug(f"Pair {pair_id} is already being executed. Lock denied.")
                return False
            self._locked_pairs.add(pair_id)
            return True

    async def release_pair_lock(self, pair_id: str) -> None:
        """Release execution lock for a market pair."""
        async with self._lock:
            self._locked_pairs.discard(pair_id)

    def validate_opportunity(self, opp: ArbitrageOpportunity) -> tuple[bool, str]:
        """Validate opportunity against all financial and risk limits."""
        if self.kill_switch_active:
            return False, f"Kill switch active: {self.kill_switch_reason}"

        # Target size capped by max_order_size_dollars and max_contracts_per_trade
        max_by_dollars = math.floor(self.config.max_order_size_dollars / opp.gross_cost_per_unit) if opp.gross_cost_per_unit > 0 else 0
        max_allowed_qty = min(max_by_dollars, getattr(self.config, "max_contracts_per_trade", 10.0))
        executable_qty = min(opp.executable_quantity, max_allowed_qty)

        if executable_qty < 1.0:
            return False, f"Executable quantity {executable_qty} below minimum 1 contract"

        notional = opp.gross_cost_per_unit * executable_qty

        # 1. Size bounds
        if notional < self.config.min_order_size_dollars:
            return False, f"Trade size ${notional:.2f} below minimum ${self.config.min_order_size_dollars:.2f}"
        if notional > self.config.max_order_size_dollars + 0.01:
            return False, f"Trade size ${notional:.2f} exceeds maximum ${self.config.max_order_size_dollars:.2f}"

        # 2. Exposure limits
        pair_id = opp.market_pair.pair_id
        current_mkt_exp = self.market_exposure.get(pair_id, 0.0)
        if (current_mkt_exp + notional) > self.config.max_market_exposure_dollars + 0.01:
            return False, f"Market exposure ${current_mkt_exp + notional:.2f} exceeds limit ${self.config.max_market_exposure_dollars:.2f}"

        if (self.current_global_exposure + notional) > self.config.max_total_exposure_dollars + 0.01:
            return False, f"Global exposure ${self.current_global_exposure + notional:.2f} exceeds limit ${self.config.max_total_exposure_dollars:.2f}"

        # 3. Daily loss check
        if self.daily_realized_loss >= self.config.max_daily_loss_dollars:
            self.trigger_kill_switch("Max daily loss limit breached")
            return False, "Daily loss limit breached"

        return True, "OK"

    def record_trade_result(self, profit_dollars: float, notional: float, pair_id: str) -> None:
        """Update risk metrics after trade completion or failure."""
        if profit_dollars < 0:
            loss = abs(profit_dollars)
            self.daily_realized_loss += loss
            self.consecutive_failures += 1
            logger.warning(
                f"Trade loss recorded: -${loss:.2f}. Total daily loss: ${self.daily_realized_loss:.2f} "
                f"(Failures: {self.consecutive_failures}/{self.config.max_consecutive_failures})"
            )
            if self.daily_realized_loss >= self.config.max_daily_loss_dollars:
                self.trigger_kill_switch(f"Daily loss ${self.daily_realized_loss:.2f} breached max limit")
            elif self.consecutive_failures >= self.config.max_consecutive_failures:
                self.trigger_kill_switch(f"Consecutive failures ({self.consecutive_failures}) reached threshold")
        else:
            self.consecutive_failures = 0
            logger.info(f"Profitable trade recorded: +${profit_dollars:.2f}")

    def update_exposure(self, pair_id: str, notional_delta: float) -> None:
        """Update active position exposure."""
        self.current_global_exposure = max(0.0, self.current_global_exposure + notional_delta)
        self.market_exposure[pair_id] = max(0.0, self.market_exposure.get(pair_id, 0.0) + notional_delta)

    def trigger_kill_switch(self, reason: str) -> None:
        """Halt all trading immediately."""
        self.kill_switch_active = True
        self.kill_switch_reason = reason
        logger.critical(f"EMERGENCY KILL SWITCH TRIGGERED: {reason}. All trading halted.")


Tuple = tuple
Tuple_bool_str = tuple[bool, str]
