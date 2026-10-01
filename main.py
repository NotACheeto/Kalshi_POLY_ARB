#!/usr/bin/env python3
"""
Production Polymarket <-> Kalshi Arbitrage Bot CLI Entry Point.
Defaults strictly to DRY RUN (Paper Trading) with live market data.
"""

import sys
import os
import argparse
import asyncio
import logging
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.resolve()))

from src.config import load_config_from_yaml, BotConfig
from src.bot import ArbitrageBot


def setup_logging(log_level: str = "INFO") -> None:
    """Configure clean structured logging with timestamps."""
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
        ],
    )


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Polymarket <-> Kalshi Cross-Exchange Arbitrage Engine"
    )
    parser.add_argument(
        "--config",
        type=str,
        default="config/config.yaml",
        help="Path to configuration YAML file (default: config/config.yaml)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Run in paper trading mode with live market data (Default: True)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        default=False,
        help="Enable LIVE order submission (Requires --confirm-live-risk)",
    )
    parser.add_argument(
        "--confirm-live-risk",
        action="store_true",
        default=False,
        help="Explicit confirmation acknowledging real capital risk for live mode",
    )
    parser.add_argument(
        "--scan-once",
        action="store_true",
        default=False,
        help="Perform a single discovery and evaluation pass, then exit",
    )
    parser.add_argument(
        "--dashboard",
        dest="dashboard",
        action="store_true",
        default=True,
        help="Launch the real-time web dashboard on localhost:8000 (Default: True)",
    )
    parser.add_argument(
        "--no-dashboard",
        dest="dashboard",
        action="store_false",
        help="Disable the web dashboard",
    )
    parser.add_argument(
        "--dashboard-port",
        type=int,
        default=8000,
        help="Port for the web dashboard (default: 8000)",
    )
    return parser.parse_args()


async def main_async() -> None:
    args = parse_arguments()

    # Load configuration
    try:
        config = load_config_from_yaml(args.config)
    except Exception as e:
        print(f"Error loading configuration from {args.config}: {e}", file=sys.stderr)
        sys.exit(1)

    # Determine execution mode with hard safety gate
    if args.live:
        if not args.confirm_live_risk:
            print(
                "\n[ERROR] CRITICAL SAFETY GATE: Live trading requested without --confirm-live-risk!\n"
                "To trade with real capital, you must pass BOTH --live and --confirm-live-risk.\n"
                "Aborting for your financial safety.\n",
                file=sys.stderr,
            )
            sys.exit(1)
        config.execution.dry_run = False
        config.execution.live_trading_confirmed = True
    else:
        config.execution.dry_run = True
        config.execution.live_trading_confirmed = False

    setup_logging(config.execution.log_level)
    logger = logging.getLogger("Main")

    dashboard_state = None
    dashboard_task = None
    if args.dashboard and not args.scan_once:
        try:
            from src.dashboard.state import DashboardState
            from src.dashboard.server import create_dashboard_app
            import uvicorn
            dashboard_state = DashboardState()
            dashboard_state.telemetry.mode = "DRY RUN (Paper Trading)" if config.execution.dry_run else "LIVE TRADING"
            dashboard_state.telemetry.max_daily_loss_dollars = config.risk.max_daily_loss_dollars
            dashboard_state.telemetry.max_exposure_dollars = config.risk.max_total_exposure_dollars
            app = create_dashboard_app(
                dashboard_state,
                auth_username=config.dashboard.username,
                auth_password=config.dashboard.password,
            )
            server_cfg = uvicorn.Config(
                app=app,
                host="0.0.0.0",
                port=args.dashboard_port,
                log_level="warning",
                access_log=False,
            )
            server = uvicorn.Server(server_cfg)
            dashboard_task = asyncio.create_task(server.serve())
            logger.info("=" * 70)
            logger.info(f"[DASHBOARD] REAL-TIME DASHBOARD ACTIVE: http://localhost:{args.dashboard_port}")
            logger.info("=" * 70)
        except Exception as e:
            logger.warning(f"Could not initialize dashboard server: {e}")

    bot = ArbitrageBot(config, dashboard_state=dashboard_state)

    # Register OS signal handlers for graceful shutdown
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def handle_signal():
        logger.info("Shutdown signal received. Initiating graceful shutdown...")
        stop_event.set()

    for sig in ("SIGINT", "SIGTERM"):
        if hasattr(signal, sig):
            try:
                loop.add_signal_handler(getattr(signal, sig), handle_signal)
            except (NotImplementedError, RuntimeError):
                pass  # Windows event loop fallback

    if args.scan_once:
        logger.info("Running in SCAN-ONCE mode...")
        await bot.kalshi_client.connect()
        await bot.poly_client.connect()
        await bot.refresh_market_pairs()
        now = datetime.now(timezone.utc)
        for pair in bot.matched_pairs:
            await bot._evaluate_pair(pair, now)
        await bot.stop()
        return

    # Run bot until interrupted
    try:
        bot_task = asyncio.create_task(bot.start())
        while not stop_event.is_set():
            await asyncio.sleep(0.5)
            if bot_task.done():
                break
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received.")
    finally:
        await bot.stop()
        if dashboard_task:
            dashboard_task.cancel()


if __name__ == "__main__":
    from datetime import datetime, timezone
    import signal

    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        pass
