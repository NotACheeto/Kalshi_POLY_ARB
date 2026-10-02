"""
High-Performance, Low-Latency Kalshi API Client.
Supports modern Kalshi API v2 endpoints, RSA authentication,
connection pooling, orderbook_fp parsing, and dry-run safety.
"""

import time
import base64
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


class KalshiClient:
    """
    Asynchronous Kalshi API Client.
    Optimized for connection reuse, fast JSON parsing, and dry-run execution.
    """

    def __init__(self, config: APIConfig, dry_run: bool = True):
        self.config = config
        self.dry_run = dry_run
        self.base_url = config.kalshi_base_url.rstrip("/")
        self._client: Optional[httpx.AsyncClient] = None
        self._rsa_key = None
        self.last_latency_ms: float = 30.0
        self._init_auth()

    def _init_auth(self) -> None:
        """Initialize RSA private key if available for authenticated requests."""
        if not self.config.kalshi_api_key_id:
            return

        key_content = self.config.kalshi_private_key_content
        if not key_content and self.config.kalshi_private_key_path:
            try:
                with open(self.config.kalshi_private_key_path, "r", encoding="utf-8") as f:
                    key_content = f.read()
            except Exception as e:
                logger.warning(f"Could not load Kalshi RSA key file: {e}")

        if key_content:
            try:
                from cryptography.hazmat.primitives import serialization
                self._rsa_key = serialization.load_pem_private_key(
                    key_content.encode("utf-8"),
                    password=None
                )
                logger.info("Kalshi RSA private key successfully loaded.")
            except Exception as e:
                logger.error(f"Failed to parse Kalshi RSA private key: {e}")

    async def connect(self) -> None:
        """Initialize persistent HTTP client with connection pooling."""
        if self._client is None or self._client.is_closed:
            limits = httpx.Limits(max_keepalive_connections=50, max_connections=100)
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.config.timeout_seconds, connect=3.0),
                limits=limits,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )

    async def close(self) -> None:
        """Close persistent HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> "KalshiClient":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()

    def _sign_request(self, method: str, path: str, timestamp_str: str) -> str:
        """Generate Kalshi v2 RSA signature: SHA256 with PSS padding."""
        if not self._rsa_key:
            return ""

        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        full_path = f"/trade-api/v2{path}" if not path.startswith("/trade-api/v2") else path
        message = f"{timestamp_str}{method}{full_path}".encode("utf-8")
        signature = self._rsa_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def _get_auth_headers(self, method: str, path: str) -> Dict[str, str]:
        """Generate authenticated headers if credentials are configured."""
        if not self.config.kalshi_api_key_id or not self._rsa_key:
            return {}

        now_ms = str(int(time.time() * 1000))
        sig = self._sign_request(method, path, now_ms)
        return {
            "KALSHI-ACCESS-KEY": self.config.kalshi_api_key_id,
            "KALSHI-ACCESS-SIGNATURE": sig,
            "KALSHI-ACCESS-TIMESTAMP": now_ms,
        }

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        authenticated: bool = False,
    ) -> Dict[str, Any]:
        """Perform resilient HTTP request with rate limit handling."""
        if self._client is None:
            await self.connect()

        headers = self._get_auth_headers(method, path) if authenticated else {}

        for attempt in range(self.config.max_retries + 1):
            try:
                t0 = time.perf_counter()
                response = await self._client.request(
                    method=method,
                    url=path,
                    params=params,
                    json=json_body,
                    headers=headers,
                )
                self.last_latency_ms = (time.perf_counter() - t0) * 1000.0
                if response.status_code == 429:
                    retry_after = float(response.headers.get("Retry-After", 1.0))
                    logger.warning(f"Kalshi 429 Rate Limit. Backing off for {retry_after}s")
                    await httpx.AsyncClient().sleep(retry_after)
                    continue

                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                logger.warning(f"Kalshi HTTP {e.response.status_code} on {path}: {e}")
                if attempt == self.config.max_retries:
                    raise
            except httpx.RequestError as e:
                logger.warning(f"Kalshi network error on {path}: {e}")
                if attempt == self.config.max_retries:
                    raise

        return {}

    async def get_series_markets(
        self,
        series_ticker: str,
        limit: int = 50,
    ) -> List[NormalizedMarket]:
        """Fetch open markets for a specific series (e.g. KXBTCD, KXNFLGAME)."""
        data = await self._request(
            "GET",
            "/markets",
            params={"series_ticker": series_ticker, "status": "open", "limit": limit},
        )
        markets_raw = data.get("markets", [])
        results: List[NormalizedMarket] = []
        for m in markets_raw:
            norm = self._normalize_market(m)
            if norm:
                results.append(norm)
        return results

    async def get_orderbook(self, ticker: str) -> Optional[NormalizedOrderBook]:
        """
        Fetch and parse modern Kalshi orderbook_fp.
        Returns unified NormalizedOrderBook with bids and asks in dollars.
        """
        try:
            data = await self._request("GET", f"/markets/{ticker}/orderbook")
            ob_fp = data.get("orderbook_fp") or data.get("orderbook", {})

            yes_bids: List[PriceLevel] = []
            no_bids: List[PriceLevel] = []

            # Modern Kalshi v2 format: yes_dollars = [['0.0800', '4000']], no_dollars = [['0.9100', '3750']]
            for item in ob_fp.get("yes_dollars", []):
                price = float(item[0])
                size = float(item[1])
                yes_bids.append(PriceLevel(price=price, size=size))

            for item in ob_fp.get("no_dollars", []):
                price = float(item[0])
                size = float(item[1])
                no_bids.append(PriceLevel(price=price, size=size))

            # Legacy fallback if cents format is returned
            if not yes_bids and "yes" in ob_fp:
                for item in ob_fp.get("yes", []):
                    yes_bids.append(PriceLevel(price=float(item[0]) / 100.0, size=float(item[1])))
            if not no_bids and "no" in ob_fp:
                for item in ob_fp.get("no", []):
                    no_bids.append(PriceLevel(price=float(item[0]) / 100.0, size=float(item[1])))

            # Sort bids descending (highest price first)
            yes_bids.sort(key=lambda x: x.price, reverse=True)
            no_bids.sort(key=lambda x: x.price, reverse=True)

            # Derive synthetic asks
            # If someone bids X for NO, they implicitly offer YES at (1.0 - X)
            yes_asks: List[PriceLevel] = []
            for nb in no_bids:
                ask_p = round(1.0 - nb.price, 4)
                yes_asks.append(PriceLevel(price=ask_p, size=nb.size))
            yes_asks.sort(key=lambda x: x.price)  # Asks ascending (lowest price first)

            # If someone bids X for YES, they implicitly offer NO at (1.0 - X)
            no_asks: List[PriceLevel] = []
            for yb in yes_bids:
                ask_p = round(1.0 - yb.price, 4)
                no_asks.append(PriceLevel(price=ask_p, size=yb.size))
            no_asks.sort(key=lambda x: x.price)

            return NormalizedOrderBook(
                platform=Platform.KALSHI,
                market_id=ticker,
                timestamp=datetime.now(timezone.utc),
                yes_bids=yes_bids,
                yes_asks=yes_asks,
                no_bids=no_bids,
                no_asks=no_asks,
            )
        except Exception as e:
            logger.warning(f"Failed to fetch Kalshi orderbook for {ticker}: {e}")
            return None

    def _normalize_market(self, m: Dict[str, Any]) -> Optional[NormalizedMarket]:
        """Convert Kalshi market JSON into NormalizedMarket."""
        ticker = m.get("ticker", "")
        if not ticker or ticker.startswith("KXMVE"):
            return None  # Exclude synthetic multi-leg combos

        close_time_str = m.get("close_time") or m.get("expiration_time")
        if not close_time_str:
            return None

        try:
            res_time = datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
        except Exception:
            return None

        # Category mapping from ticker prefix or category field
        cat = m.get("category", "").lower()
        if ticker.startswith(("KXBTC", "KXETH", "KXSOL")) or "crypto" in cat:
            category = "CRYPTO"
        elif ticker.startswith(("KXINX", "KXNASDAQ", "KXFED", "KXTENYEAR")) or "financial" in cat:
            category = "FINANCIALS"
        elif ticker.startswith(("KXNFL", "KXNBA", "KXMLB", "KXSOCCER", "KXCFB")) or "sports" in cat:
            category = "SPORTS"
        elif ticker.startswith("KXHIGH") or "weather" in cat or "climate" in cat:
            category = "WEATHER"
        else:
            category = cat.upper() or "GENERAL"

        vol = float(m.get("volume_24h_fp") or m.get("volume_fp") or m.get("volume") or 0.0)
        oi = float(m.get("open_interest_fp") or m.get("open_interest") or 0.0)

        return NormalizedMarket(
            platform=Platform.KALSHI,
            market_id=ticker,
            event_id=m.get("event_ticker", ""),
            title=f"{m.get('title', '')} {m.get('subtitle', '')}".strip(),
            description=m.get("rules_primary") or "",
            category=category,
            resolution_time=res_time,
            settlement_source="Kalshi Official Rulebook",
            is_active=(m.get("status") in ("active", "open")),
            volume_24h=vol,
            open_interest=oi,
        )

    async def place_order(
        self,
        market_id: str,
        token_type: TokenType,
        side: OrderSide,
        price: float,
        size: float,
        client_order_id: str,
    ) -> LiveOrder:
        """
        Place limit order on Kalshi.
        Enforces strict DRY RUN safety: logs simulation when dry_run=True.
        """
        order = LiveOrder(
            client_order_id=client_order_id,
            platform=Platform.KALSHI,
            market_id=market_id,
            token_type=token_type,
            side=side,
            price=price,
            size=size,
            status=OrderStatus.SUBMITTED,
        )

        if self.dry_run:
            logger.info(
                f"[DRY-RUN KALSHI] Order simulated: {side.value} {size} {token_type.value} "
                f"@ ${price:.4f} on {market_id} (Client ID: {client_order_id})"
            )
            order.status = OrderStatus.FILLED
            order.filled_size = size
            order.average_fill_price = price
            order.exchange_order_id = f"sim_kalshi_{client_order_id}"
            return order

        # LIVE ORDER EXECUTION
        # Determine book_side and dollar price for Kalshi v2
        # On Kalshi:
        # - Buy YES: side="bid", price=price
        # - Buy NO: side="ask", price=1.0 - price (quotes YES price)
        # - Sell YES: side="ask", price=price
        # - Sell NO: side="bid", price=1.0 - price
        if side == OrderSide.BUY:
            if token_type == TokenType.YES:
                book_side = "bid"
                kalshi_price = round(price, 4)
            else:  # NO
                book_side = "ask"
                kalshi_price = round(1.0 - price, 4)
        else:  # SELL
            if token_type == TokenType.YES:
                book_side = "ask"
                kalshi_price = round(price, 4)
            else:  # NO
                book_side = "bid"
                kalshi_price = round(1.0 - price, 4)

        # Enforce valid price bounds [0.0100, 0.9900]
        kalshi_price = max(0.0100, min(0.9900, kalshi_price))

        payload = {
            "ticker": market_id,
            "side": book_side,
            "count": f"{size:.2f}",
            "price": f"{kalshi_price:.4f}",
            "time_in_force": "immediate_or_cancel",
            "self_trade_prevention_type": "taker_at_cross",
            "client_order_id": client_order_id,
        }

        try:
            resp = await self._request("POST", "/portfolio/events/orders", json_body=payload, authenticated=True)
            order.exchange_order_id = resp.get("order_id")
            order.raw_response = resp
            
            fill_count = float(resp.get("fill_count", "0.00"))
            order.filled_size = fill_count
            if fill_count > 0:
                avg_px_fp = resp.get("average_fill_price")
                if avg_px_fp:
                    fill_p = float(avg_px_fp)
                    order.average_fill_price = fill_p if (side == OrderSide.BUY and token_type == TokenType.YES) or (side == OrderSide.SELL and token_type == TokenType.YES) else round(1.0 - fill_p, 4)
                else:
                    order.average_fill_price = price
                order.status = OrderStatus.FILLED if fill_count >= size else OrderStatus.PARTIAL
                logger.info(
                    f"[LIVE KALSHI] Order filled: {fill_count}/{size} contracts @ ${order.average_fill_price:.4f} "
                    f"(Order ID: {order.exchange_order_id})"
                )
            else:
                order.status = OrderStatus.CANCELLED
                logger.info(f"[LIVE KALSHI] Order unfilled (IOC): Order ID {order.exchange_order_id}")

            return order
        except Exception as e:
            logger.error(f"[LIVE KALSHI] Order placement failed: {e}")
            order.status = OrderStatus.REJECTED
            raise

    async def cancel_order(self, exchange_order_id: str, market_id: Optional[str] = None) -> bool:
        """Cancel an active order on Kalshi."""
        if self.dry_run:
            logger.info(f"[DRY-RUN KALSHI] Cancel order simulated: {exchange_order_id}")
            return True

        try:
            params = {"market_ticker": market_id} if market_id else {}
            await self._request("DELETE", f"/portfolio/events/orders/{exchange_order_id}", params=params, authenticated=True)
            logger.info(f"[LIVE KALSHI] Order cancelled: {exchange_order_id}")
            return True
        except Exception as e:
            logger.error(f"[LIVE KALSHI] Cancel failed for {exchange_order_id}: {e}")
            return False

    async def get_balance(self) -> float:
        """Fetch account balance in dollars from Kalshi."""
        if self.dry_run and not self._rsa_key:
            return 10000.0  # Simulated default balance

        try:
            resp = await self._request("GET", "/portfolio/balance", authenticated=True)
            # Kalshi returns balance in cents (e.g. 1154 cents = $11.54)
            balance_cents = resp.get("balance", 0)
            return float(balance_cents) / 100.0
        except Exception as e:
            logger.error(f"Failed to fetch Kalshi balance: {e}")
            return 0.0
