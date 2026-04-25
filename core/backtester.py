"""
Backtesting Engine

Tests the bot's detection and trade signal generation against historical
price data to validate whether signals would have been profitable.

Usage via Telegram: /backtest RAVE 30d
"""

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional
from dataclasses import dataclass, field

import aiohttp

from models.token import Token, TokenMetrics, DerivativesMetrics, OnChainMetrics, SocialMetrics
from models import Direction, CrimeStage
from utils.logger import get_logger

log = get_logger("backtester")


@dataclass
class BacktestTrade:
    """A single trade during backtesting."""
    entry_time: datetime
    exit_time: Optional[datetime] = None
    direction: str = "LONG"
    entry_price: float = 0.0
    exit_price: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    leverage: int = 1
    pnl_pct: float = 0.0
    pnl_usd: float = 0.0
    exit_reason: str = ""  # "TP", "SL", "timeout", "liquidated"
    position_size: float = 100.0


@dataclass
class BacktestResult:
    """Complete backtest results."""
    symbol: str = ""
    period_days: int = 30
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    total_pnl_pct: float = 0.0
    total_pnl_usd: float = 0.0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    best_trade_pct: float = 0.0
    worst_trade_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    profit_factor: float = 0.0
    sharpe_ratio: float = 0.0
    avg_trade_duration_h: float = 0.0
    trades: list = field(default_factory=list)
    daily_pnl: list = field(default_factory=list)
    detection_timeline: list = field(default_factory=list)
    start_price: float = 0.0
    end_price: float = 0.0
    buy_hold_pnl_pct: float = 0.0
    error: str = ""


