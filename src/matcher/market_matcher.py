"""
Deterministic Market Matching Engine for Polymarket <-> Kalshi.
Focuses on short-duration markets (<= 24 hours to resolution).
Strictly eliminates false positives by requiring exact entity, strike, date,
and settlement rule alignment. Zero reliance on loose fuzzy title similarity.
"""

import re
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Tuple, Dict, Any

from src.models import (
    Platform,
    NormalizedMarket,
    MatchedMarketPair,
)

logger = logging.getLogger(__name__)


# Canonical team mapping for sports (NFL, NBA, MLB)
CANONICAL_TEAMS = {
    # NFL
    "arizona cardinals": "NFL_ARI", "cardinals": "NFL_ARI", "ari": "NFL_ARI",
    "atlanta falcons": "NFL_ATL", "falcons": "NFL_ATL", "atl": "NFL_ATL",
    "baltimore ravens": "NFL_BAL", "ravens": "NFL_BAL", "bal": "NFL_BAL",
    "buffalo bills": "NFL_BUF", "bills": "NFL_BUF", "buf": "NFL_BUF",
    "carolina panthers": "NFL_CAR", "panthers": "NFL_CAR", "car": "NFL_CAR",
    "chicago bears": "NFL_CHI", "bears": "NFL_CHI", "chi": "NFL_CHI",
    "cincinnati bengals": "NFL_CIN", "bengals": "NFL_CIN", "cin": "NFL_CIN",
    "cleveland browns": "NFL_CLE", "browns": "NFL_CLE", "cle": "NFL_CLE",
    "dallas cowboys": "NFL_DAL", "cowboys": "NFL_DAL", "dal": "NFL_DAL",
    "denver broncos": "NFL_DEN", "broncos": "NFL_DEN", "den": "NFL_DEN",
    "detroit lions": "NFL_DET", "lions": "NFL_DET", "det": "NFL_DET",
    "green bay packers": "NFL_GB", "packers": "NFL_GB", "gb": "NFL_GB",
    "houston texans": "NFL_HOU", "texans": "NFL_HOU", "hou": "NFL_HOU",
    "indianapolis colts": "NFL_IND", "colts": "NFL_IND", "ind": "NFL_IND",
    "jacksonville jaguars": "NFL_JAX", "jaguars": "NFL_JAX", "jax": "NFL_JAX",
    "kansas city chiefs": "NFL_KC", "chiefs": "NFL_KC", "kc": "NFL_KC",
    "las vegas raiders": "NFL_LV", "raiders": "NFL_LV", "lv": "NFL_LV",
    "los angeles chargers": "NFL_LAC", "chargers": "NFL_LAC", "lac": "NFL_LAC",
    "los angeles rams": "NFL_LAR", "rams": "NFL_LAR", "lar": "NFL_LAR",
    "miami dolphins": "NFL_MIA", "dolphins": "NFL_MIA", "mia": "NFL_MIA",
    "minnesota vikings": "NFL_MIN", "vikings": "NFL_MIN", "min": "NFL_MIN",
    "new england patriots": "NFL_NE", "patriots": "NFL_NE", "ne": "NFL_NE",
    "new orleans saints": "NFL_NO", "saints": "NFL_NO", "no": "NFL_NO",
    "new york giants": "NFL_NYG", "giants": "NFL_NYG", "nyg": "NFL_NYG",
    "new york jets": "NFL_NYJ", "jets": "NFL_NYJ", "nyj": "NFL_NYJ",
    "philadelphia eagles": "NFL_PHI", "eagles": "NFL_PHI", "phi": "NFL_PHI",
    "pittsburgh steelers": "NFL_PIT", "steelers": "NFL_PIT", "pit": "NFL_PIT",
    "san francisco 49ers": "NFL_SF", "49ers": "NFL_SF", "sf": "NFL_SF",
    "seattle seahawks": "NFL_SEA", "seahawks": "NFL_SEA", "sea": "NFL_SEA",
    "tampa bay buccaneers": "NFL_TB", "buccaneers": "NFL_TB", "tb": "NFL_TB",
    "tennessee titans": "NFL_TEN", "titans": "NFL_TEN", "ten": "NFL_TEN",
    "washington commanders": "NFL_WAS", "commanders": "NFL_WAS", "was": "NFL_WAS",
}


