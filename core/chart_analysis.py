"""
Chart Analysis Module

Provides expert-level chart reading capabilities:
- Candlestick pattern recognition (engulfing, doji, hammer, etc.)
- Support/resistance level detection
- Trend analysis using EMA crossovers
- RSI calculation
- MACD signals
- Bollinger Band analysis

This is the core of the "expert crypto trader" logic.
All analysis is done programmatically on OHLCV candle data.
"""

from dataclasses import dataclass, field
from typing import Optional
from utils.logger import get_logger

log = get_logger("chart_analysis")


@dataclass
class CandlestickPattern:
    """A detected candlestick pattern."""
    name: str
    pattern_type: str  # "bullish", "bearish", "neutral"
    significance: str  # Plain language explanation
    weight: float = 0.5  # Signal strength 0-1


@dataclass
class SupportResistance:
    """A support or resistance level."""
    price: float
    level_type: str  # "support" or "resistance"
    strength: int = 1  # How many times price touched this level
    description: str = ""


@dataclass
class ChartAnalysisResult:
    """Complete chart analysis output."""
    patterns: list[CandlestickPattern] = field(default_factory=list)
    support_levels: list[SupportResistance] = field(default_factory=list)
    resistance_levels: list[SupportResistance] = field(default_factory=list)
    trend: str = "neutral"  # "bullish", "bearish", "neutral"
    trend_strength: float = 0.0  # 0-1
    rsi: float = 50.0
    rsi_signal: str = ""
    ema_signal: str = ""
    macd_signal: str = ""
    bb_signal: str = ""
    overall_bias: str = "neutral"
    overall_confidence: float = 0.5


