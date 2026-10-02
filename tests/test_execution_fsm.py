"""
Unit tests for Two-Leg Execution FSM, Risk Manager, and Crash Recovery.
Validates state machine transitions, partial fills, pair lock concurrency,
and crash recovery journaling.
"""

import pytest
import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path

from src.models import (
    Platform,
    TokenType,
    OrderSide,
    OrderStatus,
    ExecutionState,
    NormalizedMarket,
    MatchedMarketPair,
    ArbitrageOpportunity,
    ArbitrageLegSpec,
    LiveOrder,
)
from src.config import RiskConfig, ExecutionConfig, APIConfig
from src.execution.risk_manager import RiskManager
from src.execution.reconciler import ExecutionReconciler
from src.execution.leg_risk_fsm import LegRiskFSM
from src.clients.kalshi_client import KalshiClient
from src.clients.polymarket_client import PolymarketClient


@pytest.fixture
def risk_manager():
    cfg = RiskConfig(
        max_order_size_dollars=50.0,
        min_order_size_dollars=0.01,
        max_contracts_per_trade=10.0,
        max_market_exposure_dollars=100.0,
        max_total_exposure_dollars=200.0,
        max_daily_loss_dollars=20.0,
        max_consecutive_failures=3,
        kill_switch_enabled=True,
    )
    return RiskManager(cfg)


@pytest.fixture
def temp_journal(tmp_path):
    return str(tmp_path / "test_journal.jsonl")


@pytest.fixture
def reconciler(temp_journal):
    return ExecutionReconciler(journal_path=temp_journal)


@pytest.fixture
def sample_opp():
    now = datetime.now(timezone.utc)
    pm = NormalizedMarket(
        platform=Platform.POLYMARKET,
        market_id="poly_m1",
        event_id="e1",
        title="Sample Poly Market",
        description="",
        category="CRYPTO",
        resolution_time=now + timedelta(hours=5),
        settlement_source="CF",
        yes_token_id="poly_token_yes",
        no_token_id="poly_token_no",
    )
    km = NormalizedMarket(
        platform=Platform.KALSHI,
        market_id="kalshi_m1",
        event_id="e2",
        title="Sample Kalshi Market",
        description="",
        category="CRYPTO",
        resolution_time=now + timedelta(hours=5),
        settlement_source="CF",
    )
    pair = MatchedMarketPair(
        pair_id="poly_m1|kalshi_m1",
        poly_market=pm,
        kalshi_market=km,
        underlying_entity="BTC",
        target_metric="PRICE",
        resolution_time=now + timedelta(hours=5),
    )
    leg1 = ArbitrageLegSpec(
        platform=Platform.POLYMARKET,
        market_id="poly_m1",
        token_type=TokenType.YES,
        side=OrderSide.BUY,
        executable_price=0.40,
        available_size=20.0,
        expected_fee=0.0,
        token_id="poly_token_yes",
    )
    leg2 = ArbitrageLegSpec(
        platform=Platform.KALSHI,
        market_id="kalshi_m1",
        token_type=TokenType.NO,
        side=OrderSide.BUY,
        executable_price=0.52,
        available_size=20.0,
        expected_fee=0.40,
        token_id="kalshi_m1",
    )
    return ArbitrageOpportunity(
        opportunity_id="test_opp_123",
        market_pair=pair,
        leg1=leg1,
        leg2=leg2,
        executable_quantity=20.0,
        gross_cost_per_unit=0.92,
        gross_payout_per_unit=1.00,
        gross_edge_per_unit=0.08,
        poly_fee_total=0.0,
        kalshi_fee_total=0.40,
        slippage_buffer_total=0.10,
        capital_cost_total=0.01,
        adverse_buffer_total=0.10,
        total_costs=0.61,
        net_profit=0.99,
        net_edge_pct=0.05,
        annualized_return=80.0,
        hours_to_resolution=5.0,
        detected_at=now,
    )


@pytest.mark.asyncio
async def test_pair_locking_prevents_duplicate_orders(risk_manager):
    pair_id = "poly_btc|kalshi_btc"
    
    # First acquisition succeeds
    acq1 = await risk_manager.acquire_pair_lock(pair_id)
    assert acq1 is True

    # Second acquisition for same pair is denied (concurrency protection)
    acq2 = await risk_manager.acquire_pair_lock(pair_id)
    assert acq2 is False

    # After release, acquisition succeeds
    await risk_manager.release_pair_lock(pair_id)
    acq3 = await risk_manager.acquire_pair_lock(pair_id)
    assert acq3 is True
    await risk_manager.release_pair_lock(pair_id)


def test_daily_loss_triggers_kill_switch(risk_manager):
    assert risk_manager.kill_switch_active is False

    # Record trade loss of $10
    risk_manager.record_trade_result(-10.0, 50.0, "p1")
    assert risk_manager.kill_switch_active is False
    assert risk_manager.daily_realized_loss == 10.0

    # Record second trade loss of $12 (total $22 > $20 max daily loss)
    risk_manager.record_trade_result(-12.0, 50.0, "p1")
    assert risk_manager.kill_switch_active is True
    assert "Daily loss" in risk_manager.kill_switch_reason