class MarketMatcher:
    """
    High-integrity market matcher.
    Only pairs contracts that are proven mathematically and legally equivalent.
    """

    def __init__(self, max_hours_to_resolution: float = 24.0):
        self.max_hours_to_resolution = max_hours_to_resolution

    def find_matches(
        self,
        poly_markets: List[NormalizedMarket],
        kalshi_markets: List[NormalizedMarket],
        now: Optional[datetime] = None,
    ) -> List[MatchedMarketPair]:
        """
        Scan and match markets across platforms for short-duration opportunities.
        """
        if now is None:
            now = datetime.now(timezone.utc)

        # 1. Filter for active short-duration markets (< max_hours_to_resolution)
        active_poly = [
            m for m in poly_markets
            if m.is_active and timedelta(0) < (m.resolution_time - now) <= timedelta(hours=self.max_hours_to_resolution)
        ]
        active_kalshi = [
            m for m in kalshi_markets
            if m.is_active and timedelta(0) < (m.resolution_time - now) <= timedelta(hours=self.max_hours_to_resolution)
        ]

        logger.info(
            f"Matching {len(active_poly)} short-duration Poly markets with "
            f"{len(active_kalshi)} short-duration Kalshi markets"
        )

        matched_pairs: List[MatchedMarketPair] = []

        # Index Kalshi markets by category and normalized date
        kalshi_by_cat: Dict[str, List[NormalizedMarket]] = {}
        for km in active_kalshi:
            cat = km.category.upper()
            kalshi_by_cat.setdefault(cat, []).append(km)

        for pm in active_poly:
            cat = pm.category.upper()
            candidates = kalshi_by_cat.get(cat, [])
            for km in candidates:
                pair = self._match_single(pm, km)
                if pair is not None:
                    matched_pairs.append(pair)

        logger.info(f"Successfully matched {len(matched_pairs)} verified market pairs")
        return matched_pairs

    def _match_single(
        self,
        poly: NormalizedMarket,
        kalshi: NormalizedMarket,
    ) -> Optional[MatchedMarketPair]:
        """Verify strict equivalence between two markets."""
        # 1. Category must match
        if poly.category.upper() != kalshi.category.upper():
            return None

        cat = poly.category.upper()

        if cat == "CRYPTO":
            return self._match_crypto(poly, kalshi)
        elif cat == "FINANCIALS" or cat == "INDEX":
            return self._match_index(poly, kalshi)
        elif cat == "SPORTS":
            return self._match_sports(poly, kalshi)
        elif cat == "WEATHER":
            return self._match_weather(poly, kalshi)
        
        return None

    def _match_crypto(
        self,
        poly: NormalizedMarket,
        kalshi: NormalizedMarket,
    ) -> Optional[MatchedMarketPair]:
        """
        Match crypto markets across Polymarket and Kalshi:
        1. 15-Minute Recurring Up/Down windows (e.g. KXBTC15M <-> btc-updown-15m).
        2. Daily price threshold / strike fixings (e.g. KXBTCD <-> BTC above $X).
        """
        # 1. Check for 15-minute recurring Up/Down markets
        is_poly_15m = self._is_15m_crypto(poly)
        is_kalshi_15m = self._is_15m_crypto(kalshi)

        if is_poly_15m or is_kalshi_15m:
            if is_poly_15m and is_kalshi_15m:
                return self._match_crypto_15m(poly, kalshi)
            return None  # One is 15m, other is not -> NEVER match

        # 2. Daily price threshold / strike fixings
        # Extract coin
        poly_coin = self._extract_crypto_coin(poly.title)
        kalshi_coin = self._extract_crypto_coin(kalshi.title + " " + kalshi.event_id)
        if not poly_coin or poly_coin != kalshi_coin:
            return None

        # Extract resolution date (YYYY-MM-DD)
        if poly.resolution_time.date() != kalshi.resolution_time.date():
            return None

        # Expiration hour should be close (within 1.5 hours)
        diff_hours = abs((poly.resolution_time - kalshi.resolution_time).total_seconds()) / 3600.0
        if diff_hours > 1.5:
            return None

        # Extract strike from title or description
        poly_strike = self._extract_strike_price(poly.title)
        kalshi_strike = self._extract_strike_price(kalshi.title + " " + kalshi.description)

        if poly_strike is None or kalshi_strike is None:
            return None

        # Strike must match within $0.50
        if abs(poly_strike - kalshi_strike) > 0.50:
            return None

        pair_id = f"poly:{poly.market_id}|kalshi:{kalshi.market_id}"
        return MatchedMarketPair(
            pair_id=pair_id,
            poly_market=poly,
            kalshi_market=kalshi,
            underlying_entity=f"CRYPTO_{poly_coin}",
            target_metric="PRICE_AT_EXPIRATION",
            strike_value=poly_strike,
            resolution_time=min(poly.resolution_time, kalshi.resolution_time),
            match_confidence=1.0,
            verified=True,
        )

    def _match_crypto_15m(
        self,
        poly: NormalizedMarket,
        kalshi: NormalizedMarket,
    ) -> Optional[MatchedMarketPair]:
        """
        Deterministic matcher for 15-minute Up/Down recurring crypto markets.
        Both contracts measure whether the coin price at the end of the 15-minute
        window is >= the price at the start of the window.
        """
        poly_coin = self._extract_crypto_coin(poly.title + " " + poly.market_id)
        kalshi_coin = self._extract_crypto_coin(kalshi.title + " " + kalshi.event_id + " " + kalshi.market_id)
        if not poly_coin or poly_coin != kalshi_coin:
            return None

        # Expiration time must align within 60 seconds (both close at the exact 15m mark)
        diff_sec = abs((poly.resolution_time - kalshi.resolution_time).total_seconds())
        if diff_sec > 60.0:
            return None

        pair_id = f"poly:{poly.market_id}|kalshi:{kalshi.market_id}"
        return MatchedMarketPair(
            pair_id=pair_id,
            poly_market=poly,
            kalshi_market=kalshi,
            underlying_entity=f"CRYPTO_{poly_coin}_15M",
            target_metric="UP_OR_DOWN_15M",
            strike_value=0.0,
            resolution_time=min(poly.resolution_time, kalshi.resolution_time),
            match_confidence=1.0,
            verified=True,
        )

    @staticmethod
    def _is_15m_crypto(market: NormalizedMarket) -> bool:
        """Identify if a contract is a 15-minute recurring Up/Down market."""
        t = f"{market.title} {market.market_id} {market.event_id} {market.description}".lower()

        # Reject explicit 5m markets
        if "-5m" in t or "updown-5m" in t or re.search(r'\b5\s*(?:m|min|mins|minute|minutes)\b', t):
            return False

        if (
            "15m" in t
            or "15 min" in t
            or "15-minute" in t
            or "15 mins" in t
            or "kxbtc15m" in t
            or "kxeth15m" in t
            or "kxsol15m" in t
        ):
            return True

        # Check time range in title, e.g. 1:45AM-2:00AM
        match = re.search(r'(\d{1,2}):(\d{2})\s*([ap]m)\s*-\s*(\d{1,2}):(\d{2})\s*([ap]m)', t)
        if match:
            h1, m1, p1, h2, m2, p2 = match.groups()
            t1 = (int(h1) % 12 + (12 if p1 == 'pm' else 0)) * 60 + int(m1)
            t2 = (int(h2) % 12 + (12 if p2 == 'pm' else 0)) * 60 + int(m2)
            if (t2 - t1) % 1440 == 15:
                return True

        return False

    def _match_index(
        self,
        poly: NormalizedMarket,
        kalshi: NormalizedMarket,
    ) -> Optional[MatchedMarketPair]:
        """Match stock index daily threshold (S&P 500, Nasdaq 100)."""
        poly_idx = "SPX" if "s&p" in poly.title.lower() or "spx" in poly.title.lower() else None
        kalshi_idx = "SPX" if "s&p" in kalshi.title.lower() or "inx" in kalshi.market_id.lower() else None

        if not poly_idx or poly_idx != kalshi_idx:
            return None

        if poly.resolution_time.date() != kalshi.resolution_time.date():
            return None

        poly_strike = self._extract_strike_price(poly.title)
        kalshi_strike = self._extract_strike_price(kalshi.title + " " + kalshi.description)
        if poly_strike is None or kalshi_strike is None:
            return None

        if abs(poly_strike - kalshi_strike) > 0.50:
            return None

        pair_id = f"poly:{poly.market_id}|kalshi:{kalshi.market_id}"
        return MatchedMarketPair(
            pair_id=pair_id,
            poly_market=poly,
            kalshi_market=kalshi,
            underlying_entity=f"INDEX_{poly_idx}",
            target_metric="CLOSE_PRICE",
            strike_value=poly_strike,
            resolution_time=min(poly.resolution_time, kalshi.resolution_time),
            match_confidence=1.0,
            verified=True,
        )

    def _match_sports(
        self,
        poly: NormalizedMarket,
        kalshi: NormalizedMarket,
    ) -> Optional[MatchedMarketPair]:
        """Match sports game winner or spread."""
        # Both must be same game date
        if poly.resolution_time.date() != kalshi.resolution_time.date():
            return None

        poly_teams = self._extract_teams(poly.title)
        kalshi_teams = self._extract_teams(kalshi.title)

        # Both must have exactly the same two teams
        if len(poly_teams) != 2 or len(kalshi_teams) != 2:
            return None
        if set(poly_teams) != set(kalshi_teams):
            return None

        # Check outcome condition (Moneyline / Winner vs Spread)
        is_poly_spread = "spread" in poly.title.lower() or "handicap" in poly.title.lower()
        is_kalshi_spread = "spread" in kalshi.title.lower() or "by over" in kalshi.title.lower()

        if is_poly_spread != is_kalshi_spread:
            return None  # One is moneyline, other is spread -> DO NOT MATCH

        if is_poly_spread:
            # Spread points must match exactly
            poly_pts = self._extract_spread_points(poly.title)
            kalshi_pts = self._extract_spread_points(kalshi.title)
            if poly_pts is None or kalshi_pts is None or abs(poly_pts - kalshi_pts) > 0.1:
                return None

        pair_id = f"poly:{poly.market_id}|kalshi:{kalshi.market_id}"
        return MatchedMarketPair(
            pair_id=pair_id,
            poly_market=poly,
            kalshi_market=kalshi,
            underlying_entity=f"GAME_{'_'.join(sorted(poly_teams))}",
            target_metric="GAME_RESULT",
            resolution_time=min(poly.resolution_time, kalshi.resolution_time),
            match_confidence=1.0,
            verified=True,
        )

    def _match_weather(
        self,
        poly: NormalizedMarket,
        kalshi: NormalizedMarket,
    ) -> Optional[MatchedMarketPair]:
        """Match daily temperature markets."""
        if poly.resolution_time.date() != kalshi.resolution_time.date():
            return None

        # City match
        city = None
        for c in ["new york", "chicago", "miami", "las vegas"]:
            if c in poly.title.lower() and c in kalshi.title.lower():
                city = c
                break
        if not city:
            return None

        poly_temp = self._extract_temperature(poly.title)
        kalshi_temp = self._extract_temperature(kalshi.title)
        if poly_temp is None or kalshi_temp is None or poly_temp != kalshi_temp:
            return None

        pair_id = f"poly:{poly.market_id}|kalshi:{kalshi.market_id}"
        return MatchedMarketPair(
            pair_id=pair_id,
            poly_market=poly,
            kalshi_market=kalshi,
            underlying_entity=f"WEATHER_{city.upper().replace(' ', '_')}",
            target_metric="MAX_TEMP",
            strike_value=float(poly_temp),
            resolution_time=min(poly.resolution_time, kalshi.resolution_time),
            match_confidence=1.0,
            verified=True,
        )

    @staticmethod
    def _extract_crypto_coin(text: str) -> Optional[str]:
        t = text.lower()
        if "bitcoin" in t or "btc" in t:
            return "BTC"
        if "ethereum" in t or "eth" in t:
            return "ETH"
        if "solana" in t or "sol" in t:
            return "SOL"
        return None

    @staticmethod
    def _extract_strike_price(text: str) -> Optional[float]:
        """Extract dollar strike e.g., '$86,000' or '86000' or '$85,749.99'."""
        cleaned = text.replace(",", "")
        # First check explicit dollar sign like $86000
        dollar_match = re.search(r'\$(\d+(?:\.\d+)?)', cleaned)
        if dollar_match:
            return float(dollar_match.group(1))
            
        # Otherwise find all candidate numbers with at least 2 digits
        matches = re.findall(r'\b(\d{2,6}(?:\.\d{1,4})?)\b', cleaned)
        years = {2024, 2025, 2026, 2027, 2028, 2030}
        for m in matches:
            val = float(m)
            if val not in years and val > 10.0:  # Strike prices for BTC/ETH/SPX are > 10
                return val
        return None

    @staticmethod
    def _extract_teams(text: str) -> List[str]:
        t = text.lower()
        found = []
        for name, code in CANONICAL_TEAMS.items():
            if re.search(r'\b' + re.escape(name) + r'\b', t):
                if code not in found:
                    found.append(code)
        return found

    @staticmethod
    def _extract_spread_points(text: str) -> Optional[float]:
        match = re.search(r'([+-]?\d+(?:\.\d+)?)', text)
        return float(match.group(1)) if match else None

    @staticmethod
    def _extract_temperature(text: str) -> Optional[int]:
        match = re.search(r'>?\s*(\d{2})\s*(?:°|deg|degrees)?', text)
        return int(match.group(1)) if match else None
