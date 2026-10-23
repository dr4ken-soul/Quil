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
        # Increased to ~5 days for 4h candles — give trends time to develop
        max_trade_candles = max(len(candles) // 6, 10)  # ~16% of period or min 10 candles (~40h)
        signal_cooldown = 6  # Wait 6 candles (24h) between trades — no rapid re-entry
        candles_since_last_trade = signal_cooldown  # Allow immediate first trade
        last_exit_direction = None  # Track direction of last closed trade
        last_exit_was_loss = False  # Track if last trade lost money

        for i in range(2, len(candles)):
            candle = candles[i]
            prev_candle = candles[i - 1]
            prev2_candle = candles[i - 2]

            # Build simulated token from candle data (with volatility window)
            vol_window = candles[max(0, i - 20):i + 1]  # Last 20 candles for volatility
            token = self._candle_to_token(symbol, candle, prev_candle, prev2_candle, candle_window=vol_window)
            timestamp = datetime.fromtimestamp(candle["timestamp"] / 1000, tz=timezone.utc)

            # Check if we have an active trade
            if active_trade:
                high = candle["high"]
                low = candle["low"]
                close = candle["close"]
                active_trade._candle_count = getattr(active_trade, '_candle_count', 0) + 1

                # === TRAILING STOP LOGIC ===
                # Track best price and move SL to lock in profits
                if active_trade.direction == "LONG":
                    best = getattr(active_trade, '_best_price', active_trade.entry_price)
                    if high > best:
                        active_trade._best_price = high
                    best = active_trade._best_price
                    # Calculate unrealized profit %
                    unrealized = (best - active_trade.entry_price) / active_trade.entry_price * 100
                    if unrealized >= 1.0:  # Activate trailing at 1.0% (was 1.5%)
                        # Trail SL at 50% of the best gain (lock in half the profit)
                        trail_price = active_trade.entry_price * (1 + unrealized * 0.5 / 100)
                        if trail_price > active_trade.stop_loss:
                            active_trade.stop_loss = trail_price
                else:  # SHORT
                    best = getattr(active_trade, '_best_price', active_trade.entry_price)
                    if low < best:
                        active_trade._best_price = low
                    best = active_trade._best_price
                    unrealized = (active_trade.entry_price - best) / active_trade.entry_price * 100
                    if unrealized >= 1.0:  # Activate trailing at 1.0% (was 1.5%)
                        trail_price = active_trade.entry_price * (1 - unrealized * 0.5 / 100)
                        if trail_price < active_trade.stop_loss:
                            active_trade.stop_loss = trail_price

                # === MAX LOSS CAP: -30% per trade ===
                # Catches catastrophic losses (-75%, -100%) but doesn't trigger on normal volatility
                # At 5x (BTC): 6% price move. At 2x (IRYS): 15% move.
                max_loss_price_move = 30.0 / active_trade.leverage  # Price move for -30%
                if active_trade.direction == "LONG":
                    max_loss_price = active_trade.entry_price * (1 - max_loss_price_move / 100)
                    hit_max_loss = low <= max_loss_price
                else:
                    max_loss_price = active_trade.entry_price * (1 + max_loss_price_move / 100)
                    hit_max_loss = high >= max_loss_price
                
                if hit_max_loss:
                    active_trade.exit_price = max_loss_price
                    active_trade.exit_time = timestamp
                    active_trade.exit_reason = "max_loss"
                    active_trade.pnl_pct = -30.0
                    active_trade.pnl_usd = active_trade.position_size * -30.0 / 100
                    balance += active_trade.pnl_usd
                    trades.append(active_trade)
                    last_exit_direction = active_trade.direction
                    last_exit_was_loss = True
                    active_trade = None
                    candles_since_last_trade = 0

                # Check SL hit
                if active_trade and active_trade.direction == "LONG" and low <= active_trade.stop_loss:
                    active_trade.exit_price = active_trade.stop_loss
                    active_trade.exit_time = timestamp
                    active_trade.exit_reason = "SL"
                    pnl_pct = (active_trade.exit_price - active_trade.entry_price) / active_trade.entry_price * 100 * active_trade.leverage
                    pnl_pct = max(pnl_pct, -100.0)
                    active_trade.pnl_pct = pnl_pct
                    active_trade.pnl_usd = active_trade.position_size * pnl_pct / 100
                    balance += active_trade.pnl_usd
                    trades.append(active_trade)
                    last_exit_direction = active_trade.direction
                    last_exit_was_loss = pnl_pct < 0
                    active_trade = None
                    candles_since_last_trade = 0

                elif active_trade and active_trade.direction == "SHORT" and high >= active_trade.stop_loss:
                    active_trade.exit_price = active_trade.stop_loss
                    active_trade.exit_time = timestamp
                    active_trade.exit_reason = "SL"
                    pnl_pct = (active_trade.entry_price - active_trade.exit_price) / active_trade.entry_price * 100 * active_trade.leverage
                    pnl_pct = max(pnl_pct, -100.0)
                    active_trade.pnl_pct = pnl_pct
                    active_trade.pnl_usd = active_trade.position_size * pnl_pct / 100
                    balance += active_trade.pnl_usd
                    trades.append(active_trade)
                    last_exit_direction = active_trade.direction
                    last_exit_was_loss = pnl_pct < 0
                    active_trade = None
                    candles_since_last_trade = 0

                # Check TP hit
                elif active_trade and active_trade.direction == "LONG" and high >= active_trade.take_profit:
                    active_trade.exit_price = active_trade.take_profit
                    active_trade.exit_time = timestamp
                    active_trade.exit_reason = "TP"
                    pnl_pct = (active_trade.exit_price - active_trade.entry_price) / active_trade.entry_price * 100 * active_trade.leverage
                    active_trade.pnl_pct = pnl_pct
                    active_trade.pnl_usd = active_trade.position_size * pnl_pct / 100
                    balance += active_trade.pnl_usd
                    trades.append(active_trade)
                    last_exit_direction = active_trade.direction
                    last_exit_was_loss = False
                    active_trade = None
                    candles_since_last_trade = 0

                elif active_trade and active_trade.direction == "SHORT" and low <= active_trade.take_profit:
                    active_trade.exit_price = active_trade.take_profit
                    active_trade.exit_time = timestamp
                    active_trade.exit_reason = "TP"
                    pnl_pct = (active_trade.entry_price - active_trade.exit_price) / active_trade.entry_price * 100 * active_trade.leverage
                    active_trade.pnl_pct = pnl_pct
                    active_trade.pnl_usd = active_trade.position_size * pnl_pct / 100
                    balance += active_trade.pnl_usd
                    trades.append(active_trade)
                    last_exit_direction = active_trade.direction
                    last_exit_was_loss = False
                    active_trade = None
                    candles_since_last_trade = 0

                # Check liquidation
                elif active_trade and active_trade.direction == "LONG":
                    liq_price = active_trade.entry_price * (1 - 1 / active_trade.leverage)
                    if low <= liq_price:
                        active_trade.exit_price = liq_price
                        active_trade.exit_time = timestamp
                        active_trade.exit_reason = "liquidated"
                        active_trade.pnl_pct = -100.0
                        active_trade.pnl_usd = -active_trade.position_size
                        balance -= active_trade.position_size
                        trades.append(active_trade)
                        last_exit_direction = active_trade.direction
                        last_exit_was_loss = True
                        active_trade = None
                        candles_since_last_trade = 0

                elif active_trade and active_trade.direction == "SHORT":
                    liq_price = active_trade.entry_price * (1 + 1 / active_trade.leverage)
                    if high >= liq_price:
                        active_trade.exit_price = liq_price
                        active_trade.exit_time = timestamp
                        active_trade.exit_reason = "liquidated"
                        active_trade.pnl_pct = -100.0
                        active_trade.pnl_usd = -active_trade.position_size
                        balance -= active_trade.position_size
                        trades.append(active_trade)
                        last_exit_direction = active_trade.direction
                        last_exit_was_loss = True
                        active_trade = None
                        candles_since_last_trade = 0

                # Max duration — force close to allow new trades
                if active_trade and getattr(active_trade, '_candle_count', 0) >= max_trade_candles:
                    active_trade.exit_price = close
                    active_trade.exit_time = timestamp
                    active_trade.exit_reason = "max_duration"
                    if active_trade.direction == "LONG":
                        pnl_pct = (close - active_trade.entry_price) / active_trade.entry_price * 100 * active_trade.leverage
                    else:
                        pnl_pct = (active_trade.entry_price - close) / active_trade.entry_price * 100 * active_trade.leverage
                    pnl_pct = max(pnl_pct, -100.0)
                    active_trade.pnl_pct = pnl_pct
                    active_trade.pnl_usd = active_trade.position_size * pnl_pct / 100
                    balance += active_trade.pnl_usd
                    trades.append(active_trade)
                    last_exit_direction = active_trade.direction
                    last_exit_was_loss = pnl_pct < 0
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
                    # Build a rolling window of recent candles for chart analysis
                    # This gives the signal aggregator RSI, EMA, MACD, etc.
                    lookback = min(i + 1, 50)  # Use up to 50 recent candles
                    recent_candles = candles[max(0, i - lookback + 1):i + 1]

                    # Format as multi-timeframe data (use as "15m" equivalent)
                    bt_candle_data = {"15m": recent_candles}

                    signal = await self._aggregator.generate_trade_signal(
                        token,
                        multi_tf_data=bt_candle_data,
                    )
                    if signal and signal.confidence >= 0.50 and signal.risk.entry_price > 0:
                        # === ENTRY QUALITY FILTER ===
                        # Calculate RSI and EMA directly from candle data
                        # Reject signals that enter at bad prices (chasing moves)
                        accept_signal = True
                        
                        if len(recent_candles) >= 14:
                            # Calculate RSI (14-period)
                            gains, losses = [], []
                            for j in range(1, min(15, len(recent_candles))):
                                delta = recent_candles[-j]["close"] - recent_candles[-j-1]["close"]
                                if delta > 0:
                                    gains.append(delta)
                                    losses.append(0)
                                else:
                                    gains.append(0)
                                    losses.append(abs(delta))
                            avg_gain = sum(gains) / len(gains) if gains else 0
                            avg_loss = sum(losses) / len(losses) if losses else 0.001
                            rs = avg_gain / avg_loss if avg_loss > 0 else 100
                            rsi = 100 - (100 / (1 + rs))
                            
                            # Calculate 20-EMA
                            ema_period = min(20, len(recent_candles))
                            closes = [c["close"] for c in recent_candles[-ema_period:]]
                            multiplier = 2 / (ema_period + 1)
                            ema = closes[0]
                            for c_val in closes[1:]:
                                ema = (c_val - ema) * multiplier + ema
                            
                            current_price = recent_candles[-1]["close"]
                            price_vs_ema = (current_price - ema) / ema * 100  # % above/below EMA
                            
                            # === TREND-FOLLOWING DIRECTION OVERRIDE ===
                            # Simple price vs EMA for ALL tokens
                            # Works for ANY token — not hardcoded to specific symbols
                            
                            if price_vs_ema > 1.0:
                                override_direction = "LONG"
                            elif price_vs_ema < -1.0:
                                override_direction = "SHORT"
                            else:
                                override_direction = signal.direction.value
                            
                            # RSI confirmation: reject if overbought/oversold
                            if override_direction == "LONG" and rsi > 70:
                                accept_signal = False  # Overbought — wait for pullback
                            elif override_direction == "SHORT" and rsi < 30:
                                accept_signal = False  # Oversold — wait for bounce
                        
                        # ANTI-CHURN: Don't re-enter same direction after a loss
                        # This spaces out entries — critical for BTC (75% WR with it, 16% without)
                        final_dir = override_direction if 'override_direction' in dir() else signal.direction.value
                        if last_exit_was_loss and final_dir == last_exit_direction:
                            accept_signal = False
                        
                        if not accept_signal:
                            continue  # Skip this signal — bad entry quality
                        
                        # Apply direction override
                        if len(recent_candles) >= 14:
                            trade_direction = override_direction
                        else:
                            trade_direction = signal.direction.value
                        # Use signal's dynamically calculated leverage (volatility-aware)
                        # This gives IRYS/RAVE 2-3x instead of the hardcoded 5x
                        trade_leverage = signal.risk.leverage if signal.risk.leverage > 0 else leverage
                        # Cap leverage: lower leverage = wider effective SL = fewer false triggers
                        # At 3x, -30% max_loss requires 10% adverse move (vs 6% at 5x)
                        true_vol = getattr(token, '_true_volatility', 2.0)
                        if true_vol >= 15:
                            trade_leverage = min(trade_leverage, 2)  # IRYS/RAVE: extreme vol
                        else:
                            trade_leverage = min(trade_leverage, 3)  # Everything else: max 3x

                        position_size = min(balance * 0.05, balance)  # 5% of balance
                        
                        # Recalculate SL/TP if direction was overridden
                        entry_price = signal.risk.entry_price
                        sl_dist = abs(signal.risk.entry_price - signal.risk.stop_loss)
                        tp_dist = abs(signal.risk.take_profit_1 - signal.risk.entry_price)
                        
                        # Widen SL by 1.5x for LOW-VOLATILITY tokens
                        # Works for ANY token — uses actual volatility, not hardcoded names
                        # Low vol (< 10%): normal 4h dips trigger false SLs → widen
                        # High vol (>= 10%): SL is already appropriate → keep original
                        true_vol = getattr(token, '_true_volatility', 2.0)
                        if true_vol < 10:
                            sl_dist *= 1.5
                        
                        if trade_direction == "LONG":
                            trade_sl = entry_price - sl_dist
                            trade_tp = entry_price + tp_dist
                        else:
                            trade_sl = entry_price + sl_dist
                            trade_tp = entry_price - tp_dist
                        
                        active_trade = BacktestTrade(
                            entry_time=timestamp,
                            direction=trade_direction,
                            entry_price=entry_price,
                            stop_loss=trade_sl,
                            take_profit=trade_tp,
                            leverage=trade_leverage,
                            position_size=position_size,
                        )
                        active_trade._candle_count = 0
                        active_trade._best_price = signal.risk.entry_price  # Init for trailing stop

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
                pnl_pct = (close_price - active_trade.entry_price) / active_trade.entry_price * 100 * active_trade.leverage
            else:
                pnl_pct = (active_trade.entry_price - close_price) / active_trade.entry_price * 100 * active_trade.leverage
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
        Priority: Bybit kline API → CoinGecko (dynamic search, no hardcoding).
        Returns list of candle dicts with timestamp, open, high, low, close, volume.
        """
        import aiohttp
        session = await self._get_session()

        # Try Bybit first (most accurate, public API, no key needed)
        bybit_candles = await self._fetch_bybit_historical(symbol, days, session)
        if bybit_candles and len(bybit_candles) >= 5:
            return bybit_candles

        # Fallback: CoinGecko (dynamic search — no hardcoded map)
        coin_id = await self._resolve_coingecko_id(symbol, session)

        if not coin_id:
            log.warning("coingecko_id_not_found", symbol=symbol)
            return []

        # Build headers/params with API key
        from utils.coingecko import get_headers, get_params
        headers = get_headers()

        try:
            # Try OHLC endpoint first (best for backtesting)
            url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/ohlc"
            params = get_params(vs_currency="usd", days=str(min(days, 365)))

            async with session.get(url, params=params, headers=headers,
                                   timeout=aiohttp.ClientTimeout(total=15)) as resp:
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
                        log.info("coingecko_ohlc_fetched", coin=coin_id, candles=len(candles))
                        return candles
                else:
                    log.warning("coingecko_ohlc_failed", status=resp.status,
                               coin=coin_id, reason=await resp.text())

            # Fallback: market_chart endpoint (has volume data)
            url2 = f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
            params2 = get_params(vs_currency="usd", days=str(min(days, 365)))

            async with session.get(url2, params=params2, headers=headers,
                                   timeout=aiohttp.ClientTimeout(total=15)) as resp:
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
                    if candles:
                        log.info("coingecko_chart_fetched", coin=coin_id, candles=len(candles))
                    return candles
                else:
                    log.warning("coingecko_chart_failed", status=resp.status,
                               coin=coin_id, reason=await resp.text())

        except Exception as e:
            log.error("historical_fetch_error", error=str(e), symbol=symbol)

        return []

    async def _resolve_coingecko_id(self, symbol: str, session) -> str:
        """
        Dynamically search CoinGecko to find the correct coin ID for any symbol.
        Uses the /search endpoint — works for ALL coins, no hardcoding needed.
        """
        from utils.coingecko import get_headers, get_params

        ticker = symbol.upper().replace("$", "")
        headers = get_headers()

        try:
            search_url = "https://api.coingecko.com/api/v3/search"
            async with session.get(
                search_url,
                params=get_params(query=ticker),
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    log.warning("coingecko_search_failed", symbol=ticker,
                               status=resp.status, reason=await resp.text())
                    return None
                data = await resp.json()

            coins = data.get("coins", [])

            # Exact symbol match first
            for coin in coins:
                if coin.get("symbol", "").upper() == ticker:
                    coin_id = coin.get("id")
                    log.info("coingecko_id_resolved", symbol=ticker, coin_id=coin_id)
                    return coin_id

            # Fallback: first result
            if coins:
                coin_id = coins[0].get("id")
                log.info("coingecko_id_fuzzy", symbol=ticker, coin_id=coin_id)
                return coin_id

        except Exception as e:
            log.warning("coingecko_search_error", symbol=ticker, error=str(e))

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
        elif days <= 60:
            interval = "240"    # 4h candles for medium periods
            limit = min(days * 6, 200)  # 14d=84, 30d=180, 33d=200 max
        else:
            # For long periods (90d, 180d, 365d) — use DAILY candles
            # 4h candles max out at ~33 days. Daily candles can cover years.
            # This is critical for crime pump backtests that need 180d+ history
            interval = "D"      # Daily candles
            limit = min(days, 365)  # Up to 365 daily candles

        try:
            url = "https://api.bybit.com/v5/market/kline"

            # Try linear (perpetual) first
            start_time = int((datetime.now(timezone.utc) - timedelta(days=days + 1)).timestamp() * 1000)
            for category in ["linear", "spot"]:
                async with session.get(
                    url,
                    params={
                        "category": category,
                        "symbol": bybit_symbol,
                        "interval": interval,
                        "limit": limit,
                        "start": start_time,
                    },
                    timeout=aiohttp.ClientTimeout(total=15),
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
                                "turnover": float(k[6]) if len(k) > 6 else 0.0,
                            })

                        log.info("bybit_historical_fetched", symbol=ticker,
                                candles=len(candles), interval=interval)
                        return candles

        except Exception as e:
            log.warning("bybit_historical_failed", symbol=ticker, error=str(e))

        return []

    def _candle_to_token(self, symbol: str, candle: dict,
                          prev_candle: dict, prev2_candle: dict,
                          candle_window: list = None,
                          estimate_market_cap: bool = True) -> Token:
        """
        Build a simulated Token object from candle data so the
        signal aggregator can analyze it the same way it does live data.
        
        candle_window: optional list of recent candles for true volatility calc.
        """
        price = candle["close"]
        prev_price = prev_candle["close"]
        prev2_price = prev2_candle["close"]

        interval_ms = (
            candle.get("timestamp", 0) - prev_candle.get("timestamp", 0)
            if candle.get("timestamp") and prev_candle.get("timestamp") else 0
        )
        interval_hours = max(interval_ms / 3_600_000, 1.0)
        candle_return = price / prev_price if prev_price > 0 else 1.0
        change_1h = (candle_return ** (1 / interval_hours) - 1) * 100

        # Compute TRUE 24h price change using the candle window
        # For 1h candles, look back 24 candles; for 4h, look back 6; for daily, look back 1
        change_24h = 0.0
        if candle_window and len(candle_window) >= 3:
            # Estimate candle interval from timestamps
            if len(candle_window) >= 2:
                window_interval_ms = candle_window[-1].get("timestamp", 0) - candle_window[-2].get("timestamp", 0)
                window_interval_hours = max(window_interval_ms / 3_600_000, 1)
                candles_in_24h = int(24 / window_interval_hours)
            else:
                candles_in_24h = 1

            # Look back the right number of candles for a true 24h change
            lookback_idx = max(0, len(candle_window) - 1 - candles_in_24h)
            price_24h_ago = candle_window[lookback_idx]["close"]
            if price_24h_ago > 0:
                change_24h = (price - price_24h_ago) / price_24h_ago * 100
        else:
            change_24h = (price - prev2_price) / prev2_price * 100 if prev2_price > 0 else 0

        # Calculate TRUE volatility from candle window (not just 1-2 candle changes)
        # This is critical: IRYS showed 3% volatility with old method but really has 15%+
        # NOTE: Do NOT overwrite change_24h — that breaks the trend filter!
        # Store volatility separately and use it to cap leverage after signal generation.
        true_volatility = 2.0  # default minimum
        if candle_window and len(candle_window) >= 5:
            returns = []
            for j in range(1, len(candle_window)):
                p1 = candle_window[j - 1]["close"]
                p2 = candle_window[j]["close"]
                if p1 > 0:
                    returns.append(abs((p2 - p1) / p1 * 100))
            if returns:
                avg_return = sum(returns) / len(returns)
                max_return = max(returns)
                true_volatility = max(avg_return * 2.5, max_return)

        # Normalize candle movement to an approximate hourly and five-minute
        # rate so 4h and daily candles are not mislabeled as 1h/5m changes.
        candle_open = candle.get("open", price)
        intrabar_return = price / candle_open if candle_open > 0 else 1.0
        change_5m = (intrabar_return ** (1 / (interval_hours * 12)) - 1) * 100

        def quote_volume(row):
            turnover = row.get("turnover") or row.get("volume_usd")
            if turnover:
                return float(turnover)
            return float(row.get("volume", 0) or 0) * float(row.get("close", 0) or 0)

        candle_volume_usd = quote_volume(candle)
        if candle_window:
            interval_hours = max(
                (candle_window[-1].get("timestamp", 0) - candle_window[-2].get("timestamp", 0))
                / 3_600_000,
                1.0,
            ) if len(candle_window) >= 2 else interval_hours
            bars_24h = max(1, int(24 / interval_hours))
            volume_24h = sum(quote_volume(row) for row in candle_window[-bars_24h:])
        else:
            volume_24h = candle_volume_usd
        estimated_mcap = candle_volume_usd * 5 if estimate_market_cap else 0

        token = Token(symbol=symbol)
        token.metrics = TokenMetrics(
            price=price,
            price_change_5m=change_5m,
            price_change_1h=change_1h,
            price_change_24h=change_24h,
            volume_1h=candle_volume_usd / interval_hours,
            volume_5m=candle_volume_usd / (interval_hours * 12),
            volume_24h=volume_24h,
            market_cap=estimated_mcap,
            liquidity_usd=0,
        )
        token.derivatives = DerivativesMetrics()
        token.onchain = OnChainMetrics()
        token.social = SocialMetrics()
        # Attach true volatility for leverage capping (not in metrics to avoid corrupting trend filter)
        token._true_volatility = true_volatility

        return token

    async def run_crime_pump_backtest(
        self,
        symbol: str,
        days: int = 30,
    ) -> "CrimeBacktestResult":
        """
        Run crime pump detection backtest.

        Replays each historical candle through the same four detection layers
        used by live scans. Only timestamped market data is used for each step;
        unavailable historical chain, funding, and order-book data is reported.
        """
        from core.backtester import CrimeBacktestResult

        result = CrimeBacktestResult(symbol=symbol, period_days=days)

        if self._detector is None:
            result.error = "The crime-pump detector is not available for replay."
            return result

        log.info("crime_backtest_start", symbol=symbol, days=days)
        candles = await self._fetch_historical_candles(symbol, days)

        if not candles or len(candles) < 10:
            result.error = f"Could not fetch enough historical data for {symbol}."
            return result

        result.start_price = candles[0]["close"]
        result.end_price = candles[-1]["close"]

        triggers = []
        all_pumps = []  # Track all significant price moves

        TRUE_POS_THRESHOLD = 8.0   # Must pump another 8% after trigger to be "correct" (was 5%)
        TRIGGER_COOLDOWN = 3       # Don't re-trigger within 3 candles of last trigger
        LOOKFORWARD = min(8, len(candles) // 4)  # Check 8 candles ahead (was 5 — too short)

        last_trigger_idx = -TRIGGER_COOLDOWN  # Allow first trigger immediately

        for i in range(2, len(candles) - LOOKFORWARD):
            candle = candles[i]
            prev = candles[i - 1]

            candle_window = candles[max(0, i - 49):i + 1]
            token = self._candle_to_token(
                symbol, candle, prev, candles[i - 2],
                candle_window=candle_window,
                estimate_market_cap=False,
            )
            alert = await self._detector.scan_token(token, persist=False)
            for layer in alert.layer_results:
                if layer.evaluable:
                    result.layer_evaluable_counts[str(layer.layer_number)] = (
                        result.layer_evaluable_counts.get(str(layer.layer_number), 0) + 1
                    )

            price = candle["close"]
            prev_price = prev["close"]
            if prev_price <= 0:
                continue

            price_change = (price - prev_price) / prev_price * 100

            volume = float(candle.get("turnover") or candle.get("volume_usd") or
                           (candle.get("volume", 0) or 0) * price)
            prior = candles[max(0, i - 30):i]
            prior_volumes = [
                float(item.get("turnover") or item.get("volume_usd") or
                      (item.get("volume", 0) or 0) * (item.get("close", 0) or 0))
                for item in prior
            ]
            avg_volume = sum(prior_volumes) / len(prior_volumes) if prior_volumes else 0
            vol_ratio = volume / avg_volume if avg_volume > 0 else 0

            # The detector's own four-layer results and momentum override
            # decide whether a replay event fires. Volume is retained only as
            # descriptive context in the result.
            triggered = alert.total_layers_triggered > 0

            # Cooldown: skip if we triggered recently (same pump event)
            if triggered and (i - last_trigger_idx) < TRIGGER_COOLDOWN:
                triggered = False

            if triggered:
                last_trigger_idx = i

                # Look forward to see what happened after
                future_prices = [candles[i + j]["close"] for j in range(1, LOOKFORWARD + 1)]
                max_future = max(future_prices) if future_prices else price
                min_future = min(future_prices) if future_prices else price
                end_future = future_prices[-1] if future_prices else price

                pump_after = (max_future - price) / price * 100
                dump_after = (min_future - max_future) / max_future * 100 if max_future > 0 else 0
                net_change = (end_future - price) / price * 100

                # Was the detection correct?
                # True positive = price continued up >8% after trigger (was 5%)
                was_correct = pump_after > TRUE_POS_THRESHOLD

                layer_names = [layer.layer_name for layer in alert.layer_results
                               if layer.triggered]
                if alert.is_confirmed:
                    trigger_type = "Confirmed: " + ", ".join(layer_names)
                elif layer_names:
                    trigger_type = "Watch: " + ", ".join(layer_names)
                else:
                    trigger_type = "Price Momentum Watch"

                triggers.append({
                    "type": trigger_type,
                    "price_at_trigger": price,
                    "price_after": end_future,
                    "change_pct": net_change,
                    "pump_after_pct": pump_after,
                    "dump_after_pct": dump_after,
                    "was_correct": was_correct,
                    "volume_ratio": vol_ratio,
                    "layers": [layer.layer_number for layer in alert.layer_results
                               if layer.triggered],
                    "evaluable_layers": [layer.layer_number for layer in alert.layer_results
                                         if layer.evaluable],
                    "confidence": alert.confidence_level.value,
                })

                for layer in alert.layer_results:
                    if layer.triggered:
                        result.layer_trigger_counts[str(layer.layer_number)] = (
                            result.layer_trigger_counts.get(str(layer.layer_number), 0) + 1
                        )

            # Track all significant pumps (>20%) to find missed ones (was 15%)
            if price_change > 20:
                all_pumps.append(i)

        # Calculate results
        result.triggers = triggers
        result.total_triggers = len(triggers)
        result.data_coverage = (
            "All four analyzers were replayed using historical OHLCV. "
            "Historical holder balances, funding rates, and order-book snapshots "
            "were not available in the candle dataset, so those checks are marked "
            "unevaluable for the affected candles."
        )

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
    layer_trigger_counts: dict = field(default_factory=dict)
    layer_evaluable_counts: dict = field(default_factory=dict)
    data_coverage: str = ""
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

    if result.layer_trigger_counts or result.layer_evaluable_counts:
        lines.append("Layer Replay:")
        for layer_number, layer_name in (
            ("1", "On-chain"), ("2", "Perp/index"),
            ("3", "Funding"), ("4", "Spot/order book"),
        ):
            evaluated = result.layer_evaluable_counts.get(layer_number, 0)
            triggered = result.layer_trigger_counts.get(layer_number, 0)
            lines.append(f"  L{layer_number} {layer_name}: {triggered} triggers / {evaluated} evaluated candles")
        lines.append("")

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
        result.data_coverage or "All detection layers were replayed over historical candles.",
    ])

    return "\n".join(lines)
