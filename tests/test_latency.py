"""
Unit Tests for Latency Tracking, Quote Decay Gates, and Dashboard Telemetry.
Validates that network latency issues are strictly modeled, measured, and gated.
"""

from datetime import datetime, timezone, timedelta
import pytest

from src.models import (
    Platform,
    NormalizedMarket,
    NormalizedOrderBook,
    PriceLevel,
    MatchedMarketPair,
    TokenType,
    OrderSide,
)
from src.config import ArbitrageGateConfig
from src.math.fee_calculator import FeeCalculator
from src.math.ev_calculator import EVCalculator
from src.dashboard.state import DashboardState


@pytest.fixture
def sample_pair():
    now = datetime.now(timezone.utc)
    res_time = now + timedelta(minutes=10)
    poly = NormalizedMarket(
        platform=Platform.POLYMARKET,
        market_id="poly_btc_15m_test",
        event_id="btc-updown-15m-1",
        title="Bitcoin Up or Down - 15m",
        description="",
        category="CRYPTO",
        resolution_time=res_time,
        settlement_source="Chainlink TWAP",
        yes_token_id="tok_up",
        no_token_id="tok_down",
    )
    kalshi = NormalizedMarket(
        platform=Platform.KALSHI,
        market_id="KXBTC15M-TEST",
        event_id="KXBTC15M",
        title="BTC price up in next 15 mins?",
        description="",
        category="CRYPTO",
        resolution_time=res_time,
        settlement_source="CF Benchmarks",
    )
    return MatchedMarketPair(
        pair_id="poly:test|kalshi:test",
        poly_market=poly,
        kalshi_market=kalshi,
        underlying_entity="CRYPTO_BTC_15M",
        target_metric="UP_OR_DOWN_15M",
        strike_value=85000.0,
        resolution_time=res_time,
        match_confidence=1.0,
        verified=True,
    )


def test_dynamic_latency_penalty_widens_margin(sample_pair):
    """
    Test that high network latency adds a dynamic friction buffer,
    protecting against quote decay and execution lag.
    """
    now = datetime.now(timezone.utc)
    cfg = ArbitrageGateConfig(
        min_net_edge_pct=0.01,
        min_net_profit_dollars=0.20,
        max_hours_to_resolution=24.0,
        min_minutes_to_resolution=1.0,
        max_quote_age_seconds=2.0,
    )
    fees = FeeCalculator(poly_taker_fee_pct=0.0, kalshi_taker_multiplier=0.07, kalshi_fee_cap=0.02)
    ev_calc = EVCalculator(cfg, fees)

    # Clean profitable spread: Poly YES Ask 0.46, Kalshi NO Ask 0.46 -> Gross $0.92 (8% gross edge)
    poly_ob = NormalizedOrderBook(
        platform=Platform.POLYMARKET,
        market_id="poly",
        timestamp=now,
        yes_asks=[PriceLevel(price=0.46, size=100.0)],
        no_asks=[PriceLevel(price=0.54, size=100.0)],
    )
    kalshi_ob = NormalizedOrderBook(
        platform=Platform.KALSHI,
        market_id="kalshi",
        timestamp=now,
        yes_asks=[PriceLevel(price=0.54, size=100.0)],
        no_asks=[PriceLevel(price=0.46, size=100.0)],
    )

    # 1. Normal low latency (30ms) -> Trade passes gate
    opp_low_latency = ev_calc.evaluate_pair(
        sample_pair, poly_ob, kalshi_ob, now=now, latency_ms=30.0
    )
    assert opp_low_latency is not None
    assert opp_low_latency.latency_buffer_total == 0.0
    profit_low = opp_low_latency.net_profit

    # 2. Elevated network latency (250ms) -> Dynamic latency buffer penalizes net profit
    opp_high_latency = ev_calc.evaluate_pair(
        sample_pair, poly_ob, kalshi_ob, now=now, latency_ms=250.0
    )
    assert opp_high_latency is not None
    assert opp_high_latency.latency_buffer_total > 0.0
    # Profit must be strictly lower due to latency penalty
    assert opp_high_latency.net_profit < profit_low

    # 3. Severe latency spike (2500ms / 2.5s lag) -> Trade is strictly rejected
    opp_severe = ev_calc.evaluate_pair(
        sample_pair, poly_ob, kalshi_ob, now=now, latency_ms=2500.0
    )
    assert opp_severe is None, "Severe latency must reject the trade to preserve capital"



def test_stale_quote_rejection_at_threshold(sample_pair):
    """
    Test that quotes older than max_quote_age_seconds (2.0s) are immediately rejected.
    """
    now = datetime.now(timezone.utc)
    stale_time = now - timedelta(seconds=2.5)  # 2.5s old (exceeds 2.0s limit)

    cfg = ArbitrageGateConfig(max_quote_age_seconds=2.0)
    fees = FeeCalculator()
    ev_calc = EVCalculator(cfg, fees)

    poly_ob = NormalizedOrderBook(
        platform=Platform.POLYMARKET,
        market_id="poly",
        timestamp=stale_time,  # STALE
        yes_asks=[PriceLevel(price=0.45, size=50.0)],
        no_asks=[PriceLevel(price=0.55, size=50.0)],
    )
    kalshi_ob = NormalizedOrderBook(
        platform=Platform.KALSHI,
        market_id="kalshi",
        timestamp=now,
        yes_asks=[PriceLevel(price=0.55, size=50.0)],
        no_asks=[PriceLevel(price=0.45, size=50.0)],
    )

    opp = ev_calc.evaluate_pair(sample_pair, poly_ob, kalshi_ob, now=now)
    assert opp is None, "Stale quote (> 2.0s) must be rejected"


def test_dashboard_state_telemetry():
    """Verify DashboardState thread-safe updates and serialization."""
    dash = DashboardState()
    dash.telemetry.kalshi_latency_ms = 35.4
    dash.telemetry.poly_latency_ms = 118.2
    dash.telemetry.scan_frequency_hz = 2.4
    dash.record_opportunity({
        "time": "05:45:01",
        "direction": "POLY YES + KALSHI NO",
        "gross_cost": 0.965,
        "net_profit": 1.25,
        "net_edge": 0.025,
        "size": 50.0,
        "status": "DETECTED",
    })
    dash.log_message("Engine scan started")

    data = dash.to_dict()
    assert data["kalshi_latency_ms"] == 35.4
    assert data["poly_latency_ms"] == 118.2
    assert len(data["recent_opportunities"]) == 1
    assert len(data["recent_logs"]) == 1
    assert data["uptime_seconds"] >= 0.0