class ChartAnalyzer:
    """
    Expert-level chart analysis engine.

    Reads OHLCV candle data and produces the same kind of analysis
    a professional crypto trader would when looking at a chart.
    """

    def analyze(self, candles: list[dict]) -> ChartAnalysisResult:
        """
        Run full chart analysis on OHLCV candle data.

        candles: list of dicts with keys: open, high, low, close, volume
        Most recent candle should be LAST in the list.
        """
        result = ChartAnalysisResult()

        if not candles or len(candles) < 5:
            return result

        # Step 1: Detect candlestick patterns (last 3-5 candles)
        result.patterns = self._detect_candlestick_patterns(candles)

        # Step 2: Calculate support/resistance levels
        supports, resistances = self._find_support_resistance(candles)
        result.support_levels = supports
        result.resistance_levels = resistances

        # Step 3: Calculate RSI
        result.rsi = self._calculate_rsi(candles)
        result.rsi_signal = self._interpret_rsi(result.rsi)

        # Step 4: EMA crossover analysis
        result.ema_signal = self._ema_analysis(candles)

        # Step 5: MACD analysis
        result.macd_signal = self._macd_analysis(candles)

        # Step 6: Bollinger Band analysis
        result.bb_signal = self._bollinger_analysis(candles)

        # Step 7: Determine overall trend
        result.trend, result.trend_strength = self._determine_trend(candles)

        # Step 8: Combine into overall bias
        result.overall_bias, result.overall_confidence = self._combine_signals(result)

        return result

    def _detect_candlestick_patterns(self, candles: list[dict]) -> list[CandlestickPattern]:
        """
        Detect candlestick patterns in the most recent candles.

        Patterns detected:
        - Doji (indecision)
        - Hammer (bullish reversal)
        - Shooting Star (bearish reversal)
        - Bullish Engulfing (bullish reversal)
        - Bearish Engulfing (bearish reversal)
        - Morning Star (bullish reversal)
        - Evening Star (bearish reversal)
        - Three White Soldiers (strong bullish)
        - Three Black Crows (strong bearish)
        """
        patterns = []

        if len(candles) < 3:
            return patterns

        # Get recent candles
        c = candles[-1]   # Current candle
        p = candles[-2]   # Previous candle
        pp = candles[-3]  # Two candles ago

        c_open, c_high, c_low, c_close = c["open"], c["high"], c["low"], c["close"]
        p_open, p_high, p_low, p_close = p["open"], p["high"], p["low"], p["close"]
        pp_open, pp_close = pp["open"], pp["close"]

        c_body = abs(c_close - c_open)
        c_range = c_high - c_low if c_high > c_low else 0.001
        p_body = abs(p_close - p_open)
        p_range = p_high - p_low if p_high > p_low else 0.001

        c_bullish = c_close > c_open
        c_bearish = c_close < c_open
        p_bullish = p_close > p_open
        p_bearish = p_close < p_open

        # === DOJI ===
        # Very small body relative to range
        if c_body / c_range < 0.1:
            patterns.append(CandlestickPattern(
                name="Doji",
                pattern_type="neutral",
                significance="Market indecision. Neither buyers nor sellers are in control. "
                           "A reversal or continuation could follow depending on context.",
                weight=0.4,
            ))

        # === HAMMER (bullish reversal) ===
        # Small body at top, long lower shadow, little upper shadow
        if c_body / c_range < 0.3 and c_range > 0:
            lower_shadow = min(c_open, c_close) - c_low
            upper_shadow = c_high - max(c_open, c_close)
            if lower_shadow > c_body * 2 and upper_shadow < c_body * 0.5:
                patterns.append(CandlestickPattern(
                    name="Hammer",
                    pattern_type="bullish",
                    significance="Bullish reversal signal. Sellers pushed price down but buyers "
                               "fought back and closed near the high. Often marks a bottom.",
                    weight=0.65,
                ))

        # === SHOOTING STAR (bearish reversal) ===
        # Small body at bottom, long upper shadow, little lower shadow
        if c_body / c_range < 0.3 and c_range > 0:
            upper_shadow = c_high - max(c_open, c_close)
            lower_shadow = min(c_open, c_close) - c_low
            if upper_shadow > c_body * 2 and lower_shadow < c_body * 0.5:
                patterns.append(CandlestickPattern(
                    name="Shooting Star",
                    pattern_type="bearish",
                    significance="Bearish reversal signal. Buyers pushed price up but sellers "
                               "took control and closed near the low. Often marks a top.",
                    weight=0.65,
                ))

        # === BULLISH ENGULFING ===
        # Current bullish candle completely engulfs previous bearish candle
        if c_bullish and p_bearish:
            if c_open <= p_close and c_close >= p_open:
                patterns.append(CandlestickPattern(
                    name="Bullish Engulfing",
                    pattern_type="bullish",
                    significance="Strong bullish reversal. Current candle completely engulfs "
                               "the previous bearish candle, showing buyers have overwhelmed sellers.",
                    weight=0.75,
                ))

        # === BEARISH ENGULFING ===
        # Current bearish candle completely engulfs previous bullish candle
        if c_bearish and p_bullish:
            if c_open >= p_close and c_close <= p_open:
                patterns.append(CandlestickPattern(
                    name="Bearish Engulfing",
                    pattern_type="bearish",
                    significance="Strong bearish reversal. Current candle completely engulfs "
                               "the previous bullish candle, showing sellers have overwhelmed buyers.",
                    weight=0.75,
                ))

        # === MORNING STAR (3-candle bullish reversal) ===
        # Big bearish → small body (star) → big bullish
        pp_bearish = pp_close < pp_open
        pp_body = abs(pp_close - pp_open)
        if len(candles) >= 3 and pp_bearish and p_body < pp_body * 0.3 and c_bullish and c_body > pp_body * 0.5:
            patterns.append(CandlestickPattern(
                name="Morning Star",
                pattern_type="bullish",
                significance="Very strong bullish reversal pattern. Three candles show transition "
                           "from selling pressure to buying pressure. High probability bottom.",
                weight=0.8,
            ))

        # === EVENING STAR (3-candle bearish reversal) ===
        # Big bullish → small body (star) → big bearish
        pp_bullish = pp_close > pp_open
        if len(candles) >= 3 and pp_bullish and p_body < pp_body * 0.3 and c_bearish and c_body > pp_body * 0.5:
            patterns.append(CandlestickPattern(
                name="Evening Star",
                pattern_type="bearish",
                significance="Very strong bearish reversal pattern. Three candles show transition "
                           "from buying pressure to selling pressure. High probability top.",
                weight=0.8,
            ))

        # === THREE WHITE SOLDIERS ===
        if len(candles) >= 3:
            last3 = candles[-3:]
            if all(c["close"] > c["open"] for c in last3):
                bodies = [abs(c["close"] - c["open"]) for c in last3]
                if all(b > 0 for b in bodies) and bodies[2] >= bodies[0] * 0.5:
                    patterns.append(CandlestickPattern(
                        name="Three White Soldiers",
                        pattern_type="bullish",
                        significance="Three consecutive bullish candles. Strong uptrend "
                                   "confirmation. High probability of continued upward movement.",
                        weight=0.7,
                    ))

        # === THREE BLACK CROWS ===
        if len(candles) >= 3:
            last3 = candles[-3:]
            if all(c["close"] < c["open"] for c in last3):
                bodies = [abs(c["close"] - c["open"]) for c in last3]
                if all(b > 0 for b in bodies) and bodies[2] >= bodies[0] * 0.5:
                    patterns.append(CandlestickPattern(
                        name="Three Black Crows",
                        pattern_type="bearish",
                        significance="Three consecutive bearish candles. Strong downtrend "
                                   "confirmation. High probability of continued downward movement.",
                        weight=0.7,
                    ))

        return patterns

    def _find_support_resistance(self, candles: list[dict]) -> tuple[list, list]:
        """
        Find support and resistance levels from historical price action.

        Uses pivot point method: looks for local highs/lows where price
        reversed direction.
        """
        supports = []
        resistances = []

        if len(candles) < 10:
            return supports, resistances

        closes = [c["close"] for c in candles]
        highs = [c["high"] for c in candles]
        lows = [c["low"] for c in candles]
        current_price = closes[-1]

        # Find local minimums (support) and maximums (resistance)
        for i in range(2, len(candles) - 2):
            # Local minimum = support
            if lows[i] < lows[i-1] and lows[i] < lows[i-2] and lows[i] < lows[i+1] and lows[i] < lows[i+2]:
                # Count how many times price bounced from this level
                level = lows[i]
                touches = sum(1 for l in lows if abs(l - level) / level < 0.02)
                if level < current_price:
                    supports.append(SupportResistance(
                        price=level,
                        level_type="support",
                        strength=touches,
                        description=f"Price bounced from this level {touches} time(s)",
                    ))

            # Local maximum = resistance
            if highs[i] > highs[i-1] and highs[i] > highs[i-2] and highs[i] > highs[i+1] and highs[i] > highs[i+2]:
                level = highs[i]
                touches = sum(1 for h in highs if abs(h - level) / level < 0.02)
                if level > current_price:
                    resistances.append(SupportResistance(
                        price=level,
                        level_type="resistance",
                        strength=touches,
                        description=f"Price rejected from this level {touches} time(s)",
                    ))

        # Sort: closest support/resistance first
        supports.sort(key=lambda s: current_price - s.price)
        resistances.sort(key=lambda r: r.price - current_price)

        # Keep only top 3 of each
        return supports[:3], resistances[:3]

    def _calculate_rsi(self, candles: list[dict], period: int = 14) -> float:
        """Calculate RSI (Relative Strength Index)."""
        if len(candles) < period + 1:
            return 50.0

        closes = [c["close"] for c in candles]
        gains = []
        losses = []

        for i in range(1, len(closes)):
            change = closes[i] - closes[i-1]
            if change > 0:
                gains.append(change)
                losses.append(0)
            else:
                gains.append(0)
                losses.append(abs(change))

        # Use the last 'period' values
        recent_gains = gains[-period:]
        recent_losses = losses[-period:]

        avg_gain = sum(recent_gains) / period
        avg_loss = sum(recent_losses) / period

        if avg_loss == 0:
            return 100.0

        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return round(rsi, 1)

    def _interpret_rsi(self, rsi: float) -> str:
        """Interpret RSI value."""
        if rsi >= 80:
            return "Extremely overbought. Very high chance of a pullback."
        elif rsi >= 70:
            return "Overbought. Price may be due for a correction."
        elif rsi <= 20:
            return "Extremely oversold. Very high chance of a bounce."
        elif rsi <= 30:
            return "Oversold. Price may be due for a bounce."
        elif 45 <= rsi <= 55:
            return "Neutral. No clear momentum direction."
        elif rsi > 55:
            return "Bullish momentum. Buyers are in control."
        else:
            return "Bearish momentum. Sellers are in control."

    def _ema_analysis(self, candles: list[dict]) -> str:
        """Analyze EMA (Exponential Moving Average) crossovers."""
        if len(candles) < 26:
            return "Insufficient data for EMA analysis."

        closes = [c["close"] for c in candles]

        ema9 = self._calculate_ema(closes, 9)
        ema21 = self._calculate_ema(closes, 21)

        if ema9 is None or ema21 is None:
            return "Could not calculate EMAs."

        current_price = closes[-1]

        if ema9 > ema21 and current_price > ema9:
            return "Bullish. Price above both 9 and 21 EMA. Short-term trend is up."
        elif ema9 < ema21 and current_price < ema9:
            return "Bearish. Price below both 9 and 21 EMA. Short-term trend is down."
        elif ema9 > ema21:
            return "Mildly bullish. EMA9 above EMA21 but price pulling back."
        elif ema9 < ema21:
            return "Mildly bearish. EMA9 below EMA21 but price bouncing."
        else:
            return "EMAs converging. Trend change may be imminent."

    def _calculate_ema(self, values: list[float], period: int) -> Optional[float]:
        """Calculate EMA for a given period."""
        if len(values) < period:
            return None

        multiplier = 2 / (period + 1)
        ema = sum(values[:period]) / period

        for val in values[period:]:
            ema = (val - ema) * multiplier + ema

        return ema

    def _macd_analysis(self, candles: list[dict]) -> str:
        """Analyze MACD (Moving Average Convergence Divergence)."""
        if len(candles) < 26:
            return "Insufficient data for MACD."

        closes = [c["close"] for c in candles]

        ema12 = self._calculate_ema(closes, 12)
        ema26 = self._calculate_ema(closes, 26)

        if ema12 is None or ema26 is None:
            return "Could not calculate MACD."

        macd_line = ema12 - ema26

        # Simple signal interpretation
        if macd_line > 0:
            return "Bullish. MACD above zero line. Upward momentum."
        else:
            return "Bearish. MACD below zero line. Downward momentum."

    def _bollinger_analysis(self, candles: list[dict], period: int = 20) -> str:
        """Analyze Bollinger Bands."""
        if len(candles) < period:
            return "Insufficient data for Bollinger Bands."

        closes = [c["close"] for c in candles]
        recent = closes[-period:]
        current = closes[-1]

        sma = sum(recent) / period
        variance = sum((x - sma) ** 2 for x in recent) / period
        std_dev = variance ** 0.5

        upper_band = sma + 2 * std_dev
        lower_band = sma - 2 * std_dev

        if current >= upper_band:
            return "Price at upper Bollinger Band. Overbought, may pull back."
        elif current <= lower_band:
            return "Price at lower Bollinger Band. Oversold, may bounce."
        elif current > sma:
            return "Price above middle band. Mild bullish bias."
        else:
            return "Price below middle band. Mild bearish bias."

    def _determine_trend(self, candles: list[dict]) -> tuple[str, float]:
        """Determine overall trend from price action."""
        if len(candles) < 10:
            return "neutral", 0.5

        closes = [c["close"] for c in candles]
        recent = closes[-10:]

        # Simple trend: compare first half average to second half average
        first_half = sum(recent[:5]) / 5
        second_half = sum(recent[5:]) / 5

        if second_half == 0:
            return "neutral", 0.5

        change_pct = (second_half - first_half) / first_half * 100

        if change_pct > 5:
            return "bullish", min(0.5 + change_pct / 20, 0.9)
        elif change_pct < -5:
            return "bearish", min(0.5 + abs(change_pct) / 20, 0.9)
        elif change_pct > 1:
            return "mildly bullish", 0.55
        elif change_pct < -1:
            return "mildly bearish", 0.55
        else:
            return "neutral", 0.5

    def _combine_signals(self, result: ChartAnalysisResult) -> tuple[str, float]:
        """Combine all analysis into an overall bias and confidence."""
        bull_score = 0.0
        bear_score = 0.0

        # Candlestick patterns
        for p in result.patterns:
            if p.pattern_type == "bullish":
                bull_score += p.weight
            elif p.pattern_type == "bearish":
                bear_score += p.weight

        # RSI
        if result.rsi >= 70:
            bear_score += 0.4  # Overbought = bearish signal
        elif result.rsi <= 30:
            bull_score += 0.4  # Oversold = bullish signal
        elif result.rsi > 55:
            bull_score += 0.2
        elif result.rsi < 45:
            bear_score += 0.2

        # EMA
        if "Bullish" in result.ema_signal:
            bull_score += 0.3
        elif "Bearish" in result.ema_signal:
            bear_score += 0.3

        # MACD
        if "Bullish" in result.macd_signal:
            bull_score += 0.25
        elif "Bearish" in result.macd_signal:
            bear_score += 0.25

        # Bollinger
        if "Oversold" in result.bb_signal or "bounce" in result.bb_signal:
            bull_score += 0.2
        elif "Overbought" in result.bb_signal or "pull back" in result.bb_signal:
            bear_score += 0.2

        # Trend
        if "bullish" in result.trend:
            bull_score += 0.3 * result.trend_strength
        elif "bearish" in result.trend:
            bear_score += 0.3 * result.trend_strength

        # Support/resistance proximity
        # Near support = more likely to bounce (bullish)
        # Near resistance = more likely to reject (bearish)

        total = bull_score + bear_score
        if total == 0:
            return "neutral", 0.5

        if bull_score > bear_score:
            confidence = bull_score / total
            return "bullish", round(confidence, 2)
        elif bear_score > bull_score:
            confidence = bear_score / total
            return "bearish", round(confidence, 2)
        else:
            return "neutral", 0.5
