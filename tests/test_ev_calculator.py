"""
Comprehensive unit tests for fee calculation and conservative EV gating.
Verifies all boundary conditions, fee erosion, slippage elimination, and conservative rounding.
"""

import pytest
from datetime import datetime, timezone, timedelta

from src.models import (
    Platform,
    TokenType,
    PriceLevel,
    NormalizedOrderBook,
    NormalizedMarket,
    MatchedMarketPair,
)
from src.config import ArbitrageGateConfig
from src.math.fee_calculator import FeeCalculator
from src.math.ev_calculator import EVCalculator


@pytest.fixture
def fee_calc():
    return FeeCalculator(
        poly_taker_fee_pct=0.0,
        kalshi_taker_multiplier=0.07,
        kalshi_fee_cap=0.02,
        fixed_gas_per_poly_order=0.00,
    )


@pytest.fixture
def gate_config():
    return ArbitrageGateConfig(
        min_net_edge_pct=0.015,         # 1.5%
        min_net_profit_dollars=0.25,     # $0.25
        max_hours_to_resolution=24.0,
        min_minutes_to_resolution=5.0,
        max_quote_age_seconds=2.0,
        slippage_buffer_per_leg=0.005,  # 0.5%
        adverse_leg_buffer_cents=0.005, # $0.005
        annual_capital_cost_rate=0.05,
    )


@pytest.fixture
def ev_calc(gate_config, fee_calc):
    return EVCalculator(config=gate_config, fee_calculator=fee_calc)


@pytest.fixture
def sample_market_pair():
    now = datetime.now(timezone.utc)
    res_time = now + timedelta(hours=6)
    
    poly_m = NormalizedMarket(
        platform=Platform.POLYMARKET,
        market_id="poly_btc_86k",
        event_id="poly_btc_event",
        title="Will Bitcoin be above $86,000 on Oct 1?",
        description="",
        category="CRYPTO",
        resolution_time=res_time,
        settlement_source="CF Benchmarks",
        yes_token_id="poly_token_yes_123",
        no_token_id="poly_token_no_123",
    )
    
    kalshi_m = NormalizedMarket(
        platform=Platform.KALSHI,
        market_id="KXBTCD-26OCT0117-T85999.99",
        event_id="KXBTCD-26OCT0117",
        title="Bitcoin price on Oct 1, 2026? $86,000 or above",
        description="",
        category="CRYPTO",
        resolution_time=res_time,
        settlement_source="CF Benchmarks",
    )
    
    return MatchedMarketPair(
        pair_id="poly_btc_86k|KXBTCD-26OCT0117-T85999.99",
        poly_market=poly_m,
        kalshi_market=kalshi_m,
        underlying_entity="BTC",
        target_metric="PRICE_AT_EXPIRATION",
        strike_value=86000.0,
        resolution_time=res_time,
        match_confidence=1.0,
        verified=True,
    )


def test_kalshi_fee_formula_and_ceiling(fee_calc):
    # Quadratic formula: ceil(0.07 * P * (1-P) * 100) / 100
    # At P = 0.50: 0.07 * 0.25 = 0.0175 -> ceil to 2 cents = 0.02
    fee_50 = fee_calc.calculate_kalshi_fee_per_contract(0.50)
    assert fee_50 == 0.02

    # At P = 0.10: 0.07 * 0.10 * 0.90 = 0.0063 -> ceil to 1 cent = 0.01
    fee_10 = fee_calc.calculate_kalshi_fee_per_contract(0.10)
    assert fee_10 == 0.01

    # Maker fee is strictly zero
    maker_fee = fee_calc.calculate_kalshi_fee_per_contract(0.50, is_taker=False)
    assert maker_fee == 0.0

    # Total fee for 100 contracts at P = 0.10
    total_100 = fee_calc.calculate_kalshi_total_fee(0.10, 100)
    assert total_100 == 1.00


def test_clearly_profitable_arbitrage(ev_calc, sample_market_pair):
    now = datetime.now(timezone.utc)
    
    # Poly: Buy YES at $0.40 (size: 50)
    poly_ob = NormalizedOrderBook(
        platform=Platform.POLYMARKET,
        market_id="poly_btc_86k",
        timestamp=now,
        yes_asks=[PriceLevel(price=0.40, size=50.0)],
        no_asks=[PriceLevel(price=0.62, size=50.0)],
    )
    
    # Kalshi: Buy NO at $0.52 (size: 60)
    # Gross cost = 0.40 + 0.52 = 0.92 -> Gross edge = 0.08 (8 cents per contract!)
    kalshi_ob = NormalizedOrderBook(
        platform=Platform.KALSHI,
        market_id="KXBTCD-26OCT0117-T85999.99",
        timestamp=now,
        yes_asks=[PriceLevel(price=0.50, size=60.0)],
        no_asks=[PriceLevel(price=0.52, size=60.0)],
    )

    opp = ev_calc.evaluate_pair(sample_market_pair, poly_ob, kalshi_ob, now=now)
    assert opp is not None
    assert opp.is_positive_ev
    assert opp.executable_quantity == 50.0  # Limited by Poly size
    assert opp.gross_cost_per_unit == 0.92
    assert opp.gross_edge_per_unit == pytest.approx(0.08, abs=1e-5)
    assert opp.net_profit > 0.25  # Clears dollar profit hurdle
    assert opp.net_edge_pct > 0.015  # Clears 1.5% edge hurdle


