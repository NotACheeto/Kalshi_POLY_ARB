"""
Live Telemetry and Dashboard State Container.
Thread-safe in-memory state shared between the Arbitrage Engine and Web UI.
"""

import time
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional
from pydantic import BaseModel, Field


class DashboardTelemetry(BaseModel):
    # Status
    status: str = "INITIALIZING"
    mode: str = "DRY RUN (Paper Trading)"
    uptime_seconds: float = 0.0
    start_time: str = ""
    kill_switch_active: bool = False

    # Latency & Network
    kalshi_latency_ms: float = 0.0
    poly_latency_ms: float = 0.0
    scan_frequency_hz: float = 0.0
    total_scans: int = 0
    quote_age_ms: float = 0.0

    # Active 15M Bitcoin Market Pair
    active_pair_id: str = "None"
    active_pair_title: str = "Waiting for market discovery..."
    expiration_time: str = ""
    seconds_to_expiration: float = 0.0

    # Live Orderbooks
    kalshi_yes_bid: Optional[float] = None
    kalshi_yes_ask: Optional[float] = None
    kalshi_no_bid: Optional[float] = None
    kalshi_no_ask: Optional[float] = None
    kalshi_spread: Optional[float] = None

    poly_yes_bid: Optional[float] = None
    poly_yes_ask: Optional[float] = None
    poly_no_bid: Optional[float] = None
    poly_no_ask: Optional[float] = None
    poly_spread: Optional[float] = None

    # Synthetic Arbitrage Spreads
    dir1_cost: Optional[float] = None  # Poly YES + Kalshi NO
    dir2_cost: Optional[float] = None  # Poly NO + Kalshi YES
    best_cost: Optional[float] = None
    best_gross_edge: Optional[float] = None
    best_conservative_net_edge: Optional[float] = None
    opportunity_active: bool = False

    # Portfolio & Risk
    daily_pnl_dollars: float = 0.0
    max_daily_loss_dollars: float = 1.0
    current_exposure_dollars: float = 0.0
    max_exposure_dollars: float = 1.0
    min_net_edge_pct: float = 0.025
    total_trades_executed: int = 0
    total_opportunities_detected: int = 0

    # Recent Opportunity Feed & Logs
    recent_opportunities: List[Dict[str, Any]] = Field(default_factory=list)
    recent_trades: List[Dict[str, Any]] = Field(default_factory=list)
    recent_logs: List[str] = Field(default_factory=list)


class DashboardState:
    """Singleton-style state manager for dashboard updates."""

    def __init__(self):
        self.telemetry = DashboardTelemetry()
        self._start_time = time.time()
        self.telemetry.start_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    def update_uptime(self) -> None:
        self.telemetry.uptime_seconds = round(time.time() - self._start_time, 1)

    def log_message(self, msg: str) -> None:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self.telemetry.recent_logs.insert(0, f"[{ts}] {msg}")
        if len(self.telemetry.recent_logs) > 40:
            self.telemetry.recent_logs.pop()

    def record_opportunity(self, opp_data: Dict[str, Any]) -> None:
        self.telemetry.recent_opportunities.insert(0, opp_data)
        if len(self.telemetry.recent_opportunities) > 25:
            self.telemetry.recent_opportunities.pop()

    def record_trade(self, trade_data: Dict[str, Any]) -> None:
        self.telemetry.recent_trades.insert(0, trade_data)
        if len(self.telemetry.recent_trades) > 25:
            self.telemetry.recent_trades.pop()

    def reset_pnl(self) -> None:
        """Reset realized PnL and trade history on demand."""
        self.telemetry.daily_pnl_dollars = 0.0
        self.telemetry.total_trades_executed = 0
        self.telemetry.recent_trades.clear()
        self.log_message("Realized Net PnL and trade metrics reset to $0.00")

    def to_dict(self) -> Dict[str, Any]:
        self.update_uptime()
        return self.telemetry.model_dump()
