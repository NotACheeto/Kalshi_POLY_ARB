"""
Main Orchestrator Bot for Polymarket <-> Kalshi Arbitrage.
Manages concurrent market discovery, real-time orderbook updates,
positive-EV evaluation, and dry-run / live execution.
"""

import asyncio
import logging
import signal
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional

from src.models import (
    Platform,
    MatchedMarketPair,
    NormalizedMarket,
    NormalizedOrderBook,
    ArbitrageOpportunity,
    ExecutionState,
)
from src.config import BotConfig
from src.clients.kalshi_client import KalshiClient
from src.clients.polymarket_client import PolymarketClient
from src.matcher.market_matcher import MarketMatcher
from src.math.fee_calculator import FeeCalculator
from src.math.ev_calculator import EVCalculator
from src.execution.risk_manager import RiskManager
from src.execution.reconciler import ExecutionReconciler
from src.execution.leg_risk_fsm import LegRiskFSM

logger = logging.getLogger(__name__)


class ArbitrageBot:
    """
    Production-grade cross-exchange arbitrage engine.
    Focuses on short-duration markets (< 24 hours to resolution).
    """

    def __init__(self, config: BotConfig):
        self.config = config
        self._running = False
        self._shutdown_event = asyncio.Event()

        # Initialize clients
        self.kalshi_client = KalshiClient(config.api, dry_run=config.execution.dry_run)
        self.poly_client = PolymarketClient(config.api, dry_run=config.execution.dry_run)

        # Initialize matching and math engines
        self.matcher = MarketMatcher(max_hours_to_resolution=config.arbitrage.max_hours_to_resolution)
        self.fee_calc = FeeCalculator(
            poly_taker_fee_pct=config.arbitrage.polymarket_taker_fee_pct,
            kalshi_taker_multiplier=config.arbitrage.kalshi_taker_fee_multiplier,
            kalshi_fee_cap=config.arbitrage.kalshi_fee_cap_dollars,
        )
        self.ev_calc = EVCalculator(config.arbitrage, self.fee_calc)

        # Initialize execution components
        self.risk_manager = RiskManager(config.risk)
        self.reconciler = ExecutionReconciler(journal_path=config.execution.journal_path)
        self.fsm = LegRiskFSM(
            config=config.execution,
            poly_client=self.poly_client,
            kalshi_client=self.kalshi_client,
            risk_manager=self.risk_manager,
            reconciler=self.reconciler,
        )

        # Cached state
        self.matched_pairs: List[MatchedMarketPair] = []
        self._total_scans: int = 0
        self._total_opportunities_found: int = 0
        self._total_trades_executed: int = 0
        self._total_simulated_pnl: float = 0.0

    async def start(self) -> None:
        """Start the arbitrage bot lifecycle."""
        mode_str = "DRY RUN (Paper Trading)" if self.config.execution.dry_run else "LIVE TRADING"
        logger.info("=" * 70)
        logger.info(f"POLYMARKET <-> KALSHI ARBITRAGE ENGINE INITIALIZING [{mode_str}]")
        logger.info("=" * 70)
        logger.info(f"Max Resolution Horizon: {self.config.arbitrage.max_hours_to_resolution} hours")
        logger.info(f"Min Required Net Edge: {self.config.arbitrage.min_net_edge_pct:.2%}")
        logger.info(f"Min Required Net Profit: ${self.config.arbitrage.min_net_profit_dollars:.2f}")
        logger.info("=" * 70)

        # 1. Crash Recovery check
        unresolved = self.reconciler.recover_on_startup()
        if unresolved and not self.config.execution.dry_run:
            logger.critical("HALTING: Unresolved orders found from previous crash in live mode!")
            return

        # 2. Connect API clients
        await self.kalshi_client.connect()
        await self.poly_client.connect()
        self._running = True

        # 3. Initial Market Discovery & Pairing
        await self.refresh_market_pairs()

        # 4. Main Event Scan Loop
        await self._scan_loop()

    async def stop(self) -> None:
        """Gracefully stop the bot and release connections."""
        logger.info("Stopping Arbitrage Engine...")
        self._running = False
        self._shutdown_event.set()
        await self.kalshi_client.close()
        await self.poly_client.close()

        logger.info("=" * 70)
        logger.info("EXECUTION SUMMARY")
        logger.info(f"Total Market Scans: {self._total_scans}")
        logger.info(f"Arbitrage Opportunities Detected: {self._total_opportunities_found}")
        logger.info(f"Trades Executed: {self._total_trades_executed}")
        logger.info(f"Total Realized PnL: ${self._total_simulated_pnl:.2f}")
        logger.info("=" * 70)

    async def refresh_market_pairs(self) -> None:
        """Discover and pair short-duration markets across both platforms."""
        logger.info("Discovering short-duration markets across exchanges...")

        # Fetch short-duration Kalshi markets across whitelisted popular series
        kalshi_markets: List[NormalizedMarket] = []
        for series in self.config.short_duration_series_whitelist:
            try:
                mkts = await self.kalshi_client.get_series_markets(series, limit=50)
                kalshi_markets.extend(mkts)
            except Exception as e:
                logger.warning(f"Error fetching Kalshi series {series}: {e}")

        logger.info(f"Retrieved {len(kalshi_markets)} active short-duration Kalshi candidate markets")

        # Fetch Polymarket active markets from Gamma API
        poly_markets: List[NormalizedMarket] = []
        try:
            poly_markets = await self.poly_client.get_active_markets(limit=150)
        except Exception as e:
            logger.warning(f"Error fetching Polymarket candidates: {e}")

        logger.info(f"Retrieved {len(poly_markets)} active Polymarket candidate markets")

        # Deterministic match
        self.matched_pairs = self.matcher.find_matches(poly_markets, kalshi_markets)
        logger.info(f"Verified {len(self.matched_pairs)} matched pairs for arbitrage monitoring.")

    async def _scan_loop(self) -> None:
        """Continuous high-speed scan loop over matched market pairs."""
        logger.info("Starting real-time orderbook evaluation loop...")
        last_refresh = asyncio.get_event_loop().time()

        while self._running:
            try:
                self._total_scans += 1
                now = datetime.now(timezone.utc)

                # Refresh market pairs every 60s or immediately if any pairs have expired (vital for 15m turnovers)
                time_since_refresh = asyncio.get_event_loop().time() - last_refresh
                has_expired = any(p.resolution_time <= now for p in self.matched_pairs)
                if time_since_refresh > 60.0 or has_expired:
                    await self.refresh_market_pairs()
                    last_refresh = asyncio.get_event_loop().time()

                if not self.matched_pairs:
                    logger.info("No active matched pairs in current window. Waiting 10s...")
                    await asyncio.sleep(10.0)
                    await self.refresh_market_pairs()
                    last_refresh = asyncio.get_event_loop().time()
                    continue

                # Evaluate all matched pairs concurrently
                tasks = [self._evaluate_pair(pair, now) for pair in self.matched_pairs]
                await asyncio.gather(*tasks, return_exceptions=True)

                # Short rest between cycles to respect rate limits
                await asyncio.sleep(1.0)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in scan loop: {e}")
                await asyncio.sleep(2.0)

    async def _evaluate_pair(self, pair: MatchedMarketPair, now: datetime) -> None:
        """Fetch orderbooks and evaluate a single pair for positive EV."""
        try:
            # Fetch orderbooks in parallel for lowest latency
            poly_task = self.poly_client.get_orderbook(pair.poly_market)
            kalshi_task = self.kalshi_client.get_orderbook(pair.kalshi_market.market_id)

            poly_ob, kalshi_ob = await asyncio.gather(poly_task, kalshi_task)
            if poly_ob is None or kalshi_ob is None:
                return

            opp = self.ev_calc.evaluate_pair(pair, poly_ob, kalshi_ob, now=now)
            if opp is not None and opp.is_positive_ev:
                self._total_opportunities_found += 1
                logger.info(
                    f"\n{'='*70}\n"
                    f"🎯 ARBITRAGE OPPORTUNITY DETECTED!\n"
                    f"Pair: {pair.pair_id}\n"
                    f"Entity: {pair.underlying_entity} | Strike: ${pair.strike_value}\n"
                    f"Leg 1: {opp.leg1.side.value} {opp.leg1.token_type.value} on {opp.leg1.platform.value} @ ${opp.leg1.executable_price:.4f}\n"
                    f"Leg 2: {opp.leg2.side.value} {opp.leg2.token_type.value} on {opp.leg2.platform.value} @ ${opp.leg2.executable_price:.4f}\n"
                    f"Size: {opp.executable_quantity:.1f} contracts | Gross Cost: ${opp.gross_cost_per_unit:.4f}\n"
                    f"Fees: Poly ${opp.poly_fee_total:.2f}, Kalshi ${opp.kalshi_fee_total:.2f}\n"
                    f"Friction Buffers: Slippage ${opp.slippage_buffer_total:.2f}, Adverse ${opp.adverse_buffer_total:.2f}\n"
                    f"Conservative Net Profit: ${opp.net_profit:.2f} ({opp.net_edge_pct:.2%})\n"
                    f"Annualized Return: {opp.annualized_return:.1f}%\n"
                    f"{'='*70}"
                )

                # Dispatch to execution FSM
                success, final_state, msg = await self.fsm.execute_opportunity(opp)
                if success:
                    self._total_trades_executed += 1
                    self._total_simulated_pnl += opp.net_profit
                    logger.info(f"Execution Succeeded: {msg}")
                else:
                    logger.warning(f"Execution Incomplete or Aborted: {msg}")

        except Exception as e:
            logger.debug(f"Error evaluating pair {pair.pair_id}: {e}")