def test_fee_and_friction_erodes_micro_spread(ev_calc, sample_market_pair):
    now = datetime.now(timezone.utc)
    
    # Gross spread looks positive: 0.49 + 0.50 = 0.99 (1 cent gross profit per share)
    # BUT Kalshi fee alone is ~0.02 per share, plus slippage buffer!
    poly_ob = NormalizedOrderBook(
        platform=Platform.POLYMARKET,
        market_id="poly_btc_86k",
        timestamp=now,
        yes_asks=[PriceLevel(price=0.49, size=100.0)],
        no_asks=[PriceLevel(price=0.53, size=100.0)],
    )
    kalshi_ob = NormalizedOrderBook(
        platform=Platform.KALSHI,
        market_id="KXBTCD-26OCT0117-T85999.99",
        timestamp=now,
        yes_asks=[PriceLevel(price=0.52, size=100.0)],
        no_asks=[PriceLevel(price=0.50, size=100.0)],
    )

    # Must be REJECTED by the hard EV gate
    opp = ev_calc.evaluate_pair(sample_market_pair, poly_ob, kalshi_ob, now=now)
    assert opp is None, "A 1-cent gross spread must be rejected due to negative net EV after fees and slippage"


def test_stale_data_rejection(ev_calc, sample_market_pair):
    now = datetime.now(timezone.utc)
    stale_time = now - timedelta(seconds=5.0)  # 5 seconds old (max is 2.0s)

    poly_ob = NormalizedOrderBook(
        platform=Platform.POLYMARKET,
        market_id="poly_btc_86k",
        timestamp=stale_time,
        yes_asks=[PriceLevel(price=0.35, size=100.0)],
        no_asks=[PriceLevel(price=0.65, size=100.0)],
    )
    kalshi_ob = NormalizedOrderBook(
        platform=Platform.KALSHI,
        market_id="KXBTCD-26OCT0117-T85999.99",
        timestamp=now,
        yes_asks=[PriceLevel(price=0.60, size=100.0)],
        no_asks=[PriceLevel(price=0.55, size=100.0)],
    )

    opp = ev_calc.evaluate_pair(sample_market_pair, poly_ob, kalshi_ob, now=now)
    assert opp is None, "Must reject stale quote older than 2.0s"


def test_expiration_horizon_filtering(gate_config, fee_calc, sample_market_pair):
    now = datetime.now(timezone.utc)
    ev_calc = EVCalculator(config=gate_config, fee_calculator=fee_calc)

    # 1. Market resolving in 36 hours (exceeds 24h limit)
    sample_market_pair.resolution_time = now + timedelta(hours=36)
    poly_ob = NormalizedOrderBook(
        platform=Platform.POLYMARKET,
        market_id="poly_btc_86k",
        timestamp=now,
        yes_asks=[PriceLevel(price=0.35, size=100.0)],
        no_asks=[PriceLevel(price=0.65, size=100.0)],
    )
    kalshi_ob = NormalizedOrderBook(
        platform=Platform.KALSHI,
        market_id="KXBTCD-26OCT0117-T85999.99",
        timestamp=now,
        yes_asks=[PriceLevel(price=0.60, size=100.0)],
        no_asks=[PriceLevel(price=0.55, size=100.0)],
    )
    opp_long = ev_calc.evaluate_pair(sample_market_pair, poly_ob, kalshi_ob, now=now)
    assert opp_long is None, "Must filter out markets resolving in > 24 hours"

    # 2. Market resolving in 2 minutes (< 5 min chaotic window)
    sample_market_pair.resolution_time = now + timedelta(minutes=2)
    opp_short = ev_calc.evaluate_pair(sample_market_pair, poly_ob, kalshi_ob, now=now)
    assert opp_short is None, "Must filter out markets resolving in < 5 minutes"


def test_insufficient_liquidity(ev_calc, sample_market_pair):
    now = datetime.now(timezone.utc)
    # Only 1 contract available on Kalshi, total net profit cannot reach $0.25 threshold
    poly_ob = NormalizedOrderBook(
        platform=Platform.POLYMARKET,
        market_id="poly_btc_86k",
        timestamp=now,
        yes_asks=[PriceLevel(price=0.40, size=100.0)],
        no_asks=[PriceLevel(price=0.60, size=100.0)],
    )
    kalshi_ob = NormalizedOrderBook(
        platform=Platform.KALSHI,
        market_id="KXBTCD-26OCT0117-T85999.99",
        timestamp=now,
        yes_asks=[PriceLevel(price=0.60, size=1.0)],
        no_asks=[PriceLevel(price=0.52, size=1.0)],  # Only 1 contract
    )

    opp = ev_calc.evaluate_pair(sample_market_pair, poly_ob, kalshi_ob, now=now)
    assert opp is None, "Must reject trade when available size produces profit below minimum dollar threshold"
