"""
Data Pipeline

Central data aggregation layer that pulls data from all sources
(DexScreener, Coinglass, blockchain explorers, CCXT) and builds
unified Token objects for the detection engine.
"""

import asyncio
from typing import Optional
from datetime import datetime

from models.token import Token, TokenMetrics, OnChainMetrics, DerivativesMetrics, ExchangePresence
from models import Chain
from data.dexscreener import DexScreenerClient
from data.coinglass import CoinglassClient
from data.blockchain_explorers import (
    create_etherscan_client,
    create_bscscan_client,
    create_solscan_client,
    EVMExplorerClient,
    SolscanClient,
)
from utils.logger import get_logger

log = get_logger("data_pipeline")

# Chain ID to Chain enum mapping
CHAIN_MAP = {
    "ethereum": Chain.ETHEREUM,
    "eth": Chain.ETHEREUM,
    "bsc": Chain.BSC,
    "solana": Chain.SOLANA,
    "base": Chain.BASE,
    "arbitrum": Chain.ARBITRUM,
    "polygon": Chain.POLYGON,
    "avalanche": Chain.AVALANCHE,
}


class DataPipeline:
    """
    Aggregates data from all sources into unified Token objects.

    Acts as Layer 1 of the system architecture, feeding the strategy
    engines and detection layers.
    """

    def __init__(self):
        self.dexscreener = DexScreenerClient()
        self.coinglass = CoinglassClient()
        self.etherscan = create_etherscan_client()
        self.bscscan = create_bscscan_client()
        self.solscan = create_solscan_client()

        self._source_status = {
            "dexscreener": True,
            "coinglass": True,
            "etherscan": True,
            "bscscan": True,
            "solscan": True,
        }

    async def build_token_profile(self, query: str) -> Optional[Token]:
        """
        Build a complete Token profile from all available data sources.

        Price priority: Bybit (real-time) → CoinGecko (CEX aggregate) → DexScreener (DEX)
        query can be a ticker symbol or contract address.
        """
        log.info("building_token_profile", query=query)

        # Step 1: Try exchange APIs for the most accurate real-time price
        # Priority: Bybit → CoinGecko → DexScreener
        exchange_price = await self._get_bybit_price(query)
        if not exchange_price:
            exchange_price = await self._get_coingecko_price(query)

        price_source = "bybit" if exchange_price else "pending"

        # Step 2: Get data from DexScreener (for metadata, volume, liquidity)
        pairs = await self.dexscreener.search_tokens(query)
        if not pairs:
            # Try as direct token address
            pairs = await self.dexscreener.get_token_pairs(query)

        if not pairs:
            log.warning("token_not_found", query=query)
            return None

        # Use the highest liquidity pair as primary
        primary = max(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))
        metrics = self.dexscreener.extract_token_metrics(primary)

        # Build the Token object
        chain_id = metrics.get("chain", "").lower()
        chain = CHAIN_MAP.get(chain_id)

        # Use exchange price if available (most accurate), else DexScreener
        if exchange_price:
            price = exchange_price
        else:
            price = metrics["price"]
            price_source = "dexscreener"

        token = Token(
            symbol=metrics["symbol"],
            name=metrics.get("name"),
            contract_address=metrics.get("address"),
            chain=chain,
            pair_address=metrics.get("pair_address"),
            dex=metrics.get("dex"),
            metrics=TokenMetrics(
                price=price,
                market_cap=metrics["market_cap"],
                volume_24h=metrics["volume_24h"],
                volume_1h=metrics["volume_1h"],
                volume_5m=metrics["volume_5m"],
                liquidity_usd=metrics["liquidity"],
                price_change_1h=metrics["price_change_1h"],
                price_change_24h=metrics["price_change_24h"],
            ),
        )

        # Step 3: Enrich with derivatives data (Coinglass)
        await self._enrich_derivatives(token)

        # Step 4: Enrich with on-chain data
        await self._enrich_onchain(token)

        token.last_updated = datetime.utcnow()
        log.info("token_profile_built", symbol=token.symbol,
                price=token.metrics.price, mcap=token.metrics.market_cap,
                source=price_source)

        return token

    async def _get_bybit_price(self, query: str) -> Optional[float]:
        """
        Get real-time price from Bybit public API.
        No API key needed. Tries both spot and perpetual markets.
        """
        import aiohttp

        ticker = query.upper().replace("$", "")

        # Skip contract addresses
        if len(ticker) > 10 or ticker.startswith("0X"):
            return None

        try:
            async with aiohttp.ClientSession() as session:
                # Try spot market first (TOKEN/USDT)
                symbol = f"{ticker}USDT"
                url = "https://api.bybit.com/v5/market/tickers"

                async with session.get(
                    url,
                    params={"category": "spot", "symbol": symbol},
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        result = data.get("result", {})
                        tickers = result.get("list", [])
                        if tickers:
                            price = float(tickers[0].get("lastPrice", 0))
                            if price > 0:
                                log.info("bybit_price", symbol=ticker, price=price,
                                        market="spot")
                                return price

                # Fallback: try perpetual/futures
                async with session.get(
                    url,
                    params={"category": "linear", "symbol": symbol},
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        result = data.get("result", {})
                        tickers = result.get("list", [])
                        if tickers:
                            price = float(tickers[0].get("lastPrice", 0))
                            if price > 0:
                                log.info("bybit_price", symbol=ticker, price=price,
                                        market="linear")
                                return price

        except Exception as e:
            log.warning("bybit_price_failed", symbol=ticker, error=str(e))

        return None

    async def _get_coingecko_price(self, query: str) -> Optional[float]:
        """
        Get accurate price from CoinGecko for CEX-listed tokens.

        Uses dynamic search — no hardcoded mapping needed.
        Searches CoinGecko by symbol, picks the highest-ranked result,
        then fetches the current price.
        """
        import aiohttp

        ticker = query.upper().replace("$", "")

        # Skip contract addresses — CoinGecko uses IDs not addresses
        if len(ticker) > 42 or ticker.startswith("0X"):
            return None

        try:
            async with aiohttp.ClientSession() as session:
                # Step 1: Search CoinGecko for the token by symbol
                search_url = "https://api.coingecko.com/api/v3/search"
                async with session.get(
                    search_url,
                    params={"query": ticker},
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as resp:
                    if resp.status != 200:
                        return None
                    search_data = await resp.json()

                coins = search_data.get("coins", [])
                if not coins:
                    return None

                # Step 2: Find the best match — same symbol + highest rank
                # CoinGecko returns results sorted by market cap rank
                coin_id = None
                for coin in coins:
                    coin_symbol = coin.get("symbol", "").upper()
                    if coin_symbol == ticker:
                        coin_id = coin.get("id")
                        break  # First match with exact symbol = highest ranked

                if not coin_id:
                    # No exact symbol match, use first result
                    coin_id = coins[0].get("id")

                if not coin_id:
                    return None

                # Step 3: Get the price
                price_url = "https://api.coingecko.com/api/v3/simple/price"
                async with session.get(
                    price_url,
                    params={"ids": coin_id, "vs_currencies": "usd"},
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if coin_id in data and "usd" in data[coin_id]:
                            price = data[coin_id]["usd"]
                            log.info("coingecko_price",
                                    symbol=ticker, coin_id=coin_id, price=price)
                            return float(price)

        except Exception as e:
            log.warning("coingecko_price_failed", symbol=ticker, error=str(e))

        return None

    async def fetch_multi_timeframe_candles(self, symbol: str) -> dict:
        """
        Fetch OHLCV candle data from multiple timeframes.

        Priority: Bybit API (accurate, no key needed) → CoinGecko (DEX fallback)
        Returns: {"15m": [candles], "1h": [candles], "4h": [candles]}
        Each candle: {"open": float, "high": float, "low": float,
                      "close": float, "volume": float, "timestamp": int}
        """
        result = {}
        ticker = symbol.upper().replace("$", "")

        # Skip contract addresses
        if len(ticker) > 10 or ticker.startswith("0X"):
            return result

        # Try Bybit first (most accurate, no API key needed)
        bybit_data = await self._fetch_bybit_candles(ticker)
        if bybit_data:
            return bybit_data

        # Fallback: CoinGecko (for DEX tokens not on Bybit)
        cg_data = await self._fetch_coingecko_candles(ticker)
        if cg_data:
            return cg_data

        return result

    async def _fetch_bybit_candles(self, ticker: str) -> dict:
        """Fetch candle data from Bybit public API (no API key needed)."""
        import aiohttp

        result = {}
        symbol = f"{ticker}USDT"

        timeframe_map = {
            "15m": {"interval": "15", "limit": 100},   # 100 x 15min = ~25h
            "1h":  {"interval": "60", "limit": 100},   # 100 x 1h = ~4 days
            "4h":  {"interval": "240", "limit": 100},   # 100 x 4h = ~16 days
        }

        try:
            async with aiohttp.ClientSession() as session:
                for tf_name, params in timeframe_map.items():
                    url = "https://api.bybit.com/v5/market/kline"
                    async with session.get(
                        url,
                        params={
                            "category": "linear",
                            "symbol": symbol,
                            "interval": params["interval"],
                            "limit": params["limit"],
                        },
                        timeout=aiohttp.ClientTimeout(total=8),
                    ) as resp:
                        if resp.status != 200:
                            continue

                        data = await resp.json()
                        klines = data.get("result", {}).get("list", [])

                        if not klines:
                            # Try spot market
                            async with session.get(
                                url,
                                params={
                                    "category": "spot",
                                    "symbol": symbol,
                                    "interval": params["interval"],
                                    "limit": params["limit"],
                                },
                                timeout=aiohttp.ClientTimeout(total=8),
                            ) as resp2:
                                if resp2.status == 200:
                                    data2 = await resp2.json()
                                    klines = data2.get("result", {}).get("list", [])

                        if klines:
                            # Bybit returns [timestamp, open, high, low, close, volume, turnover]
                            # Most recent first — reverse for chronological order
                            candles = []
                            for k in reversed(klines):
                                candles.append({
                                    "timestamp": int(k[0]),
                                    "open": float(k[1]),
                                    "high": float(k[2]),
                                    "low": float(k[3]),
                                    "close": float(k[4]),
                                    "volume": float(k[5]),
                                })
                            result[tf_name] = candles

                if result:
                    log.info("bybit_candles_fetched", symbol=ticker,
                            timeframes=list(result.keys()),
                            counts={k: len(v) for k, v in result.items()})

        except Exception as e:
            log.warning("bybit_candles_failed", symbol=ticker, error=str(e))

        return result

    async def _fetch_coingecko_candles(self, ticker: str) -> dict:
        """Fetch candle data from CoinGecko (for DEX tokens not on Bybit)."""
        import aiohttp

        result = {}

        try:
            async with aiohttp.ClientSession() as session:
                # Step 1: Resolve CoinGecko coin ID
                search_url = "https://api.coingecko.com/api/v3/search"
                async with session.get(
                    search_url,
                    params={"query": ticker},
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as resp:
                    if resp.status != 200:
                        return result
                    search_data = await resp.json()

                coins = search_data.get("coins", [])
                coin_id = None
                for coin in coins:
                    if coin.get("symbol", "").upper() == ticker:
                        coin_id = coin.get("id")
                        break
                if not coin_id and coins:
                    coin_id = coins[0].get("id")
                if not coin_id:
                    return result

                # Step 2: Fetch OHLC data (CoinGecko free = 1d/7d/14d/30d)
                # 1 day = gives ~6min candles → use as "15m" equivalent
                # 7 days = gives ~hourly candles → use as "1h"
                # 30 days = gives ~4h candles → use as "4h"
                ohlc_map = {
                    "15m": {"days": "1"},
                    "1h": {"days": "7"},
                    "4h": {"days": "30"},
                }

                for tf_name, params in ohlc_map.items():
                    ohlc_url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/ohlc"
                    async with session.get(
                        ohlc_url,
                        params={"vs_currency": "usd", "days": params["days"]},
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        if resp.status != 200:
                            continue
                        ohlc_data = await resp.json()

                        if ohlc_data and isinstance(ohlc_data, list):
                            candles = []
                            for entry in ohlc_data:
                                if len(entry) >= 5:
                                    candles.append({
                                        "timestamp": int(entry[0]),
                                        "open": float(entry[1]),
                                        "high": float(entry[2]),
                                        "low": float(entry[3]),
                                        "close": float(entry[4]),
                                        "volume": 0,  # CoinGecko OHLC doesn't include volume
                                    })
                            if candles:
                                result[tf_name] = candles

                    # Rate limit: CoinGecko free = 10-30 req/min
                    await asyncio.sleep(1.5)

                if result:
                    log.info("coingecko_candles_fetched", symbol=ticker,
                            timeframes=list(result.keys()),
                            counts={k: len(v) for k, v in result.items()})

        except Exception as e:
            log.warning("coingecko_candles_failed", symbol=ticker, error=str(e))

        return result

    async def _enrich_derivatives(self, token: Token) -> None:
        """Enrich token with derivatives data from Coinglass."""
        try:
            snapshot = await self.coinglass.get_full_derivatives_snapshot(token.symbol)

            if not snapshot.get("available"):
                self._source_status["coinglass"] = False
                return

            # Open interest
            oi_data = snapshot.get("open_interest")
            if oi_data:
                if isinstance(oi_data, dict):
                    token.derivatives.open_interest = float(oi_data.get("openInterest", 0) or 0)
                elif isinstance(oi_data, list) and len(oi_data) > 0:
                    token.derivatives.open_interest = sum(
                        float(item.get("openInterest", 0) or 0) for item in oi_data
                    )

            # Calculate OI to market cap ratio
            if token.metrics.market_cap > 0 and token.derivatives.open_interest > 0:
                token.derivatives.oi_to_mcap_ratio = (
                    token.derivatives.open_interest / token.metrics.market_cap
                )

            # OI by exchange distribution
            oi_by_exchange = snapshot.get("oi_by_exchange")
            if oi_by_exchange and isinstance(oi_by_exchange, list):
                for item in oi_by_exchange:
                    ex = item.get("exchange", item.get("exchangeName", "")).lower()
                    oi = float(item.get("openInterest", 0) or 0)
                    if ex and oi > 0:
                        token.derivatives.exchange_oi_distribution[ex] = oi

            # Funding rates
            funding_data = snapshot.get("funding_rates")
            if funding_data and isinstance(funding_data, list):
                # Use the weighted average or first available
                rates = [float(f.get("fundingRate", f.get("rate", 0)) or 0)
                        for f in funding_data if f.get("fundingRate") or f.get("rate")]
                if rates:
                    token.derivatives.funding_rate = sum(rates) / len(rates)

            # Weighted funding
            weighted = snapshot.get("weighted_funding")
            if weighted and isinstance(weighted, dict):
                token.derivatives.funding_rate = float(
                    weighted.get("weightedRate", token.derivatives.funding_rate) or
                    token.derivatives.funding_rate
                )

            # Long/short ratio
            ls_data = snapshot.get("long_short_ratio")
            if ls_data and isinstance(ls_data, list) and len(ls_data) > 0:
                latest = ls_data[-1] if isinstance(ls_data[-1], dict) else {}
                token.derivatives.long_short_ratio = float(
                    latest.get("longShortRatio", latest.get("longRate", 1)) or 1
                )

            # Liquidation data
            liq_data = snapshot.get("liquidations")
            if liq_data and isinstance(liq_data, list):
                for item in liq_data:
                    long_liq = float(item.get("longLiquidationUsd", 0) or 0)
                    short_liq = float(item.get("shortLiquidationUsd", 0) or 0)
                    token.derivatives.liquidations_long_24h += long_liq
                    token.derivatives.liquidations_short_24h += short_liq

            self._source_status["coinglass"] = True

        except Exception as e:
            log.error("derivatives_enrich_failed", symbol=token.symbol, error=str(e))
            self._source_status["coinglass"] = False

    async def _enrich_onchain(self, token: Token) -> None:
        """Enrich token with on-chain data from blockchain explorers."""
        if not token.contract_address or not token.chain:
            return

        try:
            explorer = self._get_explorer(token.chain)
            if explorer is None:
                return

            if isinstance(explorer, EVMExplorerClient):
                # Get contract age
                age = await explorer.get_contract_age_days(token.contract_address)
                if age is not None:
                    token.onchain.contract_age_days = age

                # Get recent transfers for clustering analysis
                transfers = await explorer.get_token_transfers(
                    token.contract_address, offset=200
                )
                if transfers:
                    token.onchain.transfer_count_24h = len(transfers)

            elif isinstance(explorer, SolscanClient):
                # Get token metadata
                meta = await explorer.get_token_meta(token.contract_address)
                if meta and isinstance(meta, dict):
                    data = meta.get("data", meta)
                    if isinstance(data, dict):
                        supply = data.get("supply", 0)
                        if supply:
                            token.metrics.total_supply = float(supply)

        except Exception as e:
            log.error("onchain_enrich_failed", symbol=token.symbol, error=str(e))

    def _get_explorer(self, chain: Chain):
        """Get the appropriate blockchain explorer for a chain."""
        if chain == Chain.ETHEREUM:
            return self.etherscan
        elif chain == Chain.BSC:
            return self.bscscan
        elif chain == Chain.SOLANA:
            return self.solscan
        return None

    async def get_wallet_analysis(self, token: Token) -> Optional[dict]:
        """Get wallet clustering and concentration analysis for a token."""
        if not token.contract_address or not token.chain:
            return None

        try:
            explorer = self._get_explorer(token.chain)
            if isinstance(explorer, EVMExplorerClient):
                return await explorer.analyze_wallet_cluster(token.contract_address)
        except Exception as e:
            log.error("wallet_analysis_failed", symbol=token.symbol, error=str(e))

        return None

    async def get_derivatives_snapshot(self, token: Token) -> Optional[dict]:
        """Get the full derivatives snapshot for a token."""
        try:
            return await self.coinglass.get_full_derivatives_snapshot(token.symbol)
        except Exception as e:
            log.error("derivatives_snapshot_failed", symbol=token.symbol, error=str(e))
            return None

    async def scan_for_anomalies(self) -> list[dict]:
        """
        Scan DexScreener for tokens showing volume anomalies.
        Used by the autonomous monitoring system.
        """
        try:
            anomalies = await self.dexscreener.scan_volume_anomalies()
            self._source_status["dexscreener"] = True
            return anomalies
        except Exception as e:
            log.error("anomaly_scan_failed", error=str(e))
            self._source_status["dexscreener"] = False
            return []

    def get_source_status(self) -> dict:
        """Get the availability status of all data sources."""
        return dict(self._source_status)

    async def close(self) -> None:
        """Close all data source connections."""
        await self.dexscreener.close()
        await self.coinglass.close()
        await self.etherscan.close()
        await self.bscscan.close()
        await self.solscan.close()
