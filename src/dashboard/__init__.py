"""
Real-Time Web Dashboard.

FastAPI + WebSocket dashboard with HTTP Basic Auth for monitoring
arbitrage opportunities, positions, telemetry, and system health.
"""

from src.dashboard.state import DashboardState, DashboardTelemetry
from src.dashboard.server import create_dashboard_app

__all__ = ["DashboardState", "DashboardTelemetry", "create_dashboard_app"]
