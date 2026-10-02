"""
High-Performance, Low-Latency Polymarket Client.
Supports both Polymarket US (api.polymarket.us with Ed25519 signing)
and Polymarket Global (Gamma + CLOB with connection pooling).
Optimized for connection reuse, fast JSON parsing, and dry-run safety.
"""

import time
import json
import base64
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Tuple
import httpx
from cryptography.hazmat.primitives.asymmetric import ed25519

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
    Asynchronous Polymarket Client supporting:
    1. Polymarket US (CFTC-regulated exchange via api.polymarket.us + Ed25519).
    2. Polymarket Global (Gamma API + CLOB API).
    """

    def __init__(self, config: APIConfig, dry_run: bool = True):
        self.config = config
        self.dry_run = dry_run
        self.clob_url = config.polymarket_clob_url.rstrip("/")
        self.gamma_url = config.polymarket_gamma_url.rstrip("/")
        self.us_base_url = config.polymarket_us_base_url.rstrip("/")

        self._clob_client: Optional[httpx.AsyncClient] = None
        self._gamma_client: Optional[httpx.AsyncClient] = None
        self._us_client: Optional[httpx.AsyncClient] = None

        self._ed25519_key: Optional[ed25519.Ed25519PrivateKey] = None
        self.last_latency_ms: float = 40.0

        # Load Ed25519 private key if Polymarket US credentials provided
        if self.config.polymarket_us_secret:
            try:
                raw_secret = base64.b64decode(self.config.polymarket_us_secret)
                self._ed25519_key = ed25519.Ed25519PrivateKey.from_private_bytes(raw_secret[:32])
                logger.info("Polymarket US Ed25519 key loaded successfully")
            except Exception as e:
                logger.warning(f"Failed to initialize Polymarket US Ed25519 private key: {e}")

    @property
    def is_us_account(self) -> bool:
        """True if authenticated with Polymarket US Ed25519 credentials."""
        return bool(self.config.polymarket_us_key_id and self._ed25519_key)

    def _get_us_auth_headers(self, method: str, path: str) -> Dict[str, str]:
        """Sign request with Ed25519 and return Polymarket US authentication headers."""
        if not self.is_us_account or not self._ed25519_key:
            return {}

        timestamp_ms = str(int(time.time() * 1000))
        message = f"{timestamp_ms}{method.upper()}{path}".encode("utf-8")
        sig = base64.b64encode(self._ed25519_key.sign(message)).decode("utf-8")

        return {
            "X-PM-Access-Key": self.config.polymarket_us_key_id,
            "X-PM-Timestamp": timestamp_ms,
            "X-PM-Signature": sig,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

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
        if self._us_client is None or self._us_client.is_closed:
            self._us_client = httpx.AsyncClient(
                base_url=self.us_base_url,
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
        if self._us_client and not self._us_client.is_closed:
            await self._us_client.aclose()
            self._us_client = None

    async def __aenter__(self) -> "PolymarketClient":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()

    async def get_balance(self) -> float:
        """Fetch account cash balance in dollars."""
        if self.dry_run and not self.is_us_account:
            return 10000.0

        if self.is_us_account:
            path = "/v1/account/balances"
            headers = self._get_us_auth_headers("GET", path)
            resp = await self._us_client.get(path, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            balances = data.get("balances", [])
            if balances:
                return float(balances[0].get("currentBalance", 0.0))
            return 0.0

        return 10000.0

    async def get_active_markets(self, limit: int = 100) -> List[NormalizedMarket]:
        """
        Fetch active markets.
        If authenticated with Polymarket US, fetches CFTC-regulated crypto markets directly.
        Otherwise falls back to Polymarket Gamma API.
        """
        if self._gamma_client is None or self._us_client is None:
            await self.connect()

        markets_by_id: Dict[str, NormalizedMarket] = {}

        # 1. Prefer Polymarket US API when US credentials are provided
        if self.is_us_account:
            try:
                path = "/v1/markets"
                params = {"categories": "crypto", "closed": "false", "limit": limit}
                t0 = time.perf_counter()
                headers = self._get_us_auth_headers("GET", path)
                resp = await self._us_client.get(path, params=params, headers=headers)
                self.last_latency_ms = (time.perf_counter() - t0) * 1000.0

                if resp.status_code == 200:
                    data = resp.json()
                    for item in data.get("markets", []):
                        norm = self._normalize_us_market(item)
                        if norm:
                            markets_by_id[norm.market_id] = norm
                    if markets_by_id:
                        return list(markets_by_id.values())
            except Exception as e:
                logger.warning(f"Error fetching Polymarket US /v1/markets: {e}")

        # 2. Fallback to Gamma API
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

    def _normalize_us_market(self, m: Dict[str, Any]) -> Optional[NormalizedMarket]:
        """Convert Polymarket US API market JSON into NormalizedMarket."""
        slug = m.get("slug", "")
        if not slug:
            return None

        # Extract sides
        sides = m.get("marketSides", [])
        yes_side_id = None
        no_side_id = None
        for s in sides:
            if s.get("long", False) or s.get("description", "").lower() == "yes":
                yes_side_id = str(s.get("id"))
            elif not s.get("long", True) or s.get("description", "").lower() == "no":
                no_side_id = str(s.get("id"))

        # Resolution time from assetPriceTerms windowEnd or gameStartTime
        terms = m.get("assetPriceTerms") or {}
        window_end_str = terms.get("windowEnd") or m.get("gameStartTime") or m.get("endDate")
        if not window_end_str:
            return None

        try:
            res_time = datetime.fromisoformat(window_end_str.replace("Z", "+00:00"))
        except Exception:
            return None

        title = m.get("title") or m.get("question") or slug
        desc = m.get("description", "")

        return NormalizedMarket(
            platform=Platform.POLYMARKET,
            market_id=slug,
            event_id=str(m.get("id", slug)),
            title=title,
            description=desc,
            category="CRYPTO",
            resolution_time=res_time,
            settlement_source="CF Benchmarks BRTI",
            yes_token_id=yes_side_id or slug,
            no_token_id=no_side_id or slug,
            is_active=m.get("active", True) and not m.get("closed", False),
            volume_24h=0.0,
            tick_size=float(m.get("orderPriceMinTickSize") or 0.01),
            min_order_size=float(m.get("minimumTradeQty") or 0.01),
        )

    def _normalize_market(self, m: Dict[str, Any], parent_slug: str = "") -> Optional[NormalizedMarket]:
        """Convert Polymarket Gamma API JSON into NormalizedMarket."""
        token_ids_raw = m.get("clobTokenIds")
        if not token_ids_raw:
            return None

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
        Fetch order book for both YES and NO sides.
        Supports both Polymarket US (/v1/markets/{slug}/book) and Global CLOB (/book).
        """
        if self._clob_client is None or self._us_client is None:
            await self.connect()

        # 1. Polymarket US Orderbook
        if self.is_us_account and market.market_id.startswith("cpc-"):
            try:
                t0 = time.perf_counter()
                path = f"/v1/markets/{market.market_id}/book"
                headers = self._get_us_auth_headers("GET", path)
                resp = await self._us_client.get(path, headers=headers)
                resp.raise_for_status()
                data = resp.json().get("marketData", {})
                self.last_latency_ms = (time.perf_counter() - t0) * 1000.0

                yes_bids: List[PriceLevel] = []
                yes_asks: List[PriceLevel] = []

                for b in data.get("bids", []):
                    px = float(b["px"]["value"])
                    qty = float(b["qty"])
                    yes_bids.append(PriceLevel(price=px, size=qty))

                for a in data.get("offers", []):
                    px = float(a["px"]["value"])
                    qty = float(a["qty"])
                    yes_asks.append(PriceLevel(price=px, size=qty))

                # Sort YES bids descending and asks ascending
                yes_bids.sort(key=lambda x: x.price, reverse=True)
                yes_asks.sort(key=lambda x: x.price)

                # Derive NO bids and asks synthetically (NO price = 1.0 - YES price)
                no_bids: List[PriceLevel] = []
                for ya in yes_asks:
                    no_price = round(1.0 - ya.price, 4)
                    no_bids.append(PriceLevel(price=no_price, size=ya.size))
                no_bids.sort(key=lambda x: x.price, reverse=True)

                no_asks: List[PriceLevel] = []
                for yb in yes_bids:
                    no_price = round(1.0 - yb.price, 4)
                    no_asks.append(PriceLevel(price=no_price, size=yb.size))
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
                logger.warning(f"Failed to fetch Polymarket US orderbook for {market.market_id}: {e}")
                return None

        # 2. Global Polymarket CLOB Orderbook
        if not market.yes_token_id or not market.no_token_id:
            return None

        try:
            t0 = time.perf_counter()
            yes_task = self._clob_client.get("/book", params={"token_id": market.yes_token_id})
            no_task = self._clob_client.get("/book", params={"token_id": market.no_token_id})
            yes_resp, no_resp = await asyncio.gather(yes_task, no_task)
            yes_resp.raise_for_status()
            no_resp.raise_for_status()
            yes_data = yes_resp.json()
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
        Place limit order on Polymarket US (or Global CLOB).
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

        # LIVE ORDER EXECUTION
        if self.is_us_account:
            path = "/v1/orders/batched"
            outcome_side = "OUTCOME_SIDE_YES" if token_type == TokenType.YES else "OUTCOME_SIDE_NO"
            action = "ORDER_ACTION_BUY" if side == OrderSide.BUY else "ORDER_ACTION_SELL"
            us_order = {
                "marketSlug": market_id,
                "outcomeSide": outcome_side,
                "action": action,
                "type": "ORDER_TYPE_LIMIT",
                "price": {"value": f"{price:.2f}", "currency": "USD"},
                "quantity": str(int(size) if size >= 1 else size),
            }
            try:
                headers = self._get_us_auth_headers("POST", path)
                resp = await self._us_client.post(path, headers=headers, json={"orders": [us_order]})
                resp.raise_for_status()
                data = resp.json()
                created_ids = data.get("createdOrderIds", [])
                order.exchange_order_id = created_ids[0] if created_ids else f"poly_{client_order_id}"
                order.status = OrderStatus.SUBMITTED
                order.raw_response = data
                logger.info(f"[LIVE POLYMARKET US] Order submitted: {order.exchange_order_id} on {market_id}")
                return order
            except Exception as e:
                logger.error(f"[LIVE POLYMARKET US] Order placement failed: {e}")
                order.status = OrderStatus.REJECTED
                raise

        # Global CLOB fallback
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

    async def cancel_order(self, exchange_order_id: str, market_id: str = "") -> bool:
        """Cancel an active order on Polymarket US or Global CLOB."""
        if self.dry_run:
            logger.info(f"[DRY-RUN POLYMARKET] Cancel order simulated: {exchange_order_id}")
            return True

        if self.is_us_account:
            path = "/v1/orders/batched/cancel"
            headers = self._get_us_auth_headers("POST", path)
            cancel_spec = {"orderId": exchange_order_id}
            if market_id:
                cancel_spec["marketSlug"] = market_id
            try:
                resp = await self._us_client.post(path, headers=headers, json={"orders": [cancel_spec]})
                resp.raise_for_status()
                logger.info(f"[LIVE POLYMARKET US] Order cancelled: {exchange_order_id}")
                return True
            except Exception as e:
                logger.error(f"[LIVE POLYMARKET US] Cancel failed for {exchange_order_id}: {e}")
                return False

        try:
            resp = await self._clob_client.request("DELETE", f"/order/{exchange_order_id}")
            resp.raise_for_status()
            logger.info(f"[LIVE POLYMARKET] Order cancelled: {exchange_order_id}")
            return True
        except Exception as e:
            logger.error(f"[LIVE POLYMARKET] Cancel failed for {exchange_order_id}: {e}")
            return False