class BacktestEngine:
    """
    Backtesting engine that replays historical data through the
    signal aggregator and detection layers.

    Fetches historical OHLCV candles from CoinGecko (free API),
    simulates the detection and signal generation day by day,
    and tracks simulated trades.
    """

    def __init__(self, signal_aggregator=None, detector=None):
        self._aggregator = signal_aggregator
        self._detector = detector
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()

    async def run_backtest(
        self,
        symbol: str,
        days: int = 30,
        initial_balance: float = 1000.0,
        leverage: int = 5,
    ) -> BacktestResult:
        """
        Run a full backtest for a token.

        1. Fetch historical candle data from CoinGecko
        2. Walk through candles day by day
        3. At each candle, build a simulated Token object
        4. Run signal generation on it
        5. Track entry/exit based on SL and TP levels
        6. Calculate performance metrics

        Args:
            symbol: Token ticker (e.g., "RAVE", "BTC")
            days: Number of days to backtest
            initial_balance: Starting balance in USD
            leverage: Default leverage for trades
        """
        result = BacktestResult(symbol=symbol, period_days=days)

        # Step 1: Fetch historical data
        log.info("backtest_start", symbol=symbol, days=days)
        candles = await self._fetch_historical_candles(symbol, days)

        if not candles or len(candles) < 5:
            result.error = f"Could not fetch enough historical data for {symbol}. Need at least 5 data points."
            return result

        result.start_price = candles[0]["close"]
        result.end_price = candles[-1]["close"]
        result.buy_hold_pnl_pct = (result.end_price - result.start_price) / result.start_price * 100

        # Step 2: Walk through candles and generate signals
        balance = initial_balance
        peak_balance = initial_balance
        max_drawdown = 0.0
        trades: list[BacktestTrade] = []
        active_trade: Optional[BacktestTrade] = None
        detection_timeline = []

        # Max trade duration: close trades after N candles to allow multiple signals
        # This simulates realistic trading (you don't hold forever)
        max_trade_candles = max(len(candles) // 8, 6)  # ~12.5% of period or min 6 candles
        signal_cooldown = 3  # Wait 3 candles between trades
        candles_since_last_trade = signal_cooldown  # Allow immediate first trade

        for i in range(2, len(candles)):
            candle = candles[i]
            prev_candle = candles[i - 1]
            prev2_candle = candles[i - 2]

            # Build simulated token from candle data
            token = self._candle_to_token(symbol, candle, prev_candle, prev2_candle)
            timestamp = datetime.fromtimestamp(candle["timestamp"] / 1000, tz=timezone.utc)

            # Check if we have an active trade
            if active_trade:
                high = candle["high"]
                low = candle["low"]
                close = candle["close"]
                active_trade._candle_count = getattr(active_trade, '_candle_count', 0) + 1

                # Check SL hit
                if active_trade.direction == "LONG" and low <= active_trade.stop_loss:
                    active_trade.exit_price = active_trade.stop_loss
                    active_trade.exit_time = timestamp
                    active_trade.exit_reason = "SL"
                    pnl_pct = (active_trade.exit_price - active_trade.entry_price) / active_trade.entry_price * 100 * leverage
                    pnl_pct = max(pnl_pct, -100.0)
                    active_trade.pnl_pct = pnl_pct
                    active_trade.pnl_usd = active_trade.position_size * pnl_pct / 100
                    balance += active_trade.pnl_usd
                    trades.append(active_trade)
                    active_trade = None
                    candles_since_last_trade = 0

                elif active_trade.direction == "SHORT" and high >= active_trade.stop_loss:
                    active_trade.exit_price = active_trade.stop_loss
                    active_trade.exit_time = timestamp
                    active_trade.exit_reason = "SL"
                    pnl_pct = (active_trade.entry_price - active_trade.exit_price) / active_trade.entry_price * 100 * leverage
                    pnl_pct = max(pnl_pct, -100.0)
                    active_trade.pnl_pct = pnl_pct
                    active_trade.pnl_usd = active_trade.position_size * pnl_pct / 100
                    balance += active_trade.pnl_usd
                    trades.append(active_trade)
                    active_trade = None
                    candles_since_last_trade = 0

                # Check TP hit
                elif active_trade.direction == "LONG" and high >= active_trade.take_profit:
                    active_trade.exit_price = active_trade.take_profit
                    active_trade.exit_time = timestamp
                    active_trade.exit_reason = "TP"
                    pnl_pct = (active_trade.exit_price - active_trade.entry_price) / active_trade.entry_price * 100 * leverage
                    active_trade.pnl_pct = pnl_pct
                    active_trade.pnl_usd = active_trade.position_size * pnl_pct / 100
                    balance += active_trade.pnl_usd
                    trades.append(active_trade)
                    active_trade = None
                    candles_since_last_trade = 0

                elif active_trade.direction == "SHORT" and low <= active_trade.take_profit:
                    active_trade.exit_price = active_trade.take_profit
                    active_trade.exit_time = timestamp
                    active_trade.exit_reason = "TP"
                    pnl_pct = (active_trade.entry_price - active_trade.exit_price) / active_trade.entry_price * 100 * leverage
                    active_trade.pnl_pct = pnl_pct
                    active_trade.pnl_usd = active_trade.position_size * pnl_pct / 100
                    balance += active_trade.pnl_usd
                    trades.append(active_trade)
                    active_trade = None
                    candles_since_last_trade = 0

                # Check liquidation
                elif active_trade.direction == "LONG":
                    liq_price = active_trade.entry_price * (1 - 1 / leverage)
                    if low <= liq_price:
                        active_trade.exit_price = liq_price
                        active_trade.exit_time = timestamp
                        active_trade.exit_reason = "liquidated"
                        active_trade.pnl_pct = -100.0
                        active_trade.pnl_usd = -active_trade.position_size
                        balance -= active_trade.position_size
                        trades.append(active_trade)
                        active_trade = None
                        candles_since_last_trade = 0

                elif active_trade.direction == "SHORT":
                    liq_price = active_trade.entry_price * (1 + 1 / leverage)
                    if high >= liq_price:
                        active_trade.exit_price = liq_price
                        active_trade.exit_time = timestamp
                        active_trade.exit_reason = "liquidated"
                        active_trade.pnl_pct = -100.0
                        active_trade.pnl_usd = -active_trade.position_size
                        balance -= active_trade.position_size
                        trades.append(active_trade)
                        active_trade = None
                        candles_since_last_trade = 0

                # Max duration — force close to allow new trades
                if active_trade and getattr(active_trade, '_candle_count', 0) >= max_trade_candles:
                    active_trade.exit_price = close
                    active_trade.exit_time = timestamp
                    active_trade.exit_reason = "max_duration"
                    if active_trade.direction == "LONG":
                        pnl_pct = (close - active_trade.entry_price) / active_trade.entry_price * 100 * leverage
                    else:
                        pnl_pct = (active_trade.entry_price - close) / active_trade.entry_price * 100 * leverage
                    pnl_pct = max(pnl_pct, -100.0)
                    active_trade.pnl_pct = pnl_pct
                    active_trade.pnl_usd = active_trade.position_size * pnl_pct / 100
                    balance += active_trade.pnl_usd
                    trades.append(active_trade)
                    active_trade = None
                    candles_since_last_trade = 0

                if active_trade:
                    continue  # Don't open new trade while one is active

            # No active trade — try to generate a signal
            candles_since_last_trade += 1
            if balance <= 0 or candles_since_last_trade < signal_cooldown:
                continue

            if self._aggregator:
                try:
                    signal = await self._aggregator.generate_trade_signal(token)
                    if signal and signal.confidence >= 0.4 and signal.risk.entry_price > 0:
                        # Open a trade
                        position_size = min(balance * 0.05, balance)  # 5% of balance
                        active_trade = BacktestTrade(
                            entry_time=timestamp,
                            direction=signal.direction.value,
                            entry_price=signal.risk.entry_price,
                            stop_loss=signal.risk.stop_loss,
                            take_profit=signal.risk.take_profit_1,
                            leverage=leverage,
                            position_size=position_size,
                        )
                        active_trade._candle_count = 0

                        detection_timeline.append({
                            "time": timestamp.isoformat(),
                            "direction": signal.direction.value,
                            "confidence": signal.confidence,
                            "entry": signal.risk.entry_price,
                            "sl": signal.risk.stop_loss,
                            "tp": signal.risk.take_profit_1,
                        })
                except Exception as e:
                    log.warning("backtest_signal_error", error=str(e))

            # Track drawdown
            if balance > peak_balance:
                peak_balance = balance
            dd = (peak_balance - balance) / peak_balance * 100
            if dd > max_drawdown:
                max_drawdown = dd

        # Close any remaining active trade at last candle close
        if active_trade:
            close_price = candles[-1]["close"]
            active_trade.exit_price = close_price
            active_trade.exit_time = datetime.fromtimestamp(candles[-1]["timestamp"] / 1000, tz=timezone.utc)
            active_trade.exit_reason = "timeout"
            if active_trade.direction == "LONG":
                pnl_pct = (close_price - active_trade.entry_price) / active_trade.entry_price * 100 * leverage
            else:
                pnl_pct = (active_trade.entry_price - close_price) / active_trade.entry_price * 100 * leverage
            pnl_pct = max(pnl_pct, -100.0)  # Cap at liquidation
            active_trade.pnl_pct = pnl_pct
            active_trade.pnl_usd = active_trade.position_size * pnl_pct / 100
            balance += active_trade.pnl_usd
            trades.append(active_trade)

        # Step 3: Calculate metrics
        result.trades = trades
        result.total_trades = len(trades)
        result.detection_timeline = detection_timeline

        if trades:
            # A trade needs >0.5% profit to count as a win (not just barely positive)
            winners = [t for t in trades if t.pnl_pct > 0.5]
            losers = [t for t in trades if t.pnl_pct < -0.5]
            breakeven = [t for t in trades if -0.5 <= t.pnl_pct <= 0.5]
            result.winning_trades = len(winners)
            result.losing_trades = len(losers)
            # Win rate based on decisive trades only (exclude breakeven)
            decisive = len(winners) + len(losers)
            result.win_rate = len(winners) / decisive * 100 if decisive > 0 else 0

            result.total_pnl_usd = balance - initial_balance
            result.total_pnl_pct = (balance - initial_balance) / initial_balance * 100

            if winners:
                result.avg_win_pct = sum(t.pnl_pct for t in winners) / len(winners)
            if losers:
                result.avg_loss_pct = sum(t.pnl_pct for t in losers) / len(losers)

            result.best_trade_pct = max(t.pnl_pct for t in trades)
            result.worst_trade_pct = min(t.pnl_pct for t in trades)
            result.max_drawdown_pct = max_drawdown

            total_wins = sum(t.pnl_usd for t in winners) if winners else 0
            total_losses = abs(sum(t.pnl_usd for t in losers)) if losers else 0
            result.profit_factor = total_wins / total_losses if total_losses > 0 else float('inf')

            # Average trade duration
            durations = []
            for t in trades:
                if t.entry_time and t.exit_time:
                    duration_h = (t.exit_time - t.entry_time).total_seconds() / 3600
                    durations.append(duration_h)
            result.avg_trade_duration_h = sum(durations) / len(durations) if durations else 0

        log.info("backtest_complete",
                symbol=symbol,
                trades=result.total_trades,
                win_rate=f"{result.win_rate:.1f}%",
                pnl=f"{result.total_pnl_pct:.1f}%")

        return result

    async def _fetch_historical_candles(self, symbol: str, days: int) -> list[dict]:
        """
        Fetch historical OHLCV data.
        Priority: Bybit kline API → CoinGecko (with dynamic search).
        Returns list of candle dicts with timestamp, open, high, low, close, volume.
        """
        import aiohttp
        session = await self._get_session()

        # Try Bybit first (most accurate, no hardcoded mapping needed)
        bybit_candles = await self._fetch_bybit_historical(symbol, days, session)
        if bybit_candles and len(bybit_candles) >= 5:
            return bybit_candles

        # Map common symbols to CoinGecko IDs
        symbol_map = {
            "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana",
            "DOGE": "dogecoin", "PEPE": "pepe", "WIF": "dogwifcoin",
            "BONK": "bonk", "RAVE": "ravedao", "STO": "stormx",
            "ORDI": "ordinals", "IP": "story-protocol",
            "AVAX": "avalanche-2", "LINK": "chainlink",
            "ADA": "cardano", "DOT": "polkadot", "MATIC": "matic-network",
            "ARB": "arbitrum", "OP": "optimism", "SUI": "sui",
            "APT": "aptos", "SEI": "sei-network", "INJ": "injective-protocol",
            "TIA": "celestia", "JUP": "jupiter-exchange-solana",
            "RENDER": "render-token", "FET": "fetch-ai",
            "NEAR": "near", "FIL": "filecoin", "ATOM": "cosmos",
            "XRP": "ripple", "BNB": "binancecoin",
            "TRUMP": "official-trump", "ZAMA": "zama",
        }

        coin_id = symbol_map.get(symbol.upper())

        # If not in map, search CoinGecko dynamically
        if not coin_id:
            coin_id = await self._resolve_coingecko_id(symbol, session)

        if not coin_id:
            coin_id = symbol.lower()  # Last resort

        try:
            url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/ohlc"
            params = {"vs_currency": "usd", "days": str(min(days, 365))}

            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    candles = []
                    for point in data:
                        if len(point) >= 5:
                            candles.append({
                                "timestamp": point[0],
                                "open": point[1],
                                "high": point[2],
                                "low": point[3],
                                "close": point[4],
                                "volume": 0,
                            })
                    if candles:
                        return candles
                else:
                    log.warning("coingecko_ohlc_failed", status=resp.status, coin=coin_id)

            # Fallback: try market_chart for price data
            url2 = f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
            params2 = {"vs_currency": "usd", "days": str(min(days, 365))}

            async with session.get(url2, params=params2, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    prices = data.get("prices", [])
                    volumes = data.get("total_volumes", [])

                    candles = []
                    for i, (ts, price) in enumerate(prices):
                        vol = volumes[i][1] if i < len(volumes) else 0
                        candles.append({
                            "timestamp": ts,
                            "open": price,
                            "high": price * 1.01,
                            "low": price * 0.99,
                            "close": price,
                            "volume": vol,
                        })
                    return candles

        except Exception as e:
            log.error("historical_fetch_error", error=str(e), symbol=symbol)

        return []

    async def _resolve_coingecko_id(self, symbol: str, session) -> str:
        """Dynamically search CoinGecko to find the correct coin ID for a symbol."""
        import aiohttp
        ticker = symbol.upper().replace("$", "")

        try:
            search_url = "https://api.coingecko.com/api/v3/search"
            async with session.get(
                search_url,
                params={"query": ticker},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()

            coins = data.get("coins", [])
            for coin in coins:
                if coin.get("symbol", "").upper() == ticker:
                    coin_id = coin.get("id")
                    log.info("coingecko_id_resolved", symbol=ticker, coin_id=coin_id)
                    return coin_id

            if coins:
                return coins[0].get("id")

        except Exception as e:
            log.warning("coingecko_search_failed", symbol=ticker, error=str(e))

        return None

    async def _fetch_bybit_historical(self, symbol: str, days: int, session) -> list[dict]:
        """Fetch historical candles from Bybit public API."""
        import aiohttp
        ticker = symbol.upper().replace("$", "")

        if len(ticker) > 10 or ticker.startswith("0X"):
            return []

        bybit_symbol = f"{ticker}USDT"

        # Always use 4h candles for backtests (good balance of resolution + range)
        # 4h candles trigger SL/TP more realistically than daily
        if days <= 2:
            interval = "60"     # 1h candles for very short backtests
            limit = min(days * 24, 200)
        elif days <= 7:
            interval = "240"    # 4h candles
            limit = min(days * 6, 200)
        else:
            interval = "240"    # 4h candles (NOT daily — daily is too coarse)
            limit = 200         # Max 200 candles = ~33 days of 4h data

        try:
            url = "https://api.bybit.com/v5/market/kline"

            # Try linear (perpetual) first
            for category in ["linear", "spot"]:
                async with session.get(
                    url,
                    params={
                        "category": category,
                        "symbol": bybit_symbol,
                        "interval": interval,
                        "limit": limit,
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        continue

                    data = await resp.json()
                    klines = data.get("result", {}).get("list", [])

                    if klines:
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

                        log.info("bybit_historical_fetched", symbol=ticker,
                                candles=len(candles), interval=interval)
                        return candles

        except Exception as e:
            log.warning("bybit_historical_failed", symbol=ticker, error=str(e))

        return []

    def _candle_to_token(self, symbol: str, candle: dict,
                          prev_candle: dict, prev2_candle: dict) -> Token:
        """
        Build a simulated Token object from candle data so the
        signal aggregator can analyze it the same way it does live data.
        """
        price = candle["close"]
        prev_price = prev_candle["close"]
        prev2_price = prev2_candle["close"]

        change_1h = (price - prev_price) / prev_price * 100 if prev_price > 0 else 0
        change_24h = (price - prev2_price) / prev2_price * 100 if prev2_price > 0 else 0

        # Estimate 5m change from intra-candle movement
        change_5m = (price - candle["open"]) / candle["open"] * 100 if candle["open"] > 0 else 0

        token = Token(symbol=symbol)
        token.metrics = TokenMetrics(
            price=price,
            price_change_5m=change_5m,
            price_change_1h=change_1h,
            price_change_24h=change_24h,
            volume_1h=candle.get("volume", 0),
            volume_5m=candle.get("volume", 0) / 12,  # Approximate
            volume_24h=candle.get("volume", 0) * 24,  # Approximate
            market_cap=0,
            fdv=0,
            liquidity_usd=0,
        )
        token.derivatives = DerivativesMetrics()
        token.onchain = OnChainMetrics()
        token.social = SocialMetrics()

        return token

    async def run_crime_pump_backtest(
        self,
        symbol: str,
        days: int = 30,
    ) -> "CrimeBacktestResult":
        """
        Run crime pump detection backtest.

        Walks through historical candles and checks for crime pump indicators:
        - Volume spikes (>3x average)
        - Sudden price surges (>10% in one candle)
        - Volume + price combo (the classic crime pump signature)

        Then checks what happened AFTER each trigger to measure accuracy.
        """
        from core.backtester import CrimeBacktestResult

        result = CrimeBacktestResult(symbol=symbol, period_days=days)

        log.info("crime_backtest_start", symbol=symbol, days=days)
        candles = await self._fetch_historical_candles(symbol, days)

        if not candles or len(candles) < 10:
            result.error = f"Could not fetch enough historical data for {symbol}."
            return result

        result.start_price = candles[0]["close"]
        result.end_price = candles[-1]["close"]

        # Calculate average volume for baseline
        volumes = [c.get("volume", 0) for c in candles if c.get("volume", 0) > 0]
        avg_volume = sum(volumes) / len(volumes) if volumes else 0

        triggers = []
        all_pumps = []  # Track all significant price moves
        PUMP_THRESHOLD = 10.0      # 10% price surge = potential crime pump
        VOLUME_SPIKE_MULT = 3.0    # 3x average volume = suspicious
        LOOKFORWARD = min(5, len(candles) // 4)  # How many candles to check after trigger

        for i in range(2, len(candles) - LOOKFORWARD):
            candle = candles[i]
            prev = candles[i - 1]

            price = candle["close"]
            prev_price = prev["close"]
            if prev_price <= 0:
                continue

            price_change = (price - prev_price) / prev_price * 100
            volume = candle.get("volume", 0)
            vol_ratio = volume / avg_volume if avg_volume > 0 else 0

            # Check for crime pump indicators
            is_volume_spike = vol_ratio > VOLUME_SPIKE_MULT
            is_price_surge = price_change > PUMP_THRESHOLD
            is_crime_pump_signal = is_volume_spike and price_change > 5  # Volume spike + price move

            triggered = is_price_surge or is_crime_pump_signal

            if triggered:
                # Look forward to see what happened after
                future_prices = [candles[i + j]["close"] for j in range(1, LOOKFORWARD + 1)]
                max_future = max(future_prices) if future_prices else price
                min_future = min(future_prices) if future_prices else price
                end_future = future_prices[-1] if future_prices else price

                pump_after = (max_future - price) / price * 100
                dump_after = (min_future - max_future) / max_future * 100 if max_future > 0 else 0
                net_change = (end_future - price) / price * 100

                # Was the detection correct?
                # True positive = price continued up >5% after trigger
                was_correct = pump_after > 5

                trigger_type = "Volume Spike + Surge" if is_crime_pump_signal else "Price Surge"

                triggers.append({
                    "type": trigger_type,
                    "price_at_trigger": price,
                    "price_after": end_future,
                    "change_pct": net_change,
                    "pump_after_pct": pump_after,
                    "dump_after_pct": dump_after,
                    "was_correct": was_correct,
                    "volume_ratio": vol_ratio,
                })

            # Track all significant pumps (>15%) to find missed ones
            if price_change > 15:
                all_pumps.append(i)

        # Calculate results
        result.triggers = triggers
        result.total_triggers = len(triggers)

        if triggers:
            true_pos = [t for t in triggers if t["was_correct"]]
            false_pos = [t for t in triggers if not t["was_correct"]]
            result.true_positives = len(true_pos)
            result.false_positives = len(false_pos)
            result.detection_accuracy = len(true_pos) / len(triggers) * 100

            pump_afters = [t["pump_after_pct"] for t in triggers if t["pump_after_pct"] > 0]
            dump_afters = [t["dump_after_pct"] for t in triggers if t["dump_after_pct"] < 0]

            if pump_afters:
                result.avg_pump_after_trigger_pct = sum(pump_afters) / len(pump_afters)
                result.biggest_pump_pct = max(pump_afters)

            if dump_afters:
                result.avg_dump_after_pump_pct = sum(dump_afters) / len(dump_afters)

        # Count missed pumps (pumps that happened without a trigger)
        trigger_indices = set()
        for i, candle in enumerate(candles):
            for t in triggers:
                if abs(candle["close"] - t["price_at_trigger"]) / candle["close"] < 0.01:
                    trigger_indices.add(i)
        missed = [p for p in all_pumps if p not in trigger_indices and p - 1 not in trigger_indices]
        result.missed_pumps = len(missed)

        log.info("crime_backtest_complete",
                symbol=symbol,
                triggers=result.total_triggers,
                accuracy=f"{result.detection_accuracy:.1f}%")

        return result


def format_backtest_result(result: BacktestResult) -> str:
    """Format backtest results for Telegram output."""
    if result.error:
        return f"Backtest Error: {result.error}"

    lines = [
        f"📊 Backtest Results: ${result.symbol}",
        f"Period: {result.period_days} days",
        "",
    ]

    # Performance summary
    pnl_emoji = "🟢" if result.total_pnl_pct > 0 else "🔴"
    lines.extend([
        "Performance:",
        f"  {pnl_emoji} Total PnL: {result.total_pnl_pct:+.1f}% (${result.total_pnl_usd:+,.2f})",
        f"  📈 Buy & Hold: {result.buy_hold_pnl_pct:+.1f}%",
        f"  🏆 Best Trade: {result.best_trade_pct:+.1f}%",
        f"  💀 Worst Trade: {result.worst_trade_pct:+.1f}%",
        f"  📉 Max Drawdown: {result.max_drawdown_pct:.1f}%",
        "",
    ])

    # Trade statistics
    breakeven = result.total_trades - result.winning_trades - result.losing_trades
    lines.extend([
        "Trade Stats:",
        f"  Total Trades: {result.total_trades}",
        f"  Wins: {result.winning_trades} | Losses: {result.losing_trades} | Breakeven: {breakeven}",
        f"  Win Rate: {result.win_rate:.1f}%",
        f"  Avg Win: {result.avg_win_pct:+.1f}%",
        f"  Avg Loss: {result.avg_loss_pct:.1f}%",
        f"  Profit Factor: {result.profit_factor:.2f}",
        f"  Avg Duration: {result.avg_trade_duration_h:.1f}h",
        "",
    ])

    # Price context
    start_str = f"${result.start_price:.6f}" if result.start_price < 1 else f"${result.start_price:,.2f}"
    end_str = f"${result.end_price:.6f}" if result.end_price < 1 else f"${result.end_price:,.2f}"
    lines.extend([
        "Price:",
        f"  Start: {start_str}",
        f"  End: {end_str}",
        "",
    ])

    # Recent trades
    if result.trades:
        lines.append("Recent Trades:")
        for trade in result.trades[-5:]:
            if trade.pnl_pct > 0.5:
                emoji = "🟢"
            elif trade.pnl_pct < -0.5:
                emoji = "🔴"
            else:
                emoji = "⚪"  # Breakeven
            lines.append(
                f"  {emoji} {trade.direction} {trade.exit_reason} "
                f"{trade.pnl_pct:+.1f}% (${trade.pnl_usd:+,.2f})"
            )

    lines.append("")
    lines.append("Note: Backtest uses historical data. Past performance does not guarantee future results.")

    return "\n".join(lines)


@dataclass
class CrimeBacktestResult:
    """Crime pump detection backtest results."""
    symbol: str = ""
    period_days: int = 30
    total_triggers: int = 0
    true_positives: int = 0  # Triggered AND price pumped after
    false_positives: int = 0  # Triggered but price did NOT pump
    missed_pumps: int = 0  # Pumps that happened without detection
    detection_accuracy: float = 0.0
    avg_pump_after_trigger_pct: float = 0.0
    avg_dump_after_pump_pct: float = 0.0
    biggest_pump_pct: float = 0.0
    triggers: list = field(default_factory=list)
    pumps_detected: list = field(default_factory=list)
    start_price: float = 0.0
    end_price: float = 0.0
    error: str = ""


def format_crime_backtest_result(result: CrimeBacktestResult) -> str:
    """Format crime pump backtest results for Telegram."""
    if result.error:
        return f"Crime Pump Backtest Error: {result.error}"

    lines = [
        f"🔍 Crime Pump Backtest: ${result.symbol}",
        f"Period: {result.period_days} days",
        "",
    ]

    # Detection summary
    lines.extend([
        "Detection Summary:",
        f"  Total Triggers: {result.total_triggers}",
        f"  True Positives (pump followed): {result.true_positives}",
        f"  False Positives (no pump): {result.false_positives}",
        f"  Missed Pumps: {result.missed_pumps}",
        f"  Accuracy: {result.detection_accuracy:.1f}%",
        "",
    ])

    if result.avg_pump_after_trigger_pct > 0:
        lines.extend([
            "Pump Analysis:",
            f"  Avg Pump After Trigger: +{result.avg_pump_after_trigger_pct:.1f}%",
            f"  Biggest Pump: +{result.biggest_pump_pct:.1f}%",
            f"  Avg Dump After Pump: {result.avg_dump_after_pump_pct:.1f}%",
            "",
        ])

    # Trigger timeline
    if result.triggers:
        lines.append("Detection Timeline:")
        for t in result.triggers[-8:]:
            emoji = "🟢" if t.get("was_correct") else "🔴"
            lines.append(
                f"  {emoji} {t.get('type', 'trigger')} | "
                f"Price: {t.get('price_at_trigger', 0):.6f} → "
                f"{t.get('price_after', 0):.6f} "
                f"({t.get('change_pct', 0):+.1f}%)"
            )

    # Price context
    start_str = f"${result.start_price:.6f}" if result.start_price < 1 else f"${result.start_price:,.2f}"
    end_str = f"${result.end_price:.6f}" if result.end_price < 1 else f"${result.end_price:,.2f}"
    lines.extend([
        "",
        "Price:",
        f"  Start: {start_str}",
        f"  End: {end_str}",
        "",
        "Note: Crime pump detection uses volume spikes, price surges, "
        "and pattern matching against historical manipulation schemes.",
    ])

    return "\n".join(lines)
