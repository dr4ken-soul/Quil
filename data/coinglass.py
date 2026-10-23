"""
Coinglass API Client

Provides open interest, funding rates, liquidation data, and long/short ratios
across all major exchanges. Critical for crime pump detection layers 2 and 3.
"""

import httpx
import time
from typing import Optional
from utils.logger import get_logger
from config.settings import COINGLASS_API_KEY, COINGLASS_BASE_URL

log = get_logger("coinglass")


class CoinglassClient:
    """Async client for the Coinglass Open API v4."""

    def __init__(self):
        self.base_url = COINGLASS_BASE_URL
        self.api_key = COINGLASS_API_KEY
        self._client: Optional[httpx.AsyncClient] = None
        self._funding_cache: Optional[tuple[float, list]] = None
        self._snapshot_cache: dict[str, tuple[float, dict]] = {}

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=30.0,
                headers={
                    "Accept": "application/json",
                    "CG-API-KEY": self.api_key,
                },
            )
        return self._client

    async def _request(self, endpoint: str, params: dict = None) -> Optional[dict]:
        """Make an authenticated GET request to Coinglass."""
        if not self.api_key:
            log.warning("coinglass_no_key", message="No API key configured. Skipping Coinglass data.")
            return None

        try:
            client = await self._get_client()
            if endpoint.startswith("/") and not endpoint.startswith("/api/"):
                endpoint = f"/api{endpoint}"
            response = await client.get(endpoint, params=params)
            response.raise_for_status()
            data = response.json()

            if data.get("code") != "0" and data.get("code") != 0:
                log.warning("coinglass_api_error", code=data.get("code"),
                           msg=data.get("msg"), endpoint=endpoint)
                return None

            return data.get("data")
        except httpx.HTTPStatusError as e:
            log.error("coinglass_http_error", status=e.response.status_code,
                      endpoint=endpoint)
            return None
        except Exception as e:
            log.error("coinglass_error", error=str(e), endpoint=endpoint)
            return None

    # ── Open Interest ─────────────────────────────────────────────────────────

    async def get_open_interest(self, symbol: str) -> Optional[list]:
        """
        Get aggregated open interest for a symbol across all exchanges.
        Returns OI in USD and coin terms.
        """
        data = await self._request("/futures/open-interest/exchange-list", params={"symbol": symbol})
        return data

    async def get_open_interest_history(self, symbol: str, interval: str = "4h",
                                         limit: int = 100) -> Optional[list]:
        """
        Get historical open interest data.
        interval: '5m', '15m', '30m', '1h', '4h', '12h', '1d'
        """
        data = await self._request("/futures/open-interest/history", params={
            "exchange": "Binance",
            "symbol": f"{symbol.upper()}USDT",
            "interval": interval,
            "limit": limit,
            "unit": "usd",
        })
        return data

    async def get_oi_by_exchange(self, symbol: str) -> Optional[list]:
        """Get open interest breakdown by exchange for a symbol."""
        data = await self._request("/futures/open-interest/exchange-list", params={
            "symbol": symbol,
        })
        return data

    async def get_oi_change(self, symbol: str, interval: str = "4h") -> Optional[dict]:
        """Get OI change over a specific interval."""
        data = await self._request("/futures/open-interest/aggregated-history", params={
            "symbol": symbol,
            "interval": interval,
        })
        return data

    # ── Funding Rates ─────────────────────────────────────────────────────────

    async def get_funding_rate(self, symbol: str) -> Optional[list]:
        """
        Get current funding rates across all exchanges for a symbol.
        Critical for Layer 3 crime pump detection.
        """
        cached = self._funding_cache
        if cached and time.monotonic() - cached[0] < 20:
            data = cached[1]
        else:
            data = await self._request("/futures/funding-rate/exchange-list")
            if data is not None:
                self._funding_cache = (time.monotonic(), data)

        if not isinstance(data, list):
            return data

        target = symbol.upper().replace("$", "").replace("/USDT", "")
        target = target[:-4] if target.endswith("USDT") else target
        return [item for item in data if isinstance(item, dict)
                and str(item.get("symbol", "")).upper() == target]

    async def get_funding_rate_history(self, symbol: str, exchange: str = None,
                                        limit: int = 100, interval: str = "4h") -> Optional[list]:
        """Get historical funding rate data."""
        params = {
            "symbol": f"{symbol.upper()}USDT",
            "exchange": exchange or "Binance",
            "interval": interval,
            "limit": limit,
        }
        data = await self._request("/futures/funding-rate/history", params=params)
        return data

    async def get_weighted_funding_rate(self, symbol: str) -> Optional[dict]:
        """Get OI-weighted average funding rate across exchanges."""
        data = await self._request("/futures/funding-rate/oi-weight-history", params={
            "symbol": symbol,
            "interval": "4h",
            "limit": 2,
        })
        return data

    # ── Liquidations ──────────────────────────────────────────────────────────

    async def get_liquidation_data(self, symbol: str, interval: str = "4h") -> Optional[list]:
        """Get liquidation data for a symbol."""
        data = await self._request("/futures/liquidation/aggregated-history", params={
            "exchange_list": "Binance,OKX,Bybit",
            "symbol": symbol,
            "interval": interval,
            "limit": 6,
        })
        return data

    async def get_liquidation_heatmap(self, symbol: str, exchange: str = "Binance") -> Optional[dict]:
        """
        Get liquidation heatmap data showing clustered liquidation levels.
        Critical for Layer 4 detection (identifying liquidation targets above price).
        """
        data = await self._request("/futures/liquidation/aggregated-heatmap/model1", params={
            "symbol": symbol,
            "range": "3d",
        })
        return data

    async def get_liquidation_aggregated(self, symbol: str,
                                          interval: str = "1h") -> Optional[list]:
        """Get aggregated liquidation volume over time."""
        data = await self._request("/futures/liquidation/aggregated-history", params={
            "symbol": symbol,
            "interval": interval,
        })
        return data

    # ── Long/Short Ratio ──────────────────────────────────────────────────────

    async def get_long_short_ratio(self, symbol: str, interval: str = "4h") -> Optional[list]:
        """
        Get long/short ratio across exchanges.
        Critical for OI interpretation. If OI changes but L/S ratio stays
        flat it indicates hedged market maker positions.
        """
        data = await self._request("/futures/global-long-short-account-ratio/history", params={
            "exchange": "Binance",
            "symbol": f"{symbol.upper()}USDT",
            "interval": interval,
            "limit": 3,
        })
        return data

    async def get_top_trader_long_short(self, symbol: str,
                                         interval: str = "4h") -> Optional[list]:
        """Get top trader long/short ratio (whale positioning)."""
        data = await self._request("/futures/top-long-short-account-ratio/history", params={
            "exchange": "Binance",
            "symbol": f"{symbol.upper()}USDT",
            "interval": interval,
            "limit": 3,
        })
        return data

    # ── Market Overview ───────────────────────────────────────────────────────

    async def get_oi_to_mcap_list(self) -> Optional[list]:
        """
        Get OI-to-market-cap ratios for all tokens.
        High OI/mcap ratio is a primary crime coin indicator.
        """
        data = await self._request("/futures/coins-markets")
        return data

    async def get_volume_to_oi(self, symbol: str) -> Optional[dict]:
        """Get CoinGlass's OI-to-volume ratio row for one symbol (v4)."""
        data = await self.get_oi_to_mcap_list()
        if not isinstance(data, list):
            return None
        target = symbol.upper().replace("$", "").replace("/USDT", "")
        target = target[:-4] if target.endswith("USDT") else target
        return next((item for item in data if isinstance(item, dict)
                     and str(item.get("symbol", "")).upper() == target), None)

    # ── Aggregated Analysis Methods ───────────────────────────────────────────

    async def get_full_derivatives_snapshot(self, symbol: str) -> dict:
        """
        Get a complete derivatives snapshot for crime pump analysis.
        Combines OI, funding, liquidations, and long/short data.
        """
        cache_key = symbol.upper().replace("$", "").replace("/USDT", "")
        cached = self._snapshot_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < 60:
            snapshot = dict(cached[1])
            snapshot["sources_failed"] = list(cached[1].get("sources_failed", []))
            return snapshot

        snapshot = {
            "symbol": symbol,
            "available": False,
            "open_interest": None,
            "oi_by_exchange": None,
            "funding_rates": None,
            "weighted_funding": None,
            "long_short_ratio": None,
            "top_trader_long_short": None,
            "liquidations": None,
            "oi_history": None,
            "funding_history": None,
            "long_short_history": None,
            "liquidation_heatmap": None,
            "sources_failed": [],
        }

        if not self.api_key:
            snapshot["sources_failed"] = [
                "open_interest", "oi_by_exchange", "funding_rates",
                "weighted_funding", "long_short_ratio", "top_trader_long_short", "liquidations",
                "oi_history", "funding_history", "long_short_history",
                "liquidation_heatmap",
            ]
            return snapshot

        # Parallel data fetching
        import asyncio
        results = await asyncio.gather(
            self.get_open_interest(symbol),
            self.get_funding_rate(symbol),
            self.get_weighted_funding_rate(symbol),
            self.get_long_short_ratio(symbol),
            self.get_top_trader_long_short(symbol, interval="4h"),
            self.get_liquidation_data(symbol),
            self.get_open_interest_history(symbol, interval="4h", limit=50),
            self.get_funding_rate_history(symbol, limit=50, interval="4h"),
            self.get_liquidation_heatmap(symbol),
            return_exceptions=True,
        )

        field_names = [
            "open_interest", "funding_rates",
            "weighted_funding", "long_short_ratio", "top_trader_long_short", "liquidations",
            "oi_history", "funding_history", "liquidation_heatmap",
        ]

        for name, result in zip(field_names, results):
            if isinstance(result, Exception):
                snapshot["sources_failed"].append(name)
                log.warning("derivatives_source_failed", symbol=symbol,
                           source=name, error=str(result))
            else:
                snapshot[name] = result

        snapshot["oi_by_exchange"] = snapshot["open_interest"]
        snapshot["long_short_history"] = snapshot["long_short_ratio"]

        snapshot["available"] = any(snapshot.get(name) is not None for name in field_names)
        self._snapshot_cache[cache_key] = (time.monotonic(), snapshot)

        return snapshot

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
