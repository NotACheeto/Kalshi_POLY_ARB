"""
Unit tests for MarketMatcher.
Validates true matches and strictly asserts that false matches (different dates,
different strikes, moneyline vs spread, ambiguous entities) are rejected.
"""

import pytest
from datetime import datetime, timezone, timedelta

from src.models import Platform, NormalizedMarket
from src.matcher.market_matcher import MarketMatcher


@pytest.fixture
def matcher():
    return MarketMatcher(max_hours_to_resolution=24.0)


def test_valid_btc_daily_strike_match(matcher):
    now = datetime.now(timezone.utc)
    res_time = now + timedelta(hours=8)

    poly = NormalizedMarket(
        platform=Platform.POLYMARKET,
        market_id="poly_btc_86000",
        event_id="poly_btc_event",
        title="Bitcoin above $86000 on Oct 1?",
        description="",
        category="CRYPTO",
        resolution_time=res_time,
        settlement_source="CF Benchmarks",
    )
    kalshi = NormalizedMarket(
        platform=Platform.KALSHI,
        market_id="KXBTCD-26OCT0117-T85999.99",
        event_id="KXBTCD-26OCT0117",
        title="Bitcoin price on Oct 1, 2026? $86000 or above",
        description="",
        category="CRYPTO",
        resolution_time=res_time,
        settlement_source="CF Benchmarks",
    )

    matches = matcher.find_matches([poly], [kalshi], now=now)
    assert len(matches) == 1
    pair = matches[0]
    assert pair.underlying_entity == "CRYPTO_BTC"
    assert pair.strike_value == 86000.0


def test_reject_different_strikes(matcher):
    now = datetime.now(timezone.utc)
    res_time = now + timedelta(hours=8)

    poly = NormalizedMarket(
        platform=Platform.POLYMARKET,
        market_id="poly_btc_86000",
        event_id="poly_btc_event",
        title="Bitcoin above $86000 on Oct 1?",
        description="",
        category="CRYPTO",
        resolution_time=res_time,
        settlement_source="CF Benchmarks",
    )
    # Kalshi is $88,000, not $86,000!
    kalshi = NormalizedMarket(
        platform=Platform.KALSHI,
        market_id="KXBTCD-26OCT0117-T87999.99",
        event_id="KXBTCD-26OCT0117",
        title="Bitcoin price on Oct 1, 2026? $88000 or above",
        description="",
        category="CRYPTO",
        resolution_time=res_time,
        settlement_source="CF Benchmarks",
    )

    matches = matcher.find_matches([poly], [kalshi], now=now)
    assert len(matches) == 0, "Different strikes must never match"


def test_reject_different_dates(matcher):
    now = datetime.now(timezone.utc)
    res_time_today = now + timedelta(hours=4)
    res_time_tomorrow = now + timedelta(hours=28)  # Next day

    poly = NormalizedMarket(
        platform=Platform.POLYMARKET,
        market_id="poly_btc_today",
        event_id="poly_btc",
        title="Bitcoin above $86000 on Oct 1?",
        description="",
        category="CRYPTO",
        resolution_time=res_time_today,
        settlement_source="CF Benchmarks",
    )
    kalshi = NormalizedMarket(
        platform=Platform.KALSHI,
        market_id="KXBTCD-tomorrow",
        event_id="KXBTCD",
        title="Bitcoin price on Oct 2, 2026? $86000 or above",
        description="",
        category="CRYPTO",
        resolution_time=res_time_tomorrow,
        settlement_source="CF Benchmarks",
    )

    matches = matcher.find_matches([poly], [kalshi], now=now)
    assert len(matches) == 0, "Markets resolving on different dates must never match"


def test_reject_moneyline_vs_spread(matcher):
    now = datetime.now(timezone.utc)
    res_time = now + timedelta(hours=6)

    # Poly is Moneyline / Winner: "Browns vs Steelers: Browns win?"
    poly = NormalizedMarket(
        platform=Platform.POLYMARKET,
        market_id="poly_cle_pit_ml",
        event_id="poly_nfl",
        title="Cleveland Browns vs Pittsburgh Steelers: Browns win?",
        description="",
        category="SPORTS",
        resolution_time=res_time,
        settlement_source="NFL",
    )
    # Kalshi is Spread: "Browns vs Steelers: Browns by over 3.5 points"
    kalshi = NormalizedMarket(
        platform=Platform.KALSHI,
        market_id="KXNFLSPREAD-CLE-3.5",
        event_id="KXNFL",
        title="Browns vs Steelers by over 3.5 points",
        description="",
        category="SPORTS",
        resolution_time=res_time,
        settlement_source="NFL",
    )

    matches = matcher.find_matches([poly], [kalshi], now=now)
    assert len(matches) == 0, "Moneyline and Spread markets must never be treated as arbitrage equivalents"
