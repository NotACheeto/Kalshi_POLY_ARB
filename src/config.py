"""
Configuration schema and loader for the Polymarket <-> Kalshi Arbitrage Bot.
Uses Pydantic for strict type checking and validation.
Enforces safe defaults: DRY_RUN is enabled by default.
"""

import os
import yaml
from pathlib import Path
from typing import List, Optional
from pydantic import BaseModel, Field, field_validator


class APIConfig(BaseModel):
    # Polymarket endpoints
    polymarket_clob_url: str = "https://clob.polymarket.com"
    polymarket_gamma_url: str = "https://gamma-api.polymarket.com"
    polymarket_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    
    # Polymarket credentials (loaded from env)
    polymarket_api_key: Optional[str] = None
    polymarket_secret: Optional[str] = None
    polymarket_passphrase: Optional[str] = None
    polymarket_private_key: Optional[str] = None
    
    # Kalshi endpoints
    kalshi_base_url: str = "https://api.elections.kalshi.com/trade-api/v2"
    kalshi_ws_url: str = "wss://trading-api.kalshi.com/trade-api/ws/v2"
    
    # Kalshi credentials (loaded from env)
    kalshi_api_key_id: Optional[str] = None
    kalshi_private_key_path: Optional[str] = None
    kalshi_private_key_content: Optional[str] = None
    
    # Networking
    timeout_seconds: float = Field(default=8.0, ge=1.0, le=30.0)
    max_retries: int = Field(default=2, ge=0, le=5)
    retry_delay_seconds: float = Field(default=0.25, ge=0.05, le=5.0)


class ArbitrageGateConfig(BaseModel):
    # Profitability and EV Thresholds
    min_net_edge_pct: float = Field(default=0.015, ge=0.001, description="Minimum 1.5% net profit after all friction")
    min_net_profit_dollars: float = Field(default=0.25, ge=0.01, description="Minimum dollar profit per bundle trade")
    
    # Duration filtering: Ultra-short duration (< 24 hours)
    max_hours_to_resolution: float = Field(default=24.0, ge=0.1, le=168.0, description="Only trade markets resolving in <= 24h")
    min_minutes_to_resolution: float = Field(default=5.0, ge=1.0, description="Avoid trading in the chaotic final 5 minutes")
    
    # Stale Data Protections
    max_quote_age_seconds: float = Field(default=2.0, ge=0.1, le=10.0, description="Reject quotes older than 2.0s")
    
    # Execution Friction Buffers
    slippage_buffer_per_leg: float = Field(default=0.005, ge=0.0, description="Assumed 0.5% slippage allowance per leg")
    adverse_leg_buffer_cents: float = Field(default=0.005, ge=0.0, description="Extra half-cent safety margin per contract")
    annual_capital_cost_rate: float = Field(default=0.05, ge=0.0, description="5% annualized cost of locked capital")
    
    # Fee Models
    polymarket_taker_fee_pct: float = Field(default=0.0, ge=0.0, le=0.05, description="Polymarket taker fee rate (0% standard)")
    kalshi_taker_fee_multiplier: float = Field(default=0.07, ge=0.0, le=0.15, description="Kalshi CFTC taker formula multiplier")
    kalshi_fee_cap_dollars: float = Field(default=0.02, ge=0.001, le=0.10, description="Kalshi max fee per contract")


class RiskConfig(BaseModel):
    max_order_size_dollars: float = Field(default=25.0, ge=1.0, le=5000.0)
    min_order_size_dollars: float = Field(default=2.0, ge=1.0, le=100.0)
    max_market_exposure_dollars: float = Field(default=50.0, ge=5.0, le=10000.0)
    max_total_exposure_dollars: float = Field(default=100.0, ge=10.0, le=50000.0)
    max_daily_loss_dollars: float = Field(default=15.0, ge=1.0, le=5000.0)
    max_consecutive_failures: int = Field(default=3, ge=1, le=10)
    kill_switch_enabled: bool = True


class ExecutionConfig(BaseModel):
    # Mode selection: DRY RUN MUST BE DEFAULT
    dry_run: bool = True
    live_trading_confirmed: bool = False
    
    # Leg timeouts
    leg1_fill_timeout_seconds: float = Field(default=2.5, ge=0.5, le=10.0)
    leg2_fill_timeout_seconds: float = Field(default=3.0, ge=0.5, le=15.0)
    
    # Unwind behavior
    auto_unwind_unhedged_leg: bool = True
    max_unwind_loss_pct: float = Field(default=0.03, ge=0.005, le=0.20)
    
    # Logging & Journaling
    journal_path: str = "logs/execution_journal.jsonl"
    log_level: str = "INFO"


class BotConfig(BaseModel):
    api: APIConfig = Field(default_factory=APIConfig)
    arbitrage: ArbitrageGateConfig = Field(default_factory=ArbitrageGateConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    
    # Series whitelist for short duration focus
    short_duration_series_whitelist: List[str] = Field(
        default_factory=lambda: [
            "KXBTCD", "KXETHD", "KXSOLUSD",
            "KXINX", "KXNASDAQ",
            "KXHIGHNY", "KXHIGHCHI", "KXHIGHLAS", "KXHIGHMIA",
            "KXNFLGAME", "KXNBAGAME", "KXMLBGAME"
        ]
    )

    @field_validator("execution")
    @classmethod
    def validate_live_trading(cls, v: ExecutionConfig) -> ExecutionConfig:
        if not v.dry_run and not v.live_trading_confirmed:
            raise ValueError(
                "CRITICAL SAFETY GATE: Live trading cannot be enabled without explicitly setting "
                "live_trading_confirmed=True alongside dry_run=False."
            )
        return v


def load_config_from_yaml(yaml_path: str = "config/config.yaml") -> BotConfig:
    """Load configuration from YAML file and overlay environment variables."""
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    cfg_data = {}
    path = Path(yaml_path)
    if path.is_file():
        with open(path, "r", encoding="utf-8") as f:
            cfg_data = yaml.safe_load(f) or {}

    config = BotConfig(**cfg_data)
    
    # Overlay sensitive credentials from environment variables only (never YAML)
    config.api.polymarket_api_key = os.getenv("POLYMARKET_API_KEY", config.api.polymarket_api_key)
    config.api.polymarket_secret = os.getenv("POLYMARKET_SECRET", config.api.polymarket_secret)
    config.api.polymarket_passphrase = os.getenv("POLYMARKET_PASSPHRASE", config.api.polymarket_passphrase)
    config.api.polymarket_private_key = os.getenv("POLYMARKET_PRIVATE_KEY", config.api.polymarket_private_key)
    
    config.api.kalshi_api_key_id = os.getenv("KALSHI_API_KEY_ID", config.api.kalshi_api_key_id)
    config.api.kalshi_private_key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", config.api.kalshi_private_key_path)
    config.api.kalshi_private_key_content = os.getenv("KALSHI_PRIVATE_KEY_CONTENT", config.api.kalshi_private_key_content)
    
    return config
