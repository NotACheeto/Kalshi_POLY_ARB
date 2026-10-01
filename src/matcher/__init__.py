"""
Cross-Exchange Market Matcher.

Deterministic matching of equivalent binary outcome markets across Kalshi
and Polymarket by strike price, expiry date/time, and outcome type.
"""

from src.matcher.market_matcher import MarketMatcher

__all__ = ["MarketMatcher"]
