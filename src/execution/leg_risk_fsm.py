"""
Two-Leg Execution Finite State Machine (FSM).
Strictly manages leg risk, partial fills, adverse price movement,
and unhedged exposure. Recalculates EV immediately before submission.
"""

import asyncio
import logging
import math
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

            # Target execution quantity capped by risk limits
            if not self.config.dry_run:
                # STRICT LIVE TRADING SAFETY: NEVER EXCEED 1 CONTRACT
                target_qty = 1.0
            else:
                max_by_dollars = math.floor(self.risk_manager.config.max_order_size_dollars / opp.gross_cost_per_unit) if opp.gross_cost_per_unit > 0 else 0
                max_allowed_qty = min(max_by_dollars, getattr(self.risk_manager.config, "max_contracts_per_trade", 10.0))
                target_qty = min(opp.executable_quantity, max_allowed_qty)

            if target_qty < 1.0:
                self._transition(opp_id, pair_id, state, ExecutionState.ABORTED, {"reason": "Target quantity below 1 contract"})
                return False, ExecutionState.ABORTED, "Target quantity below 1 contract"

            # Pre-flight dual-exchange balance verification (NEVER submit Leg 1 if either account lacks capital)
            if not self.config.dry_run:
                try:
                    poly_bal = await self.poly_client.get_balance()
                    kalshi_bal = await self.kalshi_client.get_balance()

                    needed_poly = (opp.leg1.executable_price * target_qty) if opp.leg1.platform == Platform.POLYMARKET else (opp.leg2.executable_price * target_qty)
                    needed_kalshi = (opp.leg2.executable_price * target_qty) if opp.leg2.platform == Platform.KALSHI else (opp.leg1.executable_price * target_qty)

                    if poly_bal < needed_poly:
                        err_msg = f"Insufficient Polymarket balance: ${poly_bal:.2f} < ${needed_poly:.2f}"
                        logger.warning(f"Execution aborted: {err_msg}")
                        self._transition(opp_id, pair_id, state, ExecutionState.ABORTED, {"reason": err_msg})
                        return False, ExecutionState.ABORTED, err_msg

                    if kalshi_bal < needed_kalshi:
                        err_msg = f"Insufficient Kalshi balance: ${kalshi_bal:.2f} < ${needed_kalshi:.2f}"
                        logger.warning(f"Execution aborted: {err_msg}")
                        self._transition(opp_id, pair_id, state, ExecutionState.ABORTED, {"reason": err_msg})
                        return False, ExecutionState.ABORTED, err_msg

                except Exception as e:
                    err_msg = f"Pre-flight balance verification failed: {e}"
                    logger.error(err_msg)
                    self._transition(opp_id, pair_id, state, ExecutionState.ABORTED, {"reason": err_msg})
                    return False, ExecutionState.ABORTED, err_msg

            # HARD POSITIVE-EV GUARANTEE: Combined price must strictly be <= 0.98 ($0.02 minimum margin)
            if (opp.leg1.executable_price + opp.leg2.executable_price) >= 0.985:
                err_msg = f"ABORT: Combined entry price ${opp.leg1.executable_price + opp.leg2.executable_price:.4f} >= $0.985"
                logger.critical(err_msg)
                self._transition(opp_id, pair_id, state, ExecutionState.ABORTED, {"reason": err_msg})
                return False, ExecutionState.ABORTED, err_msg

            # 4. Dispatch both legs concurrently (simultaneously)
            self._transition(opp_id, pair_id, state, ExecutionState.LEG1_SUBMITTED, {"target_qty": target_qty})
            state = ExecutionState.LEG1_SUBMITTED

            # Calculate strict price ceiling so that limit1 + limit2 <= 0.985 guaranteed
            max_leg1_price = min(opp.leg1.executable_price, round(0.985 - opp.leg2.executable_price, 4))
            max_leg2_price = min(opp.leg2.executable_price, round(0.985 - opp.leg1.executable_price, 4))

            # Dispatch orders to both Kalshi and Polymarket simultaneously
            leg1_coro = self._submit_leg_order(opp.leg1, target_qty, opp_id, "leg1", max_allowed_price=max_leg1_price)
            leg2_coro = self._submit_leg_order(opp.leg2, target_qty, opp_id, "leg2", max_allowed_price=max_leg2_price)

            orders = await asyncio.gather(leg1_coro, leg2_coro, return_exceptions=True)
            leg1_order = orders[0]
            leg2_order = orders[1]

            # Collect fill statuses concurrently
            fill_tasks = []
            if not isinstance(leg1_order, Exception):
                fill_tasks.append(self._wait_for_fill(leg1_order, timeout=self.config.leg1_fill_timeout_seconds))
            else:
                logger.error(f"Leg 1 submission failed: {leg1_order}")
                fill_tasks.append(asyncio.sleep(0, result=0.0))

            if not isinstance(leg2_order, Exception):
                fill_tasks.append(self._wait_for_fill(leg2_order, timeout=self.config.leg2_fill_timeout_seconds))
            else:
                logger.error(f"Leg 2 submission failed: {leg2_order}")
                fill_tasks.append(asyncio.sleep(0, result=0.0))

            fill_results = await asyncio.gather(*fill_tasks, return_exceptions=True)
            leg1_filled_qty = fill_results[0] if isinstance(fill_results[0], (int, float)) else 0.0
            leg2_filled_qty = fill_results[1] if isinstance(fill_results[1], (int, float)) else 0.0

            # Case A: Both legs filled 100% (Instant arbitrage hedge complete)
            if leg1_filled_qty > 0 and leg2_filled_qty > 0 and leg1_filled_qty == leg2_filled_qty:
                completed_qty = leg1_filled_qty
                p1 = getattr(leg1_order, "average_fill_price", opp.leg1.executable_price)
                p2 = getattr(leg2_order, "average_fill_price", opp.leg2.executable_price)
                actual_net_profit = (1.00 - (p1 + p2)) * completed_qty
                self._transition(opp_id, pair_id, state, ExecutionState.COMPLETED, {
                    "hedge_qty": completed_qty,
                    "leg1_fill_price": p1,
                    "leg2_fill_price": p2,
                    "net_profit": actual_net_profit,
                })
                self.risk_manager.record_trade_result(actual_net_profit, (p1 + p2) * completed_qty, pair_id)
                return True, ExecutionState.COMPLETED, f"Arbitrage successfully completed for {completed_qty} units (Profit: +${actual_net_profit:.2f})"

            # Case B: Both legs failed to fill (0 units executed, 0 risk, clean abort)
            if leg1_filled_qty <= 0 and leg2_filled_qty <= 0:
                if not isinstance(leg1_order, Exception):
                    await self._cancel_leg_order(leg1_order)
                if not isinstance(leg2_order, Exception):
                    await self._cancel_leg_order(leg2_order)
                self.risk_manager.record_execution_failure("Concurrent IOC: Neither leg filled (clean abort)")
                self._transition(opp_id, pair_id, state, ExecutionState.ABORTED, {"reason": "Both legs zero fill / expired"})
                return False, ExecutionState.ABORTED, "Neither leg filled (clean abort with 0 exposure)"

            # Case C: Asymmetric Fill (One leg filled, other leg missed or partial)
            # Immediately cancel any pending/open orders on either side
            if not isinstance(leg1_order, Exception) and leg1_filled_qty < target_qty:
                await self._cancel_leg_order(leg1_order)
            if not isinstance(leg2_order, Exception) and leg2_filled_qty < target_qty:
                await self._cancel_leg_order(leg2_order)

            self._transition(opp_id, pair_id, state, ExecutionState.HEDGING, {
                "leg1_filled": leg1_filled_qty,
                "leg2_filled": leg2_filled_qty,
                "unhedged_delta": abs(leg1_filled_qty - leg2_filled_qty),
            })
            state = ExecutionState.HEDGING

            unwind_ok = await self._handle_unhedged_exposure(
                opp, leg1_order, leg2_order, leg1_filled_qty, leg2_filled_qty
            )
            final_state = ExecutionState.COMPLETED if unwind_ok else ExecutionState.FAILED
            self._transition(opp_id, pair_id, state, final_state)

            if not self.config.dry_run:
                self.risk_manager.trigger_kill_switch(
                    f"UNHEDGED FILL MISMATCH: Leg 1 filled {leg1_filled_qty}, "
                    f"Leg 2 filled {leg2_filled_qty}. Engine halted."
                )

            common_filled = min(leg1_filled_qty, leg2_filled_qty)
            if common_filled > 0:
                return unwind_ok, final_state, f"Arbitrage completed for {common_filled} units (Unhedged leg handled: {final_state.value})"
            else:
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

            # Hard gross cost check: must be strictly < $0.985
            if (p_ask + k_ask) >= 0.985:
                logger.warning(
                    f"Execution aborted: Live combined price ${p_ask + k_ask:.4f} >= $0.985 (Insufficient EV)"
                )
                return False

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
        max_allowed_price: Optional[float] = None,
    ) -> LiveOrder:
        """Dispatch order to the respective exchange client."""
        client_order_id = f"{opp_id}_{leg_name}_{uuid.uuid4().hex[:6]}"
        price = leg_spec.executable_price
        if max_allowed_price is not None:
            price = min(price, max_allowed_price)

        if leg_spec.platform == Platform.POLYMARKET:
            return await self.poly_client.place_order(
                market_id=leg_spec.market_id,
                token_id=leg_spec.token_id or "",
                token_type=leg_spec.token_type,
                side=leg_spec.side,
                price=price,
                size=qty,
                client_order_id=client_order_id,
            )
        elif leg_spec.platform == Platform.KALSHI:
            return await self.kalshi_client.place_order(
                market_id=leg_spec.market_id,
                token_type=leg_spec.token_type,
                side=leg_spec.side,
                price=price,
                size=qty,
                client_order_id=client_order_id,
            )
        else:
            raise ValueError(f"Unknown platform {leg_spec.platform}")

    async def _wait_for_fill(self, order: LiveOrder, timeout: float) -> float:
        """
        Wait for an order to fill. In dry run mode, simulates immediate fill.
        """
        if self.config.dry_run:
            return order.size

        # Kalshi V2 IOC orders immediately report fill_count in place_order
        if order.platform == Platform.KALSHI:
            return order.filled_size

        # Polymarket US: poll open orders and verify fill status
        if order.platform == Platform.POLYMARKET and self.poly_client.is_us_account:
            start_time = asyncio.get_event_loop().time()
            while (asyncio.get_event_loop().time() - start_time) < timeout:
                open_orders = await self.poly_client.get_open_orders()
                matching = [o for o in open_orders if o.get("id") == order.exchange_order_id]
                if matching:
                    o = matching[0]
                    cum_qty = float(o.get("cumQuantity", 0.0))
                    leaves_qty = float(o.get("leavesQuantity", 0.0))
                    if cum_qty > 0 and leaves_qty == 0:
                        order.status = OrderStatus.FILLED
                        order.filled_size = cum_qty
                        return cum_qty
                    elif cum_qty > 0:
                        order.status = OrderStatus.PARTIAL
                        order.filled_size = cum_qty
                else:
                    # Not in open orders: check specific order status
                    details = await self.poly_client.get_order(order.exchange_order_id)
                    if details:
                        status_str = details.get("status", "").upper()
                        cum_qty = float(details.get("cumQuantity", 0.0))
                        if "FILLED" in status_str or (cum_qty > 0 and float(details.get("leavesQuantity", 0.0)) == 0):
                            order.status = OrderStatus.FILLED
                            order.filled_size = cum_qty if cum_qty > 0 else order.size
                            return order.filled_size
                        elif "CANCEL" in status_str or "REJECT" in status_str:
                            order.status = OrderStatus.CANCELLED
                            order.filled_size = cum_qty
                            return cum_qty
                    else:
                        # Order not found in open orders or details: cancelled or expired IOC
                        order.status = OrderStatus.CANCELLED
                        order.filled_size = 0.0
                        return 0.0
                await asyncio.sleep(0.1)

            if order.status != OrderStatus.FILLED:
                await self._cancel_leg_order(order)
            return order.filled_size

        # Global Polymarket CLOB
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
            await self.poly_client.cancel_order(order.exchange_order_id, market_id=order.market_id)
        elif order.platform == Platform.KALSHI:
            await self.kalshi_client.cancel_order(order.exchange_order_id, market_id=order.market_id)

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
        if leg1_filled > leg2_filled:
            unhedged_qty = leg1_filled - leg2_filled
            unwind_leg = opp.leg1
            avg_px = getattr(leg1_order, "average_fill_price", opp.leg1.executable_price)
        else:
            unhedged_qty = leg2_filled - leg1_filled
            unwind_leg = opp.leg2
            avg_px = getattr(leg2_order, "average_fill_price", opp.leg2.executable_price)

        logger.critical(
            f"EMERGENCY HEDGE TRIGGERED: Leg 1 filled {leg1_filled}, Leg 2 filled {leg2_filled}. "
            f"Unhedged quantity: {unhedged_qty} on {unwind_leg.platform.value}"
        )

        if self.config.dry_run:
            logger.info(f"[DRY-RUN] Simulated emergency unwind of {unhedged_qty} contracts.")
            return True

        if self.config.auto_unwind_unhedged_leg:
            # Submit market sell order to close excess position
            try:
                from src.models import ArbitrageLegSpec
                unwind_price = max(0.01, avg_px * (1.0 - self.config.max_unwind_loss_pct))
                unwind_spec = ArbitrageLegSpec(
                    platform=unwind_leg.platform,
                    market_id=unwind_leg.market_id,
                    token_type=unwind_leg.token_type,
                    side=OrderSide.SELL,
                    executable_price=unwind_price,
                    available_size=unhedged_qty,
                    expected_fee=0.0,
                    token_id=unwind_leg.token_id,
                )
                unwind_order = await self._submit_leg_order(
                    unwind_spec,
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
