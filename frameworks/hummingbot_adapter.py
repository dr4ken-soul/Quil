"""
Hummingbot-compatible market-data adapter.

Uses CCXT's public spot endpoints to collect cross-exchange prices and a
Bybit order book. It does not place orders or require exchange credentials.
If CCXT is not installed, the adapter returns an unavailable result cleanly.
"""

import asyncio
import time
from typing import Optional

from utils.logger import get_logger

log = get_logger("hummingbot_adapter")


class HummingbotAdapter:
    """Collect public market-depth and cross-venue price context."""

    def __init__(self, cache_seconds: int = 60):
        self.cache_seconds = cache_seconds
        self._cache: dict[str, tuple[float, dict]] = {}
        self._exchanges: dict[str, object] = {}
        self._exchange_backoff: dict[str, float] = {}
        self._exchange_lock = asyncio.Lock()
        try:
            import ccxt.async_support  # noqa: F401
            self._available = True
        except ImportError:
            self._available = False
            log.info("ccxt_not_installed", message="Install ccxt for public market snapshots")

    @property
    def available(self) -> bool:
        return self._available

    async def _fetch_exchange(self, exchange_id: str, ticker: str,
                              include_order_book: bool = False) -> tuple[str, float, dict]:
        """Fetch one public ticker and, optionally, its top-of-book levels."""
        if not self._available:
            return exchange_id, 0.0, {}

        try:
            exchange = await self._get_exchange(exchange_id)
            if exchange is None:
                return exchange_id, 0.0, {}

            base = ticker.upper().replace("$", "").replace("/USDT", "")
            symbol = f"{base}/USDT"
            market = exchange.markets.get(symbol)
            if not market or market.get("active") is False or market.get("spot") is False:
                return exchange_id, 0.0, {}

            ticker_data = await asyncio.wait_for(exchange.fetch_ticker(symbol), timeout=8)
            price = float(ticker_data.get("last") or ticker_data.get("close") or 0)
            book = {}
            if include_order_book and price > 0:
                raw_book = await asyncio.wait_for(
                    exchange.fetch_order_book(symbol, limit=20), timeout=8
                )
                book = {
                    "bids": [[float(row[0]), float(row[1])] for row in raw_book.get("bids", [])[:20]],
                    "asks": [[float(row[0]), float(row[1])] for row in raw_book.get("asks", [])[:20]],
                    "timestamp": raw_book.get("timestamp"),
                    "exchange": exchange_id,
                }
            return exchange_id, price, book
        except Exception as e:
            log.debug("public_exchange_snapshot_failed", exchange=exchange_id,
                      symbol=ticker, error=str(e))
            return exchange_id, 0.0, {}

    async def _get_exchange(self, exchange_id: str):
        """Create each public CCXT client once and reuse its loaded markets."""
        if exchange_id in self._exchanges:
            return self._exchanges[exchange_id]
        if time.monotonic() < self._exchange_backoff.get(exchange_id, 0):
            return None
        async with self._exchange_lock:
            if exchange_id in self._exchanges:
                return self._exchanges[exchange_id]
            if time.monotonic() < self._exchange_backoff.get(exchange_id, 0):
                return None
            exchange = None
            try:
                import ccxt.async_support as ccxt

                exchange_class = getattr(ccxt, exchange_id, None)
                if exchange_class is None:
                    return None
                exchange = exchange_class({
                    "enableRateLimit": True,
                    "timeout": 5000,
                    "options": {"defaultType": "spot"},
                })
                await asyncio.wait_for(exchange.load_markets(), timeout=8)
                self._exchanges[exchange_id] = exchange
                return exchange
            except Exception as e:
                self._exchange_backoff[exchange_id] = time.monotonic() + 60
                log.debug("public_exchange_markets_failed", exchange=exchange_id,
                          error=str(e))
                if exchange is not None:
                    try:
                        await exchange.close()
                    except Exception:
                        pass
                return None

    async def get_market_snapshot(self, symbol: str) -> dict:
        """Return cached cross-exchange prices and a current public order book."""
        ticker = symbol.upper().replace("$", "").replace("/USDT", "")
        if len(ticker) > 15 or not ticker:
            return {"available": False, "exchange_prices": {}, "order_book": {}}

        cached = self._cache.get(ticker)
        if cached and time.monotonic() - cached[0] < self.cache_seconds:
            return dict(cached[1])

        exchanges = ("binance", "bybit", "okx")
        results = await asyncio.gather(*(
            self._fetch_exchange(name, ticker, include_order_book=(name == "bybit"))
            for name in exchanges
        ))
        prices = {name: price for name, price, _ in results if price > 0}
        order_book = next((book for name, _, book in results if name == "bybit" and book), {})
        snapshot = {"available": len(prices) >= 2,
                    "exchange_prices": prices,
                    "order_book": order_book}
        if len(prices) >= 2:
            low_exchange = min(prices, key=prices.get)
            high_exchange = max(prices, key=prices.get)
            low_price, high_price = prices[low_exchange], prices[high_exchange]
            snapshot.update({
                "spread_pct": round((high_price - low_price) / low_price * 100, 4),
                "buy_exchange": low_exchange,
                "sell_exchange": high_exchange,
            })
        self._cache[ticker] = (time.monotonic(), snapshot)
        return dict(snapshot)

    async def check_cross_exchange_spread(self, symbol: str,
                                           exchange_a: str = "binance",
                                           exchange_b: str = "bybit") -> dict:
        """Measure the public price difference between two exchanges."""
        snapshot = await self.get_market_snapshot(symbol)
        prices = snapshot.get("exchange_prices", {})
        price_a, price_b = prices.get(exchange_a), prices.get(exchange_b)
        if not price_a or not price_b:
            return {"available": False, "symbol": symbol,
                    "exchanges": [exchange_a, exchange_b]}
        low = min(price_a, price_b)
        return {
            "available": True,
            "symbol": symbol,
            "exchanges": [exchange_a, exchange_b],
            "prices": {exchange_a: price_a, exchange_b: price_b},
            "spread_pct": round(abs(price_a - price_b) / low * 100, 4),
            "buy_exchange": exchange_a if price_a < price_b else exchange_b,
            "sell_exchange": exchange_b if price_a < price_b else exchange_a,
        }

    async def analyze(self, token) -> Optional[dict]:
        """Return a non-executing arbitrage context signal for Quil."""
        prices = token.exchange_presence.exchange_price_spread or {}
        prices = {name: float(price) for name, price in prices.items() if price and price > 0}
        if len(prices) < 2:
            return None
        low_exchange = min(prices, key=prices.get)
        high_exchange = max(prices, key=prices.get)
        low_price, high_price = prices[low_exchange], prices[high_exchange]
        spread_pct = (high_price - low_price) / low_price * 100
        if spread_pct < 0.5:
            return None
        return {
            "spread_pct": spread_pct,
            "buy_exchange": low_exchange,
            "sell_exchange": high_exchange,
            "low_price": low_price,
            "high_price": high_price,
        }

    async def detect_arbitrage(self, symbol: str) -> Optional[dict]:
        """Return a public arbitrage candidate when the measured gap exceeds 0.5%."""
        snapshot = await self.get_market_snapshot(symbol)
        if snapshot.get("spread_pct", 0) < 0.5:
            return None
        return snapshot

    async def close(self) -> None:
        """Close public exchange clients and drop cached market snapshots."""
        self._cache.clear()
        for exchange in self._exchanges.values():
            try:
                await exchange.close()
            except Exception:
                pass
        self._exchanges.clear()
        self._exchange_backoff.clear()
