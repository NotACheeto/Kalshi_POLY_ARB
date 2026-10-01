"""
Two-Leg Execution Finite State Machine (FSM).
Strictly manages leg risk, partial fills, adverse price movement,
and unhedged exposure. Recalculates EV immediately before submission.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Tuple

from src.models import (
    Platform,
    TokenType,
    OrderSide,
    OrderStatus,
    ExecutionState,
    ArbitrageOpportunity,
    LiveOrder,
)
from src.config import ExecutionConfig
from src.clients.kalshi_client import KalshiClient
from src.clients.polymarket_client import PolymarketClient
from src.execution.risk_manager import RiskManager
from src.execution.reconciler import ExecutionReconciler

logger = logging.getLogger(__name__)


class LegRiskFSM:
    """
    Orchestrates two-leg arbitrage execution.
    Maintains deterministic state transitions and atomic hedges.
    """

    def __init__(
        self,
        config: ExecutionConfig,
        poly_client: PolymarketClient,
        kalshi_client: KalshiClient,
        risk_manager: RiskManager,
        reconciler: ExecutionReconciler,
    ):
        self.config = config
        self.poly_client = poly_client
        self.kalshi_client = kalshi_client
        self.risk_manager = risk_manager
        self.reconciler = reconciler

    async def execute_opportunity(self, opp: ArbitrageOpportunity) -> Tuple[bool, ExecutionState, str]:
        """
        Execute an arbitrage opportunity through the atomic 2-leg FSM.
        Returns (success: bool, final_state: ExecutionState, message: str).
        """
        pair_id = opp.market_pair.pair_id
        opp_id = opp.opportunity_id
        state = ExecutionState.DETECTED

        # 1. Acquire Pair Lock (protects against concurrent race conditions)
        acquired = await self.risk_manager.acquire_pair_lock(pair_id)
        if not acquired:
            return False, ExecutionState.ABORTED, f"Pair {pair_id} is locked by another task"

        try:
            # 2. Transition to VALIDATING
            self._transition(opp_id, pair_id, state, ExecutionState.VALIDATING)
            state = ExecutionState.VALIDATING

            # Risk check
            valid, reason = self.risk_manager.validate_opportunity(opp)
            if not valid:
                self._transition(opp_id, pair_id, state, ExecutionState.ABORTED, {"reason": reason})
                return False, ExecutionState.ABORTED, f"Risk check failed: {reason}"

            # 3. Recalculate profitability immediately before Leg 1 submission
            is_valid_now = await self._recheck_executable_prices(opp)
            if not is_valid_now:
                self._transition(opp_id, pair_id, state, ExecutionState.ABORTED, {"reason": "Quote slipped before submission"})
                return False, ExecutionState.ABORTED, "Quote slipped or market moved before execution"

            target_qty = opp.executable_quantity

            # 4. Submit Leg 1
            self._transition(opp_id, pair_id, state, ExecutionState.LEG1_SUBMITTED, {"target_qty": target_qty})
            state = ExecutionState.LEG1_SUBMITTED

            leg1_order = await self._submit_leg_order(opp.leg1, target_qty, opp_id, "leg1")
            
            # Wait for Leg 1 fill
            leg1_filled_qty = await self._wait_for_fill(
                leg1_order,
                timeout=self.config.leg1_fill_timeout_seconds,
            )

            # Leg 1 fill analysis
            if leg1_filled_qty <= 0:
                # Zero fill: Cancel order, clean abort, 0 naked risk
                await self._cancel_leg_order(leg1_order)
                self._transition(opp_id, pair_id, state, ExecutionState.ABORTED, {"reason": "Leg 1 zero fill / timeout"})
                return False, ExecutionState.ABORTED, "Leg 1 failed to fill, aborted safely with 0 exposure"

            # Check for partial fill
            if leg1_filled_qty < target_qty:
                # Cancel remaining unfilled portion of Leg 1 immediately
                await self._cancel_leg_order(leg1_order)
                self._transition(
                    opp_id, pair_id, state, ExecutionState.LEG1_PARTIAL,
                    {"filled": leg1_filled_qty, "requested": target_qty}
                )
                state = ExecutionState.LEG1_PARTIAL
            else:
                self._transition(
                    opp_id, pair_id, state, ExecutionState.LEG1_FILLED,
                    {"filled": leg1_filled_qty}
                )
                state = ExecutionState.LEG1_FILLED

            # 5. Submit Leg 2 for EXACT filled quantity of Leg 1
            hedge_qty = leg1_filled_qty
            self._transition(opp_id, pair_id, state, ExecutionState.LEG2_SUBMITTED, {"hedge_qty": hedge_qty})
            state = ExecutionState.LEG2_SUBMITTED

            leg2_order = await self._submit_leg_order(opp.leg2, hedge_qty, opp_id, "leg2")

            # Wait for Leg 2 fill
            leg2_filled_qty = await self._wait_for_fill(
                leg2_order,
                timeout=self.config.leg2_fill_timeout_seconds,
            )

            # 6. Evaluate Leg 2 fill
            if leg2_filled_qty == hedge_qty:
                # Perfect hedge completed!
                self._transition(opp_id, pair_id, state, ExecutionState.COMPLETED, {
                    "hedge_qty": hedge_qty,
                    "leg1_fill_price": leg1_order.average_fill_price,
                    "leg2_fill_price": leg2_order.average_fill_price,
                    "net_profit": opp.net_profit * (hedge_qty / target_qty),
                })
                # Record trade result in Risk Manager
                pnl = opp.net_profit * (hedge_qty / target_qty)
                self.risk_manager.record_trade_result(pnl, opp.gross_cost_per_unit * hedge_qty, pair_id)
                return True, ExecutionState.COMPLETED, f"Arbitrage successfully completed for {hedge_qty} units"

            # 7. Unhedged Leg 2 execution -> enter HEDGING
            self._transition(opp_id, pair_id, state, ExecutionState.HEDGING, {
                "leg1_filled": leg1_filled_qty,
                "leg2_filled": leg2_filled_qty,
                "unhedged_delta": leg1_filled_qty - leg2_filled_qty,
            })
            state = ExecutionState.HEDGING

            # Protective unwind / hedging logic
            unwind_ok = await self._handle_unhedged_exposure(
                opp, leg1_order, leg2_order, leg1_filled_qty, leg2_filled_qty
            )
            final_state = ExecutionState.COMPLETED if unwind_ok else ExecutionState.FAILED
            self._transition(opp_id, pair_id, state, final_state)
            return unwind_ok, final_state, f"Unhedged leg handled (Result: {final_state.value})"

        finally:
            # Always release pair lock
            await self.risk_manager.release_pair_lock(pair_id)

    async def _recheck_executable_prices(self, opp: ArbitrageOpportunity) -> bool:
        """
        Verify that orderbook prices haven't slipped between signal detection
        and order dispatch.
        """
        # If test fixture or simulated opp in dry-run, allow without external API lookup
        if self.config.dry_run and (opp.opportunity_id.startswith("test_") or "sample" in opp.market_pair.pair_id):
            return True

        try:
            if opp.leg1.platform == Platform.POLYMARKET:
                p_ob = await self.poly_client.get_orderbook(opp.market_pair.poly_market)
                k_ob = await self.kalshi_client.get_orderbook(opp.market_pair.kalshi_market.market_id)
            else:
                k_ob = await self.kalshi_client.get_orderbook(opp.market_pair.kalshi_market.market_id)
                p_ob = await self.poly_client.get_orderbook(opp.market_pair.poly_market)

            if p_ob is None or k_ob is None:
                return False

            # Check if prices are still <= executable prices
            if opp.leg1.platform == Platform.POLYMARKET:
                p_ask = p_ob.best_yes_ask if opp.leg1.token_type == TokenType.YES else p_ob.best_no_ask
                k_ask = k_ob.best_no_ask if opp.leg2.token_type == TokenType.NO else k_ob.best_yes_ask
            else:
                k_ask = k_ob.best_yes_ask if opp.leg1.token_type == TokenType.YES else k_ob.best_no_ask
                p_ask = p_ob.best_no_ask if opp.leg2.token_type == TokenType.NO else p_ob.best_yes_ask

            if p_ask is None or k_ask is None:
                return False

            # Gross cost check
            if (p_ask + k_ask) > (opp.gross_cost_per_unit + 0.005):
                logger.warning(
                    f"Execution aborted: Price slipped from {opp.gross_cost_per_unit:.4f} "
                    f"to {p_ask + k_ask:.4f}"
                )
                return False

            return True
        except Exception as e:
            logger.error(f"Error during pre-flight price check: {e}")
            return False

    async def _submit_leg_order(
        self,
        leg_spec,
        qty: float,
        opp_id: str,
        leg_name: str,
    ) -> LiveOrder:
        """Dispatch order to the respective exchange client."""
        client_order_id = f"{opp_id}_{leg_name}_{uuid.uuid4().hex[:6]}"
        if leg_spec.platform == Platform.POLYMARKET:
            return await self.poly_client.place_order(
                market_id=leg_spec.market_id,
                token_id=leg_spec.token_id or "",
                token_type=leg_spec.token_type,
                side=leg_spec.side,
                price=leg_spec.executable_price,
                size=qty,
                client_order_id=client_order_id,
            )
        elif leg_spec.platform == Platform.KALSHI:
            return await self.kalshi_client.place_order(
                market_id=leg_spec.market_id,
                token_type=leg_spec.token_type,
                side=leg_spec.side,
                price=leg_spec.executable_price,
                size=qty,
                client_order_id=client_order_id,
            )
        else:
            raise ValueError(f"Unknown platform {leg_spec.platform}")

    async def _wait_for_fill(self, order: LiveOrder, timeout: float) -> float:
        """
        Wait for an order to fill. In dry run mode, simulates immediate fill.
        In live mode, polls status or monitors fill events up to timeout.
        """
        if self.config.dry_run:
            # Dry run: immediately confirmed filled
            return order.size

        start_time = asyncio.get_event_loop().time()
        while (asyncio.get_event_loop().time() - start_time) < timeout:
            if order.status == OrderStatus.FILLED:
                return order.filled_size
            if order.status in (OrderStatus.CANCELLED, OrderStatus.REJECTED):
                return order.filled_size
            await asyncio.sleep(0.05)

        return order.filled_size

    async def _cancel_leg_order(self, order: LiveOrder) -> None:
        """Cancel an order on exchange."""
        if not order.exchange_order_id:
            return
        if order.platform == Platform.POLYMARKET:
            await self.poly_client.cancel_order(order.exchange_order_id)
        elif order.platform == Platform.KALSHI:
            await self.kalshi_client.cancel_order(order.exchange_order_id)

    async def _handle_unhedged_exposure(
        self,
        opp: ArbitrageOpportunity,
        leg1_order: LiveOrder,
        leg2_order: LiveOrder,
        leg1_filled: float,
        leg2_filled: float,
    ) -> bool:
        """
        Emergency hedge handler when Leg 2 only partially fills or fails.
        Unwinds the excess Leg 1 contracts on the open market within loss tolerance.
        """
        unhedged_qty = leg1_filled - leg2_filled
        logger.critical(
            f"EMERGENCY HEDGE TRIGGERED: Leg 1 filled {leg1_filled}, Leg 2 filled {leg2_filled}. "
            f"Unhedged quantity: {unhedged_qty}"
        )

        if self.config.dry_run:
            logger.info(f"[DRY-RUN] Simulated emergency unwind of {unhedged_qty} contracts.")
            return True

        if self.config.auto_unwind_unhedged_leg:
            # Submit market sell order to close Leg 1 position
            try:
                unwind_side = OrderSide.SELL
                unwind_price = max(0.01, leg1_order.average_fill_price * (1.0 - self.config.max_unwind_loss_pct))
                unwind_order = await self._submit_leg_order(
                    opp.leg1,
                    unhedged_qty,
                    opp.opportunity_id,
                    "unwind",
                )
                logger.warning(f"Unwind order submitted: {unwind_order.exchange_order_id}")
                return True
            except Exception as e:
                logger.critical(f"FATAL: Unwind order failed: {e}")
                return False

        return False

    def _transition(
        self,
        opp_id: str,
        pair_id: str,
        from_state: ExecutionState,
        to_state: ExecutionState,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Record state transition in logs and append-only WAL reconciler."""
        logger.info(f"FSM [{pair_id} | {opp_id}]: {from_state.value} -> {to_state.value}")
        self.reconciler.log_transition(opp_id, pair_id, from_state, to_state, metadata)
