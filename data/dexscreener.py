"""
DexScreener API Client

Free API (no key required) for DEX token data including prices, volume,
liquidity, pair info, and token profiles across all supported chains.
Used as the primary scanner for detecting volume anomalies and new tokens.
"""

import asyncio
import httpx
from typing import Optional
from datetime import datetime
from utils.logger import get_logger
from config.settings import DEXSCREENER_BASE_URL, DEXSCREENER_RATE_LIMIT

log = get_logger("dexscreener")


class DexScreenerClient:
    """Async client for the DexScreener API."""

    def __init__(self):
        self.base_url = DEXSCREENER_BASE_URL
        self.rate_limit = DEXSCREENER_RATE_LIMIT
        self._request_count = 0
        self._rate_reset = datetime.utcnow()
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=30.0,
                headers={"Accept": "application/json"},
            )
        return self._client

    async def _request(self, endpoint: str, params: dict = None) -> Optional[dict]:
        """Make a rate-limited GET request."""
        try:
            client = await self._get_client()
            response = await client.get(endpoint, params=params)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            log.error("dexscreener_http_error", status=e.response.status_code,
                      endpoint=endpoint)
            return None
        except Exception as e:
            log.error("dexscreener_error", error=str(e), endpoint=endpoint)
            return None

    async def search_tokens(self, query: str) -> list[dict]:
        """
        Search for tokens by name, symbol, or address.
        Returns a list of matching pairs.
        """
        data = await self._request(f"/latest/dex/search", params={"q": query})
        if data and "pairs" in data:
            log.info("dexscreener_search", query=query, results=len(data["pairs"]))
            return data["pairs"]
        return []

    async def get_pairs_by_chain(self, chain: str, pair_addresses: list[str]) -> list[dict]:
        """
        Get pair data for specific addresses on a chain.
        chain: 'ethereum', 'bsc', 'solana', 'base', 'arbitrum', etc.
        """
        addresses = ",".join(pair_addresses[:30])  # Max 30 per request
        data = await self._request(f"/latest/dex/pairs/{chain}/{addresses}")
        if data and "pairs" in data:
            return data["pairs"]
        return []

    async def get_token_pairs(self, token_address: str) -> list[dict]:
        """Get all DEX pairs for a token address across all chains."""
        data = await self._request(f"/latest/dex/tokens/{token_address}")
        if data and "pairs" in data:
            return data["pairs"]
        return []

    async def get_token_profiles(self) -> list[dict]:
        """Get the latest token profiles (recently updated/boosted tokens)."""
        data = await self._request("/token-profiles/latest/v1")
        if isinstance(data, list):
            return data
        return []

    async def get_boosted_tokens(self) -> list[dict]:
        """Get tokens that are currently boosted (paid promotion on DexScreener)."""
        data = await self._request("/token-boosts/latest/v1")
        if isinstance(data, list):
            return data
        return []

    async def get_top_boosted_tokens(self) -> list[dict]:
        """Get tokens with the most active boosts."""
        data = await self._request("/token-boosts/top/v1")
        if isinstance(data, list):
            return data
        return []

    async def get_orders_by_token(self, chain: str, token_address: str) -> list[dict]:
        """Check if a token has paid orders (ads) on DexScreener."""
        data = await self._request(f"/orders/v1/{chain}/{token_address}")
        if isinstance(data, list):
            return data
        return []

    async def scan_volume_anomalies(self, min_volume_spike: float = 4.0,
                                     min_liquidity: float = 10000,
                                     max_mcap: float = 50_000_000) -> list[dict]:
        """
        Scan for tokens showing sudden volume spikes.
        Uses token profiles and boosted tokens as a starting point,
        then filters for anomaly patterns.

        min_volume_spike: Minimum ratio of current volume vs baseline (4.0 = 400%)
        min_liquidity: Minimum liquidity in USD
        max_mcap: Maximum market cap (focus on low/mid cap manipulation targets)
        """
        anomalies = []

        # Get recently active tokens from profiles and boosts
        profiles = await self.get_token_profiles()
        boosted = await self.get_boosted_tokens()

        # Collect unique token addresses to scan
        token_addresses = set()
        for profile in profiles:
            if "tokenAddress" in profile:
                token_addresses.add(profile["tokenAddress"])
        for boost in boosted:
            if "tokenAddress" in boost:
                token_addresses.add(boost["tokenAddress"])

        log.info("scanning_tokens", count=len(token_addresses))

        # Analyze each token
        for address in list(token_addresses)[:50]:  # Limit to 50 per scan cycle
            pairs = await self.get_token_pairs(address)
            if not pairs:
                continue

            # Use the highest liquidity pair as primary
            primary = max(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))

            liquidity = float(primary.get("liquidity", {}).get("usd", 0) or 0)
            mcap = float(primary.get("marketCap", 0) or primary.get("fdv", 0) or 0)
            volume_24h = float(primary.get("volume", {}).get("h24", 0) or 0)
            volume_1h = float(primary.get("volume", {}).get("h1", 0) or 0)
            volume_5m = float(primary.get("volume", {}).get("m5", 0) or 0)

            if liquidity < min_liquidity or mcap > max_mcap or mcap == 0:
                continue

            # Calculate volume spike ratios
            hourly_rate = volume_24h / 24 if volume_24h > 0 else 1
            volume_spike_1h = volume_1h / hourly_rate if hourly_rate > 0 else 0

            # 5-minute volume annualized vs hourly baseline
            five_min_rate = volume_5m * 12  # Annualize to hourly
            volume_spike_5m = five_min_rate / hourly_rate if hourly_rate > 0 else 0

            if volume_spike_1h >= min_volume_spike or volume_spike_5m >= min_volume_spike:
                price_change_5m = float(primary.get("priceChange", {}).get("m5", 0) or 0)
                price_change_1h = float(primary.get("priceChange", {}).get("h1", 0) or 0)

                anomalies.append({
                    "symbol": primary.get("baseToken", {}).get("symbol", "UNKNOWN"),
                    "name": primary.get("baseToken", {}).get("name", ""),
                    "address": primary.get("baseToken", {}).get("address", ""),
                    "chain": primary.get("chainId", ""),
                    "dex": primary.get("dexId", ""),
                    "pair_address": primary.get("pairAddress", ""),
                    "price": float(primary.get("priceUsd", 0) or 0),
                    "market_cap": mcap,
                    "liquidity": liquidity,
                    "volume_24h": volume_24h,
                    "volume_1h": volume_1h,
                    "volume_5m": volume_5m,
                    "volume_spike_1h": round(volume_spike_1h, 2),
                    "volume_spike_5m": round(volume_spike_5m, 2),
                    "price_change_5m": price_change_5m,
                    "price_change_1h": price_change_1h,
                    "pair_created_at": primary.get("pairCreatedAt"),
                    "txns_5m_buys": primary.get("txns", {}).get("m5", {}).get("buys", 0),
                    "txns_5m_sells": primary.get("txns", {}).get("m5", {}).get("sells", 0),
                    "txns_1h_buys": primary.get("txns", {}).get("h1", {}).get("buys", 0),
                    "txns_1h_sells": primary.get("txns", {}).get("h1", {}).get("sells", 0),
                })

        log.info("volume_anomalies_found", count=len(anomalies))
        return sorted(anomalies, key=lambda x: x["volume_spike_1h"], reverse=True)

    def extract_token_metrics(self, pair_data: dict) -> dict:
        """Extract standardized metrics from a DexScreener pair response."""
        return {
            "symbol": pair_data.get("baseToken", {}).get("symbol", "UNKNOWN"),
            "name": pair_data.get("baseToken", {}).get("name", ""),
            "address": pair_data.get("baseToken", {}).get("address", ""),
            "chain": pair_data.get("chainId", ""),
            "dex": pair_data.get("dexId", ""),
            "pair_address": pair_data.get("pairAddress", ""),
            "price": float(pair_data.get("priceUsd", 0) or 0),
            "price_native": float(pair_data.get("priceNative", 0) or 0),
            "market_cap": float(pair_data.get("marketCap", 0) or pair_data.get("fdv", 0) or 0),
            "liquidity": float(pair_data.get("liquidity", {}).get("usd", 0) or 0),
            "volume_24h": float(pair_data.get("volume", {}).get("h24", 0) or 0),
            "volume_6h": float(pair_data.get("volume", {}).get("h6", 0) or 0),
            "volume_1h": float(pair_data.get("volume", {}).get("h1", 0) or 0),
            "volume_5m": float(pair_data.get("volume", {}).get("m5", 0) or 0),
            "price_change_5m": float(pair_data.get("priceChange", {}).get("m5", 0) or 0),
            "price_change_1h": float(pair_data.get("priceChange", {}).get("h1", 0) or 0),
            "price_change_6h": float(pair_data.get("priceChange", {}).get("h6", 0) or 0),
            "price_change_24h": float(pair_data.get("priceChange", {}).get("h24", 0) or 0),
            "txns_5m_buys": pair_data.get("txns", {}).get("m5", {}).get("buys", 0),
            "txns_5m_sells": pair_data.get("txns", {}).get("m5", {}).get("sells", 0),
            "txns_1h_buys": pair_data.get("txns", {}).get("h1", {}).get("buys", 0),
            "txns_1h_sells": pair_data.get("txns", {}).get("h1", {}).get("sells", 0),
            "txns_24h_buys": pair_data.get("txns", {}).get("h24", {}).get("buys", 0),
            "txns_24h_sells": pair_data.get("txns", {}).get("h24", {}).get("sells", 0),
            "pair_created_at": pair_data.get("pairCreatedAt"),
        }

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