def test_crash_recovery_journal(reconciler, temp_journal):
    # Log an in-progress trade
    reconciler.log_transition(
        opportunity_id="opp_crash_1",
        pair_id="pair_1",
        from_state=ExecutionState.VALIDATING,
        to_state=ExecutionState.LEG1_SUBMITTED,
        metadata={"size": 10.0},
    )

    # Log a completed trade
    reconciler.log_transition(
        opportunity_id="opp_done_2",
        pair_id="pair_2",
        from_state=ExecutionState.LEG2_SUBMITTED,
        to_state=ExecutionState.COMPLETED,
        metadata={"profit": 1.50},
    )

    # Create new reconciler reading the same file (simulating bot restart after crash)
    new_reconciler = ExecutionReconciler(journal_path=temp_journal)
    unresolved = new_reconciler.recover_on_startup()

    # opp_crash_1 should be flagged as UNRESOLVED
    assert len(unresolved) == 1
    assert unresolved[0]["opportunity_id"] == "opp_crash_1"
    assert unresolved[0]["to_state"] == ExecutionState.LEG1_SUBMITTED.value


@pytest.mark.asyncio
async def test_dry_run_fsm_execution(risk_manager, reconciler, sample_opp):
    api_cfg = APIConfig()
    exec_cfg = ExecutionConfig(dry_run=True, live_trading_confirmed=False)

    poly_client = PolymarketClient(api_cfg, dry_run=True)
    kalshi_client = KalshiClient(api_cfg, dry_run=True)

    fsm = LegRiskFSM(
        config=exec_cfg,
        poly_client=poly_client,
        kalshi_client=kalshi_client,
        risk_manager=risk_manager,
        reconciler=reconciler,
    )

    # In dry run mode, both legs fill completely
    success, final_state, msg = await fsm.execute_opportunity(sample_opp)
    assert success is True
    assert final_state == ExecutionState.COMPLETED
    assert "successfully completed" in msg


@pytest.mark.asyncio
async def test_partial_fill_hedging(risk_manager, reconciler, sample_opp):
    api_cfg = APIConfig()
    exec_cfg = ExecutionConfig(dry_run=True, live_trading_confirmed=False)

    poly_client = PolymarketClient(api_cfg, dry_run=True)
    kalshi_client = KalshiClient(api_cfg, dry_run=True)

    fsm = LegRiskFSM(
        config=exec_cfg,
        poly_client=poly_client,
        kalshi_client=kalshi_client,
        risk_manager=risk_manager,
        reconciler=reconciler,
    )

    # Mock _wait_for_fill on Leg 1 to return a PARTIAL fill of 8.0 units (requested was 20.0)
    original_wait = fsm._wait_for_fill
    async def mock_wait(order, timeout):
        if "leg1" in order.client_order_id:
            order.filled_size = 8.0
            order.status = OrderStatus.PARTIALLY_FILLED
            return 8.0
        return order.size

    fsm._wait_for_fill = mock_wait

    success, final_state, msg = await fsm.execute_opportunity(sample_opp)
    assert success is True
    assert final_state == ExecutionState.COMPLETED
    assert "8.0 units" in msg, f"Expected hedge of exactly 8.0 units, got {msg}"


@pytest.mark.asyncio
async def test_leg2_failure_triggers_emergency_unwind(risk_manager, reconciler, sample_opp):
    api_cfg = APIConfig()
    exec_cfg = ExecutionConfig(dry_run=True, live_trading_confirmed=False)

    poly_client = PolymarketClient(api_cfg, dry_run=True)
    kalshi_client = KalshiClient(api_cfg, dry_run=True)

    fsm = LegRiskFSM(
        config=exec_cfg,
        poly_client=poly_client,
        kalshi_client=kalshi_client,
        risk_manager=risk_manager,
        reconciler=reconciler,
    )

    # Leg 1 fills 10 units, but Leg 2 order placement fails/raises exception
    async def mock_submit(leg_spec, qty, opp_id, leg_name, *args, **kwargs):
        if leg_name == "leg1":
            return LiveOrder(
                client_order_id=f"{opp_id}_leg1",
                platform=leg_spec.platform,
                market_id=leg_spec.market_id,
                token_type=leg_spec.token_type,
                side=leg_spec.side,
                price=leg_spec.executable_price,
                size=qty,
                status=OrderStatus.FILLED,
                filled_size=qty,
                average_fill_price=leg_spec.executable_price,
                exchange_order_id="pm_leg1_123",
            )
        elif leg_name == "leg2":
            raise RuntimeError("Kalshi simulated API connection error")
        elif leg_name == "unwind":
            assert leg_spec.side == OrderSide.SELL, "Unwind order must be SELL"
            return LiveOrder(
                client_order_id=f"{opp_id}_unwind",
                platform=leg_spec.platform,
                market_id=leg_spec.market_id,
                token_type=leg_spec.token_type,
                side=leg_spec.side,
                price=leg_spec.executable_price,
                size=qty,
                status=OrderStatus.FILLED,
                filled_size=qty,
                exchange_order_id="pm_unwind_123",
            )

    fsm._submit_leg_order = mock_submit
    success, final_state, msg = await fsm.execute_opportunity(sample_opp)
    assert final_state == ExecutionState.COMPLETED
    assert "Unhedged leg handled" in msg
