"""
High-Performance, Low-Latency Polymarket API Client.
Integrates Gamma API (market metadata) and CLOB API (order books, EIP-712 orders).
Optimized for connection reuse, fast JSON parsing, and dry-run safety.
"""

import time
import json
import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Tuple
import httpx

from src.models import (
    Platform,
    PriceLevel,
    NormalizedOrderBook,
    NormalizedMarket,
    LiveOrder,
    OrderStatus,
    OrderSide,
    TokenType,
)
from src.config import APIConfig

logger = logging.getLogger(__name__)


class PolymarketClient:
    """
    Asynchronous Polymarket Client.
    Connects to Gamma API for discovery and CLOB API for orderbook depth and execution.
    """

    def __init__(self, config: APIConfig, dry_run: bool = True):
        self.config = config
        self.dry_run = dry_run
        self.clob_url = config.polymarket_clob_url.rstrip("/")
        self.gamma_url = config.polymarket_gamma_url.rstrip("/")
        self._clob_client: Optional[httpx.AsyncClient] = None
        self._gamma_client: Optional[httpx.AsyncClient] = None
        self.last_latency_ms: float = 120.0

    async def connect(self) -> None:
        """Initialize persistent HTTP clients with connection pooling."""
        limits = httpx.Limits(max_keepalive_connections=50, max_connections=100)
        timeout = httpx.Timeout(self.config.timeout_seconds, connect=3.0)

        if self._clob_client is None or self._clob_client.is_closed:
            self._clob_client = httpx.AsyncClient(
                base_url=self.clob_url,
                timeout=timeout,
                limits=limits,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
        if self._gamma_client is None or self._gamma_client.is_closed:
            self._gamma_client = httpx.AsyncClient(
                base_url=self.gamma_url,
                timeout=timeout,
                limits=limits,
                headers={"Accept": "application/json"},
            )

    async def close(self) -> None:
        """Close persistent HTTP clients."""
        if self._clob_client and not self._clob_client.is_closed:
            await self._clob_client.aclose()
            self._clob_client = None
        if self._gamma_client and not self._gamma_client.is_closed:
            await self._gamma_client.aclose()
            self._gamma_client = None

    async def __aenter__(self) -> "PolymarketClient":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()

    async def get_active_markets(self, limit: int = 100) -> List[NormalizedMarket]:
        """Fetch high-volume active markets and event child markets from Gamma API."""
        if self._gamma_client is None:
            await self.connect()

        markets_by_id: Dict[str, NormalizedMarket] = {}

        # 1. Fetch from /markets
        try:
            params = {
                "closed": "false",
                "order": "volume24hr",
                "ascending": "false",
                "limit": limit,
            }
            t0 = time.perf_counter()
            resp = await self._gamma_client.get("/markets", params=params)
            self.last_latency_ms = (time.perf_counter() - t0) * 1000.0
            if resp.status_code == 200:
                for item in resp.json():
                    norm = self._normalize_market(item)
                    if norm:
                        markets_by_id[norm.market_id] = norm
        except Exception as e:
            logger.warning(f"Error fetching Polymarket /markets: {e}")

        # 2. Fetch top events and unpack child markets (e.g. daily BTC strikes, spreads)
        try:
            event_params = {
                "closed": "false",
                "order": "volume24hr",
                "ascending": "false",
                "limit": 30,
            }
            resp_ev = await self._gamma_client.get("/events", params=event_params)
            if resp_ev.status_code == 200:
                for ev in resp_ev.json():
                    slug = ev.get("slug", "")
                    for child in ev.get("markets", []):
                        norm = self._normalize_market(child, parent_slug=slug)
                        if norm:
                            markets_by_id[norm.market_id] = norm
        except Exception as e:
            logger.warning(f"Error fetching Polymarket /events: {e}")

        # 3. Explicitly fetch 15-minute recurring crypto events via tag_slug=15M
        try:
            resp_15m = await self._gamma_client.get(
                "/events",
                params={"tag_slug": "15M", "closed": "false", "limit": 50},
            )
            if resp_15m.status_code == 200:
                for ev in resp_15m.json():
                    slug = ev.get("slug", "")
                    for child in ev.get("markets", []):
                        norm = self._normalize_market(child, parent_slug=slug)
                        if norm:
                            markets_by_id[norm.market_id] = norm
        except Exception as e:
            logger.warning(f"Error fetching Polymarket 15M events: {e}")

        return list(markets_by_id.values())

    def _normalize_market(self, m: Dict[str, Any], parent_slug: str = "") -> Optional[NormalizedMarket]:
        """Convert Polymarket Gamma API JSON into NormalizedMarket."""
        token_ids_raw = m.get("clobTokenIds")
        if not token_ids_raw:
            return None

        # Parse token IDs JSON string or list
        if isinstance(token_ids_raw, str):
            try:
                token_ids = json.loads(token_ids_raw)
            except Exception:
                return None
        elif isinstance(token_ids_raw, list):
            token_ids = token_ids_raw
        else:
            return None

        if len(token_ids) < 2:
            return None

        yes_token_id = token_ids[0]
        no_token_id = token_ids[1]

        end_date_str = m.get("endDate")
        if not end_date_str:
            return None

        try:
            res_time = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        except Exception:
            return None

        q = m.get("question", "")
        slug = (parent_slug or m.get("slug") or "").lower()

        # Categorization
        if any(k in q.lower() or k in slug for k in ["bitcoin", "btc", "ethereum", "eth", "solana", "crypto"]):
            category = "CRYPTO"
        elif any(k in q.lower() or k in slug for k in ["s&p", "spx", "nasdaq", "fed", "inflation"]):
            category = "FINANCIALS"
        elif any(k in q.lower() or k in slug for k in ["nfl", "nba", "mlb", "vs", "win", "champions"]):
            category = "SPORTS"
        elif any(k in q.lower() or k in slug for k in ["temperature", "weather", "high temp"]):
            category = "WEATHER"
        else:
            category = "GENERAL"

        vol = float(m.get("volume24hr") or 0.0)

        return NormalizedMarket(
            platform=Platform.POLYMARKET,
            market_id=m.get("conditionId") or m.get("id") or yes_token_id,
            event_id=parent_slug or str(m.get("id", "")),
            title=q,
            description=m.get("description", ""),
            category=category,
            resolution_time=res_time,
            settlement_source="UMA / Polymarket Oracle",
            yes_token_id=yes_token_id,
            no_token_id=no_token_id,
            is_active=not m.get("closed", False),
            volume_24h=vol,
        )

    async def get_orderbook(self, market: NormalizedMarket) -> Optional[NormalizedOrderBook]:
        """
        Fetch order book for both YES and NO tokens from Polymarket CLOB.
        Returns unified NormalizedOrderBook with bids and asks in dollars.
        """
        if self._clob_client is None:
            await self.connect()

        if not market.yes_token_id or not market.no_token_id:
            return None

        try:
            t0 = time.perf_counter()
            # Fetch YES token book
            yes_resp = await self._clob_client.get("/book", params={"token_id": market.yes_token_id})
            yes_resp.raise_for_status()
            yes_data = yes_resp.json()

            # Fetch NO token book
            no_resp = await self._clob_client.get("/book", params={"token_id": market.no_token_id})
            no_resp.raise_for_status()
            no_data = no_resp.json()
            self.last_latency_ms = (time.perf_counter() - t0) * 1000.0

            yes_bids = [
                PriceLevel(price=float(lvl["price"]), size=float(lvl["size"]))
                for lvl in yes_data.get("bids", [])
            ]
            yes_asks = [
                PriceLevel(price=float(lvl["price"]), size=float(lvl["size"]))
                for lvl in yes_data.get("asks", [])
            ]
            no_bids = [
                PriceLevel(price=float(lvl["price"]), size=float(lvl["size"]))
                for lvl in no_data.get("bids", [])
            ]
            no_asks = [
                PriceLevel(price=float(lvl["price"]), size=float(lvl["size"]))
                for lvl in no_data.get("asks", [])
            ]

            # Polymarket CLOB returns bids sorted descending and asks sorted ascending
            yes_bids.sort(key=lambda x: x.price, reverse=True)
            yes_asks.sort(key=lambda x: x.price)
            no_bids.sort(key=lambda x: x.price, reverse=True)
            no_asks.sort(key=lambda x: x.price)

            return NormalizedOrderBook(
                platform=Platform.POLYMARKET,
                market_id=market.market_id,
                timestamp=datetime.now(timezone.utc),
                yes_bids=yes_bids,
                yes_asks=yes_asks,
                no_bids=no_bids,
                no_asks=no_asks,
            )
        except Exception as e:
            logger.warning(f"Failed to fetch Polymarket orderbook for {market.market_id}: {e}")
            return None

    async def place_order(
        self,
        market_id: str,
        token_id: str,
        token_type: TokenType,
        side: OrderSide,
        price: float,
        size: float,
        client_order_id: str,
    ) -> LiveOrder:
        """
        Place limit order on Polymarket CLOB.
        Enforces strict DRY RUN safety: logs simulation when dry_run=True.
        """
        order = LiveOrder(
            client_order_id=client_order_id,
            platform=Platform.POLYMARKET,
            market_id=market_id,
            token_type=token_type,
            side=side,
            price=price,
            size=size,
            status=OrderStatus.SUBMITTED,
        )

        if self.dry_run:
            logger.info(
                f"[DRY-RUN POLYMARKET] Order simulated: {side.value} {size:.2f} {token_type.value} "
                f"@ ${price:.4f} on {market_id} (Client ID: {client_order_id})"
            )
            order.status = OrderStatus.FILLED
            order.filled_size = size
            order.average_fill_price = price
            order.exchange_order_id = f"sim_poly_{client_order_id}"
            return order

        # LIVE ORDER EXECUTION: requires EIP-712 signer
        # Build standard CLOB order structure
        payload = {
            "order": {
                "tokenID": token_id,
                "price": str(price),
                "size": str(size),
                "side": "BUY" if side == OrderSide.BUY else "SELL",
            },
            "owner": self.config.polymarket_api_key,
        }

        try:
            resp = await self._clob_client.post("/order", json=payload)
            resp.raise_for_status()
            data = resp.json()
            order.exchange_order_id = data.get("orderID")
            order.status = OrderStatus.SUBMITTED
            order.raw_response = data
            logger.info(f"[LIVE POLYMARKET] Order submitted: {order.exchange_order_id}")
            return order
        except Exception as e:
            logger.error(f"[LIVE POLYMARKET] Order placement failed: {e}")
            order.status = OrderStatus.REJECTED
            raise

    async def cancel_order(self, exchange_order_id: str) -> bool:
        """Cancel an active order on Polymarket CLOB."""
        if self.dry_run:
            logger.info(f"[DRY-RUN POLYMARKET] Cancel order simulated: {exchange_order_id}")
            return True

        try:
            resp = await self._clob_client.request("DELETE", f"/order/{exchange_order_id}")
            resp.raise_for_status()
            logger.info(f"[LIVE POLYMARKET] Order cancelled: {exchange_order_id}")
            return True
        except Exception as e:
            logger.error(f"[LIVE POLYMARKET] Cancel failed for {exchange_order_id}: {e}")
            return False
