"""
Data Pipeline

Central data aggregation layer that pulls data from all sources
(DexScreener, Coinglass, blockchain explorers, CCXT) and builds
unified Token objects for the detection engine.
"""

import asyncio
from decimal import Decimal, InvalidOperation
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
from data.social_monitor import SocialMonitor
from frameworks.hummingbot_adapter import HummingbotAdapter
from utils.logger import get_logger

log = get_logger("data_pipeline")


def number_value(data: dict, *keys) -> Optional[float]:
    """Read the first finite numeric value from a provider response mapping."""
    if not isinstance(data, dict):
        return None
    for key in keys:
        value = data.get(key)
        if value in (None, ""):
            continue
        try:
            return float(value)
        except (ValueError, TypeError):
            continue
    return None


def decimal_value(data: dict, *keys) -> Optional[Decimal]:
    """Read the first finite decimal value without losing raw token precision."""
    if not isinstance(data, dict):
        return None
    for key in keys:
        value = data.get(key)
        if value in (None, ""):
            continue
        try:
            number = Decimal(str(value))
            if number.is_finite():
                return number
        except (InvalidOperation, ValueError, TypeError):
            continue
    return None

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
        self.social_monitor = SocialMonitor()
        self.hummingbot = HummingbotAdapter()

        self._source_status = {
            "dexscreener": True,
            "coinglass": True,
            "etherscan": True,
            "bscscan": True,
            "solscan": True,
            "social": bool(self.social_monitor.twitter_token),
            "market_microstructure": self.hummingbot.available,
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
            # If we have an exchange price but no DexScreener data,
            # build a minimal token so CEX-only tokens (e.g., BRETT) still work
            if exchange_price:
                ticker = query.upper().replace("$", "")
                token = Token(
                    symbol=ticker,
                    name=ticker,
                    metrics=TokenMetrics(
                        price=exchange_price,
                        market_cap=0,
                        volume_24h=0,
                        volume_1h=0,
                        volume_5m=0,
                        liquidity_usd=0,
                        price_change_1h=0,
                        price_change_24h=0,
                    ),
                )
                await asyncio.gather(
                    self._enrich_derivatives(token),
                    self._enrich_social(token),
                    self._enrich_market_data(token),
                )
                token.last_updated = datetime.utcnow()
                log.info("token_profile_built_minimal", symbol=ticker,
                        price=exchange_price, source=price_source)
                return token

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

        # Preserve venue-level prices from the token's DEX pairs so Layer 4
        # can compare real markets instead of relying on a liquidity proxy.
        venue_prices = {}
        venue_liquidity = {}
        for pair in pairs:
            pair_address = str(pair.get("baseToken", {}).get("address", ""))
            if metrics.get("address") and pair_address.lower() != str(metrics["address"]).lower():
                continue
            venue = str(pair.get("dexId") or pair.get("chainId") or "").lower()
            pair_price = float(pair.get("priceUsd", 0) or 0)
            pair_liquidity = float(pair.get("liquidity", {}).get("usd", 0) or 0)
            if venue and pair_price > 0 and pair_liquidity >= venue_liquidity.get(venue, -1):
                venue_prices[venue] = pair_price
                venue_liquidity[venue] = pair_liquidity
        token.exchange_presence.exchange_price_spread = venue_prices
        token.exchange_presence.exchanges_listed = list(venue_prices)
        token.exchange_presence.primary_spot_exchange = metrics.get("dex")

        # Enrich independent sources concurrently; unavailable providers leave
        # their availability flags unset rather than turning unknown into zero.
        await asyncio.gather(
            self._enrich_derivatives(token),
            self._enrich_onchain(token),
            self._enrich_social(token),
            self._enrich_market_data(token),
        )

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
            from utils.coingecko import get_headers, get_params, search_url as cg_search_url
            async with aiohttp.ClientSession() as session:
                # Step 1: Search CoinGecko for the token by symbol
                async with session.get(
                    cg_search_url(),
                    params=get_params(query=ticker),
                    headers=get_headers(),
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
                from utils.coingecko import price_url as cg_price_url
                async with session.get(
                    cg_price_url(),
                    params=get_params(ids=coin_id, vs_currencies="usd"),
                    headers=get_headers(),
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
            from utils.coingecko import get_headers, get_params, search_url as cg_search_url
            async with aiohttp.ClientSession() as session:
                # Step 1: Resolve CoinGecko coin ID
                async with session.get(
                    cg_search_url(),
                    params=get_params(query=ticker),
                    headers=get_headers(),
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
                    from utils.coingecko import ohlc_url as cg_ohlc_url
                    async with session.get(
                        cg_ohlc_url(coin_id),
                        params=get_params(vs_currency="usd", days=params["days"]),
                        headers=get_headers(),
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

    async def _enrich_social(self, token: Token) -> None:
        """Populate social metrics only when the configured provider succeeds."""
        try:
            social = await self.social_monitor.get_token_social_metrics(token.symbol)
            token.social.twitter_available = bool(social.get("available"))
            if token.social.twitter_available:
                token.social.twitter_mentions_1h = int(social.get("mentions_1h", 0) or 0)
                token.social.twitter_mentions_24h = int(social.get("mentions_24h", 0) or 0)
                token.social.twitter_mention_velocity = float(
                    social.get("mention_velocity", 0) or 0
                )
                token.social.new_account_mention_pct = float(
                    social.get("new_account_pct", 0) or 0
                )
            # No Telegram search source is currently configured.
            token.social.telegram_available = False
            self._source_status["social"] = token.social.twitter_available
        except Exception as e:
            log.warning("social_enrich_failed", symbol=token.symbol, error=str(e))
            token.social.twitter_available = False
            self._source_status["social"] = False

    async def _enrich_market_data(self, token: Token) -> None:
        """Add public CEX prices and real order-book depth when available."""
        try:
            snapshot = await self.hummingbot.get_market_snapshot(token.symbol)
            prices = snapshot.get("exchange_prices", {})
            if prices:
                token.exchange_presence.exchange_price_spread.update(prices)
                token.exchange_presence.exchanges_listed = sorted(
                    set(token.exchange_presence.exchanges_listed) | set(prices)
                )
            order_book = snapshot.get("order_book")
            if order_book and order_book.get("bids") and order_book.get("asks"):
                token.order_book_data = order_book
            self._source_status["market_microstructure"] = bool(
                prices or token.exchange_presence.exchange_price_spread
                or token.order_book_data
            )
        except Exception as e:
            log.warning("market_microstructure_enrich_failed",
                        symbol=token.symbol, error=str(e))
            self._source_status["market_microstructure"] = False

    async def _enrich_derivatives(self, token: Token) -> None:
        """Enrich token with derivatives data from Coinglass."""
        try:
            snapshot = await self.coinglass.get_full_derivatives_snapshot(token.symbol)

            if not snapshot.get("available"):
                self._source_status["coinglass"] = False
                return

            def rows(data):
                if isinstance(data, list):
                    return [item for item in data if isinstance(item, dict)]
                if isinstance(data, dict):
                    for key in ("list", "data", "rows", "result"):
                        value = data.get(key)
                        if isinstance(value, list):
                            return [item for item in value if isinstance(item, dict)]
                    return [data]
                return []

            def number(item, *keys):
                if not isinstance(item, dict):
                    return None
                for key in keys:
                    value = item.get(key)
                    if value not in (None, ""):
                        try:
                            return float(value)
                        except (ValueError, TypeError):
                            continue
                return None

            # Open interest
            oi_data = snapshot.get("open_interest")
            oi_rows = rows(oi_data)
            aggregate_oi = next((
                number(item, "open_interest_usd", "openInterest", "open_interest", "oi", "value")
                for item in oi_rows if str(item.get("exchange", "")).lower() == "all"
            ), None)
            oi_values = [number(item, "open_interest_usd", "openInterest", "open_interest", "oi", "value")
                         for item in oi_rows]
            oi_values = [value for value in oi_values if value is not None and value > 0]
            if aggregate_oi and aggregate_oi > 0:
                token.derivatives.open_interest = aggregate_oi
                token.derivatives.open_interest_available = True
            elif oi_values:
                token.derivatives.open_interest = sum(oi_values)
                token.derivatives.open_interest_available = True
            aggregate_row = next((
                item for item in oi_rows if str(item.get("exchange", "")).lower() == "all"
            ), None)
            if aggregate_row:
                one_hour_change = number(aggregate_row, "open_interest_change_percent_1h")
                one_day_change = number(aggregate_row, "open_interest_change_percent_24h")
                if one_hour_change is not None:
                    token.derivatives.oi_change_1h = one_hour_change
                if one_day_change is not None:
                    token.derivatives.oi_change_24h = one_day_change

            # Calculate OI to market cap ratio
            if token.metrics.market_cap > 0 and token.derivatives.open_interest > 0:
                token.derivatives.oi_to_mcap_ratio = (
                    token.derivatives.open_interest / token.metrics.market_cap
                )

            # OI by exchange distribution
            oi_by_exchange = snapshot.get("oi_by_exchange")
            if oi_by_exchange:
                for item in rows(oi_by_exchange):
                    ex = str(item.get("exchange", item.get("exchangeName", ""))).lower()
                    if ex == "all":
                        continue
                    oi = number(item, "open_interest_usd", "openInterest", "open_interest", "oi", "value") or 0
                    if ex and oi > 0:
                        token.derivatives.exchange_oi_distribution[ex] = oi

            # Historical open interest supports the 1h/24h change and the
            # hedged-OI check. Use the provider's close value when available.
            oi_history = rows(snapshot.get("oi_history"))
            oi_points = []
            for item in oi_history:
                value = number(item, "close", "openInterest", "open_interest", "oi", "value")
                if value is not None and value > 0:
                    timestamp = number(item, "time", "timestamp", "t") or 0
                    oi_points.append((timestamp, value))
            oi_points.sort(key=lambda point: point[0])
            if len(oi_points) >= 2:
                previous, current = oi_points[-2][1], oi_points[-1][1]
                if previous > 0:
                    token.derivatives.oi_change_4h = (current - previous) / previous * 100
                    token.derivatives.oi_history_available = True
                if len(oi_points) >= 7 and oi_points[-7][1] > 0:
                    token.derivatives.oi_change_24h = (
                        (current - oi_points[-7][1]) / oi_points[-7][1] * 100
                    )

            # Funding rates
            funding_data = snapshot.get("funding_rates")
            rates = []
            for item in rows(funding_data):
                rates.extend(
                    rate for rate in (
                        number(rate_item, "funding_rate", "fundingRate", "rate")
                        for group in ("stablecoin_margin_list", "token_margin_list")
                        for rate_item in (item.get(group, []) or [])
                    ) if rate is not None
                )
                direct_rate = number(item, "fundingRate", "funding_rate", "rate")
                if direct_rate is not None:
                    rates.append(direct_rate)
            rates = [rate for rate in rates if rate is not None]
            if rates:
                token.derivatives.funding_rate = sum(rates) / len(rates)
                token.derivatives.funding_rate_available = True

            # Weighted funding
            weighted = snapshot.get("weighted_funding")
            weighted_rows = rows(weighted)
            weighted_rows.sort(key=lambda item: number(item, "time", "timestamp", "t") or 0)
            weighted_rate = (
                number(weighted_rows[-1], "close", "weightedRate", "weighted_rate", "rate")
                if weighted_rows else number(weighted, "weightedRate", "weighted_rate", "rate")
            )
            if weighted_rate is not None:
                token.derivatives.funding_rate = weighted_rate
                token.derivatives.funding_rate_available = True

            funding_history = rows(snapshot.get("funding_history"))
            funding_points = []
            for item in funding_history:
                rate = number(item, "close", "fundingRate", "funding_rate", "rate")
                timestamp = number(item, "time", "timestamp", "t") or 0
                if rate is not None:
                    funding_points.append((timestamp, rate))
            recent_funding = [point[1] for point in funding_points
                              if not point[0] or point[0] >= datetime.utcnow().timestamp() * 1000 - 7 * 86400 * 1000]
            if recent_funding:
                token.derivatives.funding_rate_avg_7d = (
                    sum(recent_funding) / len(recent_funding)
                )
                token.derivatives.funding_history_available = True

            # Long/short ratio
            ls_data = snapshot.get("long_short_ratio")
            ls_rows = rows(ls_data)
            ls_points = []
            for item in ls_rows:
                value = number(item, "global_account_long_short_ratio", "longShortRatio",
                               "long_short_ratio", "ratio", "longRate")
                if value is not None and value > 0:
                    timestamp = number(item, "time", "timestamp", "t") or 0
                    ls_points.append((timestamp, value))
            ls_points.sort(key=lambda point: point[0])
            if ls_points:
                token.derivatives.long_short_ratio = ls_points[-1][1]
                token.derivatives.long_short_ratio_available = True
                if len(ls_points) >= 2 and ls_points[-2][1] > 0:
                    token.derivatives.long_short_ratio_history_available = True
                    token.derivatives.long_short_ratio_change_4h = (
                        (ls_points[-1][1] - ls_points[-2][1]) / ls_points[-2][1] * 100
                    )

            # Top-trader positioning can qualify the generic account ratio
            # when evaluating whether an open-interest move was genuinely hedged.
            top_trader_points = []
            for item in rows(snapshot.get("top_trader_long_short")):
                value = number(item, "top_account_long_short_ratio", "top_position_long_short_ratio",
                               "longShortRatio", "long_short_ratio", "ratio")
                if value is not None and value > 0:
                    timestamp = number(item, "time", "timestamp", "t") or 0
                    top_trader_points.append((timestamp, value))
            top_trader_points.sort(key=lambda point: point[0])
            if top_trader_points:
                token.derivatives.top_trader_long_short_ratio = top_trader_points[-1][1]
                token.derivatives.top_trader_ratio_available = True
                if len(top_trader_points) >= 2:
                    previous = top_trader_points[-2][1]
                    if previous > 0:
                        token.derivatives.top_trader_ratio_history_available = True
                        token.derivatives.top_trader_long_short_ratio_change_4h = (
                            (top_trader_points[-1][1] - previous) / previous * 100
                        )

            # Liquidation data
            liq_data = snapshot.get("liquidations")
            liq_rows = rows(liq_data)
            if liq_rows:
                token.derivatives.liquidation_data_available = True
                for item in liq_rows:
                    long_liq = number(item, "aggregated_long_liquidation_usd", "longLiquidationUsd",
                                      "longLiquidation", "long_liquidation_usd") or 0
                    short_liq = number(item, "aggregated_short_liquidation_usd", "shortLiquidationUsd",
                                       "shortLiquidation", "short_liquidation_usd") or 0
                    token.derivatives.liquidations_long_24h += long_liq
                    token.derivatives.liquidations_short_24h += short_liq

            # Normalize liquidation heatmap records into price -> USD size.
            heatmap = snapshot.get("liquidation_heatmap")
            normalized_heatmap = {}
            heatmap_data = heatmap.get("data", heatmap) if isinstance(heatmap, dict) else heatmap
            if isinstance(heatmap_data, dict):
                y_axis = heatmap_data.get("y_axis", [])
                cells = heatmap_data.get("liquidation_leverage_data", [])
                for cell in cells if isinstance(cells, list) else []:
                    if not isinstance(cell, (list, tuple)) or len(cell) < 3:
                        continue
                    try:
                        y_index, amount = int(cell[1]), float(cell[2])
                        price = float(y_axis[y_index])
                        if price > 0 and amount > 0:
                            key = str(price)
                            normalized_heatmap[key] = normalized_heatmap.get(key, 0.0) + amount
                    except (ValueError, TypeError, IndexError):
                        continue
            heatmap_rows = rows(heatmap_data)
            for item in heatmap_rows:
                price = number(item, "price", "priceLevel", "price_level", "level")
                amount = number(item, "liquidationUsd", "liquidation_usd", "totalUsd",
                                "total", "value", "amount")
                if amount is None:
                    long_value = number(item, "longLiquidationUsd", "longLiquidation") or 0
                    short_value = number(item, "shortLiquidationUsd", "shortLiquidation") or 0
                    amount = long_value + short_value
                if price and amount and amount > 0:
                    key = str(price)
                    normalized_heatmap[key] = normalized_heatmap.get(key, 0.0) + amount
            if normalized_heatmap:
                token.derivatives.liquidation_heatmap = normalized_heatmap
                token.derivatives.liquidation_heatmap_available = True

            self._source_status["coinglass"] = any((
                token.derivatives.open_interest_available,
                token.derivatives.funding_rate_available,
                token.derivatives.long_short_ratio_available,
                token.derivatives.top_trader_ratio_available,
                token.derivatives.liquidation_data_available,
                token.derivatives.liquidation_heatmap_available,
            ))

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
                age, transfers, holders, supply = await asyncio.gather(
                    explorer.get_contract_age_days(token.contract_address),
                    explorer.get_token_transfers(token.contract_address, offset=1000),
                    explorer.get_token_holders(token.contract_address, offset=100),
                    explorer.get_token_supply(token.contract_address),
                    return_exceptions=True,
                )
                if isinstance(age, Exception):
                    age = None
                if isinstance(transfers, Exception):
                    transfers = []
                if isinstance(holders, Exception):
                    holders = []
                if isinstance(supply, Exception):
                    supply = None

                if age is not None:
                    token.onchain.contract_age_days = age
                    token.onchain.data_available = True

                if transfers:
                    now_ts = datetime.utcnow().timestamp()
                    timestamps = []
                    for transfer in transfers:
                        try:
                            timestamps.append(int(transfer.get("timeStamp", 0)))
                        except (ValueError, TypeError):
                            continue
                    last_24h = [ts for ts in timestamps if ts >= now_ts - 86400]
                    last_7d = [ts for ts in timestamps if ts >= now_ts - 7 * 86400]
                    token.onchain.transfer_count_24h = len(last_24h)
                    # Only publish a 7-day baseline when the returned page
                    # likely covers the complete window.
                    if len(transfers) < 1000 or (timestamps and min(timestamps) <= now_ts - 7 * 86400):
                        token.onchain.transfer_count_avg_7d = len(last_7d) / 7
                    token.onchain.data_available = True

                supply_decimal = Decimal(str(supply)) if supply is not None else None
                if holders and supply_decimal and supply_decimal > 0:
                    balances = []
                    for holder in holders:
                        amount = None
                        for key in ("TokenHolderQuantity", "tokenHolderQuantity", "balance", "quantity", "amount"):
                            if holder.get(key) not in (None, ""):
                                try:
                                    amount = Decimal(str(holder[key]))
                                    break
                                except (InvalidOperation, ValueError, TypeError):
                                    pass
                        if amount is not None and amount.is_finite() and amount >= 0:
                            balances.append(amount)
                    if balances:
                        balances.sort(reverse=True)
                        token.onchain.top10_wallet_pct = float(min(
                            Decimal("100"), sum(balances[:10]) / supply_decimal * 100
                        ))
                        token.onchain.top20_wallet_pct = float(min(
                            Decimal("100"), sum(balances[:20]) / supply_decimal * 100
                        ))
                        token.onchain.holder_concentration_source = "evm_explorer_or_transfer_index"
                        token.onchain.data_available = True

            elif isinstance(explorer, SolscanClient):
                meta, holders = await asyncio.gather(
                    explorer.get_token_meta(token.contract_address),
                    explorer.get_token_holders(token.contract_address),
                    return_exceptions=True,
                )
                if isinstance(meta, Exception):
                    meta = None
                if isinstance(holders, Exception):
                    holders = None
                meta_data = meta.get("data", meta) if isinstance(meta, dict) else {}
                holder_data = holders.get("data", holders) if isinstance(holders, dict) else {}
                supply = decimal_value(meta_data, "supply", "totalSupply", "total_supply")
                if supply and supply > 0:
                    token.metrics.total_supply = float(supply)
                    token.onchain.data_available = True
                if isinstance(holder_data, dict) and holder_data.get("supply_reconciled") is True:
                    top_holders = holder_data.get("top_holders", [])
                    if isinstance(top_holders, list):
                        token.onchain.top_holders = [
                            {
                                "address": row["address"],
                                "percentage": float(row["percentage"]),
                            }
                            for row in top_holders
                            if isinstance(row, dict)
                            and row.get("address")
                            and number_value(row, "percentage") is not None
                        ]
                    holder_count = holder_data.get("holder_count")
                    try:
                        token.onchain.unique_holders = int(holder_count)
                    except (ValueError, TypeError):
                        token.onchain.unique_holders = None
                    token.onchain.holder_distribution_verified = bool(
                        token.onchain.top_holders and token.onchain.unique_holders
                    )
                concentration = holder_data.get("concentration", {}) if isinstance(holder_data, dict) else {}
                if isinstance(concentration, dict):
                    top10 = number_value(concentration, "top10_wallet_pct")
                    top20 = number_value(concentration, "top20_wallet_pct")
                    top20_accounts = number_value(concentration, "top20_account_pct")
                    if top10 is not None:
                        token.onchain.top10_wallet_pct = top10
                    if top20 is not None:
                        token.onchain.top20_wallet_pct = top20
                    if top20_accounts is not None:
                        token.onchain.top20_account_pct = top20_accounts
                    if any(value is not None for value in (top10, top20, top20_accounts)):
                        token.onchain.holder_concentration_source = (
                            holders.get("provider") if isinstance(holders, dict) else None
                        ) or "solana_holder_provider"
                        token.onchain.data_available = True
                holder_rows = holder_data if isinstance(holder_data, list) else (
                    holder_data.get("items", holder_data.get("list", []))
                    if isinstance(holder_data, dict) else []
                )
                balances = [decimal_value(row, "amount", "balance", "quantity")
                            for row in holder_rows if isinstance(row, dict)]
                balances = [value for value in balances
                            if value is not None and value.is_finite() and value >= 0]
                if (supply and supply > 0 and balances
                        and token.onchain.top10_wallet_pct is None
                        and token.onchain.top20_wallet_pct is None
                        and token.onchain.top20_account_pct is None):
                    balances.sort(reverse=True)
                    token.onchain.top10_wallet_pct = float(min(
                        Decimal("100"), sum(balances[:10]) / supply * 100
                    ))
                    token.onchain.top20_wallet_pct = float(min(
                        Decimal("100"), sum(balances[:20]) / supply * 100
                    ))
                    token.onchain.holder_concentration_source = (
                        holders.get("provider") if isinstance(holders, dict) else None
                    ) or "solscan_holders"
                    token.onchain.data_available = True

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
        await self.social_monitor.close()
        await self.hummingbot.close()
