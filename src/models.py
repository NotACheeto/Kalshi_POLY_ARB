"""
Domain models for Polymarket <-> Kalshi Arbitrage Bot.
Designed for high performance, strict typing, and immutability where critical.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, List, Dict, Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Platform(str, Enum):
    POLYMARKET = "polymarket"
    KALSHI = "kalshi"


class TokenType(str, Enum):
    YES = "YES"
    NO = "NO"


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class ExecutionState(str, Enum):
    IDLE = "IDLE"
    DETECTED = "DETECTED"
    VALIDATING = "VALIDATING"
    LEG1_SUBMITTED = "LEG1_SUBMITTED"
    LEG1_PARTIAL = "LEG1_PARTIAL"
    LEG1_FILLED = "LEG1_FILLED"
    LEG2_SUBMITTED = "LEG2_SUBMITTED"
    LEG2_PARTIAL = "LEG2_PARTIAL"
    HEDGING = "HEDGING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ABORTED = "ABORTED"


@dataclass(frozen=True)
class PriceLevel:
    """Represents a single order book price level."""
    price: float  # In dollars: 0.00 to 1.00
    size: float   # In shares / contracts


@dataclass
class TokenOrderBookSide:
    """Represents bids or asks for a specific token."""
    levels: List[PriceLevel] = field(default_factory=list)

    @property
    def best_price(self) -> Optional[float]:
        return self.levels[0].price if self.levels else None

    @property
    def best_size(self) -> Optional[float]:
        return self.levels[0].size if self.levels else None

    def cumulative_depth_to_price(self, target_price: float, is_bid: bool) -> float:
        """Returns total quantity available up to target price."""
        total_size = 0.0
        for lvl in self.levels:
            if is_bid and lvl.price >= target_price:
                total_size += lvl.size
            elif not is_bid and lvl.price <= target_price:
                total_size += lvl.size
            else:
                break
        return total_size


@dataclass
class NormalizedOrderBook:
    """
    Standardized order book structure across both Polymarket and Kalshi.
    Prices are always in dollars: 0.00 to 1.00.
    """
    platform: Platform
    market_id: str
    timestamp: datetime
    yes_bids: List[PriceLevel] = field(default_factory=list)
    yes_asks: List[PriceLevel] = field(default_factory=list)
    no_bids: List[PriceLevel] = field(default_factory=list)
    no_asks: List[PriceLevel] = field(default_factory=list)

    @property
    def best_yes_bid(self) -> Optional[float]:
        return self.yes_bids[0].price if self.yes_bids else None

    @property
    def best_yes_bid_size(self) -> Optional[float]:
        return self.yes_bids[0].size if self.yes_bids else None

    @property
    def best_yes_ask(self) -> Optional[float]:
        return self.yes_asks[0].price if self.yes_asks else None

    @property
    def best_yes_ask_size(self) -> Optional[float]:
        return self.yes_asks[0].size if self.yes_asks else None

    @property
    def best_no_bid(self) -> Optional[float]:
        return self.no_bids[0].price if self.no_bids else None

    @property
    def best_no_bid_size(self) -> Optional[float]:
        return self.no_bids[0].size if self.no_bids else None

    @property
    def best_no_ask(self) -> Optional[float]:
        return self.no_asks[0].price if self.no_asks else None

    @property
    def best_no_ask_size(self) -> Optional[float]:
        return self.no_asks[0].size if self.no_asks else None

    @property
    def yes_spread(self) -> Optional[float]:
        if self.best_yes_ask is not None and self.best_yes_bid is not None:
            return round(self.best_yes_ask - self.best_yes_bid, 4)
        return None

    @property
    def no_spread(self) -> Optional[float]:
        if self.best_no_ask is not None and self.best_no_bid is not None:
            return round(self.best_no_ask - self.best_no_bid, 4)
        return None

    def age_seconds(self, now: Optional[datetime] = None) -> float:
        if now is None:
            now = utc_now()
        return (now - self.timestamp).total_seconds()


@dataclass
class NormalizedMarket:
    """
    Standardized representation of a prediction market contract.
    """
    platform: Platform
    market_id: str                # e.g., Condition ID on Poly, Ticker on Kalshi
    event_id: str
    title: str
    description: str
    category: str
    resolution_time: datetime     # Target expiration / resolution in UTC
    settlement_source: str        # e.g., "CF Benchmarks", "Associated Press", "NWS"
    yes_token_id: Optional[str] = None  # Poly clob token ID
    no_token_id: Optional[str] = None   # Poly clob token ID
    is_active: bool = True
    volume_24h: float = 0.0
    open_interest: float = 0.0
    tick_size: float = 0.01
    min_order_size: float = 1.0


@dataclass
class MatchedMarketPair:
    """
    Strictly validated pair representing the identical underlying real-world outcome.
    """
    pair_id: str
    poly_market: NormalizedMarket
    kalshi_market: NormalizedMarket
    underlying_entity: str        # e.g. "BTC", "ETH", "NFL_CLE_PIT"
    target_metric: str            # e.g. "PRICE_FIXING", "GAME_WINNER"
    strike_value: Optional[float] = None  # e.g. 86000.0
    resolution_time: datetime = field(default_factory=utc_now)
    match_confidence: float = 1.0
    verified: bool = True


@dataclass
class ArbitrageLegSpec:
    """Specification of a single trade leg."""
    platform: Platform
    market_id: str
    token_type: TokenType
    side: OrderSide
    executable_price: float
    available_size: float
    expected_fee: float
    token_id: Optional[str] = None


@dataclass
class ArbitrageOpportunity:
    """
    Fully calculated and gated arbitrage opportunity.
    Guaranteed risk-free structure:
    Leg 1 + Leg 2 form a synthetic bundle that pays exactly $1.00 at resolution.
    """
    opportunity_id: str
    market_pair: MatchedMarketPair
    leg1: ArbitrageLegSpec        # First leg to execute
    leg2: ArbitrageLegSpec        # Second leg to execute
    executable_quantity: float   # Max executable size across both books
    gross_cost_per_unit: float   # leg1.price + leg2.price
    gross_payout_per_unit: float # Always 1.00 for complementary YES/NO bundle
    gross_edge_per_unit: float   # 1.00 - gross_cost_per_unit
    
    # Detailed fee & friction breakdown
    poly_fee_total: float
    kalshi_fee_total: float
    slippage_buffer_total: float
    capital_cost_total: float
    adverse_buffer_total: float
    total_costs: float
    
    # Conservative EV evaluation
    net_profit: float            # Conservative net expected dollars
    net_edge_pct: float          # Conservative net edge as % of outlay
    annualized_return: float     # Annualized ROI based on hours to expiration
    hours_to_resolution: float
    detected_at: datetime = field(default_factory=utc_now)
    latency_buffer_total: float = 0.0
    
    @property
    def is_positive_ev(self) -> bool:
        return self.net_profit > 0.0


@dataclass
class LiveOrder:
    """Represents an active order sent to an exchange."""
    client_order_id: str
    platform: Platform
    market_id: str
    token_type: TokenType
    side: OrderSide
    price: float
    size: float
    status: OrderStatus = OrderStatus.PENDING
    exchange_order_id: Optional[str] = None
    filled_size: float = 0.0
    average_fill_price: float = 0.0
    fee_paid: float = 0.0
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    raw_response: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TradeFill:
    """Confirmed execution fill."""
    fill_id: str
    client_order_id: str
    platform: Platform
    market_id: str
    token_type: TokenType
    side: OrderSide
    price: float
    size: float
    fee: float
    timestamp: datetime = field(default_factory=utc_now)
